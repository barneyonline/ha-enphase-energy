from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import aiohttp
import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.enphase_ev import coordinator as coord_mod
from custom_components.enphase_ev import sensor_tariff as sensor_mod
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from custom_components.enphase_ev import tariff as tariff_mod
from custom_components.enphase_ev.api import OptionalEndpointUnavailable
from custom_components.enphase_ev.const import DOMAIN
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from custom_components.enphase_ev.sensor import (
    EnphaseCurrentTariffRateSensor,
    EnphaseTariffBillingSensor,
    EnphaseTariffExportRateValueSensor,
    EnphaseTariffRateSensor,
    EnphaseTariffRateValueSensor,
    async_setup_entry,
)
from custom_components.enphase_ev.tariff import (
    TARIFF_DATED_RATES_ENDPOINT_FAMILY,
    TARIFF_ENDPOINT_FAMILY,
    TARIFF_SUCCESS_TTL_S,
    TariffBillingUpdate,
    TariffRateLocator,
    TariffRateSnapshot,
    TariffRateUpdate,
    TariffRuntime,
    _clean_text,
    _format_rate,
    _locate_tariff_rate,
    current_tariff_rate_sensor_spec,
    export_rate_sensor_specs,
    next_billing_date,
    next_tariff_rate_change,
    parse_tariff_billing,
    parse_dated_tariff_rate,
    parse_tariff_rate,
    tariff_rate_sensor_specs,
)


def test_parse_tariff_billing_monthly_and_day_based() -> None:
    monthly = parse_tariff_billing(
        {
            "anyBillPeriodStartDate": "2025-08-18",
            "billingFrequency": "MONTH",
            "billingIntervalValue": 1,
        }
    )
    assert monthly is not None
    assert monthly.state == "Monthly"
    assert monthly.attributes == {
        "start_date": "2025-08-18",
        "billing_frequency": "MONTH",
        "billing_interval_value": 1,
        "billing_cycle": "Monthly",
    }
    assert next_billing_date(monthly, today=date(2026, 4, 26)) == date(2026, 5, 18)

    day_based = parse_tariff_billing(
        {
            "anyBillPeriodStartDate": "2025-08-17",
            "billingFrequency": "DAY",
            "billingIntervalValue": 30,
        }
    )
    assert day_based is not None
    assert day_based.state == "Every 30 days"
    assert next_billing_date(day_based, today=date(2025, 9, 16)) == date(2025, 10, 16)

    two_month = parse_tariff_billing(
        {"billingFrequency": "MONTH", "billingIntervalValue": 2}
    )
    assert two_month is not None
    assert two_month.state == "Every 2 months"

    daily = parse_tariff_billing({"billingFrequency": "DAY", "billingIntervalValue": 1})
    assert daily is not None
    assert daily.state == "Daily"

    custom = parse_tariff_billing({"billingFrequency": "WEEK"})
    assert custom is not None
    assert custom.state == "WEEK"
    assert next_billing_date(custom, today=date(2026, 4, 26)) is None
    custom_with_start = parse_tariff_billing(
        {"anyBillPeriodStartDate": "2026-04-01", "billingFrequency": "WEEK"}
    )
    assert custom_with_start is not None
    assert next_billing_date(custom_with_start, today=date(2026, 4, 26)) is None


def test_next_billing_date_month_end_and_invalid_values() -> None:
    month_end = parse_tariff_billing(
        {
            "anyBillPeriodStartDate": "2025-01-31",
            "billingFrequency": "MONTH",
            "billingIntervalValue": 1,
        }
    )
    assert month_end is not None
    assert next_billing_date(month_end, today=date(2025, 2, 27)) == date(2025, 2, 28)
    assert next_billing_date(month_end, today=date(2025, 2, 28)) == date(2025, 3, 31)

    invalid_date = parse_tariff_billing(
        {
            "anyBillPeriodStartDate": "not-a-date",
            "billingFrequency": "MONTH",
            "billingIntervalValue": 1,
        }
    )
    assert invalid_date is not None
    assert next_billing_date(invalid_date, today=date(2026, 4, 26)) is None

    invalid_interval = parse_tariff_billing(
        {
            "anyBillPeriodStartDate": "2026-04-01",
            "billingFrequency": "DAY",
            "billingIntervalValue": 0,
        }
    )
    assert invalid_interval is not None
    assert next_billing_date(invalid_interval, today=date(2026, 4, 26)) is None


def test_tariff_update_parsers_cover_passthrough_and_invalid_inputs() -> None:
    billing = TariffBillingUpdate(
        start_date="2026-04-01",
        billing_frequency="MONTH",
        billing_interval_value=1,
    )
    assert TariffBillingUpdate.from_object(billing) is billing
    assert TariffBillingUpdate.from_object("bad") is None
    with pytest.raises(ServiceValidationError) as err:
        TariffBillingUpdate.from_object(
            {"billing_frequency": "MONTH", "billing_interval_value": 1}
        )
    assert err.value.translation_key == "tariff_billing_start_date_invalid"

    locator = TariffRateLocator(
        branch="purchase",
        kind="off_peak",
        season_index=1,
        season_id="default",
    )
    update = TariffRateUpdate(locator=locator, rate=0.1)
    assert TariffRateUpdate.from_object(update) is update
    assert TariffRateUpdate.from_object((locator, 0.2)) == TariffRateUpdate(
        locator=locator,
        rate=0.2,
    )
    assert TariffRateUpdate.from_object("bad") is None


def test_parse_tariff_rate_flat_tou_and_tiered_shapes() -> None:
    payload = {
        "currency": "$",
        "purchase": {
            "typeKind": "seasonal-and-weekends",
            "typeId": "tou",
            "source": "manual",
            "seasons": [
                {
                    "id": "summer",
                    "startMonth": "12",
                    "endMonth": "5",
                    "days": [
                        {
                            "id": "weekdays",
                            "days": [1, 2, 3, 4, 5],
                            "periods": [
                                {
                                    "id": "peak-1",
                                    "rate": "0.31",
                                    "type": "peak",
                                    "startTime": "840",
                                    "endTime": "1260",
                                }
                            ],
                        }
                    ],
                }
            ],
        },
        "buyback": {
            "typeKind": "single",
            "typeId": "tiered",
            "source": "netFit",
            "exportPlan": "netFit",
            "seasons": [
                {
                    "id": "default",
                    "startMonth": "1",
                    "endMonth": "12",
                    "offPeak": "0.04",
                    "tiers": [
                        {
                            "id": "tier-1",
                            "rate": "0.06",
                            "startValue": "10",
                            "endValue": "20",
                        },
                        {
                            "id": "tier-2",
                            "rate": "0.10",
                            "startValue": "20",
                            "endValue": -1,
                        },
                    ],
                }
            ],
        },
    }

    purchase = parse_tariff_rate(payload, "purchase")
    assert purchase is not None
    assert purchase.state == "Time of use"
    assert purchase.variation_type == "Seasonal weekdays and weekends"
    assert purchase.seasons[0]["days"][0]["periods"][0]["start_time"] == "14:00"
    assert purchase.seasons[0]["days"][0]["periods"][0]["end_time"] == "21:00"

    buyback = parse_tariff_rate(payload, "buyback")
    assert buyback is not None
    assert buyback.state == "Tiered"
    assert buyback.export_plan == "netFit"
    assert buyback.attributes["export_plan"] == "netFit"
    assert buyback.seasons[0]["off_peak"] == "0.04"
    assert buyback.seasons[0]["tiers"][1]["unbounded"] is True


def test_parse_dated_tariff_rate_builds_read_only_buyback_snapshot() -> None:
    snapshot = parse_dated_tariff_rate(
        {
            "type": "tariff-rates",
            "data": {
                "tariff-version-id": "abc123",
                "tariff-type": "static",
                "currency": "$",
                "siteDetails": {"exportPlanType": "NEM3"},
                "buyback": [
                    {"start": 0, "end": 59, "rate": 0.0789},
                    {"start": 60, "end": 119, "rate": "0.0733"},
                    {"start": 1380, "end": 1439, "rate": 0.0764},
                ],
            },
        },
        "buyback",
    )

    assert snapshot is not None
    assert snapshot.state == "Time of use"
    assert snapshot.source == "static"
    assert snapshot.export_plan == "NEM3"
    assert snapshot.branch_key is None
    specs = tariff_rate_sensor_specs(snapshot)
    assert len(specs) == 3
    assert specs[0]["state"] == 0.0789
    assert specs[0]["attributes"]["start_time"] == "00:00"
    assert specs[0]["attributes"]["end_time"] == "01:00"
    assert specs[2]["attributes"]["start_time"] == "23:00"
    assert specs[2]["attributes"]["end_time"] == "00:00"
    assert "tariff_locator" not in specs[0]["attributes"]
    current = current_tariff_rate_sensor_spec(
        snapshot,
        datetime(2026, 5, 1, 1, 30, tzinfo=timezone.utc),
    )
    assert current is not None
    assert current["state"] == 0.0733


def test_parse_dated_tariff_rate_rejects_bad_payloads() -> None:
    assert parse_dated_tariff_rate({}, "buyback") is None
    assert parse_dated_tariff_rate({"data": {"buyback": {}}}, "buyback") is None
    assert parse_dated_tariff_rate({"data": {"buyback": []}}, "buyback") is None
    assert (
        parse_dated_tariff_rate({"data": {"buyback": [{"rate": "bad"}]}}, "buyback")
        is None
    )
    flat = parse_dated_tariff_rate(
        {
            "data": {
                "siteDetails": "bad",
                "currency": "USD",
                "buyback": [{"rate": "0.01"}],
            }
        },
        "buyback",
    )
    assert flat is not None
    assert flat.state == "Flat"
    assert flat.currency == "USD"
    assert (
        parse_dated_tariff_rate({"data": {"buyback": [{"rate": "0.01"}]}}, "bad")
        is None
    )


def test_tariff_rate_sensor_specs_for_tou_and_tiered_rates() -> None:
    import_tou = parse_tariff_rate(
        {
            "currency": "$",
            "purchase": {
                "typeKind": "single",
                "typeId": "tou",
                "source": "manual",
                "seasons": [
                    {
                        "id": "default",
                        "days": [
                            {
                                "id": "week",
                                "periods": [
                                    {
                                        "id": "off-peak",
                                        "type": "off-peak",
                                        "rate": "0.18",
                                    },
                                    {"id": "peak-1", "type": "peak", "rate": "0.31"},
                                ],
                            }
                        ],
                    }
                ],
            },
        },
        "purchase",
    )

    import_specs = tariff_rate_sensor_specs(import_tou)

    assert [spec["name"] for spec in import_specs] == ["Off-Peak", "Peak"]
    assert [spec["state"] for spec in import_specs] == [0.18, 0.31]
    assert [spec["unit"] for spec in import_specs] == ["$/kWh", "$/kWh"]
    assert import_specs[0]["attributes"]["formatted_rate"] == "$0.18"
    assert import_specs[0]["attributes"]["source"] == "manual"
    assert import_specs[0]["attributes"]["tariff_locator"] == {
        "branch": "purchase",
        "kind": "period",
        "season_index": 1,
        "season_id": "default",
        "day_index": 1,
        "day_group_id": "week",
        "period_index": 1,
        "period_id": "off-peak",
        "period_type": "off-peak",
    }

    export_tou = parse_tariff_rate(
        {
            "currency": "$",
            "buyback": {
                "typeKind": "single",
                "typeId": "tou",
                "source": "netFit",
                "exportPlan": "netFit",
                "seasons": [
                    {
                        "id": "default",
                        "startMonth": "1",
                        "endMonth": "12",
                        "days": [
                            {
                                "id": "week",
                                "days": [1, 2, 3, 4, 5, 6, 7],
                                "periods": [
                                    {
                                        "id": "off-peak",
                                        "type": "off-peak",
                                        "rate": "0.02",
                                    },
                                    {
                                        "id": "peak-1",
                                        "type": "peak",
                                        "rate": "0.06",
                                        "startTime": "960",
                                        "endTime": "1320",
                                    },
                                ],
                            }
                        ],
                    }
                ],
            },
        },
        "buyback",
    )

    specs = export_rate_sensor_specs(export_tou)

    assert [spec["name"] for spec in specs] == ["Off-Peak", "Peak"]
    assert [spec["state"] for spec in specs] == [0.02, 0.06]
    assert [spec["unit"] for spec in specs] == ["$/kWh", "$/kWh"]
    assert specs[0]["key"] == "default_week_off_peak"
    assert specs[1]["attributes"]["rate_structure"] == "Time of use"
    assert specs[1]["attributes"]["period_type"] == "peak"
    assert specs[1]["attributes"]["start_time"] == "16:00"
    assert specs[1]["attributes"]["end_time"] == "22:00"
    assert specs[1]["attributes"]["export_plan"] == "netFit"

    tiered = parse_tariff_rate(
        {
            "currency": "AUD",
            "buyback": {
                "typeKind": "single",
                "typeId": "tiered",
                "seasons": [
                    {
                        "id": "default",
                        "tiers": [
                            {"id": "tier-1", "rate": "0.04", "startValue": "0"},
                            {"id": "tier-2", "rate": "0.10", "endValue": -1},
                        ],
                    }
                ],
            },
        },
        "buyback",
    )

    tier_specs = export_rate_sensor_specs(tiered)

    assert [spec["state"] for spec in tier_specs] == [0.04, 0.10]
    assert [spec["unit"] for spec in tier_specs] == ["AUD/kWh", "AUD/kWh"]
    assert tier_specs[1]["attributes"]["unbounded"] is True

    tiered_with_off_peak = parse_tariff_rate(
        {
            "currency": "$",
            "purchase": {
                "typeKind": "single",
                "typeId": "tiered",
                "seasons": [
                    {
                        "id": "default",
                        "offPeak": "0.03",
                        "tiers": [{"id": "tier-1", "rate": "0.06"}],
                    }
                ],
            },
        },
        "purchase",
    )

    off_peak_specs = tariff_rate_sensor_specs(tiered_with_off_peak)

    assert [spec["key"] for spec in off_peak_specs] == [
        "default_off_peak",
        "default_tier_1",
    ]
    assert [spec["state"] for spec in off_peak_specs] == [0.03, 0.06]
    assert off_peak_specs[0]["attributes"]["tariff_locator"] == {
        "branch": "purchase",
        "kind": "off_peak",
        "season_index": 1,
        "season_id": "default",
    }


