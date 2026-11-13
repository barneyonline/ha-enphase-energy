"""Cloud evidence and lifecycle contracts for charger-to-gateway diagnostics."""

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from freezegun import freeze_time
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.enphase_ev.binary_sensor_gateway import (
    GatewayConnectedBinarySensor,
)
from custom_components.enphase_ev.evse_gateway_connectivity import gateway_connectivity
from custom_components.enphase_ev.summary import SummaryStore
from .random_ids import RANDOM_SERIAL


@pytest.fixture
def freezer():
    with freeze_time(NOW, real_asyncio=True) as clock:
        yield clock


NOW = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)


def payload(**overrides):
    return {
        "serialNumber": RANDOM_SERIAL,
        "lastReportedAt": NOW.isoformat() + "[UTC]",
        "isLocallyConnected": True,
        "gatewayConnectivityDetails": [
            {
                "gwConnStatus": 0,
                "lastConnTime": int((NOW - timedelta(days=2)).timestamp() * 1000),
                "serialNumber": "PRIVATE",
            }
        ],
        **overrides,
    }


@pytest.mark.parametrize(
    ("local", "details", "expected"),
    [
        (True, [{"gwConnStatus": 0}], True),
        (False, [{"gwConnStatus": 1}], False),
        (False, [{"gwConnStatus": 0}], None),
        (True, [{"gwConnStatus": 1}], None),
        (True, [{"gwConnStatus": 0}, {"gwConnStatus": 1}], None),
        (True, [{"gwConnStatus": 0}, {"gwConnStatus": "0"}], True),
        (None, [{"gwConnStatus": 0}], True),
        (None, [{"gwConnStatus": 99}], None),
        ("TRUE", None, True),
        ("0", None, False),
        (1, None, True),
        (0, None, False),
        (2, None, None),
        (float("nan"), None, None),
        ("garbage", None, None),
        (True, [], None),
        (True, [None], None),
        (True, [{}], None),
        (True, [{"gwConnStatus": True}], None),
        (True, [{"gwConnStatus": -1}], None),
        (True, [{"gwConnStatus": "٠"}], None),
        (True, [{"gwConnStatus": 0.0}], None),
        (True, "malformed", None),
    ],
)
def test_state_requires_consistent_evidence(local, details, expected):
    snapshot = gateway_connectivity(
        payload(isLocallyConnected=local, gatewayConnectivityDetails=details), NOW
    )
    assert snapshot.connected is expected


@pytest.mark.parametrize(
    "received",
    [None, "bad", NOW.replace(tzinfo=None)],
)
def test_invalid_receipt_timestamp_is_optional_metadata(received):
    snapshot = gateway_connectivity(payload(), received)
    assert snapshot.received_at is None
    assert snapshot.connected is True


@pytest.mark.parametrize(
    "reported",
    [
        None,
        123,
        "bad",
        "2026-11-11T12:00:00",
    ],
)
def test_invalid_charger_report_timestamp_is_optional_metadata(reported):
    snapshot = gateway_connectivity(payload(lastReportedAt=reported), NOW)
    assert snapshot.reported_at is None
    assert snapshot.connected is True


@pytest.mark.parametrize(
    "stamp",
    [
        None,
        -1,
        True,
        "bad",
        10**100,
        0,
        int((NOW + timedelta(days=1)).timestamp() * 1000),
    ],
)
def test_invalid_connection_event_is_optional_metadata(stamp):
    snapshot = gateway_connectivity(
        payload(
            gatewayConnectivityDetails=[{"gwConnStatus": 0, "lastConnTime": stamp}]
        ),
        NOW,
    )
    assert snapshot.last_connection is None
    assert snapshot.connected is True


def test_multiple_gateway_counts_and_latest_connection():
    old = NOW - timedelta(days=2)
    snapshot = gateway_connectivity(
        payload(
            gatewayConnectivityDetails=[
                {"gwConnStatus": 0, "lastConnTime": int(old.timestamp() * 1000)},
                {
                    "gwConnStatus": 0,
                    "lastConnTime": str(
                        int((old + timedelta(hours=1)).timestamp() * 1000)
                    ),
                },
            ]
        ),
        NOW,
    )
    assert snapshot.gateway_count == snapshot.connected_count == 2
    assert snapshot.last_connection == old + timedelta(hours=1)


