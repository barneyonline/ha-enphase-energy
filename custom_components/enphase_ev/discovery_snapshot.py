from __future__ import annotations

import asyncio
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

_DISCOVERY_RECORD_KEYS = frozenset(
    {
        "array_name",
        "battery_id",
        "channel_type",
        "channelType",
        "application-version",
        "applicationVersion",
        "application_version",
        "ap_mode",
        "device-id",
        "device-type",
        "device-uid",
        "device_id",
        "device_sn",
        "device_type",
        "device_uid",
        "fw1",
        "fw2",
        "firmware-version",
        "firmwareVersion",
        "firmware_version",
        "hardware_sku",
        "hardware-version",
        "hardwareVersion",
        "hardware_version",
        "hems_device_facet_id",
        "hems_device_id",
        "id",
        "identity",
        "inverter_id",
        "inverter_type",
        "ip",
        "ip_address",
        "iqer_uid",
        "manufacturer",
        "model",
        "model_name",
        "modelId",
        "model_id",
        "name",
        "part_num",
        "part_number",
        "parent",
        "parentDeviceUid",
        "parentId",
        "parentUid",
        "parent_device_uid",
        "parent_id",
        "parent_uid",
        "phase",
        "serial",
        "serialNumber",
        "serial_number",
        "sku",
        "sku_id",
        "sn",
        "software-version",
        "softwareVersion",
        "software_version",
        "supportsEntrez",
        "sw_version",
        "system_version",
        "type",
        "type_label",
        "uid",
    }
)


def _is_discovery_record_key(key: object) -> bool:
    key_text = str(key)
    if key_text in _DISCOVERY_RECORD_KEYS:
        return True
    normalized = key_text.strip().lower().replace("-", "_")
    return bool(
        normalized.endswith(("_id", "_uid", "_version"))
        or normalized.startswith(("has_", "supports_"))
        or any(
            token in normalized
            for token in (
                "capability",
                "device_type",
                "firmware",
                "hardware_sku",
                "model",
                "parent",
                "serial",
            )
        )
    )


def _compact_discovery_record(record: object) -> dict[str, object]:
    """Return only stable identity and capability fields used for discovery."""

    if not isinstance(record, dict):
        return {}
    return {
        str(key): _snapshot_compatible_value(value)
        for key, value in record.items()
        if _is_discovery_record_key(key) and value is not None
    }


def _compact_type_buckets(value: object) -> dict[str, object]:
    """Compact inventory buckets without persisting changing telemetry."""

    if not isinstance(value, dict):
        return {}
    compact: dict[str, object] = {}
    for raw_key, raw_bucket in value.items():
        if not isinstance(raw_bucket, dict):
            continue
        bucket: dict[str, object] = {}
        for key in ("count", "type_label", "model_counts", "model_summary"):
            item = raw_bucket.get(key)
            if item is not None:
                bucket[key] = _snapshot_compatible_value(item)
        members = raw_bucket.get("devices")
        if isinstance(members, list):
            bucket["devices"] = [
                compact_member
                for member in members
                if (compact_member := _compact_discovery_record(member))
            ]
        else:
            bucket["devices"] = []
        compact[str(raw_key)] = bucket
    return compact


