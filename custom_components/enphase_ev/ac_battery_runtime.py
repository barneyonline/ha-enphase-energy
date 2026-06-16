"""Parse and refresh Enphase AC Battery device state."""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone as _tz
from html import unescape
from zoneinfo import ZoneInfo

from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .service_validation import raise_translated_service_validation

# Enphase exposes AC Battery controls through an HTML page rather than the JSON
# endpoints used for most battery data.
_AC_BATTERY_TABLE_RE = re.compile(
    r"<table[^>]*id=['\"]ac_batteries['\"][^>]*>(?P<table>.*?)</table>",
    re.IGNORECASE | re.DOTALL,
)
_AC_BATTERY_ROW_RE = re.compile(
    r"<tr\b[^>]*>(?P<row>.*?)</tr>",
    re.IGNORECASE | re.DOTALL,
)
_AC_BATTERY_CELL_RE = re.compile(
    r"<t[dh]\b[^>]*>(?P<cell>.*?)</t[dh]>",
    re.IGNORECASE | re.DOTALL,
)
_AC_BATTERY_LINK_RE = re.compile(
    r"/systems/(?P<site_id>[^/]+)/ac_batteries/(?P<battery_id>[^\"'>/?#]+)",
    re.IGNORECASE,
)
_AC_BATTERY_SLEEP_CONTROL_RE = re.compile(
    r"class=['\"][^'\"]*\b(?P<class>sleep|cancel|wake)\b[^'\"]*['\"][^>]*>(?P<label>.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
_AC_BATTERY_SELECTED_SOC_RE = re.compile(
    r"<option\b(?=[^>]*selected)(?=[^>]*value=['\"](?P<value>\d+)['\"])[^>]*>(?P<label>.*?)</option>",
    re.IGNORECASE | re.DOTALL,
)
_AC_BATTERY_TIMESTAMP_RE = re.compile(
    r"(?P<month>\d{1,2})/(?P<day>\d{1,2})/(?P<year>\d{4})\s+"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>AM|PM)\s*(?P<tz>[A-Z]{2,5})?",
    re.IGNORECASE,
)


