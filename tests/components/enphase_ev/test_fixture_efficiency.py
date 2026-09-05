"""Regression tests for low-overhead shared pytest fixtures."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from .random_ids import RANDOM_SERIAL


def test_pure_unit_test_does_not_start_home_assistant(hass_fixture_setup) -> None:
    """Keep the shared autouse fixtures from constructing Home Assistant."""
    assert hass_fixture_setup == []


def test_namespace_has_no_implicit_inventory_capabilities():
    """Importing test fixtures must not modify the standard library."""
    assert SimpleNamespace.__module__ == "types"
    assert not hasattr(SimpleNamespace(), "inventory_view")


def test_coordinator_factory_preserves_empty_inputs(coordinator_factory):
    empty = coordinator_factory(serials=[], data={})
    assert empty.serials == set()
    assert empty.data == {}
    assert not empty.inventory_view.has_type("iqevse")

    waiting = coordinator_factory(serials=[RANDOM_SERIAL], data={})
    assert waiting.serials == {RANDOM_SERIAL}
    assert waiting.data == {}

    default = coordinator_factory()
    assert default.serials == {RANDOM_SERIAL}
    assert RANDOM_SERIAL in default.data


def test_coordinator_factory_preserves_empty_config(coordinator_factory):
    with pytest.raises(KeyError, match="site_id"):
        coordinator_factory(config={}, serials=[])
