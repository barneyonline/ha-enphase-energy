"""Regression coverage for charger current readback precedence (issue #959)."""

from unittest.mock import AsyncMock, MagicMock, call

import pytest

from custom_components.enphase_ev.const import DEFAULT_CHARGE_LEVEL_SETTING
from custom_components.enphase_ev.number import DefaultChargeLevelNumber
from custom_components.enphase_ev.sensor import EnphaseChargingLevelSensor
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL


@pytest.mark.asyncio
@pytest.mark.parametrize("decimal_strings", [False, True])
@pytest.mark.parametrize("summary_default", ["disabled", "16", None])
@pytest.mark.parametrize("initial_default", [6, None])
async def test_default_current_survives_write_readback_and_summary(
    coordinator_factory, summary_default, initial_default, decimal_strings
):
    """Summary labels and older defaults cannot overwrite config readback."""
    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._has_successful_refresh = True  # noqa: SLF001
    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": RANDOM_SERIAL,
                    "connectors": [{}],
                    "charging": True,
                    "pluggedIn": True,
                    "chargingLevel": 16,
                }
            ]
        }
    )
    coord.client.charger_auth_settings = AsyncMock(return_value=[])
    coord.client.green_charging_settings = AsyncMock(return_value=[])
    coord.summary.prepare_refresh = MagicMock(return_value=False)
    summary = [
        {
            "serialNumber": RANDOM_SERIAL,
            "chargeLevelDetails": {
                "min": "6",
                "max": "32",
                "defaultChargeLevel": summary_default,
            },
        }
    ]
    coord.summary.async_fetch = AsyncMock(return_value=summary)
    coord.energy._async_refresh_site_energy = AsyncMock()  # noqa: SLF001
    coord.client.charger_config = AsyncMock(
        return_value=[{"key": DEFAULT_CHARGE_LEVEL_SETTING, "value": initial_default}]
    )
    await coord.async_refresh()
    number = DefaultChargeLevelNumber(coord, RANDOM_SERIAL)
    sensor = EnphaseChargingLevelSensor(coord, RANDOM_SERIAL)
    for entity in (number, sensor):
        entity.async_write_ha_state = MagicMock()
        coord.async_add_listener(entity._handle_coordinator_update)  # noqa: SLF001
    assert number.native_value == initial_default
    # Session setpoint and persistent default are distinct cloud settings.
    assert sensor.native_value == 16

    coord.client.set_default_charge_level = AsyncMock(return_value={})
    coord.async_request_refresh = coord.async_refresh
    for amps in (12, 10, 9, 8):
        coord.client.charger_config.return_value = [
            {
                "key": DEFAULT_CHARGE_LEVEL_SETTING,
                "value": f"{amps}.0" if decimal_strings else str(amps),
            }
        ]
        coord.client.status.return_value["evChargerData"][0]["chargingLevel"] = amps
        await number.async_set_native_value(amps)
        assert number.available
        assert number.native_value == amps
        assert sensor.native_value == amps
        assert coord.control_updates.status(RANDOM_SERIAL) == "idle"
        coord.apply_warmup_evse_summary(summary, coord.data)
        assert number.native_value == amps

    assert coord.client.set_default_charge_level.await_args_list == [
        call(RANDOM_SERIAL, amps) for amps in (12, 10, 9, 8)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("readback", [None, "", "invalid"])
async def test_pending_default_current_keeps_last_numeric_readback(
    coordinator_factory, readback
):
    """An empty read while applying a write cannot erase the confirmed current."""
    coord = coordinator_factory(
        serials=[RANDOM_SERIAL],
        data={
            RANDOM_SERIAL: {
                "default_charge_level_supported": True,
                "default_charge_level": 6,
            }
        },
    )
    coord.client.set_default_charge_level = AsyncMock(return_value={})
    coord.async_request_refresh = AsyncMock()
    number = DefaultChargeLevelNumber(coord, RANDOM_SERIAL)
    number.async_write_ha_state = MagicMock()
    coord.async_add_listener(number._handle_coordinator_update)  # noqa: SLF001
    await number.async_set_native_value(12)
    coord.client.charger_config = AsyncMock(
        return_value=[{"key": DEFAULT_CHARGE_LEVEL_SETTING, "value": readback}]
    )
    result = await coord.evse_runtime.async_get_charger_config(
        RANDOM_SERIAL, keys=[DEFAULT_CHARGE_LEVEL_SETTING]
    )
    coord.data[RANDOM_SERIAL]["default_charge_level"] = result[
        DEFAULT_CHARGE_LEVEL_SETTING
    ]
    assert number.native_value == 6
    assert coord.control_updates.status(RANDOM_SERIAL) == "pending"
    coord.client.charger_config.return_value = [
        {"key": DEFAULT_CHARGE_LEVEL_SETTING, "value": "12"}
    ]
    result = await coord.evse_runtime.async_get_charger_config(
        RANDOM_SERIAL, keys=[DEFAULT_CHARGE_LEVEL_SETTING]
    )
    coord.data[RANDOM_SERIAL]["default_charge_level"] = result[
        DEFAULT_CHARGE_LEVEL_SETTING
    ]
    assert number.native_value == 12
    assert coord.control_updates.status(RANDOM_SERIAL) == "idle"


@pytest.mark.parametrize(
    ("summary_default", "previous", "expected"),
    [("20", None, 20), (None, 18, 18), ("disabled", None, None)],
)
def test_warmup_summary_default_fallback(
    coordinator_factory, summary_default, previous, expected
):
    """Summary and previous data can still fill a missing config field."""
    coord = coordinator_factory(
        serials=[RANDOM_SERIAL],
        data={RANDOM_SERIAL: {"default_charge_level": previous}},
    )
    target = {RANDOM_SERIAL: {}}
    coord.apply_warmup_evse_summary(
        [
            {
                "serialNumber": RANDOM_SERIAL,
                "chargeLevelDetails": {"defaultChargeLevel": summary_default},
            }
        ],
        target,
    )
    assert target[RANDOM_SERIAL].get("default_charge_level") == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failed", "cancelled"])
async def test_unset_readback_after_default_current_command_ends(
    coordinator_factory, outcome
):
    """Ended commands cannot hide a later authoritative unset configuration."""
    coord = coordinator_factory(
        serials=[RANDOM_SERIAL],
        data={
            RANDOM_SERIAL: {
                "default_charge_level_supported": True,
                "default_charge_level": 6,
            }
        },
    )
    number = DefaultChargeLevelNumber(coord, RANDOM_SERIAL)
    number.async_write_ha_state = MagicMock()
    coord.async_add_listener(number._handle_coordinator_update)  # noqa: SLF001
    coord.async_request_refresh = AsyncMock()
    coord.client.set_default_charge_level = AsyncMock(return_value={})
    if outcome == "failed":
        coord.client.set_default_charge_level.side_effect = RuntimeError("write failed")
        with pytest.raises(RuntimeError, match="write failed"):
            await number.async_set_native_value(12)
    else:
        await number.async_set_native_value(12)
        coord.control_updates.cancel_group("default_charge_level", RANDOM_SERIAL)
    assert number.native_value == 6
    coord.client.charger_config = AsyncMock(
        return_value=[{"key": DEFAULT_CHARGE_LEVEL_SETTING, "value": None}]
    )
    result = await coord.evse_runtime.async_get_charger_config(
        RANDOM_SERIAL, keys=[DEFAULT_CHARGE_LEVEL_SETTING]
    )
    coord.data[RANDOM_SERIAL]["default_charge_level"] = result[
        DEFAULT_CHARGE_LEVEL_SETTING
    ]
    assert number.native_value is None
    update = coord.control_updates.updates[("default_charge_level", RANDOM_SERIAL)]
    assert update.status == outcome


@pytest.mark.parametrize("previous", [6, None])
@pytest.mark.parametrize("config_supported", [False, None])
def test_warmup_refreshes_existing_summary_default(
    coordinator_factory, previous, config_supported
):
    """Fresh summary data can replace a fallback without config readback."""
    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    target = {
        RANDOM_SERIAL: {
            "default_charge_level": previous,
            "default_charge_level_supported": config_supported,
        }
    }
    coord.apply_warmup_evse_summary(
        [
            {
                "serialNumber": RANDOM_SERIAL,
                "chargeLevelDetails": {"defaultChargeLevel": "12"},
            }
        ],
        target,
    )
    assert target[RANDOM_SERIAL]["default_charge_level"] == 12
