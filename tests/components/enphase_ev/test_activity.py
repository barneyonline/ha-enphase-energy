"""Descriptive Activity entries retain states and avoid polling noise."""

from datetime import UTC, datetime
import json
from pathlib import Path
from string import Formatter
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from homeassistant.const import EVENT_LOGBOOK_ENTRY, EVENT_STATE_CHANGED
from homeassistant.core import Event, State
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enphase_ev.activity import (
    ACTIVITY_LABELS,
    ActivityPublisher,
    ActivitySnapshot,
    MAX_MESSAGE_LENGTH,
    _freeze,
)
from custom_components.enphase_ev.const import DOMAIN

SITE = "1234567"
SERIAL = "EV1234567890"


@pytest.fixture
def activity(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": SITE})
    entry.add_to_hass(hass)
    coord = SimpleNamespace(
        site_id=SITE,
        serials={SERIAL},
        last_success_utc=datetime(2026, 9, 30, 5, 12, tzinfo=UTC),
        last_failure_status=None,
        diagnostics=SimpleNamespace(endpoint_family_health_diagnostics=lambda: {}),
        async_add_listener=Mock(return_value=Mock()),
    )
    events = []
    unsub = hass.bus.async_listen(
        EVENT_LOGBOOK_ENTRY, lambda event: events.append(event.data)
    )
    publishers = []

    def make(
        key,
        value="online",
        attrs=None,
        *,
        domain="sensor",
        unique=None,
        owner=None,
        platform=DOMAIN,
    ):
        record = er.async_get(hass).async_get_or_create(
            domain,
            platform,
            unique_id=unique or f"{DOMAIN}_site_{SITE}_{key}",
            config_entry=(owner or entry),
            suggested_object_id=key,
            original_name=key,
        )
        hass.states.async_set(record.entity_id, value, attrs or {})
        return record

    def start(runtime_coord=None):
        publisher = ActivityPublisher(hass, entry, runtime_coord or coord)
        publishers.append(publisher)
        return publisher

    yield SimpleNamespace(
        entry=entry, coord=coord, events=events, make=make, start=start
    )
    for publisher in publishers:
        publisher.stop()
    unsub()


@pytest.mark.parametrize(
    ("key", "initial", "updated", "attrs", "changes", "expected"),
    [
        (
            "charger",
            "Ready",
            "Faulted",
            {"charger_problem": False},
            {
                "charger_problem": True,
                "suspended_by_evse": True,
                "offline_since": "2026-09-30T05:12:00Z",
            },
            "Reported fault: Yes",
        ),
        (
            "gateway_connectivity_status",
            "online",
            "offline",
            {"connected_devices": 1},
            {
                "connected_devices": 0,
                "disconnected_devices": 1,
                "unknown_connection_devices": 0,
                "connection_method": "Wi-Fi",
                "latest_reported_utc": "2026-09-30T05:12:00Z",
            },
            "Disconnected gateways: 1",
        ),
        (
            "microinverter_connectivity_status",
            "online",
            "not_reporting",
            {"reporting_inverters": 12},
            {
                "reporting_inverters": 10,
                "not_reporting_inverters": 2,
                "unknown_inverters": 0,
                "power_telemetry_status": "rate_limited",
                "power_telemetry_next_retry": "2026-09-30T05:20:00Z",
            },
            "Non-reporting inverters: 2",
        ),
        (
            "heat_pump_status",
            "normal",
            "normal",
            {"sg_ready_mode_raw": "MODE_2"},
            {
                "sg_ready_mode_raw": "MODE_3",
                "sg_ready_contact_state": "closed",
                "vpp_sgready_mode_override": False,
            },
            "SG Ready mode: MODE_3",
        ),
        (
            "active_system_events",
            "off",
            "on",
            {"active_count": 0},
            {
                "active_count": 1,
                "active_events": [
                    {
                        "type": "Fault",
                        "device_type": "Gateway",
                        "state": "active",
                        "severity": "error",
                        "description": "Check connection",
                        "event_date": "2026-09-30T05:12:00Z",
                    }
                ],
            },
            "Description: Check connection",
        ),
        (
            "battery_overall_status",
            "normal",
            "error",
            {"per_battery_status": {"private-storage": "normal"}},
            {
                "per_battery_status": {"private-storage": "error"},
                "per_battery_status_text": {"private-storage": "Needs service"},
                "worst_storage_key": "private-storage",
            },
            "Battery 1: Error; Needs service",
        ),
        (
            "service_status",
            "ok",
            "degraded",
            {},
            {
                "degraded_services": ["battery_status"],
                "degraded_endpoint_families": ["battery_status"],
                "endpoint_failure_details": {
                    "battery_status": {
                        "reason": "Temporarily unavailable",
                        "retry_utc": "2026-09-30T05:20:00Z",
                    }
                },
            },
            "Reason: Temporarily unavailable",
        ),
    ],
)
async def test_entity_change_and_recovery(
    activity, hass, key, initial, updated, attrs, changes, expected
):
    domain = "binary_sensor" if key == "active_system_events" else "sensor"
    unique = f"{DOMAIN}_{SERIAL}_status" if key == "charger" else None
    record = activity.make(key, initial, attrs, domain=domain, unique=unique)
    await hass.async_block_till_done()
    activity.start()
    assert not activity.events
    hass.states.async_set(record.entity_id, updated, changes)
    await hass.async_block_till_done()
    assert len(activity.events) == 1
    assert expected in activity.events[0]["message"]
    assert activity.events[0]["entity_id"] == record.entity_id
    assert activity.events[0]["domain"] == DOMAIN
    assert hass.states.get(record.entity_id).state == updated
    # Ordinary poll timestamps are context, never an event signature.
    hass.states.async_set(
        record.entity_id,
        updated,
        {
            **changes,
            "last_success_utc": "2026-09-30T05:15:00Z",
            "offline_since": "2026-09-30T05:13:00Z",
            "latest_reported_utc": "2026-09-30T05:15:00Z",
        },
    )
    await hass.async_block_till_done()
    assert len(activity.events) == 1
    hass.states.async_set(record.entity_id, "unavailable")
    await hass.async_block_till_done()
    assert len(activity.events) == 1
    hass.states.async_set(record.entity_id, initial, attrs)
    await hass.async_block_till_done()
    assert len(activity.events) == 2


async def test_same_state_material_detail_change(activity, hass):
    record = activity.make(
        "gateway_connectivity_status",
        "degraded",
        {"connected_devices": 1, "disconnected_devices": 2},
    )
    await hass.async_block_till_done()
    activity.start()
    hass.states.async_set(
        record.entity_id,
        "degraded",
        {"connected_devices": 2, "disconnected_devices": 1},
    )
    await hass.async_block_till_done()
    assert len(activity.events) == 1
    assert "Connected gateways: 2" in activity.events[0]["message"]


async def test_services_reasons_codes_reordering_and_retries(activity, hass):
    initial = {
        "degraded_services": ["a", "b"],
        "degraded_endpoint_families": ["b"],
        "endpoint_failure_details": {
            "a": {"reason": "Timeout", "retry_utc": "bad"},
            "b": "invalid",
        },
    }
    record = activity.make("service_status", "degraded", initial)
    await hass.async_block_till_done()
    activity.start()
    health = {"a": {"last_status": 503}}
    activity.coord.diagnostics.endpoint_family_health_diagnostics = lambda: health
    hass.states.async_set(
        record.entity_id,
        "degraded",
        {**initial, "degraded_services": ["b", "a"], "consecutive_failures": 2},
    )
    await hass.async_block_till_done()
    assert "HTTP status: 503" in activity.events[-1]["message"]
    hass.states.async_set(
        record.entity_id,
        "degraded",
        {
            **initial,
            "endpoint_failure_details": {
                "a": {"reason": "Timeout", "retry_utc": "2026-09-30T05:20:00Z"},
                "b": "invalid",
            },
        },
    )
    await hass.async_block_till_done()
    assert len(activity.events) == 1
    hass.states.async_set(
        record.entity_id,
        "degraded",
        {**initial, "endpoint_failure_details": {"a": {"reason": "Denied"}}},
    )
    await hass.async_block_till_done()
    assert len(activity.events) == 2
    # Malformed lists/details do not crash, and a missing reason is not invented.
    hass.states.async_set(
        record.entity_id,
        "degraded",
        {
            "degraded_services": None,
            "degraded_endpoint_families": {},
            "endpoint_failure_details": [],
        },
    )
    await hass.async_block_till_done()
    assert len(activity.events) == 3


async def test_cloud_coalescing_recovery_and_reason_change(activity, hass):
    reach = activity.make("cloud_reachable", "on", domain="binary_sensor")
    error = activity.make("last_error_code", "none")
    await hass.async_block_till_done()
    activity.start()
    activity.coord.last_failure_status = 503
    hass.states.async_set(reach.entity_id, "off")
    hass.states.async_set(error.entity_id, "service_unavailable")
    await hass.async_block_till_done()
    assert len(activity.events) == 1
    assert activity.events[-1]["entity_id"] == reach.entity_id
    assert "HTTP status: 503" in activity.events[-1]["message"]
    activity.coord.last_failure_status = 429
    hass.states.async_set(error.entity_id, "rate_limited")
    await hass.async_block_till_done()
    assert len(activity.events) == 2
    assert activity.events[-1]["entity_id"] == error.entity_id
    activity.coord.last_success_utc = datetime(2026, 9, 30, 5, 20, tzinfo=UTC)
    hass.states.async_set(error.entity_id, "none")
    hass.states.async_set(reach.entity_id, "on")
    await hass.async_block_till_done()
    assert len(activity.events) == 3
    assert "Cloud reachable: Yes" in activity.events[-1]["message"]
    # Repeated healthy updates should not log even as success advances.
    hass.states.async_set(reach.entity_id, "on", {"last_success_utc": "new"})
    await hass.async_block_till_done()
    assert len(activity.events) == 3


async def test_unavailable_cloud_counterpart_keeps_valid_observations(activity, hass):
    reach = activity.make("cloud_reachable", "off", domain="binary_sensor")
    error = activity.make("last_error_code", "service_unavailable")
    activity.coord.last_failure_status = 503
    await hass.async_block_till_done()
    publisher = activity.start()
    original = publisher.baselines["cloud"].signature
    for record in (reach, error):
        original_state = hass.states.get(record.entity_id).state
        hass.states.async_set(record.entity_id, "unavailable")
        await hass.async_block_till_done()
        assert not activity.events
        assert publisher.baselines["cloud"].signature == original
        if record == error:
            hass.states.async_set(reach.entity_id, "off", {"poll_time": "new"})
            await hass.async_block_till_done()
            assert not activity.events
        hass.states.async_set(record.entity_id, original_state)
        await hass.async_block_till_done()
        assert not activity.events
    hass.states.async_set(reach.entity_id, "unknown")
    hass.states.async_set(error.entity_id, "unknown")
    await hass.async_block_till_done()
    assert not activity.events
    assert publisher.baselines["cloud"].signature == original
    hass.states.async_set(error.entity_id, "rate_limited")
    activity.coord.last_failure_status = 429
    await hass.async_block_till_done()
    assert len(activity.events) == 1
    assert activity.events[-1]["entity_id"] == error.entity_id
    assert "Cloud reachable: No" in activity.events[-1]["message"]


@pytest.mark.parametrize(
    "key,domain", [("last_error_code", "sensor"), ("cloud_reachable", "binary_sensor")]
)
async def test_cloud_single_enabled_entity(activity, hass, key, domain):
    record = activity.make(key, "none" if domain == "sensor" else "on", domain=domain)
    await hass.async_block_till_done()
    activity.start()
    activity.coord.last_failure_status = 503
    hass.states.async_set(
        record.entity_id, "service_unavailable" if domain == "sensor" else "off"
    )
    await hass.async_block_till_done()
    assert len(activity.events) == 1
    assert activity.events[-1]["entity_id"] == record.entity_id


async def test_cloud_unknown_first_observation_and_removal(activity, hass):
    activity.coord.last_success_utc = None
    record = activity.make("cloud_reachable", "off", domain="binary_sensor")
    error = activity.make("last_error_code", "unknown")
    await hass.async_block_till_done()
    publisher = activity.start()
    assert not publisher.baselines
    publisher._flush_cloud()
    hass.states.async_set(error.entity_id, "unavailable")
    await hass.async_block_till_done()
    assert not activity.events
    activity.coord.last_success_utc = datetime(2026, 9, 30, tzinfo=UTC)
    hass.states.async_set(record.entity_id, "on")
    await hass.async_block_till_done()
    assert not activity.events
    hass.states.async_remove(record.entity_id)
    await hass.async_block_till_done()
    publisher._flush_cloud()
    assert not activity.events
    assert publisher._cloud_snapshot(None) is None


async def test_cloud_stop_cancels_pending_callback(activity, hass):
    record = activity.make("cloud_reachable", "on", domain="binary_sensor")
    await hass.async_block_till_done()
    publisher = activity.start()
    publisher._state_changed(
        Event(
            EVENT_STATE_CHANGED,
            {
                "entity_id": record.entity_id,
                "new_state": State(record.entity_id, "off"),
            },
        )
    )
    assert publisher._cloud_handle is not None
    publisher.stop()
    assert publisher._cloud_handle is None
    publisher._state_changed(
        Event(EVENT_STATE_CHANGED, {"entity_id": record.entity_id, "new_state": None})
    )
    await hass.async_block_till_done()
    assert not activity.events


async def test_lifecycle_scope_rename_enable_and_delayed_discovery(activity, hass):
    other = MockConfigEntry(domain=DOMAIN, data={"site_id": "other"})
    other.add_to_hass(hass)
    foreign = activity.make("gateway_connectivity_status", owner=other)
    irrelevant = activity.make("last_reported")
    non_enphase = activity.make("gateway_connectivity_status", platform="other")
    unknown = activity.make("export_limit", "unknown")
    publisher = activity.start()
    hass.states.async_set(foreign.entity_id, "offline")
    hass.states.async_set(irrelevant.entity_id, "new timestamp")
    hass.states.async_set(non_enphase.entity_id, "offline")
    hass.states.async_set("sensor.unregistered", "error")
    hass.states.async_set(unknown.entity_id, "limited", {"confirmed_watts": 2000})
    await hass.async_block_till_done()
    assert not activity.events
    registry = er.async_get(hass)
    renamed = registry.async_update_entity(
        unknown.entity_id, new_entity_id="sensor.my_limit"
    )
    await hass.async_block_till_done()
    hass.states.async_set(
        renamed.entity_id,
        "zero_export",
        {"confirmed_watts": 0, "friendly_name": "My limit"},
    )
    await hass.async_block_till_done()
    assert activity.events[-1]["entity_id"] == "sensor.my_limit"
    assert activity.events[-1]["name"] == "My limit"
    registry.async_update_entity(
        renamed.entity_id, disabled_by=er.RegistryEntryDisabler.USER
    )
    await hass.async_block_till_done()
    hass.states.async_set(renamed.entity_id, "limited", {"confirmed_watts": 3000})
    await hass.async_block_till_done()
    assert len(activity.events) == 1
    registry.async_update_entity(renamed.entity_id, disabled_by=None)
    hass.states.async_set(renamed.entity_id, "limited", {"confirmed_watts": 4000})
    await hass.async_block_till_done()
    assert len(activity.events) == 1
    hass.states.async_remove(renamed.entity_id)
    await hass.async_block_till_done()
    registry.async_remove(renamed.entity_id)
    await hass.async_block_till_done()
    publisher.stop()
    replacement = activity.start()
    assert not replacement.baselines
    late = activity.make("heat_pump_status", "normal")
    await hass.async_block_till_done()
    assert len(activity.events) == 1
    hass.states.async_set(late.entity_id, "warning")
    await hass.async_block_till_done()
    assert len(activity.events) == 2


@pytest.mark.parametrize("result", ["confirmed", "unconfirmed", "rejected"])
@pytest.mark.parametrize("target", [0, None, 3000])
async def test_export_request_context_and_outcome(activity, hass, result, target):
    record = activity.make(
        "export_limit", "limited", {"confirmed_watts": 5000, "request_status": "idle"}
    )
    await hass.async_block_till_done()
    activity.start()
    pending = {
        "confirmed_watts": 5000,
        "pending": True,
        "request_status": "pending",
        "requested_action": "disable" if target is None else "set",
        "requested_watts": target,
        "pending_requested_at": "2026-09-30T05:12:00Z",
    }
    hass.states.async_set(record.entity_id, "pending", pending)
    await hass.async_block_till_done()
    assert len(activity.events) == 1
    outcome = {
        "confirmed_watts": target if result == "confirmed" else 5000,
        "pending": result == "unconfirmed",
        "request_status": result,
        "requested_watts": target if result == "unconfirmed" else None,
        "requested_action": (
            pending["requested_action"] if result == "unconfirmed" else None
        ),
    }
    hass.states.async_set(
        record.entity_id,
        (
            "unconfirmed"
            if result == "unconfirmed"
            else "disabled" if target is None and result == "confirmed" else "limited"
        ),
        outcome,
    )
    await hass.async_block_till_done()
    assert len(activity.events) == 2
    expected = "Unknown" if target is None else str(target)
    assert f"Requested limit (W): {expected}" in activity.events[-1]["message"]
    if target is None:
        assert "Requested action: disable" in activity.events[-1]["message"]
    hass.states.async_set(
        record.entity_id,
        "pending",
        {**pending, "pending_requested_at": "2026-09-30T05:30:00Z"},
    )
    await hass.async_block_till_done()
    assert len(activity.events) == 3


async def test_battery_names_order_and_redaction(activity, hass):
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=activity.entry.entry_id,
        identifiers={(DOMAIN, "private-b")},
        name="Garage battery",
    )
    dr.async_get(hass).async_update_device(device.id, name_by_user="My battery")
    initial = {"per_battery_status": {"private-a": "normal", "private-b": "normal"}}
    record = activity.make("battery_overall_status", "normal", initial)
    await hass.async_block_till_done()
    activity.start()
    attrs = {
        "per_battery_status": {"private-b": "normal", "private-a": "error"},
        "per_battery_status_text": {
            "private-a": f"Fault at 192.168.1.1 token=secret {SERIAL} https://example.com/private?key=secret"
        },
        "worst_storage_key": "private-a",
    }
    hass.states.async_set(record.entity_id, "error", attrs)
    await hass.async_block_till_done()
    message = activity.events[-1]["message"]
    assert "My battery" in message
    assert "Worst battery status: Battery 1" in message
    for private in (
        "private-a",
        "private-b",
        "192.168.1.1",
        "secret",
        SERIAL,
        "https://",
    ):
        assert private not in message
    hass.states.async_set(
        record.entity_id,
        "error",
        {**attrs, "battery_order": ["private-b", "private-a"]},
    )
    await hass.async_block_till_done()
    assert len(activity.events) == 1


