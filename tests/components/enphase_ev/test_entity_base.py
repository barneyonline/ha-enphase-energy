from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from custom_components.enphase_ev.entity import EnphaseBaseEntity
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL


class DummyEntity(EnphaseBaseEntity):
    """Concrete subclass for exercising base entity logic."""


def test_charge_mode_ignores_blank_resolved_and_raw_modes():
    from custom_components.enphase_ev.entity import evse_resolved_charge_mode

    coord = SimpleNamespace(
        _resolve_charge_mode_pref=lambda serial: " ",
        data={
            RANDOM_SERIAL: {"charge_mode_pref": " ", "charge_mode": "manual_charging"}
        },
    )
    assert evse_resolved_charge_mode(coord, RANDOM_SERIAL) == "MANUAL_CHARGING"


def test_base_entity_data_without_coordinator_is_empty():
    entity = object.__new__(EnphaseBaseEntity)
    assert entity.data == {}


def test_base_entity_initially_rejects_non_mapping_data(coordinator_factory):
    coord = coordinator_factory()
    coord.data = ["invalid"]
    entity = DummyEntity(coord, RANDOM_SERIAL)
    assert not entity.available
    assert entity.data == {}


@pytest.mark.parametrize("had_data", [False, True])
def test_base_entity_repeated_missing_data_does_not_repeat_log(
    coordinator_factory, caplog, had_data
):
    coord = coordinator_factory(data={})
    entity = DummyEntity(coord, RANDOM_SERIAL)
    entity._ever_had_data = had_data
    entity.async_write_ha_state = lambda: None
    caplog.set_level(logging.INFO)
    entity._handle_coordinator_update()
    assert "data unavailable" not in caplog.text


def test_base_entity_data_falls_back_to_coordinator(coordinator_factory):
    coord = coordinator_factory()
    entity = DummyEntity(coord, RANDOM_SERIAL)
    delattr(entity, "_data")
    coord.data[RANDOM_SERIAL]["hw_version"] = "1.2"

    assert entity.data["hw_version"] == "1.2"


def test_base_entity_data_handles_missing_coord():
    class RaisesAttributeError:
        @property
        def data(self):
            raise AttributeError("boom")

    entity = object.__new__(EnphaseBaseEntity)
    entity._coord = RaisesAttributeError()  # type: ignore[attr-defined]
    entity._sn = "nope"  # type: ignore[attr-defined]

    assert entity.data == {}


def test_base_entity_logs_transitions(coordinator_factory, caplog):
    coord = coordinator_factory(
        data={RANDOM_SERIAL: {"sn": RANDOM_SERIAL, "name": "Garage"}}
    )
    entity = DummyEntity(coord, RANDOM_SERIAL)
    entity.async_write_ha_state = lambda *args, **kwargs: None  # type: ignore[attr-defined]

    caplog.set_level(logging.INFO)
    entity._unavailable_logged = True  # type: ignore[attr-defined]
    coord.data = {RANDOM_SERIAL: {"sn": RANDOM_SERIAL}}
    entity._handle_coordinator_update()
    assert "data available again" in caplog.text
    assert entity._unavailable_logged is False  # type: ignore[attr-defined]

    caplog.clear()
    entity._ever_had_data = True  # type: ignore[attr-defined]
    entity._unavailable_logged = False  # type: ignore[attr-defined]
    entity._has_data = True  # type: ignore[attr-defined]
    coord.data = {}
    coord._last_error = "timeout"  # type: ignore[attr-defined]
    entity._handle_coordinator_update()
    assert "data unavailable (timeout)" in caplog.text
    assert entity._unavailable_logged is True  # type: ignore[attr-defined]

    caplog.clear()
    entity._unavailable_logged = False  # type: ignore[attr-defined]
    entity._has_data = True  # type: ignore[attr-defined]
    coord._last_error = None  # type: ignore[attr-defined]
    entity._handle_coordinator_update()
    assert "data unavailable" in caplog.text
    assert entity._unavailable_logged is True  # type: ignore[attr-defined]


def test_base_entity_data_returns_empty_when_source_not_mapping():
    class NonMappingCoord:
        def __init__(self) -> None:
            self.data = ["bad-entry"]

    entity = object.__new__(EnphaseBaseEntity)
    entity._coord = NonMappingCoord()  # type: ignore[attr-defined]
    entity._sn = "missing"  # type: ignore[attr-defined]

    assert entity.data == {}
