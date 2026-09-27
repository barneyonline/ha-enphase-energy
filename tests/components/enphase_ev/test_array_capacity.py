"""Array capacity metadata, availability, and sensor registration contracts."""

from datetime import timedelta
from unittest.mock import AsyncMock

import asyncio
import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.util import dt as dt_util

from custom_components.enphase_ev.array_capacity import (
    PanelRatingsParser,
    async_refresh_array_capacity,
    capacity_snapshot,
    positive_number,
)
from custom_components.enphase_ev.sensor_array_capacity import (
    EnphaseArrayCapacitySensor,
)


@pytest.fixture
def builder():
    return {
        "arrays": [
            {
                "id": 1,
                "label": "North",
                "modules": [{"serial_num": "A"}, {"serial_num": "B"}],
            },
            {"id": 2, "label": "West", "modules": [{"serial_num": "C"}]},
        ],
        "inventory_details": [
            {"serialNum": sn, "capacity": 349, "count": 1, "type": "IQ7A"}
            for sn in "ABC"
        ],
    }


@pytest.mark.parametrize(
    "value", [None, True, [], {}, "435 W", "bad", "nan", "inf", 0, -1]
)
def test_invalid_rating(value):
    assert positive_number(value) is None


def test_panel_ratings_only_selected_options():
    parser = PanelRatingsParser()
    parser.feed(
        """<input value="secret"><select id="other"><option selected data-stc-rating="999"></select>
    <select id="pv_module_model_1"><option data-stc-rating="425">Wrong</option>
    <option selected="selected" data-stc-rating="435">Panel</option></select>
    <option selected data-stc-rating="999">Outside</option>
    <select id="pv_module_model_2"><option selected>Missing</option></select>
    <select><option selected data-stc-rating="999"></select>"""
    )
    assert parser.ratings == {"1": 435, "2": None}


def test_capacity_totals_and_mixed_panels(builder):
    result = capacity_snapshot(builder, {"1": 435, "2": 425})
    assert result == {
        "array_size": {"value": 1.295, "arrays": {"North": 0.870, "West": 0.425}},
        "inverter_capacity": {
            "value": 1.047,
            "arrays": {"North": 0.698, "West": 0.349},
        },
    }
    builder["arrays"][1]["label"] = "North"
    assert capacity_snapshot(builder, {"1": 435, "2": 425})["array_size"]["arrays"] == {
        "North": 1.295
    }
    result = capacity_snapshot(builder, {"1": 435})
    assert result["array_size"] == {"value": None, "arrays": {"North": None}}


def test_partial_capacity_is_not_total(builder):
    builder["inventory_details"][1]["capacity"] = None
    result = capacity_snapshot(builder, {"1": 435, "2": 435})
    assert result["array_size"]["value"] == 1.305
    assert result["inverter_capacity"] == {
        "value": None,
        "arrays": {"North": None, "West": 0.349},
    }
    builder["inventory_details"] = builder["inventory_details"][:1]
    assert capacity_snapshot(builder, {})["inverter_capacity"]["value"] is None


def test_unmapped_inventory_and_empty_layout(builder):
    builder["inventory_details"].append({"serialNum": "D", "capacity": 349, "count": 1})
    assert (
        capacity_snapshot(builder, {"1": 435, "2": 435})["array_size"]["value"] is None
    )
    assert (
        capacity_snapshot({"arrays": [], "inventory_details": []}, {})["array_size"][
            "value"
        ]
        is None
    )


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"arrays": []},
        {"arrays": [], "inventory_details": [None]},
        {"arrays": [], "inventory_details": [{"serialNum": 2}]},
    ],
)
def test_invalid_builder_payload(payload):
    with pytest.raises(ValueError):
        capacity_snapshot(payload, {})


@pytest.mark.parametrize(
    "change",
    [
        lambda b: b["inventory_details"][0].update(serialNum=""),
        lambda b: b["inventory_details"][0].update(count=2),
        lambda b: b["inventory_details"][0].update(count=True),
        lambda b: b["inventory_details"].append(b["inventory_details"][0]),
        lambda b: b["arrays"].append(None),
        lambda b: b["arrays"][0].update(modules=None),
        lambda b: b["arrays"][0].update(id=None),
        lambda b: b["arrays"][1].update(id=1),
        lambda b: b["arrays"][0].update(label=None),
        lambda b: b["arrays"][0].update(label=" "),
        lambda b: b["arrays"][0]["modules"].append(None),
        lambda b: b["arrays"][0]["modules"].append({}),
        lambda b: b["arrays"][0]["modules"].append({"serial_num": " "}),
        lambda b: b["arrays"][0]["modules"].append({"serial_num": "A"}),
    ],
)
def test_ambiguous_membership(builder, change):
    change(builder)
    with pytest.raises(ValueError):
        capacity_snapshot(builder, {})