async def test_item_limit_full_events_and_truncation(activity, hass):
    runtime = SimpleNamespace(activity_events=())
    activity.coord.system_events_runtime = runtime
    record = activity.make(
        "active_system_events", "off", {"active_count": 0}, domain="binary_sensor"
    )
    await hass.async_block_till_done()
    activity.start()
    runtime.activity_events = tuple(
        {
            "fingerprint": f"private-{i}",
            "type": "Fault",
            "device_type": "Gateway",
            "severity": "error",
            "description": f"Fault {i}",
            "event_date": "2026/09/30 05:12:00 +0000 (UTC)",
        }
        for i in range(22)
    )
    hass.states.async_set(record.entity_id, "on", {"active_count": 22})
    await hass.async_block_till_done()
    assert "More items: 17" in activity.events[-1]["message"]
    assert "private-" not in activity.events[-1]["message"]
    assert "First reported:" in activity.events[-1]["message"]
    # A change beyond the 20 entity-attribute rows is still an event.
    runtime.activity_events = (
        *runtime.activity_events[:-1],
        {**runtime.activity_events[-1], "description": "Updated fault"},
    )
    hass.states.async_set(
        record.entity_id, "on", {"active_count": 22, "last_success_utc": "new"}
    )
    await hass.async_block_till_done()
    assert len(activity.events) == 2
    # Replacing a like-for-like alarm is meaningful even with the same count/text.
    runtime.activity_events = (
        *runtime.activity_events[:-1],
        {**runtime.activity_events[-1], "fingerprint": "replacement-private"},
    )
    hass.states.async_set(
        record.entity_id, "on", {"active_count": 22, "last_success_utc": "later"}
    )
    await hass.async_block_till_done()
    assert len(activity.events) == 3
    assert "replacement-private" not in activity.events[-1]["message"]
    publisher = activity.start()
    publisher._publish(
        record.id,
        hass.states.get(record.entity_id),
        ActivitySnapshot(("different",), (("description", "x" * 2000),)),
    )
    await hass.async_block_till_done()
    assert len(activity.events[-1]["message"]) == MAX_MESSAGE_LENGTH
    assert activity.events[-1]["message"].endswith("…")


