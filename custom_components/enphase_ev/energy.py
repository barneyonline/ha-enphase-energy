from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import timezone as _tz
from typing import Callable, Iterable, Protocol

from homeassistant.const import UnitOfPower
from homeassistant.util import dt as dt_util

from .api import SiteEnergyUnavailable
from .log_redaction import redact_site_id, redact_text

LIFETIME_DROP_JITTER_KWH = 0.02
LIFETIME_RESET_DROP_THRESHOLD_KWH = 0.5
LIFETIME_RESET_FLOOR_KWH = 5.0
LIFETIME_RESET_RATIO = 0.5
LIFETIME_CONFIRM_TOLERANCE_KWH = 0.05
LIFETIME_CONFIRM_COUNT = 2
LIFETIME_CONFIRM_WINDOW_S = 180.0
SITE_ENERGY_CACHE_TTL = 300.0
SITE_ENERGY_DEFAULT_INTERVAL_MIN = 5.0
SITE_ENERGY_FAILURE_BACKOFF_S = 15 * 60
HEMS_LIFETIME_FAILURE_BACKOFF_S = 60 * 60
DEVICE_LIFETIME_CHANNELS: tuple[str, ...] = ("evse", "heatpump", "water_heater")
HEMS_DEVICE_CHANNELS_META_KEY = "_hems_device_channels"


class EnergyClient(Protocol):
    async def lifetime_energy(self) -> dict[str, object] | None: ...


@dataclass(slots=True)
class LifetimeGuardState:
    last: float | None = None
    pending_value: float | None = None
    pending_ts: float | None = None
    pending_count: int = 0


@dataclass(slots=True)
class SiteEnergyFlow:
    """Aggregated site-level energy flow."""

    value_kwh: float | None
    bucket_count: int
    fields_used: list[str]
    start_date: str | None
    last_report_date: datetime | None
    update_pending: bool | None
    source_unit: str = UnitOfPower.WATT
    last_reset_at: str | None = None
    interval_minutes: float | None = None


