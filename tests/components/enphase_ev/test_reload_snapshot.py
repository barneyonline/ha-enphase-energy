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


def test_reload_snapshot_detaches_and_restores_only_selected_chargers():
    data = {"a": {"values": [1, 2], "flags": {"ready"}}, "b": {"value": 3}}
    source = SimpleNamespace(
        site_id="site",
        data=data,
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
        discovery_snapshot=SimpleNamespace(capture=lambda: {}),
        last_success_utc=None,
        last_update_success=True,
    )
    snapshot = ReloadSnapshot.capture(source)
    target = SimpleNamespace(
        site_id="site",
        serials=set(),
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