async def test_malformed_snapshots_and_localized_fields(activity, hass):
    record = activity.make(
        "active_system_events", "on", {"active_events": "bad"}, domain="binary_sensor"
    )
    battery = activity.make(
        "battery_overall_status",
        "normal",
        {"per_battery_status": [], "per_battery_status_text": []},
    )
    await hass.async_block_till_done()
    publisher = activity.start()
    hass.states.async_set(
        record.entity_id,
        "on",
        {"active_events": [None, {"type": "Fault"}], "active_count": 2},
    )
    hass.states.async_set(
        battery.entity_id,
        "error",
        {"per_battery_status": [], "per_battery_status_text": []},
    )
    await hass.async_block_till_done()
    assert len(activity.events) == 2
    await hass.config.async_set_time_zone("Australia/Melbourne")
    assert publisher._value("2026-09-30T05:12:00Z") == "2026-09-30 15:12:00 AEST"
    with patch(
        "custom_components.enphase_ev.activity._shared_label", return_value="Unbekannt"
    ):
        assert publisher._value(None) == "Unbekannt"
    export = activity.make("export_limit", "limited")
    charger = activity.make("charger", "Ready", unique=f"{DOMAIN}_{SERIAL}_status")
    with patch(
        "custom_components.enphase_ev.activity._entity_translation_value",
        return_value="Bestätigt",
    ):
        assert (
            publisher._field_value(export, "request_status", "confirmed") == "Bestätigt"
        )
        assert (
            publisher._field_value(charger, "suspended_by_evse", "true") == "Bestätigt"
        )
        assert (
            dict(
                publisher._snapshot(
                    "export", State(export.entity_id, "limited"), None
                ).fields
            )["status"]
            == "Bestätigt"
        )
    assert publisher._field_value(None, "requested_watts", 0) == "0"
    assert publisher._safe({"token": "secret"}) == ""
    assert _freeze(object()) is None
    assert _freeze({"a": [2, 1]}) == (("a", (1, 2)),)