def test_export_rate_sensor_specs_handles_sparse_and_duplicate_rows() -> None:
    assert export_rate_sensor_specs(None) == ()
    sparse = parse_tariff_rate(
        {
            "buyback": {
                "typeId": "tou",
                "seasons": [
                    {
                        "days": [
                            "bad",
                            {
                                "periods": [
                                    "bad",
                                    {"id": "", "rate": None},
                                    {"id": "text", "rate": "not-numeric"},
                                    {"id": "", "rate": "0.01"},
                                    {"id": "", "rate": "0.02"},
                                ]
                            },
                        ],
                        "tiers": [
                            "bad",
                            {"id": "", "rate": None},
                            {"id": "", "rate": "0.03"},
                        ],
                    }
                ],
            }
        },
        "buyback",
    )

    specs = export_rate_sensor_specs(sparse)

    assert [spec["key"] for spec in specs] == [
        "season_1_days_1_period_3",
        "season_1_days_1_period_4",
        "season_1_tier_2",
    ]
    assert [spec["name"] for spec in specs] == ["Period 3", "Period 4", "Tier 2"]
    assert [spec["state"] for spec in specs] == [0.01, 0.02, 0.03]
    assert [spec["unit"] for spec in specs] == [None, None, None]

    duplicate_keys = parse_tariff_rate(
        {
            "buyback": {
                "typeId": "tou",
                "seasons": [
                    {
                        "id": "default",
                        "days": [
                            {
                                "id": "week",
                                "periods": [
                                    {"type": "peak", "rate": "0.01"},
                                    {"type": "peak", "rate": "0.02"},
                                    {"type": "free"},
                                ],
                            }
                        ],
                    }
                ],
            }
        },
        "buyback",
    )

    duplicate_specs = export_rate_sensor_specs(duplicate_keys)

    assert [spec["key"] for spec in duplicate_specs] == [
        "default_week_peak",
        "default_week_peak_2",
    ]

    assert (
        export_rate_sensor_specs(
            TariffRateSnapshot(
                state="Time of use",
                rate_structure="Time of use",
                variation_type=None,
                source=None,
                currency=None,
                export_plan=None,
                seasons=(
                    {
                        "days": ["bad", {"periods": ["bad", {"rate": "0.04"}]}],
                        "tiers": ["bad", {"id": "free"}],
                    },
                ),
            )
        )[0]["state"]
        == 0.04
    )


def test_current_tariff_rate_sensor_spec_selects_active_period() -> None:
    snapshot = parse_tariff_rate(
        {
            "currency": "$",
            "purchase": {
                "typeKind": "seasonal-and-weekends",
                "typeId": "tou",
                "source": "manual",
                "seasons": [
                    {
                        "id": "summer",
                        "startMonth": "12",
                        "endMonth": "2",
                        "days": [
                            {
                                "id": "weekday",
                                "days": [1, 2, 3, 4, 5],
                                "periods": [
                                    {
                                        "type": "off-peak",
                                        "rate": "0.18",
                                        "startTime": "1320",
                                        "endTime": "420",
                                    },
                                    {
                                        "type": "peak",
                                        "rate": "0.42",
                                        "startTime": "420",
                                        "endTime": "1320",
                                    },
                                ],
                            },
                            {
                                "id": "weekend",
                                "days": [6, 7],
                                "periods": [{"type": "shoulder", "rate": "0.24"}],
                            },
                        ],
                    },
                    {
                        "id": "winter",
                        "startMonth": "3",
                        "endMonth": "11",
                        "days": [{"id": "all", "periods": [{"rate": "0.30"}]}],
                    },
                ],
            },
        },
        "purchase",
    )

    peak = current_tariff_rate_sensor_spec(
        snapshot,
        datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc),
    )
    off_peak = current_tariff_rate_sensor_spec(
        snapshot,
        datetime(2026, 1, 5, 23, 30, tzinfo=timezone.utc),
    )
    weekend = current_tariff_rate_sensor_spec(
        snapshot,
        datetime(2026, 1, 10, 9, 0, tzinfo=timezone.utc),
    )
    winter = current_tariff_rate_sensor_spec(
        snapshot,
        datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc),
    )

    assert peak is not None
    assert peak["name"] == "Peak"
    assert peak["state"] == 0.42
    assert off_peak is not None
    assert off_peak["name"] == "Off-Peak"
    assert weekend is not None
    assert weekend["name"] == "Shoulder"
    assert winter is not None
    assert winter["state"] == 0.30


def test_current_tariff_rate_sensor_spec_prefers_timed_period_over_fallback() -> None:
    snapshot = parse_tariff_rate(
        {
            "currency": "$",
            "purchase": {
                "typeKind": "single",
                "typeId": "tou",
                "source": "manual",
                "seasons": [
                    {
                        "id": "default",
                        "startMonth": "1",
                        "endMonth": "12",
                        "days": [
                            {
                                "id": "week",
                                "days": [1, 2, 3, 4, 5, 6, 7],
                                "periods": [
                                    {
                                        "id": "off-peak",
                                        "type": "off-peak",
                                        "rate": "0.18",
                                        "startTime": "",
                                        "endTime": "",
                                    },
                                    {
                                        "id": "peak-1",
                                        "type": "peak",
                                        "rate": "0.31",
                                        "startTime": "840",
                                        "endTime": "1260",
                                    },
                                ],
                            }
                        ],
                    }
                ],
            },
        },
        "purchase",
    )

    peak = current_tariff_rate_sensor_spec(
        snapshot,
        datetime(2026, 5, 1, 15, 12, tzinfo=timezone.utc),
    )
    off_peak = current_tariff_rate_sensor_spec(
        snapshot,
        datetime(2026, 5, 1, 21, 30, tzinfo=timezone.utc),
    )

    assert peak is not None
    assert peak["name"] == "Peak"
    assert peak["state"] == 0.31
    assert off_peak is not None
    assert off_peak["name"] == "Off-Peak"
    assert off_peak["state"] == 0.18


def test_current_tariff_rate_sensor_spec_rejects_overlapping_timed_periods() -> None:
    snapshot = parse_tariff_rate(
        {
            "currency": "$",
            "purchase": {
                "typeKind": "single",
                "typeId": "tou",
                "seasons": [
                    {
                        "id": "default",
                        "days": [
                            {
                                "id": "week",
                                "periods": [
                                    {
                                        "type": "peak",
                                        "rate": "0.31",
                                        "startTime": "840",
                                        "endTime": "1260",
                                    },
                                    {
                                        "type": "shoulder",
                                        "rate": "0.25",
                                        "startTime": "900",
                                        "endTime": "1080",
                                    },
                                ],
                            }
                        ],
                    }
                ],
            },
        },
        "purchase",
    )

    assert (
        current_tariff_rate_sensor_spec(
            snapshot,
            datetime(2026, 5, 1, 15, 30, tzinfo=timezone.utc),
        )
        is None
    )


