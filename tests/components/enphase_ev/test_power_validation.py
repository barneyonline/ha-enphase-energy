"""Tests for shared extreme site-power validation."""

from custom_components.enphase_ev.power_validation import ExtremePowerValidator


def test_normal_power_is_accepted_and_clears_pending_extreme() -> None:
    validator = ExtremePowerValidator()

    pending = validator.evaluate(1_200_000, sample_ts=100.0)
    accepted = validator.evaluate(50_000, sample_ts=101.0)

    assert pending.accepted is False
    assert accepted.accepted is True
    assert accepted.state == "accepted"
    assert validator.pending_count == 0


def test_extreme_requires_newer_comparable_sample() -> None:
    validator = ExtremePowerValidator()

    first = validator.evaluate(-1_200_000, sample_ts=100.0)
    repeated = validator.evaluate(-1_300_000, sample_ts=100.0)
    confirmed = validator.evaluate(-1_500_000, sample_ts=101.0)

    assert first.reason == "extreme_sample_requires_confirmation"
    assert repeated.reason == "extreme_sample_timestamp_not_newer"
    assert confirmed.accepted is True
    assert confirmed.confirmed_extreme is True
    assert validator.pending_count == 0


def test_missing_timestamp_extreme_never_confirms() -> None:
    validator = ExtremePowerValidator()

    first = validator.evaluate(1_200_000, sample_ts=None)
    second = validator.evaluate(1_300_000, sample_ts=None)

    assert first.reason == "extreme_sample_missing_timestamp"
    assert second.reason == "extreme_sample_missing_timestamp"
    assert validator.pending_value_w == 1_200_000
    assert validator.pending_sample_ts is None


def test_non_comparable_extreme_restarts_confirmation() -> None:
    validator = ExtremePowerValidator()

    validator.evaluate(1_200_000, sample_ts=100.0)
    opposite = validator.evaluate(-1_300_000, sample_ts=101.0)
    too_large = validator.evaluate(-3_000_000, sample_ts=102.0)

    assert opposite.reason == "extreme_sample_not_comparable"
    assert too_large.reason == "extreme_sample_not_comparable"
    assert validator.pending_value_w == -3_000_000
    assert validator.pending_sample_ts == 102.0
