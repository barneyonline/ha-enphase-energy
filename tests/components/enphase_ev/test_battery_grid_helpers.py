"""Grid relay observation normalization independent of coordinator ownership."""

import pytest

from custom_components.enphase_ev.battery_grid_helpers import (
    grid_relay_candidates,
    normalize_grid_mode_status_value,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("unsupported", None),
        (" oper_relay_open ", "off_grid"),
        ("OPER_RELAY_OFFGRID_AC_GRID_PRESENT", "off_grid"),
        ("OPER_RELAY_OFFGRID_READY_FOR_RESYNC_CMD", "off_grid"),
        ("OPER_RELAY_CLOSED", "on_grid"),
        ("OPER_RELAY_WAITING_TO_INITIALIZE_ON_GRID", "on_grid"),
    ],
)
def test_relay_mode_normalization(value, expected):
    assert normalize_grid_mode_status_value(value) == expected


def test_relay_wrapper_traversal_preserves_candidate_order_and_unknown_values():
    payload = {
        "gridRelay": "primary",
        "grid_relay": None,
        "meters": [None, {"gridRelay": "meter"}],
        "data": {"meters": {"gridRelay": "nested meter"}},
        "payload": [{"gridRelay": "nested payload"}],
        "message": "unparsed message",
        "unrelated": {"gridRelay": "ignored"},
    }
    assert grid_relay_candidates(payload) == [
        "primary",
        None,
        "meter",
        "nested meter",
        "nested payload",
    ]
    assert grid_relay_candidates("unsupported scalar") == []