@pytest.mark.asyncio
async def test_runtime_refresh_cache_errors_and_disable(
    coordinator_factory, builder, monkeypatch
):
    coord = coordinator_factory()
    coord.include_inverters = True
    state = coord.inventory_state
    client = coord.client
    client.array_builder_inventory = AsyncMock(return_value=builder)
    client.array_panel_ratings = AsyncMock(return_value={"1": 435, "2": 435})
    monkeypatch.setattr(
        "custom_components.enphase_ev.array_capacity.time.monotonic", lambda: 100.0
    )
    await async_refresh_array_capacity(coord.inventory_runtime)
    assert state.array_capacity["array_size"]["value"] == 1.305
    assert state.array_capacity_next_refresh == 21700
    await async_refresh_array_capacity(coord.inventory_runtime)
    client.array_builder_inventory.assert_awaited_once()
    state.array_capacity_next_refresh = 0
    client.array_panel_ratings.side_effect = RuntimeError("optional denied")
    await async_refresh_array_capacity(coord.inventory_runtime)
    assert state.array_capacity["inverter_capacity"]["value"] == 1.047
    assert state.array_capacity["array_size"]["value"] is None
    assert state.array_capacity_next_refresh == 3700
    state.array_capacity_next_refresh = 0
    client.array_builder_inventory.side_effect = RuntimeError("failed")
    await async_refresh_array_capacity(coord.inventory_runtime)
    assert state.array_capacity == {}
    coord.include_inverters = False
    await async_refresh_array_capacity(coord.inventory_runtime)
    assert state.array_capacity_next_refresh == 0
    coord.include_inverters = True
    client.array_builder_inventory = None
    await async_refresh_array_capacity(coord.inventory_runtime)
    client.array_builder_inventory = AsyncMock()
    client.array_panel_ratings = None
    await async_refresh_array_capacity(coord.inventory_runtime)
    client.array_builder_inventory.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_propagates(coordinator_factory):
    coord = coordinator_factory()
    coord.include_inverters = True
    coord.client.array_builder_inventory = AsyncMock(side_effect=asyncio.CancelledError)
    coord.client.array_panel_ratings = AsyncMock()
    with pytest.raises(asyncio.CancelledError):
        await async_refresh_array_capacity(coord.inventory_runtime)


@pytest.mark.asyncio
async def test_api_endpoints(coordinator_factory, builder):
    client = coordinator_factory().client
    client._json = AsyncMock(return_value=builder)
    assert await client.array_builder_inventory() == builder
    args, kwargs = client._json.call_args
    assert args == (
        "GET",
        f"https://enlighten.enphaseenergy.com/service/builder/api/v3/systems/{client._site}/arrays",
    )
    assert kwargs["headers"] == client._layout_headers
    client._json.return_value = []
    assert await client.array_builder_inventory() == {}
    client._text = AsyncMock(
        return_value='<select id="pv_module_model_1"><option selected data-stc-rating="435"></select>'
    )
    assert await client.array_panel_ratings() == {"1": 435}
    assert client._text.call_args.args == (
        "GET",
        f"https://enlighten.enphaseenergy.com/systems/{client._site}/details",
    )


def test_sensor_contract(coordinator_factory, builder):
    coord = coordinator_factory()
    coord.include_inverters = True
    coord._type_device_buckets = {
        "microinverter": {"count": 3, "type_key": "microinverter"}
    }
    coord._selected_type_keys = {"microinverter"}
    coord.last_success_utc = dt_util.utcnow() - timedelta(days=1)
    coord.last_update_success = False
    coord.inventory_state.array_capacity = capacity_snapshot(
        builder, {"1": 435, "2": 435}
    )
    dc = EnphaseArrayCapacitySensor(coord)
    ac = EnphaseArrayCapacitySensor(coord, inverter=True)
    assert dc.native_value == 1.305
    assert ac.native_value == 1.047
    assert dc.native_unit_of_measurement == "kW"
    assert ac.native_unit_of_measurement == "kVA"
    assert dc.device_class == SensorDeviceClass.POWER
    assert ac.device_class == SensorDeviceClass.APPARENT_POWER
    assert dc.state_class is None
    assert dc.available and ac.available
    assert dc.extra_state_attributes == {"arrays": {"North": 0.870, "West": 0.435}}
    attributes = dc.extra_state_attributes
    attributes["arrays"]["West"] = -1
    assert dc.extra_state_attributes["arrays"]["West"] == 0.435
    assert ("enphase_ev", f"type:{coord.site_id}:microinverter") in dc.device_info[
        "identifiers"
    ]
    coord.include_inverters = False
    assert not dc.available
    coord.include_inverters = True
    coord.inventory_state.array_capacity = {}
    assert dc.native_value is None and not dc.available
    assert ac.extra_state_attributes == {"arrays": {}}


