from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.components.enphase_ev.random_ids import RANDOM_SERIAL, RANDOM_SITE_ID


@pytest.mark.asyncio
async def test_power_field_not_populated_from_status(hass, monkeypatch):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
        OPT_FAST_POLL_INTERVAL,
        OPT_NOMINAL_VOLTAGE,
        OPT_SLOW_POLL_INTERVAL,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }
    options = {
        OPT_NOMINAL_VOLTAGE: 240,
        OPT_FAST_POLL_INTERVAL: 5,
        OPT_SLOW_POLL_INTERVAL: 20,
    }

    class DummyEntry:
        def __init__(self, options):
            self.options = options

        def async_on_unload(self, cb):
            return None

    entry = DummyEntry(options)

    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg, config_entry=entry)

    class StubClient:
        def __init__(self, payload):
            self._payload = payload

        async def status(self):
            return self._payload

    payload = {
        "evChargerData": [
            {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "charging": True,
                "pluggedIn": True,
                # No power keys here; coordinator should estimate from chargingLevel
                "chargingLevel": 16,
            }
        ],
        "ts": 1725600423,
    }
    coord.client = StubClient(payload)
    out = await coord._async_update_data()
    sn = RANDOM_SERIAL
    assert "power_w" not in out[sn]


@pytest.mark.asyncio
async def test_missing_report_time_does_not_synthesize_sampled_at_utc(
    hass, monkeypatch
):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    class DummyEntry:
        options = {}

        def async_on_unload(self, cb):
            return None

    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg, config_entry=DummyEntry())

    class StubClient:
        async def site_bootstrap(self):
            return {}

        async def system_dashboard_summary(self, **kwargs):
            return {}

        def __init__(self, payloads):
            self._payloads = list(payloads)

        async def status(self):
            return self._payloads.pop(0)

    coord.client = StubClient(
        [
            {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "charging": True,
                        "pluggedIn": True,
                        "chargingLevel": 16,
                        "lifetime": {"all": 1.0},
                    }
                ]
            },
            {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "charging": True,
                        "pluggedIn": True,
                        "chargingLevel": 16,
                        "lifetime": {"all": 1.5},
                    }
                ]
            },
        ]
    )

    first = await coord._async_update_data()
    second = await coord._async_update_data()

    assert first[RANDOM_SERIAL]["sampled_at_utc"] is None
    assert second[RANDOM_SERIAL]["sampled_at_utc"] is None
    assert first[RANDOM_SERIAL]["derived_last_sample_ts"] is None
    assert second[RANDOM_SERIAL]["derived_last_sample_ts"] is None
    assert first[RANDOM_SERIAL]["derived_power_w"] == 0
    assert second[RANDOM_SERIAL]["derived_power_w"] == 0


@pytest.mark.asyncio
async def test_missing_lifetime_consumption_preserves_previous_evse_baseline(
    hass, monkeypatch
):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    class DummyEntry:
        options = {}

        def async_on_unload(self, cb):
            return None

    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg, config_entry=DummyEntry())

    class StubClient:
        async def site_bootstrap(self):
            return {}

        async def system_dashboard_summary(self, **kwargs):
            return {}

        def __init__(self, payloads):
            self._payloads = list(payloads)

        async def status(self):
            return self._payloads.pop(0)

    coord.client = StubClient(
        [
            {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "charging": True,
                        "pluggedIn": True,
                        "lastReportedAt": "2024-01-01T00:05:00+00:00",
                    }
                ]
            },
            {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "charging": True,
                        "pluggedIn": True,
                        "lastReportedAt": "2024-01-01T00:10:00+00:00",
                    }
                ]
            },
        ]
    )
    coord.summary.prepare_refresh = lambda **kwargs: True
    coord.summary.async_fetch = AsyncMock(
        side_effect=[
            [
                {
                    "serialNumber": RANDOM_SERIAL,
                    "lifeTimeConsumption": 2.0,
                    "lastReportedAt": "2024-01-01T00:05:00+00:00",
                }
            ],
            [
                {
                    "serialNumber": RANDOM_SERIAL,
                    "lastReportedAt": "2024-01-01T00:10:00+00:00",
                }
            ],
        ]
    )

    first = await coord._async_update_data()
    coord.data = first
    second = await coord._async_update_data()

    assert first[RANDOM_SERIAL]["lifetime_kwh"] == pytest.approx(2.0)
    assert second[RANDOM_SERIAL]["lifetime_kwh"] == pytest.approx(2.0)
    assert second[RANDOM_SERIAL]["derived_last_lifetime_kwh"] == pytest.approx(2.0)
    assert second[RANDOM_SERIAL]["derived_last_energy_ts"] == pytest.approx(
        datetime(2024, 1, 1, 0, 5, tzinfo=timezone.utc).timestamp()
    )