class AcBatteryRuntime:
    """Dedicated AC Battery parsing and control helper."""

    def __init__(self, battery_runtime) -> None:
        self._battery_runtime = battery_runtime

    @property
    def coordinator(self):
        return self._battery_runtime.coordinator

    @property
    def battery_state(self):
        return self._battery_runtime.battery_state

    def _ac_battery_text(self, value: object) -> str | None:
        if value is None:
            return None
        try:
            text = str(value)
        except Exception:  # noqa: BLE001
            return None
        text = re.sub(r"<[^>]+>", " ", text)
        text = unescape(text)
        text = " ".join(text.split()).strip()
        return text or None

    def _ac_battery_sleep_state(self, value: object) -> str | None:
        text = self._battery_runtime._coerce_optional_text(value)
        if text is None:
            return None
        normalized = text.strip().lower()
        if normalized == "sleep":
            return "off"
        if normalized == "cancel":
            return "pending"
        if normalized == "wake":
            return "on"
        return None

    def _ac_battery_parse_timestamp(self, value: object) -> datetime | None:
        text = self._ac_battery_text(value)
        if not text:
            return None
        match = _AC_BATTERY_TIMESTAMP_RE.search(text)
        if not match:
            return None
        try:
            naive = datetime.strptime(
                (
                    f"{match.group('month')}/{match.group('day')}/{match.group('year')} "
                    f"{match.group('hour')}:{match.group('minute')} {match.group('ampm').upper()}"
                ),
                "%m/%d/%Y %I:%M %p",
            )
        except Exception:  # noqa: BLE001
            return None
        tz_name = (
            self._battery_runtime._coerce_optional_text(
                getattr(self.battery_state, "_battery_timezone", None)
            )
            or "UTC"
        )
        try:
            return naive.replace(tzinfo=ZoneInfo(tz_name)).astimezone(_tz.utc)
        except Exception:  # noqa: BLE001
            return naive.replace(tzinfo=_tz.utc)

    def _ac_battery_parse_float(self, value: object) -> float | None:
        text = self._ac_battery_text(value)
        if text is None:
            return None
        cleaned = re.sub(r"[^0-9.+-]", "", text)
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except Exception:  # noqa: BLE001
            return None

    def _ac_battery_parse_int(self, value: object) -> int | None:
        parsed = self._ac_battery_parse_float(value)
        if parsed is None:
            return None
        try:
            return int(round(parsed))
        except Exception:  # noqa: BLE001
            return None

    def _ac_battery_key(
        self,
        *,
        serial: object = None,
        battery_id: object = None,
    ) -> str | None:
        serial_text = self._battery_runtime._coerce_optional_text(serial)
        if serial_text:
            return serial_text
        battery_id_text = self._battery_runtime._normalize_battery_id(battery_id)
        if battery_id_text:
            return f"id_{battery_id_text}"
        return None

    def _clear_ac_battery_telemetry_state(self) -> None:
        state = self.battery_state
        rows = dict(getattr(state, "_ac_battery_data", {}) or {})
        for key, snapshot in rows.items():
            if not isinstance(snapshot, dict):
                continue
            trimmed = dict(snapshot)
            for field in ("power_w", "operating_mode", "last_reported"):
                trimmed.pop(field, None)
            rows[key] = trimmed
        state._ac_battery_data = rows
        state._ac_battery_power_w = None
        state._ac_battery_summary_sample_utc = None
        details = dict(getattr(state, "_ac_battery_aggregate_status_details", {}) or {})
        details["power_w"] = None
        details["power_map_w"] = {}
        details["reporting_count"] = 0
        details["latest_reported_utc"] = None
        state._ac_battery_aggregate_status_details = details
        state._ac_battery_telemetry_cache_until = None
        state._ac_battery_telemetry_payloads = None

    def _clear_ac_battery_events_state(self) -> None:
        state = self.battery_state
        state._ac_battery_events_payloads = None

    def _clear_ac_battery_device_state(self, *, refresh_topology: bool) -> None:
        state = self.battery_state
        state._ac_battery_data = {}
        state._ac_battery_order = []
        state._ac_battery_aggregate_status = None
        state._ac_battery_aggregate_status_details = {}
        state._ac_battery_sleep_state = None
        state._ac_battery_devices_cache_until = None
        state._ac_battery_devices_payload = None
        state._ac_battery_devices_html_payload = None
        self._clear_ac_battery_telemetry_state()
        self._clear_ac_battery_events_state()
        if refresh_topology:
            self._battery_runtime._refresh_cached_topology()

    def _preserve_ac_battery_status_fallback(self) -> bool:
        # Battery status refresh runs before the dedicated AC Battery devices page.
        # If that page is empty or unparsable, keep the known battery-status rows
        # instead of erasing AC Battery entities before Home Assistant sees them.
        state = self.battery_state
        details = getattr(state, "_ac_battery_aggregate_status_details", None)
        if not isinstance(details, dict):
            return False
        if details.get("status_source") != "battery_status":
            return False
        rows = getattr(state, "_ac_battery_data", None)
        if not isinstance(rows, dict):
            return False
        valid_rows = {
            key: snapshot
            for key, snapshot in rows.items()
            if isinstance(snapshot, dict)
        }
        if not valid_rows:
            return False
        order = [
            key
            for key in getattr(state, "_ac_battery_order", []) or []
            if key in valid_rows
        ]
        order.extend(key for key in valid_rows if key not in order)
        state._ac_battery_data = valid_rows
        state._ac_battery_order = list(dict.fromkeys(order))
        self.refresh_ac_battery_summary()
        self._battery_runtime._refresh_cached_topology()
        return True

    def parse_ac_battery_devices_page(self, html_text: object) -> None:
        state = self.battery_state
        page_text = self._battery_runtime._coerce_optional_text(html_text)
        if not page_text:
            if self._preserve_ac_battery_status_fallback():
                return
            state._ac_battery_data = {}
            state._ac_battery_order = []
            state._ac_battery_aggregate_status = None
            state._ac_battery_aggregate_status_details = {}
            state._ac_battery_sleep_state = None
            state._ac_battery_selected_sleep_min_soc = None
            self._battery_runtime._refresh_cached_topology()
            return

        table_match = _AC_BATTERY_TABLE_RE.search(page_text)
        table_text = table_match.group("table") if table_match else page_text
        rows: dict[str, dict[str, object]] = {}
        order: list[str] = []
        sleep_states: dict[str, str] = {}
        raw_sleep_states: dict[str, str | None] = {}
        selected_sleep_min_soc: int | None = None
        worst_key: str | None = None
        worst_status: str | None = None
        worst_severity = self._battery_runtime._battery_status_severity_value("normal")

        for row_match in _AC_BATTERY_ROW_RE.finditer(table_text):
            row_text = row_match.group("row")
            cells = [
                match.group("cell")
                for match in _AC_BATTERY_CELL_RE.finditer(row_text)
                if match.group("cell")
            ]
            if len(cells) < 6:
                continue
            link_match = _AC_BATTERY_LINK_RE.search(cells[0])
            serial_number = self._ac_battery_text(cells[0])
            battery_id = (
                self._battery_runtime._normalize_battery_id(
                    link_match.group("battery_id")
                )
                if link_match
                else None
            )
            key = self._ac_battery_key(serial=serial_number, battery_id=battery_id)
            if not key:
                continue

            # The sleep link CSS class is the stable signal; the label can vary
            # with localization or minor Enlighten page copy changes.
            sleep_match = _AC_BATTERY_SLEEP_CONTROL_RE.search(row_text)
            sleep_class = (
                sleep_match.group("class").strip().lower() if sleep_match else None
            )
            sleep_label = self._ac_battery_text(
                sleep_match.group("label") if sleep_match else None
            )
            sleep_state = self._ac_battery_sleep_state(sleep_class)
            status_text = self._ac_battery_text(cells[5])
            status_normalized = self._battery_runtime._normalize_battery_status_text(
                status_text
            )
            if status_normalized is None:
                status_normalized = "unknown"
            severity = self._battery_runtime._battery_status_severity_value(
                status_normalized
            )

            row: dict[str, object] = {
                "serial_number": serial_number or key,
                "battery_id": battery_id,
                "part_number": self._ac_battery_text(cells[1]),
                "phase": self._ac_battery_text(cells[2]),
                "current_charge_pct": self._battery_runtime._parse_percent_value(
                    cells[3]
                ),
                "cycle_count": self._ac_battery_parse_int(cells[4]),
                "status_text": status_text,
                "status_normalized": status_normalized,
                "sleep_control_class": sleep_class,
                "sleep_control_label": sleep_label,
                "sleep_state": sleep_state,
            }
            selected_match = _AC_BATTERY_SELECTED_SOC_RE.search(row_text)
            if selected_match:
                selected_value = self._ac_battery_parse_int(
                    selected_match.group("value")
                )
                row["sleep_min_soc"] = selected_value
                row["sleep_min_soc_label"] = self._ac_battery_text(
                    selected_match.group("label")
                )
                if selected_value is not None:
                    selected_sleep_min_soc = selected_value

            rows[key] = row
            order.append(key)
            if sleep_state is not None:
                sleep_states[key] = sleep_state
            raw_sleep_states[key] = sleep_class

            if severity > worst_severity or worst_status is None:
                worst_severity = severity
                worst_status = status_normalized
                worst_key = key

        if not rows and self._preserve_ac_battery_status_fallback():
            return

        aggregate_sleep_state = None
        if sleep_states:
            if any(state_text == "pending" for state_text in sleep_states.values()):
                aggregate_sleep_state = "pending"
            elif all(state_text == "on" for state_text in sleep_states.values()):
                aggregate_sleep_state = "on"
            elif all(state_text == "off" for state_text in sleep_states.values()):
                aggregate_sleep_state = "off"
            else:
                aggregate_sleep_state = "mixed"

        state._ac_battery_data = rows
        state._ac_battery_order = list(dict.fromkeys(order))
        state._ac_battery_selected_sleep_min_soc = (
            selected_sleep_min_soc
            if selected_sleep_min_soc is not None
            else getattr(state, "_ac_battery_selected_sleep_min_soc", None)
        )
        state._ac_battery_sleep_state = aggregate_sleep_state
        state._ac_battery_control_pending = aggregate_sleep_state == "pending"
        state._ac_battery_aggregate_status = worst_status or (
            "unknown" if rows else None
        )
        state._ac_battery_aggregate_status_details = {
            "battery_count": len(rows),
            "sleep_state": aggregate_sleep_state,
            "selected_sleep_min_soc": getattr(
                state, "_ac_battery_selected_sleep_min_soc", None
            ),
            "sleep_state_map": dict(sleep_states),
            "sleep_state_raw": dict(raw_sleep_states),
            "worst_storage_key": worst_key,
            "worst_status": worst_status,
            "battery_ids": {
                key: snapshot.get("battery_id") for key, snapshot in rows.items()
            },
        }
        self._battery_runtime._refresh_cached_topology()

    def parse_ac_battery_show_stat_data(
        self,
        serial: str,
        battery_id: str | None,
        html_text: object,
    ) -> dict[str, object]:
        snapshot = dict(
            getattr(self.battery_state, "_ac_battery_data", {}).get(serial, {}) or {}
        )
        if battery_id and snapshot.get("battery_id") is None:
            snapshot["battery_id"] = battery_id

        text = self._battery_runtime._coerce_optional_text(html_text) or ""
        if not text:
            return snapshot

        power_match = re.search(
            r"formatted-value['\"]>(?P<value>.*?)</span>\s*<span[^>]*class=['\"]units['\"]>(?P<units>.*?)</span>",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if power_match:
            power_value = self._ac_battery_parse_float(power_match.group("value"))
            units = self._ac_battery_text(power_match.group("units"))
            if power_value is not None:
                if units and units.lower() == "kw":
                    power_value *= 1000.0
                snapshot["power_w"] = round(power_value, 3)

        mode_match = re.search(
            r"<span>\s*\((?P<mode>[^<]+)\)\s*</span>",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if mode_match:
            snapshot["operating_mode"] = self._ac_battery_text(mode_match.group("mode"))

        soc_match = re.search(
            r"State of Charge\s*<span[^>]*class=['\"]value['\"]>(?P<value>.*?)</span>",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if soc_match:
            snapshot["current_charge_pct"] = self._battery_runtime._parse_percent_value(
                soc_match.group("value")
            )
        cycle_match = re.search(
            r"Charge Cycles\s*<span[^>]*class=['\"]value['\"]>(?P<value>.*?)</span>",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if cycle_match:
            snapshot["cycle_count"] = self._ac_battery_parse_int(
                cycle_match.group("value")
            )

        parsed_timestamp = self._ac_battery_parse_timestamp(text)
        if parsed_timestamp is not None:
            snapshot["last_reported"] = parsed_timestamp
        return snapshot

    def refresh_ac_battery_summary(self) -> None:
        state = self.battery_state
        rows = getattr(state, "_ac_battery_data", {}) or {}
        ordered_keys = [
            key for key in getattr(state, "_ac_battery_order", []) or [] if key in rows
        ]
        if not ordered_keys:
            state._ac_battery_power_w = None
            state._ac_battery_summary_sample_utc = None
            return
        power_map: dict[str, float] = {}
        latest_reported: datetime | None = None
        for key in ordered_keys:
            snapshot = rows.get(key)
            if not isinstance(snapshot, dict):
                continue
            power_value = snapshot.get("power_w")
            if isinstance(power_value, (int, float)):
                power_map[key] = float(power_value)
            last_reported = snapshot.get("last_reported")
            if isinstance(last_reported, datetime) and (
                latest_reported is None or last_reported > latest_reported
            ):
                latest_reported = last_reported

        state._ac_battery_power_w = (
            round(sum(power_map.values()), 3) if power_map else None
        )
        details = dict(getattr(state, "_ac_battery_aggregate_status_details", {}) or {})
        details["power_w"] = state._ac_battery_power_w
        details["power_map_w"] = power_map
        details["reporting_count"] = len(power_map)
        details["latest_reported_utc"] = (
            latest_reported.isoformat() if latest_reported is not None else None
        )
        state._ac_battery_aggregate_status_details = details
        state._ac_battery_summary_sample_utc = dt_util.utcnow()

    async def async_refresh_ac_battery_devices(self, *, force: bool = False) -> None:
        coord = self.coordinator
        state = self.battery_state
        if not coord.inventory_view.has_type_for_entities("ac_battery"):
            self._clear_ac_battery_device_state(refresh_topology=True)
            return
        family = "ac_battery_devices"
        if not force and state._ac_battery_devices_cache_until:
            now = time.monotonic()
            if now < state._ac_battery_devices_cache_until:
                return
        now = time.monotonic()
        fetcher = getattr(coord.client, "ac_battery_devices_page", None)
        if not callable(fetcher):
            self._clear_ac_battery_device_state(refresh_topology=True)
            return
        if not coord._endpoint_family_should_run(family, force=force):
            if not coord._endpoint_family_can_use_stale(family):
                self._clear_ac_battery_device_state(refresh_topology=True)
            return
        try:
            payload = await fetcher()
        except Exception as err:  # noqa: BLE001
            coord._note_endpoint_family_failure(family, err)
            if not coord._endpoint_family_can_use_stale(family):
                self._clear_ac_battery_device_state(refresh_topology=True)
            return
        self.parse_ac_battery_devices_page(payload)
        redacted = coord.redact_battery_payload(
            {
                "battery_count": len(getattr(state, "_ac_battery_data", {}) or {}),
                "records": [
                    dict(snapshot)
                    for key in getattr(state, "_ac_battery_order", []) or []
                    for snapshot in [getattr(state, "_ac_battery_data", {}).get(key)]
                    if isinstance(snapshot, dict)
                ],
                "sleep_state": getattr(state, "_ac_battery_sleep_state", None),
                "selected_sleep_min_soc": getattr(
                    state, "_ac_battery_selected_sleep_min_soc", None
                ),
            }
        )
        if isinstance(redacted, dict):
            # These payload copies feed diagnostics, so identifiers must stay
            # redacted even though the source page was HTML.
            state._ac_battery_devices_payload = redacted
            state._ac_battery_devices_html_payload = redacted
        else:
            state._ac_battery_devices_payload = {"value": redacted}
            state._ac_battery_devices_html_payload = {"value": redacted}
        # The AC Battery page is slow-changing and expensive enough to keep on
        # its own short-lived cache separate from endpoint-family backoff.
        state._ac_battery_devices_cache_until = now + 300.0
        coord._note_endpoint_family_success(family, success_ttl_s=300.0)

    def ac_battery_devices_refresh_due(self, *, force: bool = False) -> bool:
        """Return True when AC Battery inventory should be refreshed."""

        coord = self.coordinator
        state = self.battery_state
        has_cached_state = bool(
            getattr(state, "_ac_battery_data", None)
            or getattr(state, "_ac_battery_order", None)
            or getattr(state, "_ac_battery_aggregate_status", None) is not None
            or getattr(state, "_ac_battery_aggregate_status_details", None)
            or getattr(state, "_ac_battery_sleep_state", None) is not None
            or getattr(state, "_ac_battery_devices_payload", None) is not None
            or getattr(state, "_ac_battery_devices_html_payload", None) is not None
            or getattr(state, "_ac_battery_telemetry_payloads", None) is not None
            or getattr(state, "_ac_battery_events_payloads", None) is not None
            or getattr(state, "_ac_battery_power_w", None) is not None
        )
        if not coord.inventory_view.has_type_for_entities("ac_battery"):
            return has_cached_state
        if not force and state._ac_battery_devices_cache_until:
            now = time.monotonic()
            if now < state._ac_battery_devices_cache_until:
                return False
        family = "ac_battery_devices"
        fetcher = getattr(coord.client, "ac_battery_devices_page", None)
        if not callable(fetcher):
            return has_cached_state
        if coord._endpoint_family_should_run(family, force=force):
            return True
        return has_cached_state and not coord._endpoint_family_can_use_stale(family)

    async def async_refresh_ac_battery_telemetry(self, *, force: bool = False) -> None:
        coord = self.coordinator
        state = self.battery_state
        if not coord.inventory_view.has_type_for_entities("ac_battery"):
            self._clear_ac_battery_telemetry_state()
            return
        family = "ac_battery_telemetry"
        if not force and state._ac_battery_telemetry_cache_until:
            now = time.monotonic()
            if now < state._ac_battery_telemetry_cache_until:
                return
        now = time.monotonic()
        fetcher = getattr(coord.client, "ac_battery_show_stat_data", None)
        if not callable(fetcher):
            self._clear_ac_battery_telemetry_state()
            return
        if not coord._endpoint_family_should_run(family, force=force):
            if not coord._endpoint_family_can_use_stale(family):
                self._clear_ac_battery_telemetry_state()
            return
        rows = dict(getattr(state, "_ac_battery_data", {}) or {})
        payloads: list[dict[str, object]] = []
        try:
            for serial, snapshot in rows.items():
                if not isinstance(snapshot, dict):
                    continue
                battery_id = self._battery_runtime._normalize_battery_id(
                    snapshot.get("battery_id")
                )
                if not battery_id:
                    continue
                payload = await fetcher(battery_id)
                merged = self.parse_ac_battery_show_stat_data(
                    serial, battery_id, payload
                )
                rows[serial] = merged
                payloads.append(
                    {"serial": serial, "battery_id": battery_id, "payload": merged}
                )
        except Exception as err:  # noqa: BLE001
            coord._note_endpoint_family_failure(family, err)
            if not coord._endpoint_family_can_use_stale(family):
                self._clear_ac_battery_telemetry_state()
            return
        state._ac_battery_data = rows
        redacted_payloads = coord.redact_battery_payload(payloads)
        if isinstance(redacted_payloads, list):
            state._ac_battery_telemetry_payloads = {"records": redacted_payloads}
        else:
            state._ac_battery_telemetry_payloads = {"value": redacted_payloads}
        state._ac_battery_telemetry_cache_until = now + 300.0
        self.refresh_ac_battery_summary()
        coord._note_endpoint_family_success(family, success_ttl_s=300.0)

    async def async_refresh_ac_battery_events(self, *, force: bool = False) -> None:
        coord = self.coordinator
        state = self.battery_state
        if not coord.inventory_view.has_type_for_entities("ac_battery"):
            self._clear_ac_battery_events_state()
            return
        family = "ac_battery_events"
        fetcher = getattr(coord.client, "ac_battery_events_page", None)
        if not callable(fetcher):
            self._clear_ac_battery_events_state()
            return
        if not coord._endpoint_family_should_run(family, force=force):
            if not coord._endpoint_family_can_use_stale(family):
                self._clear_ac_battery_events_state()
            return
        rows = dict(getattr(state, "_ac_battery_data", {}) or {})
        payloads: list[dict[str, object]] = []
        try:
            for serial, snapshot in rows.items():
                if not isinstance(snapshot, dict):
                    continue
                battery_id = self._battery_runtime._normalize_battery_id(
                    snapshot.get("battery_id")
                )
                if not battery_id:
                    continue
                payload = await fetcher(battery_id)
                payloads.append(
                    {
                        "serial": serial,
                        "battery_id": battery_id,
                        "location": f"/systems/{coord.site_id}/ac_batteries/{battery_id}/events",
                        "html_excerpt": (
                            self._battery_runtime._coerce_optional_text(payload)[:512]
                            if self._battery_runtime._coerce_optional_text(payload)
                            else None
                        ),
                    }
                )
        except Exception as err:  # noqa: BLE001
            coord._note_endpoint_family_failure(family, err)
            if not coord._endpoint_family_can_use_stale(family):
                self._clear_ac_battery_events_state()
            return
        redacted_payloads = coord.redact_battery_payload(payloads)
        if isinstance(redacted_payloads, list):
            state._ac_battery_events_payloads = {"records": redacted_payloads}
        else:
            state._ac_battery_events_payloads = {"value": redacted_payloads}
        coord._note_endpoint_family_success(family, success_ttl_s=900.0)

    async def async_set_ac_battery_sleep_mode(self, enabled: bool) -> None:
        coord = self.coordinator
        state = self.battery_state
        rows = dict(getattr(state, "_ac_battery_data", {}) or {})
        if not rows:
            raise_translated_service_validation(
                translation_domain=DOMAIN,
                translation_key="ac_battery_unavailable",
                message="No AC Battery devices are currently available.",
            )
        target_soc = None
        if enabled:
            target_soc = getattr(state, "_ac_battery_selected_sleep_min_soc", None)
            if target_soc is None:
                target_soc = 20
        results: list[dict[str, object]] = []
        for serial, snapshot in rows.items():
            if not isinstance(snapshot, dict):
                continue
            battery_id = self._battery_runtime._normalize_battery_id(
                snapshot.get("battery_id")
            )
            if not battery_id:
                continue
            if enabled:
                response = await coord.client.set_ac_battery_sleep(
                    battery_id, target_soc
                )
            else:
                response = await coord.client.set_ac_battery_wake(battery_id)
            results.append(
                {
                    "serial": serial,
                    "battery_id": battery_id,
                    "status": response.status,
                    "location": response.location,
                }
            )
        state._ac_battery_last_command = {
            "action": "sleep" if enabled else "wake",
            "requested_at_utc": dt_util.utcnow().isoformat(),
            "sleep_min_soc": target_soc if enabled else None,
            "results": results,
        }
        state._ac_battery_control_pending = enabled
        await self.async_refresh_ac_battery_devices(force=True)
        await self.async_refresh_ac_battery_telemetry(force=True)

    async def async_set_ac_battery_target_soc(self, value: int) -> None:
        state = self.battery_state
        state._ac_battery_selected_sleep_min_soc = int(value)
        sleep_state = self._battery_runtime._coerce_optional_text(
            getattr(state, "_ac_battery_sleep_state", None)
        )
        if sleep_state in {"on", "pending"}:
            await self.async_set_ac_battery_sleep_mode(True)