@pytest.mark.asyncio
async def test_refresh_flow_and_platform_registration(
    hass, config_entry, coordinator_factory, builder
):
    from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[])
    coord.include_inverters = True
    coord._selected_type_keys = {"microinverter"}
    coord._type_device_buckets = {
        "microinverter": {"count": 3, "type_key": "microinverter"}
    }
    coord.client.array_builder_inventory = AsyncMock(return_value=builder)
    coord.client.array_panel_ratings = AsyncMock(return_value={"1": 435, "2": 435})
    coord.client.inverters_inventory = None
    await coord.inventory_runtime._async_refresh_inverters()
    assert coord.inventory_state.array_capacity["inverter_capacity"]["value"] == 1.047
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    entities = []
    await async_setup_entry(
        hass, config_entry, lambda values, **kwargs: entities.extend(values)
    )
    capacities = [e for e in entities if isinstance(e, EnphaseArrayCapacitySensor)]
    assert len(capacities) == 2
    assert {e.translation_key for e in capacities} == {
        "total_array_size",
        "total_inverter_capacity",
    }


@pytest.mark.asyncio
async def test_request_timeouts_are_isolated(coordinator_factory, builder, monkeypatch):
    coord = coordinator_factory()
    coord.include_inverters = True
    coord.client.array_builder_inventory = AsyncMock(return_value=builder)

    async def stall():
        await asyncio.sleep(0)

    original_timeout = asyncio.timeout
    monkeypatch.setattr(
        "custom_components.enphase_ev.array_capacity.asyncio.timeout",
        lambda _seconds: original_timeout(0),
    )
    coord.client.array_panel_ratings = stall
    await async_refresh_array_capacity(coord.inventory_runtime)
    assert coord.inventory_state.array_capacity["inverter_capacity"]["value"] == 1.047
    assert coord.inventory_state.array_capacity["array_size"]["value"] is None
    coord.inventory_state.array_capacity_next_refresh = 0
    coord.client.array_builder_inventory = stall
    await async_refresh_array_capacity(coord.inventory_runtime)
    assert coord.inventory_state.array_capacity == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["builder", "settings"])
@pytest.mark.parametrize("status", [401, 403])
async def test_permission_gated_discovery_and_recovery(
    hass, config_entry, coordinator_factory, builder, monkeypatch, source, status
):
    import aiohttp
    from types import SimpleNamespace
    from custom_components.enphase_ev.api import Unauthorized
    from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[])
    coord.include_inverters = True
    coord.last_success_utc = dt_util.utcnow()
    coord._selected_type_keys = {"microinverter"}
    coord._type_device_buckets = {
        "microinverter": {"count": 3, "type_key": "microinverter"}
    }
    coord.client.array_builder_inventory = AsyncMock(return_value=builder)
    coord.client.array_panel_ratings = AsyncMock(return_value={"1": 435, "2": 435})
    error = (
        Unauthorized()
        if status == 401
        else aiohttp.ClientResponseError(
            request_info=SimpleNamespace(real_url="https://example.test"),
            history=(),
            status=status,
        )
    )
    denied = (
        coord.client.array_builder_inventory
        if source == "builder"
        else coord.client.array_panel_ratings
    )
    denied.side_effect = error
    monkeypatch.setattr(
        "custom_components.enphase_ev.array_capacity.time.monotonic", lambda: 100.0
    )
    entities = []
    callbacks = []
    coord.async_add_topology_listener = lambda _cb: lambda: None

    def listen(cb, **_kwargs):
        callbacks.append(cb)
        return lambda: None

    coord.async_add_listener = listen
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    await async_setup_entry(
        hass, config_entry, lambda values, **_kwargs: entities.extend(values)
    )
    assert not any(isinstance(e, EnphaseArrayCapacitySensor) for e in entities)
    await async_refresh_array_capacity(coord.inventory_runtime)
    for cb in callbacks:
        cb()
    capacities = [e for e in entities if isinstance(e, EnphaseArrayCapacitySensor)]
    assert {e.translation_key for e in capacities} == (
        set() if source == "builder" else {"total_inverter_capacity"}
    )
    assert coord.inventory_state.array_capacity_next_refresh == 21700
    await async_refresh_array_capacity(coord.inventory_runtime)
    denied.assert_awaited_once()
    if source == "builder":
        coord.client.array_panel_ratings.assert_not_awaited()
    denied.side_effect = None
    coord.inventory_state.array_capacity_next_refresh = 0
    await async_refresh_array_capacity(coord.inventory_runtime)
    for cb in callbacks:
        cb()
        cb()
    capacities = [e for e in entities if isinstance(e, EnphaseArrayCapacitySensor)]
    assert len(capacities) == 2
    assert all(e.available for e in capacities)
    # Permission loss after discovery does not delete registered entities/history.
    denied.side_effect = error
    coord.inventory_state.array_capacity_next_refresh = 0
    await async_refresh_array_capacity(coord.inventory_runtime)
    for cb in callbacks:
        cb()
    assert len([e for e in entities if isinstance(e, EnphaseArrayCapacitySensor)]) == 2
    assert not next(
        e for e in capacities if e.translation_key == "total_array_size"
    ).available
    inverter = next(
        e for e in capacities if e.translation_key == "total_inverter_capacity"
    )
    assert inverter.available is (source == "settings")