@pytest.mark.asyncio
async def test_same_sample_timestamp_still_drops_power_when_charging_stops(
    hass, monkeypatch
):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    class DummyEntry:
        options = {}

        def async_on_unload(self, cb):
            return None

    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg, config_entry=DummyEntry())

    class StubClient:
        async def site_bootstrap(self):
            return {}

        async def system_dashboard_summary(self, **kwargs):
            return {}

        def __init__(self, payloads):
            self._payloads = list(payloads)

        async def status(self):
            return self._payloads.pop(0)

    sample_time = "2024-01-01T00:05:00+00:00"
    coord.client = StubClient(
        [
            {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "charging": True,
                        "pluggedIn": True,
                        "lastReportedAt": sample_time,
                    }
                ]
            },
            {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "charging": False,
                        "pluggedIn": True,
                        "lastReportedAt": sample_time,
                    }
                ]
            },
        ]
    )
    coord.summary.prepare_refresh = lambda **kwargs: True
    coord.summary.async_fetch = AsyncMock(
        return_value=[
            {
                "serialNumber": RANDOM_SERIAL,
                "lifeTimeConsumption": 2.0,
                "lastReportedAt": sample_time,
            }
        ]
    )
    coord.evse_state._evse_power_snapshots[RANDOM_SERIAL] = {  # noqa: SLF001
        "derived_last_sample_ts": sample_time,
        "derived_last_energy_ts": sample_time,
        "derived_last_lifetime_kwh": 2.0,
        "derived_power_w": 7200,
        "derived_power_method": "lifetime_energy_window",
        "derived_power_window_seconds": 300.0,
    }

    await coord._async_update_data()
    result = await coord._async_update_data()

    assert result[RANDOM_SERIAL]["derived_power_w"] == 0
    assert result[RANDOM_SERIAL]["derived_power_method"] == "idle"
    assert result[RANDOM_SERIAL]["derived_power_window_seconds"] is None


@pytest.mark.asyncio
async def test_suspended_ev_status_zeroes_power_and_charging(hass, monkeypatch):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    class DummyEntry:
        options = {}

        def async_on_unload(self, cb):
            return None

    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg, config_entry=DummyEntry())

    class StubClient:
        async def site_bootstrap(self):
            return {}

        async def system_dashboard_summary(self, **kwargs):
            return {}

        def __init__(self, payloads):
            self._payloads = list(payloads)

        async def status(self):
            return self._payloads.pop(0)

    coord.client = StubClient(
        [
            {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "charging": True,
                        "pluggedIn": True,
                        "connectorStatusType": "SUSPENDED_EV",
                        "lastReportedAt": "2024-01-01T00:00:00+00:00",
                    }
                ]
            },
            {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "charging": True,
                        "pluggedIn": True,
                        "connectorStatusType": "SUSPENDED_EV",
                        "lastReportedAt": "2024-01-01T00:05:00+00:00",
                    }
                ]
            },
        ]
    )
    coord.summary.prepare_refresh = lambda **kwargs: True
    coord.summary.async_fetch = AsyncMock(
        side_effect=[
            [
                {
                    "serialNumber": RANDOM_SERIAL,
                    "lifeTimeConsumption": 2.0,
                    "lastReportedAt": "2024-01-01T00:00:00+00:00",
                }
            ],
            [
                {
                    "serialNumber": RANDOM_SERIAL,
                    "lifeTimeConsumption": 2.5,
                    "lastReportedAt": "2024-01-01T00:05:00+00:00",
                }
            ],
        ]
    )

    first = await coord._async_update_data()
    coord.data = first
    second = await coord._async_update_data()

    assert first[RANDOM_SERIAL]["derived_power_method"] == "seeded"
    assert first[RANDOM_SERIAL]["charging"] is True
    assert first[RANDOM_SERIAL]["actual_charging"] is False
    assert second[RANDOM_SERIAL]["derived_power_method"] == "idle"
    assert second[RANDOM_SERIAL]["derived_power_w"] == 0
    assert second[RANDOM_SERIAL]["charging"] is True
    assert second[RANDOM_SERIAL]["actual_charging"] is False


