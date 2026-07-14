"""Build redacted Home Assistant diagnostics for Enphase config entries/devices."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import CONF_EMAIL, CONF_SITE_ID, DOMAIN
from .device_types import parse_type_identifier
from .energy import SiteEnergyFlow
from .log_redaction import redact_text
from .runtime_data import EnphaseConfigEntry, get_runtime_data

DIAGNOSTIC_CAPTURE_ERRORS = (RuntimeError, TypeError, ValueError, AttributeError)

TO_REDACT = [
    "e_auth_token",
    "access_token",
    "cookie",
    "session_id",
    "auth_identifier",
    "auth_token",
    "session_auth_identifier",
    "session_auth_token",
    "enlighten_manager_token_production",
    "enlm-token",
    "authorization",
    "password",
    CONF_EMAIL,
]

DIAGNOSTIC_IDENTIFIER_KEYS = [
    # Enphase diagnostics often include nested raw payloads. Redact broad key
    # families so future payload additions are safe by default.
    "site",
    "site_id",
    "site_name",
    "siteId",
    "siteName",
    "serial",
    "serials",
    "serial_number",
    "serialNumber",
    "serialNum",
    "sn",
    "part_num",
    "partNum",
    "profile_id",
    "grid_profile_id",
    "name",
    "device_name",
    "hostname",
    "host",
    "ip",
    "ip_address",
    "ip-address",
    "mac",
    "mac_address",
    "uid",
    "device_uid",
    "device-uid",
    "iqer_uid",
    "iqer-uid",
    "hems_device_id",
    "hems-device-id",
    "hems_device_facet_id",
    "hems-device-facet-id",
    "latest_reported_device",
    "device_uid",
    "device-uid",
    "parent_uid",
    "parent-uid",
    "imei",
    "imsi",
    "iccid",
    "sim_id",
    "sim-id",
    "apn",
    "apn_name",
    "apn-name",
    "remote_ip",
    "remote-ip",
    "remote_host",
    "remote-host",
    "tunnel_host",
    "tunnel-host",
    "tunnel_endpoint",
    "tunnel-endpoint",
    "device_link",
    "battery_id",
    "battery_ids",
    "data-battery-id",
    "data_battery_id",
    "data-site-id",
    "data_site_id",
    "href",
    "url",
    "location",
    "interface_ip",
    "ip_addr",
    "gateway_ip_addr",
    "default_route",
    "mac_addr",
]

DIAGNOSTICS_REDACT_KEYS = [*TO_REDACT, *DIAGNOSTIC_IDENTIFIER_KEYS]


def _normalize_site_ids(values: list[Any]) -> tuple[str, ...]:
    """Return normalized site IDs to redact from embedded diagnostics strings."""

    site_ids: list[str] = []
    for value in values:
        if value is None:
            continue
        try:
            text = str(value).strip()
        except Exception:  # noqa: BLE001
            continue
        if text and text not in site_ids:
            site_ids.append(text)
    return tuple(site_ids)


def _value_matches_site_id(value: Any, site_ids: tuple[str, ...]) -> bool:
    """Return true when a scalar diagnostics value exactly matches a site ID."""

    if not site_ids or isinstance(value, bool):
        return False
    try:
        text = str(value).strip()
    except Exception:  # noqa: BLE001
        return False
    if text in site_ids:
        return True
    if isinstance(value, float) and value.is_integer():
        return str(int(value)) in site_ids
    return False


def _redact_embedded_diagnostics_text(value: Any, site_ids: tuple[str, ...]) -> Any:
    """Redact site IDs embedded in otherwise non-sensitive diagnostics strings."""

    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, child_value in value.items():
            if isinstance(key, str):
                safe_key = redact_text(key, site_ids=site_ids, max_length=512)
            elif _value_matches_site_id(key, site_ids):
                safe_key = "[site]"
            else:
                safe_key = key
            redacted[safe_key] = _redact_embedded_diagnostics_text(
                child_value, site_ids
            )
        return redacted
    if isinstance(value, list):
        return [_redact_embedded_diagnostics_text(item, site_ids) for item in value]
    if isinstance(value, tuple):
        return tuple(
            _redact_embedded_diagnostics_text(item, site_ids) for item in value
        )
    if isinstance(value, str):
        return redact_text(value, site_ids=site_ids, max_length=4096)
    if _value_matches_site_id(value, site_ids):
        return "[site]"
    return value


def _redact_diagnostics_payload(
    payload: dict[str, Any],
    *,
    site_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Return a redacted diagnostics payload safe for external sharing."""

    redacted = async_redact_data(payload, DIAGNOSTICS_REDACT_KEYS)
    return cast(dict[str, Any], _redact_embedded_diagnostics_text(redacted, site_ids))


