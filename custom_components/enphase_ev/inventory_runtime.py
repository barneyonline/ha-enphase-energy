"""Maintain Enphase inventory topology and diagnostics payload caches."""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from homeassistant.core import callback
from homeassistant.util import dt as dt_util

from .api import OptionalEndpointUnavailable
from .const import DEFAULT_FAST_POLL_INTERVAL, DOMAIN
from .device_types import (
    member_is_retired as device_member_is_retired,
    normalize_type_key,
    sanitize_member,
    type_display_label,
)
from .log_redaction import redact_site_id, redact_text
from .payload_debug import debug_field_keys, debug_render_summary, debug_sorted_keys
from .parsing_helpers import (
    coerce_optional_bool,
    coerce_optional_text,
    heatpump_member_device_type,
    heatpump_status_text,
    parse_inverter_last_report,
    type_member_text,
)
from .runtime_helpers import (
    coerce_int,
    copy_diagnostics_value,
    normalize_iso_date,
    redact_battery_payload,
    resolve_inverter_start_date,
    resolve_site_local_current_date,
    resolve_site_timezone_name,
)
from .state_models import install_state_descriptors
from .system_dashboard_helpers import (
    build_system_dashboard_summaries,
    system_dashboard_battery_detail_subset,
    system_dashboard_detail_records,
    system_dashboard_meter_kind,
    system_dashboard_type_key,
)

if TYPE_CHECKING:  # pragma: no cover
    from .coordinator import EnphaseCoordinator

_LOGGER = logging.getLogger(__name__)

DEVICES_INVENTORY_CACHE_TTL = 600.0
HEMS_INVENTORY_ENDPOINT_FAMILY = "hems_inventory"
HEMS_DEVICES_STALE_AFTER_S = 90.0
HEMS_DEVICES_CACHE_TTL = 60.0
# System-dashboard detail calls are fan-out requests against the same cloud service.
SYSTEM_DASHBOARD_DETAIL_CONCURRENCY = 3
SYSTEM_DASHBOARD_DIAGNOSTIC_TYPES: tuple[str, ...] = (
    "envoys",
    "meters",
    "enpowers",
    "encharges",
    "modems",
    "inverters",
)


@dataclass(frozen=True)
class CoordinatorTopologySnapshot:
    charger_serials: tuple[str, ...]
    battery_serials: tuple[str, ...]
    ac_battery_serials: tuple[str, ...]
    inverter_serials: tuple[str, ...]
    active_type_keys: tuple[str, ...]
    gateway_iq_router_keys: tuple[str, ...]
    inventory_ready: bool


