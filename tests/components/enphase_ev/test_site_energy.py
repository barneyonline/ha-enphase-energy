"""Site-level lifetime energy sensors and parsing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import UnitOfEnergy, UnitOfPower

from custom_components.enphase_ev import energy as energy_mod
from custom_components.enphase_ev import sensor as sensor_mod
from custom_components.enphase_ev.api import SiteEnergyUnavailable
from custom_components.enphase_ev.energy import (
    LifetimeGuardState,
    SiteEnergyFlow,
)
from custom_components.enphase_ev.sensor import (
    EnphaseBatteryPowerSensor,
    EnphaseGridPowerSensor,
    EnphaseSiteConsumptionPowerSensor,
    EnphaseSiteEnergySensor,
    _SiteConsumptionPowerRestoreData,
    _SiteEnergyRestoreData,
    _SiteLifetimePowerRestoreData,
    _lifetime_energy_delta,
)


def test_site_energy_aggregation_with_fallbacks(coordinator_factory) -> None:
    coord = coordinator_factory()
    payload = {
        "production": [1000, None, 2000, -5],
        "consumption": [1500, 500],
        "solar_home": [400, 100],
        "solar_grid": [600, None],
        "generator_grid": [25],
        "grid_battery": [50],
        "battery_grid": [100],
        "charge": None,
        "solar_battery": [100],
        "discharge": [],
        "battery_home": [150],
        "start_date": "2023-08-10",
        "last_report_date": 1_700_000_001,
        "update_pending": False,
        "interval_minutes": 60,
    }
    flows, meta = coord.energy._aggregate_site_energy(payload)  # noqa: SLF001
    assert flows is not None
    assert set(flows) == {
        "solar_production",
        "consumption",
        "grid_import",
        "grid_export",
        "battery_charge",
        "battery_discharge",
    }
    assert flows["solar_production"].value_kwh == pytest.approx(3.0)
    assert flows["consumption"].value_kwh == pytest.approx(2.0)
    assert flows["consumption"].latest_bucket_wh == pytest.approx(500.0)
    assert flows["consumption"].previous_bucket_wh == pytest.approx(1500.0)
    assert flows["consumption"].raw_bucket_count == 2
    assert flows["grid_import"].fields_used == [
        "consumption",
        "solar_home",
        "battery_home",
        "grid_battery",
    ]
    assert flows["grid_import"].value_kwh == pytest.approx(1.4)
    assert flows["grid_export"].fields_used == [
        "solar_grid",
        "battery_grid",
        "generator_grid",
    ]
    assert flows["grid_export"].value_kwh == pytest.approx(0.725)
    assert flows["battery_charge"].bucket_count == 1
    assert flows["battery_charge"].value_kwh == pytest.approx(0.15)
    assert flows["battery_discharge"].value_kwh == pytest.approx(0.25)
    assert meta["start_date"] == "2023-08-10"
    assert isinstance(meta["last_report_date"], datetime)
    assert meta["update_pending"] is False
    assert meta["interval_minutes"] == pytest.approx(60.0)


def test_site_energy_refresh_due_respects_backoff(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    manager = coord.energy
    manager._service_backoff_until = 150.0  # noqa: SLF001
    monkeypatch.setattr(energy_mod.time, "monotonic", lambda: 100.0)

    assert manager.site_energy_refresh_due() is False


def test_site_energy_grid_export_includes_battery_discharge_to_grid(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    payload = {
        "solar_grid": [0, 0],
        "battery_grid": [8000, 8180],
        "interval_minutes": 60,
    }

    flows, _meta = coord.energy._aggregate_site_energy(payload)  # noqa: SLF001

    assert flows["grid_export"].value_kwh == pytest.approx(16.18)
    assert flows["grid_export"].fields_used == ["solar_grid", "battery_grid"]
    assert flows["grid_export"].legacy_offset_kwh == pytest.approx(16.18)


def test_site_energy_grid_export_preserves_zero_channels(coordinator_factory) -> None:
    coord = coordinator_factory()
    payload = {
        "solar_grid": [0, 0],
        "battery_grid": [0, 0],
        "interval_minutes": 60,
    }

    flows, _meta = coord.energy._aggregate_site_energy(payload)  # noqa: SLF001

    assert flows["grid_export"].value_kwh == 0.0
    assert flows["grid_export"].bucket_count == 2
    assert flows["grid_export"].fields_used == ["solar_grid", "battery_grid"]
    assert flows["grid_export"].legacy_offset_kwh == 0.0


def test_site_energy_refresh_due_skips_when_cache_is_fresh(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    manager = coord.energy
    manager._site_energy_cache_ts = 95.0  # noqa: SLF001
    manager._site_energy_cache_ttl = 10.0  # noqa: SLF001
    manager._service_backoff_until = None  # noqa: SLF001
    coord.client.lifetime_energy = AsyncMock(return_value={})
    monkeypatch.setattr(energy_mod.time, "monotonic", lambda: 100.0)

    assert manager.site_energy_refresh_due() is False


def test_site_energy_aggregation_includes_additional_device_channels(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    payload = {
        "evse": [1200, 800],
        "heatpump": [500, 250],
        "water_heater": [300, None, 200],
        "start_date": "2024-01-01",
        "last_report_date": 1_700_000_000,
        "interval_minutes": 60,
    }

    flows, _meta = coord.energy._aggregate_site_energy(payload)  # noqa: SLF001

    assert flows["evse_charging"].value_kwh == pytest.approx(2.0)
    assert flows["evse_charging"].fields_used == ["evse"]
    assert flows["heat_pump"].value_kwh == pytest.approx(0.75)
    assert flows["heat_pump"].fields_used == ["heatpump"]
    assert flows["water_heater"].value_kwh == pytest.approx(0.5)
    assert flows["water_heater"].fields_used == ["water_heater"]


def test_site_energy_aggregation_skips_zero_only_primary_device_channels(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    payload = {
        "evse": [0, 0],
        "heatpump": [0],
        "water_heater": [0, 0, 0],
        "start_date": "2024-01-01",
        "interval_minutes": 60,
    }

    flows, _meta = coord.energy._aggregate_site_energy(payload)  # noqa: SLF001

    assert "evse_charging" not in flows
    assert "heat_pump" not in flows
    assert "water_heater" not in flows


def test_merge_device_lifetime_channels_keeps_present_primary_values(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    merged = coord.energy._merge_device_lifetime_channels(  # noqa: SLF001
        {
            "evse": [10.0],
            "heatpump": [],
            "water_heater": [],
            "start_date": "2024-01-01",
            "last_report_date": None,
        },
        {
            "evse": [999.0],
            "heatpump": [20.0],
            "water_heater": [30.0],
            "last_report_date": 1_700_000_000,
        },
    )

    assert merged["evse"] == [10.0]
    assert merged["heatpump"] == [20.0]
    assert merged["water_heater"] == [30.0]
    assert merged["last_report_date"] == 1_700_000_000


def test_lifetime_channel_missing_accepts_zero_values(coordinator_factory) -> None:
    coord = coordinator_factory()

    assert coord.energy._lifetime_channel_missing({}, "evse") is True  # noqa: SLF001
    assert (  # noqa: SLF001
        coord.energy._lifetime_channel_missing({"evse": ["bad", None, -1]}, "evse")
        is True
    )
    assert (
        coord.energy._lifetime_channel_missing({"evse": [0]}, "evse") is False
    )  # noqa: SLF001


def test_device_lifetime_channel_missing_requires_positive_values(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    assert (
        coord.energy._device_lifetime_channel_missing({"evse": [0, "0"]}, "evse")
        is True
    )  # noqa: SLF001
    assert (
        coord.energy._device_lifetime_channel_missing({"evse": [0, 25]}, "evse")
        is False
    )  # noqa: SLF001


def test_aggregate_site_energy_supports_zero_device_channels_from_hems_fallback(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    payload = {
        "evse": [0],
        "heatpump": [0],
        "water_heater": [0],
        "interval_minutes": 60,
        "_hems_device_channels": {"evse", "heatpump", "water_heater"},
    }

    flows, meta = coord.energy._aggregate_site_energy(payload)  # noqa: SLF001

    assert flows["evse_charging"].value_kwh == pytest.approx(0.0)
    assert flows["heat_pump"].value_kwh == pytest.approx(0.0)
    assert flows["water_heater"].value_kwh == pytest.approx(0.0)
    assert meta["bucket_lengths"]["water_heater"] == 1
    assert meta["raw_bucket_lengths"]["water_heater"] == 1


def test_apply_lifetime_guard_holds_small_backward_jitter(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.energy._lifetime_guard["sn"] = LifetimeGuardState(last=5.0)

    assert coord.energy._apply_lifetime_guard("sn", 4.99, None) == pytest.approx(5.0)


def test_site_energy_cache_age_and_invalidate(coordinator_factory, monkeypatch) -> None:
    coord = coordinator_factory()
    coord.energy._site_energy_cache_ts = 90.0
    monkeypatch.setattr(
        "custom_components.enphase_ev.energy.time.monotonic", lambda: 100.0
    )
    assert coord.energy._site_energy_cache_age() == pytest.approx(10.0)  # noqa: SLF001
    coord.energy._invalidate_site_energy_cache()  # noqa: SLF001
    assert coord.energy._site_energy_cache_ts is None


def test_parse_site_energy_timestamp_variants(coordinator_factory, monkeypatch) -> None:
    coord = coordinator_factory()
    # Milliseconds epoch
    ts = coord.energy._parse_site_energy_timestamp(1_700_000_000_000)  # noqa: SLF001
    assert isinstance(ts, datetime)
    # ISO date fallback
    parsed_date = coord.energy._parse_site_energy_timestamp(
        "2024-05-01"
    )  # noqa: SLF001
    assert isinstance(parsed_date, datetime)
    # Digit string recursion
    ts_digit = coord.energy._parse_site_energy_timestamp(
        "1700000000000"
    )  # noqa: SLF001
    assert isinstance(ts_digit, datetime)
    # ISO datetime parsing
    parsed_dt = coord.energy._parse_site_energy_timestamp(
        "2024-01-01T00:00:00"
    )  # noqa: SLF001
    assert parsed_dt.tzinfo is not None
    # Invalid string
    assert (
        coord.energy._parse_site_energy_timestamp("not-a-date") is None
    )  # noqa: SLF001


def test_coerce_energy_value_exceptions(coordinator_factory) -> None:
    coord = coordinator_factory()

    class Boom:
        def __float__(self):
            raise ValueError("fail")

    assert coord.energy._coerce_energy_value(Boom()) is None  # noqa: SLF001
    assert coord.energy._coerce_energy_value("bad-number") is None  # noqa: SLF001
    assert coord.energy._aggregate_site_energy(None) is None  # noqa: SLF001

    class BoomFloat(float):
        def __float__(self):
            raise ValueError("boom")

    assert coord.energy._coerce_energy_value(BoomFloat(1.0)) is None  # noqa: SLF001
    assert coord.energy._coerce_energy_value("   ") is None  # noqa: SLF001
    assert coord.energy._parse_site_energy_timestamp(["bad"]) is None  # noqa: SLF001


def test_parse_site_energy_timestamp_error_branches(
    monkeypatch, coordinator_factory
) -> None:
    coord = coordinator_factory()

    class BadInt(int):
        def __new__(cls, value=1):
            return super().__new__(cls, value)

        def __int__(self):
            raise ValueError("no-int")

    assert coord.energy._parse_site_energy_timestamp(BadInt()) is None  # noqa: SLF001
    assert coord.energy._parse_site_energy_timestamp("   ") is None  # noqa: SLF001

    with monkeypatch.context() as m:
        m.setattr(
            "custom_components.enphase_ev.energy.dt_util.parse_datetime",
            lambda _v: (_ for _ in ()).throw(ValueError("dt boom")),
        )
        m.setattr(
            "custom_components.enphase_ev.energy.dt_util.parse_date",
            lambda _v: (_ for _ in ()).throw(ValueError("date boom")),
        )
        assert (
            coord.energy._parse_site_energy_timestamp("2024/01/01") is None
        )  # noqa: SLF001

    with monkeypatch.context() as m:
        m.setattr(
            "custom_components.enphase_ev.energy.dt_util.parse_datetime",
            lambda _v: None,
        )
        from datetime import date

        m.setattr(
            "custom_components.enphase_ev.energy.dt_util.parse_date",
            lambda _v: date(2024, 1, 2),
        )
        parsed = coord.energy._parse_site_energy_timestamp("2024/01/02")  # noqa: SLF001
        assert parsed.date().isoformat() == "2024-01-02"


def test_site_energy_sampled_at_utc_requires_parseable_timestamp(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.energy.site_energy = {
        "consumption": SiteEnergyFlow(
            value_kwh=1.25,
            start_date="2024-01-01",
            last_report_date="soon",
            bucket_count=1,
            fields_used=["consumption"],
            source_unit="Wh",
            update_pending=False,
        )
    }

    sensor = EnphaseSiteEnergySensor(
        coord, "consumption", "site_consumption", "Consumption"
    )
    attrs = sensor.extra_state_attributes

    assert "last_report_date" not in attrs
    assert "sampled_at_utc" not in attrs


def test_diff_energy_fields_when_neg_exceeds(coordinator_factory) -> None:
    coord = coordinator_factory()
    payload = {"consumption": [100], "solar_home": [200]}
    total, count, fields = coord.energy._diff_energy_fields(
        payload, "consumption", "solar_home", None
    )  # noqa: SLF001
    assert total == 0.0 and count == 0 and fields == []


def test_diff_energy_fields_allows_zero_subtrahend(coordinator_factory) -> None:
    coord = coordinator_factory()
    payload = {"consumption": [100], "solar_home": [0]}
    total, count, fields = coord.energy._diff_energy_fields(
        payload, "consumption", "solar_home", None
    )  # noqa: SLF001
    assert total == pytest.approx(100.0)
    assert count == 1
    assert fields == ["consumption", "solar_home"]


def test_diff_energy_fields_returns_positive_delta(coordinator_factory) -> None:
    coord = coordinator_factory()
    payload = {"consumption": [200, 150], "solar_home": [100, 50]}
    total, count, fields = coord.energy._diff_energy_fields(
        payload, "consumption", "solar_home", None
    )  # noqa: SLF001
    assert total == pytest.approx(200.0)
    assert count == 2
    assert fields == ["consumption", "solar_home"]


def test_diff_energy_fields_multi_returns_empty_when_no_subtrahends_match(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    payload = {"consumption": [200], "solar_home": [], "battery_home": [None]}
    total, count, fields = coord.energy._diff_energy_fields_multi(
        payload, "consumption", ("solar_home", "battery_home"), None
    )  # noqa: SLF001
    assert total == 0.0
    assert count == 0
    assert fields == []


def test_site_energy_guard_drop_without_reset(coordinator_factory) -> None:
    coord = coordinator_factory()
    # Seed last value
    coord.energy._apply_site_energy_guard("solar_production", 5.0, None)  # noqa: SLF001
    filtered, reset_at = coord.energy._apply_site_energy_guard(
        "solar_production", 4.6, 5.0
    )  # noqa: SLF001
    assert filtered == pytest.approx(5.0)
    assert reset_at is None
    filtered_inc, _ = coord.energy._apply_site_energy_guard(
        "solar_production", 5.5, 5.0
    )  # noqa: SLF001
    assert filtered_inc == pytest.approx(5.5)


def test_site_energy_guard_filtered_none_skips_store(monkeypatch, coordinator_factory):
    coord = coordinator_factory()
    monkeypatch.setattr(
        coord.energy, "_apply_site_energy_guard", lambda *_args, **_kwargs: (None, None)
    )
    flows, meta = coord.energy._aggregate_site_energy(
        {"production": [1000]}
    )  # noqa: SLF001
    assert flows == {}
    assert meta["interval_minutes"] == pytest.approx(5.0)


def test_site_energy_guard_handles_invalid_sample(coordinator_factory):
    coord = coordinator_factory()
    coord.energy._apply_site_energy_guard("solar_production", 2.0, None)  # noqa: SLF001
    filtered, reset = coord.energy._apply_site_energy_guard(
        "solar_production", "bad", 2.0
    )  # noqa: SLF001
    assert filtered == pytest.approx(2.0)
    assert reset is None


def test_site_energy_guard_sets_prev_when_missing(coordinator_factory):
    coord = coordinator_factory()
    filtered, reset = coord.energy._apply_site_energy_guard(
        "grid_import", None, 1.5
    )  # noqa: SLF001
    assert filtered == pytest.approx(1.5)
    assert reset is None


def test_site_energy_default_interval_applied(coordinator_factory) -> None:
    coord = coordinator_factory()
    payload = {"production": [600]}
    flows, meta = coord.energy._aggregate_site_energy(payload)  # noqa: SLF001
    assert flows is not None
    assert flows["solar_production"].value_kwh == pytest.approx(0.6)
    assert flows["solar_production"].interval_minutes == pytest.approx(5.0)
    assert meta["interval_minutes"] == pytest.approx(5.0)


def _consumption_flow(
    *,
    latest_bucket_wh: object,
    reported_at: datetime,
    bucket_count: int = 2,
    previous_bucket_wh: object = 900.0,
    start_date: str | None = "2024-01-01",
    interval_minutes: float = 5.0,
    update_pending: bool | None = False,
) -> SiteEnergyFlow:
    return SiteEnergyFlow(
        value_kwh=2.0,
        bucket_count=bucket_count,
        fields_used=["consumption"],
        start_date=start_date,
        last_report_date=reported_at,
        update_pending=update_pending,
        interval_minutes=interval_minutes,
        latest_bucket_wh=latest_bucket_wh,  # type: ignore[arg-type]
        previous_bucket_wh=previous_bucket_wh,  # type: ignore[arg-type]
        raw_bucket_count=bucket_count,
    )


def test_site_consumption_power_uses_active_bucket_delta(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseSiteConsumptionPowerSensor(coord)
    base_ts = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
    coord.energy.site_energy = {
        "consumption": _consumption_flow(
            latest_bucket_wh=1000.0,
            reported_at=base_ts,
        )
    }

    assert sensor.native_value is None
    assert sensor.extra_state_attributes["method"] == "seeded"

    coord.energy.site_energy["consumption"] = _consumption_flow(
        latest_bucket_wh=1100.0,
        reported_at=base_ts + timedelta(minutes=5),
    )

    assert sensor.native_value == 1200
    assert sensor.translation_key == "site_consumption_power"
    assert sensor.unique_id.endswith("_site_consumption_power")
    assert sensor.extra_state_attributes == {
        "sampled_at_utc": (base_ts + timedelta(minutes=5)).isoformat(),
        "last_window_seconds": 300.0,
        "method": "consumption_bucket_delta",
    }

    coord.energy.site_energy["consumption"] = _consumption_flow(
        latest_bucket_wh=1100.0,
        reported_at=base_ts + timedelta(minutes=10),
    )
    assert sensor.native_value == 0


def test_site_consumption_power_ignores_historical_bucket_corrections(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseSiteConsumptionPowerSensor(coord)
    base_ts = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
    first, _ = coord.energy._aggregate_site_energy(
        {
            "consumption": [1000.0, 2000.0],
            "last_report_date": base_ts.timestamp(),
            "interval_minutes": 5,
        }
    )
    coord.energy.site_energy = first
    assert sensor.native_value is None

    corrected, _ = coord.energy._aggregate_site_energy(
        {
            "consumption": [9000.0, 2100.0],
            "production": [999999.0],
            "charge": [888888.0],
            "last_report_date": (base_ts + timedelta(minutes=5)).timestamp(),
            "interval_minutes": 5,
        }
    )
    coord.energy.site_energy = corrected

    assert sensor.native_value == 1200


def test_site_consumption_power_handles_bucket_rollover(coordinator_factory) -> None:
    coord = coordinator_factory()
    sensor = EnphaseSiteConsumptionPowerSensor(coord)
    base_ts = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
    coord.energy.site_energy = {
        "consumption": _consumption_flow(
            latest_bucket_wh=1000.0,
            previous_bucket_wh=800.0,
            bucket_count=2,
            reported_at=base_ts,
        )
    }
    assert sensor.native_value is None

    coord.energy.site_energy["consumption"] = _consumption_flow(
        latest_bucket_wh=50.0,
        previous_bucket_wh=1100.0,
        bucket_count=3,
        reported_at=base_ts + timedelta(minutes=5),
    )

    assert sensor.native_value == 1800
    assert sensor.extra_state_attributes["method"] == "consumption_bucket_rollover"


def test_site_consumption_power_reseeds_decreasing_rollover_bucket(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseSiteConsumptionPowerSensor(coord)
    base_ts = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
    coord.energy.site_energy = {
        "consumption": _consumption_flow(
            latest_bucket_wh=1000.0,
            previous_bucket_wh=800.0,
            bucket_count=2,
            reported_at=base_ts,
        )
    }
    assert sensor.native_value is None

    coord.energy.site_energy["consumption"] = _consumption_flow(
        latest_bucket_wh=200.0,
        previous_bucket_wh=900.0,
        bucket_count=3,
        reported_at=base_ts + timedelta(minutes=5),
    )

    assert sensor.native_value is None
    assert sensor.extra_state_attributes["method"] == "bucket_discontinuity"


@pytest.mark.parametrize(
    ("next_flow", "expected_method"),
    [
        ({"latest_bucket_wh": 900.0}, "bucket_discontinuity"),
        ({"latest_bucket_wh": None}, "invalid_bucket"),
        ({"latest_bucket_wh": float("nan")}, "invalid_bucket"),
        ({"latest_bucket_wh": 1100.0, "bucket_count": 4}, "bucket_discontinuity"),
        (
            {"latest_bucket_wh": 1100.0, "start_date": "2024-01-02"},
            "bucket_discontinuity",
        ),
        (
            {"latest_bucket_wh": 1100.0, "interval_minutes": 10.0},
            "interval_discontinuity",
        ),
    ],
)
def test_site_consumption_power_reseeds_untrustworthy_samples(
    coordinator_factory,
    next_flow,
    expected_method,
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseSiteConsumptionPowerSensor(coord)
    base_ts = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
    coord.energy.site_energy = {
        "consumption": _consumption_flow(
            latest_bucket_wh=1000.0,
            reported_at=base_ts,
        )
    }
    assert sensor.native_value is None

    coord.energy.site_energy["consumption"] = _consumption_flow(
        reported_at=base_ts + timedelta(minutes=5),
        **next_flow,
    )

    assert sensor.native_value is None
    assert sensor.extra_state_attributes["method"] == expected_method


def test_site_consumption_power_ignores_pending_and_repeated_samples(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseSiteConsumptionPowerSensor(coord)
    base_ts = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
    coord.energy.site_energy = {
        "consumption": _consumption_flow(
            latest_bucket_wh=1000.0,
            reported_at=base_ts,
        )
    }
    assert sensor.native_value is None
    coord.energy.site_energy["consumption"] = _consumption_flow(
        latest_bucket_wh=1100.0,
        reported_at=base_ts + timedelta(minutes=5),
    )
    assert sensor.native_value == 1200

    coord.energy.site_energy["consumption"] = _consumption_flow(
        latest_bucket_wh=9000.0,
        reported_at=base_ts + timedelta(minutes=10),
        update_pending=True,
    )
    assert sensor.native_value == 1200

    coord.energy.site_energy["consumption"] = _consumption_flow(
        latest_bucket_wh=1100.0,
        reported_at=base_ts + timedelta(minutes=5),
    )
    assert sensor.native_value == 1200


def test_site_consumption_power_ignores_out_of_order_source_sample(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseSiteConsumptionPowerSensor(coord)
    base_ts = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
    coord.energy.site_energy = {
        "consumption": _consumption_flow(
            latest_bucket_wh=1000.0,
            reported_at=base_ts,
        )
    }
    assert sensor.native_value is None
    coord.energy.site_energy["consumption"] = _consumption_flow(
        latest_bucket_wh=1100.0,
        reported_at=base_ts + timedelta(minutes=5),
    )
    assert sensor.native_value == 1200

    coord.energy.site_energy["consumption"] = _consumption_flow(
        latest_bucket_wh=1050.0,
        reported_at=base_ts + timedelta(minutes=2),
    )
    assert sensor.native_value == 1200
    assert (
        sensor.extra_state_attributes["sampled_at_utc"]
        == (base_ts + timedelta(minutes=5)).isoformat()
    )

    coord.energy.site_energy["consumption"] = _consumption_flow(
        latest_bucket_wh=1200.0,
        reported_at=base_ts + timedelta(minutes=10),
    )
    assert sensor.native_value == 1200


def test_site_consumption_power_interval_floor_and_long_gap(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseSiteConsumptionPowerSensor(coord)
    base_ts = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
    coord.energy.site_energy = {
        "consumption": _consumption_flow(
            latest_bucket_wh=1000.0,
            interval_minutes=10,
            reported_at=base_ts,
        )
    }
    assert sensor.native_value is None
    coord.energy.site_energy["consumption"] = _consumption_flow(
        latest_bucket_wh=1100.0,
        interval_minutes=10,
        reported_at=base_ts + timedelta(minutes=5),
    )
    assert sensor.native_value == 600

    coord.energy.site_energy["consumption"] = _consumption_flow(
        latest_bucket_wh=1500.0,
        interval_minutes=10,
        reported_at=base_ts + timedelta(minutes=40),
    )
    assert sensor.native_value is None
    assert sensor.extra_state_attributes["method"] == "interval_discontinuity"


def test_site_consumption_power_stale_sample_is_unavailable(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseSiteConsumptionPowerSensor(coord)
    base_ts = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=25)
    coord.energy.site_energy = {
        "consumption": _consumption_flow(
            latest_bucket_wh=1000.0,
            reported_at=base_ts,
        )
    }
    assert sensor.native_value is None
    coord.energy.site_energy["consumption"] = _consumption_flow(
        latest_bucket_wh=1100.0,
        reported_at=base_ts + timedelta(minutes=5),
    )
    assert sensor.native_value == 1200
    assert sensor.available is False


def test_site_consumption_power_rejects_future_sample_and_recovers(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseSiteConsumptionPowerSensor(coord)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    coord.energy.site_energy = {
        "consumption": _consumption_flow(
            latest_bucket_wh=1000.0,
            reported_at=now + timedelta(minutes=2),
        )
    }
    assert sensor.native_value is None
    assert sensor._last_energy_ts is None

    coord.energy.site_energy["consumption"] = _consumption_flow(
        latest_bucket_wh=1000.0,
        reported_at=now - timedelta(minutes=5),
    )
    assert sensor.native_value is None

    coord.energy.site_energy["consumption"] = _consumption_flow(
        latest_bucket_wh=1100.0,
        reported_at=now,
    )
    assert sensor.native_value == 1200
    assert sensor.available is True


def test_site_consumption_power_restore_data_round_trip() -> None:
    restored = _SiteConsumptionPowerRestoreData.from_dict(
        _SiteConsumptionPowerRestoreData(
            latest_bucket_wh=1100.0,
            raw_bucket_count=2,
            start_date="2024-01-01",
            energy_ts=1_700_000_000.0,
            interval_minutes=5.0,
            last_power_w=1200,
            last_window_seconds=300.0,
            method="consumption_bucket_delta",
        ).as_dict()
    )

    assert restored.last_power_w == 1200
    assert restored.latest_bucket_wh == pytest.approx(1100.0)
    assert _SiteConsumptionPowerRestoreData.from_dict(None).last_power_w is None
    invalid = _SiteConsumptionPowerRestoreData.from_dict(
        {
            "latest_bucket_wh": "bad",
            "raw_bucket_count": "bad",
            "energy_ts": float("inf"),
        }
    )
    assert invalid.latest_bucket_wh is None
    assert invalid.raw_bucket_count is None
    assert invalid.energy_ts is None


@pytest.mark.asyncio
async def test_site_consumption_power_validates_and_uses_restored_baseline(
    hass,
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    base_ts = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=5)
    coord.energy.site_energy = {
        "consumption": _consumption_flow(
            latest_bucket_wh=1100.0,
            reported_at=base_ts,
        )
    }
    sensor = EnphaseSiteConsumptionPowerSensor(coord)
    sensor.hass = hass
    sensor.async_get_last_extra_data = AsyncMock(
        return_value=_SiteConsumptionPowerRestoreData(
            latest_bucket_wh=1100.0,
            raw_bucket_count=2,
            start_date="2024-01-01",
            energy_ts=base_ts.timestamp(),
            interval_minutes=5.0,
            last_power_w=1200,
            last_window_seconds=300.0,
            method="consumption_bucket_delta",
        )
    )

    await sensor.async_added_to_hass()

    assert sensor.available is True
    assert sensor.native_value == 1200
    stored = sensor.extra_restore_state_data
    assert isinstance(stored, _SiteConsumptionPowerRestoreData)
    assert stored.last_power_w == 1200

    coord.energy.site_energy["consumption"] = _consumption_flow(
        latest_bucket_wh=1200.0,
        reported_at=base_ts + timedelta(minutes=5),
    )
    assert sensor.native_value == 1200


@pytest.mark.asyncio
async def test_site_consumption_power_rejects_invalid_or_mismatched_restore(
    hass,
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    base_ts = datetime.now(timezone.utc).replace(microsecond=0)
    coord.energy.site_energy = {
        "consumption": _consumption_flow(
            latest_bucket_wh=1200.0,
            reported_at=base_ts,
        )
    }
    invalid = EnphaseSiteConsumptionPowerSensor(coord)
    invalid.hass = hass
    invalid.async_get_last_extra_data = AsyncMock(
        return_value=_SiteConsumptionPowerRestoreData(
            None, None, None, None, None, None, None, None
        )
    )
    await invalid.async_added_to_hass()
    assert invalid.native_value is None

    mismatched = EnphaseSiteConsumptionPowerSensor(coord)
    mismatched.hass = hass
    mismatched.async_get_last_extra_data = AsyncMock(
        return_value=_SiteConsumptionPowerRestoreData(
            latest_bucket_wh=1100.0,
            raw_bucket_count=2,
            start_date="2024-01-01",
            energy_ts=base_ts.timestamp(),
            interval_minutes=5.0,
            last_power_w=1200,
            last_window_seconds=300.0,
            method="consumption_bucket_delta",
        )
    )
    await mismatched.async_added_to_hass()
    assert mismatched.available is False
    assert mismatched.extra_state_attributes["method"] == "restore_mismatch"

    coord.energy.site_energy["consumption"] = _consumption_flow(
        latest_bucket_wh=1200.0,
        reported_at=base_ts,
        interval_minutes=10.0,
    )
    interval_mismatch = EnphaseSiteConsumptionPowerSensor(coord)
    interval_mismatch.hass = hass
    interval_mismatch.async_get_last_extra_data = AsyncMock(
        return_value=_SiteConsumptionPowerRestoreData(
            latest_bucket_wh=1200.0,
            raw_bucket_count=2,
            start_date="2024-01-01",
            energy_ts=base_ts.timestamp(),
            interval_minutes=5.0,
            last_power_w=1200,
            last_window_seconds=300.0,
            method="consumption_bucket_delta",
        )
    )
    await interval_mismatch.async_added_to_hass()
    assert interval_mismatch.available is False
    assert interval_mismatch.extra_state_attributes["method"] == "restore_mismatch"

    future_restore = EnphaseSiteConsumptionPowerSensor(coord)
    future_restore.hass = hass
    future_restore.async_get_last_extra_data = AsyncMock(
        return_value=_SiteConsumptionPowerRestoreData(
            latest_bucket_wh=1200.0,
            raw_bucket_count=2,
            start_date="2024-01-01",
            energy_ts=(base_ts + timedelta(hours=1)).timestamp(),
            interval_minutes=10.0,
            last_power_w=1200,
            last_window_seconds=600.0,
            method="consumption_bucket_delta",
        )
    )
    await future_restore.async_added_to_hass()
    assert future_restore.native_value is None
    assert future_restore._last_energy_ts == base_ts.timestamp()


def test_site_consumption_power_guard_paths(coordinator_factory, monkeypatch) -> None:
    coord = coordinator_factory()
    sensor = EnphaseSiteConsumptionPowerSensor(coord)

    assert sensor._sample_is_fresh() is False
    assert sensor._coerce_positive_int("bad") is None
    coord.energy.site_energy = None  # type: ignore[assignment]
    assert sensor._flow_data() == {}
    coord.energy.site_energy = {"consumption": {"latest_bucket_wh": 1.0}}
    assert sensor._flow_data() == {"latest_bucket_wh": 1.0}

    base_ts = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=5)
    coord.energy.site_energy = {
        "consumption": _consumption_flow(
            latest_bucket_wh=1000.0,
            reported_at=base_ts,
            interval_minutes=0,
        )
    }
    assert sensor.native_value is None
    assert sensor._last_interval_minutes == 5.0

    coord.energy.site_energy["consumption"] = _consumption_flow(
        latest_bucket_wh=1100.0,
        reported_at=base_ts + timedelta(minutes=5),
        interval_minutes=0,
    )
    assert sensor.native_value == 1200
    monkeypatch.setattr(
        sensor_mod.dt_util,
        "utcnow",
        lambda: (base_ts + timedelta(minutes=6)).replace(tzinfo=None),
    )
    assert sensor._sample_is_fresh() is True

    coord.last_success_utc = None
    coord.last_update_success = False
    assert sensor.available is False

    missing_timestamp = EnphaseSiteConsumptionPowerSensor(coordinator_factory())
    missing_timestamp._coord.energy.site_energy = {
        "consumption": _consumption_flow(
            latest_bucket_wh=1000.0,
            reported_at=base_ts,
        )
    }
    missing_timestamp._coord.energy.site_energy["consumption"].last_report_date = None
    assert missing_timestamp.native_value is None


def test_site_energy_interval_hours_edge_cases(coordinator_factory) -> None:
    coord = coordinator_factory()
    # Non-dict payload falls back to default interval
    hours, minutes = coord.energy._site_energy_interval_hours(None)  # noqa: SLF001
    assert minutes == pytest.approx(5.0)
    assert hours == pytest.approx(5.0 / 60.0)


def test_latest_energy_buckets_rejects_invalid_tail_values(coordinator_factory) -> None:
    manager = coordinator_factory().energy

    assert manager._latest_energy_buckets(None) == (None, None, 0)
    assert manager._latest_energy_buckets([100.0, -1.0]) == (None, 100.0, 2)
    assert manager._latest_energy_buckets([float("nan")]) == (None, None, 1)
    # Fallback "interval" key is honored
    hours, minutes = manager._site_energy_interval_hours({"interval": 10})
    assert minutes == pytest.approx(10.0)
    assert hours == pytest.approx(10.0 / 60.0)

    hours, minutes = manager._site_energy_interval_hours({"interval_minutes": 0})
    assert minutes == pytest.approx(5.0)
    assert hours == pytest.approx(5.0 / 60.0)


@pytest.mark.asyncio
async def test_async_refresh_site_energy_parsed_none(monkeypatch, coordinator_factory):
    coord = coordinator_factory()
    coord.client.lifetime_energy = AsyncMock(return_value={"bad": "payload"})
    monkeypatch.setattr(coord.energy, "_aggregate_site_energy", lambda payload: None)
    await coord.energy._async_refresh_site_energy()  # noqa: SLF001
    assert coord.energy.site_energy == {}


@pytest.mark.asyncio
async def test_async_refresh_site_energy_marks_service_unavailable(coordinator_factory):
    coord = coordinator_factory()
    coord.client.lifetime_energy = AsyncMock(side_effect=SiteEnergyUnavailable("down"))
    await coord.energy._async_refresh_site_energy(force=True)  # noqa: SLF001
    assert coord.energy.service_available is False
    assert coord.energy.service_backoff_active is True


@pytest.mark.asyncio
async def test_async_refresh_site_energy_handles_generic_fetch_failure(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.lifetime_energy = AsyncMock(side_effect=RuntimeError("boom"))

    await coord.energy._async_refresh_site_energy(force=True)  # noqa: SLF001

    assert coord.energy.site_energy == {}
    assert coord.energy.service_available is True


def test_site_energy_mark_service_available_resets_state(coordinator_factory) -> None:
    coord = coordinator_factory()
    energy = coord.energy
    energy._service_available = False
    energy._service_failures = 2
    energy._service_last_error = "down"
    energy._service_backoff_until = time.monotonic() + 60
    energy._mark_service_available()  # noqa: SLF001
    assert energy.service_available is True
    assert energy.service_failures == 0
    assert energy.service_last_error is None


def test_site_energy_note_service_unavailable_default_and_backoff_error(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    energy = coord.energy

    calls = {"count": 0}

    def fake_utcnow():
        calls["count"] += 1
        if calls["count"] == 1:
            return datetime(2025, 1, 1, tzinfo=timezone.utc)
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "custom_components.enphase_ev.energy.dt_util.utcnow", fake_utcnow
    )

    energy._note_service_unavailable(None)  # noqa: SLF001

    assert energy.service_last_error == "Site energy unavailable"
    assert energy.service_backoff_ends_utc is None


@pytest.mark.asyncio
async def test_async_refresh_site_energy_returns_on_backoff(coordinator_factory):
    coord = coordinator_factory()
    coord.client.lifetime_energy = AsyncMock(return_value={"data": {}})
    coord.energy._service_backoff_until = time.monotonic() + 60  # noqa: SLF001

    await coord.energy._async_refresh_site_energy(force=True)  # noqa: SLF001

    coord.client.lifetime_energy.assert_not_awaited()


def test_collect_site_metrics_includes_site_energy_meta(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.energy._site_energy_meta = {
        "start_date": "2024-01-01",
        "last_report_date": datetime(2024, 1, 2, tzinfo=timezone.utc),
        "update_pending": True,
    }
    coord.energy._site_energy_cache_ts = None
    metrics = coord.collect_site_metrics()
    assert "site_energy" in metrics
    assert metrics["site_energy"]["start_date"] == "2024-01-01"


@pytest.mark.asyncio
async def test_success_clears_reauth_issue(
    hass, monkeypatch, mock_issue_registry, coordinator_factory
):
    coord = coordinator_factory()
    coord.client.status = AsyncMock(return_value={"evChargerData": []})
    coord._fake_unauth = 1

    def _get(self):
        return getattr(self, "_fake_unauth", 0)

    def _set(self, val):
        if val == 0 and getattr(self, "_fake_unauth", 0) > 0:
            return
        self._fake_unauth = val

    monkeypatch.setattr(
        coord.__class__, "_unauth_errors", property(_get, _set), raising=False
    )
    await coord._async_update_data()
    assert ("enphase_ev", "reauth_required") in mock_issue_registry.deleted
    assert (
        len([d for d in mock_issue_registry.deleted if d[1] == "reauth_required"]) >= 2
    )


@pytest.mark.asyncio
async def test_summary_ip_address_empty_string(monkeypatch, coordinator_factory):
    coord = coordinator_factory()
    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": "SN1",
                    "name": "Test",
                    "networkConfig": {"ipaddr": ""},
                    "connectors": [{}],
                    "session_d": {},
                }
            ]
        }
    )
    coord._configured_serials = {"SN1"}
    coord.serials = {"SN1"}
    coord._serial_order = ["SN1"]
    coord.data = {"SN1": {}}
    coord._amp_restart_tasks = {}
    await coord._async_update_data()
    assert coord.data["SN1"].get("ip_address") is None


@pytest.mark.asyncio
async def test_summary_ip_address_empty_string_list(monkeypatch, coordinator_factory):
    coord = coordinator_factory()

    class FlakyStr(str):
        def __new__(cls, value=""):
            obj = super().__new__(cls, value)
            obj._count = 0
            return obj

        def __bool__(self):
            self._count += 1
            if self._count < 3:
                return True
            return False

    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": "SN1",
                    "name": "Test",
                    "connectors": [{}],
                    "session_d": {},
                }
            ]
        }
    )
    coord.summary.async_fetch = AsyncMock(
        return_value=[
            {
                "serialNumber": "SN1",
                "networkConfig": [{"ipaddr": (flaky := FlakyStr(""))}],
                "connectors": [{}],
            }
        ]
    )
    coord._configured_serials = {"SN1"}
    coord.serials = {"SN1"}
    coord._serial_order = ["SN1"]
    coord.data = {"SN1": {}}
    coord._amp_restart_tasks = {}
    await coord._async_update_data()
    assert coord.data["SN1"].get("ip_address") is None
    assert flaky._count >= 3


@pytest.mark.asyncio
async def test_async_update_sets_update_interval_with_exception(
    monkeypatch, coordinator_factory
):
    coord = coordinator_factory()
    coord.config_entry = SimpleNamespace(options={})
    coord.client.status = AsyncMock(return_value={"evChargerData": []})
    coord._fast_until = time.monotonic() + 5

    class BoomCoord(type(coord)):
        def async_set_update_interval(self, *_args, **_kwargs):
            raise ValueError("fail")

    coord.__class__.async_set_update_interval = BoomCoord.async_set_update_interval  # type: ignore[assignment]
    coord.update_interval = timedelta(seconds=1)
    await coord._async_update_data()


@pytest.mark.asyncio
async def test_async_update_handles_async_set_update_interval_error(
    monkeypatch, coordinator_factory
):
    coord = coordinator_factory()
    coord.config_entry = SimpleNamespace(options={})
    coord.client.status = AsyncMock(return_value={"evChargerData": []})

    def boom_update_interval(*_args, **_kwargs):
        raise ValueError("boom")

    coord.async_set_update_interval = boom_update_interval
    coord._fast_until = time.monotonic() + 5
    coord.update_interval = timedelta(seconds=5)
    await coord._async_update_data()


def test_slow_interval_floor_handles_bad_update_interval(
    monkeypatch, coordinator_factory
):
    coord = coordinator_factory()

    class BadInterval:
        def total_seconds(self):
            raise ValueError("bad")

    monkeypatch.setattr(
        coord.__class__, "update_interval", property(lambda self: BadInterval())
    )
    assert coord._slow_interval_floor() >= 1


def test_site_energy_import_diff_fallback(coordinator_factory) -> None:
    coord = coordinator_factory()
    payload = {
        "consumption": [500, 500],
        "solar_home": [100, None],
        "start_date": "2024-02-01",
        "interval_minutes": 60,
    }
    flows, _meta = coord.energy._aggregate_site_energy(payload)  # noqa: SLF001
    assert flows is not None
    assert flows["grid_import"].fields_used == ["consumption", "solar_home"]
    assert flows["grid_import"].value_kwh == pytest.approx(0.9)
    assert flows["grid_import"].bucket_count == 1


def test_site_energy_import_diff_with_battery_home(coordinator_factory) -> None:
    coord = coordinator_factory()
    payload = {
        "consumption": [1000, 500],
        "solar_home": [500, 100],
        "battery_home": [400, 300],
        "interval_minutes": 60,
    }
    flows, _meta = coord.energy._aggregate_site_energy(payload)  # noqa: SLF001
    assert flows is not None
    # Derived grid_home: (1500-600-700)=200 Wh
    assert flows["grid_import"].value_kwh == pytest.approx(0.2)
    assert flows["grid_import"].fields_used == [
        "consumption",
        "solar_home",
        "battery_home",
    ]


def test_site_energy_import_diff_skips_when_battery_overlaps(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    payload = {
        "consumption": [500],
        "solar_home": [200],
        "battery_home": [400],
        "interval_minutes": 60,
    }
    flows, _meta = coord.energy._aggregate_site_energy(payload)  # noqa: SLF001
    assert "grid_import" not in flows


def test_site_energy_import_fallbacks_to_grid_home(coordinator_factory) -> None:
    coord = coordinator_factory()
    payload = {
        "consumption": [500],
        "grid_home": [500],
        "interval_minutes": 60,
    }
    flows, _meta = coord.energy._aggregate_site_energy(payload)  # noqa: SLF001
    assert flows["grid_import"].value_kwh == pytest.approx(0.5)
    assert flows["grid_import"].fields_used == ["grid_home"]


def test_site_energy_import_adds_grid_battery_to_derived_home_import(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    payload = {
        "consumption": [1000],
        "solar_home": [400],
        "battery_home": [300],
        "grid_battery": [150],
        "interval_minutes": 60,
    }
    flows, _meta = coord.energy._aggregate_site_energy(payload)  # noqa: SLF001
    assert flows is not None
    assert flows["grid_import"].value_kwh == pytest.approx(0.45)
    assert flows["grid_import"].fields_used == [
        "consumption",
        "solar_home",
        "battery_home",
        "grid_battery",
    ]


def test_site_energy_import_direct_channel_takes_precedence(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    payload = {
        "import": [500],
        "grid_home": [999],
        "grid_battery": [999],
        "consumption": [999],
        "solar_home": [0],
        "battery_home": [0],
        "interval_minutes": 60,
    }
    flows, _meta = coord.energy._aggregate_site_energy(payload)  # noqa: SLF001
    assert flows is not None
    assert flows["grid_import"].value_kwh == pytest.approx(0.5)
    assert flows["grid_import"].fields_used == ["import"]


def test_site_energy_sparse_positive_import_channel_falls_back_to_component_totals(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    payload = {
        "import": [500, None, None],
        "grid_home": [600, 700, 800],
        "grid_battery": [50, 60, 70],
        "interval_minutes": 60,
    }
    flows, _meta = coord.energy._aggregate_site_energy(payload)  # noqa: SLF001
    assert flows is not None
    assert flows["grid_import"].value_kwh == pytest.approx(2.28)
    assert flows["grid_import"].bucket_count == 3
    assert flows["grid_import"].fields_used == ["grid_home", "grid_battery"]


def test_site_energy_sparse_zero_import_channel_falls_back_to_component_totals(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    payload = {
        "import": [0, None],
        "consumption": [999],
        "solar_home": [0],
        "battery_home": [0],
        "grid_home": [999],
        "grid_battery": [999],
        "interval_minutes": 60,
    }
    flows, _meta = coord.energy._aggregate_site_energy(payload)  # noqa: SLF001
    assert flows is not None
    assert flows["grid_import"].value_kwh == pytest.approx(1.998)
    assert flows["grid_import"].fields_used == ["grid_home", "grid_battery"]


def test_site_energy_zero_import_channel_keeps_zero_when_fallback_is_empty(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    payload = {
        "import": [0],
        "consumption": [0],
        "solar_home": [0],
        "battery_home": [0],
        "interval_minutes": 60,
    }
    flows, _meta = coord.energy._aggregate_site_energy(payload)  # noqa: SLF001
    assert flows is not None
    assert flows["grid_import"].value_kwh == pytest.approx(0.0)
    assert flows["grid_import"].fields_used == ["import"]


def test_site_energy_zero_grid_home_channel_falls_back_to_derived_home_import(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    payload = {
        "grid_home": [0, None],
        "consumption": [500],
        "solar_home": [100],
        "battery_home": [100],
        "interval_minutes": 60,
    }
    flows, _meta = coord.energy._aggregate_site_energy(payload)  # noqa: SLF001
    assert flows is not None
    assert flows["grid_import"].value_kwh == pytest.approx(0.3)
    assert flows["grid_import"].fields_used == [
        "consumption",
        "solar_home",
        "battery_home",
    ]


def test_site_energy_honors_interval_minutes(coordinator_factory) -> None:
    coord = coordinator_factory()
    payload = {
        "production": [1200, 600, None],
        "interval_minutes": 15,
    }
    flows, meta = coord.energy._aggregate_site_energy(payload)  # noqa: SLF001
    assert flows is not None
    assert flows["solar_production"].value_kwh == pytest.approx(1.8)
    assert flows["solar_production"].bucket_count == 2
    assert flows["solar_production"].interval_minutes == pytest.approx(15.0)
    assert meta["interval_minutes"] == pytest.approx(15.0)
    assert flows["solar_production"].source_unit == "Wh"


def test_site_energy_guard_confirms_reset(coordinator_factory) -> None:
    coord = coordinator_factory()
    base_payload = {
        "production": [1000],
        "start_date": "2024-01-01",
        "interval_minutes": 60,
    }
    flows, _ = coord.energy._aggregate_site_energy(base_payload)  # noqa: SLF001
    coord.energy.site_energy = flows

    drop_payload = {
        "production": [100],
        "start_date": "2024-01-01",
        "interval_minutes": 60,
    }
    flows_drop, _ = coord.energy._aggregate_site_energy(drop_payload)  # noqa: SLF001
    coord.energy.site_energy = flows_drop
    assert flows_drop["solar_production"].value_kwh == pytest.approx(1.0)
    assert coord.energy._site_energy_force_refresh is True  # noqa: SLF001

    flows_reset, _ = coord.energy._aggregate_site_energy(drop_payload)  # noqa: SLF001
    assert flows_reset["solar_production"].value_kwh == pytest.approx(0.1)
    assert flows_reset["solar_production"].last_reset_at is not None


@pytest.mark.asyncio
async def test_site_energy_cache_respects_ttl(monkeypatch, coordinator_factory):
    coord = coordinator_factory()
    monotonic_val = 1000.0

    async def _payload():
        return {"production": [500], "interval_minutes": 60}

    coord.client.lifetime_energy = AsyncMock(side_effect=_payload)
    monkeypatch.setattr(
        "custom_components.enphase_ev.energy.time.monotonic",
        lambda: monotonic_val,
    )

    await coord.energy._async_refresh_site_energy()  # noqa: SLF001
    assert coord.client.lifetime_energy.call_count == 1

    monotonic_val += 100
    await coord.energy._async_refresh_site_energy()  # noqa: SLF001
    assert coord.client.lifetime_energy.call_count == 1

    coord.energy._site_energy_force_refresh = True  # noqa: SLF001
    await coord.energy._async_refresh_site_energy()  # noqa: SLF001
    assert coord.client.lifetime_energy.call_count == 2

    monotonic_val += coord.energy._site_energy_cache_ttl + 1  # noqa: SLF001
    await coord.energy._async_refresh_site_energy()  # noqa: SLF001
    assert coord.client.lifetime_energy.call_count == 3


@pytest.mark.asyncio
async def test_site_energy_sensor_attributes(hass, coordinator_factory):
    coord = coordinator_factory()
    coord.energy.site_energy = {
        "solar_production": SiteEnergyFlow(
            value_kwh=1.234,
            bucket_count=3,
            fields_used=["production"],
            start_date="2024-01-01",
            last_report_date=datetime(2024, 1, 2, tzinfo=timezone.utc),
            update_pending=False,
            source_unit="Wh",
            last_reset_at="2024-01-03T00:00:00+00:00",
            interval_minutes=60,
        ),
        "evse_charging": SiteEnergyFlow(
            value_kwh=0.456,
            bucket_count=3,
            fields_used=["evse"],
            start_date="2024-01-01",
            last_report_date=datetime(2024, 1, 2, tzinfo=timezone.utc),
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=60,
        ),
    }
    sensor = EnphaseSiteEnergySensor(
        coord,
        "solar_production",
        "site_solar_production",
        "Solar Production",
    )
    sensor.hass = hass
    await sensor.async_added_to_hass()
    assert sensor.available is True
    assert sensor.native_value == pytest.approx(1.23)
    attrs = sensor.extra_state_attributes
    assert attrs["last_reset_at"] == "2024-01-03T00:00:00+00:00"
    assert attrs["sampled_at_utc"].startswith("2024-01-02")
    assert "evse_charging_kwh" not in attrs
    assert sensor.entity_registry_enabled_default is True


def test_site_grid_power_sensor_from_lifetime_import_energy(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseGridPowerSensor(coord)
    base_ts = datetime(2024, 1, 2, tzinfo=timezone.utc)
    coord.energy.site_energy = {
        "grid_import": SiteEnergyFlow(
            value_kwh=1.0,
            bucket_count=1,
            fields_used=["import"],
            start_date="2024-01-01",
            last_report_date=base_ts,
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=5,
        )
    }

    assert sensor.native_value is None
    assert sensor.extra_state_attributes["method"] == "seeded"

    coord.energy.site_energy["grid_import"] = SiteEnergyFlow(
        value_kwh=1.5,
        bucket_count=1,
        fields_used=["import"],
        start_date="2024-01-01",
        last_report_date=base_ts + timedelta(minutes=5),
        update_pending=False,
        source_unit="Wh",
        last_reset_at=None,
        interval_minutes=5,
    )

    assert sensor.native_value == 6000
    assert sensor.native_value == 6000
    assert sensor.extra_state_attributes["method"] == "lifetime_energy_window"

    coord.energy.site_energy["grid_import"] = SiteEnergyFlow(
        value_kwh=1.5,
        bucket_count=1,
        fields_used=["import"],
        start_date="2024-01-01",
        last_report_date=base_ts + timedelta(minutes=10),
        update_pending=False,
        source_unit="Wh",
        last_reset_at=None,
        interval_minutes=5,
    )

    assert sensor.native_value == 0
    assert sensor.extra_state_attributes["method"] == "no_change"


def test_site_grid_power_sensor_seeds_then_uses_signed_lifetime_delta(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseGridPowerSensor(coord)
    base_ts = datetime(2024, 1, 2, tzinfo=timezone.utc)
    coord.energy.site_energy = {
        "grid_import": SiteEnergyFlow(
            value_kwh=24.0,
            bucket_count=8,
            fields_used=["import"],
            start_date="2024-01-01",
            last_report_date=base_ts,
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=15,
        ),
        "grid_export": SiteEnergyFlow(
            value_kwh=8.0,
            bucket_count=8,
            fields_used=["solar_grid"],
            start_date="2024-01-01",
            last_report_date=base_ts,
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=15,
        ),
    }

    assert sensor.native_value is None
    assert sensor.extra_state_attributes["method"] == "seeded"

    coord.energy.site_energy["grid_import"] = SiteEnergyFlow(
        value_kwh=24.75,
        bucket_count=8,
        fields_used=["import"],
        start_date="2024-01-01",
        last_report_date=base_ts + timedelta(minutes=15),
        update_pending=False,
        source_unit="Wh",
        last_reset_at=None,
        interval_minutes=15,
    )
    coord.energy.site_energy["grid_export"] = SiteEnergyFlow(
        value_kwh=8.25,
        bucket_count=8,
        fields_used=["solar_grid"],
        start_date="2024-01-01",
        last_report_date=base_ts + timedelta(minutes=15),
        update_pending=False,
        source_unit="Wh",
        last_reset_at=None,
        interval_minutes=15,
    )

    assert sensor.native_value == 2000
    assert sensor.extra_state_attributes["method"] == "lifetime_energy_window"
    assert sensor.extra_state_attributes["last_window_seconds"] == pytest.approx(900.0)


def test_site_battery_power_sensor_uses_signed_lifetime_deltas(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseBatteryPowerSensor(coord)
    base_ts = datetime(2024, 1, 2, tzinfo=timezone.utc)
    coord.energy.site_energy = {
        "battery_discharge": SiteEnergyFlow(
            value_kwh=5.0,
            bucket_count=8,
            fields_used=["discharge"],
            start_date="2024-01-01",
            last_report_date=base_ts,
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=5,
        ),
        "battery_charge": SiteEnergyFlow(
            value_kwh=2.0,
            bucket_count=8,
            fields_used=["charge"],
            start_date="2024-01-01",
            last_report_date=base_ts,
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=5,
        ),
    }

    assert sensor.native_value is None

    coord.energy.site_energy["battery_discharge"] = SiteEnergyFlow(
        value_kwh=5.5,
        bucket_count=8,
        fields_used=["discharge"],
        start_date="2024-01-01",
        last_report_date=base_ts + timedelta(minutes=5),
        update_pending=False,
        source_unit="Wh",
        last_reset_at=None,
        interval_minutes=5,
    )
    coord.energy.site_energy["battery_charge"] = SiteEnergyFlow(
        value_kwh=2.1,
        bucket_count=8,
        fields_used=["charge"],
        start_date="2024-01-01",
        last_report_date=base_ts + timedelta(minutes=5),
        update_pending=False,
        source_unit="Wh",
        last_reset_at=None,
        interval_minutes=5,
    )

    assert sensor.native_value == 4800
    assert sensor.extra_state_attributes["method"] == "lifetime_energy_window"


def test_site_grid_power_sensor_zero_floor_on_seed(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseGridPowerSensor(coord)
    base_ts = datetime(2024, 1, 2, tzinfo=timezone.utc)
    coord.energy.site_energy = {
        "grid_import": SiteEnergyFlow(
            value_kwh=24.0,
            bucket_count=8,
            fields_used=["import"],
            start_date="2024-01-01",
            last_report_date=base_ts,
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=15,
        )
    }

    assert sensor.native_value is None
    assert sensor.extra_state_attributes["method"] == "seeded"


def test_site_grid_power_sensor_reseeds_when_runtime_baseline_missing(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseGridPowerSensor(coord)
    sensor._live_flow_sample_count = 1
    base_ts = datetime(2024, 1, 2, tzinfo=timezone.utc)
    coord.energy.site_energy = {
        "grid_import": SiteEnergyFlow(
            value_kwh=24.0,
            bucket_count=8,
            fields_used=["import"],
            start_date="2024-01-01",
            last_report_date=base_ts,
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=15,
        )
    }

    assert sensor.native_value is None
    assert sensor.extra_state_attributes["method"] == "seeded"
    assert sensor.extra_state_attributes["last_flow_kwh"] == {"grid_import": 24.0}


def test_site_battery_power_sensor_uses_signed_charge_and_discharge_deltas(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseBatteryPowerSensor(coord)
    base_ts = datetime(2024, 1, 2, tzinfo=timezone.utc)
    coord.energy.site_energy = {
        "battery_discharge": SiteEnergyFlow(
            value_kwh=2.0,
            bucket_count=1,
            fields_used=["discharge"],
            start_date="2024-01-01",
            last_report_date=base_ts,
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=5,
        ),
        "battery_charge": SiteEnergyFlow(
            value_kwh=1.0,
            bucket_count=1,
            fields_used=["charge"],
            start_date="2024-01-01",
            last_report_date=base_ts,
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=5,
        ),
    }

    assert sensor.native_value is None

    coord.energy.site_energy["battery_discharge"] = SiteEnergyFlow(
        value_kwh=2.2,
        bucket_count=1,
        fields_used=["discharge"],
        start_date="2024-01-01",
        last_report_date=base_ts + timedelta(minutes=5),
        update_pending=False,
        source_unit="Wh",
        last_reset_at=None,
        interval_minutes=5,
    )
    assert sensor.native_value == 2400

    coord.energy.site_energy["battery_charge"] = SiteEnergyFlow(
        value_kwh=1.15,
        bucket_count=1,
        fields_used=["charge"],
        start_date="2024-01-01",
        last_report_date=base_ts + timedelta(minutes=10),
        update_pending=False,
        source_unit="Wh",
        last_reset_at=None,
        interval_minutes=5,
    )
    coord.energy.site_energy["battery_discharge"] = SiteEnergyFlow(
        value_kwh=2.2,
        bucket_count=1,
        fields_used=["discharge"],
        start_date="2024-01-01",
        last_report_date=base_ts + timedelta(minutes=10),
        update_pending=False,
        source_unit="Wh",
        last_reset_at=None,
        interval_minutes=5,
    )

    assert sensor.native_value == -1800
    assert sensor.extra_state_attributes["source_flows"] == [
        "battery_discharge",
        "battery_charge",
    ]


def test_site_grid_power_sensor_from_lifetime_export_energy(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseGridPowerSensor(coord)
    base_ts = datetime(2024, 1, 2, tzinfo=timezone.utc)
    coord.energy.site_energy = {
        "grid_export": SiteEnergyFlow(
            value_kwh=0.25,
            bucket_count=1,
            fields_used=["solar_grid"],
            start_date="2024-01-01",
            last_report_date=base_ts,
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=5,
        )
    }

    assert sensor.native_value is None
    coord.energy.site_energy["grid_export"] = SiteEnergyFlow(
        value_kwh=0.55,
        bucket_count=1,
        fields_used=["solar_grid"],
        start_date="2024-01-01",
        last_report_date=base_ts + timedelta(minutes=5),
        update_pending=False,
        source_unit="Wh",
        last_reset_at=None,
        interval_minutes=5,
    )

    assert sensor.native_value == -3600
    assert sensor.translation_key == "site_grid_power"


def test_site_grid_power_sensor_uses_interval_floor_for_tiny_timestamp_gap(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseGridPowerSensor(coord)
    base_ts = datetime(2024, 1, 2, tzinfo=timezone.utc)
    coord.energy.site_energy = {
        "grid_export": SiteEnergyFlow(
            value_kwh=1.0,
            bucket_count=1,
            fields_used=["solar_grid"],
            start_date="2024-01-01",
            last_report_date=base_ts,
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=5,
        )
    }

    assert sensor.native_value is None

    coord.energy.site_energy["grid_export"] = SiteEnergyFlow(
        value_kwh=1.2,
        bucket_count=1,
        fields_used=["solar_grid"],
        start_date="2024-01-01",
        last_report_date=base_ts + timedelta(seconds=5),
        update_pending=False,
        source_unit="Wh",
        last_reset_at=None,
        interval_minutes=5,
    )

    assert sensor.native_value == -2400
    assert sensor.extra_state_attributes["last_window_seconds"] == pytest.approx(300.0)


def test_site_grid_power_sensor_stays_available_at_zero_when_channel_known(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.energy.site_energy = {}
    coord.energy._site_energy_meta = {  # noqa: SLF001
        "bucket_lengths": {"battery_grid": 12},
        "last_report_date": datetime(2024, 1, 2, tzinfo=timezone.utc),
    }
    sensor = EnphaseGridPowerSensor(coord)

    assert sensor.available is True
    assert sensor.native_value == 0
    assert sensor.extra_state_attributes["method"] == "seeded"


def test_site_battery_power_sensor_stays_available_at_zero_when_channel_known(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.energy.site_energy = {}
    coord.energy._site_energy_meta = {  # noqa: SLF001
        "bucket_lengths": {"charge": 12, "discharge": 12},
        "last_report_date": datetime(2024, 1, 2, tzinfo=timezone.utc),
    }
    sensor = EnphaseBatteryPowerSensor(coord)

    assert sensor.available is True
    assert sensor.native_value == 0
    assert sensor.device_info["identifiers"] == {
        ("enphase_ev", f"type:{coord.site_id}:cloud")
    }
    assert sensor.extra_state_attributes["source_flows"] == [
        "battery_discharge",
        "battery_charge",
    ]


def test_site_lifetime_power_sensor_waits_for_first_real_lifetime_sample(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.energy.site_energy = {}
    base_ts = datetime(2024, 1, 2, tzinfo=timezone.utc)
    coord.energy._site_energy_meta = {  # noqa: SLF001
        "bucket_lengths": {"import": 12},
        "last_report_date": base_ts,
    }
    sensor = EnphaseGridPowerSensor(coord)

    assert sensor.available is True
    assert sensor.native_value == 0
    assert sensor.extra_state_attributes["method"] == "seeded"
    assert sensor.extra_state_attributes["last_flow_kwh"] == {}

    coord.energy.site_energy["grid_import"] = SiteEnergyFlow(
        value_kwh=1.5,
        bucket_count=1,
        fields_used=["import"],
        start_date="2024-01-01",
        last_report_date=base_ts + timedelta(minutes=5),
        update_pending=False,
        source_unit="Wh",
        last_reset_at=None,
        interval_minutes=5,
    )

    assert sensor.available is True
    assert sensor.native_value is None
    assert sensor.native_value is None
    assert sensor.extra_state_attributes["method"] == "seeded"
    assert sensor.extra_state_attributes["last_flow_kwh"] == {"grid_import": 1.5}

    coord.energy.site_energy["grid_import"] = SiteEnergyFlow(
        value_kwh=1.7,
        bucket_count=1,
        fields_used=["import"],
        start_date="2024-01-01",
        last_report_date=base_ts + timedelta(minutes=10),
        update_pending=False,
        source_unit="Wh",
        last_reset_at=None,
        interval_minutes=5,
    )

    assert sensor.native_value == 2400
    assert sensor.extra_state_attributes["method"] == "lifetime_energy_window"


def test_site_lifetime_power_sensor_available_with_current_flow(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.energy.site_energy = {"grid_import": {"value_kwh": 1.0}}
    sensor = EnphaseGridPowerSensor(coord)
    assert sensor.available is True
    assert "last_flow_kwh" in sensor._unrecorded_attributes  # noqa: SLF001
    assert "source_flows" in sensor._unrecorded_attributes  # noqa: SLF001
    export_sensor = EnphaseGridPowerSensor(coord)
    coord.energy.site_energy = {"grid_export": {"value_kwh": 1.0}}
    assert export_sensor.available is True


@pytest.mark.asyncio
async def test_site_lifetime_power_sensor_restores_and_handles_resets(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseBatteryPowerSensor(coord)
    sensor.hass = hass

    class LastState:
        state = "1500"
        attributes = {
            "last_flow_kwh": {
                "battery_discharge": 2.5,
                "battery_charge": 1.25,
            },
            "last_energy_ts": 1_700_000_000.0,
            "last_sample_ts": 1_700_000_000.0,
            "last_window_seconds": 300.0,
            "last_reset_at": 1_699_999_700.0,
            "method": "lifetime_energy_window",
            "last_report_date": "2023-11-14T22:13:20+00:00",
        }

    sensor.async_get_last_state = AsyncMock(return_value=LastState())
    sensor.async_get_last_extra_data = AsyncMock(return_value=None)
    await sensor.async_added_to_hass()
    assert sensor.available is False

    coord.energy.site_energy = {
        "battery_discharge": SiteEnergyFlow(
            value_kwh=2.7,
            bucket_count=1,
            fields_used=["discharge"],
            start_date="2024-01-01",
            last_report_date=datetime.fromtimestamp(1_700_000_300, tz=timezone.utc),
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=5,
        ),
        "battery_charge": SiteEnergyFlow(
            value_kwh=1.25,
            bucket_count=1,
            fields_used=["charge"],
            start_date="2024-01-01",
            last_report_date=datetime.fromtimestamp(1_700_000_300, tz=timezone.utc),
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=5,
        ),
    }
    assert sensor.native_value is None
    assert sensor.extra_state_attributes["method"] == "seeded"

    coord.energy.site_energy["battery_discharge"] = SiteEnergyFlow(
        value_kwh=2.9,
        bucket_count=1,
        fields_used=["discharge"],
        start_date="2024-01-01",
        last_report_date=datetime.fromtimestamp(1_700_000_600, tz=timezone.utc),
        update_pending=False,
        source_unit="Wh",
        last_reset_at=None,
        interval_minutes=5,
    )
    assert sensor.native_value == 2400

    coord.energy.site_energy["battery_discharge"] = SiteEnergyFlow(
        value_kwh=1.0,
        bucket_count=1,
        fields_used=["discharge"],
        start_date="2024-01-01",
        last_report_date=datetime.fromtimestamp(1_700_000_900, tz=timezone.utc),
        update_pending=False,
        source_unit="Wh",
        last_reset_at=None,
        interval_minutes=5,
    )
    assert sensor.native_value == 0
    attrs = sensor.extra_state_attributes
    assert attrs["method"] == "lifetime_reset"
    assert attrs["last_reset_at"] == pytest.approx(1_700_000_600.0)


@pytest.mark.asyncio
async def test_site_lifetime_power_sensor_clears_stale_restore_when_zero_channel_known(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory()
    coord.energy.site_energy = {}
    coord.energy._site_energy_meta = {  # noqa: SLF001
        "bucket_lengths": {"solar_grid": 12},
        "last_report_date": datetime(2024, 1, 2, tzinfo=timezone.utc),
    }
    sensor = EnphaseGridPowerSensor(coord)
    sensor.hass = hass

    class LastState:
        state = "900"
        attributes = {
            "last_flow_kwh": {"grid_export": 0.25},
            "last_energy_ts": datetime(
                2024, 1, 1, 23, 55, tzinfo=timezone.utc
            ).timestamp(),
            "last_sample_ts": datetime(
                2024, 1, 1, 23, 55, tzinfo=timezone.utc
            ).timestamp(),
            "last_power_w": 900,
        }

    sensor.async_get_last_state = AsyncMock(return_value=LastState())
    await sensor.async_added_to_hass()

    assert sensor.available is True
    assert sensor.native_value == 0
    assert sensor.extra_state_attributes["method"] == "no_live_data"

    coord.energy.site_energy = {
        "grid_export": SiteEnergyFlow(
            value_kwh=0.2,
            bucket_count=1,
            fields_used=["solar_grid"],
            start_date="2024-01-02",
            last_report_date=datetime(2024, 1, 2, 0, 5, tzinfo=timezone.utc),
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=5,
        )
    }
    assert sensor.available is True
    assert sensor.native_value is None
    assert sensor.extra_state_attributes["method"] == "seeded"

    coord.energy.site_energy["grid_export"] = SiteEnergyFlow(
        value_kwh=0.4,
        bucket_count=1,
        fields_used=["solar_grid"],
        start_date="2024-01-02",
        last_report_date=datetime(2024, 1, 2, 0, 10, tzinfo=timezone.utc),
        update_pending=False,
        source_unit="Wh",
        last_reset_at=None,
        interval_minutes=5,
    )
    assert sensor.native_value == -2400
    assert sensor.extra_state_attributes["method"] == "lifetime_energy_window"


@pytest.mark.asyncio
async def test_site_lifetime_power_sensor_ignores_restored_non_live_history_on_startup(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseGridPowerSensor(coord)
    sensor.hass = hass
    base_ts = datetime(2024, 1, 2, tzinfo=timezone.utc)

    class LastState:
        state = "0"
        attributes = {
            "last_flow_kwh": {
                "grid_import": 0.0,
                "grid_export": 0.0,
            },
            "last_energy_ts": base_ts.timestamp(),
            "last_sample_ts": base_ts.timestamp(),
            "last_power_w": 0,
            "method": "no_live_data",
            "last_report_date": base_ts.isoformat(),
        }

    class LastExtra:
        def as_dict(self):
            return {
                "previous_live_flow_kwh": {
                    "grid_import": 1800.0,
                    "grid_export": 19000.0,
                },
                "previous_live_energy_ts": (base_ts - timedelta(minutes=5)).timestamp(),
                "previous_live_sample_ts": (base_ts - timedelta(minutes=5)).timestamp(),
                "last_live_interval_minutes": 5.0,
                "last_live_flow_sources": {
                    "grid_import": ["import"],
                    "grid_export": ["solar_grid"],
                },
                "previous_live_flow_sources": {
                    "grid_import": ["import"],
                    "grid_export": ["solar_grid"],
                },
            }

    sensor.async_get_last_state = AsyncMock(return_value=LastState())
    sensor.async_get_last_extra_data = AsyncMock(return_value=LastExtra())
    await sensor.async_added_to_hass()

    coord.energy.site_energy = {
        "grid_import": SiteEnergyFlow(
            value_kwh=1809.87,
            bucket_count=1,
            fields_used=["import"],
            start_date="2024-01-02",
            last_report_date=base_ts + timedelta(minutes=5),
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=5,
        ),
        "grid_export": SiteEnergyFlow(
            value_kwh=19079.62,
            bucket_count=1,
            fields_used=["solar_grid"],
            start_date="2024-01-02",
            last_report_date=base_ts + timedelta(minutes=5),
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=5,
        ),
    }

    assert sensor.available is True
    assert sensor.native_value is None
    assert sensor.extra_state_attributes["method"] == "seeded"
    assert sensor.extra_state_attributes["last_flow_kwh"] == {
        "grid_import": 1809.87,
        "grid_export": 19079.62,
    }

    coord.energy.site_energy["grid_import"] = SiteEnergyFlow(
        value_kwh=1810.07,
        bucket_count=1,
        fields_used=["import"],
        start_date="2024-01-02",
        last_report_date=base_ts + timedelta(minutes=10),
        update_pending=False,
        source_unit="Wh",
        last_reset_at=None,
        interval_minutes=5,
    )
    coord.energy.site_energy["grid_export"] = SiteEnergyFlow(
        value_kwh=19079.82,
        bucket_count=1,
        fields_used=["solar_grid"],
        start_date="2024-01-02",
        last_report_date=base_ts + timedelta(minutes=10),
        update_pending=False,
        source_unit="Wh",
        last_reset_at=None,
        interval_minutes=5,
    )

    assert sensor.native_value == 0
    assert sensor.extra_state_attributes["method"] == "no_change"


@pytest.mark.asyncio
async def test_site_lifetime_power_sensor_ignores_legacy_zeroed_history_on_startup(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseGridPowerSensor(coord)
    sensor.hass = hass
    base_ts = datetime(2024, 1, 2, tzinfo=timezone.utc)

    class LastState:
        state = "0"
        attributes = {
            "last_flow_kwh": {
                "grid_import": 0.0,
                "grid_export": 0.0,
            },
            "last_energy_ts": base_ts.timestamp(),
            "last_sample_ts": base_ts.timestamp(),
            "last_power_w": 0,
            "last_report_date": base_ts.isoformat(),
        }

    class LastExtra:
        def as_dict(self):
            return {
                "previous_live_flow_kwh": {
                    "grid_import": 1800.0,
                    "grid_export": 19000.0,
                },
                "previous_live_energy_ts": (base_ts - timedelta(minutes=5)).timestamp(),
                "previous_live_sample_ts": (base_ts - timedelta(minutes=5)).timestamp(),
                "last_live_interval_minutes": 5.0,
                "last_live_flow_sources": {
                    "grid_import": ["import"],
                    "grid_export": ["solar_grid"],
                },
                "previous_live_flow_sources": {
                    "grid_import": ["import"],
                    "grid_export": ["solar_grid"],
                },
            }

    sensor.async_get_last_state = AsyncMock(return_value=LastState())
    sensor.async_get_last_extra_data = AsyncMock(return_value=LastExtra())
    await sensor.async_added_to_hass()

    coord.energy.site_energy = {
        "grid_import": SiteEnergyFlow(
            value_kwh=1809.87,
            bucket_count=1,
            fields_used=["import"],
            start_date="2024-01-02",
            last_report_date=base_ts + timedelta(minutes=5),
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=5,
        ),
        "grid_export": SiteEnergyFlow(
            value_kwh=19079.62,
            bucket_count=1,
            fields_used=["solar_grid"],
            start_date="2024-01-02",
            last_report_date=base_ts + timedelta(minutes=5),
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=5,
        ),
    }

    assert sensor.available is True
    assert sensor.native_value is None
    assert sensor.extra_state_attributes["method"] == "seeded"
    assert sensor.extra_state_attributes["last_flow_kwh"] == {
        "grid_import": 1809.87,
        "grid_export": 19079.62,
    }


@pytest.mark.asyncio
async def test_site_lifetime_power_sensor_restores_two_live_samples_for_same_bucket(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseGridPowerSensor(coord)
    sensor.hass = hass
    base_ts = datetime(2024, 1, 2, tzinfo=timezone.utc)

    class LastState:
        state = "6000"
        attributes = {
            "last_flow_kwh": {"grid_import": 1.5},
            "last_energy_ts": (base_ts + timedelta(minutes=5)).timestamp(),
            "last_sample_ts": (base_ts + timedelta(minutes=5)).timestamp(),
            "last_power_w": 6000,
            "last_window_seconds": 300.0,
            "last_report_date": (base_ts + timedelta(minutes=5)).isoformat(),
        }

    class LastExtra:
        def as_dict(self):
            return {
                "previous_live_flow_kwh": {"grid_import": 1.0},
                "previous_live_energy_ts": base_ts.timestamp(),
                "previous_live_sample_ts": base_ts.timestamp(),
                "last_live_interval_minutes": 5.0,
                "last_live_flow_sources": {"grid_import": ["import"]},
                "previous_live_flow_sources": {"grid_import": ["import"]},
            }

    sensor.async_get_last_state = AsyncMock(return_value=LastState())
    sensor.async_get_last_extra_data = AsyncMock(return_value=LastExtra())
    await sensor.async_added_to_hass()

    coord.energy.site_energy = {
        "grid_import": SiteEnergyFlow(
            value_kwh=1.5,
            bucket_count=1,
            fields_used=["import"],
            start_date="2024-01-01",
            last_report_date=base_ts + timedelta(minutes=5),
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=5,
        )
    }

    assert sensor.available is True
    assert sensor.native_value == 6000
    assert sensor.extra_state_attributes["method"] == "restored_lifetime_energy_window"


@pytest.mark.asyncio
async def test_site_lifetime_power_sensor_restore_uses_interval_floor_for_tiny_gap(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseGridPowerSensor(coord)
    sensor.hass = hass
    base_ts = datetime(2024, 1, 2, tzinfo=timezone.utc)

    class LastState:
        state = "-144000000"
        attributes = {
            "last_flow_kwh": {"grid_export": 1.2},
            "last_energy_ts": (base_ts + timedelta(seconds=5)).timestamp(),
            "last_sample_ts": (base_ts + timedelta(seconds=5)).timestamp(),
            "last_power_w": -144000000,
            "last_window_seconds": 5.0,
            "last_report_date": (base_ts + timedelta(seconds=5)).isoformat(),
        }

    class LastExtra:
        def as_dict(self):
            return {
                "previous_live_flow_kwh": {"grid_export": 1.0},
                "previous_live_energy_ts": base_ts.timestamp(),
                "previous_live_sample_ts": base_ts.timestamp(),
                "last_live_interval_minutes": 5.0,
                "last_live_flow_sources": {"grid_export": ["solar_grid"]},
                "previous_live_flow_sources": {"grid_export": ["solar_grid"]},
            }

    sensor.async_get_last_state = AsyncMock(return_value=LastState())
    sensor.async_get_last_extra_data = AsyncMock(return_value=LastExtra())
    await sensor.async_added_to_hass()

    coord.energy.site_energy = {
        "grid_export": SiteEnergyFlow(
            value_kwh=1.2,
            bucket_count=1,
            fields_used=["solar_grid"],
            start_date="2024-01-01",
            last_report_date=base_ts + timedelta(seconds=5),
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=5,
        )
    }

    assert sensor.available is True
    assert sensor.native_value == -2400
    assert sensor.extra_state_attributes["method"] == "restored_lifetime_energy_window"
    assert sensor.extra_state_attributes["last_window_seconds"] == pytest.approx(300.0)


@pytest.mark.asyncio
async def test_site_lifetime_power_sensor_does_not_reuse_same_bucket_restore_without_extra_history(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseGridPowerSensor(coord)
    sensor.hass = hass
    base_ts = datetime(2024, 1, 2, tzinfo=timezone.utc)

    class LastState:
        state = "6000"
        attributes = {
            "last_flow_kwh": {"grid_import": 1.5},
            "last_energy_ts": (base_ts + timedelta(minutes=5)).timestamp(),
            "last_sample_ts": (base_ts + timedelta(minutes=5)).timestamp(),
            "last_power_w": 6000,
            "last_window_seconds": 300.0,
            "last_report_date": (base_ts + timedelta(minutes=5)).isoformat(),
        }

    sensor.async_get_last_state = AsyncMock(return_value=LastState())
    sensor.async_get_last_extra_data = AsyncMock(return_value=None)
    await sensor.async_added_to_hass()

    coord.energy.site_energy = {
        "grid_import": SiteEnergyFlow(
            value_kwh=1.5,
            bucket_count=1,
            fields_used=["import"],
            start_date="2024-01-01",
            last_report_date=base_ts + timedelta(minutes=5),
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=5,
        )
    }

    assert sensor.available is True
    assert sensor.native_value is None
    assert sensor.extra_state_attributes["method"] == "seeded"


@pytest.mark.asyncio
async def test_site_lifetime_power_sensor_ignores_same_bucket_restore_with_tiny_window_without_extra_history(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseGridPowerSensor(coord)
    sensor.hass = hass
    base_ts = datetime(2024, 1, 2, tzinfo=timezone.utc)

    class LastState:
        state = "-144000000"
        attributes = {
            "last_flow_kwh": {"grid_export": 1.2},
            "last_energy_ts": (base_ts + timedelta(seconds=5)).timestamp(),
            "last_sample_ts": (base_ts + timedelta(seconds=5)).timestamp(),
            "last_power_w": -144000000,
            "last_window_seconds": 5.0,
            "last_report_date": (base_ts + timedelta(seconds=5)).isoformat(),
        }

    sensor.async_get_last_state = AsyncMock(return_value=LastState())
    sensor.async_get_last_extra_data = AsyncMock(return_value=None)
    await sensor.async_added_to_hass()

    coord.energy.site_energy = {
        "grid_export": SiteEnergyFlow(
            value_kwh=1.2,
            bucket_count=1,
            fields_used=["solar_grid"],
            start_date="2024-01-01",
            last_report_date=base_ts + timedelta(seconds=5),
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=5,
        )
    }

    assert sensor.available is True
    assert sensor.native_value is None
    assert sensor.extra_state_attributes["method"] == "seeded"


@pytest.mark.asyncio
async def test_site_lifetime_power_sensor_ignores_zeroed_previous_restore_history(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseGridPowerSensor(coord)
    sensor.hass = hass
    base_ts = datetime(2024, 1, 2, tzinfo=timezone.utc)

    class LastState:
        state = "-207236928"
        attributes = {
            "last_flow_kwh": {
                "grid_import": 1809.87,
                "grid_export": 19079.614,
            },
            "last_energy_ts": (base_ts + timedelta(minutes=5)).timestamp(),
            "last_sample_ts": (base_ts + timedelta(minutes=5)).timestamp(),
            "last_power_w": -207236928,
            "last_window_seconds": 300.0,
            "last_report_date": (base_ts + timedelta(minutes=5)).isoformat(),
        }

    class LastExtra:
        def as_dict(self):
            return {
                "previous_live_flow_kwh": {
                    "grid_import": 0.0,
                    "grid_export": 0.0,
                },
                "previous_live_energy_ts": base_ts.timestamp(),
                "previous_live_sample_ts": base_ts.timestamp(),
                "last_live_interval_minutes": 5.0,
                "last_live_flow_sources": {
                    "grid_import": ["import"],
                    "grid_export": ["solar_grid"],
                },
                "previous_live_flow_sources": {
                    "grid_import": ["import"],
                    "grid_export": ["solar_grid"],
                },
            }

    sensor.async_get_last_state = AsyncMock(return_value=LastState())
    sensor.async_get_last_extra_data = AsyncMock(return_value=LastExtra())
    await sensor.async_added_to_hass()

    assert sensor._restored_power_w is None

    coord.energy.site_energy = {
        "grid_import": SiteEnergyFlow(
            value_kwh=1809.87,
            bucket_count=1,
            fields_used=["import"],
            start_date="2024-01-02",
            last_report_date=base_ts + timedelta(minutes=5),
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=5,
        ),
        "grid_export": SiteEnergyFlow(
            value_kwh=19079.614,
            bucket_count=1,
            fields_used=["solar_grid"],
            start_date="2024-01-02",
            last_report_date=base_ts + timedelta(minutes=5),
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=5,
        ),
    }

    assert sensor.available is True
    assert sensor.native_value is None
    assert sensor.extra_state_attributes["method"] == "seeded"


@pytest.mark.asyncio
async def test_site_lifetime_power_sensor_discards_restored_power_when_extra_history_is_invalid(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseGridPowerSensor(coord)
    sensor.hass = hass
    base_ts = datetime(2024, 1, 2, tzinfo=timezone.utc)

    class LastState:
        state = "6000"
        attributes = {
            "last_flow_kwh": {"grid_import": 1.5},
            "last_energy_ts": (base_ts + timedelta(minutes=5)).timestamp(),
            "last_sample_ts": (base_ts + timedelta(minutes=5)).timestamp(),
            "last_power_w": 6000,
            "last_window_seconds": 300.0,
            "last_report_date": (base_ts + timedelta(minutes=5)).isoformat(),
        }

    class LastExtra:
        def as_dict(self):
            return {
                "previous_live_flow_kwh": {"grid_import": 1.0},
                "previous_live_energy_ts": (base_ts + timedelta(minutes=5)).timestamp(),
                "previous_live_sample_ts": (base_ts + timedelta(minutes=5)).timestamp(),
                "last_live_interval_minutes": 5.0,
            }

    sensor.async_get_last_state = AsyncMock(return_value=LastState())
    sensor.async_get_last_extra_data = AsyncMock(return_value=LastExtra())
    await sensor.async_added_to_hass()

    assert sensor._restored_power_w is None

    coord.energy.site_energy = {
        "grid_import": SiteEnergyFlow(
            value_kwh=1.5,
            bucket_count=1,
            fields_used=["import"],
            start_date="2024-01-02",
            last_report_date=base_ts + timedelta(minutes=5),
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=5,
        )
    }

    assert sensor.available is True
    assert sensor.native_value is None


def test_site_grid_power_sensor_ignores_implausible_outlier_but_advances_baseline(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseGridPowerSensor(coord)
    base_ts = datetime(2024, 1, 2, tzinfo=timezone.utc)

    coord.energy.site_energy = {
        "grid_export": SiteEnergyFlow(
            value_kwh=1.0,
            bucket_count=1,
            fields_used=["solar_grid"],
            start_date="2024-01-01",
            last_report_date=base_ts,
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=5,
        )
    }
    assert sensor.native_value is None

    coord.energy.site_energy["grid_export"] = SiteEnergyFlow(
        value_kwh=1.2,
        bucket_count=1,
        fields_used=["solar_grid"],
        start_date="2024-01-01",
        last_report_date=base_ts + timedelta(minutes=5),
        update_pending=False,
        source_unit="Wh",
        last_reset_at=None,
        interval_minutes=5,
    )
    assert sensor.native_value == -2400

    coord.energy.site_energy["grid_export"] = SiteEnergyFlow(
        value_kwh=11.2,
        bucket_count=1,
        fields_used=["solar_grid"],
        start_date="2024-01-01",
        last_report_date=base_ts + timedelta(minutes=10),
        update_pending=False,
        source_unit="Wh",
        last_reset_at=None,
        interval_minutes=5,
    )
    assert sensor.native_value == -2400
    assert sensor.extra_state_attributes["method"] == "outlier_ignored"
    assert sensor.extra_state_attributes["last_flow_kwh"] == {"grid_export": 11.2}

    coord.energy.site_energy["grid_export"] = SiteEnergyFlow(
        value_kwh=11.4,
        bucket_count=1,
        fields_used=["solar_grid"],
        start_date="2024-01-01",
        last_report_date=base_ts + timedelta(minutes=15),
        update_pending=False,
        source_unit="Wh",
        last_reset_at=None,
        interval_minutes=5,
    )
    assert sensor.native_value == -2400
    assert sensor.extra_state_attributes["method"] == "lifetime_energy_window"


def test_site_grid_power_sensor_recovers_after_sustained_high_power_post_reset(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseGridPowerSensor(coord)
    base_ts = datetime(2024, 1, 2, tzinfo=timezone.utc)

    coord.energy.site_energy = {
        "grid_import": SiteEnergyFlow(
            value_kwh=0.0,
            bucket_count=1,
            fields_used=["import"],
            start_date="2024-01-01",
            last_report_date=base_ts,
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=5,
        )
    }
    assert sensor.native_value is None

    latest_value = None
    for step in range(1, 8):
        coord.energy.site_energy["grid_import"] = SiteEnergyFlow(
            value_kwh=step * 10.0,
            bucket_count=1,
            fields_used=["import"],
            start_date="2024-01-01",
            last_report_date=base_ts + timedelta(minutes=step * 5),
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=5,
        )
        latest_value = sensor.native_value

    assert latest_value == 120000
    assert sensor.extra_state_attributes["method"] == "lifetime_energy_window"
    assert sensor.extra_state_attributes["last_flow_kwh"] == {"grid_import": 70.0}


@pytest.mark.asyncio
async def test_site_lifetime_power_sensor_discards_restored_outlier_live_history(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseGridPowerSensor(coord)
    sensor.hass = hass
    base_ts = datetime(2024, 1, 2, tzinfo=timezone.utc)

    class LastState:
        state = "-120000"
        attributes = {
            "last_flow_kwh": {"grid_export": 11.0},
            "last_energy_ts": (base_ts + timedelta(minutes=5)).timestamp(),
            "last_sample_ts": (base_ts + timedelta(minutes=5)).timestamp(),
            "last_power_w": -120000,
            "last_window_seconds": 300.0,
            "last_report_date": (base_ts + timedelta(minutes=5)).isoformat(),
        }

    class LastExtra:
        def as_dict(self):
            return {
                "previous_live_flow_kwh": {"grid_export": 1.0},
                "previous_live_energy_ts": base_ts.timestamp(),
                "previous_live_sample_ts": base_ts.timestamp(),
                "last_live_interval_minutes": 5.0,
                "last_live_flow_sources": {"grid_export": ["solar_grid"]},
                "previous_live_flow_sources": {"grid_export": ["solar_grid"]},
            }

    sensor.async_get_last_state = AsyncMock(return_value=LastState())
    sensor.async_get_last_extra_data = AsyncMock(return_value=LastExtra())
    await sensor.async_added_to_hass()

    assert sensor._restored_power_w is None
    assert sensor._live_flow_sample_count == 0
    assert sensor._last_sample_ts is None
    assert sensor._last_flow_kwh == {}

    coord.energy.site_energy = {
        "grid_export": SiteEnergyFlow(
            value_kwh=11.0,
            bucket_count=1,
            fields_used=["solar_grid"],
            start_date="2024-01-01",
            last_report_date=base_ts + timedelta(minutes=5),
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=5,
        )
    }

    assert sensor.native_value is None
    assert sensor.extra_state_attributes["method"] == "seeded"
    assert sensor.extra_state_attributes["last_flow_kwh"] == {"grid_export": 11.0}


@pytest.mark.asyncio
async def test_site_lifetime_power_sensor_discards_restored_extreme_power(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseGridPowerSensor(coord)
    sensor.hass = hass
    base_ts = datetime(2024, 1, 2, tzinfo=timezone.utc)

    class LastState:
        state = "1200000"
        attributes = {
            "last_flow_kwh": {"grid_import": 1100.0},
            "last_energy_ts": (base_ts + timedelta(minutes=5)).timestamp(),
            "last_sample_ts": (base_ts + timedelta(minutes=5)).timestamp(),
            "last_power_w": 1_200_000,
            "last_window_seconds": 300.0,
        }

    class LastExtra:
        def as_dict(self):
            return {
                "previous_live_flow_kwh": {"grid_import": 1000.0},
                "previous_live_energy_ts": base_ts.timestamp(),
                "previous_live_sample_ts": base_ts.timestamp(),
                "last_live_interval_minutes": 5.0,
                "last_live_flow_sources": {"grid_import": ["import"]},
                "previous_live_flow_sources": {"grid_import": ["import"]},
            }

    sensor.async_get_last_state = AsyncMock(return_value=LastState())
    sensor.async_get_last_extra_data = AsyncMock(return_value=LastExtra())

    await sensor.async_added_to_hass()

    assert sensor._restored_power_w is None
    assert sensor._last_flow_kwh == {}
    assert sensor._last_sample_ts is None


@pytest.mark.asyncio
async def test_site_lifetime_power_sensor_discards_restored_source_change(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseGridPowerSensor(coord)
    sensor.hass = hass
    base_ts = datetime(2024, 1, 2, tzinfo=timezone.utc)

    class LastState:
        state = "2400"
        attributes = {
            "last_flow_kwh": {"grid_import": 17_912.128},
            "last_energy_ts": (base_ts + timedelta(minutes=5)).timestamp(),
            "last_sample_ts": (base_ts + timedelta(minutes=5)).timestamp(),
            "last_power_w": 2400,
            "last_window_seconds": 300.0,
        }

    class LastExtra:
        def as_dict(self):
            return {
                "previous_live_flow_kwh": {"grid_import": 17_000.2},
                "previous_live_energy_ts": base_ts.timestamp(),
                "previous_live_sample_ts": base_ts.timestamp(),
                "last_live_interval_minutes": 5.0,
                "last_live_flow_sources": {
                    "grid_import": ["grid_home", "grid_battery"]
                },
                "previous_live_flow_sources": {"grid_import": ["import"]},
            }

    sensor.async_get_last_state = AsyncMock(return_value=LastState())
    sensor.async_get_last_extra_data = AsyncMock(return_value=LastExtra())

    await sensor.async_added_to_hass()

    assert sensor._restored_power_w is None
    assert sensor._last_flow_kwh == {}
    assert sensor._previous_live_flow_kwh == {}


def test_site_grid_power_sensor_accepts_large_plausible_site_delta(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseGridPowerSensor(coord)
    base_ts = datetime(2024, 1, 2, tzinfo=timezone.utc)

    coord.energy.site_energy = {
        "grid_import": SiteEnergyFlow(
            value_kwh=1000.0,
            bucket_count=1,
            fields_used=["import"],
            start_date="2024-01-01",
            last_report_date=base_ts,
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=5,
        )
    }
    assert sensor.native_value is None

    coord.energy.site_energy["grid_import"] = SiteEnergyFlow(
        value_kwh=1010.0,
        bucket_count=1,
        fields_used=["import"],
        start_date="2024-01-01",
        last_report_date=base_ts + timedelta(minutes=5),
        update_pending=False,
        source_unit="Wh",
        last_reset_at=None,
        interval_minutes=5,
    )

    assert sensor.native_value == 120000
    assert sensor.extra_state_attributes["method"] == "lifetime_energy_window"


def test_site_grid_power_quarantines_reported_multi_megawatt_spike(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseGridPowerSensor(coord)
    base_ts = datetime(2024, 1, 2, tzinfo=timezone.utc)

    def _export(value_kwh: float, minutes: int) -> SiteEnergyFlow:
        return SiteEnergyFlow(
            value_kwh=value_kwh,
            bucket_count=1433,
            fields_used=["solar_grid"],
            start_date="2022-08-10",
            last_report_date=base_ts + timedelta(minutes=minutes),
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=5,
        )

    coord.energy.site_energy = {"grid_export": _export(19_455.0, 0)}
    assert sensor.native_value is None

    coord.energy.site_energy["grid_export"] = _export(19_455.2, 5)
    assert sensor.native_value == -2400

    coord.energy.site_energy["grid_export"] = _export(20_455.2, 10)
    assert sensor.native_value == -2400
    assert sensor.extra_state_attributes["method"] == "extreme_pending"

    coord.energy.site_energy["grid_export"] = _export(20_455.4, 15)
    assert sensor.native_value == -2400
    assert sensor.extra_state_attributes["method"] == "lifetime_energy_window"


def test_site_grid_power_confirms_sustained_extreme_site_power(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseGridPowerSensor(coord)
    base_ts = datetime(2024, 1, 2, tzinfo=timezone.utc)

    def _import(value_kwh: float, minutes: int) -> SiteEnergyFlow:
        return SiteEnergyFlow(
            value_kwh=value_kwh,
            bucket_count=1,
            fields_used=["import"],
            start_date="2024-01-01",
            last_report_date=base_ts + timedelta(minutes=minutes),
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=5,
        )

    coord.energy.site_energy = {"grid_import": _import(1000.0, 0)}
    assert sensor.native_value is None

    coord.energy.site_energy["grid_import"] = _import(1100.0, 5)
    assert sensor.native_value is None
    assert sensor.extra_state_attributes["method"] == "extreme_pending"

    coord.energy.site_energy["grid_import"] = _import(1200.0, 10)
    assert sensor.native_value == 1_200_000
    assert sensor.extra_state_attributes["method"] == "extreme_confirmed"


def test_site_grid_power_does_not_confirm_extreme_without_source_timestamp(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseGridPowerSensor(coord)
    base_ts = datetime(2024, 1, 2, tzinfo=timezone.utc)

    def _import(value_kwh: float) -> SiteEnergyFlow:
        return SiteEnergyFlow(
            value_kwh=value_kwh,
            bucket_count=1,
            fields_used=["import"],
            start_date="2024-01-01",
            last_report_date=None,
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=5,
        )

    coord.last_success_utc = base_ts
    coord.energy.site_energy = {"grid_import": _import(1000.0)}
    assert sensor.native_value is None

    coord.last_success_utc = base_ts + timedelta(minutes=5)
    coord.energy.site_energy["grid_import"] = _import(1100.0)
    assert sensor.native_value is None
    assert sensor.extra_state_attributes["method"] == "extreme_pending"

    coord.last_success_utc = base_ts + timedelta(minutes=10)
    coord.energy.site_energy["grid_import"] = _import(1200.0)
    assert sensor.native_value == 0
    assert sensor.extra_state_attributes["method"] == "extreme_pending"
    assert sensor._extreme_power_validator.pending_count == 1  # noqa: SLF001


def test_site_grid_power_reseeds_when_cumulative_source_changes(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseGridPowerSensor(coord)
    base_ts = datetime(2024, 1, 2, tzinfo=timezone.utc)

    def _import(
        value_kwh: float, minutes: int, fields_used: list[str]
    ) -> SiteEnergyFlow:
        return SiteEnergyFlow(
            value_kwh=value_kwh,
            bucket_count=1433,
            fields_used=fields_used,
            start_date="2022-08-10",
            last_report_date=base_ts + timedelta(minutes=minutes),
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=5,
        )

    coord.energy.site_energy = {"grid_import": _import(17_000.0, 0, ["import"])}
    assert sensor.native_value is None

    coord.energy.site_energy["grid_import"] = _import(17_000.2, 5, ["import"])
    assert sensor.native_value == 2400

    coord.energy.site_energy["grid_import"] = _import(
        17_912.128, 10, ["grid_battery", "grid_home"]
    )
    assert sensor.native_value == 2400
    assert sensor.extra_state_attributes["method"] == "source_changed_reseed"

    coord.energy.site_energy["grid_import"] = _import(
        17_912.328, 15, ["grid_home", "grid_battery"]
    )
    assert sensor.native_value == 2400
    assert sensor.extra_state_attributes["method"] == "lifetime_energy_window"


def test_site_lifetime_power_plausibility_helper_handles_bad_scale_values(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseGridPowerSensor(coord)

    class BadFloat:
        def __float__(self):
            raise ValueError("boom")

    assert (
        sensor._power_sample_is_plausible(
            power_w=120000,
            signed_delta_kwh=10.0,
            current_values={"grid_import": BadFloat()},
            previous_values={},
        )
        is True
    )


def test_site_lifetime_power_normalizes_dict_flow_source_signatures(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseGridPowerSensor(coord)

    class BadString:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert sensor._current_flow_sources(  # noqa: SLF001
        {"grid_import": {"fields_used": ["import", "import", BadString()]}},
        {"grid_import": 1.0},
    ) == {"grid_import": ("import",)}


@pytest.mark.asyncio
async def test_site_lifetime_power_sensor_uses_restored_live_history_on_first_fresh_sample(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseGridPowerSensor(coord)
    sensor.hass = hass
    base_ts = datetime(2024, 1, 2, tzinfo=timezone.utc)

    class LastState:
        state = "6000"
        attributes = {
            "last_flow_kwh": {"grid_import": 1.5},
            "last_energy_ts": (base_ts + timedelta(minutes=5)).timestamp(),
            "last_sample_ts": (base_ts + timedelta(minutes=5)).timestamp(),
            "last_power_w": 6000,
            "last_window_seconds": 300.0,
            "last_report_date": (base_ts + timedelta(minutes=5)).isoformat(),
        }

    class LastExtra:
        def as_dict(self):
            return {
                "previous_live_flow_kwh": {"grid_import": 1.0},
                "previous_live_energy_ts": base_ts.timestamp(),
                "previous_live_sample_ts": base_ts.timestamp(),
                "last_live_flow_sources": {"grid_import": ["import"]},
                "previous_live_flow_sources": {"grid_import": ["import"]},
            }

    sensor.async_get_last_state = AsyncMock(return_value=LastState())
    sensor.async_get_last_extra_data = AsyncMock(return_value=LastExtra())
    await sensor.async_added_to_hass()

    coord.energy.site_energy = {
        "grid_import": SiteEnergyFlow(
            value_kwh=1.75,
            bucket_count=1,
            fields_used=["import"],
            start_date="2024-01-01",
            last_report_date=base_ts + timedelta(minutes=10),
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=5,
        )
    }

    assert sensor.native_value == 3000
    assert sensor.extra_state_attributes["method"] == "lifetime_energy_window"


def test_site_grid_power_sensor_rebases_missing_flow_to_zero_before_first_interval(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseGridPowerSensor(coord)
    previous_ts = datetime(2024, 1, 1, 23, 55, tzinfo=timezone.utc)
    zero_ts = datetime(2024, 1, 2, 0, 0, tzinfo=timezone.utc)
    first_positive_ts = datetime(2024, 1, 2, 0, 5, tzinfo=timezone.utc)

    sensor._last_flow_kwh = {"grid_export": 1.2}
    sensor._last_energy_ts = previous_ts.timestamp()
    sensor._last_sample_ts = previous_ts.timestamp()
    sensor._last_power_w = 900
    sensor._live_flow_sample_count = 1

    coord.energy.site_energy = {}
    coord.energy._site_energy_meta = {"last_report_date": zero_ts}  # noqa: SLF001
    assert sensor.native_value == 0
    assert sensor._last_flow_kwh == {"grid_export": 0.0}
    assert sensor.extra_state_attributes["method"] == "no_live_data"

    coord.energy.site_energy = {
        "grid_export": SiteEnergyFlow(
            value_kwh=0.2,
            bucket_count=1,
            fields_used=["solar_grid"],
            start_date="2024-01-02",
            last_report_date=first_positive_ts,
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=5,
        )
    }
    assert sensor.native_value == -2400
    assert sensor.extra_state_attributes["method"] == "lifetime_energy_window"


def test_site_battery_power_sensor_ignores_missing_component_flow_in_delta(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseBatteryPowerSensor(coord)
    base_ts = datetime(2024, 1, 2, tzinfo=timezone.utc)

    sensor._last_flow_kwh = {"battery_discharge": 1.0}
    sensor._last_energy_ts = base_ts.timestamp()
    sensor._last_sample_ts = base_ts.timestamp()
    sensor._live_flow_sample_count = 1

    coord.energy.site_energy = {
        "battery_discharge": SiteEnergyFlow(
            value_kwh=1.2,
            bucket_count=1,
            fields_used=["discharge"],
            start_date="2024-01-02",
            last_report_date=base_ts + timedelta(minutes=5),
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=5,
        )
    }

    assert sensor.native_value == 2400
    assert sensor.extra_state_attributes["method"] == "lifetime_energy_window"


@pytest.mark.asyncio
async def test_site_lifetime_power_sensor_restore_edge_cases(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseGridPowerSensor(coord)
    sensor.hass = hass

    class Boom:
        def __float__(self):
            raise ValueError("boom")

    class BadState:
        state = "invalid"
        attributes = {
            "last_flow_kwh": {"grid_import": Boom()},
            "last_energy_ts": Boom(),
            "last_sample_ts": Boom(),
            "last_power_w": Boom(),
            "last_window_seconds": Boom(),
            "last_reset_at": Boom(),
            "method": "",
            "last_report_date": " ",
        }

    sensor.async_get_last_state = AsyncMock(return_value=BadState())
    await sensor.async_added_to_hass()
    assert sensor._last_flow_kwh == {}
    assert sensor._last_energy_ts is None
    assert sensor._last_sample_ts is None
    assert sensor._restored_power_w is None
    assert sensor._last_window_s is None
    assert sensor._last_reset_at is None
    assert sensor.native_value is None

    coord.last_update_success = False
    coord.last_success_utc = None
    assert sensor.available is False

    sensor2 = EnphaseGridPowerSensor(coord)
    sensor2.hass = hass

    class AttrState:
        state = "bad"
        attributes = {"last_power_w": "321"}

    sensor2.async_get_last_state = AsyncMock(return_value=AttrState())
    await sensor2.async_added_to_hass()
    assert sensor2._last_power_w == 0
    assert sensor2._restored_power_w is None
    assert sensor2.native_value is None


def test_site_lifetime_power_sensor_helper_edge_cases(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseGridPowerSensor(coord)

    assert _lifetime_energy_delta(
        current_kwh=1.0,
        previous_kwh=None,
        reset_drop_kwh=sensor._RESET_DROP_KWH,
    ) == (None, False)
    assert sensor._coerce_flow_value({"value_kwh": "bad"}) is None
    assert sensor._coerce_flow_value({"value_kwh": -1}) is None
    assert sensor._parse_sample_timestamp(None) is None
    assert sensor._parse_sample_timestamp(0) is None
    assert sensor._parse_sample_timestamp(1_700_000_000_000) == 1_700_000_000
    assert sensor._parse_sample_timestamp(datetime(2024, 1, 1, 0, 0, 0)) is not None
    assert (
        sensor._parse_sample_timestamp("2024-01-01T00:00:00")
        == datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
    )
    assert sensor._parse_sample_timestamp("") is None
    assert sensor._parse_sample_timestamp("1700000000000") == 1_700_000_000
    assert sensor._parse_sample_timestamp([]) is None

    with monkeypatch.context() as m:
        m.setattr(
            "custom_components.enphase_ev.sensor.dt_util.parse_datetime",
            lambda _v: None,
        )
        m.setattr(
            "custom_components.enphase_ev.sensor.dt_util.parse_date",
            lambda _v: datetime(2024, 1, 3, tzinfo=timezone.utc).date(),
        )
        assert (
            sensor._parse_sample_timestamp("2024/01/03")
            == datetime(2024, 1, 3, tzinfo=timezone.utc).timestamp()
        )

    with monkeypatch.context() as m:
        m.setattr(
            "custom_components.enphase_ev.sensor.dt_util.parse_datetime",
            lambda _v: None,
        )
        m.setattr(
            "custom_components.enphase_ev.sensor.dt_util.parse_date",
            lambda _v: (_ for _ in ()).throw(ValueError("bad date")),
        )
        assert sensor._parse_sample_timestamp("not-a-date") is None

    coord.energy = None  # type: ignore[assignment]
    coord.site_energy_meta = "bad"  # type: ignore[assignment]
    assert sensor._site_energy_meta() == {}
    coord.site_energy_meta = {"last_report_date": 1_700_000_100}  # type: ignore[assignment]
    ts, iso = sensor._sample_timestamp({})
    assert ts == 1_700_000_100
    assert iso is not None

    coord.site_energy_meta = {}  # type: ignore[assignment]
    coord.last_success_utc = datetime.fromtimestamp(1_700_000_200, tz=timezone.utc)
    ts, _ = sensor._sample_timestamp({})
    assert ts == 1_700_000_200

    coord.energy = None  # type: ignore[assignment]
    coord.discovery_snapshot.site_energy_channel_known = (  # type: ignore[assignment]
        lambda flow_key: flow_key == "grid_export"
    )
    export_sensor = EnphaseGridPowerSensor(coord)
    assert export_sensor._flow_supported("grid_export") is True
    assert export_sensor._current_flow_values() == (
        {"grid_export": 0.0},
        {"grid_export"},
    )
    coord.energy = SimpleNamespace(site_energy={"grid_export": {"value_kwh": 1.0}})
    assert export_sensor._flow_supported("grid_export") is True

    coord.energy = None  # type: ignore[assignment]

    def _raise_known(_flow_key: str) -> bool:
        raise RuntimeError("boom")

    coord.discovery_snapshot.site_energy_channel_known = _raise_known  # type: ignore[assignment]
    coord.site_energy_meta = {"bucket_lengths": {"solar_grid": "yes"}}  # type: ignore[assignment]
    assert export_sensor._flow_supported("grid_export") is True
    coord.site_energy_meta = {"bucket_lengths": {"solar_grid": 0}}  # type: ignore[assignment]
    assert export_sensor._flow_supported("grid_export") is False

    with monkeypatch.context() as m:
        m.setattr(
            "custom_components.enphase_ev.sensor.dt_util.utcnow",
            lambda: datetime(2024, 1, 4, 0, 0, 0),
        )
        coord.last_success_utc = None
        ts, iso = sensor._sample_timestamp({})
        assert ts == datetime(2024, 1, 4, tzinfo=timezone.utc).timestamp()
        assert iso is not None

    flows = {
        "grid_import": {
            "value_kwh": 1.0,
            "last_report_date": "2024-01-05T00:00:00+00:00",
        }
    }
    ts, _ = sensor._sample_timestamp(flows)
    assert ts == datetime(2024, 1, 5, tzinfo=timezone.utc).timestamp()
    assert sensor._minimum_window_seconds(flows, {"grid_import": 1.0}) is None

    flows["grid_import"]["interval_minutes"] = 5
    assert sensor._minimum_window_seconds(flows, {"grid_import": 1.0}) == pytest.approx(
        300.0
    )

    coord.site_energy_meta = {"interval_minutes": 15}  # type: ignore[assignment]
    assert sensor._minimum_window_seconds({}, {}) == pytest.approx(900.0)

    battery_sensor = EnphaseBatteryPowerSensor(coordinator_factory())
    battery_sensor._last_flow_kwh = {"battery_discharge": 1.0}
    battery_sensor._last_energy_ts = datetime(
        2024, 1, 6, tzinfo=timezone.utc
    ).timestamp()
    battery_sensor._live_flow_sample_count = 1
    battery_sensor._DEFAULT_WINDOW_S = 0
    battery_sensor._coord.energy.site_energy = {
        "battery_charge": SiteEnergyFlow(
            value_kwh=0.5,
            bucket_count=1,
            fields_used=["charge"],
            start_date="2024-01-01",
            last_report_date=datetime(2024, 1, 6, tzinfo=timezone.utc),
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=5,
        )
    }
    assert battery_sensor.native_value == 0


def test_site_lifetime_power_restore_data_helper_edges() -> None:
    restore_data = _SiteLifetimePowerRestoreData.from_dict(None)
    assert restore_data.previous_live_flow_kwh == {}
    assert restore_data.previous_live_energy_ts is None
    assert restore_data.previous_live_sample_ts is None
    assert restore_data.last_live_interval_minutes is None

    parsed = _SiteLifetimePowerRestoreData.from_dict(
        {
            "previous_live_flow_kwh": {
                "grid_import": "1.5",
                123: "2.0",
                "grid_export": object(),
                "bad_negative": -1,
            },
            "previous_live_energy_ts": object(),
            "previous_live_sample_ts": "1700000000",
            "last_live_interval_minutes": "5",
            "last_live_flow_sources": {
                "grid_import": ["grid_home", "grid_battery", "grid_home"],
                123: ["ignored"],
            },
            "previous_live_flow_sources": {"grid_import": ["import"]},
        }
    )
    assert parsed.previous_live_flow_kwh == {"grid_import": 1.5}
    assert parsed.previous_live_energy_ts is None
    assert parsed.previous_live_sample_ts == pytest.approx(1_700_000_000.0)
    assert parsed.last_live_interval_minutes == pytest.approx(5.0)
    assert parsed.last_live_flow_sources == {
        "grid_import": ("grid_battery", "grid_home")
    }
    assert parsed.previous_live_flow_sources == {"grid_import": ("import",)}
    assert parsed.as_dict() == {
        "previous_live_flow_kwh": {"grid_import": 1.5},
        "previous_live_energy_ts": None,
        "previous_live_sample_ts": pytest.approx(1_700_000_000.0),
        "last_live_interval_minutes": pytest.approx(5.0),
        "last_live_flow_sources": {"grid_import": ["grid_battery", "grid_home"]},
        "previous_live_flow_sources": {"grid_import": ["import"]},
    }


@pytest.mark.asyncio
async def test_site_lifetime_power_sensor_restored_history_handles_no_change_and_reset(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory()
    sensor = EnphaseGridPowerSensor(coord)
    sensor.hass = hass
    base_ts = datetime(2024, 1, 2, tzinfo=timezone.utc)

    class NoChangeState:
        state = "0"
        attributes = {
            "last_flow_kwh": {"grid_import": 1.5},
            "last_energy_ts": (base_ts + timedelta(minutes=5)).timestamp(),
            "last_sample_ts": (base_ts + timedelta(minutes=5)).timestamp(),
        }

    class NoChangeExtra:
        def as_dict(self):
            return {
                "previous_live_flow_kwh": {"grid_import": 1.5},
                "previous_live_energy_ts": base_ts.timestamp(),
                "previous_live_sample_ts": base_ts.timestamp(),
                "last_live_flow_sources": {"grid_import": ["import"]},
                "previous_live_flow_sources": {"grid_import": ["import"]},
            }

    sensor.async_get_last_state = AsyncMock(return_value=NoChangeState())
    sensor.async_get_last_extra_data = AsyncMock(return_value=NoChangeExtra())
    await sensor.async_added_to_hass()

    assert sensor._last_method == "restored_no_change"
    assert sensor._last_power_w == 0
    extra = sensor.extra_restore_state_data
    assert extra is not None
    assert extra.as_dict()["previous_live_flow_kwh"] == {"grid_import": 1.5}

    reset_sensor = EnphaseGridPowerSensor(coord)
    reset_sensor.hass = hass

    class ResetState:
        state = "0"
        attributes = {
            "last_flow_kwh": {"grid_import": 1.0},
            "last_energy_ts": (base_ts + timedelta(minutes=10)).timestamp(),
            "last_sample_ts": (base_ts + timedelta(minutes=10)).timestamp(),
        }

    class ResetExtra:
        def as_dict(self):
            return {
                "previous_live_flow_kwh": {"grid_import": 2.0},
                "previous_live_energy_ts": (base_ts + timedelta(minutes=5)).timestamp(),
                "previous_live_sample_ts": (base_ts + timedelta(minutes=5)).timestamp(),
                "last_live_flow_sources": {"grid_import": ["import"]},
                "previous_live_flow_sources": {"grid_import": ["import"]},
            }

    reset_sensor.async_get_last_state = AsyncMock(return_value=ResetState())
    reset_sensor.async_get_last_extra_data = AsyncMock(return_value=ResetExtra())
    await reset_sensor.async_added_to_hass()

    assert reset_sensor._last_method == "restored_lifetime_reset"
    assert reset_sensor._last_power_w == 0
    assert reset_sensor._last_reset_at == pytest.approx(
        (base_ts + timedelta(minutes=10)).timestamp()
    )

    zeroed_reset_sensor = EnphaseGridPowerSensor(coord)
    zeroed_reset_sensor.hass = hass

    class ZeroedResetState:
        state = "0"
        attributes = {
            "last_flow_kwh": {"grid_import": 0.0},
            "last_energy_ts": (base_ts + timedelta(minutes=10)).timestamp(),
            "last_sample_ts": (base_ts + timedelta(minutes=10)).timestamp(),
            "method": "lifetime_reset",
        }

    class ZeroedResetExtra:
        def as_dict(self):
            return {
                "previous_live_flow_kwh": {"grid_import": 2.0},
                "previous_live_energy_ts": (base_ts + timedelta(minutes=5)).timestamp(),
                "previous_live_sample_ts": (base_ts + timedelta(minutes=5)).timestamp(),
                "last_live_flow_sources": {"grid_import": ["import"]},
                "previous_live_flow_sources": {"grid_import": ["import"]},
            }

    zeroed_reset_sensor.async_get_last_state = AsyncMock(
        return_value=ZeroedResetState()
    )
    zeroed_reset_sensor.async_get_last_extra_data = AsyncMock(
        return_value=ZeroedResetExtra()
    )
    await zeroed_reset_sensor.async_added_to_hass()

    assert zeroed_reset_sensor._previous_live_flow_kwh == {}
    assert zeroed_reset_sensor._previous_live_energy_ts is None
    assert zeroed_reset_sensor._previous_live_sample_ts is None


@pytest.mark.asyncio
async def test_site_energy_sensor_restoration(monkeypatch, hass, coordinator_factory):
    coord = coordinator_factory()
    sensor = EnphaseSiteEnergySensor(
        coord, "grid_import", "site_grid_import", "Grid Import"
    )
    sensor.hass = hass

    class LastData:
        native_value = "2.5"

    sensor.async_get_last_sensor_data = AsyncMock(return_value=LastData())

    class LastState:
        attributes = {"last_reset_at": "2024-02-01T00:00:00+00:00"}

    sensor.async_get_last_state = AsyncMock(return_value=LastState())
    await sensor.async_added_to_hass()
    assert sensor.native_value == pytest.approx(2.5)
    assert sensor.available is True
    assert sensor.extra_state_attributes["last_reset_at"].startswith("2024-02-01")

    # flow data as dict should be returned verbatim
    coord.energy.site_energy = {"grid_import": {"value_kwh": "bad"}}
    assert sensor._flow_data() == {"value_kwh": "bad"}
    assert sensor._current_value() is None
    sensor._restored_value = 1.0
    coord.energy.site_energy = {}
    attrs = sensor.extra_state_attributes
    assert "last_report_date" not in attrs
    assert "sampled_at_utc" not in attrs
    assert sensor.native_value == 1.0

    coord.energy.site_energy = {
        "grid_import": {"value_kwh": 3.0, "last_report_date": "soon"}
    }
    assert "last_report_date" not in sensor.extra_state_attributes
    assert "sampled_at_utc" not in sensor.extra_state_attributes

    sensor2 = EnphaseSiteEnergySensor(
        coord, "grid_export", "site_grid_export", "Grid Export"
    )
    sensor2.hass = hass
    sensor2.async_get_last_sensor_data = AsyncMock(return_value=None)
    sensor2.async_get_last_state = AsyncMock(side_effect=RuntimeError("boom"))
    sensor2.async_get_last_extra_data = AsyncMock(return_value=None)
    await sensor2.async_added_to_hass()
    assert sensor2.native_value is None
    assert "composition_offset_kwh" not in sensor2.extra_state_attributes
    assert sensor2.extra_restore_state_data is not None
    assert (
        sensor2.extra_restore_state_data.as_dict()["composition_migration_checked"]
        is False
    )

    class BadLastData:
        @property
        def native_value(self):
            raise ValueError("boom")

    sensor3 = EnphaseSiteEnergySensor(
        coord, "battery_charge", "site_battery_charge", "Battery Charge"
    )
    sensor3.hass = hass
    sensor3.async_get_last_sensor_data = AsyncMock(return_value=BadLastData())
    sensor3.async_get_last_state = AsyncMock(return_value=None)
    sensor3.async_get_last_extra_data = AsyncMock(return_value=None)
    await sensor3.async_added_to_hass()
    sensor3._restored_value = 1.5
    assert sensor3.available is True

    class BadFlow(SiteEnergyFlow):
        def __getattribute__(self, name):
            if name == "__dict__":
                raise RuntimeError("no dict")
            return super().__getattribute__(name)

    coord.energy.site_energy = {
        "battery_charge": BadFlow(None, 0, [], None, None, None)
    }
    assert sensor3._flow_data() == {
        "value_kwh": None,
        "bucket_count": 0,
        "fields_used": [],
        "start_date": None,
        "last_report_date": None,
        "update_pending": None,
        "source_unit": UnitOfPower.WATT,
        "last_reset_at": None,
        "interval_minutes": None,
        "legacy_offset_kwh": 0.0,
    }

    class BadStr:
        def __str__(self):
            raise ValueError("bad str")

    coord.energy.site_energy = {
        "battery_charge": {"value_kwh": 1.0, "last_report_date": BadStr()}
    }
    attrs = sensor3.extra_state_attributes
    assert "last_report_date" not in attrs


@pytest.mark.asyncio
async def test_grid_export_sensor_preserves_statistics_across_composition_migration(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory()
    coord.energy.site_energy = {
        "grid_export": SiteEnergyFlow(
            value_kwh=26.105,
            bucket_count=2,
            fields_used=["solar_grid", "battery_grid"],
            start_date="2023-08-10",
            last_report_date=datetime(2026, 8, 2, tzinfo=timezone.utc),
            update_pending=False,
            source_unit="Wh",
            interval_minutes=60,
            legacy_offset_kwh=16.18,
        )
    }
    sensor = EnphaseSiteEnergySensor(
        coord, "grid_export", "site_grid_export", "Grid Export"
    )
    sensor.hass = hass

    class LastData:
        native_value = 9.9

    class LastState:
        attributes = {}

    sensor.async_get_last_sensor_data = AsyncMock(return_value=LastData())
    sensor.async_get_last_state = AsyncMock(return_value=LastState())
    sensor.async_get_last_extra_data = AsyncMock(return_value=None)
    await sensor.async_added_to_hass()

    assert sensor.native_value == pytest.approx(9.93)
    restore_data = sensor.extra_restore_state_data
    assert restore_data is not None
    assert restore_data.as_dict()["composition_offset_kwh"] == pytest.approx(16.18)

    coord.energy.site_energy["grid_export"] = SiteEnergyFlow(
        value_kwh=26.28,
        bucket_count=2,
        fields_used=["solar_grid", "battery_grid"],
        start_date="2023-08-10",
        last_report_date=datetime(2026, 8, 2, 1, tzinfo=timezone.utc),
        update_pending=False,
        source_unit="Wh",
        interval_minutes=60,
        legacy_offset_kwh=16.255,
    )

    assert sensor.native_value == pytest.approx(10.1)
    restore_data = sensor.extra_restore_state_data
    assert restore_data is not None
    assert restore_data.as_dict()["composition_offset_kwh"] == pytest.approx(16.18)

    coord.energy.site_energy["grid_export"] = SiteEnergyFlow(
        value_kwh=1.0,
        bucket_count=2,
        fields_used=["solar_grid", "battery_grid"],
        start_date="2026-08-03",
        last_report_date=datetime(2026, 8, 3, tzinfo=timezone.utc),
        update_pending=False,
        source_unit="Wh",
        last_reset_at="2026-08-03T00:00:00+00:00",
        interval_minutes=60,
        legacy_offset_kwh=0.7,
    )

    assert sensor.native_value == pytest.approx(1.0)
    restore_data = sensor.extra_restore_state_data
    assert restore_data is not None
    assert restore_data.as_dict()["composition_offset_kwh"] == 0.0


@pytest.mark.asyncio
async def test_grid_export_sensor_restores_composition_offset(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory()
    coord.energy.site_energy = {
        "grid_export": SiteEnergyFlow(
            value_kwh=26.28,
            bucket_count=2,
            fields_used=["solar_grid", "battery_grid"],
            start_date="2023-08-10",
            last_report_date=datetime(2026, 8, 2, 1, tzinfo=timezone.utc),
            update_pending=False,
            source_unit="Wh",
            interval_minutes=60,
            legacy_offset_kwh=16.255,
        )
    }
    sensor = EnphaseSiteEnergySensor(
        coord, "grid_export", "site_grid_export", "Grid Export"
    )
    sensor.hass = hass

    class LastData:
        native_value = 10.1

    class LastState:
        attributes = {}

    class LastExtra:
        def as_dict(self):
            return {
                "composition_offset_kwh": "16.18",
                "composition_offset_reset_at": None,
                "composition_source_start_date": "2023-08-10",
                "composition_migration_checked": True,
            }

    sensor.async_get_last_sensor_data = AsyncMock(return_value=LastData())
    sensor.async_get_last_state = AsyncMock(return_value=LastState())
    sensor.async_get_last_extra_data = AsyncMock(return_value=LastExtra())
    await sensor.async_added_to_hass()

    assert sensor.native_value == pytest.approx(10.1)
    restore_data = sensor.extra_restore_state_data
    assert restore_data is not None
    assert restore_data.as_dict()["composition_offset_kwh"] == pytest.approx(16.18)


@pytest.mark.asyncio
async def test_grid_export_sensor_does_not_offset_migrated_composite_history(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory()
    coord.energy.site_energy = {
        "grid_export": SiteEnergyFlow(
            value_kwh=26.105,
            bucket_count=2,
            fields_used=["solar_grid", "battery_grid"],
            start_date="2023-08-10",
            last_report_date=datetime(2026, 8, 2, tzinfo=timezone.utc),
            update_pending=False,
            source_unit="Wh",
            interval_minutes=60,
            legacy_offset_kwh=16.18,
        )
    }
    sensor = EnphaseSiteEnergySensor(
        coord, "grid_export", "site_grid_export", "Grid Export"
    )
    sensor.hass = hass

    class LastData:
        native_value = 26.0

    class LastState:
        attributes = {}

    sensor.async_get_last_sensor_data = AsyncMock(return_value=LastData())
    sensor.async_get_last_state = AsyncMock(return_value=LastState())
    sensor.async_get_last_extra_data = AsyncMock(return_value=None)
    await sensor.async_added_to_hass()

    assert sensor.native_value == pytest.approx(26.11)
    restore_data = sensor.extra_restore_state_data
    assert restore_data is not None
    assert restore_data.as_dict()["composition_offset_kwh"] == 0.0


@pytest.mark.asyncio
async def test_grid_export_sensor_clears_offset_after_offline_lifetime_reset(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory()
    coord.energy.site_energy = {
        "grid_export": SiteEnergyFlow(
            value_kwh=1.0,
            bucket_count=2,
            fields_used=["solar_grid", "battery_grid"],
            start_date="2026-08-03",
            last_report_date=datetime(2026, 8, 3, tzinfo=timezone.utc),
            update_pending=False,
            source_unit="Wh",
            interval_minutes=60,
            legacy_offset_kwh=0.7,
        )
    }
    sensor = EnphaseSiteEnergySensor(
        coord, "grid_export", "site_grid_export", "Grid Export"
    )
    sensor.hass = hass

    class LastData:
        native_value = 10.1

    class LastState:
        attributes = {}

    class LastExtra:
        def as_dict(self):
            return {
                "composition_offset_kwh": 16.18,
                "composition_offset_reset_at": None,
                "composition_source_start_date": "2023-08-10",
                "composition_migration_checked": True,
            }

    sensor.async_get_last_sensor_data = AsyncMock(return_value=LastData())
    sensor.async_get_last_state = AsyncMock(return_value=LastState())
    sensor.async_get_last_extra_data = AsyncMock(return_value=LastExtra())
    await sensor.async_added_to_hass()

    assert sensor.native_value == pytest.approx(1.0)
    restore_data = sensor.extra_restore_state_data
    assert restore_data is not None
    assert restore_data.as_dict() == {
        "native_value": 1.0,
        "native_unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
        "composition_offset_kwh": 0.0,
        "composition_offset_reset_at": None,
        "composition_source_start_date": "2026-08-03",
        "composition_migration_checked": True,
    }


@pytest.mark.asyncio
async def test_new_grid_export_sensor_uses_full_composite_total(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory()
    coord.energy.site_energy = {
        "grid_export": SiteEnergyFlow(
            value_kwh=26.105,
            bucket_count=2,
            fields_used=["solar_grid", "battery_grid"],
            start_date="2023-08-10",
            last_report_date=datetime(2026, 8, 2, tzinfo=timezone.utc),
            update_pending=False,
            source_unit="Wh",
            interval_minutes=60,
            legacy_offset_kwh=16.18,
        )
    }
    sensor = EnphaseSiteEnergySensor(
        coord, "grid_export", "site_grid_export", "Grid Export"
    )
    sensor.hass = hass
    sensor.async_get_last_sensor_data = AsyncMock(return_value=None)
    sensor.async_get_last_state = AsyncMock(return_value=None)
    sensor.async_get_last_extra_data = AsyncMock(return_value=None)

    await sensor.async_added_to_hass()

    assert sensor.native_value == pytest.approx(26.11)
    restore_data = sensor.extra_restore_state_data
    assert restore_data is not None
    assert restore_data.as_dict() == {
        "native_value": 26.11,
        "native_unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
        "composition_offset_kwh": 0.0,
        "composition_offset_reset_at": None,
        "composition_source_start_date": "2023-08-10",
        "composition_migration_checked": True,
    }
    assert "composition_offset_kwh" not in sensor.extra_state_attributes
    assert sensor._coerce_nonnegative_float("bad") is None  # noqa: SLF001
    assert sensor._coerce_nonnegative_float(-1) is None  # noqa: SLF001
    assert sensor._coerce_nonnegative_float(float("inf")) is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_grid_export_zero_channels_finalize_migration_before_first_export(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory()
    zero_flows, _meta = coord.energy._aggregate_site_energy(  # noqa: SLF001
        {"solar_grid": [0, 0], "battery_grid": [0, 0]}
    )
    coord.energy.site_energy = zero_flows
    sensor = EnphaseSiteEnergySensor(
        coord, "grid_export", "site_grid_export", "Grid Export"
    )
    sensor.hass = hass

    class LastData:
        native_value = 5.0

    class LastState:
        attributes = {}

    sensor.async_get_last_sensor_data = AsyncMock(return_value=LastData())
    sensor.async_get_last_state = AsyncMock(return_value=LastState())
    sensor.async_get_last_extra_data = AsyncMock(return_value=None)
    await sensor.async_added_to_hass()

    assert sensor.native_value == 0.0
    restore_data = sensor.extra_restore_state_data
    assert restore_data is not None
    assert restore_data.as_dict()["composition_migration_checked"] is True

    export_flows, _meta = coord.energy._aggregate_site_energy(  # noqa: SLF001
        {"solar_grid": [0, 0], "battery_grid": [8000, 8180]}
    )
    coord.energy.site_energy = export_flows

    assert sensor.native_value == pytest.approx(16.18)


@pytest.mark.asyncio
async def test_site_energy_sensor_restore_data_round_trip(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory()
    source = EnphaseSiteEnergySensor(
        coord, "grid_export", "site_grid_export", "Grid Export"
    )
    source.hass = hass
    source._restored_value = 10.1  # noqa: SLF001
    source._composition_offset_kwh = 16.18  # noqa: SLF001
    source._composition_source_start_date = "2023-08-10"  # noqa: SLF001
    source._composition_migration_checked = True  # noqa: SLF001

    stored = source.extra_restore_state_data
    assert stored is not None
    assert stored.as_dict() == {
        "native_value": 10.1,
        "native_unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
        "composition_offset_kwh": 16.18,
        "composition_offset_reset_at": None,
        "composition_source_start_date": "2023-08-10",
        "composition_migration_checked": True,
    }

    restored = EnphaseSiteEnergySensor(
        coord, "grid_export", "site_grid_export", "Grid Export"
    )
    restored.hass = hass
    restored.async_get_last_extra_data = AsyncMock(return_value=stored)
    restored.async_get_last_state = AsyncMock(return_value=None)

    await restored.async_added_to_hass()

    assert restored.native_value == pytest.approx(10.1)
    assert restored._composition_offset_kwh == pytest.approx(16.18)  # noqa: SLF001
    assert restored.async_get_last_extra_data.await_count == 2

    non_export = EnphaseSiteEnergySensor(
        coord, "grid_import", "site_grid_import", "Grid Import"
    )
    non_export._restored_value = 2.5  # noqa: SLF001
    non_export_stored = non_export.extra_restore_state_data
    assert non_export_stored is not None
    assert non_export_stored.as_dict() == {
        "native_value": 2.5,
        "native_unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
    }


def test_site_energy_restore_data_handles_invalid_payloads() -> None:
    assert _SiteEnergyRestoreData.from_dict(None).as_dict() == {
        "native_value": None,
        "native_unit_of_measurement": None,
        "composition_offset_kwh": 0.0,
        "composition_offset_reset_at": None,
        "composition_source_start_date": None,
        "composition_migration_checked": False,
    }
    assert _SiteEnergyRestoreData.from_dict(
        {
            "composition_offset_kwh": "bad",
            "composition_offset_reset_at": 123,
            "composition_source_start_date": 456,
            "composition_migration_checked": "true",
        }
    ).as_dict() == {
        "native_value": None,
        "native_unit_of_measurement": None,
        "composition_offset_kwh": 0.0,
        "composition_offset_reset_at": None,
        "composition_source_start_date": None,
        "composition_migration_checked": False,
    }
    assert (
        _SiteEnergyRestoreData.from_dict(
            {
                "composition_offset_kwh": -1,
                "composition_offset_reset_at": "2026-08-03T00:00:00+00:00",
                "composition_migration_checked": True,
            }
        ).composition_offset_kwh
        == 0.0
    )
    assert (
        _SiteEnergyRestoreData.from_dict(
            {"composition_offset_kwh": float("nan")}
        ).composition_offset_kwh
        == 0.0
    )


@pytest.mark.asyncio
async def test_site_lifetime_power_sensor_async_added_to_hass_without_restore_state(
    hass, coordinator_factory
):
    coord = coordinator_factory()
    sensor = EnphaseGridPowerSensor(coord)
    sensor.hass = hass
    sensor.async_get_last_state = AsyncMock(return_value=None)

    await sensor.async_added_to_hass()

    assert sensor._last_flow_kwh == {}
    assert sensor._restored_power_w is None


@pytest.mark.asyncio
async def test_site_energy_sensor_available_follows_super(hass, coordinator_factory):
    coord = coordinator_factory()
    coord.last_update_success = False
    coord.last_success_utc = None
    coord.energy.site_energy = {}
    sensor = EnphaseSiteEnergySensor(
        coord, "grid_import", "site_grid_import", "Grid Import"
    )
    sensor.hass = hass
    sensor._restored_value = None
    assert sensor.available is False


def test_site_energy_sensor_device_info_targets_cloud(coordinator_factory) -> None:
    coord = coordinator_factory()
    sensor = EnphaseSiteEnergySensor(
        coord, "grid_import", "site_grid_import", "Grid Import"
    )
    info = sensor.device_info
    assert info["identifiers"] == {("enphase_ev", f"type:{coord.site_id}:cloud")}


def test_site_energy_sensor_device_info_uses_coordinator_cloud_info(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    provided = {"identifiers": {("enphase_ev", "type:site-provided:cloud")}}
    coord.inventory_view.type_device_info = lambda key: (
        provided if key == "cloud" else None
    )  # type: ignore[assignment]
    sensor = EnphaseSiteEnergySensor(
        coord, "grid_import", "site_grid_import", "Grid Import"
    )
    assert sensor.device_info is provided


def test_site_energy_sensor_available_not_gated_by_envoy(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.inventory_view.has_type_for_entities = lambda _type_key: False
    coord.last_update_success = True
    coord.last_success_utc = None
    coord.energy.site_energy = {"grid_import": {"value_kwh": 1.0}}
    sensor = EnphaseSiteEnergySensor(
        coord, "grid_import", "site_grid_import", "Grid Import"
    )
    assert sensor.available is True


def test_site_grid_power_exposes_sampled_at_utc(coordinator_factory):
    coord = coordinator_factory()
    base_ts = datetime(2024, 1, 2, tzinfo=timezone.utc)
    sensor = EnphaseGridPowerSensor(coord)
    coord.energy.site_energy = {
        "grid_import": SiteEnergyFlow(
            value_kwh=1.0,
            bucket_count=1,
            fields_used=["grid_import"],
            start_date="2024-01-02",
            last_report_date=base_ts,
            interval_minutes=5,
            update_pending=False,
        ),
        "grid_export": SiteEnergyFlow(
            value_kwh=0.0,
            bucket_count=1,
            fields_used=["grid_export"],
            start_date="2024-01-02",
            last_report_date=base_ts,
            interval_minutes=5,
            update_pending=False,
        ),
    }

    sensor.native_value

    assert sensor.extra_state_attributes["sampled_at_utc"] == base_ts.isoformat()


@pytest.mark.asyncio
async def test_site_energy_sensor_evse_attribute_edge_cases(hass, coordinator_factory):
    coord = coordinator_factory()
    coord.energy.site_energy = {
        "grid_import": SiteEnergyFlow(
            value_kwh=1.0,
            bucket_count=1,
            fields_used=["grid_home"],
            start_date="2024-01-01",
            last_report_date=datetime(2024, 1, 2, tzinfo=timezone.utc),
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=60,
        ),
        "evse_charging": {"value_kwh": "bad"},
    }

    sensor = EnphaseSiteEnergySensor(
        coord, "grid_import", "site_grid_import", "Grid Import"
    )
    sensor.hass = hass
    attrs = sensor.extra_state_attributes
    assert "evse_charging_kwh" not in attrs

    class BadEnergy:
        @property
        def site_energy(self):
            raise RuntimeError("boom")

    sensor._flow_data = lambda: {}  # type: ignore[method-assign]
    coord.energy = BadEnergy()  # type: ignore[assignment]
    attrs = sensor.extra_state_attributes
    assert "evse_charging_kwh" not in attrs


@pytest.mark.asyncio
async def test_site_energy_direct_arrays(monkeypatch, coordinator_factory):
    coord = coordinator_factory()
    coord.client.lifetime_energy = AsyncMock(
        return_value={
            "consumption": [200],
            "solar_home": [50],
            "solar_grid": [75],
            "charge": [25],
            "discharge": [10],
            "evse": [12],
            "start_date": "2024-01-01",
            "last_report_date": 1_700_000_000,
            "interval_minutes": 60,
        }
    )
    await coord.energy._async_refresh_site_energy()  # noqa: SLF001
    assert set(coord.energy.site_energy) == {
        "consumption",
        "grid_import",
        "grid_export",
        "battery_charge",
        "battery_discharge",
        "evse_charging",
    }


@pytest.mark.asyncio
async def test_site_energy_refresh_merges_missing_channels_from_hems(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client._hems_site_supported = True  # noqa: SLF001
    coord.client.lifetime_energy = AsyncMock(
        return_value={
            "production": [1000],
            "evse": [],
            "heatpump": [],
            "water_heater": [],
            "start_date": "2024-01-01",
            "interval_minutes": 60,
        }
    )
    coord.client.hems_consumption_lifetime = AsyncMock(
        return_value={
            "evse": [125],
            "heatpump": [250],
            "water_heater": [375],
            "last_report_date": 1_700_000_000,
            "interval_minutes": 60,
        }
    )

    await coord.energy._async_refresh_site_energy()  # noqa: SLF001

    assert coord.client.hems_consumption_lifetime.await_count == 1
    assert coord.energy.site_energy["evse_charging"].value_kwh == pytest.approx(0.125)
    assert coord.energy.site_energy["heat_pump"].value_kwh == pytest.approx(0.25)
    assert coord.energy.site_energy["water_heater"].value_kwh == pytest.approx(0.375)


@pytest.mark.asyncio
async def test_site_energy_refresh_skips_hems_when_site_known_unsupported(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client._hems_site_supported = False  # noqa: SLF001
    coord.client.lifetime_energy = AsyncMock(
        return_value={
            "production": [1000],
            "evse": [],
            "heatpump": [],
            "water_heater": [],
            "start_date": "2024-01-01",
            "interval_minutes": 60,
        }
    )
    coord.client.hems_consumption_lifetime = AsyncMock(
        return_value={
            "evse": [125],
            "heatpump": [250],
            "water_heater": [375],
        }
    )

    await coord.energy._async_refresh_site_energy()  # noqa: SLF001

    coord.client.hems_consumption_lifetime.assert_not_awaited()
    assert coord.energy._hems_lifetime_supported is False  # noqa: SLF001
    assert "evse_charging" not in coord.energy.site_energy
    assert "heat_pump" not in coord.energy.site_energy
    assert "water_heater" not in coord.energy.site_energy


@pytest.mark.asyncio
async def test_site_energy_refresh_merges_channels_with_only_invalid_primary_values(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.lifetime_energy = AsyncMock(
        return_value={
            "production": [1000],
            "evse": [None, "bad"],
            "heatpump": [None],
            "water_heater": ["bad"],
            "start_date": "2024-01-01",
            "interval_minutes": 60,
        }
    )
    coord.client.hems_consumption_lifetime = AsyncMock(
        return_value={
            "evse": [100],
            "heatpump": [200],
            "water_heater": [300],
            "interval_minutes": 60,
        }
    )

    await coord.energy._async_refresh_site_energy()  # noqa: SLF001

    assert coord.client.hems_consumption_lifetime.await_count == 1
    assert coord.energy.site_energy["evse_charging"].value_kwh == pytest.approx(0.1)
    assert coord.energy.site_energy["heat_pump"].value_kwh == pytest.approx(0.2)
    assert coord.energy.site_energy["water_heater"].value_kwh == pytest.approx(0.3)


@pytest.mark.asyncio
async def test_site_energy_refresh_skips_hems_when_channels_present(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.lifetime_energy = AsyncMock(
        return_value={
            "production": [1000],
            "evse": [100],
            "heatpump": [200],
            "water_heater": [300],
            "start_date": "2024-01-01",
            "interval_minutes": 60,
        }
    )
    coord.client.hems_consumption_lifetime = AsyncMock(
        return_value={
            "evse": [100],
            "heatpump": [200],
            "water_heater": [300],
        }
    )

    await coord.energy._async_refresh_site_energy()  # noqa: SLF001

    coord.client.hems_consumption_lifetime.assert_not_awaited()
    assert coord.energy.site_energy["evse_charging"].value_kwh == pytest.approx(0.1)
    assert coord.energy.site_energy["heat_pump"].value_kwh == pytest.approx(0.2)
    assert coord.energy.site_energy["water_heater"].value_kwh == pytest.approx(0.3)


@pytest.mark.asyncio
async def test_site_energy_refresh_uses_hems_when_device_channels_only_zero(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.lifetime_energy = AsyncMock(
        return_value={
            "production": [1000],
            "evse": [0],
            "heatpump": [0],
            "water_heater": [0],
            "start_date": "2024-01-01",
            "interval_minutes": 60,
        }
    )
    coord.client.hems_consumption_lifetime = AsyncMock(
        return_value={
            "evse": [100],
            "heatpump": [200],
            "water_heater": [300],
            "interval_minutes": 60,
        }
    )

    await coord.energy._async_refresh_site_energy()  # noqa: SLF001

    assert coord.client.hems_consumption_lifetime.await_count == 1
    assert coord.energy.site_energy["evse_charging"].value_kwh == pytest.approx(0.1)
    assert coord.energy.site_energy["heat_pump"].value_kwh == pytest.approx(0.2)
    assert coord.energy.site_energy["water_heater"].value_kwh == pytest.approx(0.3)


@pytest.mark.asyncio
async def test_site_energy_refresh_uses_zero_from_hems_when_primary_channel_missing(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.lifetime_energy = AsyncMock(
        return_value={
            "production": [1000],
            "evse": [],
            "heatpump": [],
            "water_heater": [],
            "start_date": "2024-01-01",
            "interval_minutes": 60,
        }
    )
    coord.client.hems_consumption_lifetime = AsyncMock(
        return_value={
            "evse": [0],
            "heatpump": [0],
            "water_heater": [0],
            "interval_minutes": 60,
        }
    )

    await coord.energy._async_refresh_site_energy()  # noqa: SLF001

    assert coord.client.hems_consumption_lifetime.await_count == 1
    assert coord.energy.site_energy["evse_charging"].value_kwh == pytest.approx(0.0)
    assert coord.energy.site_energy["heat_pump"].value_kwh == pytest.approx(0.0)
    assert coord.energy.site_energy["water_heater"].value_kwh == pytest.approx(0.0)
    assert coord.energy.site_energy_meta["bucket_lengths"]["water_heater"] == 1


@pytest.mark.asyncio
async def test_site_energy_refresh_hems_failure_does_not_break_primary(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.lifetime_energy = AsyncMock(
        return_value={
            "production": [1000],
            "evse": [200],
            "heatpump": [],
            "water_heater": [],
            "start_date": "2024-01-01",
            "interval_minutes": 60,
        }
    )
    coord.client.hems_consumption_lifetime = AsyncMock(
        side_effect=RuntimeError("hems unavailable")
    )

    await coord.energy._async_refresh_site_energy()  # noqa: SLF001

    assert coord.client.hems_consumption_lifetime.await_count == 1
    assert coord.energy.site_energy["evse_charging"].value_kwh == pytest.approx(0.2)
    assert "heat_pump" not in coord.energy.site_energy
    assert "water_heater" not in coord.energy.site_energy


@pytest.mark.asyncio
async def test_site_energy_refresh_hems_unavailable_is_cached_as_unsupported(
    monkeypatch, coordinator_factory
) -> None:
    coord = coordinator_factory()
    monotonic_value = 10_000.0

    monkeypatch.setattr(
        "custom_components.enphase_ev.energy.time.monotonic",
        lambda: monotonic_value,
    )
    coord.client.lifetime_energy = AsyncMock(
        return_value={
            "production": [1000],
            "evse": [],
            "heatpump": [],
            "water_heater": [],
            "interval_minutes": 60,
        }
    )
    coord.client.hems_consumption_lifetime = AsyncMock(return_value=None)

    await coord.energy._async_refresh_site_energy(force=True)  # noqa: SLF001
    assert coord.client.hems_consumption_lifetime.await_count == 1
    assert coord.energy._hems_lifetime_supported is False  # noqa: SLF001

    monotonic_value += 60.0
    await coord.energy._async_refresh_site_energy(force=True)  # noqa: SLF001
    assert coord.client.hems_consumption_lifetime.await_count == 1

    monotonic_value += 24 * 60 * 60
    await coord.energy._async_refresh_site_energy(force=True)  # noqa: SLF001
    assert coord.client.hems_consumption_lifetime.await_count == 1


@pytest.mark.asyncio
async def test_site_energy_refresh_hems_failure_uses_backoff(
    monkeypatch, coordinator_factory
) -> None:
    from custom_components.enphase_ev import energy as energy_mod

    coord = coordinator_factory()
    monotonic_value = 20_000.0

    monkeypatch.setattr(
        "custom_components.enphase_ev.energy.time.monotonic",
        lambda: monotonic_value,
    )
    coord.client.lifetime_energy = AsyncMock(
        return_value={
            "production": [1000],
            "evse": [],
            "heatpump": [],
            "water_heater": [],
            "interval_minutes": 60,
        }
    )
    coord.client.hems_consumption_lifetime = AsyncMock(
        side_effect=RuntimeError("hems unavailable")
    )

    await coord.energy._async_refresh_site_energy(force=True)  # noqa: SLF001
    assert coord.client.hems_consumption_lifetime.await_count == 1
    assert coord.energy._hems_lifetime_supported is None  # noqa: SLF001

    monotonic_value += 60.0
    await coord.energy._async_refresh_site_energy(force=True)  # noqa: SLF001
    assert coord.client.hems_consumption_lifetime.await_count == 1

    monotonic_value += energy_mod.HEMS_LIFETIME_FAILURE_BACKOFF_S + 1.0
    await coord.energy._async_refresh_site_energy(force=True)  # noqa: SLF001
    assert coord.client.hems_consumption_lifetime.await_count == 2
