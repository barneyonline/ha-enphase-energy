from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.enphase_ev.entity_cleanup import (
    entries_for_device,
    is_owned_entity,
    iter_entity_registry_entries,
    prune_managed_entities,
)


def test_iter_entity_registry_entries_handles_edge_shapes() -> None:
    assert iter_entity_registry_entries(SimpleNamespace()) == []
    assert iter_entity_registry_entries(SimpleNamespace(entities={"one": 1})) == [1]

    class _ValuesRaises:
        def values(self):
            raise RuntimeError("boom")

    assert iter_entity_registry_entries(SimpleNamespace(entities=_ValuesRaises())) == []

    class _DictWithNonCallableValues(dict):
        values = 1

    assert iter_entity_registry_entries(
        SimpleNamespace(entities=_DictWithNonCallableValues({"one": 1}))
    ) == [1]
    assert (
        iter_entity_registry_entries(
            SimpleNamespace(entities=SimpleNamespace(values=1))
        )
        == []
    )


def test_entries_for_device_scans_registry_when_ha_helper_returns_empty(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "custom_components.enphase_ev.entity_cleanup.er.async_entries_for_device",
        lambda _ent_reg, _device_id: [],
    )
    ent_reg = SimpleNamespace(
        entities={
            "sensor.attached": SimpleNamespace(
                entity_id="sensor.attached",
                device_id="device-1",
            ),
            "sensor.other": SimpleNamespace(
                entity_id="sensor.other",
                device_id="device-2",
            ),
        }
    )

    entries = entries_for_device(ent_reg, "device-1")

    assert [entry.entity_id for entry in entries] == ["sensor.attached"]


def test_is_owned_entity_filters_domain_platform_and_entry() -> None:
    assert (
        is_owned_entity(
            SimpleNamespace(
                entity_id="switch.test",
                domain="switch",
                platform="enphase_ev",
                config_entry_id="entry-1",
            ),
            "entry-1",
            "switch",
        )
        is True
    )
    assert (
        is_owned_entity(
            SimpleNamespace(
                entity_id="sensor.test",
                domain="sensor",
                platform="enphase_ev",
                config_entry_id="entry-1",
            ),
            "entry-1",
            "switch",
        )
        is False
    )
    assert (
        is_owned_entity(
            SimpleNamespace(
                entity_id="switch.test",
                platform="enphase_ev",
                config_entry_id="entry-1",
            ),
            "entry-1",
            "switch",
        )
        is True
    )
    assert (
        is_owned_entity(
            SimpleNamespace(
                entity_id=None,
                platform="enphase_ev",
                config_entry_id="entry-1",
            ),
            "entry-1",
            "switch",
        )
        is False
    )
    assert (
        is_owned_entity(
            SimpleNamespace(
                entity_id="switch.test",
                domain="switch",
                platform="other",
                config_entry_id="entry-1",
            ),
            "entry-1",
            "switch",
        )
        is False
    )
    assert (
        is_owned_entity(
            SimpleNamespace(
                entity_id="switch.test",
                domain="switch",
                platform="enphase_ev",
                config_entry_id="entry-2",
            ),
            "entry-1",
            "switch",
        )
        is False
    )


def test_prune_managed_entities_removes_only_stale_owned_entries() -> None:
    removed: list[str] = []
    ent_reg = SimpleNamespace(
        entities={
            "switch.keep": SimpleNamespace(
                entity_id="switch.keep",
                unique_id="enphase_ev_keep",
                domain="switch",
                platform="enphase_ev",
                config_entry_id="entry-1",
            ),
            "switch.remove": SimpleNamespace(
                entity_id="switch.remove",
                unique_id="enphase_ev_remove",
                domain="switch",
                platform="enphase_ev",
                config_entry_id="entry-1",
            ),
            "switch.other": SimpleNamespace(
                entity_id="switch.other",
                unique_id="enphase_ev_other",
                domain="switch",
                platform="enphase_ev",
                config_entry_id="entry-2",
            ),
        },
        async_remove=lambda entity_id: removed.append(entity_id),
    )

    assert (
        prune_managed_entities(
            ent_reg,
            "entry-1",
            domain="switch",
            active_unique_ids={"enphase_ev_keep"},
            is_managed=lambda unique_id: unique_id.startswith("enphase_ev_"),
        )
        == 1
    )
    assert removed == ["switch.remove"]


def test_prune_managed_entities_handles_remove_failures() -> None:
    ent_reg = SimpleNamespace(
        entities={
            "switch.remove": SimpleNamespace(
                entity_id="switch.remove",
                unique_id="enphase_ev_remove",
                domain="switch",
                platform="enphase_ev",
                config_entry_id="entry-1",
            )
        },
        async_remove=MagicMock(side_effect=RuntimeError("boom")),
    )

    assert (
        prune_managed_entities(
            ent_reg,
            "entry-1",
            domain="switch",
            active_unique_ids=set(),
            is_managed=lambda unique_id: True,
        )
        == 0
    )


def test_prune_managed_entities_skips_entries_without_entity_id() -> None:
    ent_reg = SimpleNamespace(
        entities={
            "switch.no_entity_id": SimpleNamespace(
                entity_id=None,
                unique_id="enphase_ev_remove",
                domain="switch",
                platform="enphase_ev",
                config_entry_id="entry-1",
            )
        },
        async_remove=MagicMock(),
    )

    assert (
        prune_managed_entities(
            ent_reg,
            "entry-1",
            domain="switch",
            active_unique_ids=set(),
            is_managed=lambda unique_id: True,
        )
        == 0
    )
    ent_reg.async_remove.assert_not_called()