def _text(value: Any) -> str | None:
    if value is None:
        return None
    try:
        out = str(value).strip()
    except Exception:  # noqa: BLE001
        return None
    return out or None


def _optional_bool(value: Any) -> bool | None:
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


def _normalize_gateway_status(value: Any) -> str:
    text = _text(value)
    if not text:
        return "unknown"
    normalized = text.lower().replace("-", "_").replace(" ", "_")
    if any(token in normalized for token in ("fault", "error", "critical")):
        return "error"
    if "warn" in normalized:
        return "warning"
    if any(
        token in normalized for token in ("not_reporting", "offline", "disconnected")
    ):
        return "not_reporting"
    if any(token in normalized for token in ("normal", "online", "connected", "ok")):
        return "normal"
    return "unknown"


def _gateway_summary(devices: list[dict[str, Any]], count: int) -> dict[str, Any]:
    total = max(count, len(devices))
    connected = 0
    disconnected = 0
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

    for member in devices:
        property_keys.update(str(key) for key in member.keys())
        status_raw = member.get("statusText")
        if status_raw is None:
            status_raw = member.get("status")
        status = _normalize_gateway_status(status_raw)
        status_counts[status] = status_counts.get(status, 0) + 1

        connected_raw = _optional_bool(member.get("connected"))
        if connected_raw is None:
            if status == "normal":
                connected_raw = True
            elif status == "not_reporting":
                connected_raw = False
        if connected_raw is True:
            connected += 1
        elif connected_raw is False:
            disconnected += 1

        model = _text(member.get("model")) or _text(member.get("channel_type"))
        if model:
            model_counts[model] = model_counts.get(model, 0) + 1

        firmware = _text(member.get("envoy_sw_version")) or _text(
            member.get("sw_version")
        )
        if firmware:
            firmware_counts[firmware] = firmware_counts.get(firmware, 0) + 1

    unknown_connection = max(0, total - connected - disconnected)
    if total <= 0:
        connectivity = None
    elif connected >= total:
        connectivity = "online"
    elif connected == 0 and disconnected > 0:
        connectivity = "offline"
    elif connected > 0:
        connectivity = "degraded"
    else:
        connectivity = "unknown"

    return {
        "connectivity": connectivity,
        "connected_devices": connected,
        "disconnected_devices": disconnected,
        "unknown_connection_devices": unknown_connection,
        "status_counts": status_counts,
        "model_counts": model_counts,
        "firmware_counts": firmware_counts,
        "property_keys": sorted(property_keys),
    }


