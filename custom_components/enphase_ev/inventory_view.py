"""Expose inventory-derived type visibility and Home Assistant device metadata."""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable

from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC
from homeassistant.helpers.entity import DeviceInfo

from .device_info_helpers import _cloud_device_info
from .device_info_helpers import _is_redundant_model_id
from .device_types import (
    normalize_type_key,
    parse_type_identifier,
    type_display_label,
    type_identifier,
)
from .inventory_runtime import InventoryRuntime
from .parsing_helpers import type_member_text

if TYPE_CHECKING:  # pragma: no cover
    from .coordinator import EnphaseCoordinator


class InventoryView:
    """Inventory-derived entity gating and device metadata helpers."""

    def __init__(self, coordinator: EnphaseCoordinator) -> None:
        self.coordinator = coordinator

    @property
    def site_id(self) -> str:
        return self.coordinator.site_id

    @property
    def inventory_runtime(self):
        return self.coordinator.inventory_runtime

    @property
    def heatpump_runtime(self):
        return self.coordinator.heatpump_runtime

    def _has_known_chargers(self) -> bool:
        has_chargers = bool(getattr(self.coordinator, "data", None))
        if not has_chargers:
            serials = getattr(self.coordinator, "serials", None)
            if isinstance(serials, (set, list, tuple)):
                has_chargers = bool(serials)
        if not has_chargers:
            iter_serials = getattr(self.coordinator, "iter_serials", None)
            if callable(iter_serials):
                try:
                    has_chargers = bool(list(iter_serials()))
                except Exception:
                    has_chargers = False
        return has_chargers

    def iter_type_keys(self) -> list[str]:
        type_order = getattr(self.coordinator, "_type_device_order", None)
        if isinstance(type_order, list) and type_order:
            return [key for key in type_order if self._type_is_selected(key)]
        buckets = getattr(self.coordinator, "_type_device_buckets", None)
        if isinstance(buckets, dict) and buckets:
            return [key for key in buckets if self._type_is_selected(key)]
        selected = getattr(self.coordinator, "_selected_type_keys", None)
        if isinstance(selected, (set, list, tuple)) and selected:
            return [key for key in selected if self._type_is_selected(key)]
        inferred: list[str] = []
        if getattr(self.coordinator, "site_id", None):
            inferred.append("envoy")
        if self._has_known_chargers():
            inferred.append("iqevse")
        if getattr(self.coordinator, "_battery_has_encharge", None) is True:
            inferred.append("encharge")
        if getattr(self.coordinator, "_battery_has_acb", None) is True:
            inferred.append("ac_battery")
        return [key for key in inferred if self._type_is_selected(key)]

    def _type_is_selected(self, type_key: object) -> bool:
        normalized = normalize_type_key(type_key)
        if not normalized:
            return False
        selected = getattr(self.coordinator, "_selected_type_keys", None)
        if selected is None or not selected:
            return True
        return normalized in selected

    def has_type(self, type_key: object) -> bool:  # pragma: no cover
        normalized = normalize_type_key(type_key)
        if not normalized:
            return False
        buckets = getattr(self.coordinator, "_type_device_buckets", None)
        if not isinstance(buckets, dict):
            return False
        bucket = buckets.get(normalized)
        if not isinstance(bucket, dict):
            return False
        try:
            return int(bucket.get("count", 0)) > 0
        except Exception:
            return False

    def has_type_for_entities(self, type_key: object) -> bool:  # pragma: no cover
        """Return whether a type should gate entity creation/availability."""

        normalized = normalize_type_key(type_key)
        if not normalized:
            return False
        if not self._type_is_selected(normalized):
            return False
        if not getattr(self.coordinator, "_devices_inventory_ready", False):
            # During startup, keep selected entities available until inventory
            # proves the type absent.
            return True
        if self.has_type(normalized):
            return True
        if normalized == "heatpump":
            runtime = getattr(self.coordinator, "heatpump_runtime", None)
            if bool(getattr(self.coordinator, "_heatpump_known_present", False)):
                return True
            helper = getattr(runtime, "heatpump_entities_established", None)
            if callable(helper):
                try:
                    return bool(helper())
                except Exception:  # noqa: BLE001
                    return False
        if normalized == "encharge":
            return getattr(self.coordinator, "_battery_has_encharge", None) is True
        if normalized == "ac_battery":
            return getattr(self.coordinator, "_battery_has_acb", None) is True
        if normalized == "iqevse":
            return self._has_known_chargers()
        return False

    def type_bucket(
        self, type_key: object
    ) -> dict[str, object] | None:  # pragma: no cover
        normalized = normalize_type_key(type_key)
        if not normalized:
            return None
        buckets = getattr(self.coordinator, "_type_device_buckets", None)
        if not isinstance(buckets, dict):
            return None
        bucket = buckets.get(normalized)
        if not isinstance(bucket, dict):
            return None
        members = bucket.get("devices")
        if isinstance(members, list):
            members_out = [dict(item) for item in members if isinstance(item, dict)]
        else:
            members_out = []
        out = {
            "type_key": normalized,
            "type_label": bucket.get("type_label") or type_display_label(normalized),
            "count": bucket.get("count", len(members_out)),
            "devices": members_out,
        }
        for key, value in bucket.items():
            if key in out or key == "devices":
                continue
            if isinstance(value, dict):
                out[key] = dict(value)
            elif isinstance(value, list):
                out[key] = list(value)
            else:
                out[key] = value
        return out

    def type_label(self, type_key: object) -> str | None:  # pragma: no cover
        normalized = normalize_type_key(type_key)
        if not normalized:
            return None
        buckets = getattr(self.coordinator, "_type_device_buckets", None)
        bucket = buckets.get(normalized) if isinstance(buckets, dict) else None
        if isinstance(bucket, dict):
            label = bucket.get("type_label")
            if isinstance(label, str) and label.strip():
                return label
        return type_display_label(normalized)

    def type_identifier(
        self, type_key: object
    ) -> tuple[str, str] | None:  # pragma: no cover
        normalized = normalize_type_key(type_key)
        if not normalized:
            return None
        if not self._type_is_selected(normalized):
            return None
        if self.has_type(normalized):
            return type_identifier(self.site_id, normalized)
        if normalized not in {"encharge", "ac_battery"}:
            return None
        # Battery controls can be discovered through BatteryConfig before the
        # generic devices inventory exposes a concrete member bucket.
        if not self.has_type_for_entities(normalized):
            return None
        return type_identifier(self.site_id, normalized)

    def _type_bucket_members(self, type_key: object) -> list[dict[str, object]]:
        bucket = self.type_bucket(type_key)
        if not isinstance(bucket, dict):
            return []
        members = bucket.get("devices")
        if not isinstance(members, list):
            return []
        return [dict(item) for item in members if isinstance(item, dict)]

    @staticmethod
    def _type_member_text(member: dict[str, object] | None, *keys: str) -> str | None:
        return type_member_text(member, *keys)

    def _type_summary_from_values(self, values: Iterable[object]) -> str | None:
        counts: dict[str, int] = {}
        for value in values:
            if value is None:
                continue
            try:
                text = str(value).strip()
            except Exception:
                continue
            if not text:
                continue
            counts[text] = counts.get(text, 0) + 1
        return InventoryRuntime._format_inverter_model_summary(counts)

    def _type_member_summary(
        self,
        members: Iterable[dict[str, object]],
        *keys: str,
    ) -> str | None:
        values: list[str] = []
        for member in members:
            value = self._type_member_text(member, *keys)
            if value:
                values.append(value)
        return self._type_summary_from_values(values)

    @staticmethod
    def _iq_type_device_name(type_key: str) -> str | None:
        return {
            "envoy": "IQ Gateway",
            "encharge": "IQ Battery",
            "ac_battery": "AC Battery",
            "iqevse": "IQ EV Charger",
            "heatpump": "Heat Pump",
            "microinverter": "IQ Microinverters",
            "generator": "IQ Generator",
        }.get(type_key)

    def _type_member_single_value(
        self, members: Iterable[dict[str, object]], *keys: str
    ) -> str | None:
        values: list[str] = []
        for member in members:
            value = self._type_member_text(member, *keys)
            if value:
                values.append(value)
        if not values:
            return None
        unique_values = list(dict.fromkeys(values))
        if len(unique_values) == 1:
            return unique_values[0]
        return None

    @staticmethod
    def _normalize_mac(value: object) -> str | None:
        if value is None:
            return None
        try:
            text = str(value).strip().lower()
        except Exception:
            return None
        if not text:
            return None

        def _all_hex(chars: str) -> bool:
            return bool(chars) and all(ch in "0123456789abcdef" for ch in chars)

        def _compact_to_colon_hex(compact: str) -> str:
            return ":".join(compact[idx : idx + 2] for idx in range(0, 12, 2))

        if ":" in text or "-" in text:
            parts = [part for part in text.replace("-", ":").split(":") if part]
            if len(parts) != 6:
                return None
            normalized_parts: list[str] = []
            for part in parts:
                if len(part) == 1:
                    part = f"0{part}"
                if len(part) != 2 or not _all_hex(part):
                    return None
                normalized_parts.append(part)
            return ":".join(normalized_parts)

        if "." in text:
            groups = [group for group in text.split(".") if group]
            if len(groups) != 3:
                return None
            if any(len(group) != 4 or not _all_hex(group) for group in groups):
                return None
            return _compact_to_colon_hex("".join(groups))

        if len(text) == 12 and _all_hex(text):
            return _compact_to_colon_hex(text)

        return None

    def _envoy_controller_mac(self) -> str | None:
        controller = self._envoy_system_controller_member()
        if not isinstance(controller, dict):
            return None
        for key in (
            "mac",
            "mac_address",
            "macAddress",
            "eth0_mac",
            "ethernet_mac",
            "wifi_mac",
            "wireless_mac",
        ):
            normalized = self._normalize_mac(controller.get(key))
            if normalized:
                return normalized
        return None

    @staticmethod
    def _envoy_member_kind(member: dict[str, object]) -> str | None:
        channel_type = InventoryView._type_member_text(
            member,
            "channel_type",
            "channelType",
            "meter_type",
        )
        if channel_type:
            normalized = "".join(
                ch if ch.isalnum() else "_" for ch in channel_type.lower()
            )
            if (
                normalized in ("enpower", "system_controller", "systemcontroller")
                or "enpower" in normalized
                or "system_controller" in normalized
                or normalized.startswith("systemcontroller")
            ):
                return "controller"
            if "production" in normalized or normalized in ("prod", "pv", "solar"):
                return "production"
            if "consumption" in normalized or normalized in (
                "cons",
                "load",
                "site_load",
            ):
                return "consumption"
        name = (InventoryView._type_member_text(member, "name") or "").lower()
        if "system controller" in name:
            return "controller"
        if "controller" in name and "meter" not in name:
            return "controller"
        if "production" in name:
            return "production"
        if "consumption" in name:
            return "consumption"
        return None

    def _envoy_system_controller_member(self) -> dict[str, object] | None:
        for member in self._type_bucket_members("envoy"):
            if self._envoy_member_kind(member) == "controller":
                return member
        return None

    def _envoy_member_looks_like_gateway(self, member: dict[str, object]) -> bool:
        if self._envoy_member_kind(member) in (
            "production",
            "consumption",
            "controller",
        ):
            return False
        if any(
            member.get(key) is not None
            for key in (
                "envoy_sw_version",
                "ap_mode",
                "supportsEntrez",
                "show_connection_details",
                "ip",
                "ip_address",
            )
        ):
            return True
        name = (self._type_member_text(member, "name") or "").lower()
        return "gateway" in name

    def _envoy_primary_gateway_member(self) -> dict[str, object] | None:
        for member in self._type_bucket_members("envoy"):
            if self._envoy_member_looks_like_gateway(member):
                return member
        return None

    def _envoy_preferred_member(self) -> dict[str, object] | None:
        gateway = self._envoy_primary_gateway_member()
        if gateway is not None:
            return gateway
        return self._envoy_system_controller_member()

    def _heatpump_primary_member(self) -> dict[str, object] | None:
        return self.heatpump_runtime._heatpump_primary_member()

    def type_device_name(self, type_key: object) -> str | None:  # pragma: no cover
        normalized = normalize_type_key(type_key)
        if not normalized:
            return None
        canonical_iq_name = self._iq_type_device_name(normalized)
        if canonical_iq_name:
            return canonical_iq_name
        bucket = self.type_bucket(normalized)
        if not bucket:
            return None
        label = bucket.get("type_label")
        if not isinstance(label, str) or not label.strip():
            return None
        return label.strip()

    def type_device_model(self, type_key: object) -> str | None:  # pragma: no cover
        normalized = normalize_type_key(type_key)
        if not normalized:
            return None
        if normalized == "envoy":
            member = self._envoy_preferred_member()
            controller_name = self._type_member_text(
                member,
                "name",
                "model",
                "channel_type",
                "sku_id",
                "model_id",
            )
            if controller_name:
                return controller_name
            return self.type_device_name(normalized) or self.type_label(normalized)
        if normalized == "heatpump":
            primary_member = self._heatpump_primary_member()
            primary_model = self._type_member_text(
                primary_member,
                "model",
                "sku_id",
                "model_id",
                "part_num",
                "part_number",
                "hardware_sku",
                "name",
            )
            if primary_model:
                return primary_model
            members = self._type_bucket_members(normalized)
            summary_model = self._type_member_summary(
                members,
                "model",
                "sku_id",
                "model_id",
                "part_num",
                "part_number",
                "hardware_sku",
                "name",
            )
            if summary_model:
                return summary_model
            return self.type_device_name(normalized) or self.type_label(normalized)
        members = self._type_bucket_members(normalized)
        model = self._type_member_single_value(
            members,
            "model",
            "sku_id",
            "model_id",
            "part_num",
            "part_number",
            "channel_type",
            "name",
        )
        if model:
            return model
        return self.type_device_name(normalized) or self.type_label(normalized)

    def type_device_serial_number(
        self, type_key: object
    ) -> str | None:  # pragma: no cover
        normalized = normalize_type_key(type_key)
        if not normalized:
            return None
        if normalized == "envoy":
            serial_keys = ("serial_number", "serial", "serialNumber", "device_sn")
            member = self._envoy_preferred_member()
            return self._type_member_text(member, *serial_keys)
        if normalized == "heatpump":
            primary = self._heatpump_primary_member()
            serial = self._type_member_text(
                primary,
                "serial_number",
                "serial",
                "serialNumber",
                "device_sn",
                "uid",
                "device_uid",
            )
            if serial:
                return serial
            return self._type_member_single_value(
                self._type_bucket_members(normalized),
                "serial_number",
                "serial",
                "serialNumber",
                "device_sn",
                "uid",
                "device_uid",
            )
        if normalized in ("encharge", "microinverter", "iqevse", "generator"):
            return self._type_member_single_value(
                self._type_bucket_members(normalized),
                "serial_number",
                "serial",
                "serialNumber",
                "device_sn",
            )
        return None

    def type_device_model_id(self, type_key: object) -> str | None:  # pragma: no cover
        normalized = normalize_type_key(type_key)
        if not normalized:
            return None
        model_id_keys = (
            "sku_id",
            "model_id",
            "sku",
            "modelId",
            "part_num",
            "part_number",
        )
        if normalized == "envoy":
            member = self._envoy_preferred_member()
            model_id = self._type_member_text(member, *model_id_keys)
        elif normalized == "heatpump":
            primary = self._heatpump_primary_member()
            model_id = self._type_member_text(
                primary,
                *model_id_keys,
                "hardware_sku",
            )
            if not model_id:
                model_id = self._type_member_single_value(
                    self._type_bucket_members(normalized),
                    *model_id_keys,
                    "hardware_sku",
                )
            if not model_id:
                model_id = self._type_member_summary(
                    self._type_bucket_members(normalized),
                    *model_id_keys,
                    "hardware_sku",
                )
        elif normalized in ("encharge", "microinverter", "iqevse", "generator"):
            model_id = self._type_member_single_value(
                self._type_bucket_members(normalized),
                *model_id_keys,
            )
        else:
            return None
        if _is_redundant_model_id(self.type_device_model(type_key), model_id):
            return None
        return model_id

    def type_device_sw_version(
        self, type_key: object
    ) -> str | None:  # pragma: no cover
        normalized = normalize_type_key(type_key)
        if not normalized:
            return None
        sw_keys = (
            "envoy_sw_version",
            "sw_version",
            "firmware_version",
            "firmwareVersion",
            "firmware-version",
            "software_version",
            "softwareVersion",
            "software-version",
            "system_version",
            "application_version",
            "applicationVersion",
            "application-version",
        )
        if normalized == "envoy":
            member = self._envoy_preferred_member()
            sw_version = self._type_member_text(member, *sw_keys)
            if sw_version:
                return sw_version
            other_member = (
                self._envoy_system_controller_member()
                if member is self._envoy_primary_gateway_member()
                else self._envoy_primary_gateway_member()
            )
            return self._type_member_text(other_member, *sw_keys)
        if normalized == "heatpump":
            primary = self._heatpump_primary_member()
            sw_version = self._type_member_text(primary, *sw_keys)
            if sw_version:
                return sw_version
            sw_version = self._type_member_single_value(
                self._type_bucket_members(normalized), *sw_keys
            )
            return sw_version
        if normalized in ("encharge", "iqevse", "generator"):
            return self._type_member_single_value(
                self._type_bucket_members(normalized), *sw_keys
            )
        if normalized == "microinverter":
            return self._type_member_single_value(
                self._type_bucket_members(normalized),
                "fw1",
                "fw2",
                *sw_keys,
            )
        return None

    def type_device_sw_version_summary(
        self, type_key: object
    ) -> str | None:  # pragma: no cover
        normalized = normalize_type_key(type_key)
        if not normalized:
            return None
        sw_version = self.type_device_sw_version(normalized)
        if sw_version:
            return sw_version
        sw_keys = (
            "envoy_sw_version",
            "sw_version",
            "firmware_version",
            "firmwareVersion",
            "firmware-version",
            "software_version",
            "softwareVersion",
            "software-version",
            "system_version",
            "application_version",
            "applicationVersion",
            "application-version",
        )
        if normalized in ("encharge", "generator"):
            return self._type_member_summary(
                self._type_bucket_members(normalized), *sw_keys
            )
        if normalized == "heatpump":
            return self._type_member_summary(
                self._type_bucket_members(normalized), *sw_keys
            )
        if normalized == "microinverter":
            return self._type_member_summary(
                self._type_bucket_members(normalized),
                "fw1",
                "fw2",
                *sw_keys,
            )
        return None

    def type_device_hw_version(
        self, type_key: object
    ) -> str | None:  # pragma: no cover
        normalized = normalize_type_key(type_key)
        if not normalized:
            return None
        if normalized == "envoy":
            member = self._envoy_preferred_member()
            return self._type_member_text(
                member,
                "hw_version",
                "hardware_version",
                "hardwareVersion",
            )
        if normalized == "heatpump":
            primary = self._heatpump_primary_member()
            hw_version = self._type_member_text(
                primary,
                "hw_version",
                "hardware_version",
                "hardwareVersion",
                "hardware_sku",
                "part_num",
                "part_number",
                "sku_id",
            )
            if hw_version:
                return hw_version
            hw_version = self._type_member_single_value(
                self._type_bucket_members(normalized),
                "hw_version",
                "hardware_version",
                "hardwareVersion",
                "hardware_sku",
                "part_num",
                "part_number",
                "sku_id",
            )
            if hw_version:
                return hw_version
            return self._type_member_summary(
                self._type_bucket_members(normalized),
                "hw_version",
                "hardware_version",
                "hardwareVersion",
                "hardware_sku",
                "part_num",
                "part_number",
                "sku_id",
            )
        if normalized in ("microinverter", "encharge", "iqevse", "generator"):
            return self._type_member_single_value(
                self._type_bucket_members(normalized),
                "hw_version",
                "hardware_version",
                "hardwareVersion",
                "part_num",
                "part_number",
                "sku_id",
            )
        return None

    def type_device_info(self, type_key: object):  # pragma: no cover
        normalized = normalize_type_key(type_key)
        if not normalized:
            return None
        if normalized == "cloud":
            return _cloud_device_info(self.site_id)
        identifier = self.type_identifier(type_key)
        if identifier is None:
            return None
        label = self.type_label(type_key) or "Device"
        name = self.type_device_name(type_key) or label
        model = self.type_device_model(type_key) or label
        info_kwargs: dict[str, object] = {
            "identifiers": {identifier},
            "manufacturer": "Enphase",
            "model": model,
            "name": name,
        }
        serial_number = self.type_device_serial_number(type_key)
        if serial_number:
            info_kwargs["serial_number"] = serial_number
        model_id = self.type_device_model_id(type_key)
        if model_id:
            info_kwargs["model_id"] = model_id
        sw_version = self.type_device_sw_version_summary(type_key)
        if sw_version:
            info_kwargs["sw_version"] = sw_version
        hw_summary = self.type_device_hw_version(type_key)
        if hw_summary:
            info_kwargs["hw_version"] = hw_summary
        if normalized == "envoy":
            controller_mac = self._envoy_controller_mac()
            if controller_mac:
                info_kwargs["connections"] = {(CONNECTION_NETWORK_MAC, controller_mac)}
        return DeviceInfo(**info_kwargs)

    def gateway_iq_energy_router_records(self) -> list[dict[str, object]]:
        return self.coordinator.discovery_snapshot.gateway_iq_energy_router_records()

    def gateway_iq_energy_router_summary_records(self) -> list[dict[str, object]]:
        return self.inventory_runtime.gateway_iq_energy_router_summary_records()

    def gateway_iq_energy_router_record(
        self, router_key: object
    ) -> dict[str, object] | None:
        return self.inventory_runtime.gateway_iq_energy_router_record(router_key)

    @staticmethod
    def parse_type_identifier(
        identifier: object,
    ) -> tuple[str, str] | None:  # pragma: no cover
        return parse_type_identifier(identifier)
