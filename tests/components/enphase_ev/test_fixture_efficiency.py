"""Regression tests for low-overhead shared pytest fixtures."""

from __future__ import annotations


def test_pure_unit_test_does_not_start_home_assistant(hass_fixture_setup) -> None:
    """Keep the shared autouse fixtures from constructing Home Assistant."""
    assert hass_fixture_setup == []