def test_all_activity_labels_localized_and_placeholder_parity():
    root = Path(__file__).resolve().parents[3] / "custom_components" / DOMAIN
    english = json.loads((root / "strings.json").read_text())["entity"]
    for locale in (root / "translations").glob("*.json"):
        data = json.loads(locale.read_text())["entity"]
        labels = data["sensor"]["shared_labels"]["state"]
        for key, fallback in ACTIVITY_LABELS.items():
            value = labels[f"activity_{key}"]
            assert value.strip()

            def fields(template):
                return {
                    field for _, field, _, _ in Formatter().parse(template) if field
                }

            assert fields(value) == fields(fallback)
        if not locale.stem.startswith("en"):
            assert labels["activity_more"] != ACTIVITY_LABELS["more"]
            assert (
                data["binary_sensor"]["active_system_events"]["name"] != "System Events"
            )
        else:
            assert (
                data["binary_sensor"]["active_system_events"]["name"] == "System Events"
            )
    assert english["binary_sensor"]["active_system_events"]["name"] == "System Events"


async def test_battery_name_sources_and_unrelated_registry_updates(activity, hass):
    record = activity.make(
        "battery_private_status",
        "normal",
        unique=f"{DOMAIN}_site_{SITE}_battery_private_status",
    )
    er.async_get(hass).async_update_entity(record.entity_id, name="Storage room")
    arbitrary = activity.make("unrelated", unique="foreign")
    reach = activity.make("cloud_reachable", "on", domain="binary_sensor")
    await hass.async_block_till_done()
    publisher = activity.start()
    assert publisher._kind(arbitrary) is None
    assert publisher._battery_name("private") == "Storage room"
    activity.coord.battery_storage = lambda identity: {"name": "Roof battery"}
    assert publisher._battery_name("other") == "Roof battery"
    activity.coord.battery_storage = lambda identity: {
        "name": identity,
        "serial_number": identity,
    }
    assert publisher._battery_name("third") == "Battery 3"
    before = publisher.baselines["cloud"]
    er.async_get(hass).async_remove(arbitrary.entity_id)
    await hass.async_block_till_done()
    assert publisher.baselines["cloud"] == before
    er.async_get(hass).async_update_entity(
        reach.entity_id, disabled_by=er.RegistryEntryDisabler.USER
    )
    await hass.async_block_till_done()
    assert "cloud" not in publisher.baselines


