from __future__ import annotations

import pytest

from homeassistant.exceptions import ServiceValidationError

from tests.components.enphase_ev.random_ids import RANDOM_SERIAL, RANDOM_SITE_ID


@pytest.mark.asyncio
async def test_charging_amps_number_reads_and_sets(hass, monkeypatch):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.number import ChargingAmpsNumber

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 30,
    }
    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg)
    sn = RANDOM_SERIAL
    # Populate coordinator data with min/max
    coord.data = {
        sn: {"name": "Garage EV", "charging_level": None, "min_amp": 6, "max_amp": 40}
    }
    coord.last_set_amps = {}

    class StubClient:
        def __init__(self):
            self.calls = []

        async def start_charging(
            self,
            s,
            amps,
            connector_id=1,
            *,
            include_level=None,
            strict_preference=False,
        ):
            self.calls.append((s, amps, connector_id))
            return {"status": "ok"}

    coord.client = StubClient()

    # Avoid debouncer refresh
    async def _noop():
        return None

    coord.async_request_refresh = _noop  # type: ignore

    ent = ChargingAmpsNumber(coord, sn)
    # Unknown -> coordinator falls back to default (clamped to limits)
    assert ent.native_value == 32.0
    assert ent.native_min_value == 6.0
    assert ent.native_max_value == 40.0

    await ent.async_set_native_value(24)
    # Number entity no longer starts charging; only records desired amps
    assert coord.client.calls == []
    assert coord.last_set_amps[sn] == 24


@pytest.mark.asyncio
async def test_charging_switch_turn_on_off(hass, monkeypatch):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.switch import ChargingSwitch

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 30,
    }
    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg)
    sn = RANDOM_SERIAL
    coord.data = {
        sn: {
            "name": "Garage EV",
            "charging": False,
            "min_amp": 6,
            "max_amp": 16,
            "plugged": True,
        }
    }
    coord.last_set_amps = {}

    class StubClient:
        def __init__(self):
            self.start_calls = []
            self.stop_calls = []

        async def start_charging(
            self,
            s,
            amps,
            connector_id=1,
            *,
            include_level=None,
            strict_preference=False,
        ):
            self.start_calls.append((s, amps, connector_id))
            return {"status": "ok"}

        async def stop_charging(self, s):
            self.stop_calls.append(s)
            return {"status": "ok"}

    coord.client = StubClient()

    async def _noop():
        return None

    coord.async_request_refresh = _noop  # type: ignore

    sw = ChargingSwitch(coord, sn)
    assert sw.is_on is False
    assert coord.get_desired_charging(sn) is None

    await sw.async_turn_on()
    assert coord.client.start_calls[-1] == (sn, 16, 1)
    assert coord.last_set_amps[sn] == 16
    assert coord.get_desired_charging(sn) is True

    await sw.async_turn_off()
    assert coord.client.stop_calls[-1] == sn
    assert coord.get_desired_charging(sn) is False


@pytest.mark.asyncio
async def test_charging_switch_requires_plugged(hass, monkeypatch):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.switch import ChargingSwitch

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 30,
    }
    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg)
    sn = RANDOM_SERIAL
    coord.data = {
        sn: {
            "name": "Garage EV",
            "charging": False,
            "min_amp": 6,
            "max_amp": 16,
            "plugged": False,
        }
    }
    coord.last_set_amps = {}

    class StubClient:
        def __init__(self):
            self.start_calls = []

        async def start_charging(
            self,
            s,
            amps,
            connector_id=1,
            *,
            include_level=None,
            strict_preference=False,
        ):
            self.start_calls.append((s, amps, connector_id))
            return {"status": "ok"}

    coord.client = StubClient()

    async def _noop():
        return None

    coord.async_request_refresh = _noop  # type: ignore

    sw = ChargingSwitch(coord, sn)

    with pytest.raises(ServiceValidationError):
        await sw.async_turn_on()
    assert coord.client.start_calls == []
    assert coord.get_desired_charging(sn) is None