class EnergyManager:
    def __init__(
        self,
        *,
        client_provider: Callable[[], EnergyClient],
        site_id: str,
        logger: logging.Logger,
        summary_invalidator: Callable[[], None] | None = None,
        hems_auth_skip: Callable[[str], bool] | None = None,
        hems_auth_failure: Callable[[BaseException, str], bool] | None = None,
        hems_auth_success: Callable[[str], None] | None = None,
    ) -> None:
        self._client_provider = client_provider
        self.site_id = site_id
        self._logger = logger
        self._summary_invalidator = summary_invalidator
        self._hems_auth_skip = hems_auth_skip
        self._hems_auth_failure = hems_auth_failure
        self._hems_auth_success = hems_auth_success
        self.site_energy: dict[str, SiteEnergyFlow] = {}
        self._site_energy_meta: dict[str, object] = {}
        self._site_energy_cache_ts: float | None = None
        self._site_energy_cache_ttl: float = SITE_ENERGY_CACHE_TTL
        self._site_energy_guard: dict[str, LifetimeGuardState] = {}
        self._site_energy_last_reset: dict[str, str | None] = {}
        self._site_energy_force_refresh = False
        self._lifetime_guard: dict[str, LifetimeGuardState] = {}
        self._service_available = True
        self._service_failures = 0
        self._service_last_error: str | None = None
        self._service_last_failure_utc: datetime | None = None
        self._service_backoff_until: float | None = None
        self._service_backoff_ends_utc: datetime | None = None
        self._hems_lifetime_supported: bool | None = None
        self._hems_lifetime_backoff_until: float | None = None
        self._hems_lifetime_last_error: str | None = None

    def _site_energy_cache_age(self) -> float | None:
        """Return the age of the cached site energy payload."""
        if self._site_energy_cache_ts is None:
            return None
        return time.monotonic() - self._site_energy_cache_ts

    @property
    def site_energy_cache_age(self) -> float | None:
        """Return the age of cached site energy data."""

        return self._site_energy_cache_age()

    @property
    def site_energy_meta(self) -> dict[str, object]:
        """Return a copy of cached site energy metadata."""

        return dict(self._site_energy_meta)

    def _invalidate_site_energy_cache(self) -> None:
        """Drop the cached site energy payload."""
        self._site_energy_cache_ts = None

    def site_energy_refresh_due(self, *, force: bool = False) -> bool:
        """Return True when the site-energy cache should be refreshed."""

        force_refresh = force or self._site_energy_force_refresh
        if self._service_backoff_active():
            return False
        now_mono = time.monotonic()
        if (
            not force_refresh
            and self._site_energy_cache_ts is not None
            and (now_mono - self._site_energy_cache_ts) < self._site_energy_cache_ttl
        ):
            return False
        client = self._client_provider()
        fetcher = getattr(client, "lifetime_energy", None)
        return callable(fetcher)

    @property
    def service_available(self) -> bool:
        """Return True when site energy service is available."""
        return bool(self._service_available and not self._service_backoff_active())

    @property
    def service_last_error(self) -> str | None:
        return self._service_last_error

    @property
    def service_failures(self) -> int:
        return self._service_failures

    @property
    def service_backoff_active(self) -> bool:
        return self._service_backoff_active()

    @property
    def service_backoff_ends_utc(self) -> datetime | None:
        return self._service_backoff_ends_utc

    @property
    def service_last_failure_utc(self) -> datetime | None:
        return self._service_last_failure_utc

    def _service_backoff_active(self) -> bool:
        return bool(
            self._service_backoff_until
            and time.monotonic() < self._service_backoff_until
        )

    def _mark_service_available(self) -> None:
        if self._service_available:
            return
        self._service_available = True
        self._service_failures = 0
        self._service_last_error = None
        self._service_last_failure_utc = None
        self._service_backoff_until = None
        self._service_backoff_ends_utc = None

    def _note_service_unavailable(self, err: Exception | str | None) -> None:
        reason = redact_text(err, site_ids=(self.site_id,)) if err else ""
        if not reason:
            reason = "Site energy unavailable"
        self._service_available = False
        self._service_failures += 1
        self._service_last_error = reason
        self._service_last_failure_utc = dt_util.utcnow()
        delay = max(
            SITE_ENERGY_FAILURE_BACKOFF_S, SITE_ENERGY_DEFAULT_INTERVAL_MIN * 60
        )
        self._service_backoff_until = time.monotonic() + delay
        try:
            self._service_backoff_ends_utc = dt_util.utcnow() + timedelta(seconds=delay)
        except Exception:
            self._service_backoff_ends_utc = None

    def _parse_site_energy_timestamp(self, value: object) -> datetime | None:
        """Best-effort parsing for last_report_date fields."""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            try:
                iv = int(value)
                if iv > 10**12:
                    iv = iv // 1000
                return datetime.fromtimestamp(iv, tz=_tz.utc)
            except Exception:  # noqa: BLE001
                return None
        if isinstance(value, str):
            cleaned = value.strip()
            if not cleaned:
                return None
            if cleaned.isdigit():
                return self._parse_site_energy_timestamp(int(cleaned))
            parsed_dt = None
            try:
                parsed_dt = dt_util.parse_datetime(cleaned)
            except Exception:  # noqa: BLE001
                parsed_dt = None
            if parsed_dt is None:
                try:
                    parsed_date = dt_util.parse_date(cleaned)
                except Exception:  # noqa: BLE001
                    parsed_date = None
                if parsed_date is not None:
                    parsed_dt = datetime.combine(
                        parsed_date, datetime.min.time(), tzinfo=_tz.utc
                    )
            if parsed_dt is None:
                return None
            if parsed_dt.tzinfo is None:
                parsed_dt = parsed_dt.replace(tzinfo=_tz.utc)
            return parsed_dt.astimezone(_tz.utc)
        return None

    @staticmethod
    def _coerce_energy_value(value: object) -> float | None:
        """Normalize numeric bucket values into floats."""
        if isinstance(value, (int, float)):
            try:
                return float(value)
            except Exception:  # noqa: BLE001
                return None
        if isinstance(value, str):
            s = value.strip()
            if not s:
                return None
            try:
                return float(s)
            except Exception:  # noqa: BLE001
                return None
        return None

    def _site_energy_interval_hours(
        self, payload: dict[str, object] | None
    ) -> tuple[float | None, float | None]:
        """Extract reporting interval (minutes -> hours) from payload; defaults to 5 min."""
        if not isinstance(payload, dict):
            payload = {}
        interval_raw = payload.get("interval_minutes")
        if interval_raw is None and "interval" in payload:
            interval_raw = payload.get("interval")
        minutes = self._coerce_energy_value(interval_raw)
        if minutes is None or minutes <= 0:
            minutes = SITE_ENERGY_DEFAULT_INTERVAL_MIN
        minutes_float = float(minutes)
        hours = minutes_float / 60.0
        return hours, minutes_float

    def _sum_energy_buckets(
        self, values: object, _interval_hours: float | None
    ) -> tuple[float, int]:
        """Return total Wh and bucket count for a field.

        The lifetime_energy endpoint returns bucket values already expressed in Wh.
        We sum them directly; the reporting interval is retained only for metadata.
        """
        total = 0.0
        count = 0
        if not isinstance(values, list):
            return total, count
        for val in values:
            num = self._coerce_energy_value(val)
            if num is None:
                continue
            bucket_wh = num
            if bucket_wh < 0:
                continue
            total += bucket_wh
            count += 1
        return total, count

    def _sum_energy_fields(
        self,
        payload: dict[str, object],
        fields: Iterable[str],
        interval_hours: float | None,
    ) -> tuple[float, int, list[str]]:
        """Aggregate Wh totals across multiple fields."""
        total = 0.0
        max_count = 0
        used: list[str] = []
        for field in fields:
            field_total, field_count = self._sum_energy_buckets(
                payload.get(field), interval_hours
            )
            if field_count <= 0 or field_total <= 0:
                continue
            total += field_total
            max_count = max(max_count, field_count)
            used.append(field)
        return total, max_count, used

    def _diff_energy_fields(
        self,
        payload: dict[str, object],
        minuend: str,
        subtrahend: str,
        interval_hours: float | None,
    ) -> tuple[float, int, list[str]]:
        """Derive a flow by subtracting one field from another."""
        pos_total, pos_count = self._sum_energy_buckets(
            payload.get(minuend), interval_hours
        )
        neg_total, neg_count = self._sum_energy_buckets(
            payload.get(subtrahend), interval_hours
        )
        if pos_total <= 0 or pos_count <= 0:
            return 0.0, 0, []
        if neg_count <= 0:
            return 0.0, 0, []
        if neg_total <= 0:
            bucket_count = max(min(pos_count, neg_count), 1)
            return pos_total, bucket_count, [minuend, subtrahend]
        if pos_total <= neg_total:
            return 0.0, 0, []
        bucket_count = max(min(pos_count, neg_count), 1)
        return pos_total - neg_total, bucket_count, [minuend, subtrahend]

    def _diff_energy_fields_multi(
        self,
        payload: dict[str, object],
        minuend: str,
        subtrahends: Iterable[str],
        interval_hours: float | None,
    ) -> tuple[float, int, list[str]]:
        """Derive a flow by subtracting multiple fields from another."""

        pos_total, pos_count = self._sum_energy_buckets(
            payload.get(minuend), interval_hours
        )
        if pos_total <= 0 or pos_count <= 0:
            return 0.0, 0, []

        neg_total = 0.0
        neg_counts: list[int] = []
        used_fields: list[str] = []
        for field in subtrahends:
            field_total, field_count = self._sum_energy_buckets(
                payload.get(field), interval_hours
            )
            if field_count <= 0:
                continue
            neg_total += field_total
            neg_counts.append(field_count)
            used_fields.append(field)

        if not used_fields:
            return 0.0, 0, []

        derived_total = pos_total - neg_total
        if derived_total <= 0:
            return 0.0, 0, []

        bucket_count = max(min(pos_count, *neg_counts), 1)
        return derived_total, bucket_count, [minuend, *used_fields]

    @staticmethod
    def _prefer_direct_energy_source(
        *,
        direct_total: float,
        direct_count: int,
        direct_fields: list[str],
        fallback_total: float,
        fallback_count: int,
        fallback_fields: list[str],
    ) -> tuple[float, int, list[str]]:
        """Prefer direct channels unless they are zero-only or sparsely populated."""

        if (
            direct_count > 0
            and fallback_count > direct_count
            and fallback_total > direct_total
        ):
            return fallback_total, fallback_count, fallback_fields
        if direct_count > 0 and (
            direct_total > 0 or fallback_count <= 0 or fallback_total <= 0
        ):
            return direct_total, direct_count, direct_fields
        return fallback_total, fallback_count, fallback_fields

    def _apply_site_energy_guard(
        self, flow: str, sample: float | None, prev: float | None
    ) -> tuple[float | None, str | None]:
        """Apply monotonic guard to a site energy flow."""
        state = self._site_energy_guard.setdefault(flow, LifetimeGuardState())
        if state.last is None and prev is not None:
            state.last = prev

        try:
            value = float(sample) if sample is not None else None
        except Exception:  # noqa: BLE001
            value = None

        if value is None or value < 0:
            return (state.last if state.last is not None else prev), None

        value = round(value, 3)
        last = state.last
        if last is None:
            state.last = value
            state.pending_value = None
            state.pending_ts = None
            state.pending_count = 0
            return value, None

        drop = last - value
        if drop < 0:
            state.last = value
            state.pending_value = None
            state.pending_ts = None
            state.pending_count = 0
            return value, None

        if drop <= LIFETIME_DROP_JITTER_KWH:
            state.pending_value = None
            state.pending_ts = None
            state.pending_count = 0
            return last, None

        is_reset_candidate = drop >= LIFETIME_RESET_DROP_THRESHOLD_KWH and (
            value <= LIFETIME_RESET_FLOOR_KWH or value <= (last * LIFETIME_RESET_RATIO)
        )
        if is_reset_candidate:
            now = time.monotonic()
            if (
                state.pending_value is not None
                and abs(value - state.pending_value) <= LIFETIME_CONFIRM_TOLERANCE_KWH
            ):
                state.pending_count += 1
            else:
                state.pending_value = value
                state.pending_ts = now
                state.pending_count = 1
                self._site_energy_force_refresh = True
                self._logger.debug(
                    "Ignoring suspected site energy reset for %s: %.3f -> %.3f",
                    flow,
                    last,
                    value,
                )
            if state.pending_count >= LIFETIME_CONFIRM_COUNT or (
                state.pending_ts is not None
                and (now - state.pending_ts) >= LIFETIME_CONFIRM_WINDOW_S
            ):
                confirm_count = state.pending_count
                state.last = value
                state.pending_value = None
                state.pending_ts = None
                state.pending_count = 0
                reset_at = dt_util.utcnow().isoformat()
                self._logger.debug(
                    "Accepting site energy reset for %s after %d samples: %.3f -> %.3f",
                    flow,
                    confirm_count,
                    last,
                    value,
                )
                return value, reset_at
            return last, None

        state.pending_value = None
        state.pending_ts = None
        state.pending_count = 0
        return last, None

    def _aggregate_site_energy(
        self, payload: dict[str, object] | None
    ) -> tuple[dict[str, SiteEnergyFlow], dict[str, object]] | None:
        """Aggregate lifetime energy payload into kWh totals."""
        if not isinstance(payload, dict):
            return None

        start_date_raw = payload.get("start_date")
        start_date = str(start_date_raw) if start_date_raw is not None else None
        last_report_date = self._parse_site_energy_timestamp(
            payload.get("last_report_date")
        )
        update_pending = payload.get("update_pending")
        prev = self.site_energy or {}
        flows: dict[str, SiteEnergyFlow] = {}
        interval_hours, interval_minutes = self._site_energy_interval_hours(payload)
        source_unit = "Wh"
        hems_device_channels_raw = payload.get(HEMS_DEVICE_CHANNELS_META_KEY)
        hems_device_channels = (
            {
                str(channel).strip()
                for channel in hems_device_channels_raw
                if str(channel).strip()
            }
            if isinstance(hems_device_channels_raw, (set, tuple, list))
            else set()
        )

        def _store(
            flow: str,
            total_wh: float,
            fields: list[str],
            bucket_count: int,
            *,
            allow_zero: bool = False,
        ) -> None:
            if bucket_count <= 0 or total_wh < 0:
                return
            if total_wh == 0 and not allow_zero:
                return
            total_kwh = round(total_wh / 1000.0, 3)
            prev_entry = prev.get(flow)
            prev_value = None
            prev_reset_at = None
            if isinstance(prev_entry, SiteEnergyFlow):
                prev_value = prev_entry.value_kwh
                prev_reset_at = prev_entry.last_reset_at
            filtered, reset_at = self._apply_site_energy_guard(
                flow, total_kwh, prev_value
            )
            if filtered is None:
                return
            last_reset = (
                reset_at or prev_reset_at or self._site_energy_last_reset.get(flow)
            )
            flows[flow] = SiteEnergyFlow(
                value_kwh=filtered,
                bucket_count=bucket_count,
                fields_used=fields,
                start_date=start_date,
                last_report_date=last_report_date,
                update_pending=(
                    bool(update_pending) if update_pending is not None else None
                ),
                source_unit=source_unit,
                last_reset_at=last_reset,
                interval_minutes=interval_minutes,
            )
            if last_reset:
                self._site_energy_last_reset[flow] = last_reset

        # Solar production
        prod_total, prod_count = self._sum_energy_buckets(
            payload.get("production"), interval_hours
        )
        _store("solar_production", prod_total, ["production"], prod_count)

        # Site consumption (total energy consumed)
        cons_total, cons_count = self._sum_energy_buckets(
            payload.get("consumption"), interval_hours
        )
        _store("consumption", cons_total, ["consumption"], cons_count)

        # EVSE lifetime charging energy when the backend provides a dedicated flow.
        evse_total, evse_count = self._sum_energy_buckets(
            payload.get("evse"), interval_hours
        )
        _store(
            "evse_charging",
            evse_total,
            ["evse"],
            evse_count,
            allow_zero="evse" in hems_device_channels,
        )

        # Heat pump lifetime consumption.
        heat_pump_total, heat_pump_count = self._sum_energy_buckets(
            payload.get("heatpump"), interval_hours
        )
        _store(
            "heat_pump",
            heat_pump_total,
            ["heatpump"],
            heat_pump_count,
            allow_zero="heatpump" in hems_device_channels,
        )

        # Water heater lifetime consumption.
        water_heater_total, water_heater_count = self._sum_energy_buckets(
            payload.get("water_heater"), interval_hours
        )
        _store(
            "water_heater",
            water_heater_total,
            ["water_heater"],
            water_heater_count,
            allow_zero="water_heater" in hems_device_channels,
        )

        # Grid import (total site import).
        direct_import_total, direct_import_count = self._sum_energy_buckets(
            payload.get("import"), interval_hours
        )
        direct_grid_home_total, direct_grid_home_count = self._sum_energy_buckets(
            payload.get("grid_home"), interval_hours
        )
        derived_grid_home_total, derived_grid_home_count, derived_grid_home_fields = (
            self._diff_energy_fields_multi(
                payload,
                "consumption",
                ("solar_home", "battery_home"),
                interval_hours,
            )
        )
        grid_home_total, grid_home_count, grid_home_fields = (
            self._prefer_direct_energy_source(
                direct_total=direct_grid_home_total,
                direct_count=direct_grid_home_count,
                direct_fields=["grid_home"],
                fallback_total=derived_grid_home_total,
                fallback_count=derived_grid_home_count,
                fallback_fields=derived_grid_home_fields,
            )
        )

        direct_grid_battery_total, direct_grid_battery_count = self._sum_energy_buckets(
            payload.get("grid_battery"), interval_hours
        )
        (
            derived_grid_battery_total,
            derived_grid_battery_count,
            derived_grid_battery_fields,
        ) = self._diff_energy_fields(payload, "charge", "solar_battery", interval_hours)
        grid_battery_total, grid_battery_count, grid_battery_fields = (
            self._prefer_direct_energy_source(
                direct_total=direct_grid_battery_total,
                direct_count=direct_grid_battery_count,
                direct_fields=["grid_battery"],
                fallback_total=derived_grid_battery_total,
                fallback_count=derived_grid_battery_count,
                fallback_fields=derived_grid_battery_fields,
            )
        )

        import_total = 0.0
        import_fields: list[str] = []
        import_count = 0
        if grid_home_fields and grid_home_count > 0:
            import_total += grid_home_total
            import_fields.extend(grid_home_fields)
            import_count = max(import_count, grid_home_count)
        if grid_battery_fields and grid_battery_count > 0:
            import_total += grid_battery_total
            import_fields.extend(grid_battery_fields)
            import_count = max(import_count, grid_battery_count)

        import_total, import_count, import_fields = self._prefer_direct_energy_source(
            direct_total=direct_import_total,
            direct_count=direct_import_count,
            direct_fields=["import"],
            fallback_total=import_total,
            fallback_count=import_count,
            fallback_fields=import_fields,
        )
        if import_fields and import_count > 0:
            _store(
                "grid_import",
                import_total,
                import_fields,
                import_count,
                allow_zero=True,
            )

        # Grid export
        exp_total, exp_count = self._sum_energy_buckets(
            payload.get("solar_grid"), interval_hours
        )
        _store("grid_export", exp_total, ["solar_grid"], exp_count)

        # Battery charge (into battery)
        charge_total, charge_count = self._sum_energy_buckets(
            payload.get("charge"), interval_hours
        )
        if charge_total > 0 and charge_count > 0:
            _store("battery_charge", charge_total, ["charge"], charge_count)
        else:
            charge_total, charge_count, charge_fields = self._sum_energy_fields(
                payload, ("solar_battery", "grid_battery"), interval_hours
            )
            if charge_total > 0 and charge_fields:
                _store("battery_charge", charge_total, charge_fields, charge_count)

        # Battery discharge (out of battery)
        discharge_total, discharge_count = self._sum_energy_buckets(
            payload.get("discharge"), interval_hours
        )
        if discharge_total > 0 and discharge_count > 0:
            _store(
                "battery_discharge",
                discharge_total,
                ["discharge"],
                discharge_count,
            )
        else:
            discharge_total, discharge_count, discharge_fields = (
                self._sum_energy_fields(
                    payload, ("battery_home", "battery_grid"), interval_hours
                )
            )
            if discharge_total > 0 and discharge_fields:
                _store(
                    "battery_discharge",
                    discharge_total,
                    discharge_fields,
                    discharge_count,
                )

        raw_bucket_lengths = {
            key: len(value) for key, value in payload.items() if isinstance(value, list)
        }
        bucket_lengths = dict(raw_bucket_lengths)
        for channel in DEVICE_LIFETIME_CHANNELS:
            if self._device_lifetime_channel_has_positive(payload, channel):
                continue
            if channel in hems_device_channels:
                continue
            if channel in bucket_lengths:
                bucket_lengths[channel] = 0

        meta: dict[str, object] = {
            "start_date": start_date,
            "last_report_date": last_report_date,
            "update_pending": (
                bool(update_pending) if update_pending is not None else None
            ),
            "interval_minutes": interval_minutes,
            "bucket_lengths": bucket_lengths,
            "raw_bucket_lengths": raw_bucket_lengths,
        }
        return flows, meta

    def _lifetime_channel_missing(
        self, payload: dict[str, object], channel: str
    ) -> bool:
        values = payload.get(channel)
        if not isinstance(values, list) or len(values) == 0:
            return True
        # Lists containing only None/non-numeric/negative values are treated as missing.
        for value in values:
            numeric = self._coerce_energy_value(value)
            if numeric is not None and numeric >= 0:
                return False
        return True

    def _device_lifetime_channel_missing(
        self, payload: dict[str, object], channel: str
    ) -> bool:
        """Treat zero-only device arrays as missing so HEMS can fill them."""

        values = payload.get(channel)
        if not isinstance(values, list) or len(values) == 0:
            return True
        for value in values:
            numeric = self._coerce_energy_value(value)
            if numeric is not None and numeric > 0:
                return False
        return True

    def _device_lifetime_channel_has_positive(
        self, payload: dict[str, object], channel: str
    ) -> bool:
        """Return True when a device channel contains a positive numeric sample."""

        values = payload.get(channel)
        if not isinstance(values, list) or len(values) == 0:
            return False
        for value in values:
            numeric = self._coerce_energy_value(value)
            if numeric is not None and numeric > 0:
                return True
        return False

    def _merge_device_lifetime_channels(
        self,
        primary: dict[str, object],
        fallback: dict[str, object],
    ) -> dict[str, object]:
        """Merge device lifetime channels from fallback when primary is missing."""

        merged = dict(primary)
        hems_device_channels_raw = merged.get(HEMS_DEVICE_CHANNELS_META_KEY)
        hems_device_channels = (
            {
                str(channel).strip()
                for channel in hems_device_channels_raw
                if str(channel).strip()
            }
            if isinstance(hems_device_channels_raw, (set, tuple, list))
            else set()
        )
        for channel in DEVICE_LIFETIME_CHANNELS:
            if not self._device_lifetime_channel_missing(merged, channel):
                continue
            fallback_values = fallback.get(channel)
            if (
                isinstance(fallback_values, list)
                and fallback_values
                and not self._lifetime_channel_missing(fallback, channel)
            ):
                merged[channel] = list(fallback_values)
                hems_device_channels.add(channel)

        for metadata_key in (
            "start_date",
            "last_report_date",
            "update_pending",
            "interval_minutes",
            "system_id",
        ):
            if (
                merged.get(metadata_key) is None
                and fallback.get(metadata_key) is not None
            ):
                merged[metadata_key] = fallback.get(metadata_key)
        if hems_device_channels:
            merged[HEMS_DEVICE_CHANNELS_META_KEY] = hems_device_channels
        return merged

    def _hems_lifetime_backoff_active(self) -> bool:
        return bool(
            self._hems_lifetime_backoff_until
            and time.monotonic() < self._hems_lifetime_backoff_until
        )

    def _mark_hems_lifetime_available(self) -> None:
        self._hems_lifetime_supported = True
        self._hems_lifetime_backoff_until = None
        self._hems_lifetime_last_error = None

    def _note_hems_lifetime_unavailable(self, err: Exception | str | None) -> None:
        reason = redact_text(err, site_ids=(self.site_id,)) if err else ""
        if not reason:
            reason = "HEMS lifetime unavailable"
        self._hems_lifetime_last_error = reason
        if err is None:
            self._hems_lifetime_supported = False
            self._hems_lifetime_backoff_until = None
            return
        self._hems_lifetime_backoff_until = (
            time.monotonic() + HEMS_LIFETIME_FAILURE_BACKOFF_S
        )

    async def _async_refresh_site_energy(self, *, force: bool = False) -> None:
        """Refresh lifetime energy cache with TTL enforcement."""
        force_refresh = force or self._site_energy_force_refresh
        self._site_energy_force_refresh = False
        now_mono = time.monotonic()
        if self._service_backoff_active():
            return
        if (
            not force_refresh
            and self._site_energy_cache_ts is not None
            and (now_mono - self._site_energy_cache_ts) < self._site_energy_cache_ttl
        ):
            return
        try:
            client = self._client_provider()
            payload = await client.lifetime_energy()
        except SiteEnergyUnavailable as err:
            self._logger.debug(
                "Site energy service unavailable for site %s: %s",
                redact_site_id(self.site_id),
                redact_text(err, site_ids=(self.site_id,)),
            )
            self._note_service_unavailable(err)
            return
        except Exception as err:  # noqa: BLE001
            self._logger.debug(
                "Failed to fetch lifetime energy for site %s: %s",
                redact_site_id(self.site_id),
                redact_text(err, site_ids=(self.site_id,)),
            )
            return
        if isinstance(payload, dict):
            missing_channels = [
                channel
                for channel in DEVICE_LIFETIME_CHANNELS
                if self._device_lifetime_channel_missing(payload, channel)
            ]
            if missing_channels:
                hems_fetcher = getattr(client, "hems_consumption_lifetime", None)
                if callable(hems_fetcher):
                    hems_site_supported = getattr(client, "hems_site_supported", None)
                    if hems_site_supported is True:
                        self._hems_lifetime_supported = True
                    elif hems_site_supported is False:
                        self._hems_lifetime_supported = False
                    if (
                        self._hems_lifetime_supported is False
                        or self._hems_lifetime_backoff_active()
                        or (
                            self._hems_auth_skip is not None
                            and self._hems_auth_skip("hems_consumption_lifetime")
                        )
                    ):
                        hems_payload = None
                    else:
                        try:
                            hems_payload = await hems_fetcher()
                        except Exception as err:  # noqa: BLE001
                            if self._hems_auth_failure is not None:
                                self._hems_auth_failure(
                                    err,
                                    "hems_consumption_lifetime",
                                )
                            self._note_hems_lifetime_unavailable(err)
                            self._logger.debug(
                                "Optional HEMS lifetime fallback failed for site %s: %s",
                                redact_site_id(self.site_id),
                                redact_text(err, site_ids=(self.site_id,)),
                            )
                        else:
                            if isinstance(hems_payload, dict):
                                if self._hems_auth_success is not None:
                                    self._hems_auth_success("hems_consumption_lifetime")
                                self._mark_hems_lifetime_available()
                                payload = self._merge_device_lifetime_channels(
                                    payload, hems_payload
                                )
                            else:
                                self._note_hems_lifetime_unavailable(None)
        parsed = self._aggregate_site_energy(payload)
        if parsed is None:
            return
        flows, meta = parsed
        self._mark_service_available()
        self.site_energy = flows
        self._site_energy_meta = meta
        self._site_energy_cache_ts = time.monotonic()

    def _apply_lifetime_guard(
        self,
        sn: str,
        raw_value: object,
        prev: dict[str, object] | None,
    ) -> float | None:
        state = self._lifetime_guard.setdefault(sn, LifetimeGuardState())
        prev_val: float | None = None
        if isinstance(prev, dict):
            raw_prev = prev.get("lifetime_kwh")
            if isinstance(raw_prev, (int, float)):
                try:
                    prev_val = round(float(raw_prev), 3)
                except Exception:
                    prev_val = None
        if state.last is None and prev_val is not None:
            state.last = prev_val

        try:
            sample = float(str(raw_value))
        except (TypeError, ValueError):
            sample = None

        if sample is not None:
            if sample > 200:
                sample = sample / 1000.0
            sample = round(sample, 3)
            if sample < 0:
                sample = 0.0

        if sample is None:
            return state.last if state.last is not None else prev_val

        last = state.last
        if last is None:
            state.last = sample
            state.pending_value = None
            state.pending_ts = None
            state.pending_count = 0
            return sample

        drop = last - sample
        if drop < 0:
            state.last = sample
            state.pending_value = None
            state.pending_ts = None
            state.pending_count = 0
            return sample

        if drop <= LIFETIME_DROP_JITTER_KWH:
            state.pending_value = None
            state.pending_ts = None
            state.pending_count = 0
            return last

        is_reset_candidate = drop >= LIFETIME_RESET_DROP_THRESHOLD_KWH and (
            sample <= LIFETIME_RESET_FLOOR_KWH
            or sample <= (last * LIFETIME_RESET_RATIO)
        )

        if is_reset_candidate:
            now = time.monotonic()
            if (
                state.pending_value is not None
                and abs(sample - state.pending_value) <= LIFETIME_CONFIRM_TOLERANCE_KWH
            ):
                state.pending_count += 1
            else:
                state.pending_value = sample
                state.pending_ts = now
                state.pending_count = 1
                # Force next poll to refresh summary to validate reset
                if self._summary_invalidator is not None:
                    self._summary_invalidator()
                self._logger.debug(
                    "Ignoring suspected lifetime reset for %s: %.3f -> %.3f",
                    sn,
                    last,
                    sample,
                )
            if state.pending_count >= LIFETIME_CONFIRM_COUNT or (
                state.pending_ts is not None
                and (now - state.pending_ts) >= LIFETIME_CONFIRM_WINDOW_S
            ):
                confirm_count = state.pending_count
                state.last = sample
                state.pending_value = None
                state.pending_ts = None
                state.pending_count = 0
                self._logger.debug(
                    "Accepting lifetime reset for %s after %d samples: %.3f -> %.3f",
                    sn,
                    confirm_count,
                    last,
                    sample,
                )
                return sample
            return last

        # Generic backward jitter – hold previous reading
        state.pending_value = None
        state.pending_ts = None
        state.pending_count = 0
        return last