async def test_cloud_reachability_uses_failure_evidence_when_error_disabled(
    activity, hass
):
    record = activity.make("cloud_reachable", "on", domain="binary_sensor")
    await hass.async_block_till_done()
    activity.start()
    activity.coord.last_failure_utc = datetime(2026, 9, 30, 5, 15, tzinfo=UTC)
    activity.coord.last_failure_status = 503
    activity.coord.last_failure_source = "http"
    hass.states.async_set(record.entity_id, "off")
    await hass.async_block_till_done()
    assert "HTTP status: 503" in activity.events[-1]["message"]
    assert "Reason: service_unavailable" in activity.events[-1]["message"]


async def test_real_coordinator_service_health(activity, hass, coordinator_factory):
    coord = coordinator_factory(serials=[])
    publisher = activity.start()
    publisher.coord = coord
    record = activity.make(
        "service_status", "degraded", {"degraded_services": ["battery_status"]}
    )
    assert (
        publisher._snapshot("services", hass.states.get(record.entity_id), None)
        is not None
    )


async def test_actual_translation_cache(activity, hass):
    from custom_components.enphase_ev.labels import async_prime_label_translations

    hass.config.language = "de"
    await async_prime_label_translations(hass)
    record = activity.make("export_limit", "limited", {"request_status": "idle"})
    await hass.async_block_till_done()
    activity.start()
    hass.states.async_set(
        record.entity_id,
        "pending",
        {
            "request_status": "pending",
            "requested_action": "set",
            "requested_watts": 0,
            "pending": True,
        },
    )
    await hass.async_block_till_done()
    message = activity.events[-1]["message"]
    assert "Angeforderter Grenzwert (W): 0" in message
    assert "Request status" not in message
    assert "Anfragestatus:" in message