async def test_cache_has_per_charger_evidence_failure_and_recovery(monkeypatch):
    monkeypatch.setattr(dt_util, "utcnow", lambda: NOW)
    client = SimpleNamespace(summary_v2=AsyncMock(return_value=[payload()]))
    store = SummaryStore(lambda: client)
    assert store.gateway_connectivity(RANDOM_SERIAL) is None
    await store.async_fetch(force=True)
    assert store.gateway_connectivity(RANDOM_SERIAL).connected is True
    assert store.gateway_connectivity("other") is None
    # Cached reads keep the original receipt time and make no network calls.
    await store.async_fetch()
    assert client.summary_v2.await_count == 1
    assert store.gateway_connectivity(RANDOM_SERIAL).received_at == NOW
    client.summary_v2.side_effect = RuntimeError("offline")
    await store.async_fetch(force=True)
    assert store.gateway_connectivity(RANDOM_SERIAL) is None
    store._state["using_stale"] = True
    store._state["available"] = True
    assert store.gateway_connectivity(RANDOM_SERIAL) is None
    store.invalidate()
    store._state["backoff_until"] = None
    client.summary_v2.side_effect = None
    client.summary_v2.return_value = [
        payload(isLocallyConnected=False, gatewayConnectivityDetails=None)
    ]
    await store.async_fetch(force=True)
    assert store.gateway_connectivity(RANDOM_SERIAL).connected is False
    client.summary_v2.return_value = [payload(), payload()]
    await store.async_fetch(force=True)
    assert store.gateway_connectivity(RANDOM_SERIAL) is None
    client.summary_v2.return_value = [None, {}]
    await store.async_fetch(force=True)
    assert store.gateway_connectivity(RANDOM_SERIAL) is None


async def test_entity_keeps_reported_state_until_summary_fails(
    hass, coordinator_factory, freezer, config_entry
):
    freezer.move_to(NOW)
    coord = coordinator_factory(data={RANDOM_SERIAL: {"connected": True}})
    coord.update_interval = None
    coord.summary = SummaryStore(lambda: None)
    coord.summary._cache = (0, [payload()], 600)
    coord.summary._state["last_success_utc"] = NOW
    sensor = GatewayConnectedBinarySensor(coord, RANDOM_SERIAL)
    assert sensor.available
    assert sensor.is_on is True
    assert sensor.unique_id.endswith("_gateway_connected")
    assert sensor.extra_state_attributes == {
        "gateway_connection_count": 1,
        "gateway_connected_count": 1,
        "gateway_last_connection_at": NOW - timedelta(days=2),
        "telemetry_received_at": NOW,
        "charger_last_reported_at": NOW,
    }
    assert "PRIVATE" not in str(sensor.extra_state_attributes)
    component = EntityComponent(logging.getLogger(__name__), "binary_sensor", hass)
    component._platforms["binary_sensor"].config_entry = config_entry
    await component.async_add_entities([sensor])
    assert hass.states.get(sensor.entity_id).state == "on"
    freezer.move_to(NOW + timedelta(hours=1))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert hass.states.get(sensor.entity_id).state == "on"
    assert sensor.extra_state_attributes["charger_last_reported_at"] == NOW
    # A failed summary read makes its cached flags unavailable.
    coord.summary._state["using_stale"] = True
    coord.async_set_updated_data(coord.data)
    await hass.async_block_till_done()
    assert hass.states.get(sensor.entity_id).state == "unknown"
    assert sensor.extra_state_attributes == {}
    coord.summary._state["using_stale"] = False
    coord.summary._state["last_success_utc"] = dt_util.utcnow()
    coord.summary._cache = (
        0,
        [
            payload(
                lastReportedAt=None,
                isLocallyConnected=False,
                gatewayConnectivityDetails=None,
            )
        ],
        600,
    )
    coord.async_set_updated_data(coord.data)
    await hass.async_block_till_done()
    assert hass.states.get(sensor.entity_id).state == "off"
    assert sensor.extra_state_attributes["charger_last_reported_at"] is None
    coord.last_update_success = False
    assert sensor.is_on is None
    await component.async_remove_entity(sensor.entity_id)


def test_gateway_publication_tracks_receipt_and_evidence():
    store = SummaryStore(lambda: None)
    row = payload()
    store._cache = (0, [row], 600)
    store._state["last_success_utc"] = NOW - timedelta(seconds=1)
    initial = store.gateway_publication_state([RANDOM_SERIAL, "absent"])
    assert initial == store.gateway_publication_state(["absent", RANDOM_SERIAL])
    assert "PRIVATE" not in str(initial)
    store._state["failures"] = 3
    store._state["last_error"] = "different error"
    store._cache = (1, [row], 5)
    assert store.gateway_publication_state([RANDOM_SERIAL, "absent"]) == initial
    store._state["last_success_utc"] = NOW
    received = store.gateway_publication_state([RANDOM_SERIAL, "absent"])
    assert received != initial

    store._cache = (1, [row, row], 5)
    assert store.gateway_publication_state([RANDOM_SERIAL, "absent"]) != received
    store._cache = (1, [row], 5)
    assert store.gateway_publication_state([RANDOM_SERIAL, "absent"]) == received

    store._cache = (1, [payload(lastReportedAt=None)], 5)
    assert store.gateway_publication_state([RANDOM_SERIAL, "absent"]) != received
