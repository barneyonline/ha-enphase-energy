"""Verify shared normalization preserves distinct cloud payload policies."""

from datetime import datetime, timezone

import pytest

from custom_components.enphase_ev.scalar_helpers import (
    coerce_optional_bool,
    coerce_snapshot_bool,
    snapshot_compatible_value,
    sum_optional_values,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (True, True),
        (False, False),
        (0, False),
        (2.5, True),
        (" YES ", True),
        ("disabled", False),
        ("other", None),
        ({}, None),
    ],
)
def test_boolean_payload_policies(value, expected):
    """Ordinary telemetry has the same meaning for both consumers."""
    assert coerce_snapshot_bool(value) is expected
    assert coerce_optional_bool(value) is expected


def test_imperative_capabilities_are_not_telemetry():
    """Consolidation must not expand the discovery snapshot vocabulary."""
    assert coerce_optional_bool(" ENABLE ") is True
    assert coerce_optional_bool(" DISABLE ") is False
    assert coerce_snapshot_bool("enable") is None
    assert coerce_snapshot_bool("disable") is None


def test_sum_distinguishes_missing_zero_and_bad_samples():
    assert sum_optional_values(None) is None
    assert sum_optional_values([]) is None
    assert sum_optional_values([None, "bad", float("nan"), float("inf")]) is None
    assert sum_optional_values([None, "0"]) == 0
    assert sum_optional_values(["1.5", 2, "bad", float("-inf")]) == 3.5


class _InvalidText:
    def __str__(self):
        raise ValueError("invalid text")


def test_snapshot_detaches_nested_metadata_and_tolerates_invalid_text():
    timestamp = datetime(2026, 9, 5, tzinfo=timezone.utc)
    original = {1: [timestamp, (True, 2), {3}], _InvalidText(): "omitted"}
    result = snapshot_compatible_value(original)
    assert result == {"1": [timestamp.isoformat(), [True, 2], [3]]}
    original[1].append("later")
    assert result == {"1": [timestamp.isoformat(), [True, 2], [3]]}
    assert snapshot_compatible_value(_InvalidText()) is None
    assert snapshot_compatible_value(b"text") == "b'text'"