async def test_failed_setup_cleanup_stops_activity_publisher(activity, hass):
    from unittest.mock import AsyncMock
    from custom_components.enphase_ev import _async_cleanup_failed_runtime
    from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData

    record = activity.make("cloud_reachable", "on", domain="binary_sensor")
    await hass.async_block_till_done()
    publisher = activity.start()
    activity.coord.async_close = AsyncMock()
    await _async_cleanup_failed_runtime(
        activity.entry,
        EnphaseRuntimeData(coordinator=activity.coord, activity_publisher=publisher),
    )
    hass.states.async_set(record.entity_id, "off")
    await hass.async_block_till_done()
    assert not activity.events
    assert publisher._stopped


def test_scalar_activity_values_and_standing_alarm_label(activity):
    publisher = activity.start()
    assert publisher._value("Standing Alarm") == "Standing alarm"
    assert publisher._value(1234567) == "1234567"


async def test_cloud_delayed_discovery_and_enabled_counterpart_baselines(
    activity, hass
):
    reach = activity.make("cloud_reachable", "on", domain="binary_sensor")
    await hass.async_block_till_done()
    publisher = activity.start()
    error = activity.make("last_error_code", "unknown")
    await hass.async_block_till_done()
    hass.states.async_set(error.entity_id, "none")
    await hass.async_block_till_done()
    assert not activity.events
    hass.states.async_set(reach.entity_id, "off")
    hass.states.async_set(error.entity_id, "service_unavailable")
    await hass.async_block_till_done()
    assert len(activity.events) == 1
    registry = er.async_get(hass)
    registry.async_update_entity(
        reach.entity_id, disabled_by=er.RegistryEntryDisabler.USER
    )
    await hass.async_block_till_done()
    assert "cloud" in publisher.baselines
    activity.coord.last_failure_status = 429
    hass.states.async_set(error.entity_id, "rate_limited")
    await hass.async_block_till_done()
    assert len(activity.events) == 2
    registry.async_update_entity(reach.entity_id, disabled_by=None)
    hass.states.async_set(reach.entity_id, "on")
    await hass.async_block_till_done()
    assert len(activity.events) == 2
    hass.states.async_remove(reach.entity_id)
    await hass.async_block_till_done()
    assert "cloud" in publisher.baselines
    hass.states.async_set(error.entity_id, "none")
    await hass.async_block_till_done()
    assert len(activity.events) == 3
    assert activity.events[-1]["entity_id"] == error.entity_id


