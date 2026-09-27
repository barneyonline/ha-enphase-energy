"""Verify reload handoff contains detached state and respects new topology."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from custom_components.enphase_ev.reload_snapshot import ReloadSnapshot
from custom_components.enphase_ev.runtime_data import (
    EnphaseRuntimeData,
    loaded_runtime_data,
)
from homeassistant.config_entries import ConfigEntryState


@pytest.mark.parametrize("members", [[{"serial_number": "active-device"}], []])
def test_reload_preserves_other_device_inventory(coordinator_factory, members):
    """Other device families retain discovery without config serial overrides."""
    source = coordinator_factory()
    type_keys = ["encharge", "envoy", "microinverter", "heatpump", "dry_contact"]
    source.inventory_runtime._set_type_device_buckets(
        {key: {"count": len(members), "devices": members} for key in type_keys},
        type_keys,
    )
    serials = [member["serial_number"] for member in members]
    source._battery_storage_order = serials
    source._battery_storage_data = {
        member["serial_number"]: member for member in members
    }
    source.inventory_runtime._update_shared_state(
        _inverter_order=serials,
        _inverter_data={member["serial_number"]: member for member in members},
    )
    snapshot = ReloadSnapshot.capture(source)
    target = coordinator_factory()
    # Reload constructs a fresh coordinator with no inventory; undo fixture seeds.
    target.inventory_runtime._set_type_device_buckets({}, [], authoritative=False)
    target._devices_inventory_ready = False
    snapshot.apply(target)
    assert target.iter_battery_serials() == serials
    assert target.iter_inverter_serials() == serials
    for key in type_keys:
        bucket = target.inventory_view.type_bucket(key)
        if members:
            assert bucket["devices"] == members
            assert bucket["count"] == len(members)
        else:
            assert bucket is None
    target._selected_type_keys = {"envoy"}
    assert target.inventory_view.iter_type_keys() == ["envoy"]
    assert not target.inventory_view.has_type_for_entities("encharge")


def test_reload_snapshot_detaches_and_restores_only_selected_chargers():
    data = {"a": {"values": [1, 2], "flags": {"ready"}}, "b": {"value": 3}}
    source = SimpleNamespace(
        site_id="site",
        data=data,
        _configured_serials={"a", "b"},
        discovery_snapshot=SimpleNamespace(
            capture=lambda: {"serial_order": ["a", "b"]}
        ),
        last_success_utc=datetime(2026, 9, 5, tzinfo=timezone.utc),
        last_update_success=False,
    )
    snapshot = ReloadSnapshot.capture(source)
    data["a"]["values"].append(3)
    publish = Mock()
    target = SimpleNamespace(
        site_id="site",
        serials={"a"},
        _serial_order=["a"],
        site_only=False,
        config_entry=None,
        discovery_snapshot=SimpleNamespace(apply=Mock()),
        async_set_updated_data=publish,
    )
    snapshot.apply(target)
    publish.assert_called_once_with({"a": {"values": [1, 2], "flags": {"ready"}}})
    assert target.last_success_utc == source.last_success_utc
    assert target.last_update_success is False
    assert target._has_successful_refresh
    with pytest.raises(TypeError):
        snapshot.chargers["new"] = {}
    with pytest.raises(ValueError, match="different site"):
        snapshot.apply(SimpleNamespace(site_id="other"))


@pytest.mark.parametrize("site_only", [True, False])
def test_reload_snapshot_applies_entry_selection_and_empty_inventory(site_only):
    source = SimpleNamespace(
        site_id="site",
        data=None,
        _configured_serials=set(),
        discovery_snapshot=SimpleNamespace(capture=lambda: {"serial_order": []}),
        last_success_utc=None,
        last_update_success=True,
    )
    snapshot = ReloadSnapshot.capture(source)
    target = SimpleNamespace(
        site_id="site",
        serials=set(),
        _serial_order=[],
        site_only=site_only,
        config_entry=SimpleNamespace(data={"site_id": "site"}),
        apply_config_entry_data=Mock(),
        discovery_snapshot=SimpleNamespace(apply=Mock()),
        async_set_updated_data=Mock(),
    )
    snapshot.apply(target)
    target.apply_config_entry_data.assert_called_once_with(target.config_entry.data)
    target.async_set_updated_data.assert_called_once_with({})


@pytest.mark.parametrize("state", list(ConfigEntryState))
def test_action_runtime_requires_loaded_entry_state(state):
    runtime = EnphaseRuntimeData(coordinator=SimpleNamespace())
    entry = SimpleNamespace(state=state, disabled_by=None, runtime_data=runtime)
    assert loaded_runtime_data(entry) is (
        runtime if state is ConfigEntryState.LOADED else None
    )
    entry.disabled_by = "user"
    assert loaded_runtime_data(entry) is None
    entry.state = ConfigEntryState.LOADED
    entry.disabled_by = None
    entry.runtime_data = None
    assert loaded_runtime_data(entry) is None
