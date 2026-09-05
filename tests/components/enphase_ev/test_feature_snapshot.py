"""Ensure unchanged chargers cannot hide changes in other device families."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.enphase_ev.energy import SiteEnergyFlow
from custom_components.enphase_ev.tariff import (
    TariffBillingSnapshot,
    TariffRateSnapshot,
)
from custom_components.enphase_ev.system_events import StandingAlarm
from custom_components.enphase_ev.feature_snapshot import (
    _matches_frozen,
    capture_feature_snapshot,
)
from custom_components.enphase_ev.state_models import (
    BatteryState,
    HeatpumpState,
    InventoryState,
)


def test_family_snapshots_detach_nested_mutations_and_reuse_unchanged_content():
    battery, heatpump, inventory = BatteryState(), HeatpumpState(), InventoryState()
    inventory._inverter_data = {"INV": {"values": [1, {"power": 2}]}}
    battery._battery_schedules_payload = {"cfg": {"details": [{"limit": 50}]}}
    first = capture_feature_snapshot(battery, heatpump, inventory)
    second = capture_feature_snapshot(battery, heatpump, inventory, first)
    assert second == first
    assert second.battery is first.battery
    assert second.heatpump is first.heatpump
    assert second.inventory is first.inventory
    inventory._inverter_data["INV"]["values"][1]["power"] = 3
    battery._battery_schedules_payload["cfg"]["details"][0]["limit"] = 60
    changed = capture_feature_snapshot(battery, heatpump, inventory, second)
    assert changed != first
    assert first.inventory["_inverter_data"]["INV"]["values"][1]["power"] == 2
    assert (
        first.battery["_battery_schedules_payload"]["cfg"]["details"][0]["limit"] == 50
    )
    assert changed.heatpump is first.heatpump
    with pytest.raises(TypeError):
        changed.inventory["_inverter_data"]["INV"]["values"][1]["power"] = 4


def test_acquisition_clocks_and_raw_status_do_not_invalidate_publication():
    battery, heatpump, inventory = BatteryState(), HeatpumpState(), InventoryState()
    first = capture_feature_snapshot(battery, heatpump, inventory)
    battery._battery_status_cache_until = 99
    battery._battery_settings_last_write_mono = 12
    heatpump._heatpump_power_last_success_utc = datetime.now(timezone.utc)
    heatpump._heatpump_power_sample_history = [{"power": 12}]
    inventory._status_payload_cache = {"fetched_at_utc": "new"}
    inventory._inverter_parameter_success_mono = {"INV": {"power": 10}}
    assert capture_feature_snapshot(battery, heatpump, inventory, first) == first
    heatpump._heatpump_power_sample_utc = datetime.now(timezone.utc)
    assert capture_feature_snapshot(battery, heatpump, inventory, first) != first


@pytest.mark.parametrize("family", ["battery", "heatpump", "inventory", "schedule"])
async def test_manager_only_changes_notify_coordinator_listeners(
    coordinator_factory, family
):
    coord = coordinator_factory(serials=["EVSE"])
    coord.async_set_updated_data({"EVSE": {"charging": False}})
    coord._async_update_data_impl = AsyncMock(
        return_value={"EVSE": {"charging": False}}
    )
    published = []
    remove = coord.async_add_listener(
        lambda: published.append(coord.integration_snapshot)
    )
    try:
        first = coord.integration_snapshot
        if family == "battery":
            coord.battery_state._battery_aggregate_charge_pct = 70
        elif family == "heatpump":
            coord.heatpump_state._heatpump_power_w = 200
        elif family == "inventory":
            coord.inventory_state._inverter_data["INV"] = {"lifetime": 100}
        else:
            coord.battery_state._battery_schedules_payload = {"cfg": {"details": []}}
        await coord.async_refresh()
        assert len(published) == 1
        assert published[0] != first
        await coord.async_refresh()
        assert len(published) == 1
    finally:
        remove()


@pytest.mark.parametrize(
    ("value", "frozen", "matches"),
    [
        ({"a": 1}, None, False),
        ({"a": 1}, {"b": 1}, False),
        ([1], None, False),
        ([1], (), False),
        ([1], (2,), False),
        ((1,), (1,), True),
        ({1}, frozenset({1}), True),
    ],
)
def test_comparison_preserves_container_content(value, frozen, matches):
    assert _matches_frozen(value, frozen) is matches


def test_site_snapshots_detach_mutable_dataclasses_and_nested_tariff_seasons():
    battery, heatpump, inventory = BatteryState(), HeatpumpState(), InventoryState()
    flow = SiteEnergyFlow(1.0, 1, ["production"], "2026-01-01", None, False)
    tariff = TariffRateSnapshot(
        "flat", "flat", None, None, "AUD", None, ({"rate": 0.2},)
    )
    first = capture_feature_snapshot(
        battery,
        heatpump,
        inventory,
        site_energy={"production": flow},
        tariff={"purchase": tariff},
    )
    with patch(
        "custom_components.enphase_ev.feature_snapshot._publication_value",
        side_effect=AssertionError("unchanged family must not be copied"),
    ):
        second = capture_feature_snapshot(
            battery,
            heatpump,
            inventory,
            first,
            site_energy={"production": flow},
            tariff={"purchase": tariff},
        )
    assert second.site_energy is first.site_energy
    assert second.tariff is first.tariff
    flow.value_kwh = 2.0
    flow.fields_used.append("solar")
    tariff.seasons[0]["rate"] = 0.3
    changed = capture_feature_snapshot(
        battery,
        heatpump,
        inventory,
        second,
        site_energy={"production": flow},
        tariff={"purchase": tariff},
    )
    assert changed != first
    assert first.site_energy["production"]["value_kwh"] == 1.0
    assert first.site_energy["production"]["fields_used"] == ("production",)
    assert first.tariff["purchase"]["seasons"][0]["rate"] == 0.2


@pytest.mark.parametrize("family", ["site_energy", "tariff", "events", "grid_profile"])
async def test_site_manager_changes_notify_with_unchanged_chargers(
    coordinator_factory, family
):
    coord = coordinator_factory(serials=["EVSE"])
    coord.async_set_updated_data({"EVSE": {"charging": False}})
    coord._async_update_data_impl = AsyncMock(
        return_value={"EVSE": {"charging": False}}
    )
    published = []
    remove = coord.async_add_listener(
        lambda: published.append(coord.integration_snapshot)
    )
    try:
        first = coord.integration_snapshot
        if family == "site_energy":
            coord.energy.site_energy["production"] = SiteEnergyFlow(
                1.0, 1, ["production"], "2026-01-01", None, False
            )
        elif family == "tariff":
            coord.tariff_billing = TariffBillingSnapshot(
                "2026-01-01", "MONTH", 1, "monthly"
            )
        elif family == "events":
            coord.system_events_runtime._standing_alarms = (
                StandingAlarm("alarm", "critical", "gateway", "2026-01-01"),
            )
        else:
            coord.grid_profile_runtime.set_search_query("new profile")
        await coord.async_refresh()
        assert len(published) == 1
        assert published[0] != first
        await coord.async_refresh()
        assert len(published) == 1
    finally:
        remove()
