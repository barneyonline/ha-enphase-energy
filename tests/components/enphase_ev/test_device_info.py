from types import SimpleNamespace

import pytest


def test_device_info_uses_display_name_and_model():
    from custom_components.enphase_ev.entity import EnphaseBaseEntity
    from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC

    entity = object.__new__(EnphaseBaseEntity)
    entity._coord = SimpleNamespace(
        data={
            "555555555555": {
                "display_name": "Garage Charger",
                "model_name": "IQ-EVSE-EU-3032",
                "model_id": "IQ-EVSE-EU-3032-01",
                "mac_address": "00-11-22-33-44-55",
                "hw_version": "2.0",
                "sw_version": "3.1",
            }
        },
        site_id="1234567",
    )
    entity._sn = "555555555555"

    info = entity.device_info

    assert info["name"] == "Garage Charger"
    assert info["model"] == "Garage Charger (IQ-EVSE-EU-3032)"
    assert "model_id" not in info
    assert info["serial_number"] == "555555555555"
    assert info["sw_version"] == "3.1"
    assert info["connections"] == {(CONNECTION_NETWORK_MAC, "00:11:22:33:44:55")}


def test_device_info_keeps_non_redundant_model_id():
    from custom_components.enphase_ev.entity import EnphaseBaseEntity

    entity = object.__new__(EnphaseBaseEntity)
    entity._coord = SimpleNamespace(
        data={
            "1000": {
                "display_name": "Garage Charger",
                "model_name": "IQ-EVSE-EU-3032",
                "model_id": "IQ-EVSE-BOARD-REV-A",
            }
        },
        site_id="1234567",
    )
    entity._sn = "1000"

    info = entity.device_info
    assert info["model"] == "Garage Charger (IQ-EVSE-EU-3032)"
    assert info["model_id"] == "IQ-EVSE-BOARD-REV-A"


def test_device_info_falls_back_to_model_name():
    from custom_components.enphase_ev.entity import EnphaseBaseEntity

    entity = object.__new__(EnphaseBaseEntity)
    entity._coord = SimpleNamespace(
        data={
            "1234": {
                "model_name": "IQ-EVSE-EU-3032",
                "hw_version": "2.1",
            }
        },
        site_id="7654321",
    )
    entity._sn = "1234"

    info = entity.device_info

    assert info["name"] == "IQ-EVSE-EU-3032"
    assert info["model"] == "IQ-EVSE-EU-3032"
    assert info["identifiers"] == {("enphase_ev", "1234")}


def test_device_info_defaults_when_metadata_missing():
    from custom_components.enphase_ev.entity import EnphaseBaseEntity

    entity = object.__new__(EnphaseBaseEntity)
    entity._coord = SimpleNamespace(
        data={"321": {}},
        site_id="1001",
    )
    entity._sn = "321"

    info = entity.device_info

    assert info["name"] == "Enphase Energy Charger"
    assert info.get("model") is None


def test_device_info_ignores_bad_mac_values():
    from custom_components.enphase_ev.entity import EnphaseBaseEntity

    class BadMac:
        def __str__(self) -> str:
            raise ValueError("boom")

    entity = object.__new__(EnphaseBaseEntity)
    entity._coord = SimpleNamespace(
        data={
            "321": {
                "display_name": "Garage Charger",
                "mac_address": BadMac(),
            }
        },
        site_id="1001",
    )
    entity._sn = "321"

    info = entity.device_info
    assert "connections" not in info


def test_device_info_uses_display_name_when_model_missing():
    from custom_components.enphase_ev.entity import EnphaseBaseEntity

    entity = object.__new__(EnphaseBaseEntity)
    entity._coord = SimpleNamespace(
        data={
            "999": {
                "display_name": "Driveway Charger",
            }
        },
        site_id="1002",
    )
    entity._sn = "999"

    info = entity.device_info

    assert info["name"] == "Driveway Charger"
    assert info["model"] == "Driveway Charger"


def test_cloud_device_info_uses_unknown_for_blank_site_id():
    from custom_components.enphase_ev.device_info_helpers import _cloud_device_info

    info = _cloud_device_info("   ")
    assert info["identifiers"] == {("enphase_ev", "type:unknown:cloud")}
    assert info["name"] == "Enphase Cloud"
    assert info["model"] == "Cloud Service"


def test_cloud_device_info_handles_bad_site_id_str():
    from custom_components.enphase_ev.device_info_helpers import _cloud_device_info

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    info = _cloud_device_info(BadStr())
    assert info["identifiers"] == {("enphase_ev", "type:unknown:cloud")}


def test_device_info_helpers_imports_without_home_assistant(monkeypatch):
    import builtins
    import importlib

    import custom_components.enphase_ev.device_info_helpers as helpers

    original_import = builtins.__import__

    def _blocked_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name.startswith("homeassistant"):
            raise ModuleNotFoundError(name)
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)
    helpers = importlib.reload(helpers)

    info = helpers._cloud_device_info("12345")
    assert info["identifiers"] == {("enphase_ev", "type:12345:cloud")}
    assert info["name"] == "Enphase Cloud"


def test_evse_display_name_normalization_and_model_deduping() -> None:
    from custom_components.enphase_ev.device_info_helpers import (
        _clean_text,
        _compose_charger_model_display,
        _is_redundant_model_id,
        _normalize_evse_display_name,
    )

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert _normalize_evse_display_name(BadStr()) is None
    assert (
        _normalize_evse_display_name(
            "Q EV Charger (IQ-EVSE-EU-3032) (IQ-EVSE-EU-3032-0105-1300)"
        )
        == "IQ EV Charger (IQ-EVSE-EU-3032)"
    )
    assert (
        _compose_charger_model_display(
            "Q EV Charger (IQ-EVSE-EU-3032) (IQ-EVSE-EU-3032-0105-1300)",
            "IQ-EVSE-EU-3032-0105-1300",
        )
        == "IQ EV Charger (IQ-EVSE-EU-3032)"
    )
    assert _is_redundant_model_id(
        "IQ EV Charger (IQ-EVSE-EU-3032)", "IQ-EVSE-EU-3032-0105-1300"
    )
    assert _is_redundant_model_id("B05-T02-ROW00-1-2", "B05-T02-ROW00-1-2")
    assert _is_redundant_model_id("IQ EV Charger IQ-EVSE-EU-3032", "IQ-EVSE-EU-3032")
    assert not _is_redundant_model_id("IQ EV Charger (AB12)", "AB12-0000")
    assert not _is_redundant_model_id("IQ Battery", "B05-T02-ROW00-1-2")
    assert _clean_text("   ") is None
    assert _clean_text(None) is None

    class _BadText:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert _clean_text(_BadText()) is None


def test_integration_version_handles_manifest_edge_cases(monkeypatch) -> None:
    import custom_components.enphase_ev.device_info_helpers as helpers

    helpers._integration_version.cache_clear()

    def _raise(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(helpers.Path, "read_text", _raise)
    assert helpers._integration_version() is None

    helpers._integration_version.cache_clear()
    monkeypatch.setattr(
        helpers.Path, "read_text", lambda *_args, **_kwargs: '{"version": 1}'
    )
    assert helpers._integration_version() is None


@pytest.mark.asyncio
async def test_async_prime_integration_version_uses_executor(hass, monkeypatch) -> None:
    import custom_components.enphase_ev.device_info_helpers as helpers

    called = False

    def _fake_integration_version() -> str | None:
        nonlocal called
        called = True
        return "2.0.0b4"

    monkeypatch.setattr(helpers, "_integration_version", _fake_integration_version)
    await helpers.async_prime_integration_version(hass)
    assert called
