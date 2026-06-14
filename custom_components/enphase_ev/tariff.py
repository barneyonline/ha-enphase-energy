"""Read-only tariff parsing and refresh helpers for Enphase sites."""

from __future__ import annotations

import calendar
import copy
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
import logging
import math
import re
from typing import TYPE_CHECKING

import aiohttp
from homeassistant.util import dt as dt_util

from .api import InvalidPayloadError, OptionalEndpointUnavailable
from .const import DOMAIN
from .service_validation import raise_translated_service_validation

if TYPE_CHECKING:
    from .coordinator import EnphaseCoordinator

TARIFF_ENDPOINT_FAMILY = "tariff"
TARIFF_DATED_RATES_ENDPOINT_FAMILY = "tariff_dated_rates"
TARIFF_SUCCESS_TTL_S = 900.0
TARIFF_BRANCH_KEYS = frozenset({"purchase", "buyback"})
TARIFF_TYPE_IDS = frozenset({"flat", "tou", "tiered"})
TARIFF_TYPE_KINDS = frozenset(
    {"single", "seasonal", "weekends", "seasonal-and-weekends"}
)
_EXPORT_RATE_KEY_RE = re.compile(r"[^a-z0-9]+")
_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class TariffRateLocator:
    """Stable location for an editable tariff rate in the raw payload."""

    branch: str
    kind: str
    season_index: int
    season_id: str | None = None
    day_index: int | None = None
    day_group_id: str | None = None
    period_index: int | None = None
    period_id: str | None = None
    period_type: str | None = None
    tier_index: int | None = None
    tier_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serializable locator."""

        return {
            key: value
            for key, value in {
                "branch": self.branch,
                "kind": self.kind,
                "season_index": self.season_index,
                "season_id": self.season_id,
                "day_index": self.day_index,
                "day_group_id": self.day_group_id,
                "period_index": self.period_index,
                "period_id": self.period_id,
                "period_type": self.period_type,
                "tier_index": self.tier_index,
                "tier_id": self.tier_id,
            }.items()
            if value is not None
        }

    @classmethod
    def from_object(cls, value: object) -> "TariffRateLocator | None":
        """Parse a locator from entity state attributes or service data."""

        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            return None
        branch = _clean_text(value.get("branch"))
        kind = _clean_text(value.get("kind"))
        season_index = _int_or_none(value.get("season_index"))
        if branch not in {"purchase", "buyback"} or kind not in {
            "period",
            "tier",
            "off_peak",
        }:
            return None
        if season_index is None or season_index < 1:
            return None
        return cls(
            branch=branch,
            kind=kind,
            season_index=season_index,
            season_id=_clean_text(value.get("season_id")),
            day_index=_int_or_none(value.get("day_index")),
            day_group_id=_clean_text(value.get("day_group_id")),
            period_index=_int_or_none(value.get("period_index")),
            period_id=_clean_text(value.get("period_id")),
            period_type=_clean_text(value.get("period_type")),
            tier_index=_int_or_none(value.get("tier_index")),
            tier_id=_clean_text(value.get("tier_id")),
        )


@dataclass(slots=True, frozen=True)
class TariffBillingSnapshot:
    """Normalized billing-cycle metadata."""

    start_date: str | None
    billing_frequency: str | None
    billing_interval_value: int | None
    billing_cycle: str | None

    @property
    def state(self) -> str | None:
        """Return the concise billing-cycle label."""

        return self.billing_cycle

    @property
    def attributes(self) -> dict[str, object]:
        """Return Home Assistant state attributes."""

        return {
            "start_date": self.start_date,
            "billing_frequency": self.billing_frequency,
            "billing_interval_value": self.billing_interval_value,
            "billing_cycle": self.billing_cycle,
        }


@dataclass(slots=True, frozen=True)
class TariffBillingUpdate:
    """Validated tariff billing-cycle update payload."""

    start_date: str
    billing_frequency: str
    billing_interval_value: int

    @property
    def payload(self) -> dict[str, object]:
        """Return the Enphase billing-details write payload."""

        return {
            "anyBillPeriodStartDate": self.start_date,
            "billingFrequency": self.billing_frequency,
            "billingIntervalValue": self.billing_interval_value,
        }

    @classmethod
    def from_object(cls, value: object) -> "TariffBillingUpdate | None":
        """Parse and validate a billing-cycle update."""

        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            return None
        start_date = _clean_text(
            value.get("anyBillPeriodStartDate")
            if "anyBillPeriodStartDate" in value
            else value.get("billing_start_date")
        )
        frequency = (
            _clean_text(
                value.get("billingFrequency")
                if "billingFrequency" in value
                else value.get("billing_frequency")
            )
            or ""
        ).upper()
        interval = _int_or_none(
            value.get("billingIntervalValue")
            if "billingIntervalValue" in value
            else value.get("billing_interval_value")
        )
        if start_date is None:
            _raise_tariff_validation(
                "tariff_billing_start_date_invalid",
                message="Tariff billing start date must be a valid ISO date.",
            )
        try:
            date.fromisoformat(start_date)
        except ValueError:
            _raise_tariff_validation(
                "tariff_billing_start_date_invalid",
                message="Tariff billing start date must be a valid ISO date.",
            )
        if frequency not in {"MONTH", "DAY"}:
            _raise_tariff_validation(
                "tariff_billing_frequency_invalid",
                message="Tariff billing frequency must be MONTH or DAY.",
            )
        max_interval = 24 if frequency == "MONTH" else 100
        if interval is None or interval < 1 or interval > max_interval:
            _raise_tariff_validation(
                "tariff_billing_interval_invalid",
                placeholders={"minimum": "1", "maximum": str(max_interval)},
                message=(
                    "Tariff billing interval must be between " f"1 and {max_interval}."
                ),
            )
        return cls(
            start_date=start_date,
            billing_frequency=frequency,
            billing_interval_value=interval,
        )


@dataclass(slots=True, frozen=True)
class TariffRateSnapshot:
    """Normalized tariff branch metadata."""

    state: str | None
    rate_structure: str | None
    variation_type: str | None
    source: str | None
    currency: str | None
    export_plan: str | None
    seasons: tuple[dict[str, object], ...]
    branch_key: str | None = None

    @property
    def attributes(self) -> dict[str, object]:
        """Return Home Assistant state attributes."""

        attrs: dict[str, object] = {
            "rate_structure": self.rate_structure,
            "variation_type": self.variation_type,
            "source": self.source,
            "currency": self.currency,
            "seasons": [dict(season) for season in self.seasons],
        }
        if self.export_plan is not None:
            attrs["export_plan"] = self.export_plan
        return attrs


@dataclass(slots=True, frozen=True)
class TariffRateUpdate:
    """Validated tariff rate update request."""

    locator: TariffRateLocator
    rate: float

    @classmethod
    def from_object(cls, value: object) -> "TariffRateUpdate | None":
        """Parse a rate update from service/runtime input."""

        if isinstance(value, cls):
            return value
        if isinstance(value, tuple) and len(value) == 2:
            locator_raw, rate_raw = value
        elif isinstance(value, dict):
            locator_raw = value.get("locator", value.get("tariff_locator"))
            rate_raw = value.get("rate")
        else:
            return None
        locator = TariffRateLocator.from_object(locator_raw)
        if locator is None:
            return None
        try:
            rate = float(rate_raw)
        except (TypeError, ValueError):
            _raise_tariff_validation(
                "tariff_rate_invalid",
                message="Tariff rate must be a non-negative number.",
            )
        return cls(locator=locator, rate=rate)


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:
        return None
    return text or None


def _int_or_none(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _rate_structure_label(value: object) -> str | None:
    type_id = (_clean_text(value) or "").lower()
    return {
        "flat": "Flat",
        "tou": "Time of use",
        "tiered": "Tiered",
    }.get(type_id, _clean_text(value))


def _variation_label(value: object) -> str | None:
    type_kind = (_clean_text(value) or "").lower()
    return {
        "single": "Single",
        "seasonal": "Seasonal",
        "weekends": "Weekdays and weekends",
        "seasonal-and-weekends": "Seasonal weekdays and weekends",
    }.get(type_kind, _clean_text(value))


def _minutes_to_hhmm(value: object) -> str | None:
    minutes = _int_or_none(value)
    if minutes is None:
        return None
    minutes %= 24 * 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _normalize_days(values: object) -> list[int]:
    if not isinstance(values, list):
        return []
    days: list[int] = []
    for value in values:
        day = _int_or_none(value)
        if day is not None:
            days.append(day)
    return days


def _normalize_period(period: object) -> dict[str, object] | None:
    if not isinstance(period, dict):
        return None
    normalized: dict[str, object] = {
        "id": _clean_text(period.get("id")),
        "type": _clean_text(period.get("type")),
        "rate": _clean_text(period.get("rate")),
        "start_time": _minutes_to_hhmm(period.get("startTime")),
        "end_time": _minutes_to_hhmm(period.get("endTime")),
    }
    return {key: value for key, value in normalized.items() if value is not None}


def _normalize_tier(tier: object) -> dict[str, object] | None:
    if not isinstance(tier, dict):
        return None
    normalized: dict[str, object] = {
        "id": _clean_text(tier.get("id")),
        "rate": _clean_text(tier.get("rate")),
        "start_value": _clean_text(tier.get("startValue")),
    }
    end_value = tier.get("endValue")
    if end_value == -1 or _clean_text(end_value) == "-1":
        normalized["end_value"] = None
        normalized["unbounded"] = True
    else:
        normalized["end_value"] = _clean_text(end_value)
    return {key: value for key, value in normalized.items() if value is not None}


def _normalize_day_group(day_group: object) -> dict[str, object] | None:
    if not isinstance(day_group, dict):
        return None
    periods = [
        item
        for period in day_group.get("periods", [])
        if (item := _normalize_period(period)) is not None
    ]
    normalized: dict[str, object] = {
        "id": _clean_text(day_group.get("id")),
        "days": _normalize_days(day_group.get("days")),
        "periods": periods,
    }
    return {key: value for key, value in normalized.items() if value not in (None, [])}


def _normalize_season(season: object) -> dict[str, object] | None:
    if not isinstance(season, dict):
        return None
    normalized: dict[str, object] = {
        "id": _clean_text(season.get("id")),
        "start_month": _int_or_none(season.get("startMonth")),
        "end_month": _int_or_none(season.get("endMonth")),
    }
    days = [
        item
        for day_group in season.get("days", [])
        if (item := _normalize_day_group(day_group)) is not None
    ]
    tiers = [
        item
        for tier in season.get("tiers", [])
        if (item := _normalize_tier(tier)) is not None
    ]
    if days:
        normalized["days"] = days
    if tiers:
        normalized["tiers"] = tiers
    off_peak = _clean_text(season.get("offPeak"))
    if off_peak is not None:
        normalized["off_peak"] = off_peak
    return {key: value for key, value in normalized.items() if value is not None}


def _slug(value: object, fallback: str) -> str:
    text = (_clean_text(value) or fallback).lower()
    slug = _EXPORT_RATE_KEY_RE.sub("_", text).strip("_")
    return slug or fallback


def _rate_value(rate: object) -> float | None:
    rate_text = _clean_text(rate)
    if rate_text is None:
        return None
    try:
        return float(rate_text)
    except ValueError:
        return None


def _rate_unit(currency: object) -> str | None:
    currency_text = _clean_text(currency)
    if currency_text is None:
        return None
    if len(currency_text) == 3 and currency_text.isalpha():
        currency_text = currency_text.upper()
    return f"{currency_text}/kWh"


def _format_rate(rate: object, currency: object) -> str | None:
    rate_text = _clean_text(rate)
    if rate_text is None:
        return None
    currency_text = _clean_text(currency)
    if currency_text is None:
        return rate_text
    if len(currency_text) == 3 and currency_text.isalpha():
        return f"{rate_text} {currency_text.upper()}"
    return f"{currency_text}{rate_text}"


def _rate_detail_name(*parts: object) -> str:
    names = [_clean_text(part) for part in parts]
    return " ".join(part.title() for part in names if part)


def _month_matches(
    month: int,
    start_month: object,
    end_month: object,
) -> bool:
    start = _int_or_none(start_month)
    end = _int_or_none(end_month)
    if start is None or end is None:
        return True
    if not 1 <= start <= 12 or not 1 <= end <= 12:
        return True
    if start <= end:
        return start <= month <= end
    return month >= start or month <= end


def _time_to_minutes(value: object) -> int | None:
    text = _clean_text(value)
    if text is None:
        return None
    parts = text.split(":", 1)
    if len(parts) != 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return None
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    return hour * 60 + minute


def _time_window_matches(
    minute_of_day: int,
    start_time: object,
    end_time: object,
) -> bool:
    start = _time_to_minutes(start_time)
    end = _time_to_minutes(end_time)
    if start is None or end is None or start == end:
        return True
    if start < end:
        return start <= minute_of_day < end
    return minute_of_day >= start or minute_of_day < end


def _time_window_is_constrained(start_time: object, end_time: object) -> bool:
    start = _time_to_minutes(start_time)
    end = _time_to_minutes(end_time)
    return start is not None and end is not None and start != end


def _tariff_rate_scope_key(attrs: dict) -> tuple[object, ...]:
    days = attrs.get("days")
    days_key: tuple[object, ...] | None = None
    if isinstance(days, list):
        days_key = tuple(days)
    return (
        attrs.get("season_id"),
        attrs.get("start_month"),
        attrs.get("end_month"),
        attrs.get("day_group_id"),
        days_key,
    )


def current_tariff_rate_sensor_spec(
    snapshot: TariffRateSnapshot | None,
    when: datetime,
) -> dict | None:
    """Return the unambiguous current rate spec for an Energy price sensor."""

    specs = tariff_rate_sensor_specs(snapshot)
    if not specs:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
    month = when.month
    weekday = when.isoweekday()
    minute_of_day = when.hour * 60 + when.minute

    matches: list[dict] = []
    for spec in specs:
        attrs = spec.get("attributes") or {}
        if not _month_matches(
            month,
            attrs.get("start_month"),
            attrs.get("end_month"),
        ):
            continue
        days = attrs.get("days")
        if (
            isinstance(days, list)
            and days
            and weekday
            not in {day for item in days if (day := _int_or_none(item)) is not None}
        ):
            continue
        if not _time_window_matches(
            minute_of_day,
            attrs.get("start_time"),
            attrs.get("end_time"),
        ):
            continue
        matches.append(spec)
    if len(matches) != 1:
        constrained_matches = [
            spec
            for spec in matches
            if _time_window_is_constrained(
                (spec.get("attributes") or {}).get("start_time"),
                (spec.get("attributes") or {}).get("end_time"),
            )
        ]
        if len(constrained_matches) == 1:
            constrained_attrs = constrained_matches[0].get("attributes") or {}
            constrained_scope = _tariff_rate_scope_key(constrained_attrs)
            if all(
                _tariff_rate_scope_key(spec.get("attributes") or {})
                == constrained_scope
                for spec in matches
            ):
                return constrained_matches[0]
        return None
    return matches[0]


def next_tariff_rate_change(
    snapshot: TariffRateSnapshot | None,
    when: datetime,
) -> datetime | None:
    """Return the next time the active tariff rate may change."""

    specs = tariff_rate_sensor_specs(snapshot)
    if not specs:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
    active = current_tariff_rate_sensor_spec(snapshot, when)
    active_key = None if active is None else active.get("key")
    active_state = None if active is None else active.get("state")
    tzinfo = when.tzinfo
    candidates: set[datetime] = set()
    start_date = when.date()
    for day_offset in range(0, 370):
        day = start_date + timedelta(days=day_offset)
        candidates.add(datetime.combine(day, dt_time.min, tzinfo))
        for spec in specs:
            attrs = spec.get("attributes") or {}
            for attr in ("start_time", "end_time"):
                minute_of_day = _time_to_minutes(attrs.get(attr))
                if minute_of_day is None:
                    continue
                candidates.add(
                    datetime.combine(
                        day,
                        dt_time(minute_of_day // 60, minute_of_day % 60),
                        tzinfo,
                    )
                )

    for candidate in sorted(candidates):
        if candidate <= when:
            continue
        candidate_spec = current_tariff_rate_sensor_spec(snapshot, candidate)
        candidate_key = None if candidate_spec is None else candidate_spec.get("key")
        candidate_state = (
            None if candidate_spec is None else candidate_spec.get("state")
        )
        if (candidate_key, candidate_state) != (active_key, active_state):
            return candidate
    return None


def tariff_rate_sensor_specs(snapshot: TariffRateSnapshot | None) -> tuple[dict, ...]:
    """Return per-rate sensor specs for period and tiered tariffs."""

    if snapshot is None:
        return ()
    specs: list[dict] = []
    used_keys: set[str] = set()

    def _append_spec(base_key: str, name: str, state: float, attrs: dict) -> None:
        key = base_key
        index = 2
        while key in used_keys:
            key = f"{base_key}_{index}"
            index += 1
        used_keys.add(key)
        specs.append(
            {
                "key": key,
                "name": name,
                "state": state,
                "unit": _rate_unit(snapshot.currency),
                "attributes": attrs,
            }
        )

    base_attrs = {
        "rate_structure": snapshot.rate_structure,
        "variation_type": snapshot.variation_type,
        "source": snapshot.source,
        "currency": snapshot.currency,
        "export_plan": snapshot.export_plan,
    }
    branch_key = (
        snapshot.branch_key if snapshot.branch_key in {"purchase", "buyback"} else None
    )
    for season_index, season in enumerate(snapshot.seasons, start=1):
        season_attrs = {
            **base_attrs,
            "season_id": season.get("id"),
            "start_month": season.get("start_month"),
            "end_month": season.get("end_month"),
        }
        off_peak_state = _rate_value(season.get("off_peak"))
        if off_peak_state is not None:
            attrs = {
                **season_attrs,
                "rate": season.get("off_peak"),
                "formatted_rate": _format_rate(
                    season.get("off_peak"), snapshot.currency
                ),
            }
            if branch_key is not None:
                attrs["tariff_locator"] = TariffRateLocator(
                    branch=branch_key,
                    kind="off_peak",
                    season_index=season_index,
                    season_id=_clean_text(season.get("id")),
                ).as_dict()
            _append_spec(
                "_".join(
                    (
                        _slug(season.get("id"), f"season_{season_index}"),
                        "off_peak",
                    )
                ),
                "Off-Peak",
                off_peak_state,
                {attr: value for attr, value in attrs.items() if value is not None},
            )
        for day_index, day_group in enumerate(season.get("days", []), start=1):
            if not isinstance(day_group, dict):
                continue
            day_attrs = {
                **season_attrs,
                "day_group_id": day_group.get("id"),
                "days": day_group.get("days"),
            }
            for period_index, period in enumerate(
                day_group.get("periods", []), start=1
            ):
                if not isinstance(period, dict):
                    continue
                state = _rate_value(period.get("rate"))
                if state is None:
                    continue
                period_type = _clean_text(period.get("type"))
                period_id = _clean_text(period.get("id"))
                name = _rate_detail_name(period_type or period_id) or (
                    f"Period {period_index}"
                )
                key = "_".join(
                    (
                        _slug(season.get("id"), f"season_{season_index}"),
                        _slug(day_group.get("id"), f"days_{day_index}"),
                        _slug(period_type or period_id, f"period_{period_index}"),
                    )
                )
                attrs = {
                    **day_attrs,
                    "period_id": period_id,
                    "period_type": period_type,
                    "start_time": period.get("start_time"),
                    "end_time": period.get("end_time"),
                    "rate": period.get("rate"),
                    "formatted_rate": _format_rate(
                        period.get("rate"), snapshot.currency
                    ),
                }
                if branch_key is not None:
                    attrs["tariff_locator"] = TariffRateLocator(
                        branch=branch_key,
                        kind="period",
                        season_index=season_index,
                        season_id=_clean_text(season.get("id")),
                        day_index=day_index,
                        day_group_id=_clean_text(day_group.get("id")),
                        period_index=period_index,
                        period_id=period_id,
                        period_type=period_type,
                    ).as_dict()
                _append_spec(
                    key,
                    name,
                    state,
                    {attr: value for attr, value in attrs.items() if value is not None},
                )
        for tier_index, tier in enumerate(season.get("tiers", []), start=1):
            if not isinstance(tier, dict):
                continue
            state = _rate_value(tier.get("rate"))
            if state is None:
                continue
            tier_id = _clean_text(tier.get("id"))
            name = _rate_detail_name(tier_id) or f"Tier {tier_index}"
            key = "_".join(
                (
                    _slug(season.get("id"), f"season_{season_index}"),
                    _slug(tier_id, f"tier_{tier_index}"),
                )
            )
            attrs = {
                **season_attrs,
                "tier_id": tier_id,
                "start_value": tier.get("start_value"),
                "end_value": tier.get("end_value"),
                "unbounded": tier.get("unbounded"),
                "rate": tier.get("rate"),
                "formatted_rate": _format_rate(tier.get("rate"), snapshot.currency),
            }
            if branch_key is not None:
                attrs["tariff_locator"] = TariffRateLocator(
                    branch=branch_key,
                    kind="tier",
                    season_index=season_index,
                    season_id=_clean_text(season.get("id")),
                    tier_index=tier_index,
                    tier_id=tier_id,
                ).as_dict()
            _append_spec(
                key,
                name,
                state,
                {attr: value for attr, value in attrs.items() if value is not None},
            )
    return tuple(specs)


def export_rate_sensor_specs(snapshot: TariffRateSnapshot | None) -> tuple[dict, ...]:
    """Return per-export-rate sensor specs for period and tiered tariffs."""

    return tariff_rate_sensor_specs(snapshot)


def _add_months_clamped(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def next_billing_date(
    snapshot: TariffBillingSnapshot,
    *,
    today: date | None = None,
) -> date | None:
    """Return the next billing date after today for a billing snapshot."""

    if snapshot.start_date is None or snapshot.billing_frequency is None:
        return None
    interval = (
        1
        if snapshot.billing_interval_value is None
        else snapshot.billing_interval_value
    )
    if interval < 1:
        return None
    try:
        start = date.fromisoformat(snapshot.start_date)
    except ValueError:
        return None
    candidate = start
    today = today or dt_util.now().date()
    if snapshot.billing_frequency == "DAY":
        while candidate <= today:
            candidate = candidate + timedelta(days=interval)
        return candidate
    if snapshot.billing_frequency == "MONTH":
        elapsed_months = 0
        while candidate <= today:
            elapsed_months += interval
            candidate = _add_months_clamped(start, elapsed_months)
        return candidate
    return None


def parse_tariff_billing(payload: object) -> TariffBillingSnapshot | None:
    """Return a normalized billing snapshot from a billing-details payload."""

    if not isinstance(payload, dict):
        return None
    frequency = (_clean_text(payload.get("billingFrequency")) or "").upper()
    interval = _int_or_none(payload.get("billingIntervalValue"))
    start_date = _clean_text(payload.get("anyBillPeriodStartDate"))
    if not frequency and interval is None and start_date is None:
        return None
    state: str | None
    if frequency == "MONTH":
        if interval in (None, 1):
            state = "Monthly"
        else:
            state = f"Every {interval} months"
    elif frequency == "DAY":
        if interval in (None, 1):
            state = "Daily"
        else:
            state = f"Every {interval} days"
    else:
        state = _clean_text(payload.get("billingFrequency"))
    return TariffBillingSnapshot(
        start_date=start_date,
        billing_frequency=frequency or None,
        billing_interval_value=interval,
        billing_cycle=state,
    )


def parse_tariff_rate(
    payload: object,
    branch_key: str,
) -> TariffRateSnapshot | None:
    """Return a normalized import/export tariff snapshot."""

    if not isinstance(payload, dict):
        return None
    branch = payload.get(branch_key)
    if not isinstance(branch, dict):
        return None
    seasons = [
        item
        for season in branch.get("seasons", [])
        if (item := _normalize_season(season)) is not None
    ]
    rate_structure = _rate_structure_label(branch.get("typeId"))
    variation_type = _variation_label(branch.get("typeKind"))
    state = rate_structure
    if state is None and not seasons:
        return None
    return TariffRateSnapshot(
        state=state,
        rate_structure=rate_structure,
        variation_type=variation_type,
        source=_clean_text(branch.get("source")),
        currency=_clean_text(payload.get("currency")),
        export_plan=(
            _clean_text(branch.get("exportPlan")) if branch_key == "buyback" else None
        ),
        seasons=tuple(seasons),
        branch_key=branch_key,
    )


def parse_dated_tariff_rate(
    payload: object,
    branch_key: str,
) -> TariffRateSnapshot | None:
    """Return a read-only tariff snapshot from the dated rates endpoint."""

    if branch_key not in TARIFF_BRANCH_KEYS or not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    rate_rows = data.get(branch_key)
    if not isinstance(rate_rows, list):
        return None

    periods: list[dict[str, object]] = []
    for index, row in enumerate(rate_rows, start=1):
        if not isinstance(row, dict) or _rate_value(row.get("rate")) is None:
            continue
        period: dict[str, object] = {
            "id": f"rate-{index}",
            "type": branch_key,
            "rate": _clean_text(row.get("rate")),
        }
        start = _int_or_none(row.get("start"))
        end = _int_or_none(row.get("end"))
        if (
            start is not None
            and end is not None
            and 0 <= start < 24 * 60
            and 0 <= end < 24 * 60
        ):
            period["start_time"] = _minutes_to_hhmm(start)
            period["end_time"] = _minutes_to_hhmm(end + 1)
        periods.append(period)

    if not periods:
        return None

    site_details = data.get("siteDetails")
    if not isinstance(site_details, dict):
        site_details = {}
    currency = _clean_text(data.get("currency")) or _clean_text(
        site_details.get("currency")
    )
    rate_structure = "Flat" if len(periods) == 1 else "Time of use"
    export_plan = None
    if branch_key == "buyback":
        export_plan = _clean_text(site_details.get("exportPlanType"))

    return TariffRateSnapshot(
        state=rate_structure,
        rate_structure=rate_structure,
        variation_type="Single",
        source=_clean_text(data.get("tariff-type")) or _clean_text(payload.get("type")),
        currency=currency,
        export_plan=export_plan,
        seasons=(
            {
                "id": _clean_text(data.get("tariff-version-id")) or "current",
                "days": [
                    {
                        "id": "all",
                        "days": [1, 2, 3, 4, 5, 6, 7],
                        "periods": periods,
                    }
                ],
            },
        ),
        branch_key=None,
    )


def _format_write_rate(value: float) -> str:
    if not math.isfinite(value) or value < 0:
        _raise_tariff_validation(
            "tariff_rate_invalid",
            message="Tariff rate must be a non-negative number.",
        )
    return f"{value:.10f}".rstrip("0").rstrip(".") or "0"


def _validate_write_rate(value: object) -> None:
    try:
        _format_write_rate(float(value))
    except (TypeError, ValueError):
        _raise_tariff_validation(
            "tariff_rate_invalid",
            message="Tariff rate must be a non-negative number.",
        )


def _minutes_or_empty(value: object) -> int | None:
    if value == "":
        return None
    minutes = _int_or_none(value)
    if minutes is None or minutes < 0 or minutes > 24 * 60:
        _raise_tariff_validation(
            "tariff_structure_invalid",
            message="Tariff structure is invalid.",
        )
    return minutes


def _validate_tariff_period(period: object) -> None:
    if not isinstance(period, dict):
        _raise_tariff_validation(
            "tariff_structure_invalid",
            message="Tariff structure is invalid.",
        )
    _validate_write_rate(period.get("rate"))
    start = _minutes_or_empty(period.get("startTime", ""))
    end = _minutes_or_empty(period.get("endTime", ""))
    if (start is None) != (end is None) or (
        start is not None and end is not None and start >= end
    ):
        _raise_tariff_validation(
            "tariff_structure_invalid",
            message="Tariff structure is invalid.",
        )


def _validate_tariff_tier(tier: object) -> None:
    if not isinstance(tier, dict):
        _raise_tariff_validation(
            "tariff_structure_invalid",
            message="Tariff structure is invalid.",
        )
    _validate_write_rate(tier.get("rate"))
    for key in ("startValue", "endValue"):
        value = tier.get(key)
        if value in (None, "") or (key == "endValue" and _clean_text(value) == "-1"):
            continue
        try:
            numeric = float(str(value).strip())
        except (TypeError, ValueError):
            _raise_tariff_validation(
                "tariff_structure_invalid",
                message="Tariff structure is invalid.",
            )
        if not math.isfinite(numeric) or numeric < 0:
            _raise_tariff_validation(
                "tariff_structure_invalid",
                message="Tariff structure is invalid.",
            )


def _validate_tariff_season(season: object, *, type_id: str) -> None:
    if not isinstance(season, dict):
        _raise_tariff_validation(
            "tariff_structure_invalid",
            message="Tariff structure is invalid.",
        )
    for key in ("startMonth", "endMonth"):
        value = season.get(key)
        if value in (None, ""):
            continue
        month = _int_or_none(value)
        if month is None or month < 1 or month > 12:
            _raise_tariff_validation(
                "tariff_structure_invalid",
                message="Tariff structure is invalid.",
            )
    if type_id == "tiered":
        tiers = season.get("tiers")
        if not isinstance(tiers, list):
            _raise_tariff_validation(
                "tariff_structure_invalid",
                message="Tariff structure is invalid.",
            )
        for tier in tiers:
            _validate_tariff_tier(tier)
        if "offPeak" in season:
            _validate_write_rate(season.get("offPeak"))
        return
    days = season.get("days")
    if not isinstance(days, list) or not days:
        _raise_tariff_validation(
            "tariff_structure_invalid",
            message="Tariff structure is invalid.",
        )
    for day_group in days:
        if not isinstance(day_group, dict):
            _raise_tariff_validation(
                "tariff_structure_invalid",
                message="Tariff structure is invalid.",
            )
        periods = day_group.get("periods")
        if not isinstance(periods, list) or not periods:
            _raise_tariff_validation(
                "tariff_structure_invalid",
                message="Tariff structure is invalid.",
            )
        for period in periods:
            _validate_tariff_period(period)


def _validated_tariff_branch(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        _raise_tariff_validation(
            "tariff_structure_invalid",
            message="Tariff structure is invalid.",
        )
    branch = copy.deepcopy(value)
    type_id = (_clean_text(branch.get("typeId")) or "").lower()
    type_kind = (_clean_text(branch.get("typeKind")) or "").lower()
    seasons = branch.get("seasons")
    if (
        type_id not in TARIFF_TYPE_IDS
        or type_kind not in TARIFF_TYPE_KINDS
        or not isinstance(seasons, list)
        or not seasons
    ):
        _raise_tariff_validation(
            "tariff_structure_invalid",
            message="Tariff structure is invalid.",
        )
    for season in seasons:
        _validate_tariff_season(season, type_id=type_id)
    return branch


def _validated_tariff_payload(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        _raise_tariff_validation(
            "tariff_structure_invalid",
            message="Tariff structure is invalid.",
        )
    payload = copy.deepcopy(value)
    if not any(branch in payload for branch in TARIFF_BRANCH_KEYS):
        _raise_tariff_validation(
            "tariff_structure_invalid",
            message="Tariff structure is invalid.",
        )
    for branch_key in TARIFF_BRANCH_KEYS:
        if branch_key in payload:
            payload[branch_key] = _validated_tariff_branch(payload[branch_key])
    return payload


def _locator_key(locator: TariffRateLocator) -> tuple[tuple[str, object], ...]:
    """Return a stable hashable key for a tariff locator."""

    return tuple(locator.as_dict().items())


def _raise_tariff_validation(
    key: str,
    *,
    placeholders: dict[str, object] | None = None,
    message: str | None = None,
) -> None:
    raise_translated_service_validation(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders,
        message=message,
    )


def _index_item(items: object, index: int | None) -> dict[str, object] | None:
    if index is None or index < 1 or not isinstance(items, list):
        return None
    try:
        item = items[index - 1]
    except IndexError:
        return None
    return item if isinstance(item, dict) else None


def _matches_text(actual: object, expected: str | None) -> bool:
    return expected is None or _clean_text(actual) == expected


def _locate_tariff_rate(
    payload: dict[str, object],
    locator: TariffRateLocator,
) -> tuple[dict[str, object], str]:
    branch = payload.get(locator.branch)
    if not isinstance(branch, dict):
        _raise_tariff_validation(
            "tariff_rate_target_not_found",
            message="Tariff rate target was not found in the latest tariff payload.",
        )
    season = _index_item(branch.get("seasons"), locator.season_index)
    if season is None or not _matches_text(season.get("id"), locator.season_id):
        _raise_tariff_validation(
            "tariff_rate_target_not_found",
            message="Tariff rate target was not found in the latest tariff payload.",
        )

    if locator.kind == "off_peak":
        if "offPeak" not in season:
            _raise_tariff_validation(
                "tariff_rate_target_not_found",
                message=(
                    "Tariff rate target was not found in the latest tariff payload."
                ),
            )
        return season, "offPeak"

    if locator.kind == "period":
        day_group = _index_item(season.get("days"), locator.day_index)
        if day_group is None or not _matches_text(
            day_group.get("id"), locator.day_group_id
        ):
            _raise_tariff_validation(
                "tariff_rate_target_not_found",
                message=(
                    "Tariff rate target was not found in the latest tariff payload."
                ),
            )
        period = _index_item(day_group.get("periods"), locator.period_index)
        if (
            period is None
            or not _matches_text(period.get("id"), locator.period_id)
            or not _matches_text(period.get("type"), locator.period_type)
        ):
            _raise_tariff_validation(
                "tariff_rate_target_not_found",
                message=(
                    "Tariff rate target was not found in the latest tariff payload."
                ),
            )
        return period, "rate"

    if locator.kind == "tier":
        tier = _index_item(season.get("tiers"), locator.tier_index)
        if tier is None or not _matches_text(tier.get("id"), locator.tier_id):
            _raise_tariff_validation(
                "tariff_rate_target_not_found",
                message=(
                    "Tariff rate target was not found in the latest tariff payload."
                ),
            )
        return tier, "rate"

    _raise_tariff_validation(
        "tariff_rate_target_invalid",
        message="Tariff rate target is invalid.",
    )


def _site_local_today(coord: EnphaseCoordinator) -> date:
    tz_name = None
    site_tz = getattr(coord, "_site_timezone_name", None)
    if callable(site_tz):
        tz_name = site_tz()
    tzinfo = dt_util.get_time_zone(tz_name) if isinstance(tz_name, str) else None
    return dt_util.now(tzinfo or dt_util.DEFAULT_TIME_ZONE).date()


def _endpoint_family_diagnostics(coord: EnphaseCoordinator, family: str) -> dict:
    health = coord._endpoint_family_state(family)
    return {
        "support_state": health.support_state,
        "cooldown_active": health.cooldown_active,
        "consecutive_failures": health.consecutive_failures,
        "last_success_utc": (
            health.last_success_utc.isoformat()
            if health.last_success_utc is not None
            else None
        ),
        "last_failure_utc": (
            health.last_failure_utc.isoformat()
            if health.last_failure_utc is not None
            else None
        ),
        "last_status": health.last_status,
        "last_error": health.last_error,
        "next_retry_utc": (
            health.next_retry_utc.isoformat()
            if health.next_retry_utc is not None
            else None
        ),
    }


class TariffRuntime:
    """Fetch, normalize, and update site tariff data."""

    def __init__(self, coordinator: EnphaseCoordinator) -> None:
        self.coordinator = coordinator

    def refresh_due(self) -> bool:
        """Return whether tariff data should be refreshed this cycle."""

        return self.coordinator._endpoint_family_should_run(TARIFF_ENDPOINT_FAMILY)

    async def async_refresh(self, *, force: bool = False) -> None:
        """Refresh tariff billing and rate snapshots."""

        coord = self.coordinator
        if not coord._endpoint_family_should_run(TARIFF_ENDPOINT_FAMILY, force=force):
            return
        try:
            site_tariff_bundle = getattr(coord.client, "site_tariff_bundle", None)
            if not callable(site_tariff_bundle):
                raise OptionalEndpointUnavailable("Tariff API is unavailable")
            billing_payload, tariff_payload = await site_tariff_bundle()
            billing = parse_tariff_billing(billing_payload)
            import_rate = parse_tariff_rate(tariff_payload, "purchase")
            export_rate = parse_tariff_rate(tariff_payload, "buyback")
            export_rate = await self._async_export_rate_with_dated_fallback(export_rate)
        except (
            aiohttp.ClientError,
            AttributeError,
            InvalidPayloadError,
            OptionalEndpointUnavailable,
            TimeoutError,
        ) as err:
            if self._has_stale_data():
                coord._note_endpoint_family_failure(TARIFF_ENDPOINT_FAMILY, err)
                return
            raise OptionalEndpointUnavailable("Tariff data is unavailable") from err
        if billing is None and import_rate is None and export_rate is None:
            err = OptionalEndpointUnavailable("Tariff payload did not include data")
            if self._has_stale_data():
                coord._note_endpoint_family_failure(TARIFF_ENDPOINT_FAMILY, err)
                return

        refresh_time = dt_util.utcnow()
        coord.tariff_billing = billing
        coord.tariff_import_rate = import_rate
        coord.tariff_export_rate = export_rate
        coord.tariff_last_refresh_utc = refresh_time
        if isinstance(tariff_payload, dict) and tariff_payload:
            coord.tariff_rates_last_refresh_utc = refresh_time
        coord._note_endpoint_family_success(TARIFF_ENDPOINT_FAMILY)

    async def _async_export_rate_with_dated_fallback(
        self,
        export_rate: TariffRateSnapshot | None,
    ) -> TariffRateSnapshot | None:
        """Fetch dated export rates when the main tariff payload has no rates."""

        if export_rate is None:
            return None
        if tariff_rate_sensor_specs(export_rate):
            return export_rate
        coord = self.coordinator
        site_tariff_rates = getattr(coord.client, "site_tariff_rates", None)
        if not callable(site_tariff_rates):
            return export_rate
        if not coord._endpoint_family_should_run(TARIFF_DATED_RATES_ENDPOINT_FAMILY):
            previous = getattr(coord, "tariff_export_rate", None)
            if tariff_rate_sensor_specs(previous):
                return previous
            return export_rate
        try:
            payload = await site_tariff_rates(
                rate_type="BUYBACK",
                request_date=_site_local_today(coord),
            )
            fallback = parse_dated_tariff_rate(payload, "buyback")
        except (
            aiohttp.ClientError,
            AttributeError,
            InvalidPayloadError,
            OptionalEndpointUnavailable,
            TimeoutError,
        ) as err:
            coord._note_endpoint_family_failure(TARIFF_DATED_RATES_ENDPOINT_FAMILY, err)
            _LOGGER.debug(
                "Dated export tariff rates are unavailable for site %s: %s",
                getattr(coord, "site_id", None),
                err,
            )
            previous = getattr(coord, "tariff_export_rate", None)
            if tariff_rate_sensor_specs(previous):
                return previous
            return export_rate
        if fallback is None:
            coord._note_endpoint_family_failure(
                TARIFF_DATED_RATES_ENDPOINT_FAMILY,
                OptionalEndpointUnavailable(
                    "Dated export tariff rates did not include data"
                ),
            )
            return export_rate
        coord._note_endpoint_family_success(TARIFF_DATED_RATES_ENDPOINT_FAMILY)
        return fallback

    async def async_set_tariff_rate(
        self,
        locator: TariffRateLocator | dict[str, object],
        value: float,
    ) -> dict:
        """Update one existing tariff rate value."""

        result = await self.async_update_tariff(
            rate_updates=({"locator": locator, "rate": value},)
        )
        return result.get("tariff") or {}

    async def async_update_tariff(
        self,
        *,
        billing: TariffBillingUpdate | dict[str, object] | None = None,
        rate_updates: (
            tuple[TariffRateUpdate | dict[str, object], ...]
            | list[TariffRateUpdate | dict[str, object]]
        ) = (),
        tariff_payload: dict[str, object] | None = None,
        purchase_tariff: dict[str, object] | None = None,
        buyback_tariff: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Update tariff billing details and/or existing rate values."""

        billing_update = TariffBillingUpdate.from_object(billing) if billing else None
        payload_update = (
            _validated_tariff_payload(tariff_payload)
            if tariff_payload is not None
            else None
        )
        purchase_update = (
            _validated_tariff_branch(purchase_tariff)
            if purchase_tariff is not None
            else None
        )
        buyback_update = (
            _validated_tariff_branch(buyback_tariff)
            if buyback_tariff is not None
            else None
        )
        parsed_rate_updates: list[TariffRateUpdate] = []
        seen_locators: set[tuple[tuple[str, object], ...]] = set()
        for update in rate_updates:
            parsed_update = TariffRateUpdate.from_object(update)
            if parsed_update is None:
                _raise_tariff_validation(
                    "tariff_rate_target_invalid",
                    message="Tariff rate target is invalid.",
                )
            locator_key = _locator_key(parsed_update.locator)
            if locator_key in seen_locators:
                _raise_tariff_validation(
                    "tariff_rate_target_duplicate",
                    message="Duplicate tariff rate targets are not allowed.",
                )
            seen_locators.add(locator_key)
            parsed_rate_updates.append(parsed_update)

        tariff_write_requested = any(
            (
                parsed_rate_updates,
                payload_update is not None,
                purchase_update is not None,
                buyback_update is not None,
            )
        )
        if billing_update is None and not tariff_write_requested:
            _raise_tariff_validation(
                "tariff_update_required",
                message="Provide billing details or at least one tariff rate update.",
            )

        coord = self.coordinator
        site_tariff = getattr(coord.client, "site_tariff", None)
        site_tariff_update = getattr(coord.client, "site_tariff_update", None)
        needs_current_tariff = payload_update is None and (
            parsed_rate_updates
            or purchase_update is not None
            or buyback_update is not None
        )
        if tariff_write_requested and not callable(site_tariff_update):
            _raise_tariff_validation(
                "tariff_rate_api_unavailable",
                message="Tariff write API is unavailable.",
            )
        if needs_current_tariff and not callable(site_tariff):
            _raise_tariff_validation(
                "tariff_rate_api_unavailable",
                message="Tariff write API is unavailable.",
            )
        site_tariff_billing_update = getattr(
            coord.client, "site_tariff_billing_update", None
        )
        if billing_update is not None and not callable(site_tariff_billing_update):
            _raise_tariff_validation(
                "tariff_billing_api_unavailable",
                message="Tariff billing write API is unavailable.",
            )

        tariff_result: dict | None = None
        billing_result: dict | None = None
        if tariff_write_requested:
            if payload_update is not None:
                update_payload = payload_update
            else:
                payload = await site_tariff()
                if not isinstance(payload, dict):
                    _raise_tariff_validation(
                        "tariff_rate_api_unavailable",
                        message="Tariff write API is unavailable.",
                    )
                update_payload = copy.deepcopy(payload)
            if purchase_update is not None:
                update_payload["purchase"] = purchase_update
            if buyback_update is not None:
                update_payload["buyback"] = buyback_update
            located_updates: list[tuple[dict[str, object], str, str]] = []
            for update in parsed_rate_updates:
                rate = _format_write_rate(update.rate)
                target, field = _locate_tariff_rate(update_payload, update.locator)
                located_updates.append((target, field, rate))
            for target, field, rate in located_updates:
                target[field] = rate
            tariff_result = await site_tariff_update(update_payload)

        if billing_update is not None:
            billing_result = await site_tariff_billing_update(billing_update.payload)

        notifier = getattr(coord.client, "notify_tariff_change", None)
        if callable(notifier):
            try:
                await notifier()
            except (aiohttp.ClientError, AttributeError, TimeoutError) as err:
                _LOGGER.debug(
                    "Tariff change notification failed for site %s: %s",
                    getattr(coord, "site_id", None),
                    err,
                )
        await coord.tariff_runtime.async_refresh(force=True)
        return {"tariff": tariff_result, "billing": billing_result}

    def _has_stale_data(self) -> bool:
        """Return whether a prior tariff snapshot can stay visible."""

        coord = self.coordinator
        return (
            getattr(coord, "tariff_billing", None) is not None
            or getattr(coord, "tariff_import_rate", None) is not None
            or getattr(coord, "tariff_export_rate", None) is not None
        )

    def diagnostics(self) -> dict[str, object]:
        """Return tariff refresh diagnostics without raw tariff payloads."""

        coord = self.coordinator
        last_refresh_utc = getattr(coord, "tariff_last_refresh_utc", None)
        rates_last_refresh_utc = getattr(coord, "tariff_rates_last_refresh_utc", None)
        return {
            "billing_available": getattr(coord, "tariff_billing", None) is not None,
            "import_rate_available": (
                getattr(coord, "tariff_import_rate", None) is not None
            ),
            "export_rate_available": (
                getattr(coord, "tariff_export_rate", None) is not None
            ),
            "last_refresh_utc": (
                last_refresh_utc.isoformat()
                if hasattr(last_refresh_utc, "isoformat")
                else None
            ),
            "rates_last_refresh_utc": (
                rates_last_refresh_utc.isoformat()
                if hasattr(rates_last_refresh_utc, "isoformat")
                else None
            ),
            "endpoint_family": _endpoint_family_diagnostics(
                coord, TARIFF_ENDPOINT_FAMILY
            ),
            "dated_rates_endpoint_family": _endpoint_family_diagnostics(
                coord, TARIFF_DATED_RATES_ENDPOINT_FAMILY
            ),
        }