@pytest.mark.asyncio
async def test_power_snapshot_reseeds_when_charging_resumes(hass, monkeypatch):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    class DummyEntry:
        options = {}

        def async_on_unload(self, cb):
            return None

    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg, config_entry=DummyEntry())

    class StubClient:
        async def site_bootstrap(self):
            return {}

        async def system_dashboard_summary(self, **kwargs):
            return {}

        def __init__(self, payloads):
            self._payloads = list(payloads)

        async def status(self):
            return self._payloads.pop(0)

    coord.client = StubClient(
        [
            {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "charging": False,
                        "pluggedIn": True,
                        "connectorStatusType": "AVAILABLE",
                        "lastReportedAt": "2024-01-01T00:00:00+00:00",
                    }
                ]
            },
            {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "charging": True,
                        "pluggedIn": True,
                        "connectorStatusType": "CHARGING",
                        "lastReportedAt": "2024-01-01T00:05:00+00:00",
                    }
                ]
            },
            {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "charging": True,
                        "pluggedIn": True,
                        "connectorStatusType": "CHARGING",
                        "lastReportedAt": "2024-01-01T00:10:00+00:00",
                    }
                ]
            },
        ]
    )
    coord.summary.prepare_refresh = lambda **kwargs: True
    coord.summary.async_fetch = AsyncMock(
        side_effect=[
            [
                {
                    "serialNumber": RANDOM_SERIAL,
                    "lifeTimeConsumption": 2.0,
                    "lastReportedAt": "2024-01-01T00:00:00+00:00",
                }
            ],
            [
                {
                    "serialNumber": RANDOM_SERIAL,
                    "lifeTimeConsumption": 2.0,
                    "lastReportedAt": "2024-01-01T00:05:00+00:00",
                }
            ],
            [
                {
                    "serialNumber": RANDOM_SERIAL,
                    "lifeTimeConsumption": 2.5,
                    "lastReportedAt": "2024-01-01T00:10:00+00:00",
                }
            ],
        ]
    )

    first = await coord._async_update_data()
    coord.data = first
    second = await coord._async_update_data()
    coord.data = second
    third = await coord._async_update_data()

    assert second[RANDOM_SERIAL]["derived_power_method"] == "seeded"
    assert second[RANDOM_SERIAL]["derived_power_w"] == 0
    assert second[RANDOM_SERIAL]["derived_last_energy_ts"] == pytest.approx(
        datetime(2024, 1, 1, 0, 5, 0, tzinfo=timezone.utc).timestamp()
    )
    assert third[RANDOM_SERIAL]["derived_power_method"] == "lifetime_energy_window"
    assert third[RANDOM_SERIAL]["derived_power_w"] == 6000


@pytest.mark.asyncio
async def test_site_only_refresh_accepts_naive_utcnow(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
        CONF_SITE_ONLY,
    )
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = coordinator_factory(
        config={
            CONF_SITE_ID: RANDOM_SITE_ID,
            CONF_SERIALS: [],
            CONF_EAUTH: "EAUTH",
            CONF_COOKIE: "COOKIE",
            CONF_SCAN_INTERVAL: 15,
            CONF_SITE_ONLY: True,
        },
        serials=[],
    )
    coord.energy._async_refresh_site_energy = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    monkeypatch.setattr(
        coord_mod.dt_util,
        "utcnow",
        lambda: datetime(2026, 4, 2, 10, 0, 0),
    )

    assert await coord._async_update_data() == {}