async def test_export_unchanged_initial_readback_is_quiet(activity, hass):
    record = activity.make(
        "export_limit",
        "limited",
        {
            "confirmed_watts": 3000,
            "request_status": "idle",
            "pending": False,
            "requested_watts": None,
            "requested_action": None,
        },
    )
    await hass.async_block_till_done()
    publisher = activity.start()
    initial = publisher.baselines[record.id].signature
    hass.states.async_set(
        record.entity_id,
        "limited",
        {
            **hass.states.get(record.entity_id).attributes,
            "last_successful_readback": "2026-09-30T05:30:00Z",
        },
    )
    await hass.async_block_till_done()
    assert publisher.baselines[record.id].signature == initial
    assert not activity.events


async def test_cloud_category_changes_without_enabled_state_changes(activity, hass):
    reach = activity.make("cloud_reachable", "off", domain="binary_sensor")
    error = activity.make("last_error_code", "service_unavailable")
    er.async_get(hass).async_update_entity(
        error.entity_id, disabled_by=er.RegistryEntryDisabler.USER
    )
    activity.coord.last_failure_utc = datetime(2026, 9, 30, 5, 15, tzinfo=UTC)
    activity.coord.last_failure_source = "http"
    activity.coord.last_failure_status = 503
    await hass.async_block_till_done()
    publisher = activity.start()
    notify = activity.coord.async_add_listener.call_args.args[0]
    notify()
    await hass.async_block_till_done()
    assert not activity.events
    activity.coord.last_failure_status = 429
    notify()
    await hass.async_block_till_done()
    assert len(activity.events) == 1
    assert activity.events[-1]["entity_id"] == reach.entity_id
    assert publisher.baselines["cloud"].signature[1] == "rate_limited"
    assert "HTTP status: 429" in activity.events[-1]["message"]
    assert hass.states.get(reach.entity_id).state == "off"
    activity.coord.last_failure_utc = datetime(2026, 9, 30, 5, 16, tzinfo=UTC)
    notify()
    await hass.async_block_till_done()
    assert len(activity.events) == 1
    unsubscribe = activity.coord.async_add_listener.return_value
    publisher.stop()
    unsubscribe.assert_called_once_with()
    notify()
    await hass.async_block_till_done()
    assert len(activity.events) == 1


