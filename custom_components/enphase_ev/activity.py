"""Describe material entity changes in Home Assistant Activity."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
import re
from typing import TYPE_CHECKING, Any, cast

from homeassistant.components import logbook
from homeassistant.const import EVENT_STATE_CHANGED, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, State
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .cloud_errors import cloud_error_code
from .entity import callback
from .labels import _entity_translation_value, _shared_label, status_label
from .log_redaction import redact_text

if TYPE_CHECKING:
    from .coordinator import EnphaseCoordinator
    from .runtime_data import EnphaseConfigEntry

MAX_MESSAGE_LENGTH = 1_000
MAX_DETAIL_ITEMS = 5
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_INVALID_STATES = {STATE_UNKNOWN, STATE_UNAVAILABLE}
_SITE_KINDS = {
    "active_system_events": "events",
    "service_status": "services",
    "cloud_reachable": "cloud",
    "last_error_code": "cloud",
    "gateway_connectivity_status": "gateway",
    "microinverter_connectivity_status": "inverters",
    "battery_overall_status": "batteries",
    "export_limit": "export",
    "heat_pump_status": "heatpump",
}
# Values are contextual; only these fields participate in comparison.
_FIELDS = {
    "charger": ("charger_problem", "suspended_by_evse"),
    "gateway": (
        "connected_devices",
        "disconnected_devices",
        "unknown_connection_devices",
        "connection_method",
    ),
    "inverters": (
        "reporting_inverters",
        "not_reporting_inverters",
        "unknown_inverters",
        "power_telemetry_status",
    ),
    "heatpump": (
        "sg_ready_mode_raw",
        "sg_ready_contact_state",
        "vpp_sgready_mode_override",
    ),
    "export": (
        "confirmed_watts",
        "request_status",
        "requested_action",
        "requested_watts",
    ),
}
_CONTEXT_FIELDS = {
    "charger": ("offline_since",),
    "gateway": ("latest_reported_utc",),
    "inverters": ("power_telemetry_next_retry",),
}


def _freeze(value: object) -> object:
    """Detach JSON-like values and ignore collection ordering."""
    if isinstance(value, Mapping):
        return tuple(sorted((str(key), _freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple, set)):
        return tuple(sorted((_freeze(item) for item in value), key=repr))
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return None


@dataclass(frozen=True, slots=True)
class ActivitySnapshot:
    """Immutable comparison and independently rendered event context."""

    signature: tuple[object, ...]
    fields: tuple[tuple[str, str], ...]
    items: tuple[str, ...] = ()


class ActivityPublisher:
    """Observe enabled entities without changing states or polling behavior."""

    def __init__(
        self, hass: HomeAssistant, entry: EnphaseConfigEntry, coord: EnphaseCoordinator
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.coord = coord
        self.registry = er.async_get(hass)
        self.baselines: dict[str, ActivitySnapshot] = {}
        self._battery_numbers: dict[str, int] = {}
        self._entity_keys: dict[str, str] = {}
        self._cloud_handle: asyncio.Handle | None = None
        self._cloud_emit = False
        self._stopped = False
        self._unsubscribers = [
            hass.bus.async_listen(EVENT_STATE_CHANGED, self._state_changed),
            hass.bus.async_listen(
                er.EVENT_ENTITY_REGISTRY_UPDATED, self._registry_changed
            ),
        ]
        self._unsubscribers.append(coord.async_add_listener(self._coordinator_updated))
        for registered in er.async_entries_for_config_entry(
            self.registry, entry.entry_id
        ):
            state = hass.states.get(registered.entity_id)
            if state is not None:
                self._observe(registered, state, emit=False)

    def _kind(self, registered: er.RegistryEntry) -> str | None:
        if (
            registered.config_entry_id != self.entry.entry_id
            or registered.platform != DOMAIN
            or registered.disabled_by is not None
        ):
            return None
        prefix = f"{DOMAIN}_site_{self.coord.site_id}_"
        unique = registered.unique_id
        if unique.startswith(prefix):
            suffix = unique.removeprefix(prefix)
            domain = (
                "binary_sensor"
                if suffix in {"active_system_events", "cloud_reachable"}
                else "sensor"
            )
            return _SITE_KINDS.get(suffix) if registered.domain == domain else None
        if registered.domain == "sensor" and unique in {
            f"{DOMAIN}_{serial}_status" for serial in self.coord.serials
        }:
            return "charger"
        return None

    @callback
    def _coordinator_updated(self) -> None:
        """Observe cloud evidence even when enabled entity states are unchanged."""
        if self._stopped:
            return
        records = [
            record
            for record in er.async_entries_for_config_entry(
                self.registry, self.entry.entry_id
            )
            if self._kind(record) == "cloud"
        ]
        # The error entity already emits category transitions. Only supplement
        # its disabled/absent case, preserving quiet counterpart discovery.
        if any(record.domain == "sensor" for record in records):
            return
        self._cloud_emit |= any(
            record.entity_id in self._entity_keys for record in records
        )
        if self._cloud_handle is None:
            self._cloud_handle = self.hass.loop.call_soon(self._flush_cloud)

    @callback
    def _registry_changed(self, event: Event[Any]) -> None:
        entity_id = event.data["entity_id"]
        old_entity_id = event.data.get("changes", {}).get("entity_id", entity_id)
        key = self._entity_keys.pop(old_entity_id, None)
        registered = self.registry.async_get(entity_id)
        if registered is not None and self._kind(registered) is not None:
            if key is not None:
                self._entity_keys[registered.entity_id] = key
            return
        if key is not None:
            self._discard_baseline(key, entity_id)

    def _discard_baseline(self, key: str, entity_id: str) -> None:
        if key != "cloud" or not any(
            item.entity_id != entity_id and self._kind(item) == "cloud"
            for item in er.async_entries_for_config_entry(
                self.registry, self.entry.entry_id
            )
        ):
            self.baselines.pop(key, None)

    @callback
    def _state_changed(self, event: Event[Any]) -> None:
        state: State | None = event.data.get("new_state")
        registered = self.registry.async_get(event.data["entity_id"])
        if self._stopped or registered is None or self._kind(registered) is None:
            return
        key = "cloud" if self._kind(registered) == "cloud" else registered.id
        if state is None:
            self._entity_keys.pop(registered.entity_id, None)
            self._discard_baseline(key, registered.entity_id)
            return
        if self._kind(registered) == "cloud":
            if state.state in _INVALID_STATES:
                return
            self._cloud_emit |= registered.entity_id in self._entity_keys
            self._entity_keys[registered.entity_id] = key
            if self._cloud_handle is None:
                self._cloud_handle = self.hass.loop.call_soon(self._flush_cloud)
            return
        self._observe(registered, state)

    @callback
    def _flush_cloud(self) -> None:
        self._cloud_handle = None
        emit, self._cloud_emit = self._cloud_emit, False
        records = [
            record
            for record in er.async_entries_for_config_entry(
                self.registry, self.entry.entry_id
            )
            if self._kind(record) == "cloud"
            and (state := self.hass.states.get(record.entity_id)) is not None
            and state.state not in _INVALID_STATES
        ]
        if not records:
            return
        previous = self.baselines.get("cloud")
        reachable = next((r for r in records if r.domain == "binary_sensor"), None)
        error = next((r for r in records if r.domain == "sensor"), None)
        record = reachable or error
        assert record is not None
        state = self.hass.states.get(record.entity_id)
        assert state is not None
        snapshot = self._snapshot("cloud", state, previous)
        if snapshot is None:
            return
        if previous is not None and error is not None:
            # Prefer the reachability entity only when its observed value changed.
            if previous.signature[0] == snapshot.signature[0]:
                record = error
                state = self.hass.states.get(record.entity_id)
                assert state is not None
        self._publish("cloud", state, snapshot, emit=emit)

    def _observe(
        self, record: er.RegistryEntry, state: State, *, emit: bool = True
    ) -> None:
        kind = self._kind(record)
        if kind is None or state.state in _INVALID_STATES:
            return
        key = "cloud" if kind == "cloud" else record.id
        self._entity_keys[record.entity_id] = key
        snapshot = self._snapshot(kind, state, self.baselines.get(key))
        if snapshot is not None:
            self._publish(key, state, snapshot, emit=emit)

    def _publish(
        self, key: str, state: State, snapshot: ActivitySnapshot, *, emit: bool = True
    ) -> None:
        previous = self.baselines.get(key)
        self.baselines[key] = snapshot
        if not emit or previous is None or previous.signature == snapshot.signature:
            return
        fields = [f"{self._label(key)}: {value}" for key, value in snapshot.fields]
        summary = (
            f"{self._label('more')}: {len(snapshot.items) - MAX_DETAIL_ITEMS}"
            if len(snapshot.items) > MAX_DETAIL_ITEMS
            else ""
        )
        details = snapshot.items[:MAX_DETAIL_ITEMS]
        if details:
            budget = max(
                1,
                (MAX_MESSAGE_LENGTH - len(". ".join(fields)) - len(summary) - 16)
                // len(details),
            )
            fields.extend(
                item if len(item) <= budget else item[: budget - 1] + "…"
                for item in details
            )
        message = ". ".join(fields)
        suffix = f". {summary}" if summary else ""
        if len(message) + len(suffix) > MAX_MESSAGE_LENGTH:
            message = message[: MAX_MESSAGE_LENGTH - len(suffix) - 1] + "…"
        message += suffix
        logbook.async_log_entry(
            self.hass, state.name, message, domain=DOMAIN, entity_id=state.entity_id
        )

    def _label(self, key: str) -> str:
        return str(
            _shared_label(f"activity_{key}", ACTIVITY_LABELS[key], hass=self.hass)
        )

    def _safe(self, value: object) -> str:
        identifiers = list(self.coord.serials) + list(self._battery_numbers)
        # Drop URLs completely, rather than recording private paths/query strings.
        text = str(value) if isinstance(value, (str, int, float)) else ""
        return redact_text(
            _URL_RE.sub("[redacted]", text),
            site_ids=(self.coord.site_id,),
            identifiers=identifiers,
            max_length=160,
        )

    def _value(self, value: object) -> str:
        if value is None or value == "":
            return self._label("unknown")
        if isinstance(value, bool):
            return self._label("yes" if value else "no")
        if isinstance(value, (int, float)):
            return str(value)
        if value == "Standing Alarm":
            return self._label("standing_alarm")
        text = self._safe(value)
        parsed = dt_util.parse_datetime(text) if isinstance(value, str) else None
        if parsed is None and isinstance(value, str):
            try:
                parsed = datetime.strptime(
                    text.split(" (", 1)[0], "%Y/%m/%d %H:%M:%S %z"
                )
            except ValueError:
                pass
        if parsed is not None and parsed.tzinfo is not None:
            return str(dt_util.as_local(parsed).strftime("%Y-%m-%d %H:%M:%S %Z"))
        return status_label(text, hass=self.hass) or text

    def _snapshot(
        self, kind: str, state: State, previous: ActivitySnapshot | None
    ) -> ActivitySnapshot | None:
        attrs = state.attributes
        status = self._value(
            ("error" if state.state == "on" else "normal")
            if kind == "events"
            else state.state
        )
        record = self.registry.async_get(state.entity_id)
        if record is not None and kind != "charger":
            suffix = record.unique_id.removeprefix(
                f"{DOMAIN}_site_{self.coord.site_id}_"
            )
            translated = _entity_translation_value(
                self.hass,
                record.domain,
                {
                    "service_status": "site_service_status",
                    "last_error_code": "cloud_error_code",
                }.get(suffix, suffix),
                f"state.{state.state}",
            )
            status = translated or status
        fields: list[tuple[str, str]] = [("status", status)]
        items: list[str] = []
        signature: tuple[object, ...]
        if kind == "cloud":
            return self._cloud_snapshot(previous)
        if kind == "events":
            runtime = getattr(self.coord, "system_events_runtime", None)
            rows = (
                runtime.activity_events
                if runtime is not None
                else attrs.get("active_events", ())
            )
            if not isinstance(rows, (list, tuple)):
                rows = ()
            comparisons = []
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                comparisons.append(
                    {
                        k: row.get(k)
                        for k in (
                            "type",
                            "device_type",
                            "state",
                            "severity",
                            "description",
                            "fingerprint",
                        )
                    }
                )
                items.append(
                    "; ".join(
                        f"{self._label(k)}: {self._value(row.get(k))}"
                        for k in (
                            "type",
                            "device_type",
                            "severity",
                            "description",
                            "event_date",
                        )
                        if row.get(k) is not None
                    )
                )
            fields.append(("active_count", self._value(attrs.get("active_count"))))
            signature = (state.state, attrs.get("active_count"), _freeze(comparisons))
        elif kind == "services":
            services = attrs.get("degraded_services", ())
            families = attrs.get("degraded_endpoint_families", ())
            details = attrs.get("endpoint_failure_details", {})
            if not isinstance(details, Mapping):
                details = {}
            health = self.coord.diagnostics.endpoint_family_health_diagnostics()
            reasons = {}
            for family in sorted(
                set(services if isinstance(services, (list, tuple)) else ())
                | set(families if isinstance(families, (list, tuple)) else ())
            ):
                detail = details.get(family, {})
                if not isinstance(detail, Mapping):
                    detail = {}
                family_health = health.get(family, {})
                reason = detail.get("reason")
                code = (
                    family_health.get("last_status")
                    if isinstance(family_health, Mapping)
                    else None
                )
                reasons[family] = (self._safe(reason), code)
                text = self._value(family)
                for key, value in (
                    ("reason", reason),
                    ("http_status", code),
                    ("retry", detail.get("retry_utc")),
                ):
                    if value is not None:
                        text += f"; {self._label(key)}: {self._value(value)}"
                items.append(text)
            signature = (
                state.state,
                _freeze(services),
                _freeze(families),
                _freeze(reasons),
            )
        elif kind == "batteries":
            statuses = attrs.get("per_battery_status", {})
            texts = attrs.get("per_battery_status_text", {})
            if not isinstance(statuses, Mapping):
                statuses = {}
            if not isinstance(texts, Mapping):
                texts = {}
            for key in sorted(set(statuses) | set(texts), key=str):
                identity = str(key)
                self._battery_numbers.setdefault(
                    identity, len(self._battery_numbers) + 1
                )
                name = self._battery_name(identity)
                items.append(
                    f"{name}: {self._value(statuses.get(key))}; {self._value(texts.get(key))}"
                )
            worst = attrs.get("worst_storage_key")
            if worst is not None:
                fields.append(("worst_battery", self._battery_name(str(worst))))
            signature = (state.state, _freeze(statuses), _freeze(texts), worst)
        else:
            values = {key: attrs.get(key) for key in _FIELDS[kind]}
            if kind == "export":
                # Readback clears pending intent. Carry its description into the outcome.
                if not attrs.get("pending"):
                    prior = dict(previous.fields) if previous is not None else {}
                    for key in ("requested_action", "requested_watts"):
                        fields.append((key, prior.get(key, self._label("unknown"))))
                        values.pop(key)
                signature = (
                    state.state,
                    _freeze(values),
                    attrs.get("pending_requested_at") if attrs.get("pending") else None,
                )
            else:
                signature = (state.state, _freeze(values))
            fields.extend(
                (key, self._field_value(record, key, value))
                for key, value in values.items()
            )
            fields.extend(
                (key, self._value(attrs.get(key)))
                for key in _CONTEXT_FIELDS.get(kind, ())
            )
        return ActivitySnapshot(signature, tuple(fields), tuple(sorted(items)))

    def _field_value(
        self, record: er.RegistryEntry | None, key: str, value: object
    ) -> str:
        if record is not None and isinstance(value, str):
            translation_key = record.unique_id.removeprefix(
                f"{DOMAIN}_site_{self.coord.site_id}_"
            )
            if self._kind(record) == "charger":
                translation_key = "status"
            translated = _entity_translation_value(
                self.hass,
                record.domain,
                translation_key,
                f"state_attributes.{key}.state.{value}",
            )
            if translated is not None:
                return translated
        return self._value(value)

    def _cloud_snapshot(
        self, previous: ActivitySnapshot | None
    ) -> ActivitySnapshot | None:
        records = [
            r
            for r in er.async_entries_for_config_entry(
                self.registry, self.entry.entry_id
            )
            if self._kind(r) == "cloud"
        ]
        reach: str | None = None
        error: str | None = None
        for record in records:
            state = self.hass.states.get(record.entity_id)
            if state is None or state.state in _INVALID_STATES:
                continue
            if record.domain == "binary_sensor":
                reach = state.state
            else:
                error = state.state
        if reach is None and error is None:
            return None
        # An unavailable counterpart must not become a new incident or recovery.
        if previous is not None:
            if reach is None:
                reach = cast(str | None, previous.signature[0])
            if error is None and any(r.domain == "sensor" for r in records):
                error = cast(str | None, previous.signature[1])
        last_success = self.coord.last_success_utc
        if error is None:
            error = cloud_error_code(self.coord)
        if last_success is None and error == "none":
            return None
        code = self.coord.last_failure_status if error not in (None, "none") else None
        fields = [
            ("reachable", self._value(None if reach is None else reach == "on")),
            (
                "reason",
                _entity_translation_value(
                    self.hass, "sensor", "cloud_error_code", f"state.{error}"
                )
                or self._value(error),
            ),
            ("http_status", self._value(code)),
            (
                "last_success",
                self._value(
                    last_success.isoformat()
                    if isinstance(last_success, datetime)
                    else None
                ),
            ),
        ]
        return ActivitySnapshot((reach, error, code), tuple(fields))

    def _battery_name(self, identity: str) -> str:
        number = self._battery_numbers.setdefault(
            identity, len(self._battery_numbers) + 1
        )
        device = dr.async_get(self.hass).async_get_device(
            identifiers={(DOMAIN, identity)}
        )
        if device is not None and (name := device.name_by_user or device.name):
            return self._safe(name)
        entity_id = self.registry.async_get_entity_id(
            "sensor",
            DOMAIN,
            f"{DOMAIN}_site_{self.coord.site_id}_battery_{identity}_status",
        )
        registered = (
            self.registry.async_get(entity_id) if entity_id is not None else None
        )
        if registered is not None and registered.name:
            return self._safe(registered.name)
        getter = getattr(self.coord, "battery_storage", None)
        snapshot = getter(identity) if callable(getter) else None
        if isinstance(snapshot, Mapping) and (name := snapshot.get("name")):
            if name not in (identity, snapshot.get("serial_number")):
                return self._safe(name)
        return f"{self._label('battery')} {number}"

    @callback
    def stop(self) -> None:
        """Detach the observer, including queued cloud coalescing."""
        self._stopped = True
        if self._cloud_handle is not None:
            self._cloud_handle.cancel()
            self._cloud_handle = None
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        self.baselines.clear()
        self._entity_keys.clear()


ACTIVITY_LABELS = {
    "standing_alarm": "Standing alarm",
    "status": "Status",
    "unknown": "Unknown",
    "yes": "Yes",
    "no": "No",
    "more": "More items",
    "battery": "Battery",
    "charger_problem": "Reported fault",
    "suspended_by_evse": "Suspended by charger",
    "offline_since": "Offline since",
    "connected_devices": "Connected gateways",
    "disconnected_devices": "Disconnected gateways",
    "unknown_connection_devices": "Unknown gateways",
    "connection_method": "Connection method",
    "latest_reported_utc": "Last reported",
    "reporting_inverters": "Reporting inverters",
    "not_reporting_inverters": "Non-reporting inverters",
    "unknown_inverters": "Unknown inverters",
    "power_telemetry_status": "Power telemetry",
    "power_telemetry_next_retry": "Telemetry retry",
    "sg_ready_mode_raw": "SG Ready mode",
    "sg_ready_contact_state": "Contact state",
    "vpp_sgready_mode_override": "Reported VPP override",
    "confirmed_watts": "Confirmed limit (W)",
    "requested_watts": "Requested limit (W)",
    "requested_action": "Requested action",
    "request_status": "Request status",
    "active_count": "Active problems",
    "type": "Event type",
    "device_type": "Device type",
    "severity": "Severity",
    "description": "Description",
    "event_date": "First reported",
    "reason": "Reason",
    "http_status": "HTTP status",
    "retry": "Next retry",
    "worst_battery": "Worst battery status",
    "reachable": "Cloud reachable",
    "last_success": "Last successful refresh",
}