@pytest.mark.asyncio
async def test_evse_snapshot_edge_branches_are_covered(
    coordinator_factory,
    monkeypatch,
) -> None:
    class BadFloat(float):
        def __float__(self):
            raise ValueError("bad float")

    class BadStr:
        def __str__(self):
            raise ValueError("bad str")

    serials = [
        "EVSE_NOSAMPLE",
        "EVSE_RESET",
        "EVSE_IDLE",
        "EVSE_WINDOW",
        "EVSE_THREE",
        "EVSE_TINY",
        "EVSE_SUSPENDED",
        "EVSE_DT",
        "EVSE_AWARE",
        "EVSE_EPOCH",
        "EVSE_BAD",
        "EVSE_NEG",
        "EVSE_BIG",
        "EVSE_BLANK",
        "EVSE_DIGITS",
        "EVSE_INVALID",
        "EVSE_NAIVE",
        "EVSE_LIST",
        "EVSE_PHASECOUNT3",
        "EVSE_PHASECOUNT1",
        "EVSE_CLAMP",
    ]
    coord = coordinator_factory(serials=serials)
    coord._nominal_v = None
    monkeypatch.setattr(
        type(coord),
        "nominal_voltage",
        property(lambda _self: 208),
    )
    coord.summary.prepare_refresh = lambda **kwargs: False
    coord.summary.async_fetch = AsyncMock(
        return_value=[
            {
                "serialNumber": "EVSE_NOSAMPLE",
                "lifeTimeConsumption": 1.0,
            },
            {
                "serialNumber": "EVSE_RESET",
                "lifeTimeConsumption": 4.0,
                "lastReportedAt": "2024-01-01T00:05:00+00:00",
            },
            {
                "serialNumber": "EVSE_IDLE",
                "lifeTimeConsumption": 5.2,
                "lastReportedAt": "2024-01-01T00:05:00+00:00",
            },
            {
                "serialNumber": "EVSE_WINDOW",
                "lifeTimeConsumption": 1.5,
                "lastReportedAt": "2024-01-01T00:05:00+00:00",
            },
            {
                "serialNumber": "EVSE_THREE",
                "lifeTimeConsumption": 2.0,
                "lastReportedAt": "2024-01-01T00:05:00+00:00",
                "phaseMode": "three phase",
                "wiringConfiguration": {BadStr(): "L1", "Neutral": "N"},
                "maxCurrent": 16,
            },
            {
                "serialNumber": "EVSE_TINY",
                "lifeTimeConsumption": 0.5,
                "lastReportedAt": "2024-01-01T00:05:00+00:00",
                "maxCurrent": 0.0001,
            },
            {
                "serialNumber": "EVSE_SUSPENDED",
                "lifeTimeConsumption": 5.0,
                "lastReportedAt": "2024-01-01T00:05:00+00:00",
            },
            {
                "serialNumber": "EVSE_DT",
                "lifeTimeConsumption": 2.0,
            },
            {
                "serialNumber": "EVSE_AWARE",
                "lifeTimeConsumption": 2.0,
            },
            {
                "serialNumber": "EVSE_EPOCH",
                "lifeTimeConsumption": 2.0,
            },
            {
                "serialNumber": "EVSE_BAD",
                "lifeTimeConsumption": 2.0,
                "phaseMode": BadStr(),
            },
            {
                "serialNumber": "EVSE_NEG",
                "lifeTimeConsumption": 2.0,
            },
            {
                "serialNumber": "EVSE_BIG",
                "lifeTimeConsumption": 2.0,
            },
            {
                "serialNumber": "EVSE_BLANK",
                "lifeTimeConsumption": 2.0,
            },
            {
                "serialNumber": "EVSE_DIGITS",
                "lifeTimeConsumption": 2.0,
            },
            {
                "serialNumber": "EVSE_INVALID",
                "lifeTimeConsumption": 2.0,
            },
            {
                "serialNumber": "EVSE_NAIVE",
                "lifeTimeConsumption": 2.0,
            },
            {
                "serialNumber": "EVSE_LIST",
                "lifeTimeConsumption": 2.0,
            },
            {
                "serialNumber": "EVSE_PHASECOUNT3",
                "lifeTimeConsumption": 2.0,
                "phaseCount": 3,
                "maxCurrent": 16,
                "lastReportedAt": "2024-01-01T00:05:00+00:00",
            },
            {
                "serialNumber": "EVSE_PHASECOUNT1",
                "lifeTimeConsumption": 2.0,
                "phaseCount": 1,
                "maxCurrent": 16,
                "lastReportedAt": "2024-01-01T00:05:00+00:00",
            },
            {
                "serialNumber": "EVSE_CLAMP",
                "lifeTimeConsumption": 10.0,
                "maxCurrent": 16,
                "lastReportedAt": "2024-01-01T00:05:00+00:00",
            },
        ]
    )
    coord.client = SimpleNamespace(
        status=AsyncMock(
            return_value={
                "evChargerData": [
                    {
                        "sn": "EVSE_NOSAMPLE",
                        "name": "No Sample",
                        "charging": True,
                        "pluggedIn": True,
                    },
                    {
                        "sn": "EVSE_RESET",
                        "name": "Reset",
                        "charging": True,
                        "pluggedIn": True,
                    },
                    {
                        "sn": "EVSE_IDLE",
                        "name": "Idle",
                        "charging": False,
                        "pluggedIn": True,
                    },
                    {
                        "sn": "EVSE_WINDOW",
                        "name": "Window",
                        "charging": True,
                        "pluggedIn": True,
                    },
                    {
                        "sn": "EVSE_THREE",
                        "name": "Three",
                        "charging": True,
                        "pluggedIn": True,
                    },
                    {
                        "sn": "EVSE_TINY",
                        "name": "Tiny",
                        "charging": True,
                        "pluggedIn": True,
                    },
                    {
                        "sn": "EVSE_SUSPENDED",
                        "name": "Suspended",
                        "charging": True,
                        "pluggedIn": True,
                        "connectorStatusType": "SUSPENDED",
                    },
                    {
                        "sn": "EVSE_DT",
                        "name": "Datetime",
                        "charging": True,
                        "pluggedIn": True,
                        "lastReportedAt": datetime(2024, 1, 1, 0, 5, 0),
                    },
                    {
                        "sn": "EVSE_AWARE",
                        "name": "Aware",
                        "charging": True,
                        "pluggedIn": True,
                        "lastReportedAt": datetime(
                            2024, 1, 1, 0, 5, 0, tzinfo=timezone.utc
                        ),
                    },
                    {
                        "sn": "EVSE_EPOCH",
                        "name": "Epoch",
                        "charging": True,
                        "pluggedIn": True,
                        "lastReportedAt": 1_700_000_000_000,
                    },
                    {
                        "sn": "EVSE_BAD",
                        "name": "Bad Float",
                        "charging": True,
                        "pluggedIn": True,
                        "lastReportedAt": BadFloat(1.0),
                    },
                    {
                        "sn": "EVSE_NEG",
                        "name": "Negative",
                        "charging": True,
                        "pluggedIn": True,
                        "lastReportedAt": -1,
                    },
                    {
                        "sn": "EVSE_BIG",
                        "name": "Huge",
                        "charging": True,
                        "pluggedIn": True,
                        "lastReportedAt": 10**20,
                    },
                    {
                        "sn": "EVSE_BLANK",
                        "name": "Blank",
                        "charging": True,
                        "pluggedIn": True,
                        "lastReportedAt": "   ",
                    },
                    {
                        "sn": "EVSE_DIGITS",
                        "name": "Digits",
                        "charging": True,
                        "pluggedIn": True,
                        "lastReportedAt": "1700000000",
                    },
                    {
                        "sn": "EVSE_INVALID",
                        "name": "Invalid",
                        "charging": True,
                        "pluggedIn": True,
                        "lastReportedAt": "not-a-date",
                    },
                    {
                        "sn": "EVSE_NAIVE",
                        "name": "Naive",
                        "charging": True,
                        "pluggedIn": True,
                        "lastReportedAt": "2024-01-01T00:00:00",
                    },
                    {
                        "sn": "EVSE_LIST",
                        "name": "List",
                        "charging": True,
                        "pluggedIn": True,
                        "lastReportedAt": [1],
                    },
                    {
                        "sn": "EVSE_PHASECOUNT3",
                        "name": "Phase Count 3",
                        "charging": True,
                        "pluggedIn": True,
                    },
                    {
                        "sn": "EVSE_PHASECOUNT1",
                        "name": "Phase Count 1",
                        "charging": True,
                        "pluggedIn": True,
                    },
                    {
                        "sn": "EVSE_CLAMP",
                        "name": "Clamp",
                        "charging": True,
                        "pluggedIn": True,
                    },
                ]
            }
        ),
        battery_site_settings=AsyncMock(return_value={"data": {}}),
    )
    coord.evse_state._evse_power_snapshots.update(  # noqa: SLF001
        {
            "EVSE_RESET": {
                "derived_last_lifetime_kwh": 5.0,
                "derived_last_energy_ts": "2024-01-01T00:00:00+00:00",
                "derived_last_sample_ts": 1_700_000_000_000,
                "derived_last_reset_at": "bad",
            },
            "EVSE_IDLE": {
                "derived_last_lifetime_kwh": 5.0,
                "derived_last_energy_ts": "2024-01-01T00:00:00",
                "derived_last_sample_ts": "2024-01-01T00:00:00",
                "derived_power_method": 123,
                "derived_power_w": "bad",
            },
            "EVSE_WINDOW": {
                "derived_last_lifetime_kwh": 1.0,
                "derived_last_energy_ts": " ",
                "derived_last_sample_ts": "2024-01-01T00:10:00+00:00",
            },
            "EVSE_SUSPENDED": {
                "derived_last_lifetime_kwh": 4.5,
                "derived_last_energy_ts": "2024-01-01T00:00:00+00:00",
                "derived_last_sample_ts": "2024-01-01T00:00:00+00:00",
                "derived_last_reset_at": [],
            },
            "EVSE_CLAMP": {
                "derived_last_lifetime_kwh": 1.0,
                "derived_last_energy_ts": "2024-01-01T00:00:00+00:00",
                "derived_last_sample_ts": "2024-01-01T00:00:00+00:00",
            },
        }
    )

    out = await coord._async_update_data()

    assert out["EVSE_NOSAMPLE"]["derived_last_lifetime_kwh"] == pytest.approx(1.0)
    assert out["EVSE_NOSAMPLE"]["sampled_at_utc"] is None
    assert out["EVSE_RESET"]["derived_power_method"] == "lifetime_reset"
    assert out["EVSE_IDLE"]["derived_power_method"] == "idle"
    assert out["EVSE_WINDOW"]["derived_power_method"] == "lifetime_energy_window"
    assert out["EVSE_WINDOW"]["derived_power_window_seconds"] == pytest.approx(300.0)
    assert out["EVSE_THREE"][
        "derived_power_max_throughput_phase_multiplier"
    ] == pytest.approx(3.0)
    assert out["EVSE_TINY"]["derived_power_max_throughput_source"] == "static_default"
    assert out["EVSE_SUSPENDED"]["derived_power_method"] == "idle"
    assert out["EVSE_DT"]["sampled_at_utc"] == "2024-01-01T00:05:00+00:00"
    assert out["EVSE_AWARE"]["sampled_at_utc"] == "2024-01-01T00:05:00+00:00"
    assert out["EVSE_EPOCH"]["sampled_at_utc"] == "2023-11-14T22:13:20+00:00"
    assert out["EVSE_BAD"]["sampled_at_utc"] is None
    assert out["EVSE_NEG"]["sampled_at_utc"] is None
    assert out["EVSE_BIG"]["sampled_at_utc"] is None
    assert out["EVSE_BLANK"]["sampled_at_utc"] is None
    assert out["EVSE_DIGITS"]["sampled_at_utc"] == "2023-11-14T22:13:20+00:00"
    assert out["EVSE_INVALID"]["sampled_at_utc"] is None
    assert out["EVSE_NAIVE"]["sampled_at_utc"] == "2024-01-01T00:00:00+00:00"
    assert out["EVSE_LIST"]["sampled_at_utc"] is None
    assert (
        out["EVSE_PHASECOUNT3"]["derived_power_max_throughput_topology"]
        == "three_phase"
    )
    assert (
        out["EVSE_PHASECOUNT1"]["derived_power_max_throughput_topology"]
        == "single_phase"
    )
    assert out["EVSE_CLAMP"]["derived_power_w"] == 3328