def _microinverter_summary(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}

    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    try:
        total = int(payload.get("count", 0) or 0)
    except (TypeError, ValueError):
        total = 0
    status_counts_raw = payload.get("status_counts")
    if isinstance(status_counts_raw, dict):
        status_counts = {
            key: _safe_int(status_counts_raw.get(key, 0) or 0, 0)
            for key in (
                "total",
                "normal",
                "warning",
                "error",
                "not_reporting",
                "unknown",
            )
        }
    else:
        status_counts = {
            "total": total,
            "normal": 0,
            "warning": 0,
            "error": 0,
            "not_reporting": 0,
            "unknown": total,
        }
    total = max(total, int(status_counts.get("total", 0) or 0))
    not_reporting = max(0, int(status_counts.get("not_reporting", 0) or 0))
    unknown = max(0, int(status_counts.get("unknown", 0) or 0))
    if (
        total > 0
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
        unknown = total
    if unknown + not_reporting > total:
        unknown = max(0, total - not_reporting)
    reporting = max(0, total - not_reporting - unknown)
    if total <= 0:
        connectivity = None
    elif reporting >= total:
        connectivity = "online"
    elif reporting == 0 and unknown > 0:
        connectivity = "unknown"
    elif reporting > 0:
        connectivity = "degraded"
    else:
        connectivity = "offline"
    return {
        "connectivity": connectivity,
        "total_inverters": total,
        "reporting_inverters": reporting,
        "not_reporting_inverters": not_reporting,
        "unknown_inverters": unknown,
        "status_counts": status_counts,
        "status_summary": payload.get("status_summary"),
        "model_summary": payload.get("model_summary"),
        "firmware_summary": payload.get("firmware_summary"),
        "array_summary": payload.get("array_summary"),
        "status_type_counts": payload.get("status_type_counts"),
        "panel_info": payload.get("panel_info"),
        "latest_reported_utc": payload.get("latest_reported_utc"),
        "latest_reported_device": payload.get("latest_reported_device"),
        "production_start_date": payload.get("production_start_date"),
        "production_end_date": payload.get("production_end_date"),
    }


def _battery_status_summary_for_diagnostics(summary: Any) -> dict[str, Any]:
    """Return a battery-status summary without identifier-keyed maps."""

    if not isinstance(summary, dict):
        return {}
    safe_keys = (
        "aggregate_charge_pct",
        "aggregate_status",
        "aggregate_charge_source",
        "included_count",
        "contributing_count",
        "excluded_count",
        "available_energy_kwh",
        "max_capacity_kwh",
        "site_current_charge_pct",
        "site_available_energy_kwh",
        "site_max_capacity_kwh",
        "site_available_power_kw",
        "site_max_power_kw",
        "site_total_micros",
        "site_active_micros",
        "site_inactive_micros",
        "site_included_count",
        "site_excluded_count",
        "worst_status",
    )
    return {
        key: summary.get(key)
        for key in safe_keys
        if summary.get(key) not in (None, {}, [])
    }


def _ac_battery_status_summary_for_diagnostics(summary: Any) -> dict[str, Any]:
    """Return an AC battery summary without identifier-keyed maps."""

    if not isinstance(summary, dict):
        return {}
    safe_keys = (
        "aggregate_status",
        "battery_count",
        "sleep_state",
        "selected_sleep_min_soc",
        "worst_status",
        "power_w",
        "latest_reported_utc",
        "latest_reported_key",
    )
    return {
        key: summary.get(key)
        for key in safe_keys
        if summary.get(key) not in (None, {}, [])
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EnphaseConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""

    site_ids = _normalize_site_ids([entry.data.get(CONF_SITE_ID)])
    data = async_redact_data(dict(entry.data), TO_REDACT)
    options = dict(getattr(entry, "options", {}) or {})

    diag: dict[str, Any] = {
        "entry_data": data,
        "entry_options": options,
    }

    try:
        runtime_data = get_runtime_data(entry)
        coord = runtime_data.coordinator
    except RuntimeError:
        return _redact_diagnostics_payload(diag, site_ids=site_ids)

    try:
        upd = (
            int(coord.update_interval.total_seconds())
            if coord.update_interval
            else None
        )
    except DIAGNOSTIC_CAPTURE_ERRORS:
        upd = None

    try:
        metrics: dict[str, Any] = coord.collect_site_metrics()
    except DIAGNOSTIC_CAPTURE_ERRORS:
        metrics = {}
    site_ids = _normalize_site_ids(
        [
            *site_ids,
            getattr(coord, "site_id", None),
            metrics.get("site_id") if isinstance(metrics, dict) else None,
        ]
    )

    try:
        last_modes = coord.charge_mode_cache_snapshot()
    except DIAGNOSTIC_CAPTURE_ERRORS:
        last_modes = {}

    client = getattr(coord, "client", None)
    base_header_names: list[str] = []
    if client is not None:
        header_names = getattr(client, "base_header_names", None)
        if callable(header_names):
            try:
                base_header_names = header_names()
            except DIAGNOSTIC_CAPTURE_ERRORS:
                base_header_names = []
    has_scheduler_bearer = False
    if client is not None and hasattr(client, "has_scheduler_bearer"):
        try:
            has_scheduler_bearer = bool(client.has_scheduler_bearer())
        except DIAGNOSTIC_CAPTURE_ERRORS:
            has_scheduler_bearer = False

    try:
        session_history = coord.session_history_diagnostics()
    except DIAGNOSTIC_CAPTURE_ERRORS:
        session_history = {}

    try:
        battery_config = coord.battery_diagnostics_payloads()
    except DIAGNOSTIC_CAPTURE_ERRORS:
        battery_config = {}

    try:
        evse = coord.evse_diagnostics_payloads()
    except DIAGNOSTIC_CAPTURE_ERRORS:
        evse = {}

    try:
        inverters = coord.inverter_diagnostics_payloads()
    except DIAGNOSTIC_CAPTURE_ERRORS:
        inverters = {}

    try:
        payload_health = coord.payload_health_diagnostics()
    except DIAGNOSTIC_CAPTURE_ERRORS:
        payload_health = {}

    try:
        system_dashboard = coord.system_dashboard_diagnostics()
    except DIAGNOSTIC_CAPTURE_ERRORS:
        system_dashboard = {}

    try:
        heatpump_runtime = coord.heatpump_runtime_diagnostics()
    except DIAGNOSTIC_CAPTURE_ERRORS:
        heatpump_runtime = {}

    current_power: dict[str, object] = {}
    current_power_runtime = getattr(coord, "current_power_runtime", None)
    current_power_diagnostics = getattr(current_power_runtime, "diagnostics", None)
    if callable(current_power_diagnostics):
        try:
            current_power = current_power_diagnostics()
        except DIAGNOSTIC_CAPTURE_ERRORS:
            current_power = {}

    try:
        scheduler = coord.scheduler_diagnostics()
    except DIAGNOSTIC_CAPTURE_ERRORS:
        scheduler = {}

    tariff: dict[str, Any] = {}
    tariff_runtime = getattr(coord, "tariff_runtime", None)
    tariff_diagnostics = getattr(tariff_runtime, "diagnostics", None)
    if callable(tariff_diagnostics):
        try:
            tariff = tariff_diagnostics()
        except DIAGNOSTIC_CAPTURE_ERRORS:
            tariff = {}

    system_events: dict[str, Any] = {}
    system_events_runtime = getattr(coord, "system_events_runtime", None)
    system_events_diagnostics = getattr(system_events_runtime, "diagnostics", None)
    if callable(system_events_diagnostics):
        try:
            system_events = system_events_diagnostics()
        except DIAGNOSTIC_CAPTURE_ERRORS:
            system_events = {}

    system_event_history: dict[str, Any] = {}
    system_event_history_diagnostics = getattr(
        system_events_runtime,
        "history_diagnostics",
        None,
    )
    if callable(system_event_history_diagnostics):
        try:
            system_event_history = system_event_history_diagnostics()
        except DIAGNOSTIC_CAPTURE_ERRORS:
            system_event_history = {}

    grid_profile: dict[str, Any] = {}
    grid_profile_runtime = getattr(coord, "grid_profile_runtime", None)
    grid_profile_diagnostics = getattr(grid_profile_runtime, "diagnostics", None)
    if callable(grid_profile_diagnostics):
        try:
            grid_profile = grid_profile_diagnostics()
        except DIAGNOSTIC_CAPTURE_ERRORS:
            grid_profile = {}

    firmware_catalog: dict[str, Any] = {}
    firmware_catalog_manager = getattr(coord, "firmware_catalog_manager", None)
    status_snapshot = getattr(firmware_catalog_manager, "status_snapshot", None)
    if callable(status_snapshot):
        try:
            snapshot = status_snapshot()
        except DIAGNOSTIC_CAPTURE_ERRORS:
            snapshot = {}
        if isinstance(snapshot, dict):
            firmware_catalog = snapshot

    diag["coordinator"] = {
        "site_id": metrics.get("site_id", coord.site_id),
        "site_metrics": metrics or None,
        "serials_count": len(getattr(coord, "serials", []) or []),
        "update_interval_seconds": upd,
        "last_scheduler_modes": last_modes,
        "network_errors": metrics.get("network_errors", 0),
        "backoff_until_monotonic": metrics.get("backoff_until_monotonic"),
        "last_error": metrics.get("last_error"),
        "headers_info": {
            "base_header_names": base_header_names,
            "has_scheduler_bearer": has_scheduler_bearer,
        },
        "phase_timings": metrics.get("phase_timings", coord.phase_timings),
        "session_history": session_history,
        "battery_config": battery_config,
        "evse": evse,
        "inverters": inverters,
        "payload_health": payload_health,
        "system_dashboard": system_dashboard,
        "system_events": system_events,
        "system_event_history": system_event_history,
        "heatpump_runtime": heatpump_runtime,
        "current_power": current_power,
        "scheduler": scheduler,
        "tariff": tariff,
        "grid_profile": grid_profile,
        "firmware_catalog": firmware_catalog or None,
    }

    weather_coordinator = runtime_data.weather_coordinator
    if weather_coordinator is None:
        diag["coordinator"]["weather"] = {
            "role": "child_coordinator",
            "discovery_state": "disabled",
        }
    else:
        try:
            diag["coordinator"]["weather"] = weather_coordinator.diagnostics()
        except DIAGNOSTIC_CAPTURE_ERRORS:
            diag["coordinator"]["weather"] = None

    schedule_sync = getattr(coord, "schedule_sync", None)
    if schedule_sync is not None and hasattr(schedule_sync, "diagnostics"):
        try:
            diag["coordinator"]["schedule_sync"] = schedule_sync.diagnostics()
        except DIAGNOSTIC_CAPTURE_ERRORS:
            diag["coordinator"]["schedule_sync"] = None

    site_energy: dict[str, Any] = {}
    energy = getattr(coord, "energy", None)
    try:
        flows = (
            getattr(energy, "site_energy", None)
            if energy is not None
            else getattr(coord, "site_energy", None)
        ) or {}
        for key, flow in flows.items():
            if flow is None:
                continue
            raw = flow
            if isinstance(flow, SiteEnergyFlow):
                raw = {
                    "value_kwh": flow.value_kwh,
                    "bucket_count": flow.bucket_count,
                    "fields_used": flow.fields_used,
                    "start_date": flow.start_date,
                    "last_report_date": flow.last_report_date,
                    "update_pending": flow.update_pending,
                    "source_unit": flow.source_unit,
                    "last_reset_at": flow.last_reset_at,
                    "interval_minutes": flow.interval_minutes,
                }
            elif hasattr(flow, "__dict__"):
                raw = flow.__dict__
            if not isinstance(raw, dict):
                continue
            entry_data = dict(raw)
            lr = entry_data.get("last_report_date")
            if isinstance(lr, datetime):
                entry_data["last_report_date"] = lr.isoformat()
            site_energy[str(key)] = entry_data
    except DIAGNOSTIC_CAPTURE_ERRORS:
        site_energy = {}

    cache_age = getattr(energy, "site_energy_cache_age", None) if energy else None
    meta = getattr(energy, "site_energy_meta", None) if energy else None
    if isinstance(meta, dict):
        meta_copy = dict(meta)
        lr_meta = meta_copy.get("last_report_date")
        if isinstance(lr_meta, datetime):
            meta_copy["last_report_date"] = lr_meta.isoformat()
        meta = meta_copy
    if site_energy or meta:
        diag["site_energy"] = {
            "flows": site_energy or None,
            "meta": meta,
            "cache_age_s": cache_age,
        }

    return _redact_diagnostics_payload(diag, site_ids=site_ids)


async def async_get_device_diagnostics(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    device: dr.DeviceEntry,
) -> dict[str, Any]:
    """Return diagnostics for a device."""
    site_ids = _normalize_site_ids([entry.data.get(CONF_SITE_ID)])
    dev_reg = dr.async_get(hass)
    dev = dev_reg.async_get(device.id)
    if not dev:
        return {"error": "device_not_found"}
    sn = None
    type_key = None
    type_site_id = None
    site_device_id = None
    for domain, ident in dev.identifiers:
        if domain != DOMAIN:
            continue
        ident_text = str(ident)
        if ident_text.startswith("site:"):
            site_device_id = ident_text.removeprefix("site:").strip() or None
            site_ids = _normalize_site_ids([*site_ids, site_device_id])
            continue
        if ident_text.startswith("type:"):
            parsed = parse_type_identifier(ident_text)
            if parsed:
                type_site_id, type_key = parsed
                site_ids = _normalize_site_ids([*site_ids, type_site_id])
            continue
        sn = ident_text
        break
    if type_key:
        coord = None
        try:
            coord = get_runtime_data(entry).coordinator
        except RuntimeError:
            pass
        bucket = (
            coord.inventory_view.type_bucket(type_key) if coord is not None else None
        )
        payload = {
            "site_id": type_site_id,
            "type_key": type_key,
            "type_label": (
                bucket.get("type_label")
                if isinstance(bucket, dict)
                else (
                    coord.inventory_view.type_label(type_key)
                    if coord is not None
                    else None
                )
            ),
            "count": (bucket.get("count", 0) if isinstance(bucket, dict) else 0),
            "devices": (bucket.get("devices", []) if isinstance(bucket, dict) else []),
        }
        # Type diagnostics summarize groups of devices, while serial diagnostics
        # below expose one charger snapshot.
        if isinstance(bucket, dict):
            for key, value in bucket.items():
                if key in payload or key == "devices":
                    continue
                if isinstance(value, dict):
                    payload[key] = dict(value)
                elif isinstance(value, list):
                    payload[key] = list(value)
                else:
                    payload[key] = value
        if type_key == "envoy":
            devices = payload.get("devices")
            if isinstance(devices, list):
                safe_devices = [item for item in devices if isinstance(item, dict)]
            else:
                safe_devices = []
            try:
                gateway_count = int(cast(Any, payload.get("count", 0) or 0))
            except (TypeError, ValueError):
                gateway_count = 0
            payload["gateway_summary"] = _gateway_summary(safe_devices, gateway_count)
        elif type_key == "microinverter":
            payload["microinverter_summary"] = _microinverter_summary(payload)
        if type_key in ("envoy", "encharge") and coord is not None:
            system_dashboard_payload = {}
            helper = getattr(coord, "system_dashboard_diagnostics", None)
            if callable(helper):
                try:
                    dashboard = helper()
                except DIAGNOSTIC_CAPTURE_ERRORS:
                    dashboard = {}
                if isinstance(dashboard, dict):
                    summaries = dashboard.get("type_summaries")
                    if isinstance(summaries, dict):
                        raw = summaries.get(type_key)
                        if isinstance(raw, dict):
                            if type_key == "envoy":
                                for key in (
                                    "modem",
                                    "network",
                                    "tunnel",
                                    "controller",
                                    "meters",
                                    "hierarchy",
                                ):
                                    value = raw.get(key)
                                    if value not in (None, {}, []):
                                        system_dashboard_payload[key] = value
                            else:
                                for key in (
                                    "connectivity",
                                    "software",
                                    "operation_mode",
                                    "hierarchy",
                                ):
                                    value = raw.get(key)
                                    if value not in (None, {}, []):
                                        system_dashboard_payload[key] = value
            if system_dashboard_payload:
                payload["system_dashboard_details"] = system_dashboard_payload
        if type_key == "encharge" and coord is not None:
            battery_status_payload = getattr(coord, "battery_status_payload", None)
            if not isinstance(battery_status_payload, dict):
                battery_status_payload = getattr(coord, "_battery_status_payload", None)
            if isinstance(battery_status_payload, dict) and battery_status_payload:
                payload["battery_status_payload"] = battery_status_payload

            battery_status_summary = _battery_status_summary_for_diagnostics(
                getattr(coord, "battery_status_summary", None)
            )
            if battery_status_summary:
                payload["battery_status_summary"] = battery_status_summary
        if type_key == "ac_battery" and coord is not None:
            ac_battery_devices_payload = getattr(
                coord, "_ac_battery_devices_payload", None
            )
            if (
                isinstance(ac_battery_devices_payload, dict)
                and ac_battery_devices_payload
            ):
                payload["ac_battery_devices_payload"] = ac_battery_devices_payload
            ac_battery_telemetry_payloads = getattr(
                coord, "_ac_battery_telemetry_payloads", None
            )
            if (
                isinstance(ac_battery_telemetry_payloads, dict)
                and ac_battery_telemetry_payloads
            ):
                payload["ac_battery_telemetry_payloads"] = ac_battery_telemetry_payloads
            ac_battery_events_payloads = getattr(
                coord, "_ac_battery_events_payloads", None
            )
            if (
                isinstance(ac_battery_events_payloads, dict)
                and ac_battery_events_payloads
            ):
                payload["ac_battery_events_payloads"] = ac_battery_events_payloads
            ac_battery_status_summary = _ac_battery_status_summary_for_diagnostics(
                getattr(coord, "ac_battery_status_summary", None)
            )
            if ac_battery_status_summary:
                payload["ac_battery_status_summary"] = ac_battery_status_summary
        if type_key == "heatpump" and coord is not None:
            helper = getattr(coord, "heatpump_runtime_diagnostics", None)
            if callable(helper):
                try:
                    runtime_payload = helper()
                except DIAGNOSTIC_CAPTURE_ERRORS:
                    runtime_payload = {}
                if isinstance(runtime_payload, dict) and runtime_payload:
                    payload["heatpump_runtime"] = runtime_payload
        return _redact_diagnostics_payload(payload, site_ids=site_ids)
    if not sn:
        if site_device_id:
            coord = None
            try:
                coord = get_runtime_data(entry).coordinator
            except RuntimeError:
                pass
            metrics: dict[str, Any] = {}
            metrics_available = False
            if coord is not None:
                raw_metrics: object = None
                try:
                    raw_metrics = coord.collect_site_metrics()
                except DIAGNOSTIC_CAPTURE_ERRORS:
                    pass
                if isinstance(raw_metrics, dict):
                    metrics = raw_metrics
                    metrics_available = True
                site_ids = _normalize_site_ids(
                    [
                        *site_ids,
                        getattr(coord, "site_id", None),
                        metrics.get("site_id") if metrics else None,
                    ]
                )
            return _redact_diagnostics_payload(
                {
                    "device_kind": "site",
                    "site_id": site_device_id,
                    "metrics_available": metrics_available,
                    "site_metrics": metrics or None,
                    "serials_count": (
                        len(getattr(coord, "serials", []) or [])
                        if coord is not None
                        else None
                    ),
                    "phase_timings": (
                        getattr(coord, "phase_timings", None)
                        if coord is not None
                        else None
                    ),
                },
                site_ids=site_ids,
            )
        return {"error": "serial_not_resolved"}
    coord = None
    try:
        coord = get_runtime_data(entry).coordinator
    except RuntimeError:
        pass
    snapshot = (coord.data or {}).get(sn) if coord else None
    return _redact_diagnostics_payload(
        {"serial": sn, "snapshot": snapshot or {}},
        site_ids=site_ids,
    )
