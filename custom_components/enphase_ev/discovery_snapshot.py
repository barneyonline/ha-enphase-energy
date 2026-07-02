from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .device_types import normalize_type_key
from .log_redaction import redact_site_id

if TYPE_CHECKING:  # pragma: no cover
    from .coordinator import EnphaseCoordinator

_LOGGER = logging.getLogger(__name__)

DISCOVERY_SNAPSHOT_STORE_VERSION = 1
DISCOVERY_SNAPSHOT_SAVE_DELAY_S = 1.0


def _snapshot_compatible_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for key, item in value.items():
            try:
                key_text = str(key)
            except Exception:  # noqa: BLE001
                continue
            out[key_text] = _snapshot_compatible_value(item)
        return out
    if isinstance(value, (list, tuple, set)):
        return [_snapshot_compatible_value(item) for item in value]
    try:
        return str(value)
    except Exception:  # noqa: BLE001
        return None


def _snapshot_bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1", "yes", "y", "enabled", "on"):
            return True
        if normalized in ("false", "0", "no", "n", "disabled", "off"):
            return False
    return None


class DiscoverySnapshotManager:
    """Persist and restore discovery-oriented coordinator state."""

    def __init__(self, coordinator: EnphaseCoordinator) -> None:
        self.coordinator = coordinator
        entry_id = getattr(coordinator.config_entry, "entry_id", coordinator.site_id)
        self._store = Store(
            coordinator.hass,
            DISCOVERY_SNAPSHOT_STORE_VERSION,
            f"{DOMAIN}.discovery_snapshot.{entry_id}",
        )
        self._last_persisted_signature: str | None = None

    def _snapshot_signature(self, snapshot: object) -> str:
        compatible = _snapshot_compatible_value(snapshot)
        return json.dumps(
            compatible,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )

    def _capture_signature(self) -> tuple[dict[str, object], str]:
        snapshot = self.capture()
        return snapshot, self._snapshot_signature(snapshot)

    def _mark_persisted(self, snapshot: object) -> None:
        self._last_persisted_signature = self._snapshot_signature(snapshot)

    def live_site_energy_channels(self) -> set[str]:
        channels: set[str] = set()
        energy = getattr(self.coordinator, "energy", None)
        if energy is None:
            return channels
        flows = getattr(energy, "site_energy", None)
        if isinstance(flows, dict):
            for key in flows:
                try:
                    key_text = str(key).strip()
                except Exception:  # noqa: BLE001
                    continue
                if key_text:
                    channels.add(key_text)
        meta = getattr(energy, "site_energy_meta", None)
        if isinstance(meta, dict):
            bucket_lengths = meta.get("bucket_lengths")
            if isinstance(bucket_lengths, dict):
                for key, value in bucket_lengths.items():
                    try:
                        key_text = str(key).strip()
                    except Exception:  # noqa: BLE001
                        continue
                    if not key_text:
                        continue
                    try:
                        if int(value) <= 0:
                            continue
                    except Exception:  # noqa: BLE001
                        if not value:
                            continue
                    mapped = {
                        "heatpump": "heat_pump",
                        "water_heater": "water_heater",
                        "evse": "evse_charging",
                        "solar_production": "solar_production",
                        "consumption": "consumption",
                        "grid_import": "grid_import",
                        "grid_export": "grid_export",
                        "battery_charge": "battery_charge",
                        "battery_discharge": "battery_discharge",
                    }.get(key_text, key_text)
                    channels.add(mapped)
        return channels

    def site_energy_channel_known(self, flow_key: str) -> bool:
        try:
            key = str(flow_key).strip()
        except Exception:  # noqa: BLE001
            return False
        if not key:
            return False
        if key in self.live_site_energy_channels():
            return True
        if self.coordinator._site_energy_discovery_ready:
            return False
        return key in self.coordinator._restored_site_energy_channels

    def gateway_router_discovery_ready(self) -> bool:
        return bool(getattr(self.coordinator, "_hems_inventory_ready", False))

    def gateway_iq_energy_router_records(self) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        for member in self.coordinator.inventory_runtime._hems_group_members("gateway"):
            if not isinstance(member, dict):
                continue
            raw_type = member.get("device-type")
            if raw_type is None:
                raw_type = member.get("device_type")
            if raw_type is None:
                continue
            try:
                type_text = str(raw_type).strip().upper()
            except Exception:  # noqa: BLE001
                continue
            if type_text != "IQ_ENERGY_ROUTER":
                continue
            records.append(dict(member))
        if records:
            return records
        if self.gateway_router_discovery_ready():
            return []
        return [
            dict(item)
            for item in self.coordinator._restored_gateway_iq_energy_router_records
            if isinstance(item, dict)
        ]

    def capture(self) -> dict[str, object]:
        site_energy_channels = self.live_site_energy_channels()
        if (
            not site_energy_channels
            and not self.coordinator._site_energy_discovery_ready
        ):
            site_energy_channels = set(self.coordinator._restored_site_energy_channels)
        router_records = self.gateway_iq_energy_router_records()
        snapshot = {
            "serial_order": self.coordinator.iter_serials(),
            "type_device_order": list(
                getattr(self.coordinator, "_type_device_order", []) or []
            ),
            "type_device_buckets": _snapshot_compatible_value(
                dict(getattr(self.coordinator, "_type_device_buckets", {}) or {})
            ),
            "battery_storage_order": list(
                getattr(self.coordinator, "_battery_storage_order", []) or []
            ),
            "battery_storage_data": _snapshot_compatible_value(
                dict(getattr(self.coordinator, "_battery_storage_data", {}) or {})
            ),
            "inverter_order": list(
                getattr(self.coordinator, "_inverter_order", []) or []
            ),
            "inverter_data": _snapshot_compatible_value(
                dict(getattr(self.coordinator, "_inverter_data", {}) or {})
            ),
            "battery_has_encharge": getattr(
                self.coordinator, "_battery_has_encharge", None
            ),
            "battery_has_enpower": getattr(
                self.coordinator, "_battery_has_enpower", None
            ),
            "heatpump_known_present": bool(
                getattr(self.coordinator, "_heatpump_known_present", False)
                or (
                    isinstance(
                        getattr(self.coordinator, "_type_device_buckets", None), dict
                    )
                    and "heatpump"
                    in getattr(self.coordinator, "_type_device_buckets", {})
                )
            ),
            "site_energy_channels": sorted(site_energy_channels),
            "gateway_iq_energy_router_records": _snapshot_compatible_value(
                router_records
            ),
        }
        return snapshot

    def apply(self, snapshot: object) -> None:
        if not isinstance(snapshot, dict):
            return

        serial_order = snapshot.get("serial_order")
        if isinstance(serial_order, list):
            restored_serials: list[str] = []
            for serial in serial_order:
                if serial is None:
                    continue
                try:
                    text = str(serial).strip()
                except Exception:  # noqa: BLE001
                    continue
                if text:
                    restored_serials.append(text)
            restored_serials = list(dict.fromkeys(restored_serials))
            self.coordinator._restored_evse_serial_order = restored_serials
            if restored_serials:
                self.coordinator._serial_order = list(restored_serials)
                self.coordinator.serials = set(restored_serials)

        grouped = snapshot.get("type_device_buckets")
        ordered = snapshot.get("type_device_order")
        if isinstance(grouped, dict):
            normalized_grouped: dict[str, dict[str, object]] = {}
            for raw_key, raw_bucket in grouped.items():
                type_key = normalize_type_key(raw_key)
                if not type_key or not isinstance(raw_bucket, dict):
                    continue
                bucket = dict(raw_bucket)
                members = bucket.get("devices")
                if isinstance(members, list):
                    bucket["devices"] = [
                        dict(member) for member in members if isinstance(member, dict)
                    ]
                else:
                    bucket["devices"] = []
                try:
                    count = int(bucket.get("count", len(bucket["devices"])) or 0)
                except Exception:  # noqa: BLE001
                    count = len(bucket["devices"])
                bucket["count"] = max(count, len(bucket["devices"]))
                normalized_grouped[type_key] = bucket
            ordered_keys = (
                [normalize_type_key(key) for key in ordered if normalize_type_key(key)]
                if isinstance(ordered, list)
                else list(normalized_grouped.keys())
            )
            if normalized_grouped:
                self.coordinator.inventory_runtime._set_type_device_buckets(
                    normalized_grouped, ordered_keys, authoritative=False
                )

        battery_order = snapshot.get("battery_storage_order")
        battery_data = snapshot.get("battery_storage_data")
        if isinstance(battery_order, list) and isinstance(battery_data, dict):
            self.coordinator._battery_storage_order = [
                str(item).strip() for item in battery_order if str(item).strip()
            ]
            self.coordinator._battery_storage_data = {
                str(key).strip(): dict(value)
                for key, value in battery_data.items()
                if str(key).strip() and isinstance(value, dict)
            }

        inverter_order = snapshot.get("inverter_order")
        inverter_data = snapshot.get("inverter_data")
        if isinstance(inverter_order, list) and isinstance(inverter_data, dict):
            restored_inverter_order = [
                str(item).strip() for item in inverter_order if str(item).strip()
            ]
            restored_inverter_data = {
                str(key).strip(): dict(value)
                for key, value in inverter_data.items()
                if str(key).strip() and isinstance(value, dict)
            }
            self.coordinator.inventory_runtime._update_shared_state(
                _inverter_order=restored_inverter_order,
                _inverter_data=restored_inverter_data,
            )

        has_encharge = _snapshot_bool(snapshot.get("battery_has_encharge"))
        if has_encharge is not None:
            self.coordinator._battery_has_encharge = has_encharge
        has_enpower = _snapshot_bool(snapshot.get("battery_has_enpower"))
        if has_enpower is not None:
            self.coordinator._battery_has_enpower = has_enpower
        heatpump_known_present = _snapshot_bool(snapshot.get("heatpump_known_present"))
        if heatpump_known_present is not None:
            self.coordinator._heatpump_known_present = heatpump_known_present

        restored_channels = snapshot.get("site_energy_channels")
        if isinstance(restored_channels, list):
            self.coordinator._restored_site_energy_channels = {
                str(item).strip() for item in restored_channels if str(item).strip()
            }

        restored_router_records = snapshot.get("gateway_iq_energy_router_records")
        if isinstance(restored_router_records, list):
            self.coordinator._restored_gateway_iq_energy_router_records = [
                dict(item) for item in restored_router_records if isinstance(item, dict)
            ]
        self.coordinator._refresh_cached_topology()
        self._mark_persisted(self.capture())

    async def async_restore_state(self) -> None:
        if self.coordinator._discovery_snapshot_loaded:
            return
        self.coordinator._discovery_snapshot_loaded = True
        self.coordinator._devices_inventory_ready = False
        self.coordinator._hems_inventory_ready = False
        self.coordinator._site_energy_discovery_ready = False
        try:
            snapshot = await self._store.async_load()
        except Exception:  # noqa: BLE001
            _LOGGER.debug(
                "Failed to load discovery snapshot for site %s",
                redact_site_id(self.coordinator.site_id),
                exc_info=True,
            )
            return
        self.apply(snapshot)

    async def async_save(self) -> None:
        self.coordinator._discovery_snapshot_pending = False
        snapshot, signature = self._capture_signature()
        if signature == self._last_persisted_signature:
            return
        try:
            await self._store.async_save(snapshot)
        except Exception:  # noqa: BLE001
            _LOGGER.debug(
                "Failed to save discovery snapshot for site %s",
                redact_site_id(self.coordinator.site_id),
                exc_info=True,
            )
            return
        self._last_persisted_signature = signature

    def schedule_save(self) -> None:
        _, signature = self._capture_signature()
        if signature == self._last_persisted_signature:
            self.coordinator._discovery_snapshot_pending = False
            return
        self.coordinator._discovery_snapshot_pending = True
        if self.coordinator._discovery_snapshot_save_cancel is not None:
            return

        @callback
        def _run(_now: datetime) -> None:
            self.coordinator._discovery_snapshot_save_cancel = None
            if not self.coordinator._discovery_snapshot_pending:
                return
            self.coordinator.hass.async_create_task(
                self.async_save(), name=f"{DOMAIN}_discovery_snapshot_save"
            )

        self.coordinator._discovery_snapshot_save_cancel = async_call_later(
            self.coordinator.hass, DISCOVERY_SNAPSHOT_SAVE_DELAY_S, _run
        )

    def cancel_pending_save(self) -> None:
        if self.coordinator._discovery_snapshot_save_cancel is not None:
            self.coordinator._discovery_snapshot_save_cancel()
            self.coordinator._discovery_snapshot_save_cancel = None

    def sync_site_energy_discovery_state(self) -> None:
        energy = getattr(self.coordinator, "energy", None)
        if energy is None:
            return
        if getattr(energy, "_site_energy_cache_ts", None) is not None:
            self.coordinator._site_energy_discovery_ready = True