async def test_activity_redacts_quoted_credentials(activity, hass):
    record = activity.make(
        "active_system_events", "off", {"active_count": 0}, domain="binary_sensor"
    )
    await hass.async_block_till_done()
    activity.start()
    hass.states.async_set(
        record.entity_id,
        "on",
        {
            "active_count": 1,
            "active_events": [
                {
                    "description": (
                        'Failure {"token": "private secret", "device_id": "DEVICE-PRIVATE-9999", '
                        '"reason": "denied"}'
                    )
                }
            ],
        },
    )
    await hass.async_block_till_done()
    assert "DEVICE-PRIVATE-9999" not in activity.events[-1]["message"]
    assert "private" not in activity.events[-1]["message"]
    assert "secret" not in activity.events[-1]["message"]
    assert "denied" in activity.events[-1]["message"]


async def test_coordinator_notification_keeps_cloud_discovery_quiet(activity, hass):
    activity.make("cloud_reachable", "on", domain="binary_sensor")
    await hass.async_block_till_done()
    publisher = activity.start()
    activity.make("last_error_code", "service_unavailable")
    activity.coord.async_add_listener.call_args.args[0]()
    await hass.async_block_till_done()
    assert not activity.events
    assert publisher.baselines["cloud"].signature[1] == "service_unavailable"


async def test_actual_coordinator_notification_cloud_fallback(
    activity, hass, coordinator_factory
):
    coord = coordinator_factory(config={"site_id": SITE}, serials=[])
    coord.last_success_utc = datetime(2026, 9, 30, 5, 12, tzinfo=UTC)
    coord.last_failure_utc = datetime(2026, 9, 30, 5, 15, tzinfo=UTC)
    coord.last_failure_source = "http"
    coord.last_failure_status = 503
    reach = activity.make("cloud_reachable", "off", domain="binary_sensor")
    await hass.async_block_till_done()
    publisher = activity.start(coord)
    coord.last_failure_status = 429
    coord.async_update_listeners()
    await hass.async_block_till_done()
    assert len(activity.events) == 1
    assert activity.events[-1]["entity_id"] == reach.entity_id
    publisher.stop()
    coord.last_failure_status = 503
    coord.async_update_listeners()
    await hass.async_block_till_done()
    assert len(activity.events) == 1
