from __future__ import annotations

from types import SimpleNamespace

import pytest
from homeassistant.config_entries import ConfigEntryState

from custom_components.enphase_ev.runtime_data import (
    EnphaseRuntimeData,
    get_runtime_data,
    iter_coordinators,
)


def test_get_runtime_data_returns_runtime_data_object() -> None:
    runtime_data = EnphaseRuntimeData(
        coordinator=SimpleNamespace(site_id="1234"),
        firmware_catalog=SimpleNamespace(),
        evse_firmware_details=SimpleNamespace(),
    )
    entry = SimpleNamespace(
        state=ConfigEntryState.LOADED,
        disabled_by=None,
        runtime_data=runtime_data,
        entry_id="entry-1",
    )

    assert get_runtime_data(entry) is runtime_data


def test_get_runtime_data_raises_when_missing() -> None:
    entry = SimpleNamespace(
        state=ConfigEntryState.LOADED,
        disabled_by=None,
        runtime_data=None,
        entry_id="entry-2",
    )

    with pytest.raises(RuntimeError, match="Missing runtime data for entry entry-2"):
        get_runtime_data(entry)


def test_iter_coordinators_deduplicates_and_filters_by_site() -> None:
    coord_one = SimpleNamespace(site_id="1234")
    coord_duplicate = SimpleNamespace(site_id="1234")
    coord_other = SimpleNamespace(site_id="9999")
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_entries=lambda domain: [
                SimpleNamespace(
                    state=ConfigEntryState.LOADED,
                    disabled_by=None,
                    runtime_data=EnphaseRuntimeData(coordinator=coord_one),
                ),
                SimpleNamespace(
                    state=ConfigEntryState.LOADED,
                    disabled_by=None,
                    runtime_data=EnphaseRuntimeData(coordinator=coord_duplicate),
                ),
                SimpleNamespace(
                    state=ConfigEntryState.LOADED,
                    disabled_by=None,
                    runtime_data=EnphaseRuntimeData(coordinator=coord_other),
                ),
                SimpleNamespace(
                    state=ConfigEntryState.LOADED, disabled_by=None, runtime_data=None
                ),
            ]
        )
    )

    assert iter_coordinators(hass) == [coord_one, coord_other]
    assert iter_coordinators(hass, site_ids={"9999"}) == [coord_other]


def test_internal_updates_preserve_only_changed_fields():
    runtime = EnphaseRuntimeData(
        coordinator=SimpleNamespace(), applied_data={"remove": 1, "user": 1}
    )
    runtime.mark_internal_data_update(
        {"remove": 1, "user": 2}, {"token": "new", "user": 2}
    )
    runtime.mark_internal_data_update({"token": "new"}, {"token": "newer"})
    runtime.consume_internal_data_updates()
    assert runtime.applied_data == {"user": 1, "token": "newer"}
    assert runtime.reload_suppression_count == 0
    assert runtime.internal_data_updates == []
    runtime.mark_internal_data_update({}, {"failed": True})
    runtime.unmark_internal_data_update()
    runtime.unmark_internal_data_update()
    assert runtime.internal_data_updates == []
    assert runtime.reload_suppression_count == 0
    runtime.applied_data = None
    runtime.mark_internal_data_update({}, {"setup": True})
    runtime.consume_internal_data_updates()
    assert runtime.applied_data is None
