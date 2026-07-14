"""Tests for aggregate coordinator publication state."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from custom_components.enphase_ev.current_power_runtime import CurrentPowerSample
from custom_components.enphase_ev.evse_feature_flags_runtime import (
    EvseFeatureFlagsSnapshot,
)
from custom_components.enphase_ev.integration_snapshot import (
    CoordinatorData,
    IntegrationSnapshot,
    freeze_charger_data,
)
from custom_components.enphase_ev.snapshot_helpers import freeze_snapshot_value


def _snapshot(
    *,
    power: float | None = None,
    runtime_revisions: tuple[tuple[str, int], ...] = (),
    revision: int = 1,
) -> IntegrationSnapshot:
    return IntegrationSnapshot(
        chargers=freeze_charger_data({"EVSE-1": {"charging": False}}),
        evse_feature_flags=EvseFeatureFlagsSnapshot(
            payload=None,
            site_feature_flags={},
            charger_feature_flags_by_serial={},
            charger_serial_count=0,
        ),
        current_power=CurrentPowerSample(w=power),
        runtime_revisions=runtime_revisions,
        revision=revision,
    )


def test_snapshot_is_read_only_and_revision_is_not_update_equality() -> None:
    first = _snapshot(revision=1)
    second = _snapshot(revision=2)

    assert first == second
    with pytest.raises(TypeError):
        first.chargers["EVSE-1"]["charging"] = True  # type: ignore[index]


def test_snapshot_recursively_freezes_nested_charger_data() -> None:
    source = {"EVSE-1": {"nested": {"values": [1, 2]}}}
    frozen = freeze_charger_data(source)
    source["EVSE-1"]["nested"] = {"values": [3]}

    nested = frozen["EVSE-1"]["nested"]
    assert nested == {"values": (1, 2)}
    with pytest.raises(TypeError):
        nested["other"] = True  # type: ignore[index,operator]

    assert freeze_snapshot_value({1, 2}) == frozenset({1, 2})


def test_coordinator_data_preserves_dict_api_and_uses_aggregate_equality() -> None:
    first = CoordinatorData({"EVSE-1": {"charging": False}}, _snapshot())
    runtime_changed = CoordinatorData(
        {"EVSE-1": {"charging": False}},
        _snapshot(runtime_revisions=(("grid_profile", 1),), revision=2),
    )

    assert first == {"EVSE-1": {"charging": False}}
    assert first != runtime_changed
    assert first["EVSE-1"]["charging"] is False


def test_runtime_snapshots_drive_coordinator_publication(coordinator_factory) -> None:
    coord = coordinator_factory()
    data = {"EVSE-1": {"charging": False}}

    coord.async_set_updated_data(data)
    first = coord.integration_snapshot
    assert first is not None
    assert first.revision == 1

    coord.async_set_updated_data(data)
    unchanged = coord.integration_snapshot
    assert unchanged is not None
    assert unchanged.revision == 1

    coord.current_power_runtime.replace_snapshot(
        w=123.0,
        sample_utc=datetime.now(UTC),
    )
    coord.publish_runtime_state_update("current_power")
    changed = coord.integration_snapshot
    assert changed is not None
    assert changed.revision == 2
    assert changed.current_power.w == 123.0
    assert coord.current_power_snapshot.w == 123.0
    assert changed.runtime_revisions == (("current_power", 1),)


def test_evse_feature_flag_runtime_snapshot_is_detached(coordinator_factory) -> None:
    coord = coordinator_factory()
    runtime = coord.evse_feature_flags_runtime
    runtime.replace_payload({"data": {}})
    runtime.replace_site_feature_flags({"remote_start": True})
    runtime.replace_charger_feature_flags({"EVSE-1": {"plug_and_charge": False}})

    snapshot = coord.evse_feature_flags_snapshot
    runtime.replace_site_feature_flags({})

    assert snapshot.site_feature_flags == {"remote_start": True}
    with pytest.raises(TypeError):
        snapshot.site_feature_flags["other"] = True  # type: ignore[index]


def test_migrated_state_compatibility_before_runtime_construction(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    current_power_runtime = coord.__dict__.pop("current_power_runtime")
    feature_flags_runtime = coord.__dict__.pop("evse_feature_flags_runtime")
    try:
        coord._current_power_consumption_w = 50.0  # noqa: SLF001
        assert coord._current_power_consumption_w == 50.0  # noqa: SLF001

        coord._evse_feature_flags_cache_until = 42.0  # noqa: SLF001
        coord._evse_feature_flags_payload = {"data": {}}  # noqa: SLF001
        coord._evse_site_feature_flags = {"site": True}  # noqa: SLF001
        coord._evse_feature_flags_by_serial = {  # noqa: SLF001
            "EVSE-1": {"charger": True}
        }
        assert coord._evse_feature_flags_cache_until == 42.0  # noqa: SLF001
        assert coord._evse_feature_flags_payload == {"data": {}}  # noqa: SLF001
        assert coord._evse_site_feature_flags == {"site": True}  # noqa: SLF001
        assert coord._evse_feature_flags_by_serial == {  # noqa: SLF001
            "EVSE-1": {"charger": True}
        }
    finally:
        coord.__dict__["current_power_runtime"] = current_power_runtime
        coord.__dict__["evse_feature_flags_runtime"] = feature_flags_runtime


def test_feature_flag_snapshot_supports_legacy_coordinator_shape() -> None:
    coord = SimpleNamespace(
        _evse_feature_flags_payload={"meta": {}},
        _evse_site_feature_flags={"site": True},
        _evse_feature_flags_by_serial={"EVSE-1": {"charger": True}},
    )

    snapshot = EvseFeatureFlagsSnapshot.from_coordinator(coord)  # type: ignore[arg-type]

    assert snapshot.payload == {"meta": {}}
    assert snapshot.site_feature_flags == {"site": True}
    assert snapshot.charger_serial_count == 1