class InventoryRuntime:
    """Inventory, topology, and system-dashboard runtime helpers."""

    def __init__(self, coordinator: EnphaseCoordinator) -> None:
        self.coordinator = coordinator
        self.discovery_state = coordinator.discovery_state
        self.refresh_state = coordinator.refresh_state
        self.inventory_state = coordinator.inventory_state
        self.heatpump_state = coordinator.heatpump_state

    def _set_shared_state_attr(self, name: str, value: object) -> None:
        object.__setattr__(self, name, value)
        setattr(self.coordinator, name, value)

    def _update_shared_state(self, **values: object) -> None:
        for name, value in values.items():
            self._set_shared_state_attr(name, value)

    def _coordinator_backed_attr(self, name: str, default: object = None) -> object:
        if name in self.__dict__:
            return self.__dict__[name]
        return getattr(self.coordinator, name, default)

    @property
    def client(self):
        return self.coordinator.client

    @property
    def site_id(self) -> str:
        return self.coordinator.site_id

    @property
    def include_inverters(self) -> bool:
        return self.coordinator.include_inverters

    def iter_serials(self) -> list[str]:
        return self.coordinator.iter_serials()

    def iter_battery_serials(self) -> list[str]:
        return self.coordinator.iter_battery_serials()

    def iter_type_keys(self) -> list[str]:
        return self.coordinator.inventory_view.iter_type_keys()

    def gateway_iq_energy_router_records(self) -> list[dict[str, object]]:
        return self.coordinator.inventory_view.gateway_iq_energy_router_records()

    def type_bucket(self, type_key: object) -> dict[str, object] | None:
        return self.coordinator.inventory_view.type_bucket(type_key)

    def _type_member_text(self, member: dict[str, object], *keys: str) -> str | None:
        return type_member_text(member, *keys)

    def _gateway_member_ip_address(self, member: dict[str, object]) -> str | None:
        return self._type_member_text(member, "ip", "ip_address", "ip-address")

    def _gateway_member_is_not_primary_gateway(self, member: dict[str, object]) -> bool:
        member_kind = self.coordinator.inventory_view._envoy_member_kind(member)
        return member_kind in {"production", "consumption", "controller"}

    def _gateway_member_preferred_for_ip(self, member: dict[str, object]) -> bool:
        if self._gateway_member_is_not_primary_gateway(member):
            return False
        name = (self._type_member_text(member, "name") or "").lower()
        if "gateway" in name:
            return True
        return any(
            member.get(key) is not None
            for key in (
                "envoy_sw_version",
                "ap_mode",
                "supportsEntrez",
                "show_connection_details",
            )
        )

    def _gateway_summary_ip_address(
        self,
        members: list[dict[str, object]],
        dashboard_envoy: object,
    ) -> str | None:
        candidate_members = list(members)
        if isinstance(dashboard_envoy, dict):
            candidate_members.append(dashboard_envoy)
        for member in candidate_members:
            if self._gateway_member_preferred_for_ip(member):
                ip_address = self._gateway_member_ip_address(member)
                if ip_address:
                    return ip_address
        for member in candidate_members:
            ip_address = self._gateway_member_ip_address(member)
            if ip_address and not self._gateway_member_is_not_primary_gateway(member):
                return ip_address
        return None

    def _coerce_optional_text(self, value: object) -> str | None:
        return coerce_optional_text(value)

    def _coerce_optional_bool(self, value: object) -> bool | None:
        return coerce_optional_bool(value)

    def _coerce_int(self, value: object, *, default: int = 0) -> int:
        return coerce_int(value, default=default)

    def _copy_diagnostics_value(self, value: object) -> object:
        return copy_diagnostics_value(value)

    def _normalize_iso_date(self, value: object) -> str | None:
        return normalize_iso_date(value)

    def _site_local_current_date(self) -> str:
        return resolve_site_local_current_date(
            getattr(self, "_devices_inventory_payload", None),
            getattr(self.coordinator, "_battery_timezone", None),
        )

    def _seconds_until_next_site_local_day(self) -> float:
        tz_name = resolve_site_timezone_name(
            getattr(self.coordinator, "_battery_timezone", None)
        )
        now_local = datetime.now(ZoneInfo(tz_name))
        next_midnight = datetime.combine(
            now_local.date() + timedelta(days=1),
            datetime.min.time(),
            tzinfo=now_local.tzinfo,
        )
        return max(1.0, (next_midnight - now_local).total_seconds())

    def _redact_battery_payload(self, payload: object) -> object:
        return redact_battery_payload(payload)

    def _hems_refresh_floor_s(self) -> float:
        runtime = getattr(self.coordinator, "heatpump_runtime", None)
        helper = getattr(runtime, "hems_refresh_floor_s", None)
        if callable(helper):
            try:
                return max(float(helper()), float(DEFAULT_FAST_POLL_INTERVAL))
            except (TypeError, ValueError):
                pass
        return float(DEFAULT_FAST_POLL_INTERVAL)

    def _hems_devices_cache_ttl_s(self) -> float:
        return max(HEMS_DEVICES_CACHE_TTL, self._hems_refresh_floor_s())

    @staticmethod
    def _debug_sorted_keys(value: object) -> list[str]:
        return debug_sorted_keys(value)

    @classmethod
    def _debug_field_keys(cls, members: object) -> list[str]:
        return debug_field_keys(members)

    @staticmethod
    def _debug_render_summary(summary: object) -> str:
        return debug_render_summary(summary)

    def _debug_log_summary_if_changed(
        self, summary_key: str, log_label: str, summary: object
    ) -> None:
        if not _LOGGER.isEnabledFor(logging.DEBUG):
            return
        cached = self._debug_summary_log_cache.get(summary_key)
        if cached == summary:
            return
        self._debug_summary_log_cache[summary_key] = self._copy_diagnostics_value(
            summary
        )
        _LOGGER.debug("%s: %s", log_label, self._debug_render_summary(summary))

    def _debug_devices_inventory_summary(
        self,
        grouped: dict[str, dict[str, object]],
        ordered_keys: list[str],
    ) -> dict[str, object]:
        types: dict[str, dict[str, object]] = {}
        for type_key in ordered_keys:
            bucket = grouped.get(type_key)
            if not isinstance(bucket, dict):
                continue
            members = bucket.get("devices")
            count = self._coerce_int(bucket.get("count"), default=0)
            summary: dict[str, object] = {
                "count": max(count, len(members) if isinstance(members, list) else 0),
                "field_keys": self._debug_field_keys(members),
            }
            status_counts = bucket.get("status_counts")
            if isinstance(status_counts, dict) and status_counts:
                summary["status_counts"] = {
                    str(key): self._coerce_int(value, default=0)
                    for key, value in status_counts.items()
                }
            device_type_counts = bucket.get("device_type_counts")
            if isinstance(device_type_counts, dict) and device_type_counts:
                summary["device_type_counts"] = {
                    str(key): self._coerce_int(value, default=0)
                    for key, value in device_type_counts.items()
                }
            types[type_key] = summary
        return {
            "ordered_type_keys": list(ordered_keys),
            "type_count": len(types),
            "types": types,
        }

    def _debug_hems_inventory_summary(self) -> dict[str, object]:
        grouped_devices = self._hems_grouped_devices()
        group_keys: set[str] = set()
        for grouped in grouped_devices:
            if not isinstance(grouped, dict):
                continue
            for key, value in grouped.items():
                if isinstance(value, list):
                    try:
                        key_text = str(key).strip()
                    except Exception:  # noqa: BLE001
                        continue
                    if key_text:
                        group_keys.add(key_text)

        gateway_members = self._hems_group_members("gateway")
        heatpump_members = self._hems_group_members(
            "heat-pump", "heat_pump", "heatpump"
        )
        heatpump_summary = self._build_heatpump_inventory_summary()
        router_records = self.gateway_iq_energy_router_summary_records()
        device_type_counts = heatpump_summary.get("device_type_counts")
        status_counts = heatpump_summary.get("status_counts")

        return {
            "site_supported": getattr(self.client, "hems_site_supported", None),
            "using_stale": bool(getattr(self, "_hems_devices_using_stale", False)),
            "group_keys": sorted(group_keys),
            "gateway_member_count": len(gateway_members),
            "gateway_field_keys": self._debug_field_keys(gateway_members),
            "heatpump_member_count": self._coerce_int(
                heatpump_summary.get("total_devices"),
                default=len(heatpump_members),
            ),
            "heatpump_field_keys": self._debug_field_keys(heatpump_members),
            "heatpump_device_type_counts": (
                dict(device_type_counts) if isinstance(device_type_counts, dict) else {}
            ),
            "heatpump_status_counts": (
                dict(status_counts) if isinstance(status_counts, dict) else {}
            ),
            "router_count": len(router_records),
        }

    def _debug_system_dashboard_summary(
        self,
        tree_payload: dict[str, object] | None,
        details_payloads: dict[str, dict[str, dict[str, object]]],
        type_summaries: dict[str, dict[str, object]],
        hierarchy_summary: dict[str, object],
    ) -> dict[str, object]:
        types: dict[str, dict[str, object]] = {}
        for canonical_type, payloads_by_source in details_payloads.items():
            records = self._system_dashboard_detail_records(
                payloads_by_source, *sorted(payloads_by_source)
            )
            raw_type_summary = type_summaries.get(canonical_type, {})
            type_summary = (
                raw_type_summary if isinstance(raw_type_summary, dict) else {}
            )
            summary: dict[str, object] = {
                "sources": sorted(payloads_by_source),
                "record_count": len(records),
                "field_keys": self._debug_field_keys(records),
            }
            hierarchy = type_summary.get("hierarchy")
            if isinstance(hierarchy, dict):
                summary["hierarchy_count"] = self._coerce_int(
                    hierarchy.get("count"), default=0
                )
            counts = type_summary.get("counts_by_type")
            if isinstance(counts, dict) and counts:
                summary["counts_by_type"] = {
                    str(key): self._coerce_int(value, default=0)
                    for key, value in counts.items()
                }
            status_counts = type_summary.get("status_counts")
            if isinstance(status_counts, dict) and status_counts:
                summary["status_counts"] = {
                    str(key): self._coerce_int(value, default=0)
                    for key, value in status_counts.items()
                }
            types[canonical_type] = summary

        hierarchy_counts = hierarchy_summary.get("counts_by_type")
        return {
            "tree_keys": self._debug_sorted_keys(tree_payload),
            "hierarchy_total_nodes": self._coerce_int(
                hierarchy_summary.get("total_nodes"), default=0
            ),
            "hierarchy_counts_by_type": (
                {
                    str(key): self._coerce_int(value, default=0)
                    for key, value in hierarchy_counts.items()
                }
                if isinstance(hierarchy_counts, dict)
                else {}
            ),
            "types": types,
        }

    def _debug_topology_summary(
        self, snapshot: CoordinatorTopologySnapshot
    ) -> dict[str, object]:
        return {
            "inventory_ready": bool(snapshot.inventory_ready),
            "devices_inventory_ready": bool(
                getattr(self, "_devices_inventory_ready", False)
            ),
            "hems_inventory_ready": bool(getattr(self, "_hems_inventory_ready", False)),
            "charger_count": len(snapshot.charger_serials),
            "battery_count": len(snapshot.battery_serials),
            "ac_battery_count": len(snapshot.ac_battery_serials),
            "inverter_count": len(snapshot.inverter_serials),
            "active_type_keys": list(snapshot.active_type_keys),
            "gateway_iq_router_count": len(snapshot.gateway_iq_router_keys),
            "site_energy_channels": sorted(
                self.coordinator.discovery_snapshot.live_site_energy_channels()
            ),
        }

    def _build_system_dashboard_summaries(
        self,
        tree_payload: dict[str, object] | None,
        details_payloads: dict[str, dict[str, dict[str, object]]],
    ) -> tuple[
        dict[str, dict[str, object]],
        dict[str, object],
        dict[str, dict[str, object]],
    ]:
        return build_system_dashboard_summaries(tree_payload, details_payloads)

    def _system_dashboard_type_key(self, raw_type: object) -> str | None:
        return system_dashboard_type_key(raw_type)

    def topology_snapshot(self) -> CoordinatorTopologySnapshot:
        """Return the latest cached topology snapshot."""
        return self._topology_snapshot_cache

    def gateway_inventory_summary(self) -> dict[str, object]:
        source = self._gateway_inventory_summary_marker()
        summary = getattr(self, "_gateway_inventory_summary_cache", {}) or {}
        if source != self._gateway_inventory_summary_source:
            summary = self._build_gateway_inventory_summary()
            self._gateway_inventory_summary_cache = summary
            self._gateway_inventory_summary_source = source
        return dict(summary)

    def microinverter_inventory_summary(self) -> dict[str, object]:
        source = self._microinverter_inventory_summary_marker()
        summary = getattr(self, "_microinverter_inventory_summary_cache", {}) or {}
        if source != self._microinverter_inventory_summary_source:
            summary = self._build_microinverter_inventory_summary()
            self._microinverter_inventory_summary_cache = summary
            self._microinverter_inventory_summary_source = source
        return dict(summary)

    def heatpump_inventory_summary(self) -> dict[str, object]:
        source = self._heatpump_inventory_summary_marker()
        summary = getattr(self, "_heatpump_inventory_summary_cache", {}) or {}
        if source != self._heatpump_inventory_summary_source:
            summary = self._build_heatpump_inventory_summary()
            self._heatpump_inventory_summary_cache = summary
            self._heatpump_inventory_summary_source = source
        return dict(summary)

    def heatpump_type_summary(self, device_type: str) -> dict[str, object]:
        try:
            normalized = str(device_type).strip().upper()
        except Exception:  # noqa: BLE001
            normalized = ""
        source = self._heatpump_inventory_summary_marker()
        summaries = getattr(self, "_heatpump_type_summaries_cache", {}) or {}
        if source != self._heatpump_type_summaries_source or (
            normalized and normalized not in summaries
        ):
            summaries = self._build_heatpump_type_summaries()
            self._heatpump_type_summaries_cache = summaries
            self._heatpump_type_summaries_source = source
        summary = summaries.get(normalized, {})
        return dict(summary) if isinstance(summary, dict) else {}

    def gateway_iq_energy_router_summary_records(self) -> list[dict[str, object]]:
        source = self._gateway_iq_energy_router_records_marker()
        records = getattr(self, "_gateway_iq_energy_router_records_cache", [])
        if source != self._gateway_iq_energy_router_records_source:
            records = self._gateway_iq_energy_router_summary_records(
                self.gateway_iq_energy_router_records()
            )
            self._gateway_iq_energy_router_records_cache = records
            self._gateway_iq_energy_router_records_source = source
            self._gateway_iq_energy_router_records_by_key_cache = {
                record["key"]: record
                for record in records
                if isinstance(record, dict) and isinstance(record.get("key"), str)
            }
        return [dict(record) for record in records if isinstance(record, dict)]

    @staticmethod
    def _router_record_key(record: object) -> str | None:
        if not isinstance(record, dict):
            return None
        key = record.get("key")
        if key is None:
            return None
        try:
            key_text = str(key).strip()
        except Exception:  # noqa: BLE001
            return None
        return key_text or None

    def gateway_iq_energy_router_record(
        self, router_key: object
    ) -> dict[str, object] | None:
        try:
            key = str(router_key).strip()
        except Exception:  # noqa: BLE001
            return None
        if not key:
            return None
        self.gateway_iq_energy_router_summary_records()
        record = getattr(
            self, "_gateway_iq_energy_router_records_by_key_cache", {}
        ).get(key)
        return dict(record) if isinstance(record, dict) else None

    def _current_topology_snapshot(self) -> CoordinatorTopologySnapshot:
        router_records = self.gateway_iq_energy_router_summary_records()
        router_keys = tuple(
            key
            for key in (self._router_record_key(record) for record in router_records)
            if key
        )
        return CoordinatorTopologySnapshot(
            charger_serials=tuple(self.iter_serials()),
            battery_serials=tuple(self.iter_battery_serials()),
            ac_battery_serials=tuple(
                getattr(self.coordinator, "iter_ac_battery_serials", lambda: [])()
            ),
            inverter_serials=tuple(self.iter_inverter_serials()),
            active_type_keys=tuple(self.iter_type_keys()),
            gateway_iq_router_keys=router_keys,
            inventory_ready=bool(
                getattr(self, "_devices_inventory_ready", False)
                or getattr(self, "_hems_inventory_ready", False)
            ),
        )

    @callback
    def _notify_topology_listeners(self) -> None:
        for listener in list(self._topology_listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001
                _LOGGER.debug(
                    "Topology listener failed for site %s",
                    redact_site_id(self.site_id),
                    exc_info=True,
                )

    @callback
    def _refresh_cached_topology(self) -> bool:
        if self._topology_refresh_suppressed > 0:
            # Batch refreshes coalesce Home Assistant entity-registry notifications.
            self._topology_refresh_pending = True
            return False
        try:
            self._rebuild_inventory_summary_caches()
            snapshot = self._current_topology_snapshot()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Skipping topology cache rebuild for site %s: %s",
                redact_site_id(self.site_id),
                redact_text(err, site_ids=(self.site_id,)),
            )
            return False
        if snapshot == self._topology_snapshot_cache:
            return False
        self._topology_snapshot_cache = snapshot
        self._debug_log_summary_if_changed(
            "topology",
            "Discovery topology summary updated",
            self._debug_topology_summary(snapshot),
        )
        self._notify_topology_listeners()
        return True

    @callback
    def _begin_topology_refresh_batch(self) -> None:
        self._topology_refresh_suppressed += 1

    @callback
    def _end_topology_refresh_batch(self) -> bool:
        if self._topology_refresh_suppressed > 0:
            self._topology_refresh_suppressed -= 1
        if self._topology_refresh_suppressed > 0 or not self._topology_refresh_pending:
            return False
        self._topology_refresh_pending = False
        override = getattr(self.coordinator, "__dict__", {}).get(
            "_refresh_cached_topology"
        )
        if callable(override):
            return bool(override())
        return self._refresh_cached_topology()

    async def async_ensure_system_dashboard_diagnostics(self) -> None:
        if (
            self._system_dashboard_type_summaries
            or self._system_dashboard_hierarchy_summary
        ):
            return
        override = getattr(self.coordinator, "__dict__", {}).get(
            "_async_refresh_system_dashboard"
        )
        if callable(override):
            await override(force=True)
            return
        await self._async_refresh_system_dashboard(force=True)

    @staticmethod
    async def _async_call_refreshable_fetcher(
        fetcher, *, force: bool = False
    ) -> object:
        if not force:
            return await fetcher()
        try:
            signature = inspect.signature(fetcher)
        except (TypeError, ValueError):
            signature = None
        if signature is not None:
            if "refresh_data" in signature.parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in signature.parameters.values()
            ):
                return await fetcher(refresh_data=True)
            return await fetcher()
        return await fetcher()

    def _parse_devices_inventory_payload(
        self, payload: object
    ) -> tuple[bool, dict[str, dict[str, object]], list[str]]:
        if isinstance(payload, list):
            result = payload
        elif isinstance(payload, dict):
            result = payload.get("result")
        else:
            return False, {}, []
        if not isinstance(result, list):
            return False, {}, []

        grouped: dict[str, dict[str, object]] = {}
        seen_per_type: dict[str, set[str]] = {}
        ordered_keys: list[str] = []

        def _clean_text(value: object) -> str | None:
            if value is None:
                return None
            try:
                text = str(value).strip()
            except Exception:  # noqa: BLE001
                return None
            return text or None

        def _dry_contact_member_dedupe_key(
            raw_type: object,
            member: dict[str, object],
            member_index: int,
        ) -> str:
            source_type = _clean_text(raw_type)
            serial = _clean_text(
                member.get("serial_number")
                if member.get("serial_number") is not None
                else (
                    member.get("serial")
                    if member.get("serial") is not None
                    else (
                        member.get("serialNumber")
                        if member.get("serialNumber") is not None
                        else member.get("device_sn")
                    )
                )
            )
            identity_parts: list[str] = []
            for key in (
                "device_uid",
                "device-uid",
                "uid",
                "contact_id",
                "contactId",
                "id",
                "channel_type",
                "channelType",
                "meter_type",
            ):
                value = _clean_text(member.get(key))
                if value is None:
                    continue
                identity_parts.append(f"{key}:{value}")
            if serial is not None:
                identity_parts.append(f"serial:{serial}")
            if identity_parts:
                if source_type is not None:
                    identity_parts.insert(0, f"source:{source_type}")
                return "|".join(identity_parts)

            fingerprint_parts: list[str] = []
            for key in sorted(member):
                value = member.get(key)
                if value is None or not isinstance(value, (str, int, float, bool)):
                    continue
                fingerprint_parts.append(f"{key}:{value}")
            if fingerprint_parts:
                fingerprint = "|".join(fingerprint_parts)
                if source_type is not None:
                    return f"source:{source_type}|{fingerprint}|idx:{member_index}"
                return f"{fingerprint}|idx:{member_index}"

            if source_type is not None:
                return f"source:{source_type}|idx:{member_index}"
            return f"idx:{member_index}:dry_contact"

        for bucket in result:
            if not isinstance(bucket, dict):
                continue
            raw_type = bucket.get("type")
            if raw_type is None:
                raw_type = bucket.get("deviceType")
            if raw_type is None:
                raw_type = bucket.get("device_type")
            type_key = normalize_type_key(raw_type)
            devices = bucket.get("devices")
            if not isinstance(devices, list):
                devices = bucket.get("items")
            if not isinstance(devices, list):
                devices = bucket.get("members")
            if not type_key or not isinstance(devices, list):
                continue
            if type_key not in grouped:
                grouped[type_key] = {
                    "type_key": type_key,
                    "type_label": type_display_label(type_key),
                    "count": 0,
                    "devices": [],
                }
                seen_per_type[type_key] = set()
                ordered_keys.append(type_key)
            members: list[dict[str, object]] = grouped[type_key]["devices"]  # type: ignore[assignment]
            seen_keys = seen_per_type[type_key]
            for member_index, member in enumerate(devices):
                if not isinstance(member, dict):
                    continue
                if self.member_is_retired(member):
                    continue
                sanitized = sanitize_member(member)
                if not sanitized:
                    continue
                if type_key == "dry_contact":
                    dedupe_key = _dry_contact_member_dedupe_key(
                        raw_type, sanitized, member_index
                    )
                else:
                    serial = sanitized.get("serial_number")
                    name = sanitized.get("name")
                    if isinstance(serial, str) and serial.strip():
                        dedupe_key = f"sn:{serial.strip()}"
                    elif isinstance(name, str) and name.strip():
                        dedupe_key = f"name:{name.strip()}"
                    else:
                        dedupe_key = f"idx:{len(members)}:{type_key}"
                if dedupe_key in seen_keys:
                    continue
                seen_keys.add(dedupe_key)
                members.append(sanitized)

        valid = True
        for type_key, bucket in grouped.items():
            members = bucket.get("devices")
            count = len(members) if isinstance(members, list) else 0
            bucket["count"] = count
            bucket["type_label"] = bucket.get("type_label") or type_display_label(
                type_key
            )
            if type_key == "encharge" and isinstance(members, list):
                name_counts: dict[str, int] = {}
                for member in members:
                    if not isinstance(member, dict):
                        continue
                    raw_name = member.get("name")
                    if raw_name is None:
                        continue
                    try:
                        name_text = str(raw_name).strip()
                    except Exception:  # noqa: BLE001
                        continue
                    if not name_text:
                        continue
                    name_counts[name_text] = name_counts.get(name_text, 0) + 1
                if name_counts:
                    bucket["model_counts"] = dict(name_counts)
                    summary = self._format_inverter_model_summary(name_counts)
                    if isinstance(summary, str) and summary.strip():
                        bucket["model_summary"] = summary

        return valid, dict(grouped), list(dict.fromkeys(ordered_keys))

    def _set_type_device_buckets(
        self,
        grouped: dict[str, dict[str, object]],
        ordered_keys: list[str],
        *,
        authoritative: bool = True,
    ) -> None:
        normalized_order = [
            key
            for key in ordered_keys
            if key in grouped
            and isinstance(grouped[key].get("devices"), list)
            and int(grouped[key].get("count", 0)) > 0
        ]
        buckets_out = {
            key: value
            for key, value in grouped.items()
            if int(value.get("count", 0)) > 0
        }
        self._set_shared_state_attr("_type_device_buckets", buckets_out)
        self._set_shared_state_attr("_type_device_order", normalized_order)
        if authoritative:
            self._set_shared_state_attr("_devices_inventory_ready", True)

    @staticmethod
    def _devices_inventory_buckets(payload: object) -> list[dict[str, object]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []
        result = payload.get("result")
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        wrapped = payload.get("value")
        if isinstance(wrapped, dict):
            wrapped_result = wrapped.get("result")
            if isinstance(wrapped_result, list):
                return [item for item in wrapped_result if isinstance(item, dict)]
        return []

    @staticmethod
    def _hems_devices_groups(payload: object) -> list[dict[str, object]]:
        if not isinstance(payload, dict):
            return []
        result = payload.get("result")
        if isinstance(result, dict):
            devices = result.get("devices")
            if isinstance(devices, list):
                return [grouped for grouped in devices if isinstance(grouped, dict)]
            if isinstance(devices, dict):
                return [devices]
        data = payload.get("data")
        if not isinstance(data, dict):
            return []
        hems_devices = (
            data.get("hems-devices")
            if data.get("hems-devices") is not None
            else data.get("hems_devices")
        )
        # The HEMS endpoint has appeared with both hyphenated and underscored keys.
        if not isinstance(hems_devices, dict):
            return []
        return [hems_devices]

    @classmethod
    def _legacy_hems_devices_groups(cls, payload: object) -> list[dict[str, object]]:
        groups: list[dict[str, object]] = []
        for bucket in cls._devices_inventory_buckets(payload):
            bucket_type = cls._hems_bucket_type(
                bucket.get("type")
                if bucket.get("type") is not None
                else (
                    bucket.get("deviceType")
                    if bucket.get("deviceType") is not None
                    else bucket.get("device_type")
                )
            )
            if bucket_type != "hemsdevices":
                continue
            grouped_devices = bucket.get("devices")
            if not isinstance(grouped_devices, list):
                continue
            groups.extend(
                grouped for grouped in grouped_devices if isinstance(grouped, dict)
            )
        return groups

    def _hems_grouped_devices(self) -> list[dict[str, object]]:
        groups = self._hems_devices_groups(getattr(self, "_hems_devices_payload", None))
        if groups:
            return groups
        return self._legacy_hems_devices_groups(
            getattr(self, "_devices_inventory_payload", None)
        )

    @staticmethod
    def _normalize_hems_member(member: dict[str, object]) -> dict[str, object]:
        normalized: dict[str, object] = dict(member)
        alias_pairs = (
            ("device-type", "device_type"),
            ("deviceType", "device_type"),
            ("device-uid", "device_uid"),
            ("deviceUid", "device_uid"),
            ("last-report", "last_report"),
            ("lastReport", "last_report"),
            ("last-reported", "last_reported"),
            ("lastReported", "last_reported"),
            ("last-reported-at", "last_reported_at"),
            ("lastReportedAt", "last_reported_at"),
            ("firmware-version", "firmware_version"),
            ("firmwareVersion", "firmware_version"),
            ("software-version", "software_version"),
            ("softwareVersion", "software_version"),
            ("hardware-version", "hardware_version"),
            ("hardwareVersion", "hardware_version"),
            ("hardware-sku", "hardware_sku"),
            ("hardwareSku", "hardware_sku"),
            ("part-number", "part_number"),
            ("partNumber", "part_number"),
            ("hems-device-id", "hems_device_id"),
            ("hems-device-facet-id", "hems_device_facet_id"),
            ("pairing-status", "pairing_status"),
            ("device-state", "device_state"),
            ("iqer-uid", "iqer_uid"),
            ("ip-address", "ip_address"),
            ("created-at", "created_at"),
            ("fvt-time", "fvt_time"),
        )
        for source, dest in alias_pairs:
            if dest not in normalized and source in normalized:
                normalized[dest] = normalized[source]
        if "status_text" not in normalized and "statusText" in normalized:
            normalized["status_text"] = normalized.get("statusText")
        if "serial_number" not in normalized and "serial" in normalized:
            normalized["serial_number"] = normalized.get("serial")
        if "uid" not in normalized and "device_uid" in normalized:
            normalized["uid"] = normalized.get("device_uid")
        return normalized

    @staticmethod
    def _normalize_heatpump_member(member: dict[str, object]) -> dict[str, object]:
        return InventoryRuntime._normalize_hems_member(member)

    def _extract_hems_group_members(
        self,
        groups: list[dict[str, object]],
        requested_keys: set[str],
    ) -> tuple[bool, list[dict[str, object]]]:
        members: list[dict[str, object]] = []
        seen_keys: set[str] = set()
        found_group = False
        for grouped in groups:
            for group_key in requested_keys:
                if group_key in grouped:
                    found_group = True
                raw_members = grouped.get(group_key)
                if not isinstance(raw_members, list):
                    continue
                for raw_member in raw_members:
                    if not isinstance(raw_member, dict):
                        continue
                    if self.member_is_retired(raw_member):
                        continue
                    normalized = self._normalize_hems_member(raw_member)
                    if not normalized:
                        continue
                    dedupe = (
                        self._type_member_text(
                            normalized, "device_uid", "uid", "serial_number", "name"
                        )
                        or f"idx:{len(members)}"
                    )
                    if dedupe in seen_keys:
                        continue
                    seen_keys.add(dedupe)
                    members.append(normalized)
        return found_group, members

    def _hems_group_members(self, *group_keys: str) -> list[dict[str, object]]:
        requested_keys = {key for key in group_keys if key}
        dedicated_found, dedicated_members = self._extract_hems_group_members(
            self._hems_devices_groups(getattr(self, "_hems_devices_payload", None)),
            requested_keys,
        )
        if dedicated_found:
            return dedicated_members
        _legacy_found, legacy_members = self._extract_hems_group_members(
            self._legacy_hems_devices_groups(
                getattr(self, "_devices_inventory_payload", None)
            ),
            requested_keys,
        )
        return legacy_members

    @staticmethod
    def _hems_bucket_type(raw_type: object) -> str | None:
        normalized = normalize_type_key(raw_type)
        if normalized:
            return normalized.replace("_", "")
        try:
            text = str(raw_type).strip().lower()
        except Exception:
            return None
        if not text:
            return None
        return "".join(ch for ch in text if ch.isalnum())

    @staticmethod
    def _heatpump_member_device_type(member: dict[str, object] | None) -> str | None:
        return heatpump_member_device_type(member)

    @staticmethod
    def _heatpump_worst_status_text(status_counts: dict[str, int]) -> str | None:
        if int(status_counts.get("error", 0) or 0) > 0:
            return "Error"
        if int(status_counts.get("warning", 0) or 0) > 0:
            return "Warning"
        if int(status_counts.get("not_reporting", 0) or 0) > 0:
            return "Not Reporting"
        if int(status_counts.get("unknown", 0) or 0) > 0:
            return "Unknown"
        if int(status_counts.get("normal", 0) or 0) > 0:
            return "Normal"
        return None

    def _merge_heatpump_type_bucket(self) -> None:
        ready_before = bool(getattr(self, "_devices_inventory_ready", False))
        buckets = dict(getattr(self, "_type_device_buckets", {}) or {})
        ordered = list(getattr(self, "_type_device_order", []) or [])
        key = "heatpump"

        members_out = self._hems_group_members("heat-pump", "heat_pump", "heatpump")
        if members_out:
            status_counts: dict[str, int] = {
                "total": len(members_out),
                "normal": 0,
                "warning": 0,
                "error": 0,
                "not_reporting": 0,
                "unknown": 0,
            }
            device_type_counts: dict[str, int] = {}
            model_counts: dict[str, int] = {}
            firmware_counts: dict[str, int] = {}
            latest_reported: datetime | None = None
            latest_reported_device: dict[str, object] | None = None
            overall_status_text: str | None = None

            for member in members_out:
                device_type = self._heatpump_member_device_type(member) or "UNKNOWN"
                device_type_counts[device_type] = (
                    device_type_counts.get(device_type, 0) + 1
                )
                status_text = self._heatpump_status_text(member)
                normalized_status = self._normalize_inverter_status(status_text)
                status_counts[normalized_status] = (
                    int(status_counts.get(normalized_status, 0)) + 1
                )
                if device_type == "HEAT_PUMP" and status_text:
                    overall_status_text = status_text

                model = self._type_member_text(
                    member,
                    "model",
                    "model_id",
                    "sku_id",
                    "part_number",
                    "hardware_sku",
                )
                if model:
                    model_counts[model] = model_counts.get(model, 0) + 1
                firmware = self._type_member_text(
                    member,
                    "firmware_version",
                    "sw_version",
                    "software_version",
                    "application_version",
                )
                if firmware:
                    firmware_counts[firmware] = firmware_counts.get(firmware, 0) + 1

                parsed_last = self._parse_inverter_last_report(
                    self._type_member_text(
                        member,
                        "last_report",
                        "last_reported",
                        "last_reported_at",
                    )
                )
                if parsed_last is not None and (
                    latest_reported is None or parsed_last > latest_reported
                ):
                    latest_reported = parsed_last
                    latest_reported_device = {
                        "device_type": device_type,
                        "device_uid": self._type_member_text(
                            member, "device_uid", "uid", "serial_number"
                        ),
                        "name": self._type_member_text(member, "name"),
                        "status": status_text,
                    }

            if not overall_status_text:
                overall_status_text = self._heatpump_worst_status_text(status_counts)

            buckets[key] = {
                "type_key": key,
                "type_label": "Heat Pump",
                "count": len(members_out),
                "devices": members_out,
                "status_counts": status_counts,
                "status_summary": self._format_inverter_status_summary(status_counts),
                "device_type_counts": device_type_counts,
                "model_counts": model_counts,
                "model_summary": self._format_inverter_model_summary(model_counts),
                "firmware_counts": firmware_counts,
                "firmware_summary": self._format_inverter_model_summary(
                    firmware_counts
                ),
                "overall_status_text": overall_status_text,
                "latest_reported_utc": (
                    latest_reported.isoformat() if latest_reported is not None else None
                ),
                "latest_reported_device": latest_reported_device,
            }
            if key not in ordered:
                if "iqevse" in ordered:
                    ordered.insert(ordered.index("iqevse") + 1, key)
                else:
                    ordered.append(key)
        else:
            buckets.pop(key, None)
            ordered = [item for item in ordered if item != key]

        self._set_type_device_buckets(buckets, ordered)
        if not ready_before:
            self._devices_inventory_ready = False
        self._refresh_cached_topology()

    @staticmethod
    def _summary_text(value: object) -> str | None:
        if value is None:
            return None
        try:
            text = str(value).strip()
        except Exception:  # noqa: BLE001
            return None
        return text or None

    @classmethod
    def _summary_identity(cls, value: object) -> str | None:
        text = cls._summary_text(value)
        if not text:
            return None
        normalized = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
        return normalized or None

    def _summary_type_bucket_source(self, type_key: object) -> dict[str, object] | None:
        normalized = normalize_type_key(type_key)
        if not normalized:
            return None
        buckets = getattr(self, "_type_device_buckets", None)
        if not isinstance(buckets, dict):
            return None
        bucket = buckets.get(normalized)
        return bucket if isinstance(bucket, dict) else None

    def _gateway_inventory_summary_marker(self) -> tuple[object, ...]:
        dashboard_payloads = getattr(
            self, "_system_dashboard_devices_details_raw", None
        )
        dashboard_envoy = (
            dashboard_payloads.get("envoy")
            if isinstance(dashboard_payloads, dict)
            else None
        )
        return (
            id(self._summary_type_bucket_source("envoy")),
            id(dashboard_envoy),
        )

    def _microinverter_inventory_summary_marker(self) -> tuple[object, ...]:
        return (id(self._summary_type_bucket_source("microinverter")),)

    def _heatpump_inventory_summary_marker(self) -> tuple[object, ...]:
        return (
            id(self._summary_type_bucket_source("heatpump")),
            id(getattr(self, "_hems_devices_payload", None)),
            bool(getattr(self, "_hems_devices_using_stale", False)),
            getattr(self, "_hems_devices_last_success_utc", None),
            getattr(self, "_hems_devices_last_success_mono", None),
        )

    def _gateway_iq_energy_router_records_marker(self) -> tuple[object, ...]:
        return (
            id(getattr(self, "_hems_devices_payload", None)),
            id(getattr(self, "_devices_inventory_payload", None)),
            id(
                self._coordinator_backed_attr(
                    "_restored_gateway_iq_energy_router_records"
                )
            ),
        )

    @staticmethod
    def _heatpump_status_text(member: dict[str, object] | None) -> str | None:
        return heatpump_status_text(member)

    @classmethod
    def _gateway_iq_energy_router_summary_records(
        cls, members: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        key_counts: dict[str, int] = {}
        for member in members:
            index = len(records) + 1
            base_key = None
            for key in ("device-uid", "device_uid", "uid"):
                base_key = cls._summary_identity(member.get(key))
                if base_key:
                    break
            if base_key is None:
                name_identity = cls._summary_identity(member.get("name"))
                base_key = (
                    f"name_{name_identity}" if name_identity else f"index_{index}"
                )
            key_counts[base_key] = key_counts.get(base_key, 0) + 1
            key = base_key
            if key_counts[base_key] > 1:
                key = f"{base_key}_{key_counts[base_key]}"
            records.append(
                {
                    "key": key,
                    "index": index,
                    "name": cls._summary_text(member.get("name"))
                    or f"IQ Energy Router_{index}",
                    "member": dict(member),
                }
            )
        return records

    def _build_gateway_inventory_summary(self) -> dict[str, object]:
        bucket = self.type_bucket("envoy") or {}
        members_raw = bucket.get("devices")
        members = (
            [item for item in members_raw if isinstance(item, dict)]
            if isinstance(members_raw, list)
            else []
        )
        dashboard_envoy_fetcher = self.__dict__.get("system_dashboard_envoy_detail")
        if callable(dashboard_envoy_fetcher):
            dashboard_envoy = dashboard_envoy_fetcher()
        else:
            dashboard_envoy = self.coordinator.system_dashboard_envoy_detail()
        if not members and isinstance(dashboard_envoy, dict):
            members = [dict(dashboard_envoy)]
        ip_address = self._gateway_summary_ip_address(members, dashboard_envoy)
        try:
            total_devices = int(bucket.get("count", len(members)) or 0)
        except Exception:
            total_devices = len(members)
        total_devices = max(total_devices, len(members))
        status_counts: dict[str, int] = {
            "normal": 0,
            "warning": 0,
            "error": 0,
            "not_reporting": 0,
            "unknown": 0,
        }
        model_counts: dict[str, int] = {}
        firmware_counts: dict[str, int] = {}
        property_keys: set[str] = set()
        connected_devices = 0
        disconnected_devices = 0
        latest_reported: datetime | None = None
        latest_reported_device: dict[str, object] | None = None
        without_last_report_count = 0

        for member in members:
            property_keys.update(str(key) for key in member.keys())
            status_source = None
            for key in ("statusText", "status_text", "status"):
                if member.get(key) is not None:
                    status_source = member.get(key)
                    break
            status = self._normalize_inverter_status(status_source)
            status_counts[status] = status_counts.get(status, 0) + 1

            connected = member.get("connected")
            if isinstance(connected, str):
                normalized_connected = connected.strip().lower()
                if normalized_connected in {"true", "1", "yes", "y"}:
                    connected = True
                elif normalized_connected in {"false", "0", "no", "n"}:
                    connected = False
                else:
                    connected = None
            elif isinstance(connected, (int, float)):
                connected = connected != 0
            elif not isinstance(connected, bool):
                connected = None
            if connected is None:
                if status == "normal":
                    connected = True
                elif status == "not_reporting":
                    connected = False
            if connected is True:
                connected_devices += 1
            elif connected is False:
                disconnected_devices += 1

            model_name = self._type_member_text(
                member, "model", "model_name", "part_number", "device_type"
            )
            if model_name:
                model_counts[model_name] = model_counts.get(model_name, 0) + 1
            firmware_version = self._type_member_text(
                member, "firmware_version", "sw_version", "software_version"
            )
            if firmware_version:
                firmware_counts[firmware_version] = (
                    firmware_counts.get(firmware_version, 0) + 1
                )

            parsed_last_report = None
            for key in (
                "last_report",
                "last_reported",
                "last_reported_at",
                "last-report",
            ):
                parsed_last_report = self._parse_inverter_last_report(member.get(key))
                if parsed_last_report is not None:
                    break
            if parsed_last_report is None:
                without_last_report_count += 1
                continue
            if latest_reported is None or parsed_last_report > latest_reported:
                latest_reported = parsed_last_report
                latest_reported_device = {
                    "name": self._summary_text(member.get("name")),
                    "serial_number": self._summary_text(member.get("serial_number")),
                    "status": self._summary_text(status_source),
                }

        unknown_connection_devices = max(
            0, total_devices - connected_devices - disconnected_devices
        )
        status_summary = (
            f"Normal {status_counts.get('normal', 0)} | "
            f"Warning {status_counts.get('warning', 0)} | "
            f"Error {status_counts.get('error', 0)} | "
            f"Not Reporting {status_counts.get('not_reporting', 0)} | "
            f"Unknown {status_counts.get('unknown', 0)}"
            if total_devices > 0
            else None
        )
        if latest_reported is None and isinstance(dashboard_envoy, dict):
            fallback_last = None
            for key in ("last_report", "last_interval_end_date"):
                fallback_last = self._parse_inverter_last_report(
                    dashboard_envoy.get(key)
                )
                if fallback_last is not None:
                    break
            if fallback_last is not None:
                latest_reported = fallback_last
                latest_reported_device = {
                    "name": self._summary_text(dashboard_envoy.get("name"))
                    or "IQ Gateway",
                    "serial_number": self._summary_text(
                        dashboard_envoy.get("serial_number")
                    ),
                    "status": self._summary_text(
                        dashboard_envoy.get("statusText")
                        if dashboard_envoy.get("statusText") is not None
                        else dashboard_envoy.get("status")
                    ),
                }
        return {
            "total_devices": total_devices,
            "connected_devices": connected_devices,
            "disconnected_devices": disconnected_devices,
            "unknown_connection_devices": unknown_connection_devices,
            "without_last_report_count": without_last_report_count,
            "status_counts": status_counts,
            "status_summary": status_summary,
            "model_counts": model_counts,
            "model_summary": self._format_inverter_model_summary(model_counts),
            "firmware_counts": firmware_counts,
            "firmware_summary": self._format_inverter_model_summary(firmware_counts),
            "ip_address": ip_address,
            "latest_reported": latest_reported,
            "latest_reported_utc": (
                latest_reported.isoformat() if latest_reported is not None else None
            ),
            "latest_reported_device": latest_reported_device,
            "property_keys": sorted(property_keys),
        }

    def _build_microinverter_inventory_summary(self) -> dict[str, object]:
        bucket = self.type_bucket("microinverter") or {}
        members = bucket.get("devices")
        safe_members = (
            [dict(item) for item in members if isinstance(item, dict)]
            if isinstance(members, list)
            else []
        )
        status_counts_raw = bucket.get("status_counts")
        status_counts: dict[str, int] = {}
        has_status_counts = isinstance(status_counts_raw, dict)
        if isinstance(status_counts_raw, dict):
            for key in (
                "total",
                "normal",
                "warning",
                "error",
                "not_reporting",
                "unknown",
            ):
                try:
                    status_counts[key] = int(status_counts_raw.get(key, 0) or 0)
                except Exception:
                    status_counts[key] = 0
        try:
            total_inverters = int(bucket.get("count", len(safe_members)) or 0)
        except Exception:
            total_inverters = len(safe_members)
        if status_counts.get("total", 0) > 0:
            total_inverters = max(total_inverters, int(status_counts.get("total", 0)))
        not_reporting = max(0, int(status_counts.get("not_reporting", 0)))
        unknown = max(0, int(status_counts.get("unknown", 0)))
        if not has_status_counts:
            unknown = total_inverters
        elif (
            total_inverters > 0
            and int(status_counts.get("total", 0) or 0) <= 0
            and max(
                0,
                int(status_counts.get("normal", 0) or 0)
                + int(status_counts.get("warning", 0) or 0)
                + int(status_counts.get("error", 0) or 0)
                + not_reporting
                + unknown,
            )
            == 0
        ):
            unknown = total_inverters
        known_status_total = not_reporting + unknown
        if known_status_total > total_inverters:
            unknown = max(0, unknown - (known_status_total - total_inverters))
        reporting = max(0, total_inverters - not_reporting - unknown)
        latest_reported = self._parse_inverter_last_report(
            bucket.get("latest_reported_utc")
            if bucket.get("latest_reported_utc") is not None
            else bucket.get("latest_reported")
        )
        latest_reported_device = (
            dict(bucket.get("latest_reported_device"))
            if isinstance(bucket.get("latest_reported_device"), dict)
            else None
        )
        if latest_reported is None:
            for member in safe_members:
                parsed_last = self._parse_inverter_last_report(
                    member.get("last_report")
                )
                if parsed_last is None:
                    continue
                if latest_reported is None or parsed_last > latest_reported:
                    latest_reported = parsed_last
                    latest_reported_device = {
                        "serial_number": self._summary_text(
                            member.get("serial_number")
                        ),
                        "name": self._summary_text(member.get("name")),
                        "status": self._summary_text(
                            member.get("statusText")
                            if member.get("statusText") is not None
                            else member.get("status")
                        ),
                    }
        snapshot: dict[str, object] = {
            "total_inverters": total_inverters,
            "reporting_inverters": reporting,
            "not_reporting_inverters": not_reporting,
            "unknown_inverters": unknown,
            "status_counts": status_counts,
            "status_summary": bucket.get("status_summary"),
            "model_summary": bucket.get("model_summary"),
            "firmware_summary": bucket.get("firmware_summary"),
            "array_summary": bucket.get("array_summary"),
            "panel_info": (
                dict(bucket.get("panel_info"))
                if isinstance(bucket.get("panel_info"), dict)
                else None
            ),
            "status_type_counts": (
                dict(bucket.get("status_type_counts"))
                if isinstance(bucket.get("status_type_counts"), dict)
                else None
            ),
            "latest_reported": latest_reported,
            "latest_reported_utc": (
                latest_reported.isoformat() if latest_reported is not None else None
            ),
            "latest_reported_device": latest_reported_device,
            "production_start_date": bucket.get("production_start_date"),
            "production_end_date": bucket.get("production_end_date"),
        }
        connectivity_state = bucket.get("connectivity_state")
        if not isinstance(connectivity_state, str) or not connectivity_state.strip():
            connectivity_state = "degraded"
            if total_inverters <= 0:
                connectivity_state = None
            elif reporting >= total_inverters:
                connectivity_state = "online"
            elif reporting == 0 and not_reporting > 0:
                connectivity_state = "offline"
            elif reporting > 0 and reporting < total_inverters:
                connectivity_state = "degraded"
            elif unknown >= total_inverters:
                connectivity_state = "unknown"
        snapshot["connectivity_state"] = connectivity_state
        return snapshot

    def _build_heatpump_inventory_summary(self) -> dict[str, object]:
        bucket = self.type_bucket("heatpump") or {}
        members = bucket.get("devices")
        safe_members = (
            [dict(item) for item in members if isinstance(item, dict)]
            if isinstance(members, list)
            else []
        )
        status_counts_raw = bucket.get("status_counts")
        status_counts: dict[str, int] | None = None
        if isinstance(status_counts_raw, dict):
            parsed_counts = {
                "total": 0,
                "normal": 0,
                "warning": 0,
                "error": 0,
                "not_reporting": 0,
                "unknown": 0,
            }
            try:
                for key in parsed_counts:
                    parsed_counts[key] = int(status_counts_raw.get(key, 0) or 0)
                status_counts = parsed_counts
            except Exception:
                status_counts = None
        if status_counts is None:
            status_counts = {
                "total": len(safe_members),
                "normal": 0,
                "warning": 0,
                "error": 0,
                "not_reporting": 0,
                "unknown": 0,
            }
            for member in safe_members:
                status_key = self._normalize_inverter_status(
                    self._heatpump_status_text(member)
                )
                status_counts[status_key] = int(status_counts.get(status_key, 0)) + 1
        try:
            total_devices = int(bucket.get("count", len(safe_members)) or 0)
        except Exception:
            total_devices = len(safe_members)
        total_devices = max(total_devices, len(safe_members))
        status_counts["total"] = max(
            int(status_counts.get("total", 0) or 0), total_devices
        )
        latest_reported = self._parse_inverter_last_report(
            bucket.get("latest_reported_utc")
            if bucket.get("latest_reported_utc") is not None
            else bucket.get("latest_reported")
        )
        latest_reported_device = (
            dict(bucket.get("latest_reported_device"))
            if isinstance(bucket.get("latest_reported_device"), dict)
            else None
        )
        without_last_report_count = 0
        if latest_reported is None:
            for member in safe_members:
                member_last = None
                for key in (
                    "last_report",
                    "last_reported",
                    "last_reported_at",
                    "last-report",
                ):
                    member_last = self._parse_inverter_last_report(member.get(key))
                    if member_last is not None:
                        break
                if member_last is None:
                    without_last_report_count += 1
                    continue
                if latest_reported is None or member_last > latest_reported:
                    latest_reported = member_last
                    latest_reported_device = {
                        "device_type": self._heatpump_member_device_type(member),
                        "name": self._summary_text(member.get("name")),
                        "device_uid": self._type_member_text(
                            member, "device_uid", "device-uid", "uid"
                        ),
                        "status": self._heatpump_status_text(member),
                    }
        overall_status_text = self._summary_text(bucket.get("overall_status_text"))
        if not overall_status_text:
            for member in safe_members:
                if self._heatpump_member_device_type(member) != "HEAT_PUMP":
                    continue
                overall_status_text = self._heatpump_status_text(member)
                if overall_status_text:
                    break
        if not overall_status_text:
            overall_status_text = self._heatpump_worst_status_text(status_counts)
        device_type_counts: dict[str, int] = {}
        if isinstance(bucket.get("device_type_counts"), dict):
            for key, value in bucket.get("device_type_counts", {}).items():
                if key is None:
                    continue
                try:
                    count = int(value)
                except Exception:
                    continue
                if count > 0:
                    device_type_counts[str(key)] = count
        else:
            for member in safe_members:
                device_type = self._heatpump_member_device_type(member) or "UNKNOWN"
                device_type_counts[device_type] = (
                    device_type_counts.get(device_type, 0) + 1
                )
        status_summary = bucket.get("status_summary")
        if not isinstance(status_summary, str) or not status_summary.strip():
            status_summary = self._format_inverter_status_summary(status_counts)
        hems_last_success_utc = getattr(self, "_hems_devices_last_success_utc", None)
        if not isinstance(hems_last_success_utc, datetime):
            hems_last_success_utc = None
        hems_last_success_mono = getattr(self, "_hems_devices_last_success_mono", None)
        hems_last_success_age_s: float | None = None
        if isinstance(hems_last_success_mono, (int, float)):
            age = time.monotonic() - float(hems_last_success_mono)
            if age >= 0:
                hems_last_success_age_s = round(age, 1)
        return {
            "total_devices": total_devices,
            "members": safe_members,
            "status_counts": status_counts,
            "status_summary": status_summary,
            "device_type_counts": device_type_counts,
            "model_summary": bucket.get("model_summary"),
            "firmware_summary": bucket.get("firmware_summary"),
            "latest_reported": latest_reported,
            "latest_reported_utc": (
                latest_reported.isoformat() if latest_reported is not None else None
            ),
            "latest_reported_device": latest_reported_device,
            "without_last_report_count": without_last_report_count,
            "overall_status_text": overall_status_text,
            "hems_data_stale": bool(getattr(self, "_hems_devices_using_stale", False)),
            "hems_last_success_utc": (
                hems_last_success_utc.isoformat()
                if hems_last_success_utc is not None
                else None
            ),
            "hems_last_success_age_s": hems_last_success_age_s,
        }

    def _build_heatpump_type_summaries(self) -> dict[str, dict[str, object]]:
        snapshot = self._build_heatpump_inventory_summary()
        members = [
            member for member in snapshot.get("members", []) if isinstance(member, dict)
        ]
        summaries: dict[str, dict[str, object]] = {}
        for device_type in sorted(
            {
                self._heatpump_member_device_type(member)
                for member in members
                if self._heatpump_member_device_type(member)
            }
        ):
            type_members = [
                member
                for member in members
                if self._heatpump_member_device_type(member) == device_type
            ]
            counts = {
                "total": len(type_members),
                "normal": 0,
                "warning": 0,
                "error": 0,
                "not_reporting": 0,
                "unknown": 0,
            }
            latest_reported: datetime | None = None
            latest_device: dict[str, object] | None = None
            status_texts: list[str] = []
            for member in type_members:
                status_text = self._heatpump_status_text(member)
                if status_text:
                    status_texts.append(status_text)
                status_key = self._normalize_inverter_status(status_text)
                counts[status_key] = int(counts.get(status_key, 0)) + 1
                parsed_last = None
                for key in (
                    "last_report",
                    "last_reported",
                    "last_reported_at",
                    "last-report",
                ):
                    parsed_last = self._parse_inverter_last_report(member.get(key))
                    if parsed_last is not None:
                        break
                if parsed_last is not None and (
                    latest_reported is None or parsed_last > latest_reported
                ):
                    latest_reported = parsed_last
                    latest_device = {
                        "name": self._summary_text(member.get("name")),
                        "device_uid": self._type_member_text(
                            member, "device_uid", "device-uid", "uid"
                        ),
                        "status": status_text,
                    }
            unique_statuses = list(dict.fromkeys(status_texts))
            if len(unique_statuses) == 1:
                native_status = unique_statuses[0]
            else:
                native_status = self._heatpump_worst_status_text(counts)
            summaries[device_type] = {
                "device_type": device_type,
                "members": type_members,
                "member_count": len(type_members),
                "status_counts": counts,
                "status_summary": self._format_inverter_status_summary(counts),
                "native_status": native_status,
                "latest_reported": latest_reported,
                "latest_reported_utc": (
                    latest_reported.isoformat() if latest_reported is not None else None
                ),
                "latest_reported_device": latest_device,
                "hems_data_stale": snapshot.get("hems_data_stale"),
                "hems_last_success_utc": snapshot.get("hems_last_success_utc"),
                "hems_last_success_age_s": snapshot.get("hems_last_success_age_s"),
            }
        return summaries

    @callback
    def _rebuild_inventory_summary_caches(self) -> None:
        gateway_source = self._gateway_inventory_summary_marker()
        micro_source = self._microinverter_inventory_summary_marker()
        heatpump_source = self._heatpump_inventory_summary_marker()
        router_source = self._gateway_iq_energy_router_records_marker()
        updates: dict[str, object] = {}

        if gateway_source != self._gateway_inventory_summary_source:
            updates["_gateway_inventory_summary_cache"] = (
                self._build_gateway_inventory_summary()
            )
            updates["_gateway_inventory_summary_source"] = gateway_source

        if micro_source != self._microinverter_inventory_summary_source:
            updates["_microinverter_inventory_summary_cache"] = (
                self._build_microinverter_inventory_summary()
            )
            updates["_microinverter_inventory_summary_source"] = micro_source

        if heatpump_source != self._heatpump_inventory_summary_source:
            updates["_heatpump_inventory_summary_cache"] = (
                self._build_heatpump_inventory_summary()
            )
            updates["_heatpump_inventory_summary_source"] = heatpump_source

        if heatpump_source != self._heatpump_type_summaries_source:
            updates["_heatpump_type_summaries_cache"] = (
                self._build_heatpump_type_summaries()
            )
            updates["_heatpump_type_summaries_source"] = heatpump_source

        if router_source != self._gateway_iq_energy_router_records_source:
            router_records = self._gateway_iq_energy_router_summary_records(
                self.gateway_iq_energy_router_records()
            )
            updates["_gateway_iq_energy_router_records_cache"] = router_records
            updates["_gateway_iq_energy_router_records_source"] = router_source
            updates["_gateway_iq_energy_router_records_by_key_cache"] = {
                record["key"]: record
                for record in router_records
                if isinstance(record, dict) and isinstance(record.get("key"), str)
            }

        if updates:
            self._update_shared_state(**updates)

    async def _async_refresh_devices_inventory(self, *, force: bool = False) -> None:
        coord = self.coordinator
        now = time.monotonic()
        family = "inventory_topology"
        if not coord._endpoint_family_should_run(family, force=force):
            return
        if not force and self._devices_inventory_cache_until:
            if now < self._devices_inventory_cache_until:
                return
        fetcher = getattr(self.client, "devices_inventory", None)
        if not callable(fetcher):
            return
        try:
            payload = await self._async_call_refreshable_fetcher(fetcher, force=force)
        except Exception as err:  # noqa: BLE001
            coord._note_endpoint_family_failure(family, err)
            return
        override = getattr(self.coordinator, "__dict__", {}).get(
            "_parse_devices_inventory_payload"
        )
        if callable(override):
            valid, grouped, ordered = override(payload)
        else:
            valid, grouped, ordered = self._parse_devices_inventory_payload(payload)
        if not valid:
            coord._note_endpoint_family_failure(
                family, ValueError("Device inventory payload shape was invalid")
            )
            return
        summary = self._debug_devices_inventory_summary(grouped, ordered)
        if not grouped:
            self._debug_log_summary_if_changed(
                "devices_inventory",
                "Device inventory discovery summary",
                summary,
            )
            self._set_shared_state_attr(
                "_devices_inventory_cache_until", now + DEVICES_INVENTORY_CACHE_TTL
            )
            coord._note_endpoint_family_success(family)
            return
        has_active_members = False
        for bucket in grouped.values():
            try:
                if int(bucket.get("count", 0)) > 0:
                    has_active_members = True
                    break
            except Exception:
                continue
        if not has_active_members:
            _LOGGER.debug(
                "Device inventory refresh returned no active members; keeping previous type mapping: %s",
                redact_text(summary, site_ids=(self.site_id,)),
            )
            self._devices_inventory_cache_until = now + DEVICES_INVENTORY_CACHE_TTL
            coord._note_endpoint_family_success(family)
            return
        self._set_type_device_buckets(grouped, ordered)
        redacted_payload = self._redact_battery_payload(payload)
        if isinstance(redacted_payload, dict):
            self._set_shared_state_attr("_devices_inventory_payload", redacted_payload)
        else:
            self._set_shared_state_attr(
                "_devices_inventory_payload", {"value": redacted_payload}
            )
        self._merge_heatpump_type_bucket()
        self._set_shared_state_attr(
            "_devices_inventory_cache_until", now + DEVICES_INVENTORY_CACHE_TTL
        )
        self._debug_log_summary_if_changed(
            "devices_inventory",
            "Device inventory discovery summary",
            summary,
        )
        coord._note_endpoint_family_success(family)

    def devices_inventory_refresh_due(self, *, force: bool = False) -> bool:
        coord = self.coordinator
        now = time.monotonic()
        if not coord._endpoint_family_should_run("inventory_topology", force=force):
            return False
        if not force and self._devices_inventory_cache_until:
            if now < self._devices_inventory_cache_until:
                return False
        fetcher = getattr(self.client, "devices_inventory", None)
        return callable(fetcher)

    async def _async_refresh_hems_devices(self, *, force: bool = False) -> None:
        coord = self.coordinator
        now = time.monotonic()
        cache_ttl = self._hems_devices_cache_ttl_s()
        family = HEMS_INVENTORY_ENDPOINT_FAMILY
        previous_payload = getattr(self, "_hems_devices_payload", None)
        if coord._skip_hems_polling_due_to_auth_circuit(endpoint="hems_devices"):
            self._mark_hems_devices_auth_backoff(
                now=now,
                cache_ttl=cache_ttl,
                previous_payload=previous_payload,
            )
            return
        health = coord._endpoint_family_state(family)
        if health.cooldown_active and coord._endpoint_family_wait_active(family):
            if previous_payload is not None and coord._endpoint_family_can_use_stale(
                family
            ):
                self._update_shared_state(
                    _hems_devices_payload=previous_payload,
                    _hems_devices_using_stale=True,
                    _hems_inventory_ready=True,
                    _hems_devices_cache_until=(
                        coord._endpoint_family_next_retry_mono(family)
                        or now + cache_ttl
                    ),
                )
                self._merge_heatpump_type_bucket()
                self._debug_log_summary_if_changed(
                    "hems_inventory",
                    "HEMS discovery summary",
                    self._debug_hems_inventory_summary(),
                )
            return
        if not force and self._hems_devices_cache_until:
            if now < self._hems_devices_cache_until:
                return
        await coord.heatpump_runtime.async_refresh_hems_support_preflight(force=force)
        if coord._skip_hems_polling_due_to_auth_circuit(endpoint="hems_devices"):
            self._mark_hems_devices_auth_backoff(
                now=now,
                cache_ttl=cache_ttl,
                previous_payload=previous_payload,
            )
            return
        if getattr(self.client, "hems_site_supported", None) is False:
            self._update_shared_state(
                _hems_devices_payload=None,
                _hems_devices_using_stale=False,
                _hems_inventory_ready=True,
            )
            self._merge_heatpump_type_bucket()
            self._set_shared_state_attr("_hems_devices_cache_until", now + cache_ttl)
            self._debug_log_summary_if_changed(
                "hems_inventory",
                "HEMS discovery summary",
                self._debug_hems_inventory_summary(),
            )
            return
        fetcher = getattr(self.client, "hems_devices", None)
        if not callable(fetcher):
            return

        stale_allowed = False
        if previous_payload is not None:
            last_success = getattr(self, "_hems_devices_last_success_mono", None)
            if isinstance(last_success, (int, float)):
                stale_allowed = now - float(last_success) <= HEMS_DEVICES_STALE_AFTER_S

        try:
            payload = await fetcher(refresh_data=force)
        except Exception as err:  # noqa: BLE001
            if coord._note_hems_auth_failure(err, endpoint="hems_devices"):
                self._mark_hems_devices_auth_backoff(
                    now=now,
                    cache_ttl=cache_ttl,
                    previous_payload=previous_payload,
                )
                return
            if getattr(self.client, "hems_site_supported", None) is not False:
                coord._note_endpoint_family_failure(family, err)
            _LOGGER.debug(
                "HEMS device inventory fetch failed: %s",
                redact_text(err, site_ids=(self.site_id,)),
            )
            if getattr(self.client, "hems_site_supported", None) is False:
                self._update_shared_state(
                    _hems_devices_payload=None,
                    _hems_devices_using_stale=False,
                    _hems_inventory_ready=True,
                    _hems_devices_cache_until=now + cache_ttl,
                )
            elif stale_allowed:
                # A short stale window avoids removing heat-pump entities during
                # cloud blips.
                self._update_shared_state(
                    _hems_devices_payload=previous_payload,
                    _hems_devices_using_stale=True,
                    _hems_inventory_ready=True,
                    _hems_devices_cache_until=now + cache_ttl,
                )
            else:
                self._update_shared_state(
                    _hems_devices_payload=None,
                    _hems_devices_using_stale=False,
                    _hems_inventory_ready=False,
                    _hems_devices_cache_until=now + cache_ttl,
                )
            self._merge_heatpump_type_bucket()
            self._debug_log_summary_if_changed(
                "hems_inventory",
                "HEMS discovery summary",
                self._debug_hems_inventory_summary(),
            )
            return

        if not isinstance(payload, dict):
            if getattr(self.client, "hems_site_supported", None) is False:
                self._update_shared_state(
                    _hems_devices_payload=None,
                    _hems_devices_using_stale=False,
                    _hems_inventory_ready=True,
                )
                self._merge_heatpump_type_bucket()
                self._set_shared_state_attr(
                    "_hems_devices_cache_until", now + cache_ttl
                )
                self._debug_log_summary_if_changed(
                    "hems_inventory",
                    "HEMS discovery summary",
                    self._debug_hems_inventory_summary(),
                )
                return
            if stale_allowed:
                coord._note_endpoint_family_failure(
                    family,
                    OptionalEndpointUnavailable("HEMS device inventory unavailable"),
                )
                self._update_shared_state(
                    _hems_devices_payload=previous_payload,
                    _hems_devices_using_stale=True,
                    _hems_inventory_ready=True,
                )
                self._merge_heatpump_type_bucket()
                self._set_shared_state_attr(
                    "_hems_devices_cache_until", now + cache_ttl
                )
                self._debug_log_summary_if_changed(
                    "hems_inventory",
                    "HEMS discovery summary",
                    self._debug_hems_inventory_summary(),
                )
                return
            coord._note_endpoint_family_failure(
                family,
                OptionalEndpointUnavailable("HEMS device inventory unavailable"),
            )
            self._update_shared_state(
                _hems_devices_payload=None,
                _hems_devices_using_stale=False,
                _hems_inventory_ready=False,
            )
            self._merge_heatpump_type_bucket()
            self._set_shared_state_attr("_hems_devices_cache_until", now + cache_ttl)
            self._debug_log_summary_if_changed(
                "hems_inventory",
                "HEMS discovery summary",
                self._debug_hems_inventory_summary(),
            )
            return

        redacted_payload = self._redact_battery_payload(payload)
        if isinstance(redacted_payload, dict):
            self._set_shared_state_attr("_hems_devices_payload", redacted_payload)
        else:
            self._set_shared_state_attr(
                "_hems_devices_payload", {"value": redacted_payload}
            )
        self._update_shared_state(
            _hems_devices_last_success_mono=now,
            _hems_devices_last_success_utc=dt_util.utcnow(),
            _hems_devices_using_stale=False,
            _hems_inventory_ready=True,
        )
        self._merge_heatpump_type_bucket()
        self._set_shared_state_attr("_hems_devices_cache_until", now + cache_ttl)
        coord._note_hems_auth_success(endpoint="hems_devices")
        coord._note_endpoint_family_success(family)
        self._debug_log_summary_if_changed(
            "hems_inventory",
            "HEMS discovery summary",
            self._debug_hems_inventory_summary(),
        )

    def _mark_hems_devices_auth_backoff(
        self,
        *,
        now: float,
        cache_ttl: float,
        previous_payload: object,
    ) -> None:
        """Keep HEMS inventory stale while the coordinator auth circuit is open."""

        coord = self.coordinator
        self._update_shared_state(
            _hems_devices_payload=previous_payload,
            _hems_devices_using_stale=previous_payload is not None,
            _hems_inventory_ready=True,
            _hems_devices_cache_until=(
                getattr(coord, "_hems_auth_backoff_until", None) or now + cache_ttl
            ),
        )
        self._merge_heatpump_type_bucket()
        self._debug_log_summary_if_changed(
            "hems_inventory",
            "HEMS discovery summary",
            self._debug_hems_inventory_summary(),
        )

    def hems_devices_refresh_due(self, *, force: bool = False) -> bool:
        coord = self.coordinator
        now = time.monotonic()
        health = coord._endpoint_family_state(HEMS_INVENTORY_ENDPOINT_FAMILY)
        if health.cooldown_active and coord._endpoint_family_wait_active(
            HEMS_INVENTORY_ENDPOINT_FAMILY
        ):
            return False
        if coord._hems_auth_circuit_active():
            return False
        if not force and self._hems_devices_cache_until:
            if now < self._hems_devices_cache_until:
                return False
        fetcher = getattr(self.client, "hems_devices", None)
        return callable(fetcher)

    def _system_dashboard_detail_records(
        self,
        payloads: dict[str, object],
        *source_types: str,
    ) -> list[dict[str, object]]:
        return system_dashboard_detail_records(payloads, *source_types)

    def _system_dashboard_meter_kind(self, payload: dict[str, object]) -> str | None:
        return system_dashboard_meter_kind(payload)

    def _system_dashboard_battery_detail_subset(
        self,
        payload: dict[str, object] | None,
    ) -> dict[str, object]:
        return system_dashboard_battery_detail_subset(payload)

    async def _async_refresh_system_dashboard(self, *, force: bool = False) -> None:
        coord = self.coordinator
        now = time.monotonic()
        family = "inventory_topology"
        if not coord._endpoint_family_should_run(family, force=force):
            return
        if not force and self._system_dashboard_cache_until:
            if now < self._system_dashboard_cache_until:
                return
        tree_fetcher = getattr(self.client, "devices_tree", None)
        details_fetcher = getattr(self.client, "devices_details", None)
        if not callable(tree_fetcher) and not callable(details_fetcher):
            return

        tree_payload = getattr(self, "_system_dashboard_devices_tree_raw", None)
        first_error: Exception | None = None
        fetched_payload = False
        if callable(tree_fetcher):
            try:
                tree_payload = await tree_fetcher()
            except Exception as err:  # noqa: BLE001
                first_error = err
            else:
                if isinstance(tree_payload, dict):
                    fetched_payload = True

        details_payloads = (
            dict(getattr(self, "_system_dashboard_devices_details_raw", {}) or {})
            if isinstance(
                getattr(self, "_system_dashboard_devices_details_raw", {}), dict
            )
            else {}
        )
        detail_failures: dict[str, str] = {}
        if callable(details_fetcher):
            semaphore = asyncio.Semaphore(SYSTEM_DASHBOARD_DETAIL_CONCURRENCY)

            async def _fetch_detail(
                source_type: str,
            ) -> tuple[str, dict[str, object] | None, Exception | None]:
                async with semaphore:
                    try:
                        payload = await details_fetcher(source_type)
                    except Exception as err:  # noqa: BLE001
                        return source_type, None, err
                return source_type, payload if isinstance(payload, dict) else None, None

            detail_tasks: list[
                asyncio.Task[tuple[str, dict[str, object] | None, Exception | None]]
            ] = []
            async with asyncio.TaskGroup() as task_group:
                for source_type in SYSTEM_DASHBOARD_DIAGNOSTIC_TYPES:
                    detail_tasks.append(
                        task_group.create_task(
                            _fetch_detail(source_type),
                            name=f"{DOMAIN}_system_dashboard_detail_{source_type}",
                        )
                    )
            detail_results = [task.result() for task in detail_tasks]
            for source_type, payload, err in detail_results:
                if err is not None:
                    if first_error is None:
                        first_error = err
                    detail_failures[source_type] = (
                        redact_text(err, site_ids=(self.site_id,))
                        or err.__class__.__name__
                    )
                    continue
                if payload is None:
                    continue
                fetched_payload = True
                canonical_type = self._system_dashboard_type_key(source_type)
                if not canonical_type:
                    continue
                details_payloads.setdefault(canonical_type, {})[source_type] = payload

        (
            type_summaries,
            hierarchy_summary,
            hierarchy_index,
        ) = self._build_system_dashboard_summaries(tree_payload, details_payloads)
        tree_raw = tree_payload if isinstance(tree_payload, dict) else None
        details_raw = {
            canonical_type: {
                str(source_type): dict(payload)
                for source_type, payload in payloads.items()
                if isinstance(payload, dict)
            }
            for canonical_type, payloads in details_payloads.items()
            if isinstance(payloads, dict)
        }
        self._update_shared_state(
            _system_dashboard_devices_tree_raw=tree_raw,
            _system_dashboard_devices_details_raw=details_raw,
        )
        if isinstance(tree_raw, dict):
            # Raw dashboard payloads are kept separately; diagnostics only
            # exposes redacted copies.
            redacted_tree = self._redact_battery_payload(tree_raw)
            tree_payload_out = (
                redacted_tree if isinstance(redacted_tree, dict) else None
            )
        else:
            tree_payload_out = None

        redacted_details: dict[str, dict[str, object]] = {}
        for (
            canonical_type,
            payloads_by_source,
        ) in details_raw.items():
            merged: dict[str, object] = {}
            for source_type, payload in payloads_by_source.items():
                redacted = self._redact_battery_payload(payload)
                if isinstance(redacted, dict):
                    merged[source_type] = redacted
            redacted_details[canonical_type] = merged
        self._update_shared_state(
            _system_dashboard_devices_tree_payload=tree_payload_out,
            _system_dashboard_devices_details_payloads=redacted_details,
            _system_dashboard_detail_failures=detail_failures,
            _system_dashboard_type_summaries=type_summaries,
            _system_dashboard_hierarchy_summary=hierarchy_summary,
            _system_dashboard_hierarchy_index=hierarchy_index,
            _system_dashboard_cache_until=now + DEVICES_INVENTORY_CACHE_TTL,
        )
        self._debug_log_summary_if_changed(
            "system_dashboard",
            "System dashboard discovery summary",
            self._debug_system_dashboard_summary(
                self._system_dashboard_devices_tree_raw,
                self._system_dashboard_devices_details_raw,
                type_summaries,
                hierarchy_summary,
            ),
        )
        if fetched_payload or tree_raw is not None or details_raw:
            coord._note_endpoint_family_success(family)
        elif first_error is not None:
            coord._note_endpoint_family_failure(family, first_error)

    def _inverter_start_date(self) -> str | None:
        energy = getattr(self.coordinator, "energy", None)
        site_energy_meta = getattr(energy, "_site_energy_meta", None)
        return resolve_inverter_start_date(
            site_energy_meta,
            self._coordinator_backed_attr("_inverter_data"),
        )

    @staticmethod
    def _format_inverter_model_summary(model_counts: dict[str, int]) -> str | None:
        clean: dict[str, int] = {}
        for model, count in (model_counts or {}).items():
            name = str(model).strip()
            if not name:
                continue
            try:
                count_int = int(count)
            except (TypeError, ValueError):
                continue
            if count_int <= 0:
                continue
            clean[name] = count_int
        if not clean:
            return None
        ordered = sorted(clean.items(), key=lambda item: (-item[1], item[0]))
        return ", ".join(f"{name} x{count}" for name, count in ordered)

    @staticmethod
    def _format_inverter_status_summary(summary_counts: dict[str, int]) -> str:
        normal = int(summary_counts.get("normal", 0))
        warning = int(summary_counts.get("warning", 0))
        error = int(summary_counts.get("error", 0))
        not_reporting = int(summary_counts.get("not_reporting", 0))
        summary = (
            f"Normal {normal} | Warning {warning} | "
            f"Error {error} | Not Reporting {not_reporting}"
        )
        unknown = int(summary_counts.get("unknown", 0))
        if unknown > 0:
            summary = f"{summary} | Unknown {unknown}"
        return summary

    @staticmethod
    def _normalize_inverter_status(value: object) -> str:
        if value is None:
            return "unknown"
        try:
            normalized = str(value).strip().lower().replace("-", "_").replace(" ", "_")
        except Exception:
            return "unknown"
        if not normalized:
            return "unknown"
        if any(token in normalized for token in ("fault", "error", "critical")):
            return "error"
        if "warn" in normalized:
            return "warning"
        if any(
            token in normalized
            for token in ("not_reporting", "offline", "disconnected")
        ):
            return "not_reporting"
        if any(
            token in normalized
            for token in (
                "inactive",
                "unpaired",
                "not_paired",
                "notpaired",
                "deactivated",
                "decommissioned",
                "retired",
            )
        ):
            return "not_reporting"
        if any(token in normalized for token in ("pairing", "pending")):
            return "warning"
        if any(
            token in normalized
            for token in ("normal", "online", "connected", "ok", "recommended")
        ):
            return "normal"
        return "unknown"

    @staticmethod
    def _inverter_connectivity_state(summary_counts: dict[str, int]) -> str | None:
        total = int(summary_counts.get("total", 0))
        not_reporting = int(summary_counts.get("not_reporting", 0))
        unknown = int(summary_counts.get("unknown", 0))
        reporting = max(0, total - not_reporting - unknown)
        if total <= 0:
            return None
        if reporting >= total:
            return "online"
        if reporting == 0 and unknown > 0 and not_reporting <= 0:
            return "unknown"
        if reporting > 0:
            return "degraded"
        return "offline"

    @staticmethod
    def _parse_inverter_last_report(value: object) -> datetime | None:
        return parse_inverter_last_report(value)

    def _merge_microinverter_type_bucket(self) -> None:
        ready_before = bool(getattr(self, "_devices_inventory_ready", False))
        buckets = dict(getattr(self, "_type_device_buckets", {}) or {})
        ordered = list(getattr(self, "_type_device_order", []) or [])
        key = "microinverter"

        devices_out: list[dict[str, object]] = []
        for serial in self.iter_inverter_serials():
            payload = self.inverter_data(serial)
            if not isinstance(payload, dict):
                continue
            devices_out.append(
                {
                    "name": payload.get("name"),
                    "serial_number": payload.get("serial_number"),
                    "sku_id": payload.get("sku_id"),
                    "status": payload.get("status"),
                    "statusText": payload.get("status_text"),
                    "last_report": payload.get("last_report"),
                    "array_name": payload.get("array_name"),
                    "warranty_end_date": payload.get("warranty_end_date"),
                    "device_id": payload.get("device_id"),
                    "inverter_id": payload.get("inverter_id"),
                    "fw1": payload.get("fw1"),
                    "fw2": payload.get("fw2"),
                }
            )

        if devices_out:
            model_counts = dict(self._inverter_model_counts)
            model_summary = self._format_inverter_model_summary(model_counts)
            summary_counts = dict(self._inverter_summary_counts)
            status_counts = {
                "total": int(summary_counts.get("total", len(devices_out))),
                "normal": int(summary_counts.get("normal", 0)),
                "warning": int(summary_counts.get("warning", 0)),
                "error": int(summary_counts.get("error", 0)),
                "not_reporting": int(summary_counts.get("not_reporting", 0)),
                "unknown": int(summary_counts.get("unknown", 0)),
            }
            latest_reported: datetime | None = None
            latest_reported_device: dict[str, object] | None = None
            array_counts: dict[str, int] = {}
            firmware_counts: dict[str, int] = {}
            for member in devices_out:
                parsed_last = self._parse_inverter_last_report(
                    member.get("last_report")
                )
                if parsed_last is not None and (
                    latest_reported is None or parsed_last > latest_reported
                ):
                    latest_reported = parsed_last
                    latest_reported_device = {
                        "serial_number": member.get("serial_number"),
                        "name": member.get("name"),
                        "status": (
                            member.get("statusText")
                            if member.get("statusText") is not None
                            else member.get("status")
                        ),
                    }
                raw_array = member.get("array_name")
                if raw_array is not None:
                    try:
                        array_name = str(raw_array).strip()
                    except Exception:
                        array_name = ""
                    if array_name:
                        array_counts[array_name] = array_counts.get(array_name, 0) + 1
                raw_firmware = member.get("fw1") or member.get("fw2")
                if raw_firmware is not None:
                    try:
                        firmware = str(raw_firmware).strip()
                    except Exception:
                        firmware = ""
                    if firmware:
                        firmware_counts[firmware] = firmware_counts.get(firmware, 0) + 1
            array_summary = self._format_inverter_model_summary(array_counts)
            firmware_summary = self._format_inverter_model_summary(firmware_counts)
            buckets[key] = {
                "type_key": key,
                "type_label": "Microinverters",
                "count": len(devices_out),
                "devices": devices_out,
                "model_counts": model_counts,
                "model_summary": model_summary or "Microinverters",
                "status_counts": status_counts,
                "status_summary": self._format_inverter_status_summary(summary_counts),
                "connectivity_state": self._inverter_connectivity_state(status_counts),
                "reporting_count": max(
                    0,
                    int(status_counts.get("total", len(devices_out)))
                    - int(status_counts.get("not_reporting", 0))
                    - int(status_counts.get("unknown", 0)),
                ),
                "latest_reported_utc": (
                    latest_reported.isoformat() if latest_reported is not None else None
                ),
                "latest_reported_device": latest_reported_device,
                "array_counts": array_counts,
                "array_summary": array_summary,
                "firmware_counts": firmware_counts,
                "firmware_summary": firmware_summary,
            }
            panel_info = getattr(self, "_inverter_panel_info", None)
            if isinstance(panel_info, dict):
                buckets[key]["panel_info"] = dict(panel_info)
            production_payload = getattr(self, "_inverter_production_payload", None)
            if isinstance(production_payload, dict):
                start_date = self._normalize_iso_date(
                    production_payload.get("start_date")
                )
                end_date = self._normalize_iso_date(production_payload.get("end_date"))
                if start_date:
                    buckets[key]["production_start_date"] = start_date
                if end_date:
                    buckets[key]["production_end_date"] = end_date
            status_type_counts = getattr(self, "_inverter_status_type_counts", None)
            if isinstance(status_type_counts, dict) and status_type_counts:
                buckets[key]["status_type_counts"] = dict(status_type_counts)
            if key not in ordered:
                ordered.append(key)
        else:
            buckets.pop(key, None)
            ordered = [item for item in ordered if item != key]

        self._set_type_device_buckets(buckets, ordered)
        if not ready_before:
            self._devices_inventory_ready = False

    async def _async_refresh_inverters(self) -> None:
        """Refresh inverter metadata/status/production and build serial snapshots."""
        coord = self.coordinator
        now = time.monotonic()
        if not self.include_inverters:
            self._update_shared_state(
                _inverters_inventory_cache_until=None,
                _inverters_inventory_payload=None,
                _inverter_status_cache_until=None,
                _inverter_status_payload=None,
                _inverter_production_cache_until=None,
                _inverter_production_payload=None,
                _inverter_data={},
                _inverter_order=[],
                _inverter_panel_info=None,
                _inverter_status_type_counts={},
                _inverter_model_counts={},
                _inverter_production_cache_key=None,
                _inverter_summary_counts={
                    "total": 0,
                    "normal": 0,
                    "warning": 0,
                    "error": 0,
                    "not_reporting": 0,
                },
            )
            self._merge_microinverter_type_bucket()
            self._merge_heatpump_type_bucket()
            return

        fetch_inventory = getattr(self.client, "inverters_inventory", None)
        fetch_status = getattr(self.client, "inverter_status", None)
        fetch_production = getattr(self.client, "inverter_production", None)
        if not callable(fetch_inventory) or not callable(fetch_status):
            return

        inventory_family = "inverter_inventory"
        status_family = "inverter_status"
        production_family = "inverter_production"
        cached_inventory_payload = getattr(self, "_inverters_inventory_payload", None)
        if not isinstance(cached_inventory_payload, dict):
            cached_inventory_payload = None
        inventory_cache_until = getattr(self, "_inverters_inventory_cache_until", None)
        if not isinstance(inventory_cache_until, (int, float)):
            inventory_cache_until = None

        async def _fetch_inventory_page(offset: int) -> dict[str, object] | None:
            try:
                payload = await fetch_inventory(
                    limit=1000,
                    offset=offset,
                    search="",
                )
            except TypeError:
                if offset != 0:
                    return None
                payload = await fetch_inventory()
            except Exception:
                raise
            if not isinstance(payload, dict):
                return None
            return payload

        inventory_payload: dict[str, object] | None = None
        fetch_inventory_now = True
        if inventory_cache_until is not None and now < float(inventory_cache_until):
            fetch_inventory_now = False
        else:
            fetch_inventory_now = coord._endpoint_family_should_run(inventory_family)
        if fetch_inventory_now:
            try:
                inventory_payload = await _fetch_inventory_page(0)
            except Exception as err:  # noqa: BLE001
                coord._note_endpoint_family_failure(inventory_family, err)
                self._set_shared_state_attr(
                    "_inverters_inventory_cache_until",
                    coord._endpoint_family_next_retry_mono(inventory_family),
                )
                inventory_payload = cached_inventory_payload
            else:
                if inventory_payload is None:
                    coord._note_endpoint_family_failure(
                        inventory_family,
                        ValueError("Inverters inventory payload was not a dictionary"),
                    )
                    self._set_shared_state_attr(
                        "_inverters_inventory_cache_until",
                        coord._endpoint_family_next_retry_mono(inventory_family),
                    )
                    inventory_payload = cached_inventory_payload
        else:
            inventory_payload = cached_inventory_payload
        if inventory_payload is None:
            return

        inverters_raw = inventory_payload.get("inverters")
        if not isinstance(inverters_raw, list):
            inverters_raw = []
        inverters_list = [item for item in inverters_raw if isinstance(item, dict)]
        total_expected = self._coerce_int(
            inventory_payload.get("total"), default=len(inverters_list)
        )
        if total_expected > len(inverters_list):
            merged = list(inverters_list)
            next_offset = len(merged)
            while next_offset < total_expected:
                next_payload = await _fetch_inventory_page(next_offset)
                if next_payload is None:
                    break
                next_raw = next_payload.get("inverters")
                if not isinstance(next_raw, list):
                    break
                next_items = [item for item in next_raw if isinstance(item, dict)]
                if not next_items:
                    break
                merged.extend(next_items)
                total_candidate = self._coerce_int(
                    next_payload.get("total"), default=total_expected
                )
                if total_candidate > total_expected:
                    total_expected = total_candidate
                page_size = len(next_items)
                next_offset += page_size
            inventory_payload = dict(inventory_payload)
            inventory_payload["inverters"] = merged
            inverters_list = merged
        if fetch_inventory_now and inventory_payload is not cached_inventory_payload:
            coord._note_endpoint_family_success(inventory_family)
            self._set_shared_state_attr(
                "_inverters_inventory_cache_until",
                coord._endpoint_family_next_retry_mono(inventory_family),
            )

        cached_status_payload = getattr(self, "_inverter_status_payload", None)
        if not isinstance(cached_status_payload, dict):
            cached_status_payload = {}
        status_payload: dict[str, object] = {}
        status_cache_until = getattr(self, "_inverter_status_cache_until", None)
        if isinstance(status_cache_until, (int, float)) and now < float(
            status_cache_until
        ):
            status_payload = dict(cached_status_payload)
        elif coord._endpoint_family_should_run(status_family):
            try:
                fetched_status = await fetch_status()
            except Exception as err:  # noqa: BLE001
                coord._note_endpoint_family_failure(status_family, err)
                self._set_shared_state_attr(
                    "_inverter_status_cache_until",
                    coord._endpoint_family_next_retry_mono(status_family),
                )
                if coord._endpoint_family_can_use_stale(status_family):
                    status_payload = dict(cached_status_payload)
            else:
                if isinstance(fetched_status, dict):
                    status_payload = fetched_status
                    coord._note_endpoint_family_success(status_family)
                    self._set_shared_state_attr(
                        "_inverter_status_cache_until",
                        coord._endpoint_family_next_retry_mono(status_family),
                    )
                else:
                    coord._note_endpoint_family_failure(
                        status_family,
                        ValueError("Inverter status payload was not a dictionary"),
                    )
                    self._set_shared_state_attr(
                        "_inverter_status_cache_until",
                        coord._endpoint_family_next_retry_mono(status_family),
                    )
                    if coord._endpoint_family_can_use_stale(status_family):
                        status_payload = dict(cached_status_payload)
        elif coord._endpoint_family_can_use_stale(status_family):
            status_payload = dict(cached_status_payload)

        start_date = self._inverter_start_date()
        end_date = self._site_local_current_date()
        production_payload: dict[str, object] = {}
        cached_production_payload = getattr(self, "_inverter_production_payload", None)
        if not isinstance(cached_production_payload, dict):
            cached_production_payload = {}
        current_production_cache_key = (
            (start_date, end_date) if start_date is not None else None
        )
        production_cache_until = getattr(
            self,
            "_inverter_production_cache_until",
            None,
        )
        cached_production_matches = (
            current_production_cache_key is not None
            and getattr(self, "_inverter_production_cache_key", None)
            == current_production_cache_key
            and bool(cached_production_payload)
        )
        if callable(fetch_production) and start_date is not None:
            if (
                cached_production_matches
                and isinstance(production_cache_until, (int, float))
                and now < float(production_cache_until)
            ):
                production_payload = dict(cached_production_payload)
            elif coord._endpoint_family_should_run(production_family):
                try:
                    fetched_production = await fetch_production(
                        start_date=start_date, end_date=end_date
                    )
                except Exception as err:  # noqa: BLE001
                    coord._note_endpoint_family_failure(production_family, err)
                    self._set_shared_state_attr(
                        "_inverter_production_cache_until",
                        coord._endpoint_family_next_retry_mono(production_family),
                    )
                    if cached_production_matches:
                        production_payload = dict(cached_production_payload)
                else:
                    if isinstance(fetched_production, dict):
                        production_payload = fetched_production
                        coord._note_endpoint_family_success(production_family)
                        self._set_shared_state_attr(
                            "_inverter_production_cache_key",
                            current_production_cache_key,
                        )
                        self._set_shared_state_attr(
                            "_inverter_production_cache_until",
                            coord._endpoint_family_next_retry_mono(production_family),
                        )
                    else:
                        coord._note_endpoint_family_failure(
                            production_family,
                            ValueError(
                                "Inverter production payload was not a dictionary"
                            ),
                        )
                        self._set_shared_state_attr(
                            "_inverter_production_cache_until",
                            coord._endpoint_family_next_retry_mono(production_family),
                        )
                        if cached_production_matches:
                            production_payload = dict(cached_production_payload)
            elif cached_production_matches:
                production_payload = dict(cached_production_payload)
        elif start_date is None:
            _LOGGER.debug(
                "Skipping inverter production fetch for site %s: start date unknown",
                redact_site_id(self.site_id),
            )
        production_raw = production_payload.get("production")
        if not isinstance(production_raw, dict):
            production_raw = {}

        status_by_serial: dict[str, dict[str, object]] = {}
        for inverter_id, payload in status_payload.items():
            if not isinstance(payload, dict):
                continue
            serial = str(payload.get("serialNum") or "").strip()
            if not serial:
                continue
            item = dict(payload)
            item["inverter_id"] = str(inverter_id)
            status_by_serial[serial] = item

        previous_data = self._coordinator_backed_attr("_inverter_data")
        if not isinstance(previous_data, dict):
            previous_data = {}

        inverter_data: dict[str, dict[str, object]] = {}
        inverter_order: list[str] = []
        model_counts: dict[str, int] = {}
        status_type_counts: dict[str, int] = {}
        derived_status_counts: dict[str, int] = {
            "normal": 0,
            "warning": 0,
            "error": 0,
            "not_reporting": 0,
        }
        for item in inverters_list:
            if self.member_is_retired(item):
                continue
            serial = str(item.get("serial_number") or "").strip()
            if not serial:
                continue
            previous_item = previous_data.get(serial)
            if not isinstance(previous_item, dict):
                previous_item = {}
            status_item = status_by_serial.get(serial, {})
            inverter_id = (
                str(
                    status_item.get("inverter_id")
                    or previous_item.get("inverter_id")
                    or ""
                ).strip()
                or None
            )
            production_wh = None
            if inverter_id:
                try:
                    raw_val = production_raw.get(inverter_id)
                    production_wh = float(raw_val) if raw_val is not None else None
                except (TypeError, ValueError):
                    production_wh = None
            prev_wh: float | None = None
            try:
                raw_prev_wh = previous_item.get("lifetime_production_wh")
                prev_wh = float(raw_prev_wh) if raw_prev_wh is not None else None
            except (TypeError, ValueError):
                prev_wh = None
            if production_wh is None or production_wh < 0:
                production_wh = prev_wh
            elif prev_wh is not None and production_wh < prev_wh:
                production_wh = prev_wh

            query_start = self._normalize_iso_date(production_payload.get("start_date"))
            query_end = self._normalize_iso_date(production_payload.get("end_date"))
            if query_start is None:
                query_start = self._normalize_iso_date(
                    previous_item.get("lifetime_query_start_date")
                )
            if query_end is None:
                query_end = self._normalize_iso_date(
                    previous_item.get("lifetime_query_end_date")
                )
            if query_start is None:
                query_start = start_date
            if query_end is None:
                query_end = end_date

            model_name = str(item.get("name") or "").strip()
            if model_name:
                model_counts[model_name] = model_counts.get(model_name, 0) + 1
            status_type = status_item.get("type")
            if status_type is not None:
                try:
                    status_type_text = str(status_type).strip()
                except Exception:
                    status_type_text = ""
                if status_type_text:
                    status_type_counts[status_type_text] = (
                        status_type_counts.get(status_type_text, 0) + 1
                    )
            status_bucket = self._normalize_inverter_status(
                status_item.get("statusCode")
                if status_item.get("statusCode") is not None
                else (
                    status_item.get("status")
                    if status_item.get("status") is not None
                    else item.get("status")
                )
            )
            if status_bucket in derived_status_counts:
                derived_status_counts[status_bucket] += 1
            inverter_data[serial] = {
                "serial_number": serial,
                "name": item.get("name"),
                "array_name": item.get("array_name"),
                "sku_id": item.get("sku_id"),
                "part_num": item.get("part_num"),
                "sku": item.get("sku"),
                "status": item.get("status"),
                "status_text": item.get("statusText"),
                "last_report": item.get("last_report"),
                "fw1": item.get("fw1"),
                "fw2": item.get("fw2"),
                "warranty_end_date": item.get("warranty_end_date"),
                "inverter_id": inverter_id,
                "device_id": status_item.get(
                    "deviceId", previous_item.get("device_id")
                ),
                "inverter_type": status_item.get(
                    "type", previous_item.get("inverter_type")
                ),
                "status_code": status_item.get(
                    "statusCode", previous_item.get("status_code")
                ),
                "show_sig_str": status_item.get(
                    "show_sig_str", previous_item.get("show_sig_str")
                ),
                "emu_version": status_item.get(
                    "emu_version", previous_item.get("emu_version")
                ),
                "issi": status_item.get("issi", previous_item.get("issi")),
                "rssi": status_item.get("rssi", previous_item.get("rssi")),
                "lifetime_production_wh": production_wh,
                "lifetime_query_start_date": query_start,
                "lifetime_query_end_date": query_end,
            }
            inverter_order.append(serial)

        total_count = len(inverter_data)
        normal_count = int(
            derived_status_counts.get("normal")
            or self._coerce_int(inventory_payload.get("normal_count"), default=0)
        )
        warning_count = int(
            derived_status_counts.get("warning")
            or self._coerce_int(inventory_payload.get("warning_count"), default=0)
        )
        error_count = int(
            derived_status_counts.get("error")
            or self._coerce_int(inventory_payload.get("error_count"), default=0)
        )
        not_reporting_count = int(
            derived_status_counts.get("not_reporting")
            or self._coerce_int(inventory_payload.get("not_reporting"), default=0)
        )
        normal_count = max(0, normal_count)
        warning_count = max(0, warning_count)
        error_count = max(0, error_count)
        not_reporting_count = max(0, not_reporting_count)
        counts = {
            "normal": normal_count,
            "warning": warning_count,
            "error": error_count,
            "not_reporting": not_reporting_count,
        }
        known_total = sum(counts.values())
        if known_total > total_count:
            overflow = known_total - total_count
            for key in ("not_reporting", "error", "warning", "normal"):
                if overflow <= 0:
                    break
                reducible = min(counts[key], overflow)
                counts[key] -= reducible
                overflow -= reducible
        known_total = sum(counts.values())
        unknown_count = max(0, total_count - known_total)

        summary_counts = {
            "total": total_count,
            "normal": counts["normal"],
            "warning": counts["warning"],
            "error": counts["error"],
            "not_reporting": counts["not_reporting"],
            "unknown": unknown_count,
        }
        panel_info_out: dict[str, object] | None = None
        panel_info_raw = inventory_payload.get("panel_info")
        if isinstance(panel_info_raw, dict):
            panel_info_out = {}
            for key, value in panel_info_raw.items():
                if value is None:
                    continue
                if isinstance(value, (str, int, float, bool)):
                    if isinstance(value, str):
                        value = value.strip()
                        if not value:
                            continue
                    panel_info_out[str(key)] = value
            if not panel_info_out:
                panel_info_out = None

        self._update_shared_state(
            _inverters_inventory_payload=inventory_payload,
            _inverter_status_payload=status_payload,
            _inverter_production_payload=production_payload,
            _inverter_data=inverter_data,
            _inverter_order=inverter_order,
            _inverter_panel_info=panel_info_out,
            _inverter_status_type_counts=status_type_counts,
            _inverter_model_counts=model_counts,
            _inverter_summary_counts=summary_counts,
        )
        self._merge_microinverter_type_bucket()
        self._merge_heatpump_type_bucket()

    def inverters_refresh_due(self, *, force: bool = False) -> bool:
        coord = self.coordinator
        now = time.monotonic()
        if not self.include_inverters:
            return False
        fetch_inventory = getattr(self.client, "inverters_inventory", None)
        fetch_status = getattr(self.client, "inverter_status", None)
        if not callable(fetch_inventory) or not callable(fetch_status):
            return False
        inventory_cache_until = getattr(self, "_inverters_inventory_cache_until", None)
        inventory_due = True
        if isinstance(inventory_cache_until, (int, float)) and now < float(
            inventory_cache_until
        ):
            inventory_due = False
        else:
            inventory_due = coord._endpoint_family_should_run(
                "inverter_inventory", force=force
            )
        status_cache_until = getattr(self, "_inverter_status_cache_until", None)
        status_due = False
        if isinstance(status_cache_until, (int, float)) and now < float(
            status_cache_until
        ):
            status_due = False
        else:
            status_due = coord._endpoint_family_should_run(
                "inverter_status", force=force
            )
        start_date = self._inverter_start_date()
        production_due = False
        if start_date is not None:
            end_date = self._site_local_current_date()
            current_key = (start_date, end_date)
            cached_payload = getattr(self, "_inverter_production_payload", None)
            cached_matches = (
                getattr(self, "_inverter_production_cache_key", None) == current_key
                and isinstance(cached_payload, dict)
                and bool(cached_payload)
            )
            production_cache_until = getattr(
                self,
                "_inverter_production_cache_until",
                None,
            )
            if not (
                cached_matches
                and isinstance(production_cache_until, (int, float))
                and now < float(production_cache_until)
            ):
                production_fetcher = getattr(self.client, "inverter_production", None)
                production_due = callable(
                    production_fetcher
                ) and coord._endpoint_family_should_run(
                    "inverter_production", force=force
                )
        return inventory_due or status_due or production_due

    def iter_inverter_serials(self) -> list[str]:
        """Return currently active inverter serials in a stable order."""
        order = self._coordinator_backed_attr("_inverter_order", []) or []
        data = self._coordinator_backed_attr("_inverter_data")
        if not isinstance(data, dict):
            return []
        serials = [str(sn) for sn in order if sn in data]
        serials.extend(str(sn) for sn in data.keys())
        return [sn for sn in dict.fromkeys(serials) if sn]

    def inverter_data(self, serial: str) -> dict[str, object] | None:
        """Return normalized inverter snapshot for a serial."""
        data = self._coordinator_backed_attr("_inverter_data")
        if not isinstance(data, dict):
            return None
        try:
            key = str(serial).strip()
        except Exception:
            return None
        if not key:
            return None
        payload = data.get(key)
        if not isinstance(payload, dict):
            return None
        return dict(payload)

    def inverter_diagnostics_payloads(self) -> dict[str, object]:
        """Return inverter-related payload snapshots used by diagnostics."""
        cache_until = getattr(self, "_inverter_production_cache_until", None)
        cache_key = getattr(self, "_inverter_production_cache_key", None)
        cache_remaining_s = None
        cache_age_s = None
        if isinstance(cache_until, (int, float)):
            cache_remaining_s = max(0.0, float(cache_until) - time.monotonic())
        production_health = self.coordinator._endpoint_family_state(
            "inverter_production"
        )
        last_success_mono = getattr(production_health, "last_success_mono", None)
        if isinstance(last_success_mono, (int, float)):
            cache_age_s = max(0.0, time.monotonic() - float(last_success_mono))
        bucket_snapshot = self.type_bucket("microinverter")
        return {
            "enabled": bool(getattr(self, "include_inverters", True)),
            "summary_counts": getattr(self, "_inverter_summary_counts", None),
            "model_counts": getattr(self, "_inverter_model_counts", None),
            "status_type_counts": getattr(self, "_inverter_status_type_counts", None),
            "panel_info": getattr(self, "_inverter_panel_info", None),
            "inventory_payload": getattr(self, "_inverters_inventory_payload", None),
            "status_payload": getattr(self, "_inverter_status_payload", None),
            "production_payload": getattr(self, "_inverter_production_payload", None),
            "production_cache_key": cache_key,
            "production_cache_remaining_seconds": cache_remaining_s,
            "production_cache_age_seconds": cache_age_s,
            "bucket_snapshot": bucket_snapshot,
        }

    def _system_dashboard_raw_payloads(
        self, canonical_type: str
    ) -> dict[str, dict[str, object]]:
        payloads = getattr(self, "_system_dashboard_devices_details_raw", None)
        if not isinstance(payloads, dict):
            return {}
        raw = payloads.get(canonical_type)
        if not isinstance(raw, dict):
            return {}
        return {
            str(source_type): dict(payload)
            for source_type, payload in raw.items()
            if isinstance(payload, dict)
        }

    def system_dashboard_envoy_detail(self) -> dict[str, object] | None:
        records = self._system_dashboard_detail_records(
            self._system_dashboard_raw_payloads("envoy"),
            "envoys",
            "envoy",
        )
        if not records:
            return None
        record = records[0]
        out: dict[str, object] = {}
        for key in (
            "status",
            "statusText",
            "connected",
            "last_report",
            "last_interval_end_date",
            "envoy_sw_version",
            "ap_mode",
            "sku_id",
        ):
            value = record.get(key)
            if value is not None:
                out[key] = value
        return out or None

    def system_dashboard_meter_detail(
        self, meter_kind: str
    ) -> dict[str, object] | None:
        for record in self._system_dashboard_detail_records(
            self._system_dashboard_raw_payloads("envoy"),
            "meters",
            "meter",
        ):
            if self._system_dashboard_meter_kind(record) != meter_kind:
                continue
            out: dict[str, object] = {}
            for key in (
                "name",
                "serial_number",
                "channel_type",
                "status",
                "statusText",
                "last_report",
                "meter_state",
                "config_type",
                "meter_type",
            ):
                value = record.get(key)
                if value is not None:
                    out[key] = value
            return out or None
        return None

    def system_dashboard_battery_detail(self, serial: str) -> dict[str, object] | None:
        snapshots = getattr(self, "_battery_storage_data", None)
        snapshot = snapshots.get(serial) if isinstance(snapshots, dict) else None
        candidates: set[str] = set()
        for value in (
            serial,
            snapshot.get("serial_number") if isinstance(snapshot, dict) else None,
            snapshot.get("identity") if isinstance(snapshot, dict) else None,
            snapshot.get("battery_id") if isinstance(snapshot, dict) else None,
            snapshot.get("id") if isinstance(snapshot, dict) else None,
        ):
            text = self._coerce_optional_text(value)
            if text:
                candidates.add(text)
        if not candidates:
            return None
        for record in self._system_dashboard_detail_records(
            self._system_dashboard_raw_payloads("encharge"),
            "encharges",
            "encharge",
        ):
            record_serial = self._coerce_optional_text(record.get("serial_number"))
            record_id = self._coerce_optional_text(record.get("id"))
            if record_serial not in candidates and record_id not in candidates:
                continue
            detail = self._system_dashboard_battery_detail_subset(record)
            return detail or None
        return None

    def system_dashboard_diagnostics(self) -> dict[str, object]:
        devices_tree_payload = getattr(
            self, "_system_dashboard_devices_tree_payload", None
        )
        devices_details_payloads = getattr(
            self, "_system_dashboard_devices_details_payloads", None
        )
        detail_failures = getattr(self, "_system_dashboard_detail_failures", None)
        hierarchy_summary = getattr(self, "_system_dashboard_hierarchy_summary", None)
        type_summaries = getattr(self, "_system_dashboard_type_summaries", None)
        out: dict[str, object] = {
            "devices_tree_payload": (
                self._copy_diagnostics_value(devices_tree_payload)
                if isinstance(devices_tree_payload, dict)
                else None
            ),
            "devices_details_payloads": (
                self._copy_diagnostics_value(devices_details_payloads)
                if isinstance(devices_details_payloads, dict)
                else {}
            ),
            "detail_failures": (
                self._copy_diagnostics_value(detail_failures)
                if isinstance(detail_failures, dict)
                else {}
            ),
            "hierarchy_summary": (
                self._copy_diagnostics_value(hierarchy_summary)
                if isinstance(hierarchy_summary, dict)
                else {}
            ),
            "type_summaries": (
                self._copy_diagnostics_value(type_summaries)
                if isinstance(type_summaries, dict)
                else {}
            ),
        }
        return out

    @staticmethod
    def member_is_retired(member: dict[str, object]) -> bool:
        return device_member_is_retired(member)


install_state_descriptors(InventoryRuntime)