def test_current_tariff_rate_sensor_spec_rejects_cross_season_fallback() -> None:
    snapshot = parse_tariff_rate(
        {
            "currency": "$",
            "purchase": {
                "typeKind": "seasonal",
                "typeId": "tou",
                "seasons": [
                    {
                        "id": "first",
                        "startMonth": "1",
                        "endMonth": "12",
                        "days": [
                            {
                                "id": "week",
                                "periods": [
                                    {
                                        "type": "off-peak",
                                        "rate": "0.18",
                                        "startTime": "",
                                        "endTime": "",
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "id": "second",
                        "startMonth": "1",
                        "endMonth": "12",
                        "days": [
                            {
                                "id": "week",
                                "periods": [
                                    {
                                        "type": "peak",
                                        "rate": "0.31",
                                        "startTime": "840",
                                        "endTime": "1260",
                                    }
                                ],
                            }
                        ],
                    },
                ],
            },
        },
        "purchase",
    )

    assert (
        current_tariff_rate_sensor_spec(
            snapshot,
            datetime(2026, 5, 1, 15, 30, tzinfo=timezone.utc),
        )
        is None
    )


def test_current_tariff_rate_sensor_spec_rejects_cross_day_group_fallback() -> None:
    snapshot = parse_tariff_rate(
        {
            "currency": "$",
            "purchase": {
                "typeKind": "weekends",
                "typeId": "tou",
                "seasons": [
                    {
                        "id": "default",
                        "days": [
                            {
                                "id": "all",
                                "days": [1, 2, 3, 4, 5, 6, 7],
                                "periods": [
                                    {
                                        "type": "off-peak",
                                        "rate": "0.18",
                                        "startTime": "",
                                        "endTime": "",
                                    }
                                ],
                            },
                            {
                                "id": "week",
                                "days": [1, 2, 3, 4, 5],
                                "periods": [
                                    {
                                        "type": "peak",
                                        "rate": "0.31",
                                        "startTime": "840",
                                        "endTime": "1260",
                                    }
                                ],
                            },
                        ],
                    }
                ],
            },
        },
        "purchase",
    )

    assert (
        current_tariff_rate_sensor_spec(
            snapshot,
            datetime(2026, 5, 1, 15, 30, tzinfo=timezone.utc),
        )
        is None
    )


def test_next_tariff_rate_change_finds_time_and_season_boundaries() -> None:
    snapshot = parse_tariff_rate(
        {
            "currency": "$",
            "purchase": {
                "typeKind": "seasonal-and-weekends",
                "typeId": "tou",
                "source": "manual",
                "seasons": [
                    {
                        "id": "summer",
                        "startMonth": "12",
                        "endMonth": "2",
                        "days": [
                            {
                                "id": "weekday",
                                "days": [1, 2, 3, 4, 5],
                                "periods": [
                                    {
                                        "type": "off-peak",
                                        "rate": "0.18",
                                        "startTime": "1320",
                                        "endTime": "420",
                                    },
                                    {
                                        "type": "peak",
                                        "rate": "0.42",
                                        "startTime": "420",
                                        "endTime": "1320",
                                    },
                                ],
                            }
                        ],
                    },
                    {
                        "id": "winter",
                        "startMonth": "3",
                        "endMonth": "11",
                        "days": [{"id": "all", "periods": [{"rate": "0.30"}]}],
                    },
                ],
            },
        },
        "purchase",
    )

    assert next_tariff_rate_change(
        snapshot,
        datetime(2026, 1, 5, 6, 59, tzinfo=timezone.utc),
    ) == datetime(2026, 1, 5, 7, 0, tzinfo=timezone.utc)
    assert next_tariff_rate_change(
        snapshot,
        datetime(2026, 1, 5, 21, 59, tzinfo=timezone.utc),
    ) == datetime(2026, 1, 5, 22, 0, tzinfo=timezone.utc)
    assert next_tariff_rate_change(
        snapshot,
        datetime(2026, 2, 28, 23, 30, tzinfo=timezone.utc),
    ) == datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)


def test_next_tariff_rate_change_handles_missing_naive_and_unchanged_rates() -> None:
    snapshot = parse_tariff_rate(
        {
            "currency": "$",
            "purchase": {
                "typeKind": "single",
                "typeId": "tou",
                "source": "manual",
                "seasons": [
                    {
                        "id": "default",
                        "days": [
                            {
                                "id": "all",
                                "periods": [{"type": "flat", "rate": "0.20"}],
                            }
                        ],
                    }
                ],
            },
        },
        "purchase",
    )

    assert next_tariff_rate_change(None, datetime(2026, 1, 1, 12, 0)) is None
    assert (
        next_tariff_rate_change(
            snapshot,
            datetime(2026, 1, 1, 12, 0),
        )
        is None
    )


def test_current_tariff_rate_sensor_spec_rejects_ambiguous_tiers() -> None:
    snapshot = parse_tariff_rate(
        {
            "currency": "$",
            "purchase": {
                "typeKind": "single",
                "typeId": "tiered",
                "seasons": [
                    {
                        "id": "default",
                        "tiers": [
                            {"id": "tier-1", "rate": "0.10"},
                            {"id": "tier-2", "rate": "0.20"},
                        ],
                    }
                ],
            },
        },
        "purchase",
    )

    assert (
        current_tariff_rate_sensor_spec(
            snapshot,
            datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc),
        )
        is None
    )


def test_current_tariff_rate_sensor_spec_handles_invalid_metadata() -> None:
    invalid_month = TariffRateSnapshot(
        state="Time of use",
        rate_structure="Time of use",
        variation_type=None,
        source=None,
        currency="$",
        export_plan=None,
        seasons=(
            {
                "start_month": 13,
                "end_month": 14,
                "days": [{"periods": [{"rate": "0.10"}]}],
            },
        ),
    )
    missing_separator = TariffRateSnapshot(
        state="Time of use",
        rate_structure="Time of use",
        variation_type=None,
        source=None,
        currency="$",
        export_plan=None,
        seasons=({"days": [{"periods": [{"rate": "0.11", "start_time": "bad"}]}]},),
    )
    invalid_numbers = TariffRateSnapshot(
        state="Time of use",
        rate_structure="Time of use",
        variation_type=None,
        source=None,
        currency="$",
        export_plan=None,
        seasons=({"days": [{"periods": [{"rate": "0.12", "start_time": "aa:00"}]}]},),
    )
    out_of_range = TariffRateSnapshot(
        state="Time of use",
        rate_structure="Time of use",
        variation_type=None,
        source=None,
        currency="$",
        export_plan=None,
        seasons=({"days": [{"periods": [{"rate": "0.13", "start_time": "24:00"}]}]},),
    )

    when = datetime(2026, 1, 5, 9, 0)

    assert current_tariff_rate_sensor_spec(invalid_month, when)["state"] == 0.10
    assert current_tariff_rate_sensor_spec(missing_separator, when)["state"] == 0.11
    assert current_tariff_rate_sensor_spec(invalid_numbers, when)["state"] == 0.12
    assert current_tariff_rate_sensor_spec(out_of_range, when)["state"] == 0.13


def test_parse_tariff_rate_rejects_empty_or_bad_branches() -> None:
    class BadString:
        def __str__(self) -> str:
            raise RuntimeError("bad")

    assert _clean_text(BadString()) is None
    assert _format_rate(None, "$") is None
    assert parse_tariff_rate({}, "purchase") is None
    assert parse_tariff_rate("bad", "purchase") is None
    assert parse_tariff_rate({"purchase": {"seasons": []}}, "purchase") is None
    assert parse_tariff_billing("bad") is None
    assert parse_tariff_billing({}) is None
    bad_interval = parse_tariff_billing(
        {"billingFrequency": "WEEK", "billingIntervalValue": "bad"}
    )
    assert bad_interval is not None
    assert bad_interval.billing_interval_value is None
    assert parse_tariff_rate(
        {
            "purchase": {
                "typeId": "custom",
                "typeKind": "custom-kind",
                "seasons": [
                    "bad",
                    {
                        "days": ["bad", {"periods": ["bad"]}],
                        "tiers": ["bad", {"endValue": "bad"}],
                    },
                ],
            }
        },
        "purchase",
    ).attributes["seasons"] == [{"days": [{}], "tiers": [{"end_value": "bad"}]}]


def test_tariff_rate_locator_rejects_invalid_objects() -> None:
    assert TariffRateLocator.from_object(None) is None
    assert TariffRateLocator.from_object({"branch": "bad", "kind": "period"}) is None
    assert (
        TariffRateLocator.from_object(
            {"branch": "purchase", "kind": "period", "season_index": 0}
        )
        is None
    )


def test_locate_tariff_rate_covers_guard_paths() -> None:
    with pytest.raises(ServiceValidationError):
        _locate_tariff_rate(
            {},
            TariffRateLocator(branch="purchase", kind="period", season_index=1),
        )
    with pytest.raises(ServiceValidationError):
        _locate_tariff_rate(
            {"purchase": {"seasons": "bad"}},
            TariffRateLocator(branch="purchase", kind="period", season_index=1),
        )
    with pytest.raises(ServiceValidationError):
        _locate_tariff_rate(
            {"purchase": {"seasons": [{"id": "default"}]}},
            TariffRateLocator(
                branch="purchase",
                kind="off_peak",
                season_index=1,
                season_id="default",
            ),
        )
    with pytest.raises(ServiceValidationError):
        _locate_tariff_rate(
            {"purchase": {"seasons": [{"id": "default", "days": []}]}},
            TariffRateLocator(
                branch="purchase",
                kind="period",
                season_index=1,
                season_id="default",
                day_index=1,
            ),
        )
    with pytest.raises(ServiceValidationError):
        _locate_tariff_rate(
            {
                "purchase": {
                    "seasons": [
                        {
                            "id": "default",
                            "days": [{"id": "week", "periods": []}],
                        }
                    ]
                }
            },
            TariffRateLocator(
                branch="purchase",
                kind="period",
                season_index=1,
                season_id="default",
                day_index=1,
                day_group_id="week",
                period_index=1,
            ),
        )
    tier, field = _locate_tariff_rate(
        {
            "purchase": {
                "seasons": [
                    {"id": "default", "tiers": [{"id": "tier-1", "rate": "0.1"}]}
                ]
            }
        },
        TariffRateLocator(
            branch="purchase",
            kind="tier",
            season_index=1,
            season_id="default",
            tier_index=1,
            tier_id="tier-1",
        ),
    )
    assert (tier, field) == ({"id": "tier-1", "rate": "0.1"}, "rate")
    with pytest.raises(ServiceValidationError):
        _locate_tariff_rate(
            {"purchase": {"seasons": [{"id": "default", "tiers": []}]}},
            TariffRateLocator(
                branch="purchase",
                kind="tier",
                season_index=1,
                season_id="default",
                tier_index=1,
            ),
        )
    with pytest.raises(ServiceValidationError):
        _locate_tariff_rate(
            {"purchase": {"seasons": [{"id": "default"}]}},
            TariffRateLocator(branch="purchase", kind="bad", season_index=1),
        )


@pytest.mark.asyncio
async def test_tariff_runtime_refreshes_snapshots(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.client.site_tariff_bundle = AsyncMock(
        return_value=(
            {
                "anyBillPeriodStartDate": "2025-08-18",
                "billingFrequency": "MONTH",
                "billingIntervalValue": 1,
            },
            {
                "currency": "$",
                "purchase": {
                    "typeKind": "single",
                    "typeId": "flat",
                    "source": "manual",
                    "seasons": [
                        {
                            "id": "default",
                            "startMonth": "1",
                            "endMonth": "12",
                            "days": [
                                {
                                    "id": "week",
                                    "days": [1, 2, 3, 4, 5, 6, 7],
                                    "periods": [{"rate": "0.03"}],
                                }
                            ],
                        }
                    ],
                },
                "buyback": {
                    "typeKind": "single",
                    "typeId": "flat",
                    "source": "grossFit",
                    "exportPlan": "grossFit",
                    "seasons": [
                        {
                            "id": "default",
                            "startMonth": "1",
                            "endMonth": "12",
                            "days": [
                                {
                                    "id": "week",
                                    "days": [1, 2, 3, 4, 5, 6, 7],
                                    "periods": [{"rate": "0.01"}],
                                }
                            ],
                        }
                    ],
                },
            },
        )
    )

    await TariffRuntime(coord).async_refresh()

    assert coord.tariff_billing.state == "Monthly"
    assert coord.tariff_import_rate.state == "Flat"
    assert coord.tariff_export_rate.export_plan == "grossFit"
    assert coord.tariff_last_refresh_utc is not None
    assert coord.tariff_rates_last_refresh_utc is coord.tariff_last_refresh_utc
    assert coord._endpoint_family_state("tariff").support_state == "supported"


@pytest.mark.asyncio
async def test_tariff_runtime_uses_dated_buyback_when_export_seasons_empty(
    coordinator_factory,
    monkeypatch,
) -> None:
    fixed_now = datetime(2026, 5, 1, 16, 25, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "custom_components.enphase_ev.tariff.dt_util.now",
        lambda tz=None: fixed_now.astimezone(tz) if tz is not None else fixed_now,
    )
    coord = coordinator_factory()
    coord._site_timezone_name = lambda: "UTC"  # noqa: SLF001
    coord.client.site_tariff_bundle = AsyncMock(
        return_value=(
            {
                "anyBillPeriodStartDate": "2025-08-18",
                "billingFrequency": "MONTH",
                "billingIntervalValue": 1,
            },
            {
                "currency": "$",
                "purchase": {
                    "typeKind": "single",
                    "typeId": "flat",
                    "source": "manual",
                    "seasons": [
                        {
                            "id": "default",
                            "days": [{"id": "all", "periods": [{"rate": "0.30"}]}],
                        }
                    ],
                },
                "buyback": {
                    "typeKind": "single",
                    "typeId": "tou",
                    "source": "autofill",
                    "exportPlan": "nem",
                    "seasons": [],
                },
            },
        )
    )
    coord.client.site_tariff_rates = AsyncMock(
        return_value={
            "type": "tariff-rates",
            "data": {
                "tariff-version-id": "abc123",
                "tariff-type": "static",
                "currency": "$",
                "siteDetails": {"exportPlanType": "NEM3"},
                "buyback": [
                    {"start": 0, "end": 959, "rate": 0.0402},
                    {"start": 960, "end": 1019, "rate": 0.0426},
                    {"start": 1020, "end": 1439, "rate": 0.0567},
                ],
            },
        }
    )

    await TariffRuntime(coord).async_refresh()

    coord.client.site_tariff_rates.assert_awaited_once_with(
        rate_type="BUYBACK",
        request_date=date(2026, 5, 1),
    )
    assert coord.tariff_export_rate is not None
    assert coord.tariff_export_rate.state == "Time of use"
    assert coord.tariff_export_rate.export_plan == "NEM3"
    assert (
        coord._endpoint_family_state(TARIFF_DATED_RATES_ENDPOINT_FAMILY).support_state
        == "supported"
    )
    current = current_tariff_rate_sensor_spec(
        coord.tariff_export_rate,
        fixed_now,
    )
    assert current is not None
    assert current["state"] == 0.0426
    assert "tariff_locator" not in current["attributes"]


@pytest.mark.asyncio
async def test_tariff_runtime_skips_dated_buyback_when_export_has_specs(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.site_tariff_bundle = AsyncMock(
        return_value=(
            {},
            {
                "currency": "$",
                "buyback": {
                    "typeKind": "single",
                    "typeId": "flat",
                    "seasons": [
                        {
                            "id": "default",
                            "days": [{"id": "all", "periods": [{"rate": "0.01"}]}],
                        }
                    ],
                },
            },
        )
    )
    coord.client.site_tariff_rates = AsyncMock()

    await TariffRuntime(coord).async_refresh()

    coord.client.site_tariff_rates.assert_not_awaited()
    assert coord.tariff_export_rate is not None
    assert tariff_rate_sensor_specs(coord.tariff_export_rate)[0]["state"] == 0.01


@pytest.mark.asyncio
async def test_tariff_runtime_keeps_empty_export_when_dated_api_missing(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.site_tariff_bundle = AsyncMock(
        return_value=(
            {},
            {
                "currency": "$",
                "buyback": {
                    "typeKind": "single",
                    "typeId": "tou",
                    "seasons": [],
                },
            },
        )
    )
    coord.client.site_tariff_rates = None

    await TariffRuntime(coord).async_refresh()

    assert coord.tariff_export_rate is not None
    assert coord.tariff_export_rate.state == "Time of use"
    assert tariff_rate_sensor_specs(coord.tariff_export_rate) == ()


@pytest.mark.asyncio
async def test_tariff_runtime_preserves_previous_export_on_dated_api_failure(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    previous = parse_tariff_rate(
        {
            "currency": "$",
            "buyback": {
                "typeKind": "single",
                "typeId": "flat",
                "seasons": [
                    {
                        "id": "default",
                        "days": [{"id": "all", "periods": [{"rate": "0.01"}]}],
                    }
                ],
            },
        },
        "buyback",
    )
    coord.tariff_export_rate = previous
    coord.client.site_tariff_bundle = AsyncMock(
        return_value=(
            {},
            {
                "currency": "$",
                "buyback": {
                    "typeKind": "single",
                    "typeId": "tou",
                    "seasons": [],
                },
            },
        )
    )
    coord.client.site_tariff_rates = AsyncMock(side_effect=aiohttp.ClientError("boom"))

    await TariffRuntime(coord).async_refresh()

    assert coord.tariff_export_rate is previous
    health = coord._endpoint_family_state(TARIFF_DATED_RATES_ENDPOINT_FAMILY)
    assert health.consecutive_failures == 1
    assert health.cooldown_active is True


@pytest.mark.asyncio
async def test_tariff_runtime_keeps_empty_export_on_dated_api_failure(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.site_tariff_bundle = AsyncMock(
        return_value=(
            {},
            {
                "currency": "$",
                "buyback": {
                    "typeKind": "single",
                    "typeId": "tou",
                    "seasons": [],
                },
            },
        )
    )
    coord.client.site_tariff_rates = AsyncMock(side_effect=aiohttp.ClientError("boom"))

    await TariffRuntime(coord).async_refresh()

    assert coord.tariff_export_rate is not None
    assert coord.tariff_export_rate.state == "Time of use"
    assert tariff_rate_sensor_specs(coord.tariff_export_rate) == ()
    assert (
        coord._endpoint_family_state(TARIFF_DATED_RATES_ENDPOINT_FAMILY).last_error
        == "boom"
    )


@pytest.mark.asyncio
async def test_tariff_runtime_skips_dated_buyback_during_cooldown(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    previous = parse_tariff_rate(
        {
            "currency": "$",
            "buyback": {
                "typeKind": "single",
                "typeId": "flat",
                "seasons": [
                    {
                        "id": "default",
                        "days": [{"id": "all", "periods": [{"rate": "0.01"}]}],
                    }
                ],
            },
        },
        "buyback",
    )
    coord.tariff_export_rate = previous
    coord.client.site_tariff_bundle = AsyncMock(
        return_value=(
            {},
            {
                "currency": "$",
                "buyback": {
                    "typeKind": "single",
                    "typeId": "tou",
                    "seasons": [],
                },
            },
        )
    )
    coord.client.site_tariff_rates = AsyncMock()
    coord._note_endpoint_family_failure(
        TARIFF_DATED_RATES_ENDPOINT_FAMILY,
        aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=404,
        ),
    )

    await TariffRuntime(coord).async_refresh()

    coord.client.site_tariff_rates.assert_not_awaited()
    assert coord.tariff_export_rate is previous


@pytest.mark.asyncio
async def test_tariff_runtime_keeps_empty_export_during_dated_cooldown(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.site_tariff_bundle = AsyncMock(
        return_value=(
            {},
            {
                "currency": "$",
                "buyback": {
                    "typeKind": "single",
                    "typeId": "tou",
                    "seasons": [],
                },
            },
        )
    )
    coord.client.site_tariff_rates = AsyncMock()
    coord._note_endpoint_family_failure(
        TARIFF_DATED_RATES_ENDPOINT_FAMILY,
        aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=404,
        ),
    )

    await TariffRuntime(coord).async_refresh()

    coord.client.site_tariff_rates.assert_not_awaited()
    assert coord.tariff_export_rate is not None
    assert tariff_rate_sensor_specs(coord.tariff_export_rate) == ()


@pytest.mark.asyncio
async def test_tariff_runtime_marks_empty_dated_buyback_response_unavailable(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.site_tariff_bundle = AsyncMock(
        return_value=(
            {},
            {
                "currency": "$",
                "buyback": {
                    "typeKind": "single",
                    "typeId": "tou",
                    "seasons": [],
                },
            },
        )
    )
    coord.client.site_tariff_rates = AsyncMock(return_value={"data": {"buyback": []}})

    await TariffRuntime(coord).async_refresh()

    health = coord._endpoint_family_state(TARIFF_DATED_RATES_ENDPOINT_FAMILY)
    assert health.consecutive_failures == 1
    assert health.cooldown_active is True
    assert health.last_error == "Dated export tariff rates did not include data"


@pytest.mark.asyncio
async def test_tariff_runtime_updates_single_rate_and_preserves_payload(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.site_tariff = AsyncMock(
        return_value={
            "site_id": 123,
            "unknown": {"kept": True},
            "currency": "$",
            "purchase": {
                "typeKind": "single",
                "typeId": "tou",
                "source": "manual",
                "seasons": [
                    {
                        "id": "default",
                        "days": [
                            {
                                "id": "week",
                                "periods": [
                                    {
                                        "id": "off-peak",
                                        "type": "off-peak",
                                        "rate": "0.18",
                                    },
                                    {
                                        "id": "peak-1",
                                        "type": "peak",
                                        "rate": "0.31",
                                    },
                                ],
                            }
                        ],
                    }
                ],
            },
            "buyback": {"typeId": "flat", "seasons": []},
        }
    )
    coord.client.site_tariff_update = AsyncMock(return_value={"message": "success"})
    coord.client.notify_tariff_change = AsyncMock(return_value={"data": "ok"})
    coord.tariff_runtime.async_refresh = AsyncMock()
    coord.tariff_runtime._schedule_post_write_reconciliation = Mock()  # noqa: SLF001
    coord.tariff_runtime._publish_tariff_update = Mock()  # noqa: SLF001
    locator = TariffRateLocator(
        branch="purchase",
        kind="period",
        season_index=1,
        season_id="default",
        day_index=1,
        day_group_id="week",
        period_index=2,
        period_id="peak-1",
        period_type="peak",
    )

    out = await coord.tariff_runtime.async_set_tariff_rate(locator, 0.42)

    assert out == {"message": "success"}
    update_payload = coord.client.site_tariff_update.await_args.args[0]
    assert update_payload["unknown"] == {"kept": True}
    assert update_payload["purchase"]["typeId"] == "tou"
    assert (
        update_payload["purchase"]["seasons"][0]["days"][0]["periods"][0]["rate"]
        == "0.18"
    )
    assert (
        update_payload["purchase"]["seasons"][0]["days"][0]["periods"][1]["rate"]
        == "0.42"
    )
    coord.client.notify_tariff_change.assert_awaited_once_with()
    coord.tariff_runtime.async_refresh.assert_awaited_once_with(force=True)
    coord.tariff_runtime._publish_tariff_update.assert_called_once_with()  # noqa: SLF001
    coord.tariff_runtime._schedule_post_write_reconciliation.assert_called_once_with(  # noqa: SLF001
        ()
    )


@pytest.mark.asyncio
async def test_tariff_runtime_blocks_number_write_when_pricing_edits_disabled(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._pricing_edits_enabled = False  # noqa: SLF001
    coord.client.site_tariff = AsyncMock()
    coord.client.site_tariff_update = AsyncMock()
    coord.client.site_tariff_billing_update = AsyncMock()

    with pytest.raises(ServiceValidationError) as err:
        await TariffRuntime(coord).async_set_tariff_rate({}, 0.2)

    assert err.value.translation_key == "pricing_edits_disabled"
    coord.client.site_tariff.assert_not_awaited()
    coord.client.site_tariff_update.assert_not_awaited()
    coord.client.site_tariff_billing_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_tariff_update_action_works_when_pricing_edits_disabled(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._pricing_edits_enabled = False  # noqa: SLF001
    coord.client.site_tariff = AsyncMock(return_value={"unknown": {"kept": True}})
    coord.client.site_tariff_update = AsyncMock(return_value={"message": "success"})
    coord.client.notify_tariff_change = AsyncMock(return_value={"data": "ok"})
    runtime = TariffRuntime(coord)
    runtime.async_refresh = AsyncMock()
    runtime._schedule_post_write_reconciliation = Mock()  # noqa: SLF001
    coord.tariff_runtime = runtime
    purchase = {
        "typeKind": "single",
        "typeId": "flat",
        "seasons": [
            {
                "id": "default",
                "days": [
                    {
                        "id": "all",
                        "periods": [{"id": "flat", "rate": "0.20"}],
                    }
                ],
            }
        ],
    }

    result = await runtime.async_update_tariff(purchase_tariff=purchase)

    assert result["tariff"] == {"message": "success"}
    coord.client.site_tariff_update.assert_awaited_once_with(
        {"unknown": {"kept": True}, "purchase": purchase}
    )
    coord.client.notify_tariff_change.assert_awaited_once_with()
    runtime.async_refresh.assert_awaited_once_with(force=True)


def test_tariff_runtime_rate_signature_publish_and_task_cleanup(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.tariff_runtime
    coord.tariff_import_rate = parse_tariff_rate(
        {
            "purchase": {
                "typeKind": "single",
                "typeId": "flat",
                "seasons": [
                    {
                        "id": "default",
                        "days": [
                            {
                                "id": "week",
                                "periods": [{"id": "off-peak", "rate": "0.18"}],
                            }
                        ],
                    }
                ],
            }
        },
        "purchase",
    )

    signature = runtime._rate_signature()  # noqa: SLF001
    assert signature == (("purchase", "default_week_off_peak", 0.18),)

    publish = Mock()
    coord.async_set_updated_data = publish  # type: ignore[assignment]
    coord.data = None
    runtime._publish_tariff_update()  # noqa: SLF001
    publish.assert_called_once_with({})

    coord.async_set_updated_data = None  # type: ignore[assignment]
    runtime._publish_tariff_update()  # noqa: SLF001

    current_task = MagicMock()
    runtime._post_write_reconcile_task = current_task  # type: ignore[assignment]  # noqa: SLF001
    runtime._clear_post_write_reconcile_task(MagicMock())  # type: ignore[arg-type]  # noqa: SLF001
    assert runtime._post_write_reconcile_task is current_task  # noqa: SLF001
    runtime._clear_post_write_reconcile_task(current_task)  # type: ignore[arg-type]  # noqa: SLF001
    assert runtime._post_write_reconcile_task is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_tariff_runtime_reconciles_lagging_post_write_read(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = coord.tariff_runtime
    monkeypatch.setattr(
        tariff_mod,
        "TARIFF_WRITE_RECONCILE_DELAYS_S",
        (0.0, 0.0, 0.0),
    )
    runtime.async_refresh = AsyncMock()
    runtime._publish_tariff_update = Mock()  # noqa: SLF001

    await runtime._async_reconcile_after_write(())  # noqa: SLF001

    assert runtime.async_refresh.await_count == 3
    assert runtime._publish_tariff_update.call_count == 3  # noqa: SLF001

    async def _refresh_changed_rate(*, force: bool = False) -> None:
        assert force is True
        coord.tariff_import_rate = parse_tariff_rate(
            {
                "purchase": {
                    "typeKind": "single",
                    "typeId": "flat",
                    "seasons": [
                        {
                            "id": "default",
                            "days": [
                                {
                                    "id": "week",
                                    "periods": [{"id": "peak", "rate": "0.42"}],
                                }
                            ],
                        }
                    ],
                }
            },
            "purchase",
        )

    runtime.async_refresh = AsyncMock(side_effect=_refresh_changed_rate)
    runtime._publish_tariff_update.reset_mock()  # noqa: SLF001

    await runtime._async_reconcile_after_write(())  # noqa: SLF001

    runtime.async_refresh.assert_awaited_once_with(force=True)
    runtime._publish_tariff_update.assert_called_once_with()  # noqa: SLF001


@pytest.mark.asyncio
async def test_tariff_runtime_reconciliation_handles_failure_and_cancellation(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = coord.tariff_runtime
    monkeypatch.setattr(tariff_mod, "TARIFF_WRITE_RECONCILE_DELAYS_S", (0.0,))
    runtime._publish_tariff_update = Mock()  # noqa: SLF001
    runtime.async_refresh = AsyncMock(side_effect=RuntimeError("boom"))

    await runtime._async_reconcile_after_write(())  # noqa: SLF001

    runtime._publish_tariff_update.assert_not_called()  # noqa: SLF001

    runtime.async_refresh = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await runtime._async_reconcile_after_write(())  # noqa: SLF001


@pytest.mark.asyncio
async def test_tariff_runtime_schedules_and_replaces_reconciliation(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.tariff_runtime
    runtime._async_reconcile_after_write = AsyncMock()  # type: ignore[method-assign]
    previous_task = MagicMock(done=Mock(return_value=False))
    runtime._post_write_reconcile_task = previous_task  # type: ignore[assignment]  # noqa: SLF001
    coord.track_entry_background_task = MagicMock(
        wraps=coord.track_entry_background_task
    )

    runtime._schedule_post_write_reconciliation(())  # noqa: SLF001
    scheduled_task = runtime._post_write_reconcile_task  # noqa: SLF001
    assert scheduled_task is not None
    previous_task.cancel.assert_called_once_with()
    coord.track_entry_background_task.assert_called_once_with(scheduled_task)
    await scheduled_task
    await asyncio.sleep(0)
    assert runtime._post_write_reconcile_task is None  # noqa: SLF001

    coord.track_entry_background_task = None  # type: ignore[assignment]
    runtime._schedule_post_write_reconciliation(())  # noqa: SLF001
    untracked_task = runtime._post_write_reconcile_task  # noqa: SLF001
    assert untracked_task is not None
    await untracked_task


@pytest.mark.asyncio
async def test_tariff_runtime_batches_rate_updates_with_one_tariff_put(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.site_tariff = AsyncMock(
        return_value={
            "purchase": {
                "typeId": "tou",
                "seasons": [
                    {
                        "id": "default",
                        "days": [
                            {
                                "id": "week",
                                "periods": [
                                    {"id": "off-peak", "rate": "0.18"},
                                    {"id": "peak", "rate": "0.31"},
                                ],
                            }
                        ],
                    }
                ],
            },
            "buyback": {
                "typeId": "tou",
                "seasons": [
                    {
                        "id": "default",
                        "days": [
                            {
                                "id": "week",
                                "periods": [{"id": "feed-in", "rate": "0.06"}],
                            }
                        ],
                    }
                ],
            },
        }
    )
    coord.client.site_tariff_update = AsyncMock(return_value={"message": "success"})
    coord.client.notify_tariff_change = AsyncMock(return_value={"data": "ok"})
    coord.tariff_runtime.async_refresh = AsyncMock()
    coord.tariff_runtime._schedule_post_write_reconciliation = Mock()  # noqa: SLF001

    out = await coord.tariff_runtime.async_update_tariff(
        rate_updates=[
            {
                "locator": {
                    "branch": "purchase",
                    "kind": "period",
                    "season_index": 1,
                    "season_id": "default",
                    "day_index": 1,
                    "day_group_id": "week",
                    "period_index": 2,
                    "period_id": "peak",
                },
                "rate": 0.42,
            },
            {
                "locator": {
                    "branch": "buyback",
                    "kind": "period",
                    "season_index": 1,
                    "season_id": "default",
                    "day_index": 1,
                    "day_group_id": "week",
                    "period_index": 1,
                    "period_id": "feed-in",
                },
                "rate": 0.08,
            },
        ]
    )

    assert out == {"tariff": {"message": "success"}, "billing": None}
    coord.client.site_tariff.assert_awaited_once_with()
    coord.client.site_tariff_update.assert_awaited_once()
    update_payload = coord.client.site_tariff_update.await_args.args[0]
    assert (
        update_payload["purchase"]["seasons"][0]["days"][0]["periods"][1]["rate"]
        == "0.42"
    )
    assert (
        update_payload["buyback"]["seasons"][0]["days"][0]["periods"][0]["rate"]
        == "0.08"
    )
    coord.client.notify_tariff_change.assert_awaited_once_with()
    coord.tariff_runtime.async_refresh.assert_awaited_once_with(force=True)


@pytest.mark.asyncio
async def test_tariff_runtime_updates_billing_only(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.client.site_tariff_billing_update = AsyncMock(
        return_value={"billingFrequency": "DAY"}
    )
    coord.client.notify_tariff_change = AsyncMock(return_value={"data": "ok"})
    coord.tariff_runtime.async_refresh = AsyncMock()
    coord.tariff_runtime._schedule_post_write_reconciliation = Mock()  # noqa: SLF001

    out = await coord.tariff_runtime.async_update_tariff(
        billing={
            "billing_start_date": "2026-04-01",
            "billing_frequency": "DAY",
            "billing_interval_value": 30,
        }
    )

    assert out == {"tariff": None, "billing": {"billingFrequency": "DAY"}}
    coord.client.site_tariff_billing_update.assert_awaited_once_with(
        {
            "anyBillPeriodStartDate": "2026-04-01",
            "billingFrequency": "DAY",
            "billingIntervalValue": 30,
        }
    )
    coord.client.notify_tariff_change.assert_awaited_once_with()
    coord.tariff_runtime.async_refresh.assert_awaited_once_with(force=True)
    coord.tariff_runtime._schedule_post_write_reconciliation.assert_not_called()  # noqa: SLF001


@pytest.mark.asyncio
async def test_tariff_runtime_updates_billing_and_rates(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.client.site_tariff = AsyncMock(
        return_value={
            "purchase": {
                "typeId": "tiered",
                "seasons": [{"id": "default", "offPeak": "0.03", "tiers": []}],
            }
        }
    )
    coord.client.site_tariff_update = AsyncMock(return_value={"message": "success"})
    coord.client.site_tariff_billing_update = AsyncMock(
        return_value={"billingFrequency": "MONTH"}
    )
    coord.client.notify_tariff_change = AsyncMock(return_value={"data": "ok"})
    coord.tariff_runtime.async_refresh = AsyncMock()
    coord.tariff_runtime._schedule_post_write_reconciliation = Mock()  # noqa: SLF001

    await coord.tariff_runtime.async_update_tariff(
        billing={
            "billing_start_date": "2026-04-01",
            "billing_frequency": "MONTH",
            "billing_interval_value": 2,
        },
        rate_updates=[
            {
                "locator": {
                    "branch": "purchase",
                    "kind": "off_peak",
                    "season_index": 1,
                    "season_id": "default",
                },
                "rate": 0.04,
            }
        ],
    )

    assert (
        coord.client.site_tariff_update.await_args.args[0]["purchase"]["seasons"][0][
            "offPeak"
        ]
        == "0.04"
    )
    coord.client.site_tariff_billing_update.assert_awaited_once()
    coord.client.notify_tariff_change.assert_awaited_once_with()
    coord.tariff_runtime.async_refresh.assert_awaited_once_with(force=True)


@pytest.mark.asyncio
async def test_tariff_runtime_updates_structural_branches(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.client.site_tariff = AsyncMock(
        return_value={
            "site_id": 123,
            "currency": "$",
            "purchase": {
                "typeKind": "single",
                "typeId": "flat",
                "seasons": [
                    {
                        "id": "default",
                        "days": [
                            {
                                "id": "week",
                                "periods": [
                                    {
                                        "id": "off-peak",
                                        "rate": "0.20",
                                        "startTime": "",
                                        "endTime": "",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
        }
    )
    coord.client.site_tariff_update = AsyncMock(return_value={"message": "success"})
    coord.client.notify_tariff_change = AsyncMock(return_value={"data": "ok"})
    coord.tariff_runtime.async_refresh = AsyncMock()
    coord.tariff_runtime._schedule_post_write_reconciliation = Mock()  # noqa: SLF001
    purchase = {
        "typeKind": "seasonal-and-weekends",
        "typeId": "tou",
        "source": "manual",
        "seasons": [
            {
                "id": "summer",
                "startMonth": 1,
                "endMonth": 3,
                "days": [
                    {
                        "id": "week",
                        "periods": [
                            {
                                "id": "peak",
                                "type": "peak",
                                "rate": "0.42",
                                "startTime": 900,
                                "endTime": 1260,
                            }
                        ],
                    }
                ],
            }
        ],
    }
    buyback = {
        "typeKind": "single",
        "typeId": "flat",
        "source": "manual",
        "exportPlan": "netFit",
        "seasons": [
            {
                "id": "default",
                "days": [
                    {
                        "id": "week",
                        "periods": [
                            {
                                "id": "off-peak",
                                "type": "off-peak",
                                "rate": "0.08",
                                "startTime": "",
                                "endTime": "",
                            }
                        ],
                    }
                ],
            }
        ],
    }

    out = await coord.tariff_runtime.async_update_tariff(
        purchase_tariff=purchase,
        buyback_tariff=buyback,
    )

    assert out == {"tariff": {"message": "success"}, "billing": None}
    coord.client.site_tariff.assert_awaited_once_with()
    update_payload = coord.client.site_tariff_update.await_args.args[0]
    assert update_payload["currency"] == "$"
    assert update_payload["purchase"] == purchase
    assert update_payload["buyback"] == buyback
    coord.client.notify_tariff_change.assert_awaited_once_with()
    coord.tariff_runtime.async_refresh.assert_awaited_once_with(force=True)


@pytest.mark.asyncio
async def test_tariff_runtime_updates_full_structural_payload_and_rates(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.site_tariff = AsyncMock()
    coord.client.site_tariff_update = AsyncMock(return_value={"message": "success"})
    coord.client.notify_tariff_change = AsyncMock(return_value={"data": "ok"})
    coord.tariff_runtime.async_refresh = AsyncMock()
    coord.tariff_runtime._schedule_post_write_reconciliation = Mock()  # noqa: SLF001
    payload = {
        "site_id": 123,
        "currency": "$",
        "purchase": {
            "typeKind": "single",
            "typeId": "tiered",
            "source": "manual",
            "seasons": [
                {
                    "id": "default",
                    "offPeak": "0.03",
                    "tiers": [
                        {
                            "id": "tier-1",
                            "rate": "0.20",
                            "startValue": "0",
                            "endValue": -1,
                        }
                    ],
                }
            ],
        },
    }

    await coord.tariff_runtime.async_update_tariff(
        tariff_payload=payload,
        rate_updates=[
            {
                "locator": {
                    "branch": "purchase",
                    "kind": "off_peak",
                    "season_index": 1,
                    "season_id": "default",
                },
                "rate": 0.05,
            }
        ],
    )

    coord.client.site_tariff.assert_not_awaited()
    update_payload = coord.client.site_tariff_update.await_args.args[0]
    assert update_payload["purchase"]["seasons"][0]["offPeak"] == "0.05"
    assert payload["purchase"]["seasons"][0]["offPeak"] == "0.03"


@pytest.mark.asyncio
async def test_tariff_runtime_rejects_invalid_structural_tariffs(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.site_tariff = AsyncMock()
    coord.client.site_tariff_update = AsyncMock()

    invalid_inputs = (
        ({"tariff_payload": []}, "tariff_structure_invalid"),
        ({"tariff_payload": {"currency": "$"}}, "tariff_structure_invalid"),
        ({"purchase_tariff": []}, "tariff_structure_invalid"),
        (
            {
                "purchase_tariff": {
                    "typeKind": "bad",
                    "typeId": "flat",
                    "seasons": [],
                }
            },
            "tariff_structure_invalid",
        ),
        (
            {
                "purchase_tariff": {
                    "typeKind": "single",
                    "typeId": "tou",
                    "seasons": [{"id": "default", "days": []}],
                }
            },
            "tariff_structure_invalid",
        ),
        (
            {
                "purchase_tariff": {
                    "typeKind": "single",
                    "typeId": "tou",
                    "seasons": ["default"],
                }
            },
            "tariff_structure_invalid",
        ),
        (
            {
                "purchase_tariff": {
                    "typeKind": "single",
                    "typeId": "tou",
                    "seasons": [{"id": "default", "days": ["week"]}],
                }
            },
            "tariff_structure_invalid",
        ),
        (
            {
                "purchase_tariff": {
                    "typeKind": "single",
                    "typeId": "tou",
                    "seasons": [{"id": "default", "days": [{"id": "week"}]}],
                }
            },
            "tariff_structure_invalid",
        ),
        (
            {
                "purchase_tariff": {
                    "typeKind": "single",
                    "typeId": "tou",
                    "seasons": [
                        {
                            "id": "default",
                            "days": [{"id": "week", "periods": ["peak"]}],
                        }
                    ],
                }
            },
            "tariff_structure_invalid",
        ),
        (
            {
                "purchase_tariff": {
                    "typeKind": "single",
                    "typeId": "tou",
                    "seasons": [
                        {
                            "id": "default",
                            "days": [
                                {
                                    "id": "week",
                                    "periods": [
                                        {
                                            "id": "peak",
                                            "rate": "bad",
                                            "startTime": 900,
                                            "endTime": 1260,
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            },
            "tariff_rate_invalid",
        ),
        (
            {
                "purchase_tariff": {
                    "typeKind": "single",
                    "typeId": "tiered",
                    "seasons": [{"id": "default"}],
                }
            },
            "tariff_structure_invalid",
        ),
        (
            {
                "purchase_tariff": {
                    "typeKind": "single",
                    "typeId": "tiered",
                    "seasons": [{"id": "default", "tiers": ["tier-1"]}],
                }
            },
            "tariff_structure_invalid",
        ),
        (
            {
                "purchase_tariff": {
                    "typeKind": "single",
                    "typeId": "tiered",
                    "seasons": [
                        {
                            "id": "default",
                            "tiers": [{"rate": "0.1", "endValue": "bad"}],
                        }
                    ],
                }
            },
            "tariff_structure_invalid",
        ),
    )
    for kwargs, translation_key in invalid_inputs:
        with pytest.raises(ServiceValidationError) as err:
            await TariffRuntime(coord).async_update_tariff(**kwargs)
        assert err.value.translation_key == translation_key
    coord.client.site_tariff.assert_not_awaited()
    coord.client.site_tariff_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_tariff_runtime_rejects_structural_time_and_tier_bounds(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.site_tariff = AsyncMock()
    coord.client.site_tariff_update = AsyncMock()

    invalid_inputs = (
        {
            "purchase_tariff": {
                "typeKind": "seasonal",
                "typeId": "tou",
                "seasons": [
                    {
                        "id": "default",
                        "startMonth": 13,
                        "days": [
                            {
                                "id": "week",
                                "periods": [
                                    {
                                        "id": "peak",
                                        "rate": "0.2",
                                        "startTime": 900,
                                        "endTime": 1260,
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        },
        {
            "purchase_tariff": {
                "typeKind": "single",
                "typeId": "tou",
                "seasons": [
                    {
                        "id": "default",
                        "days": [
                            {
                                "id": "week",
                                "periods": [
                                    {
                                        "id": "peak",
                                        "rate": "0.2",
                                        "startTime": 900,
                                        "endTime": "",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        },
        {
            "purchase_tariff": {
                "typeKind": "single",
                "typeId": "tou",
                "seasons": [
                    {
                        "id": "default",
                        "days": [
                            {
                                "id": "week",
                                "periods": [
                                    {
                                        "id": "peak",
                                        "rate": "0.2",
                                        "startTime": 900,
                                        "endTime": 900,
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        },
        {
            "purchase_tariff": {
                "typeKind": "single",
                "typeId": "tou",
                "seasons": [
                    {
                        "id": "default",
                        "days": [
                            {
                                "id": "week",
                                "periods": [
                                    {
                                        "id": "peak",
                                        "rate": "0.2",
                                        "startTime": 1500,
                                        "endTime": 1560,
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        },
        {
            "purchase_tariff": {
                "typeKind": "single",
                "typeId": "tiered",
                "seasons": [
                    {
                        "id": "default",
                        "tiers": [{"rate": "0.1", "startValue": "-1", "endValue": -1}],
                    }
                ],
            }
        },
    )
    for kwargs in invalid_inputs:
        with pytest.raises(ServiceValidationError) as err:
            await TariffRuntime(coord).async_update_tariff(**kwargs)
        assert err.value.translation_key == "tariff_structure_invalid"
    coord.client.site_tariff.assert_not_awaited()
    coord.client.site_tariff_update.assert_not_awaited()

    coord.client.site_tariff_update = None
    with pytest.raises(ServiceValidationError) as err:
        await TariffRuntime(coord).async_update_tariff(
            tariff_payload={
                "purchase": {
                    "typeKind": "single",
                    "typeId": "flat",
                    "seasons": [
                        {
                            "id": "default",
                            "days": [
                                {
                                    "id": "week",
                                    "periods": [{"id": "off-peak", "rate": "0.1"}],
                                }
                            ],
                        }
                    ],
                }
            }
        )
    assert err.value.translation_key == "tariff_rate_api_unavailable"


@pytest.mark.asyncio
async def test_tariff_runtime_updates_tiered_off_peak_and_ignores_notify_failure(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.site_tariff = AsyncMock(
        return_value={
            "purchase": {
                "typeId": "tiered",
                "seasons": [{"id": "default", "offPeak": "0.03", "tiers": []}],
            }
        }
    )
    coord.client.site_tariff_update = AsyncMock(return_value={"message": "success"})
    coord.client.notify_tariff_change = AsyncMock(
        side_effect=aiohttp.ClientError("boom")
    )
    coord.tariff_runtime.async_refresh = AsyncMock()
    coord.tariff_runtime._schedule_post_write_reconciliation = Mock()  # noqa: SLF001

    await coord.tariff_runtime.async_set_tariff_rate(
        {
            "branch": "purchase",
            "kind": "off_peak",
            "season_index": 1,
            "season_id": "default",
        },
        0.05,
    )

    update_payload = coord.client.site_tariff_update.await_args.args[0]
    assert update_payload["purchase"]["seasons"][0]["offPeak"] == "0.05"
    coord.tariff_runtime.async_refresh.assert_awaited_once_with(force=True)


@pytest.mark.asyncio
async def test_tariff_runtime_rejects_invalid_and_stale_targets(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.site_tariff = AsyncMock(
        return_value={"purchase": {"typeId": "tou", "seasons": []}}
    )
    coord.client.site_tariff_update = AsyncMock()

    with pytest.raises(ServiceValidationError):
        await TariffRuntime(coord).async_set_tariff_rate(
            {
                "branch": "purchase",
                "kind": "period",
                "season_index": 1,
                "day_index": 1,
                "period_index": 1,
            },
            0.1,
        )

    with pytest.raises(ServiceValidationError):
        await TariffRuntime(coord).async_set_tariff_rate(
            {
                "branch": "purchase",
                "kind": "period",
                "season_index": 1,
                "day_index": 1,
                "period_index": 1,
            },
            -1,
        )
    coord.client.site_tariff_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_tariff_runtime_rejects_invalid_input_and_unavailable_api(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    with pytest.raises(ServiceValidationError):
        await TariffRuntime(coord).async_set_tariff_rate(None, 0.1)

    coord.client.site_tariff = AsyncMock(return_value=[])
    coord.client.site_tariff_update = AsyncMock()
    with pytest.raises(ServiceValidationError):
        await TariffRuntime(coord).async_set_tariff_rate(
            {
                "branch": "purchase",
                "kind": "period",
                "season_index": 1,
                "day_index": 1,
                "period_index": 1,
            },
            "bad",
        )

    coord.client.site_tariff = None
    with pytest.raises(ServiceValidationError):
        await TariffRuntime(coord).async_set_tariff_rate(
            {
                "branch": "purchase",
                "kind": "period",
                "season_index": 1,
                "day_index": 1,
                "period_index": 1,
            },
            0.1,
        )

    coord.client.site_tariff = AsyncMock(return_value=[])
    with pytest.raises(ServiceValidationError):
        await TariffRuntime(coord).async_set_tariff_rate(
            {
                "branch": "purchase",
                "kind": "period",
                "season_index": 1,
                "day_index": 1,
                "period_index": 1,
            },
            0.1,
        )


@pytest.mark.asyncio
async def test_tariff_runtime_rejects_invalid_billing_and_duplicates(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.site_tariff = AsyncMock(
        return_value={
            "purchase": {"seasons": [{"id": "default", "offPeak": "0.03", "tiers": []}]}
        }
    )
    coord.client.site_tariff_update = AsyncMock()

    with pytest.raises(ServiceValidationError) as err:
        await TariffRuntime(coord).async_update_tariff()
    assert err.value.translation_key == "tariff_update_required"

    coord.client.site_tariff_billing_update = None
    with pytest.raises(ServiceValidationError) as err:
        await TariffRuntime(coord).async_update_tariff(
            billing={
                "billing_start_date": "2026-04-01",
                "billing_frequency": "DAY",
                "billing_interval_value": 30,
            }
        )
    assert err.value.translation_key == "tariff_billing_api_unavailable"

    with pytest.raises(ServiceValidationError) as err:
        await TariffRuntime(coord).async_update_tariff(
            billing={
                "billing_start_date": "bad",
                "billing_frequency": "MONTH",
                "billing_interval_value": 1,
            }
        )
    assert err.value.translation_key == "tariff_billing_start_date_invalid"

    with pytest.raises(ServiceValidationError) as err:
        await TariffRuntime(coord).async_update_tariff(
            billing={
                "billing_start_date": "2026-04-01",
                "billing_frequency": "WEEK",
                "billing_interval_value": 1,
            }
        )
    assert err.value.translation_key == "tariff_billing_frequency_invalid"

    with pytest.raises(ServiceValidationError) as err:
        await TariffRuntime(coord).async_update_tariff(
            billing={
                "billing_start_date": "2026-04-01",
                "billing_frequency": "MONTH",
                "billing_interval_value": 25,
            }
        )
    assert err.value.translation_key == "tariff_billing_interval_invalid"

    duplicate_update = {
        "locator": {
            "branch": "purchase",
            "kind": "off_peak",
            "season_index": 1,
            "season_id": "default",
        },
        "rate": 0.04,
    }
    with pytest.raises(ServiceValidationError) as err:
        await TariffRuntime(coord).async_update_tariff(
            rate_updates=[duplicate_update, duplicate_update]
        )
    assert err.value.translation_key == "tariff_rate_target_duplicate"
    coord.client.site_tariff_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_tariff_runtime_keeps_stale_data_on_failure(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.tariff_billing = parse_tariff_billing(
        {"billingFrequency": "MONTH", "billingIntervalValue": 1}
    )
    coord._note_endpoint_family_success("tariff")
    coord._endpoint_family_state("tariff").next_retry_mono = None
    err = aiohttp.ClientError("boom")
    coord.client.site_tariff_bundle = AsyncMock(side_effect=err)

    await TariffRuntime(coord).async_refresh()

    assert coord.tariff_billing.state == "Monthly"
    health = coord._endpoint_family_state("tariff")
    assert health.consecutive_failures == 1
    assert health.cooldown_active is True


@pytest.mark.asyncio
async def test_tariff_runtime_keeps_stale_data_on_empty_payload(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.tariff_billing = parse_tariff_billing(
        {"billingFrequency": "MONTH", "billingIntervalValue": 1}
    )
    coord._note_endpoint_family_success("tariff")
    coord._endpoint_family_state("tariff").next_retry_mono = None
    coord.client.site_tariff_bundle = AsyncMock(return_value=({}, {}))

    await TariffRuntime(coord).async_refresh()

    assert coord.tariff_billing.state == "Monthly"
    health = coord._endpoint_family_state("tariff")
    assert health.consecutive_failures == 1
    assert health.last_error == "Tariff payload did not include data"


def test_tariff_runtime_refresh_due_uses_endpoint_family_gate(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._endpoint_family_should_run = MagicMock(return_value=True)  # noqa: SLF001

    assert TariffRuntime(coord).refresh_due() is True
    coord._endpoint_family_should_run.assert_called_once_with("tariff")  # noqa: SLF001


@pytest.mark.asyncio
async def test_tariff_runtime_success_throttles_followup_refreshes(
    coordinator_factory,
    monkeypatch,
) -> None:
    coord = coordinator_factory()
    now = datetime(2026, 5, 17, 8, 45, tzinfo=timezone.utc)
    monkeypatch.setattr(coord_mod.time, "monotonic", lambda: 1_000.0)
    monkeypatch.setattr(coord_mod.dt_util, "utcnow", lambda: now)
    coord.client.site_tariff_bundle = AsyncMock(
        return_value=(
            {
                "anyBillPeriodStartDate": "2025-08-18",
                "billingFrequency": "MONTH",
                "billingIntervalValue": 1,
            },
            {
                "currency": "$",
                "purchase": {
                    "typeKind": "single",
                    "typeId": "flat",
                    "seasons": [
                        {
                            "id": "default",
                            "startMonth": "1",
                            "endMonth": "12",
                            "days": [
                                {
                                    "id": "week",
                                    "days": [1, 2, 3, 4, 5, 6, 7],
                                    "periods": [{"rate": "0.03"}],
                                }
                            ],
                        }
                    ],
                },
            },
        )
    )

    await TariffRuntime(coord).async_refresh()

    health = coord._endpoint_family_state(TARIFF_ENDPOINT_FAMILY)  # noqa: SLF001
    assert health.next_retry_mono == pytest.approx(1_000.0 + TARIFF_SUCCESS_TTL_S)
    assert health.next_retry_utc == now + coord_mod.timedelta(
        seconds=TARIFF_SUCCESS_TTL_S
    )
    assert TariffRuntime(coord).refresh_due() is False

    await TariffRuntime(coord).async_refresh()
    coord.client.site_tariff_bundle.assert_awaited_once()

    await TariffRuntime(coord).async_refresh(force=True)
    assert coord.client.site_tariff_bundle.await_count == 2


@pytest.mark.asyncio
async def test_tariff_runtime_respects_endpoint_gate(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._endpoint_family_should_run = MagicMock(return_value=False)  # noqa: SLF001
    coord.client.site_tariff_bundle = AsyncMock()

    await TariffRuntime(coord).async_refresh()

    coord.client.site_tariff_bundle.assert_not_called()


@pytest.mark.asyncio
async def test_tariff_runtime_raises_without_stale_data(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.client.site_tariff_bundle = AsyncMock(side_effect=aiohttp.ClientError("boom"))

    with pytest.raises(OptionalEndpointUnavailable):
        await TariffRuntime(coord).async_refresh()


@pytest.mark.asyncio
async def test_tariff_runtime_raises_when_client_method_missing(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client = object()

    with pytest.raises(OptionalEndpointUnavailable):
        await TariffRuntime(coord).async_refresh()


@pytest.mark.asyncio
async def test_tariff_runtime_keeps_stale_data_on_client_attribute_error(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.tariff_billing = parse_tariff_billing(
        {"billingFrequency": "MONTH", "billingIntervalValue": 1}
    )
    coord._note_endpoint_family_success("tariff")
    coord._endpoint_family_state("tariff").next_retry_mono = None
    coord.client.site_tariff_bundle = AsyncMock(side_effect=AttributeError("boom"))

    await TariffRuntime(coord).async_refresh()

    assert coord.tariff_billing.state == "Monthly"
    health = coord._endpoint_family_state("tariff")
    assert health.consecutive_failures == 1
    assert health.cooldown_active is True


@pytest.mark.asyncio
async def test_tariff_runtime_treats_empty_payload_without_stale_data_as_unconfigured(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.site_tariff_bundle = AsyncMock(return_value=({}, {}))

    await TariffRuntime(coord).async_refresh()

    assert coord.tariff_billing is None
    assert coord.tariff_import_rate is None
    assert coord.tariff_export_rate is None
    assert coord.tariff_last_refresh_utc is not None
    assert getattr(coord, "tariff_rates_last_refresh_utc", None) is None
    health = coord._endpoint_family_state("tariff")
    assert health.support_state == "supported"
    assert health.consecutive_failures == 0
    assert health.last_error is None


@pytest.mark.asyncio
async def test_tariff_runtime_marks_non_empty_no_rate_payload_authoritative(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.site_tariff_bundle = AsyncMock(
        return_value=(
            {
                "anyBillPeriodStartDate": "2026-04-01",
                "billingFrequency": "MONTH",
                "billingIntervalValue": 1,
            },
            {"currency": "$"},
        )
    )

    await TariffRuntime(coord).async_refresh()

    assert coord.tariff_billing.state == "Monthly"
    assert coord.tariff_import_rate is None
    assert coord.tariff_export_rate is None
    assert coord.tariff_rates_last_refresh_utc is coord.tariff_last_refresh_utc


def test_tariff_runtime_diagnostics(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.tariff_last_refresh_utc = datetime(2026, 4, 26, tzinfo=timezone.utc)
    coord.tariff_rates_last_refresh_utc = datetime(2026, 4, 26, 1, tzinfo=timezone.utc)
    coord.tariff_billing = parse_tariff_billing(
        {"billingFrequency": "MONTH", "billingIntervalValue": 1}
    )
    health = coord._endpoint_family_state(TARIFF_ENDPOINT_FAMILY)
    health.support_state = "supported"
    health.consecutive_failures = 1
    health.last_failure_utc = datetime(2026, 4, 25, tzinfo=timezone.utc)
    health.last_error = "Tariff payload did not include data"
    dated_health = coord._endpoint_family_state(TARIFF_DATED_RATES_ENDPOINT_FAMILY)
    dated_health.support_state = "suppressed"
    dated_health.last_status = 404
    dated_health.last_error = "404"

    diag = TariffRuntime(coord).diagnostics()

    assert diag["billing_available"] is True
    assert diag["import_rate_available"] is False
    assert diag["export_rate_available"] is False
    assert diag["last_refresh_utc"] == "2026-04-26T00:00:00+00:00"
    assert diag["rates_last_refresh_utc"] == "2026-04-26T01:00:00+00:00"
    assert diag["endpoint_family"]["support_state"] == "supported"
    assert diag["endpoint_family"]["consecutive_failures"] == 1
    assert diag["endpoint_family"]["last_failure_utc"] == "2026-04-25T00:00:00+00:00"
    assert (
        diag["endpoint_family"]["last_error"] == "Tariff payload did not include data"
    )
    assert diag["dated_rates_endpoint_family"]["support_state"] == "suppressed"
    assert diag["dated_rates_endpoint_family"]["last_status"] == 404


def test_tariff_sensors_expose_state_attributes_and_gateway_device(
    coordinator_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tariff_mod.dt_util,
        "now",
        lambda time_zone=None: datetime(2026, 4, 26, tzinfo=time_zone or timezone.utc),
    )

    coord = coordinator_factory()
    monkeypatch.setattr(
        sensor_mod,
        "next_billing_date",
        lambda snapshot: next_billing_date(snapshot, today=date(2026, 4, 26)),
    )
    coord.tariff_last_refresh_utc = datetime(2026, 4, 26, tzinfo=timezone.utc)
    coord.tariff_billing = parse_tariff_billing(
        {
            "anyBillPeriodStartDate": "2025-08-18",
            "billingFrequency": "MONTH",
            "billingIntervalValue": 1,
        }
    )
    coord.tariff_import_rate = parse_tariff_rate(
        {
            "currency": "$",
            "purchase": {
                "typeKind": "single",
                "typeId": "flat",
                "source": "manual",
                "seasons": [
                    {
                        "id": "default",
                        "startMonth": "1",
                        "endMonth": "12",
                        "days": [{"id": "week", "periods": [{"rate": "0.03"}]}],
                    }
                ],
            },
        },
        "purchase",
    )

    billing_sensor = EnphaseTariffBillingSensor(coord)
    import_sensor = EnphaseTariffRateSensor(coord, True)
    export_sensor = EnphaseTariffRateSensor(coord, False)

    assert billing_sensor.native_value == date(2026, 5, 18)
    assert billing_sensor.icon == "mdi:calendar-month"
    assert billing_sensor.device_class == "date"
    assert billing_sensor.extra_state_attributes["start_date"] == "2025-08-18"
    assert billing_sensor.extra_state_attributes["billing_cycle"] == "Monthly"
    assert import_sensor.native_value == "Flat"
    assert import_sensor.icon == "mdi:cash-minus"
    assert export_sensor.icon == "mdi:cash-plus"
    assert import_sensor.extra_state_attributes["seasons"][0]["id"] == "default"
    assert billing_sensor.device_info["identifiers"]


def test_export_rate_value_sensor_exposes_rate_state_and_attributes(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.tariff_last_refresh_utc = datetime(2026, 4, 26, tzinfo=timezone.utc)
    coord.tariff_export_rate = parse_tariff_rate(
        {
            "currency": "$",
            "buyback": {
                "typeKind": "single",
                "typeId": "tou",
                "source": "netFit",
                "exportPlan": "netFit",
                "seasons": [
                    {
                        "id": "default",
                        "startMonth": "1",
                        "endMonth": "12",
                        "days": [
                            {
                                "id": "week",
                                "days": [1, 2, 3, 4, 5, 6, 7],
                                "periods": [
                                    {
                                        "id": "peak-1",
                                        "type": "peak",
                                        "rate": "0.06",
                                        "startTime": "960",
                                        "endTime": "1320",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
        },
        "buyback",
    )
    spec = export_rate_sensor_specs(coord.tariff_export_rate)[0]

    sensor = EnphaseTariffExportRateValueSensor(coord, spec)

    assert sensor.native_value == 0.06
    assert sensor.native_unit_of_measurement == "$/kWh"
    assert sensor.state_class == "measurement"
    assert sensor.icon == "mdi:cash-plus"
    assert sensor.translation_key == "tariff_export_rate_value"
    assert sensor.translation_placeholders == {"detail": "Peak"}
    assert sensor.available is True
    assert sensor.extra_state_attributes["rate_structure"] == "Time of use"
    assert sensor.extra_state_attributes["period_type"] == "peak"
    assert (
        sensor.extra_state_attributes["last_refresh_utc"] == "2026-04-26T00:00:00+00:00"
    )

    coord.tariff_export_rate = None

    assert sensor.available is False
    assert sensor.native_value is None
    assert sensor.native_unit_of_measurement is None
    assert sensor.extra_state_attributes == {}


def test_import_rate_value_sensor_exposes_rate_state_and_attributes(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.tariff_last_refresh_utc = datetime(2026, 4, 26, tzinfo=timezone.utc)
    coord.tariff_import_rate = parse_tariff_rate(
        {
            "currency": "$",
            "purchase": {
                "typeKind": "single",
                "typeId": "tou",
                "source": "manual",
                "seasons": [
                    {
                        "id": "default",
                        "days": [
                            {
                                "id": "week",
                                "periods": [
                                    {
                                        "id": "off-peak",
                                        "type": "off-peak",
                                        "rate": "0.18",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
        },
        "purchase",
    )
    spec = tariff_rate_sensor_specs(coord.tariff_import_rate)[0]

    sensor = EnphaseTariffRateValueSensor(coord, spec, is_import=True)

    assert sensor.native_value == 0.18
    assert sensor.native_unit_of_measurement == "$/kWh"
    assert sensor.state_class == "measurement"
    assert sensor.suggested_display_precision == 4
    assert sensor.icon == "mdi:cash-minus"
    assert sensor.translation_key == "tariff_import_rate_value"
    assert sensor.translation_placeholders == {"detail": "Off-Peak"}
    assert sensor.available is True
    assert sensor.extra_state_attributes["rate_structure"] == "Time of use"
    assert sensor.extra_state_attributes["period_type"] == "off-peak"
    assert sensor.entity_registry_enabled_default is False


def test_current_import_rate_sensor_exposes_energy_price_state(
    hass,
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.tariff_last_refresh_utc = datetime(2026, 4, 26, tzinfo=timezone.utc)
    coord.tariff_import_rate = parse_tariff_rate(
        {
            "currency": "$",
            "purchase": {
                "typeKind": "single",
                "typeId": "tou",
                "source": "manual",
                "seasons": [
                    {
                        "id": "default",
                        "days": [
                            {
                                "id": "week",
                                "periods": [
                                    {
                                        "id": "peak",
                                        "type": "peak",
                                        "rate": "0.31",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
        },
        "purchase",
    )
    sensor = EnphaseCurrentTariffRateSensor(coord, is_import=True)
    sensor.hass = hass

    assert sensor.native_value == 0.31
    assert sensor.native_unit_of_measurement == f"{hass.config.currency}/kWh"
    assert sensor.state_class == "measurement"
    assert sensor.suggested_display_precision == 4
    assert sensor.icon == "mdi:cash-minus"
    assert sensor.translation_key == "tariff_current_import_rate"
    assert sensor.available is True
    attrs = sensor.extra_state_attributes
    assert attrs["period_type"] == "peak"
    assert attrs["active_rate_name"] == "Peak"
    assert attrs["configured_rates"] == [
        {
            "name": "Peak",
            "rate": "0.31",
            "formatted_rate": "$0.31",
            "unit": "$/kWh",
            "season_id": "default",
            "day_group_id": "week",
            "period_type": "peak",
            "tariff_locator": {
                "branch": "purchase",
                "kind": "period",
                "season_index": 1,
                "season_id": "default",
                "day_index": 1,
                "day_group_id": "week",
                "period_index": 1,
                "period_id": "peak",
                "period_type": "peak",
            },
        }
    ]
    assert attrs["last_refresh_utc"] == "2026-04-26T00:00:00+00:00"
    assert "configured_rates" in sensor._unrecorded_attributes
    fallback_unit_sensor = EnphaseCurrentTariffRateSensor(coord, is_import=True)
    assert fallback_unit_sensor.native_unit_of_measurement == "$/kWh"

    coord.tariff_import_rate = None

    assert sensor.available is False
    assert sensor.native_value is None
    assert sensor.native_unit_of_measurement is None
    assert sensor.extra_state_attributes == {}


def test_current_rate_sensor_uses_home_assistant_timezone_fallback(
    hass,
    coordinator_factory,
    monkeypatch,
) -> None:
    coord = coordinator_factory()
    monkeypatch.setattr(coord, "_site_timezone_name", lambda: "", raising=False)
    coord.tariff_import_rate = parse_tariff_rate(
        {
            "currency": "$",
            "purchase": {
                "typeKind": "single",
                "typeId": "tou",
                "seasons": [
                    {
                        "id": "default",
                        "days": [
                            {
                                "id": "week",
                                "periods": [{"type": "peak", "rate": "0.31"}],
                            }
                        ],
                    }
                ],
            },
        },
        "purchase",
    )
    sensor = EnphaseCurrentTariffRateSensor(coord, is_import=True)
    sensor.hass = hass

    assert sensor.native_value == 0.31


def test_current_rate_sensor_schedules_next_tariff_boundary(
    hass, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    coord.last_success_utc = datetime(2026, 1, 5, 6, 59, tzinfo=timezone.utc)
    coord._site_timezone_name = lambda: "UTC"  # noqa: SLF001
    coord.tariff_import_rate = parse_tariff_rate(
        {
            "currency": "$",
            "purchase": {
                "typeKind": "weekends",
                "typeId": "tou",
                "source": "manual",
                "seasons": [
                    {
                        "id": "default",
                        "days": [
                            {
                                "id": "week",
                                "periods": [
                                    {
                                        "id": "off_peak",
                                        "type": "off-peak",
                                        "rate": "0.11",
                                        "startTime": "1320",
                                        "endTime": "420",
                                    },
                                    {
                                        "id": "peak",
                                        "type": "peak",
                                        "rate": "0.31",
                                        "startTime": "420",
                                        "endTime": "1320",
                                    },
                                ],
                            }
                        ],
                    }
                ],
            },
        },
        "purchase",
    )
    now = datetime(2026, 1, 5, 6, 59, tzinfo=timezone.utc)
    scheduled: list[datetime] = []
    callbacks = []
    cancelled = 0

    def _track(_hass, callback, fire_at):
        callbacks.append(callback)
        scheduled.append(fire_at)

        def _cancel():
            nonlocal cancelled
            cancelled += 1

        return _cancel

    monkeypatch.setattr(sensor_mod, "async_track_point_in_utc_time", _track)
    monkeypatch.setattr(
        sensor_mod.dt_util,
        "now",
        lambda tz=None: now.astimezone(tz) if tz is not None else now,
    )
    monkeypatch.setattr(
        sensor_mod.dt_util,
        "utcnow",
        lambda: now,
    )
    sensor = EnphaseCurrentTariffRateSensor(coord, is_import=True)
    sensor.hass = hass
    sensor.async_write_ha_state = MagicMock()

    sensor._ensure_tariff_boundary_timer()  # noqa: SLF001

    assert scheduled == [datetime(2026, 1, 5, 7, 0, tzinfo=timezone.utc)]
    now = datetime(2026, 1, 5, 7, 0, tzinfo=timezone.utc)
    callbacks[0](now)

    sensor.async_write_ha_state.assert_called_once_with()
    assert scheduled == [
        datetime(2026, 1, 5, 7, 0, tzinfo=timezone.utc),
        datetime(2026, 1, 5, 22, 0, tzinfo=timezone.utc),
    ]
    assert cancelled == 1
    sensor._cancel_tariff_boundary_timer()  # noqa: SLF001
    assert cancelled == 2


@pytest.mark.asyncio
async def test_current_rate_sensor_timer_lifecycle_and_guard_branches(
    hass, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    coord.last_success_utc = datetime(2026, 1, 5, 7, 0, tzinfo=timezone.utc)
    coord._site_timezone_name = lambda: "UTC"  # noqa: SLF001
    scheduled: list[datetime] = []

    def _track(_hass, _callback, fire_at):
        scheduled.append(fire_at)
        return lambda: None

    monkeypatch.setattr(sensor_mod, "async_track_point_in_utc_time", _track)
    monkeypatch.setattr(
        sensor_mod.dt_util,
        "now",
        lambda tz=None: datetime(2026, 1, 5, 7, 0, tzinfo=tz or timezone.utc),
    )
    monkeypatch.setattr(
        sensor_mod.dt_util,
        "utcnow",
        lambda: datetime(2026, 1, 5, 7, 1, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        CoordinatorEntity,
        "async_added_to_hass",
        AsyncMock(),
    )
    monkeypatch.setattr(
        CoordinatorEntity,
        "async_will_remove_from_hass",
        AsyncMock(),
    )
    monkeypatch.setattr(
        CoordinatorEntity,
        "_handle_coordinator_update",
        MagicMock(),
    )

    sensor = EnphaseCurrentTariffRateSensor(coord, is_import=True)
    sensor._ensure_tariff_boundary_timer()  # noqa: SLF001
    assert scheduled == []

    sensor.hass = hass
    sensor._ensure_tariff_boundary_timer()  # noqa: SLF001
    assert scheduled == []

    sensor._tariff_boundary_cancel = MagicMock(side_effect=RuntimeError("boom"))
    sensor._cancel_tariff_boundary_timer()  # noqa: SLF001
    assert sensor._tariff_boundary_cancel is None

    coord.tariff_import_rate = parse_tariff_rate(
        {
            "currency": "$",
            "purchase": {
                "typeKind": "weekends",
                "typeId": "tou",
                "source": "manual",
                "seasons": [
                    {
                        "id": "default",
                        "days": [
                            {
                                "id": "week",
                                "periods": [
                                    {
                                        "id": "off_peak",
                                        "type": "off-peak",
                                        "rate": "0.11",
                                        "startTime": "1320",
                                        "endTime": "420",
                                    },
                                    {
                                        "id": "peak",
                                        "type": "peak",
                                        "rate": "0.31",
                                        "startTime": "420",
                                        "endTime": "1320",
                                    },
                                ],
                            }
                        ],
                    }
                ],
            },
        },
        "purchase",
    )
    monkeypatch.setattr(
        sensor_mod,
        "next_tariff_rate_change",
        lambda _snapshot, _when: datetime(2026, 1, 5, 7, 0, tzinfo=timezone.utc),
    )

    await sensor.async_added_to_hass()
    assert scheduled == [datetime(2026, 1, 5, 7, 1, 1, tzinfo=timezone.utc)]
    sensor._handle_coordinator_update()  # noqa: SLF001
    assert scheduled[-1] == datetime(2026, 1, 5, 7, 1, 1, tzinfo=timezone.utc)
    await sensor.async_will_remove_from_hass()


def test_rate_value_sensor_uses_home_assistant_currency_for_unit(
    hass,
    coordinator_factory,
    monkeypatch,
) -> None:
    coord = coordinator_factory()
    coord.tariff_import_rate = parse_tariff_rate(
        {
            "currency": "$",
            "purchase": {
                "typeKind": "single",
                "typeId": "tou",
                "source": "manual",
                "seasons": [
                    {
                        "id": "default",
                        "days": [
                            {
                                "id": "week",
                                "periods": [{"type": "peak", "rate": "0.31"}],
                            }
                        ],
                    }
                ],
            },
        },
        "purchase",
    )
    spec = tariff_rate_sensor_specs(coord.tariff_import_rate)[0]
    sensor = EnphaseTariffRateValueSensor(coord, spec, is_import=True)
    sensor.hass = hass
    monkeypatch.setattr(hass.config, "currency", "AUD")

    assert sensor.native_unit_of_measurement == "AUD/kWh"
    assert sensor.extra_state_attributes["currency"] == "$"
    assert sensor.extra_state_attributes["formatted_rate"] == "$0.31"


def test_tariff_sensor_falls_back_to_cloud_device(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    coord.inventory_view.type_device_info = MagicMock(return_value=None)
    coord.tariff_billing = parse_tariff_billing(
        {
            "anyBillPeriodStartDate": "2026-04-01",
            "billingFrequency": "DAY",
            "billingIntervalValue": 30,
        }
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.tariff.dt_util.now",
        lambda: datetime(2026, 4, 26, tzinfo=timezone.utc),
    )

    sensor = EnphaseTariffBillingSensor(coord)

    assert sensor.native_value == date(2026, 5, 1)
    assert ("enphase_ev", f"type:{coord.site_id}:cloud") in sensor.device_info[
        "identifiers"
    ]


def test_tariff_sensor_uses_cloud_type_device_when_gateway_absent(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    cloud_info = {"identifiers": {(DOMAIN, "cloud_type")}}

    def type_device_info(type_key):
        return cloud_info if type_key == "cloud" else None

    coord.inventory_view.type_device_info = type_device_info
    coord.tariff_billing = parse_tariff_billing(
        {
            "anyBillPeriodStartDate": "2026-04-01",
            "billingFrequency": "DAY",
            "billingIntervalValue": 1,
        }
    )

    sensor = EnphaseTariffBillingSensor(coord)

    assert sensor.device_info is cloud_info


def test_tariff_sensors_handle_missing_snapshots(coordinator_factory) -> None:
    coord = coordinator_factory()
    billing_sensor = EnphaseTariffBillingSensor(coord)
    import_sensor = EnphaseTariffRateSensor(coord, True)

    assert billing_sensor.available is False
    assert billing_sensor.native_value is None
    assert billing_sensor.extra_state_attributes == {}
    assert import_sensor.available is False
    assert import_sensor.extra_state_attributes == {}


@pytest.mark.asyncio
async def test_tariff_sensors_not_created_without_enphase_data(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    monkeypatch.setattr(
        coord,
        "async_add_topology_listener",
        lambda callback: (lambda: None),
        raising=False,
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    added = []

    await async_setup_entry(
        hass, config_entry, lambda entities, **_: added.extend(entities)
    )

    tariff_unique_ids = [
        entity.unique_id for entity in added if "tariff" in str(entity.unique_id)
    ]
    assert tariff_unique_ids == []


@pytest.mark.asyncio
async def test_tariff_sensor_created_when_previously_registered_without_data(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from homeassistant.helpers import entity_registry as er

    coord = coordinator_factory()
    monkeypatch.setattr(
        coord,
        "async_add_topology_listener",
        lambda callback: (lambda: None),
        raising=False,
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    ent_reg = er.async_get(hass)
    ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_site_{coord.site_id}_tariff_billing_cycle",
        config_entry=config_entry,
    )
    added = []

    await async_setup_entry(
        hass, config_entry, lambda entities, **_: added.extend(entities)
    )

    tariff_unique_ids = [
        entity.unique_id for entity in added if "tariff" in str(entity.unique_id)
    ]
    assert tariff_unique_ids == [f"{DOMAIN}_site_{coord.site_id}_tariff_billing_cycle"]


@pytest.mark.asyncio
async def test_tariff_dynamic_rate_entities_removed_when_branch_removed(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from homeassistant.helpers import entity_registry as er

    coord = coordinator_factory()
    monkeypatch.setattr(
        coord,
        "async_add_topology_listener",
        lambda callback: (lambda: None),
        raising=False,
    )
    coord.tariff_billing = parse_tariff_billing(
        {
            "anyBillPeriodStartDate": "2026-04-01",
            "billingFrequency": "MONTH",
            "billingIntervalValue": 1,
        }
    )
    coord.tariff_import_rate = TariffRateSnapshot(
        state="Flat",
        rate_structure="Flat",
        variation_type="Single",
        source="manual",
        currency="$",
        export_plan=None,
        seasons=(),
    )
    coord.tariff_rates_last_refresh_utc = datetime(2026, 4, 26, tzinfo=timezone.utc)
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    ent_reg = er.async_get(hass)
    old_import_unique_id = (
        f"{DOMAIN}_site_{coord.site_id}_tariff_import_rate_default_week_off_peak"
    )
    old_export_unique_id = (
        f"{DOMAIN}_site_{coord.site_id}_tariff_export_rate_default_week_peak"
    )
    old_current_import_unique_id = (
        f"{DOMAIN}_site_{coord.site_id}_tariff_current_import_rate"
    )
    old_current_export_unique_id = (
        f"{DOMAIN}_site_{coord.site_id}_tariff_current_export_rate"
    )
    for unique_id in (
        old_import_unique_id,
        old_export_unique_id,
        old_current_import_unique_id,
        old_current_export_unique_id,
    ):
        ent_reg.async_get_or_create(
            "sensor",
            DOMAIN,
            unique_id,
            config_entry=config_entry,
        )
    added = []

    await async_setup_entry(
        hass, config_entry, lambda entities, **_: added.extend(entities)
    )

    assert ent_reg.async_get_entity_id("sensor", DOMAIN, old_import_unique_id) is None
    assert ent_reg.async_get_entity_id("sensor", DOMAIN, old_export_unique_id) is None
    assert (
        ent_reg.async_get_entity_id("sensor", DOMAIN, old_current_export_unique_id)
        is None
    )
    assert [
        entity.unique_id for entity in added if "tariff" in str(entity.unique_id)
    ] == [
        f"{DOMAIN}_site_{coord.site_id}_tariff_billing_cycle",
        f"{DOMAIN}_site_{coord.site_id}_tariff_current_import_rate",
    ]


@pytest.mark.asyncio
async def test_tariff_dynamic_rate_entities_removed_when_rates_unconfigured(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from homeassistant.helpers import entity_registry as er

    coord = coordinator_factory()
    monkeypatch.setattr(
        coord,
        "async_add_topology_listener",
        lambda callback: (lambda: None),
        raising=False,
    )
    coord.tariff_billing = parse_tariff_billing(
        {
            "anyBillPeriodStartDate": "2026-04-01",
            "billingFrequency": "MONTH",
            "billingIntervalValue": 1,
        }
    )
    coord.tariff_rates_last_refresh_utc = datetime(2026, 4, 26, tzinfo=timezone.utc)
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    ent_reg = er.async_get(hass)
    old_import_unique_id = (
        f"{DOMAIN}_site_{coord.site_id}_tariff_import_rate_default_week_off_peak"
    )
    old_export_unique_id = (
        f"{DOMAIN}_site_{coord.site_id}_tariff_export_rate_default_week_peak"
    )
    old_current_import_unique_id = (
        f"{DOMAIN}_site_{coord.site_id}_tariff_current_import_rate"
    )
    old_current_export_unique_id = (
        f"{DOMAIN}_site_{coord.site_id}_tariff_current_export_rate"
    )
    for unique_id in (
        old_import_unique_id,
        old_export_unique_id,
        old_current_import_unique_id,
        old_current_export_unique_id,
    ):
        ent_reg.async_get_or_create(
            "sensor",
            DOMAIN,
            unique_id,
            config_entry=config_entry,
        )
    added = []

    await async_setup_entry(
        hass, config_entry, lambda entities, **_: added.extend(entities)
    )

    assert ent_reg.async_get_entity_id("sensor", DOMAIN, old_import_unique_id) is None
    assert ent_reg.async_get_entity_id("sensor", DOMAIN, old_export_unique_id) is None
    assert (
        ent_reg.async_get_entity_id("sensor", DOMAIN, old_current_import_unique_id)
        is None
    )
    assert (
        ent_reg.async_get_entity_id("sensor", DOMAIN, old_current_export_unique_id)
        is None
    )
    assert [
        entity.unique_id for entity in added if "tariff" in str(entity.unique_id)
    ] == [f"{DOMAIN}_site_{coord.site_id}_tariff_billing_cycle"]


@pytest.mark.asyncio
async def test_tariff_dynamic_rate_entities_removed_without_current_context(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from homeassistant.helpers import entity_registry as er

    coord = coordinator_factory()
    monkeypatch.setattr(
        coord,
        "async_add_topology_listener",
        lambda callback: (lambda: None),
        raising=False,
    )
    coord.tariff_billing = parse_tariff_billing(
        {
            "anyBillPeriodStartDate": "2026-04-01",
            "billingFrequency": "MONTH",
            "billingIntervalValue": 1,
        }
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    ent_reg = er.async_get(hass)
    old_import_unique_id = (
        f"{DOMAIN}_site_{coord.site_id}_tariff_import_rate_default_week_off_peak"
    )
    old_export_unique_id = (
        f"{DOMAIN}_site_{coord.site_id}_tariff_export_rate_default_week_peak"
    )
    for unique_id in (old_import_unique_id, old_export_unique_id):
        ent_reg.async_get_or_create(
            "sensor",
            DOMAIN,
            unique_id,
            config_entry=config_entry,
        )
    added = []

    await async_setup_entry(
        hass, config_entry, lambda entities, **_: added.extend(entities)
    )

    assert ent_reg.async_get_entity_id("sensor", DOMAIN, old_import_unique_id) is None
    assert ent_reg.async_get_entity_id("sensor", DOMAIN, old_export_unique_id) is None
    assert [
        entity.unique_id for entity in added if "tariff" in str(entity.unique_id)
    ] == [f"{DOMAIN}_site_{coord.site_id}_tariff_billing_cycle"]


@pytest.mark.asyncio
async def test_tariff_setup_registry_filter_branches(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import sensor as sensor_module

    coord = coordinator_factory()
    monkeypatch.setattr(
        coord,
        "async_add_topology_listener",
        lambda callback: (lambda: None),
        raising=False,
    )
    coord.tariff_import_rate = parse_tariff_rate(
        {
            "currency": "$",
            "purchase": {
                "typeKind": "single",
                "typeId": "tou",
                "source": "manual",
                "seasons": [
                    {
                        "id": "default",
                        "days": [
                            {
                                "id": "week",
                                "periods": [{"type": "peak", "rate": "0.31"}],
                            }
                        ],
                    }
                ],
            },
        },
        "purchase",
    )
    old_unique_id = (
        f"{DOMAIN}_site_{coord.site_id}_tariff_import_rate_default_week_peak"
    )
    current_unique_id = f"{DOMAIN}_site_{coord.site_id}_tariff_current_import_rate"
    fake_registry = SimpleNamespace(
        entities={
            "binary_sensor.skip": SimpleNamespace(
                domain=None,
                entity_id="binary_sensor.skip",
                platform=DOMAIN,
                config_entry_id=config_entry.entry_id,
                unique_id=old_unique_id,
            ),
            "sensor.other_platform": SimpleNamespace(
                domain="sensor",
                entity_id="sensor.other_platform",
                platform="other",
                config_entry_id=config_entry.entry_id,
                unique_id=old_unique_id,
            ),
            "sensor.other_entry": SimpleNamespace(
                domain="sensor",
                entity_id="sensor.other_entry",
                platform=DOMAIN,
                config_entry_id="other-entry",
                unique_id=old_unique_id,
            ),
            "sensor.current": SimpleNamespace(
                domain="sensor",
                entity_id="sensor.current",
                platform=DOMAIN,
                config_entry_id=config_entry.entry_id,
                unique_id=old_unique_id,
            ),
        },
        async_get_entity_id=MagicMock(return_value=None),
        async_remove=MagicMock(),
    )
    monkeypatch.setattr(sensor_module.er, "async_get", lambda hass: fake_registry)
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    added = []

    await async_setup_entry(
        hass, config_entry, lambda entities, **_: added.extend(entities)
    )

    assert current_unique_id in {entity.unique_id for entity in added}
    assert old_unique_id not in {entity.unique_id for entity in added}
    fake_registry.async_remove.assert_called_once_with("sensor.current")


@pytest.mark.asyncio
async def test_tariff_setup_handles_registry_without_values(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import sensor as sensor_module

    coord = coordinator_factory()
    listeners = []
    monkeypatch.setattr(
        coord,
        "async_add_topology_listener",
        lambda callback: listeners.append(callback) or (lambda: None),
        raising=False,
    )
    coord.tariff_import_rate = parse_tariff_rate(
        {
            "currency": "$",
            "purchase": {
                "typeKind": "single",
                "typeId": "tou",
                "source": "manual",
                "seasons": [
                    {
                        "id": "default",
                        "days": [{"id": "week", "periods": [{"rate": "0.20"}]}],
                    }
                ],
            },
        },
        "purchase",
    )
    fake_registry = SimpleNamespace(
        entities={},
        async_get_entity_id=MagicMock(return_value=None),
        async_remove=MagicMock(),
    )
    monkeypatch.setattr(sensor_module.er, "async_get", lambda hass: fake_registry)
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    added = []

    await async_setup_entry(
        hass, config_entry, lambda entities, **_: added.extend(entities)
    )

    assert any(
        "tariff_current_import_rate" in str(entity.unique_id) for entity in added
    )
    fake_registry.entities = object()
    for listener in listeners:
        listener()
    fake_registry.async_remove.assert_not_called()


@pytest.mark.asyncio
async def test_tariff_setup_adds_current_export_rate_sensor(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    monkeypatch.setattr(
        coord,
        "async_add_topology_listener",
        lambda callback: (lambda: None),
        raising=False,
    )
    coord.tariff_export_rate = parse_tariff_rate(
        {
            "currency": "$",
            "buyback": {
                "typeKind": "single",
                "typeId": "tou",
                "source": "manual",
                "seasons": [
                    {
                        "id": "default",
                        "days": [
                            {
                                "id": "week",
                                "periods": [{"type": "peak", "rate": "0.06"}],
                            }
                        ],
                    }
                ],
            },
        },
        "buyback",
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    added = []

    await async_setup_entry(
        hass, config_entry, lambda entities, **_: added.extend(entities)
    )

    assert f"{DOMAIN}_site_{coord.site_id}_tariff_current_export_rate" in {
        entity.unique_id for entity in added
    }
    assert (
        f"{DOMAIN}_site_{coord.site_id}_tariff_export_rate_default_week_peak"
        not in {entity.unique_id for entity in added}
    )


@pytest.mark.asyncio
async def test_tariff_setup_prunes_dynamic_export_for_summary_snapshot(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from homeassistant.helpers import entity_registry as er

    coord = coordinator_factory()
    monkeypatch.setattr(
        coord,
        "async_add_topology_listener",
        lambda callback: (lambda: None),
        raising=False,
    )
    coord.tariff_export_rate = TariffRateSnapshot(
        state="Flat",
        rate_structure="Flat",
        variation_type="Single",
        source="manual",
        currency="$",
        export_plan=None,
        seasons=(),
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    ent_reg = er.async_get(hass)
    old_export_unique_id = (
        f"{DOMAIN}_site_{coord.site_id}_tariff_export_rate_default_week_peak"
    )
    ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        old_export_unique_id,
        config_entry=config_entry,
    )
    added = []

    await async_setup_entry(
        hass, config_entry, lambda entities, **_: added.extend(entities)
    )

    assert ent_reg.async_get_entity_id("sensor", DOMAIN, old_export_unique_id) is None
    assert f"{DOMAIN}_site_{coord.site_id}_tariff_current_export_rate" in {
        entity.unique_id for entity in added
    }


@pytest.mark.asyncio
async def test_tariff_rate_structure_change_skips_retired_entity_rescan(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from homeassistant.helpers import entity_registry as er

    coord = coordinator_factory()
    listeners = []
    monkeypatch.setattr(
        coord,
        "async_add_topology_listener",
        lambda callback: (lambda: None),
        raising=False,
    )
    monkeypatch.setattr(
        coord,
        "async_add_listener",
        lambda callback: listeners.append(callback) or (lambda: None),
        raising=False,
    )
    coord.tariff_import_rate = parse_tariff_rate(
        {
            "currency": "$",
            "purchase": {
                "typeKind": "single",
                "typeId": "tou",
                "source": "manual",
                "seasons": [
                    {
                        "id": "default",
                        "days": [
                            {
                                "id": "week",
                                "periods": [{"type": "off-peak", "rate": "0.18"}],
                            }
                        ],
                    }
                ],
            },
        },
        "purchase",
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    added = []

    await async_setup_entry(
        hass, config_entry, lambda entities, **_: added.extend(entities)
    )

    old_unique_id = (
        f"{DOMAIN}_site_{coord.site_id}_tariff_import_rate_default_week_off_peak"
    )
    current_unique_id = f"{DOMAIN}_site_{coord.site_id}_tariff_current_import_rate"
    assert current_unique_id in {entity.unique_id for entity in added}
    assert old_unique_id not in {entity.unique_id for entity in added}
    ent_reg = er.async_get(hass)
    ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        old_unique_id,
        config_entry=config_entry,
    )

    coord.tariff_import_rate = parse_tariff_rate(
        {
            "currency": "$",
            "purchase": {
                "typeKind": "single",
                "typeId": "tou",
                "source": "manual",
                "seasons": [
                    {
                        "id": "default",
                        "days": [
                            {
                                "id": "week",
                                "periods": [{"type": "peak", "rate": "0.31"}],
                            }
                        ],
                    }
                ],
            },
        },
        "purchase",
    )
    for listener in listeners:
        listener()

    new_unique_id = (
        f"{DOMAIN}_site_{coord.site_id}_tariff_import_rate_default_week_peak"
    )
    assert new_unique_id not in {entity.unique_id for entity in added}
    assert ent_reg.async_get_entity_id("sensor", DOMAIN, old_unique_id) is not None