def _compact_keyed_records(value: object) -> dict[str, object]:
    """Compact a serial-keyed discovery mapping."""

    if not isinstance(value, dict):
        return {}
    return {
        str(key): _compact_discovery_record(record)
        for key, record in value.items()
        if isinstance(record, dict)
    }


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
        self._revision = 0
        self._persisted_revision = -1
        self._last_observed_key: object | None = None
        self._pending_snapshot: dict[str, object] | None = None
        self._pending_signature: str | None = None
        self._pending_revision: int | None = None
        self._save_in_progress = False
        self._save_task: asyncio.Task[None] | None = None

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

    @staticmethod
    def _record_key(record: object) -> tuple[tuple[str, str], ...]:
        compact = _compact_discovery_record(record)
        return tuple(sorted((key, repr(value)) for key, value in compact.items()))

    def _discovery_key(self) -> tuple[object, ...]:
        """Build a lightweight key for discovery-affecting coordinator state."""

        type_buckets = getattr(self.coordinator, "_type_device_buckets", {}) or {}
        type_key: list[object] = []
        if isinstance(type_buckets, dict):
            for raw_key, raw_bucket in type_buckets.items():
                if not isinstance(raw_bucket, dict):
                    continue
                members = raw_bucket.get("devices")
                member_keys = (
                    tuple(self._record_key(member) for member in members)
                    if isinstance(members, list)
                    else ()
                )
                type_key.append(
                    (
                        str(raw_key),
                        raw_bucket.get("count"),
                        raw_bucket.get("type_label"),
                        member_keys,
                    )
                )
        battery_data = getattr(self.coordinator, "_battery_storage_data", {}) or {}
        battery_key = (
            tuple(
                (str(key), self._record_key(record))
                for key, record in battery_data.items()
            )
            if isinstance(battery_data, dict)
            else ()
        )
        inverter_data = getattr(self.coordinator, "_inverter_data", {}) or {}
        inverter_key = (
            tuple(
                (str(key), self._record_key(record))
                for key, record in inverter_data.items()
            )
            if isinstance(inverter_data, dict)
            else ()
        )
        return (
            tuple(self.coordinator.iter_serials()),
            tuple(getattr(self.coordinator, "_type_device_order", []) or []),
            tuple(type_key),
            tuple(getattr(self.coordinator, "_battery_storage_order", []) or []),
            battery_key,
            tuple(getattr(self.coordinator, "_inverter_order", []) or []),
            inverter_key,
            getattr(self.coordinator, "_battery_has_encharge", None),
            getattr(self.coordinator, "_battery_has_enpower", None),
            bool(getattr(self.coordinator, "_heatpump_known_present", False)),
            bool(getattr(self.coordinator, "_site_energy_discovery_ready", False)),
            tuple(sorted(self.live_site_energy_channels())),
            tuple(
                self._record_key(record)
                for record in self.gateway_iq_energy_router_records()
            ),
        )

    def _observe_revision(self) -> bool:
        key = self._discovery_key()
        if key == self._last_observed_key:
            return False
        self._last_observed_key = key
        self._revision += 1
        return True

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
            "type_device_buckets": _compact_type_buckets(
                getattr(self.coordinator, "_type_device_buckets", {})
            ),
            "battery_storage_order": list(
                getattr(self.coordinator, "_battery_storage_order", []) or []
            ),
            "battery_storage_data": _compact_keyed_records(
                getattr(self.coordinator, "_battery_storage_data", {})
            ),
            "inverter_order": list(
                getattr(self.coordinator, "_inverter_order", []) or []
            ),
            "inverter_data": _compact_keyed_records(
                getattr(self.coordinator, "_inverter_data", {})
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
            "gateway_iq_energy_router_records": [
                compact
                for record in router_records
                if (compact := _compact_discovery_record(record))
            ],
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
            ordered_keys: list[str] = []
            if isinstance(ordered, list):
                for key in ordered:
                    normalized_key = normalize_type_key(key)
                    if normalized_key:
                        ordered_keys.append(normalized_key)
            else:  # pragma: no cover - persisted snapshots always store a list
                ordered_keys = list(normalized_grouped)
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
        # Normalize legacy/full snapshots to the compact representation once on
        # restore so the next unchanged refresh does not rewrite storage.
        self._mark_persisted(self.capture())
        self._last_observed_key = self._discovery_key()
        self._persisted_revision = self._revision

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
        if self._pending_snapshot is None:
            self._observe_revision()
            snapshot, signature = self._capture_signature()
            revision = self._revision
        else:
            snapshot = self._pending_snapshot
            signature = self._pending_signature or self._snapshot_signature(snapshot)
            revision = self._pending_revision or self._revision
        self._pending_snapshot = None
        self._pending_signature = None
        self._pending_revision = None
        self.coordinator._discovery_snapshot_pending = False
        if signature == self._last_persisted_signature:
            self._persisted_revision = max(self._persisted_revision, revision)
            return
        self._save_in_progress = True
        try:
            await self._store.async_save(snapshot)
        except asyncio.CancelledError:
            if self._pending_snapshot is None:
                self._pending_snapshot = snapshot
                self._pending_signature = signature
                self._pending_revision = revision
            self.coordinator._discovery_snapshot_pending = True
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.debug(
                "Failed to save discovery snapshot for site %s",
                redact_site_id(self.coordinator.site_id),
                exc_info=True,
            )
            if self._pending_snapshot is None:
                self._pending_snapshot = snapshot
                self._pending_signature = signature
                self._pending_revision = revision
                self.coordinator._discovery_snapshot_pending = True
            return
        finally:
            self._save_in_progress = False
        self._last_persisted_signature = signature
        self._persisted_revision = max(self._persisted_revision, revision)
        if self._pending_snapshot is not None:
            self.coordinator._discovery_snapshot_pending = True
            self._schedule_pending_save()

    def schedule_save(self) -> None:
        changed = self._observe_revision()
        if not changed and self._pending_snapshot is None:
            self.coordinator._discovery_snapshot_pending = False
            return
        if changed or self._pending_snapshot is None:
            snapshot, signature = self._capture_signature()
            if signature == self._last_persisted_signature:
                self._persisted_revision = self._revision
                self._pending_snapshot = None
                self._pending_signature = None
                self._pending_revision = None
                self.coordinator._discovery_snapshot_pending = False
                return
            self._pending_snapshot = snapshot
            self._pending_signature = signature
            self._pending_revision = self._revision
        self.coordinator._discovery_snapshot_pending = True
        self._schedule_pending_save()

    def _schedule_pending_save(self) -> None:
        if (
            self._save_in_progress
            or self.coordinator._discovery_snapshot_save_cancel is not None
        ):
            return

        @callback  # type: ignore[untyped-decorator]
        def _run(_now: datetime) -> None:
            self.coordinator._discovery_snapshot_save_cancel = None
            if not self.coordinator._discovery_snapshot_pending:
                return
            task = self.coordinator.hass.async_create_task(
                self.async_save(), name=f"{DOMAIN}_discovery_snapshot_save"
            )
            self._save_task = task
            task.add_done_callback(self._clear_save_task)

        self.coordinator._discovery_snapshot_save_cancel = async_call_later(
            self.coordinator.hass, DISCOVERY_SNAPSHOT_SAVE_DELAY_S, _run
        )

    def _clear_save_task(self, task: asyncio.Task[None]) -> None:
        """Clear a completed snapshot save task."""

        if self._save_task is task:
            self._save_task = None

    def cancel_pending_save(self) -> asyncio.Task[None] | None:
        """Cancel delayed and in-flight snapshot persistence."""

        if self.coordinator._discovery_snapshot_save_cancel is not None:
            self.coordinator._discovery_snapshot_save_cancel()
            self.coordinator._discovery_snapshot_save_cancel = None
        task = self._save_task
        if task is not None and not task.done():
            task.cancel()
            return task
        self._save_task = None
        return None

    def sync_site_energy_discovery_state(self) -> None:
        energy = getattr(self.coordinator, "energy", None)
        if energy is None:
            return
        if getattr(energy, "_site_energy_cache_ts", None) is not None:
            self.coordinator._site_energy_discovery_ready = True
