"""Time weighting, bounded history, and untrusted restore input contracts."""

import pytest

from custom_components.enphase_ev.power_average import AverageUpdate, EnergyPowerAverage


def test_weighted_window_and_retained_bracket():
    average = EnergyPowerAverage()
    assert average.value() is None
    for stamp in (1000, 1240, 1600, 1870, 2200):
        average.add(stamp, stamp / 600, "source")
    assert average.value() == (6000, 900)
    assert average.samples[0][0] == 1240
    average.add(2200, 100, "source")
    average.add(2000, 100, "source")
    average.add(float("nan"), 100, "source")
    average.add(2300, float("inf"), "source")
    assert average.value() == (6000, 900)


def test_gap_clock_and_signed_values():
    average = EnergyPowerAverage()
    average.add(1000, 100, "source")
    average.add(1300, 99.5, "source")
    assert average.value() == (-6000, 300)
    average.add(4000, 98, "source")
    assert average.value() is None
    average.add(4300, 97.5, "receipt")
    assert average.value() is None
    average.add(4600, 97, "receipt")
    assert average.value() == (-6000, 300)


def test_restore_and_bounds():
    average = EnergyPowerAverage()
    average.add(1000, 100, "source")
    average.add(1300, 100.5, "source")
    restored = EnergyPowerAverage()
    restored.restore(average.as_dict(), 1400)
    assert restored.value() == (6000, 300)
    restored.restore(average.as_dict(), 3100)
    assert restored.value() is None
    for i in range(1100):
        average.add(1301 + i / 10, 101, "source")
    assert len(average.samples) == 1024


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        {"version": 2},
        {"version": 1, "clock": "invalid", "samples": []},
        {"version": 1, "clock": "source", "samples": None},
        *[
            {"version": 1, "clock": "source", "samples": [item]}
            for item in [
                None,
                [],
                [1, 2, 3],
                [True, 1],
                [1, False],
                ["1", 1],
                [1, "2"],
                [float("nan"), 1],
                [1, float("inf")],
                [0, 1],
                [2000, 1],
            ]
        ],
        {"version": 1, "clock": "source", "samples": [[1000, 1], [1000, 2]]},
        {"version": 1, "clock": "source", "samples": []},
    ],
)
def test_invalid_restore(raw):
    average = EnergyPowerAverage()
    average.restore(raw, 1500)
    assert average.value() is None


@pytest.mark.parametrize(
    "method", ["extreme_pending", "outlier_ignored", "no_live_data"]
)
def test_rejected_observations_never_seed_history(method):
    average = EnergyPowerAverage()
    average.add(1000, 100, "source")
    average.add(1300, 100.5, "source")
    result = average.observe_lifetime(
        timestamp=1600,
        values={"import": 500000},
        signs={"import": 1},
        source_clock=True,
        advanced=True,
        interval=AverageUpdate(6000, 300, method),
    )
    assert result.watts == 6000
    assert not average.samples


@pytest.mark.parametrize(
    "raw",
    [
        {"version": 1, "clock": [], "samples": []},
        {"version": 1, "clock": {}, "samples": []},
        {"version": 1, "clock": "source", "samples": [[10**1000, 100]]},
        {"version": 1, "clock": "source", "samples": [[1000, 10**1000]]},
        {"version": 1, "clock": "source", "samples": [[1000, 1e308], [1300, 100]]},
        {"version": 1, "clock": "source", "samples": [[1000, 1e8], [1300, 100]]},
    ],
)
def test_malformed_restore_cannot_crash_or_inject_extreme_power(raw):
    average = EnergyPowerAverage()
    average.restore(raw, 1400)
    assert not average.samples


@pytest.mark.parametrize("sign", [-1, 1])
def test_repeated_pending_average_preserves_last_accepted_value(sign):
    average = EnergyPowerAverage()

    def observe(stamp, energy, interval_watts, advanced=True):
        return average.observe_lifetime(
            timestamp=stamp,
            values={"flow": energy},
            signs={"flow": sign},
            source_clock=True,
            advanced=advanced,
            interval=AverageUpdate(
                interval_watts * sign, 300, "lifetime_energy_window"
            ),
        )

    observe(1000, 100, 0)
    assert observe(1001, 100.001, 12).watts == 3600 * sign
    pending = observe(1002, 110.001, 120000)
    assert pending == AverageUpdate(3600 * sign, 1, "extreme_pending")
    assert observe(1002, 110.001, 120000, False) == pending
    assert observe(1003, 120.001, 120000).method == "extreme_confirmed"


def test_flat_curve_restores_without_renewing_its_clock():
    average = EnergyPowerAverage()
    average.restore(
        {"version": 1, "clock": "source", "samples": [[1000, 100], [1300, 100]]}, 1400
    )
    assert average.value() == (0, 300)
    assert average.samples[-1][0] == 1300
