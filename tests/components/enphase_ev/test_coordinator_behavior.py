import asyncio
import logging
import copy
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from homeassistant.exceptions import ServiceValidationError

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.util import dt as dt_util

from custom_components.enphase_ev.const import (
    BATTERY_BACKUP_HISTORY_FAILURE_CACHE_TTL,
    CONF_COOKIE,
    CONF_EAUTH,
    CONF_INCLUDE_INVERTERS,
    CONF_SCAN_INTERVAL,
    CONF_SELECTED_TYPE_KEYS,
    CONF_SERIALS,
    CONF_SITE_ID,
    CONF_SITE_ONLY,
    DEFAULT_SESSION_HISTORY_INTERVAL_MIN,
    DOMAIN,
    ISSUE_DNS_RESOLUTION,
    OPT_NOMINAL_VOLTAGE,
    OPT_SESSION_HISTORY_INTERVAL,
)
from custom_components.enphase_ev.evse_runtime import (
    FAST_TOGGLE_POLL_HOLD_S,
    ChargeModeResolution,
)
from custom_components.enphase_ev.coordinator_refresh_metrics import (
    REFRESH_PERFORMANCE_HISTORY_LIMIT,
    record_refresh_performance_sample,
    refresh_performance_history_summary,
    refresh_performance_summary,
)
from custom_components.enphase_ev.session_history import (
    SESSION_CACHE_STATE_UNAVAILABLE,
    SessionCacheEntry,
    SessionCacheView,
)
from custom_components.enphase_ev.voltage import resolve_nominal_voltage_for_hass

from tests.components.enphase_ev.random_ids import RANDOM_SERIAL, RANDOM_SITE_ID


def _make_coordinator(hass, monkeypatch):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev import coordinator as coord_mod

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
        CONF_SITE_ONLY: False,
        CONF_INCLUDE_INVERTERS: True,
    }

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        coord_mod,
        "async_call_later",
        lambda *_args, **_kwargs: (lambda: None),
    )
    coord = EnphaseCoordinator(hass, cfg)
    coord.client.storm_guard_profile = AsyncMock(return_value={"data": {}})
    coord.client.storm_guard_alert = AsyncMock(
        return_value={"criticalAlertActive": False, "stormAlerts": []}
    )
    return coord


def _client_response_error(status: int, *, message: str = "", headers=None):
    req = aiohttp.RequestInfo(
        url=aiohttp.client.URL("https://example"),
        method="GET",
        headers={},
        real_url=aiohttp.client.URL("https://example"),
    )
    return aiohttp.ClientResponseError(
        request_info=req,
        history=(),
        status=status,
        message=message,
        headers=headers or {},
    )


@pytest.mark.asyncio
async def test_coordinator_init_normalizes_serials_and_options(hass, monkeypatch):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    class BadSerial:
        def __str__(self):
            raise ValueError("boom")

    config = {
        CONF_SITE_ID: "12345",
        CONF_SERIALS: [None, " EV01 ", "", "EV02", "EV01", BadSerial()],
        CONF_EAUTH: "token",
        CONF_COOKIE: "cookie",
        CONF_SCAN_INTERVAL: 30,
    }

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=config,
        options={
            OPT_NOMINAL_VOLTAGE: "bad",
            OPT_SESSION_HISTORY_INTERVAL: "not-a-number",
        },
    )
    entry.add_to_hass(hass)

    captured_tasks: list = []
    monkeypatch.setattr(
        hass, "async_create_task", lambda coro: captured_tasks.append(coro)
    )
    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )

    class DummyClient:
        def __init__(self, *args, **kwargs) -> None:
            self.callbacks: list = []

        def set_reauth_callback(self, cb):
            async def _runner():
                self.callbacks.append(cb)

            return _runner()

    monkeypatch.setattr(coord_mod, "EnphaseEVClient", DummyClient)

    coord = EnphaseCoordinator(hass, config, config_entry=entry)

    assert coord.serials == {"EV01", "EV02"}
    assert coord._serial_order == ["EV01", "EV02"]
    assert coord._configured_serials == {"EV01", "EV02"}
    assert coord._nominal_v == resolve_nominal_voltage_for_hass(hass)
    assert coord._session_history_interval_min == DEFAULT_SESSION_HISTORY_INTERVAL_MIN
    assert coord._session_history_cache_ttl == DEFAULT_SESSION_HISTORY_INTERVAL_MIN * 60
    assert captured_tasks, "set_reauth_callback coroutine should be scheduled"
    await captured_tasks[0]


def test_coordinator_init_defaults_session_history_interval_on_helper_failure(
    hass, monkeypatch
) -> None:
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    config = {
        CONF_SITE_ID: "12345",
        CONF_SERIALS: ["EV01"],
        CONF_EAUTH: "token",
        CONF_COOKIE: "cookie",
        CONF_SCAN_INTERVAL: 30,
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=config,
        options={OPT_SESSION_HISTORY_INTERVAL: "raise"},
    )
    entry.add_to_hass(hass)

    original = coord_mod.helper_coerce_int

    def _coerce(value, *, default):
        if value == "raise":
            raise RuntimeError("bad option")
        return original(value, default=default)

    monkeypatch.setattr(coord_mod, "helper_coerce_int", _coerce)
    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )

    coord = EnphaseCoordinator(hass, config, config_entry=entry)

    assert coord._session_history_interval_min == DEFAULT_SESSION_HISTORY_INTERVAL_MIN


def test_coordinator_init_handles_invalid_selected_type_keys(monkeypatch, hass):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    config = {
        CONF_SITE_ID: "78901",
        CONF_SERIALS: " EV42 ",
        CONF_EAUTH: None,
        CONF_COOKIE: None,
        CONF_SCAN_INTERVAL: 60,
        CONF_SITE_ONLY: False,
        CONF_SELECTED_TYPE_KEYS: 123,
    }

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        coord_mod,
        "EnphaseEVClient",
        lambda *args, **kwargs: SimpleNamespace(set_reauth_callback=lambda *_: None),
    )

    coord = EnphaseCoordinator(hass, config)

    assert coord._selected_type_keys is None  # noqa: SLF001
    assert coord.inventory_view._type_is_selected(None) is False  # noqa: SLF001

    config[CONF_SELECTED_TYPE_KEYS] = "iqevse"
    coord_with_string = EnphaseCoordinator(hass, config)
    assert coord_with_string._selected_type_keys == {"iqevse"}  # noqa: SLF001


def test_coordinator_init_handles_single_serial(monkeypatch, hass):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    config = {
        CONF_SITE_ID: "78901",
        CONF_SERIALS: " EV42 ",
        CONF_EAUTH: None,
        CONF_COOKIE: None,
        CONF_SCAN_INTERVAL: 60,
        CONF_SITE_ONLY: False,
    }

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        coord_mod,
        "EnphaseEVClient",
        lambda *args, **kwargs: SimpleNamespace(set_reauth_callback=lambda *_: None),
    )

    coord = EnphaseCoordinator(hass, config)

    assert coord.serials == {"EV42"}
    assert coord._serial_order == ["EV42"]


def test_inventory_view_iter_type_keys_uses_bucket_keys_when_present(
    hass, monkeypatch
) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    coord._type_device_order = None  # noqa: SLF001
    coord._selected_type_keys = {"envoy"}  # noqa: SLF001
    coord._type_device_buckets = {  # noqa: SLF001
        "envoy": {"count": 1},
        "iqevse": {"count": 1},
    }

    assert coord.inventory_view.iter_type_keys() == ["envoy"]


def test_inventory_view_iter_type_keys_uses_selected_keys_when_no_order_or_buckets(
    hass, monkeypatch
) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    coord._type_device_order = None  # noqa: SLF001
    coord._type_device_buckets = None  # noqa: SLF001
    coord._selected_type_keys = ("envoy", "iqevse")  # noqa: SLF001

    assert coord.inventory_view.iter_type_keys() == ["envoy", "iqevse"]


def test_inventory_view_iter_type_keys_handles_iter_serials_error() -> None:
    from custom_components.enphase_ev.inventory_view import InventoryView

    def _bad_iter_serials():
        raise RuntimeError("boom")

    view = InventoryView(
        SimpleNamespace(
            site_id=RANDOM_SITE_ID,
            data={},
            serials=[],
            iter_serials=_bad_iter_serials,
            _type_device_order=None,
            _type_device_buckets=None,
            _selected_type_keys=None,
            _battery_has_encharge=False,
            _battery_has_acb=False,
        )
    )

    assert view.iter_type_keys() == ["envoy"]


def test_inventory_view_type_identifier_requires_present_type(
    hass, monkeypatch
) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    coord._selected_type_keys = {"iqevse"}  # noqa: SLF001
    coord._type_device_order = None  # noqa: SLF001
    coord._type_device_buckets = {}  # noqa: SLF001

    assert coord.inventory_view.iter_type_keys() == ["iqevse"]
    assert coord.inventory_view.type_identifier("iqevse") is None


def test_type_bucket_includes_extra_summary_fields(hass, monkeypatch) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    coord._type_device_buckets = {  # noqa: SLF001
        "microinverter": {
            "type_key": "microinverter",
            "type_label": "Microinverters",
            "count": 1,
            "devices": [{"serial_number": "INV1", "sku_id": "IQ7A-SKU"}],
            "model_summary": "IQ7A x1",
            "status_summary": "Normal 1 | Warning 0 | Error 0 | Not Reporting 0",
            "status_counts": {
                "normal": 1,
                "warning": 0,
                "error": 0,
                "not_reporting": 0,
            },
        }
    }
    coord._type_device_order = ["microinverter"]  # noqa: SLF001

    bucket = coord.inventory_view.type_bucket("microinverter")
    assert bucket is not None
    assert bucket["model_summary"] == "IQ7A x1"
    assert "status_counts" in bucket
    assert coord.inventory_view.type_device_model("microinverter") == "IQ7A-SKU"
    assert coord.inventory_view.type_device_hw_version("microinverter") == "IQ7A-SKU"


def test_type_device_envoy_prefers_system_controller_metadata(
    hass, monkeypatch
) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 3,
                "devices": [
                    {
                        "name": "IQ System Controller 3 INT",
                        "channelType": "enpower_controller_v3",
                        "serial_number": "SC-123",
                        "sku_id": "SC-SKU",
                        "sw_version": "8.2.1",
                    },
                    {
                        "name": "Consumption Meter",
                        "channel_type": "consumption_meter",
                        "serial_number": "CM-123",
                    },
                    {
                        "name": "Production Meter",
                        "channel_type": "production_meter",
                        "serial_number": "PM-123",
                    },
                ],
            }
        },
        ["envoy"],
    )

    assert coord.inventory_view.type_device_name("envoy") == "IQ Gateway"
    assert (
        coord.inventory_view.type_device_model("envoy") == "IQ System Controller 3 INT"
    )
    assert coord.inventory_view.type_device_serial_number("envoy") == "SC-123"
    assert coord.inventory_view.type_device_model_id("envoy") == "SC-SKU"
    assert coord.inventory_view.type_device_sw_version("envoy") == "8.2.1"

    info = coord.inventory_view.type_device_info("envoy")
    assert info is not None
    assert info["name"] == "IQ Gateway"
    assert info["model"] == "IQ System Controller 3 INT"
    assert info["serial_number"] == "SC-123"
    assert info["model_id"] == "SC-SKU"
    assert info["sw_version"] == "8.2.1"

    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "IQ Gateway", "serial_number": "GW-1"}],
            }
        },
        ["envoy"],
    )
    assert coord.inventory_view.type_device_name("envoy") == "IQ Gateway"
    assert coord.inventory_view.type_device_model("envoy") == "IQ Gateway"
    assert (
        coord.inventory_view._envoy_member_kind({"name": "System Controller Main"})
        == "controller"
    )  # noqa: SLF001
    assert (
        coord.inventory_view._envoy_member_kind(
            {"channel_type": "system_controller_3"}
        )  # noqa: SLF001
        == "controller"
    )
    assert (
        coord.inventory_view._envoy_member_kind({"name": "Main Controller"})
        == "controller"
    )  # noqa: SLF001
    assert (
        coord.inventory_view._envoy_member_kind({"name": "Production Meter"})
        == "production"
    )  # noqa: SLF001
    assert (
        coord.inventory_view._envoy_member_kind({"name": "Consumption Meter"})
        == "consumption"
    )  # noqa: SLF001


def test_type_device_envoy_falls_back_to_gateway_member_metadata(
    hass, monkeypatch
) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 3,
                "devices": [
                    {
                        "name": "IQ Gateway",
                        "serial_number": "GW-123",
                        "sku_id": "SC100G-M230ROW",
                        "envoy_sw_version": "D8.3.5167",
                    },
                    {
                        "name": "Consumption Meter",
                        "channel_type": "consumption_meter",
                        "serial_number": "CM-123",
                    },
                    {
                        "name": "Production Meter",
                        "channel_type": "production_meter",
                        "serial_number": "PM-123",
                    },
                ],
            }
        },
        ["envoy"],
    )

    assert coord.inventory_view.type_device_name("envoy") == "IQ Gateway"
    assert coord.inventory_view.type_device_model("envoy") == "IQ Gateway"
    assert coord.inventory_view.type_device_serial_number("envoy") == "GW-123"
    assert coord.inventory_view.type_device_model_id("envoy") == "SC100G-M230ROW"
    assert coord.inventory_view.type_device_sw_version("envoy") == "D8.3.5167"

    info = coord.inventory_view.type_device_info("envoy")
    assert info is not None
    assert info["name"] == "IQ Gateway"
    assert info["model"] == "IQ Gateway"
    assert info["serial_number"] == "GW-123"
    assert info["model_id"] == "SC100G-M230ROW"
    assert info["sw_version"] == "D8.3.5167"


def test_type_device_envoy_prefers_gateway_firmware_over_controller_version(
    hass, monkeypatch
) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 2,
                "devices": [
                    {
                        "name": "IQ Gateway",
                        "serial_number": "GW-123",
                        "sku_id": "SC-IQG-PCBA-ROW",
                        "envoy_sw_version": "D8.3.5232",
                    },
                    {
                        "name": "IQ System Controller 3 INT",
                        "serial_number": "SC-123",
                        "channel_type": "IQ System Controller",
                        "sw_version": "522-00003-01-v2.7.7054_rel/31.44",
                    },
                ],
            }
        },
        ["envoy"],
    )

    assert coord.inventory_view.type_device_model("envoy") == "IQ Gateway"
    assert coord.inventory_view.type_device_serial_number("envoy") == "GW-123"
    assert coord.inventory_view.type_device_model_id("envoy") == "SC-IQG-PCBA-ROW"
    assert coord.inventory_view.type_device_sw_version("envoy") == "D8.3.5232"
    info = coord.inventory_view.type_device_info("envoy")
    assert info is not None
    assert info["model"] == "IQ Gateway"
    assert info["serial_number"] == "GW-123"
    assert info["model_id"] == "SC-IQG-PCBA-ROW"
    assert info["sw_version"] == "D8.3.5232"


def test_type_device_envoy_does_not_promote_localized_meters_to_gateway(
    hass, monkeypatch
) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 2,
                "devices": [
                    {
                        "name": "IQ Envoy",
                        "serial_number": "GW-EIM1",
                        "channel_type": "Compteur de production integre Enphase",
                    },
                    {
                        "name": "IQ Envoy",
                        "serial_number": "GW-EIM2",
                        "channel_type": "Compteur de consommation integre Enphase",
                    },
                ],
            }
        },
        ["envoy"],
    )

    assert coord.inventory_view.type_device_name("envoy") == "IQ Gateway"
    assert coord.inventory_view.type_device_model("envoy") == "IQ Gateway"
    assert coord.inventory_view.type_device_serial_number("envoy") is None
    assert coord.inventory_view.type_device_model_id("envoy") is None
    assert coord.inventory_view.type_device_sw_version("envoy") is None

    info = coord.inventory_view.type_device_info("envoy")
    assert info is not None
    assert info["name"] == "IQ Gateway"
    assert info["model"] == "IQ Gateway"
    assert "serial_number" not in info
    assert "model_id" not in info
    assert "sw_version" not in info


def test_type_device_summary_helpers_for_battery_and_microinverter(
    hass, monkeypatch
) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "encharge": {
                "type_key": "encharge",
                "type_label": "Battery",
                "count": 3,
                "devices": [
                    {
                        "serial_number": "BAT-1",
                        "sku_id": "IQ-BAT-5P",
                        "sw_version": "1.0",
                    },
                    {
                        "serial_number": "BAT-2",
                        "sku_id": "IQ-BAT-5P",
                        "sw_version": "1.0",
                    },
                    {
                        "serial_number": "BAT-3",
                        "sku_id": "IQ-BAT-3T",
                        "sw_version": "2.0",
                    },
                ],
            },
            "microinverter": {
                "type_key": "microinverter",
                "type_label": "Microinverters",
                "count": 3,
                "devices": [
                    {"serial_number": "INV-1", "sku_id": "IQ8M", "fw1": "4.0"},
                    {"serial_number": "INV-2", "sku_id": "IQ8M", "fw1": "4.0"},
                    {"serial_number": "INV-3", "sku_id": "IQ8A", "fw2": "5.0"},
                ],
            },
        },
        ["encharge", "microinverter"],
    )

    assert coord.inventory_view.type_device_serial_number("encharge") is None
    assert coord.inventory_view.type_device_model_id("encharge") is None
    assert coord.inventory_view.type_device_sw_version("encharge") is None
    assert (
        coord.inventory_view.type_device_sw_version_summary("encharge")
        == "1.0 x2, 2.0 x1"
    )
    assert coord.inventory_view.type_device_model_id("microinverter") is None
    assert coord.inventory_view.type_device_hw_version("microinverter") is None
    assert coord.inventory_view.type_device_sw_version("microinverter") is None
    assert (
        coord.inventory_view.type_device_sw_version_summary("microinverter")
        == "4.0 x2, 5.0 x1"
    )


def test_type_device_helper_branches_for_mac_and_labels(hass, monkeypatch) -> None:
    from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC

    coord = _make_coordinator(hass, monkeypatch)

    assert (
        coord.inventory_view._type_member_summary(
            [{"name": "A"}, {"name": "A"}], "name"
        )
        == "A x2"
    )  # noqa: SLF001

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("bad")

    assert coord.inventory_view._normalize_mac(BadStr()) is None  # noqa: SLF001
    assert coord.inventory_view._normalize_mac("   ") is None  # noqa: SLF001
    assert coord.inventory_view._normalize_mac("aa:bb:cc") is None  # noqa: SLF001
    assert (
        coord.inventory_view._normalize_mac("001:bb:cc:dd:ee:ff") is None
    )  # noqa: SLF001
    assert (
        coord.inventory_view._normalize_mac("zz:bb:cc:dd:ee:ff") is None
    )  # noqa: SLF001
    assert (
        coord.inventory_view._normalize_mac("a:b:c:d:e:f") == "0a:0b:0c:0d:0e:0f"
    )  # noqa: SLF001
    assert (
        coord.inventory_view._normalize_mac("AABBCCDDEEFF") == "aa:bb:cc:dd:ee:ff"
    )  # noqa: SLF001
    assert (
        coord.inventory_view._normalize_mac("aabb.ccdd.eeff") == "aa:bb:cc:dd:ee:ff"
    )  # noqa: SLF001
    assert coord.inventory_view._normalize_mac("aabb.ccdd") is None  # noqa: SLF001
    assert coord.inventory_view._normalize_mac("aabb.ccdd.eefg") is None  # noqa: SLF001
    assert coord.inventory_view._normalize_mac("abcdef") is None  # noqa: SLF001

    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [
                    {
                        "name": "IQ Gateway",
                        "channel_type": "system_controller",
                        "serial_number": "GW-1",
                        "mac": "aa-bb-cc-dd-ee-ff",
                    }
                ],
            }
        },
        ["envoy"],
    )
    assert (
        coord.inventory_view._envoy_controller_mac() == "aa:bb:cc:dd:ee:ff"
    )  # noqa: SLF001
    assert coord.inventory_view.type_device_info("envoy")["connections"] == {
        (CONNECTION_NETWORK_MAC, "aa:bb:cc:dd:ee:ff")
    }
    assert (
        coord.inventory_view._envoy_member_kind({"channel_type": "production_meter"})
        == "production"
    )  # noqa: SLF001
    assert (
        coord.inventory_view._envoy_member_kind({"channel_type": "site_load"})
        == "consumption"
    )  # noqa: SLF001

    coord._type_device_buckets = {  # noqa: SLF001
        "envoy": {"count": 1, "devices": []},
        "wind_turbine": {"count": 1, "devices": [], "type_label": 1},
    }
    coord._type_device_order = ["envoy", "wind_turbine"]  # noqa: SLF001
    assert coord.inventory_view._envoy_controller_mac() is None  # noqa: SLF001
    assert coord.inventory_view.type_device_name("wind_turbine") is None
    assert coord.inventory_view.type_device_info(None) is None


def test_type_device_model_prefers_model_identifiers_over_display_name(
    hass, monkeypatch
) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "iqevse": {
                "type_key": "iqevse",
                "type_label": "EV Charger",
                "count": 2,
                "devices": [
                    {
                        "name": "Garage Charger",
                        "model_id": "IQ-EVSE-EU-3032-0105-1300",
                    },
                    {
                        "name": "Driveway Charger",
                        "model_id": "IQ-EVSE-EU-3032-0105-1300",
                    },
                ],
            }
        },
        ["iqevse"],
    )

    assert (
        coord.inventory_view.type_device_model("iqevse") == "IQ-EVSE-EU-3032-0105-1300"
    )


def test_type_device_model_id_omits_redundant_variants(hass, monkeypatch) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "iqevse": {
                "type_key": "iqevse",
                "type_label": "EV Charger",
                "count": 1,
                "devices": [
                    {
                        "model": "IQ EV Charger (IQ-EVSE-EU-3032)",
                        "model_id": "IQ-EVSE-EU-3032-0105-1300",
                    }
                ],
            },
            "encharge": {
                "type_key": "encharge",
                "type_label": "Battery",
                "count": 1,
                "devices": [{"sku_id": "B05-T02-ROW00-1-2"}],
            },
        },
        ["iqevse", "encharge"],
    )

    assert coord.inventory_view.type_device_model_id("iqevse") is None
    assert coord.inventory_view.type_device_model_id("encharge") is None


def test_sum_session_energy_rounds_to_two_decimals_without_session_manager(
    hass, monkeypatch
) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    if hasattr(coord, "session_history"):
        delattr(coord, "session_history")

    assert (
        coord._sum_session_energy([{"energy_kwh": 1.234}, {"energy_kwh": 2.345}])
        == 3.58
    )  # noqa: SLF001


def test_coerce_optional_kwh_falls_back_when_round_raises(hass, monkeypatch) -> None:
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = _make_coordinator(hass, monkeypatch)
    monkeypatch.setattr(
        coord_mod,
        "round",
        lambda _value, _precision: (_ for _ in ()).throw(ValueError("boom")),
        raising=False,
    )
    assert coord._coerce_optional_kwh("1.234") == 1.234  # noqa: SLF001


async def test_async_update_data_site_only_handles_heatpump_refresh_failure(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord.site_only = True
    coord.energy._async_refresh_site_energy = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_site_settings = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord.battery_runtime.async_refresh_battery_status = AsyncMock(return_value=None)  # type: ignore[method-assign]  # noqa: SLF001
    coord._async_refresh_battery_backup_history = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_settings = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_storm_guard_profile = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_storm_alert = AsyncMock(return_value=None)  # noqa: SLF001
    coord.battery_runtime.async_refresh_grid_control_check = AsyncMock(  # type: ignore[method-assign]
        return_value=None
    )  # noqa: SLF001
    coord.inventory_runtime._async_refresh_devices_inventory = AsyncMock(  # type: ignore[method-assign]
        return_value=None
    )  # noqa: SLF001
    coord.inventory_runtime._async_refresh_hems_devices = AsyncMock(return_value=None)  # type: ignore[method-assign]  # noqa: SLF001
    coord._async_refresh_inverters = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_heatpump_power = AsyncMock(
        side_effect=aiohttp.ClientError("boom")
    )  # noqa: SLF001

    assert await coord._async_update_data() == {}  # noqa: SLF001


@pytest.mark.asyncio
async def test_update_data_ignores_grid_control_and_hems_refresh_errors_site_only(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord.site_only = True
    coord.battery_runtime.async_refresh_grid_control_check = AsyncMock(  # type: ignore[method-assign]
        side_effect=aiohttp.ClientError("boom")
    )
    coord.inventory_runtime._async_refresh_hems_devices = AsyncMock(  # type: ignore[method-assign]
        side_effect=aiohttp.ClientError("boom")
    )  # noqa: SLF001

    assert await coord._async_update_data() == {}


@pytest.mark.asyncio
async def test_async_update_data_site_only_refreshes_hems_before_heatpump_power(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord.site_only = True
    coord._has_successful_refresh = True  # noqa: SLF001
    order: list[str] = []
    coord.energy._async_refresh_site_energy = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_site_settings = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord.battery_runtime.async_refresh_battery_status = AsyncMock(return_value=None)  # type: ignore[method-assign]  # noqa: SLF001
    coord._async_refresh_battery_backup_history = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_settings = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_storm_guard_profile = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_storm_alert = AsyncMock(return_value=None)  # noqa: SLF001
    coord.battery_runtime.async_refresh_grid_control_check = AsyncMock(  # type: ignore[method-assign]
        return_value=None
    )  # noqa: SLF001
    coord.inventory_runtime._async_refresh_devices_inventory = AsyncMock(  # type: ignore[method-assign]  # noqa: SLF001
        side_effect=lambda: order.append("devices")
    )
    coord.inventory_runtime._async_refresh_hems_devices = AsyncMock(  # type: ignore[method-assign]  # noqa: SLF001
        side_effect=lambda: order.append("hems")
    )
    coord._async_refresh_inverters = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_heatpump_runtime_state = AsyncMock(  # noqa: SLF001
        side_effect=lambda: order.append("heatpump_runtime")
    )
    coord._async_refresh_heatpump_daily_consumption = AsyncMock(  # noqa: SLF001
        side_effect=lambda: order.append("heatpump_daily")
    )
    coord._async_refresh_heatpump_power = AsyncMock(  # noqa: SLF001
        side_effect=lambda: order.append("heatpump_power")
    )
    coord.heatpump_runtime.heatpump_runtime_state_refresh_due = lambda: True  # type: ignore[method-assign]
    coord.heatpump_runtime.heatpump_daily_consumption_refresh_due = lambda: True  # type: ignore[method-assign]
    coord.heatpump_runtime.heatpump_power_refresh_due = lambda: True  # type: ignore[method-assign]
    coord.heatpump_runtime.has_type = lambda _key: True  # type: ignore[method-assign]
    coord.client._hems_site_supported = True  # noqa: SLF001

    assert await coord._async_update_data() == {}  # noqa: SLF001
    assert order == ["devices", "hems", "heatpump_power"]


@pytest.mark.asyncio
async def test_async_update_data_site_only_ignores_runtime_and_daily_refresh_errors(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord.site_only = True
    coord._has_successful_refresh = True  # noqa: SLF001
    order: list[str] = []
    coord.energy._async_refresh_site_energy = AsyncMock(  # noqa: SLF001
        return_value=None
    )
    coord._async_refresh_battery_site_settings = AsyncMock(  # noqa: SLF001
        return_value=None
    )
    coord.battery_runtime.async_refresh_battery_status = AsyncMock(return_value=None)  # type: ignore[method-assign]  # noqa: SLF001
    coord._async_refresh_battery_backup_history = AsyncMock(  # noqa: SLF001
        return_value=None
    )
    coord._async_refresh_battery_settings = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_storm_guard_profile = AsyncMock(  # noqa: SLF001
        return_value=None
    )
    coord._async_refresh_storm_alert = AsyncMock(return_value=None)  # noqa: SLF001
    coord.battery_runtime.async_refresh_grid_control_check = AsyncMock(  # type: ignore[method-assign]
        return_value=None
    )
    coord.inventory_runtime._async_refresh_devices_inventory = AsyncMock(  # type: ignore[method-assign]  # noqa: SLF001
        side_effect=lambda: order.append("devices")
    )
    coord.inventory_runtime._async_refresh_hems_devices = AsyncMock(  # type: ignore[method-assign]  # noqa: SLF001
        side_effect=lambda: order.append("hems")
    )
    coord._async_refresh_inverters = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_heatpump_runtime_state = AsyncMock(  # noqa: SLF001
        side_effect=aiohttp.ClientError("runtime")
    )
    coord._async_refresh_heatpump_daily_consumption = AsyncMock(  # noqa: SLF001
        side_effect=aiohttp.ClientError("daily")
    )
    coord._async_refresh_heatpump_power = AsyncMock(  # noqa: SLF001
        side_effect=lambda: order.append("heatpump_power")
    )
    coord.heatpump_runtime.heatpump_runtime_state_refresh_due = lambda: True  # type: ignore[method-assign]
    coord.heatpump_runtime.heatpump_daily_consumption_refresh_due = lambda: True  # type: ignore[method-assign]
    coord.heatpump_runtime.heatpump_power_refresh_due = lambda: True  # type: ignore[method-assign]

    assert await coord._async_update_data() == {}  # noqa: SLF001
    assert order == ["devices", "hems", "heatpump_power"]


@pytest.mark.asyncio
async def test_async_update_data_continues_when_heatpump_refresh_raises(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.summary = SimpleNamespace(
        prepare_refresh=lambda **_: False,
        async_fetch=AsyncMock(return_value=[]),
        invalidate=MagicMock(),
    )
    coord.session_history = SimpleNamespace(
        get_cache_view=lambda *_, **__: SimpleNamespace(
            sessions=[], needs_refresh=False, blocked=False
        ),
        sum_energy=lambda *_: 0.0,
    )
    coord.client.status = AsyncMock(
        return_value={
            "ts": "2026-02-28T00:00:00Z",
            "evChargerData": [
                {
                    "sn": RANDOM_SERIAL,
                    "name": "EV",
                    "connectors": [{}],
                    "pluggedIn": False,
                    "charging": False,
                    "faulted": False,
                    "session_d": {},
                }
            ],
        }
    )
    coord.energy._async_refresh_site_energy = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_inverters = AsyncMock(return_value=None)  # noqa: SLF001
    coord.inventory_runtime._async_refresh_hems_devices = AsyncMock(return_value=None)  # type: ignore[method-assign]  # noqa: SLF001
    coord._async_refresh_heatpump_power = AsyncMock(
        side_effect=RuntimeError("boom")
    )  # noqa: SLF001

    result = await coord._async_update_data()  # noqa: SLF001
    assert RANDOM_SERIAL in result


@pytest.mark.asyncio
async def test_async_update_data_prefers_heatpump_power_when_all_heatpump_refreshes_are_due(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._has_successful_refresh = True  # noqa: SLF001
    coord.summary = SimpleNamespace(
        prepare_refresh=lambda **_: False,
        async_fetch=AsyncMock(return_value=[]),
        invalidate=MagicMock(),
    )
    coord.session_history = SimpleNamespace(
        get_cache_view=lambda *_, **__: SimpleNamespace(
            sessions=[], needs_refresh=False, blocked=False
        ),
        sum_energy=lambda *_: 0.0,
    )
    coord.client.status = AsyncMock(
        return_value={
            "ts": "2026-02-28T00:00:00Z",
            "evChargerData": [
                {
                    "sn": RANDOM_SERIAL,
                    "name": "EV",
                    "connectors": [{}],
                    "pluggedIn": False,
                    "charging": False,
                    "faulted": False,
                    "session_d": {},
                }
            ],
        }
    )
    coord.energy._async_refresh_site_energy = AsyncMock(  # noqa: SLF001
        return_value=None
    )
    coord._async_refresh_inverters = AsyncMock(return_value=None)  # noqa: SLF001
    coord.inventory_runtime._async_refresh_hems_devices = AsyncMock(return_value=None)  # type: ignore[method-assign]  # noqa: SLF001
    coord._async_refresh_heatpump_runtime_state = AsyncMock(  # noqa: SLF001
        side_effect=RuntimeError("runtime")
    )
    coord._async_refresh_heatpump_daily_consumption = AsyncMock(  # noqa: SLF001
        side_effect=RuntimeError("daily")
    )
    coord._async_refresh_heatpump_power = AsyncMock(return_value=None)  # noqa: SLF001
    coord.heatpump_runtime.heatpump_runtime_state_refresh_due = lambda: True  # type: ignore[method-assign]
    coord.heatpump_runtime.heatpump_daily_consumption_refresh_due = lambda: True  # type: ignore[method-assign]
    coord.heatpump_runtime.heatpump_power_refresh_due = lambda: True  # type: ignore[method-assign]
    coord.heatpump_runtime.has_type = lambda _key: True  # type: ignore[method-assign]
    coord.client._hems_site_supported = True  # noqa: SLF001

    result = await coord._async_update_data()  # noqa: SLF001
    assert RANDOM_SERIAL in result
    coord._async_refresh_heatpump_runtime_state.assert_not_awaited()  # noqa: SLF001
    coord._async_refresh_heatpump_daily_consumption.assert_not_awaited()  # noqa: SLF001
    coord._async_refresh_heatpump_power.assert_awaited_once_with()  # noqa: SLF001


@pytest.mark.asyncio
async def test_update_data_clears_success_issues_and_ignores_grid_control_and_hems_errors(
    coordinator_factory, mock_issue_registry
) -> None:
    coord = coordinator_factory()
    coord.client.status = AsyncMock(
        return_value={"evChargerData": [], "ts": 1_700_000_000_000}
    )
    coord._unauth_errors = 1  # noqa: SLF001
    coord._dns_issue_reported = True  # noqa: SLF001
    coord.battery_runtime.async_refresh_grid_control_check = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("boom")
    )
    coord.inventory_runtime._async_refresh_hems_devices = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("boom")
    )  # noqa: SLF001

    await coord._async_update_data()

    assert (DOMAIN, "reauth_required") in mock_issue_registry.deleted
    assert (DOMAIN, ISSUE_DNS_RESOLUTION) in mock_issue_registry.deleted
    assert coord._dns_issue_reported is False  # noqa: SLF001
    assert coord._unauth_errors == 0  # noqa: SLF001


@pytest.mark.asyncio
async def test_update_data_success_clears_reauth_and_converts_millisecond_timestamp(
    coordinator_factory, mock_issue_registry
) -> None:
    coord = coordinator_factory()
    coord.summary = SimpleNamespace(
        prepare_refresh=lambda **_: False,
        async_fetch=AsyncMock(return_value=[]),
        invalidate=MagicMock(),
    )
    coord.session_history = SimpleNamespace(
        get_cache_view=lambda *_, **__: SimpleNamespace(
            sessions=[], needs_refresh=False, blocked=False
        ),
        sum_energy=lambda *_: 0.0,
    )
    coord.client.status = AsyncMock(
        return_value={
            "ts": 1_700_000_000_000,
            "evChargerData": [
                {
                    "sn": RANDOM_SERIAL,
                    "name": "EV",
                    "connectors": [{}],
                    "pluggedIn": False,
                    "charging": False,
                    "faulted": False,
                    "session_d": {},
                }
            ],
        }
    )
    coord._unauth_errors = 1  # noqa: SLF001
    coord._last_charging = {RANDOM_SERIAL: True}  # noqa: SLF001
    coord.energy._async_refresh_site_energy = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_inverters = AsyncMock(return_value=None)  # noqa: SLF001
    coord.inventory_runtime._async_refresh_hems_devices = AsyncMock(return_value=None)  # type: ignore[method-assign]  # noqa: SLF001

    result = await coord._async_update_data()  # noqa: SLF001

    assert RANDOM_SERIAL in result
    assert (DOMAIN, "reauth_required") in mock_issue_registry.deleted
    assert coord._session_end_fix[RANDOM_SERIAL] == 1_700_000_000  # noqa: SLF001


def test_heatpump_power_properties_handle_invalid_internal_values(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])

    class BadFloat:
        def __float__(self) -> float:
            raise ValueError("boom")

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    coord._heatpump_power_w = BadFloat()  # noqa: SLF001
    assert coord.heatpump_power_w is None
    coord._heatpump_power_w = float("nan")  # noqa: SLF001
    assert coord.heatpump_power_w is None
    coord._heatpump_power_w = float("inf")  # noqa: SLF001
    assert coord.heatpump_power_w is None

    coord._heatpump_power_raw_w = BadFloat()  # noqa: SLF001
    assert coord.heatpump_power_raw_w is None
    coord._heatpump_power_raw_w = float("nan")  # noqa: SLF001
    assert coord.heatpump_power_raw_w is None
    coord._heatpump_power_raw_w = float("inf")  # noqa: SLF001
    assert coord.heatpump_power_raw_w is None

    coord._heatpump_power_window_seconds = BadFloat()  # noqa: SLF001
    assert coord.heatpump_power_window_seconds is None
    coord._heatpump_power_window_seconds = float("nan")  # noqa: SLF001
    assert coord.heatpump_power_window_seconds is None
    coord._heatpump_power_window_seconds = float("inf")  # noqa: SLF001
    assert coord.heatpump_power_window_seconds is None

    coord._heatpump_power_device_uid = BadStr()  # noqa: SLF001
    coord._heatpump_power_source = BadStr()  # noqa: SLF001
    coord._heatpump_power_validation = BadStr()  # noqa: SLF001
    coord._heatpump_power_last_error = BadStr()  # noqa: SLF001
    assert coord.heatpump_power_device_uid is None
    assert coord.heatpump_power_source is None
    assert coord.heatpump_power_validation is None
    assert coord.heatpump_power_last_error is None


@pytest.mark.asyncio
async def test_refresh_runner_helpers_cover_stage_and_topology_paths(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    phase_timings: dict[str, float] = {}
    begin = Mock()
    end = Mock(return_value=False)
    coord._begin_topology_refresh_batch = begin  # type: ignore[assignment]  # noqa: SLF001
    coord._end_topology_refresh_batch = end  # type: ignore[assignment]  # noqa: SLF001

    await coord.refresh_runner.async_run_ordered_refresh_calls(
        phase_timings,
        stage_key="ordered",
        defer_topology=True,
        calls=(("first_s", "first", AsyncMock(return_value=None), None),),
    )

    assert "first_s" in phase_timings
    assert "ordered_s" in phase_timings
    begin.assert_called_once()
    end.assert_called_once()

    phase_timings.clear()
    begin.reset_mock()
    end.reset_mock()

    await coord.refresh_runner.async_run_staged_refresh_calls(
        phase_timings,
        stage_key="empty",
    )

    assert phase_timings == {"empty_s": 0.0}
    begin.assert_not_called()
    end.assert_not_called()


def test_summary_type_bucket_source_guards_invalid_inputs(coordinator_factory) -> None:
    coord = coordinator_factory(serials=[])
    coord._type_device_buckets = "bad"  # type: ignore[assignment]  # noqa: SLF001
    assert (
        coord.inventory_runtime._summary_type_bucket_source("envoy") is None
    )  # noqa: SLF001
    assert (
        coord.inventory_runtime._summary_type_bucket_source(None) is None
    )  # noqa: SLF001


def test_publish_internal_state_update_copies_current_data(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.async_set_updated_data = Mock()  # type: ignore[assignment]

    coord._publish_internal_state_update()  # noqa: SLF001

    coord.async_set_updated_data.assert_called_once_with(dict(coord.data))


def test_site_only_coordinator_notifies_listeners_for_equal_payloads(
    hass,
    monkeypatch,
) -> None:
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        coord_mod,
        "async_call_later",
        lambda *_args, **_kwargs: (lambda: None),
    )
    coord = EnphaseCoordinator(
        hass,
        {
            CONF_SITE_ID: RANDOM_SITE_ID,
            CONF_SERIALS: [],
            CONF_EAUTH: "EAUTH",
            CONF_COOKIE: "COOKIE",
            CONF_SCAN_INTERVAL: 15,
            CONF_SITE_ONLY: True,
            CONF_INCLUDE_INVERTERS: True,
        },
    )
    calls: list[dict | None] = []
    unsub = coord.async_add_listener(lambda: calls.append(coord.data))

    coord.async_set_updated_data({})
    coord.async_set_updated_data({})
    unsub()

    assert calls == [{}, {}]


def test_end_topology_refresh_batch_flushes_pending_refresh(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord._topology_refresh_suppressed = 1  # noqa: SLF001
    coord._topology_refresh_pending = True  # noqa: SLF001
    coord._refresh_cached_topology = Mock(return_value=True)  # type: ignore[assignment]  # noqa: SLF001

    assert coord._end_topology_refresh_batch() is True  # noqa: SLF001
    coord._refresh_cached_topology.assert_called_once()
    assert coord._topology_refresh_pending is False  # noqa: SLF001


def test_topology_listener_and_summary_helpers_cover_edge_paths(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    calls: list[str] = []

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    remove = coord.async_add_topology_listener(lambda: calls.append("ok"))
    coord.async_add_topology_listener(
        lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    coord._notify_topology_listeners()  # noqa: SLF001
    assert calls == ["ok"]

    remove()
    assert coord.topology_snapshot() == coord._topology_snapshot_cache  # noqa: SLF001
    assert len(coord._topology_listeners) == 1  # noqa: SLF001

    coord.inventory_runtime._build_heatpump_type_summaries = Mock(return_value={})  # type: ignore[assignment]  # noqa: SLF001
    assert coord.heatpump_type_summary(BadStr()) == {}

    original_router_marker = (
        coord.inventory_runtime._gateway_iq_energy_router_records_marker
    )
    coord.inventory_runtime._gateway_iq_energy_router_records_marker = lambda: ("stable",)  # type: ignore[assignment]  # noqa: SLF001
    coord._gateway_iq_energy_router_records_cache = [
        {"key": "router_1", "name": "Router"}
    ]  # noqa: SLF001
    coord._gateway_iq_energy_router_records_source = ("stable",)  # noqa: SLF001
    coord._gateway_iq_energy_router_records_by_key_cache = {  # noqa: SLF001
        "router_1": {"key": "router_1", "name": "Router"}
    }

    assert coord.inventory_runtime._router_record_key("bad") is None  # noqa: SLF001
    assert (
        coord.inventory_runtime._router_record_key({"key": None}) is None
    )  # noqa: SLF001
    assert (
        coord.inventory_runtime._router_record_key({"key": BadStr()}) is None
    )  # noqa: SLF001
    assert coord.inventory_view.gateway_iq_energy_router_record(BadStr()) is None
    assert coord.inventory_view.gateway_iq_energy_router_record(" ") is None
    assert coord.inventory_view.gateway_iq_energy_router_record("router_1") == {
        "key": "router_1",
        "name": "Router",
    }
    coord.inventory_runtime._gateway_iq_energy_router_records_marker = original_router_marker  # type: ignore[assignment]  # noqa: SLF001

    assert coord.inventory_runtime._summary_text(BadStr()) is None  # noqa: SLF001
    assert (
        coord.inventory_runtime._summary_identity("Router Main") == "router_main"
    )  # noqa: SLF001
    assert (
        coord.inventory_runtime._gateway_iq_energy_router_records_marker()
        == (  # noqa: SLF001
            id(getattr(coord, "_hems_devices_payload", None)),
            id(getattr(coord, "_devices_inventory_payload", None)),
            id(getattr(coord, "_restored_gateway_iq_energy_router_records", None)),
        )
    )
    assert coord.inventory_runtime._heatpump_status_text(None) is None  # noqa: SLF001

    records = coord.inventory_runtime._gateway_iq_energy_router_summary_records(  # noqa: SLF001
        [
            {"name": "Router Main"},
            {"name": "Router Main"},
            {},
        ]
    )
    assert [record["key"] for record in records] == [
        "name_router_main",
        "name_router_main_2",
        "index_3",
    ]

    records = coord.inventory_runtime._gateway_iq_energy_router_summary_records(  # noqa: SLF001
        [
            {"device_uid": "router-uid", "uid": "fallback-uid", "name": "Router UID"},
        ]
    )
    assert records[0]["key"] == "router_uid"


def test_debug_log_summary_if_changed_deduplicates_output(
    coordinator_factory, caplog
) -> None:
    coord = coordinator_factory(serials=[])
    summary = {
        "ordered_type_keys": ["iqevse"],
        "type_count": 1,
        "types": {"iqevse": {"count": 1, "field_keys": ["name", "serial_number"]}},
    }

    with caplog.at_level(logging.DEBUG):
        coord._debug_log_summary_if_changed(  # noqa: SLF001
            "devices_inventory",
            "Device inventory discovery summary",
            summary,
        )
        coord._debug_log_summary_if_changed(  # noqa: SLF001
            "devices_inventory",
            "Device inventory discovery summary",
            summary,
        )

    matches = [
        record
        for record in caplog.records
        if "Device inventory discovery summary" in record.message
    ]
    assert len(matches) == 1
    assert '"type_count": 1' in matches[0].message


@pytest.mark.asyncio
async def test_refresh_devices_inventory_logs_sanitized_discovery_summary(
    coordinator_factory, caplog
) -> None:
    payload = {
        "result": [
            {
                "type": "iqevse",
                "devices": [
                    {
                        "serial_number": "SERIAL-123456",
                        "name": "Driveway Charger",
                        "status": "online",
                        "custom_field": True,
                    }
                ],
            },
            {
                "type": "envoy",
                "devices": [
                    {
                        "serial_number": "ENV-123456",
                        "name": "Back Shed Gateway",
                        "statusText": "online",
                        "connected": True,
                    }
                ],
            },
        ]
    }
    client = SimpleNamespace(devices_inventory=AsyncMock(return_value=payload))
    coord = coordinator_factory(client=client, serials=[])

    with caplog.at_level(logging.DEBUG):
        await coord.inventory_runtime._async_refresh_devices_inventory(
            force=True
        )  # noqa: SLF001
        await coord.inventory_runtime._async_refresh_devices_inventory(
            force=True
        )  # noqa: SLF001

    matches = [
        record
        for record in caplog.records
        if "Device inventory discovery summary" in record.message
    ]
    assert len(matches) == 1
    message = matches[0].message
    assert '"iqevse"' in message
    assert '"custom_field"' in message
    assert "SERIAL-123456" not in message
    assert "Driveway Charger" not in message
    assert "ENV-123456" not in message
    assert "Back Shed Gateway" not in message


@pytest.mark.asyncio
async def test_refresh_hems_devices_logs_sanitized_discovery_summary(
    coordinator_factory, caplog
) -> None:
    payload = {
        "data": {
            "hems-devices": {
                "gateway": [
                    {
                        "device-uid": "GATEWAY-UID-123456789",
                        "name": "Main Gateway",
                        "ip-address": "10.0.0.2",
                    }
                ],
                "heat-pump": [
                    {
                        "device-uid": "DEVICE-UID-123456789",
                        "name": "Living Room Heat Pump",
                        "device-type": "HEAT_PUMP",
                        "status": "Running",
                    }
                ],
            }
        }
    }
    client = SimpleNamespace(
        hems_site_supported=True,
        hems_devices=AsyncMock(return_value=payload),
    )
    coord = coordinator_factory(client=client, serials=[])
    coord._async_refresh_hems_support_preflight = AsyncMock(return_value=None)  # type: ignore[assignment]  # noqa: SLF001

    with caplog.at_level(logging.DEBUG):
        await coord.inventory_runtime._async_refresh_hems_devices(
            force=True
        )  # noqa: SLF001

    matches = [
        record
        for record in caplog.records
        if "HEMS discovery summary" in record.message
    ]
    assert len(matches) == 1
    message = matches[0].message
    assert '"group_keys": ["gateway", "heat-pump"]' in message
    assert '"heatpump_device_type_counts": {"HEAT_PUMP": 1}' in message
    assert "DEVICE-UID-123456789" not in message
    assert "Living Room Heat Pump" not in message
    assert "10.0.0.2" not in message


@pytest.mark.asyncio
async def test_refresh_evse_feature_flags_logs_sanitized_summary(
    coordinator_factory, caplog
) -> None:
    payload = {
        "meta": {"schemaVersion": "1"},
        "data": {
            "allowRemoteStart": True,
            "SERIAL-123456": {
                "plugAndCharge": True,
                "rfid": False,
            },
        },
    }
    client = SimpleNamespace(evse_feature_flags=AsyncMock(return_value=payload))
    coord = coordinator_factory(client=client, serials=[])

    with caplog.at_level(logging.DEBUG):
        await coord._async_refresh_evse_feature_flags(force=True)  # noqa: SLF001

    matches = [
        record
        for record in caplog.records
        if "EVSE feature flag summary" in record.message
    ]
    assert len(matches) == 1
    message = matches[0].message
    assert '"site_flag_keys": ["allowRemoteStart"]' in message
    assert '"charger_flag_keys": ["plugAndCharge", "rfid"]' in message
    assert '"charger_count": 1' in message
    assert "SERIAL-123456" not in message


@pytest.mark.asyncio
async def test_update_data_ignores_devices_inventory_refresh_errors(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.site_only = True
    coord.inventory_runtime._async_refresh_devices_inventory = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError()
    )  # noqa: SLF001
    result = await coord._async_update_data()
    assert result == {}

    coord = coordinator_factory()
    coord.client.status = AsyncMock(return_value={"evChargerData": [], "ts": 0})
    coord.inventory_runtime._async_refresh_devices_inventory = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError()
    )  # noqa: SLF001
    await coord._async_update_data()


@pytest.mark.asyncio
async def test_update_data_ignores_inverter_refresh_errors_site_only(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.site_only = True
    coord._async_refresh_inverters = AsyncMock(
        side_effect=RuntimeError()
    )  # noqa: SLF001

    result = await coord._async_update_data()

    assert result == {}


@pytest.mark.asyncio
async def test_update_data_ignores_inverter_refresh_errors_non_site_only(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.status = AsyncMock(return_value={"evChargerData": [], "ts": 0})
    coord._async_refresh_inverters = AsyncMock(
        side_effect=RuntimeError()
    )  # noqa: SLF001

    await coord._async_update_data()


def test_battery_property_false_paths(hass, monkeypatch) -> None:
    coord = _make_coordinator(hass, monkeypatch)

    class _FalseControls(type(coord)):
        @property
        def battery_controls_available(self):  # type: ignore[override]
            return False

    coord.__class__ = _FalseControls
    assert coord.savings_use_battery_switch_available is False
    assert coord.battery_reserve_editable is False

    coord._battery_profile = "backup_only"  # noqa: SLF001
    assert coord.battery_reserve_min == 100

    coord._battery_charge_begin_time = None  # noqa: SLF001
    coord._battery_charge_end_time = None  # noqa: SLF001
    assert coord.charge_from_grid_schedule_available is False

    coord._battery_envoy_supports_vls = True  # noqa: SLF001
    coord._battery_very_low_soc = None  # noqa: SLF001
    assert coord.battery_shutdown_level_available is False
    coord._battery_limit_support = False  # noqa: SLF001
    assert coord.battery_shutdown_level_available is False


@pytest.mark.asyncio
async def test_update_skips_status_when_site_only(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord.site_only = True

    client = SimpleNamespace(
        status=AsyncMock(side_effect=AssertionError("should not call status"))
    )
    coord.client = client

    result = await coord._async_update_data()

    assert result == {}
    assert client.status.await_count == 0
    assert coord.last_success_utc is not None
    assert coord._has_successful_refresh is True


@pytest.mark.asyncio
async def test_update_skips_status_when_no_evse_context(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord.serials = set()
    coord._serial_order = []
    coord._configured_serials = set()  # noqa: SLF001

    client = SimpleNamespace(
        status=AsyncMock(side_effect=AssertionError("should not call status"))
    )
    coord.client = client

    result = await coord._async_update_data()

    assert result == {}
    assert client.status.await_count == 0
    assert coord.last_success_utc is not None
    assert coord._has_successful_refresh is True


@pytest.mark.asyncio
async def test_update_keeps_status_when_configured_evse_serials_are_pruned(
    hass, monkeypatch
):
    coord = _make_coordinator(hass, monkeypatch)
    coord.serials = set()
    coord._serial_order = []  # noqa: SLF001
    coord.client.status = AsyncMock(return_value={"evChargerData": [], "ts": 0})

    result = await coord._async_update_data()  # noqa: SLF001

    assert result == {}
    coord.client.status.assert_awaited_once()
    assert coord._status_charger_data_authoritative is True  # noqa: SLF001
    assert coord.iter_serials() == []


def test_evse_status_refresh_enabled_without_active_serials(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord.serials = set()
    coord._serial_order = []  # noqa: SLF001
    coord._configured_serials = set()  # noqa: SLF001
    coord._selected_type_keys = {"iqevse"}  # noqa: SLF001

    assert coord._evse_status_refresh_enabled() is True  # noqa: SLF001

    coord._selected_type_keys = None  # noqa: SLF001
    coord._restored_evse_serial_order = ["RESTORED"]  # noqa: SLF001

    assert coord._evse_status_refresh_enabled() is True  # noqa: SLF001


@pytest.mark.asyncio
async def test_site_only_clears_issues_and_counters(
    hass, monkeypatch, mock_issue_registry
):
    coord = _make_coordinator(hass, monkeypatch)
    coord.site_only = True
    coord._network_issue_reported = True
    coord._cloud_issue_reported = True
    coord._dns_issue_reported = True
    coord._rate_limit_issue_reported = True
    coord._unauth_errors = 3
    coord._rate_limit_hits = 2
    coord._http_errors = 4
    coord._network_errors = 5
    coord._dns_failures = 6
    coord._last_error = "any error"
    coord.backoff_ends_utc = object()
    coord._backoff_until = 123.0
    cancelled = {"called": False}
    coord._backoff_cancel = lambda: cancelled.__setitem__("called", True)

    await coord._async_update_data()

    assert cancelled["called"] is True
    assert coord._network_issue_reported is False
    assert coord._cloud_issue_reported is False
    assert coord._dns_issue_reported is False
    assert coord._unauth_errors == 0
    assert coord._rate_limit_hits == 0
    assert coord._http_errors == 0
    assert coord._network_errors == 0
    assert coord._dns_failures == 0
    assert coord._last_error is None
    assert coord.backoff_ends_utc is None
    assert coord._backoff_until is None
    assert ("enphase_ev", "cloud_unreachable") in mock_issue_registry.deleted
    assert ("enphase_ev", "cloud_service_unavailable") in mock_issue_registry.deleted
    assert ("enphase_ev", "cloud_dns_resolution") in mock_issue_registry.deleted
    assert ("enphase_ev", "rate_limited") in mock_issue_registry.deleted


@pytest.mark.asyncio
async def test_site_only_first_refresh_cancels_startup_followups_when_energy_fails(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.site_only = True
    coord.serials = set()
    coord._serial_order = []  # noqa: SLF001
    coord._has_successful_refresh = False  # noqa: SLF001
    followup_cancelled = asyncio.Event()

    async def _fail_site_energy() -> None:
        await asyncio.sleep(0)
        raise RuntimeError("site energy failed")

    async def _run_first_refresh_followups(_phase_timings) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            followup_cancelled.set()
            raise

    coord._async_run_first_refresh_followups = _run_first_refresh_followups  # type: ignore[method-assign]  # noqa: SLF001
    coord.energy._async_refresh_site_energy = AsyncMock(  # noqa: SLF001
        side_effect=_fail_site_energy
    )

    with pytest.raises(RuntimeError, match="site energy failed"):
        await coord._async_update_data()  # noqa: SLF001

    assert followup_cancelled.is_set()


@pytest.mark.asyncio
async def test_start_first_refresh_followups_keeps_existing_task(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import RefreshPipelineContext

    coord = coordinator_factory()
    existing_task = asyncio.create_task(asyncio.sleep(0))
    context = RefreshPipelineContext(
        started_mono=time.monotonic(),
        refresh_started_utc=datetime.now(timezone.utc),
        phase_timings={},
        fallback_data={},
        first_refresh=True,
        first_refresh_followups_task=existing_task,
    )
    coord._async_run_first_refresh_followups = AsyncMock()  # type: ignore[method-assign]  # noqa: SLF001

    coord._start_first_refresh_followups(context)  # noqa: SLF001

    assert context.first_refresh_followups_task is existing_task
    coord._async_run_first_refresh_followups.assert_not_called()
    await existing_task


@pytest.mark.asyncio
async def test_backoff_on_429(hass, monkeypatch):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    coord = _make_coordinator(hass, monkeypatch)

    class StubClient:
        async def status(self):
            raise _client_response_error(429, headers={"Retry-After": "1"})

    coord.client = StubClient()

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord._backoff_until is not None


@pytest.mark.asyncio
async def test_backoff_timer_requests_refresh(hass, monkeypatch):
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = _make_coordinator(hass, monkeypatch)
    coord.async_request_refresh = AsyncMock()

    captured: dict[str, object] = {}

    def _fake_call_later(hass_obj, delay, cb):
        captured["delay"] = delay
        captured["callback"] = cb

        def _cancel():
            captured["cancelled"] = True

        return _cancel

    monkeypatch.setattr(coord_mod, "async_call_later", _fake_call_later)

    now = datetime(2025, 11, 3, 20, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(coord_mod.dt_util, "utcnow", lambda: now)

    coord._schedule_backoff_timer(2.5)

    assert captured["delay"] == 2.5
    assert coord.backoff_ends_utc == now + timedelta(seconds=2.5)
    assert callable(coord._backoff_cancel)

    captured["callback"](now + timedelta(seconds=3))
    task = next(iter(coord._backoff_refresh_tasks))  # noqa: SLF001
    await task
    await asyncio.sleep(0)

    assert coord.async_request_refresh.await_count == 1
    assert coord.backoff_ends_utc is None
    assert coord._backoff_cancel is None
    assert coord._backoff_refresh_tasks == set()  # noqa: SLF001


@pytest.mark.asyncio
async def test_cleanup_cancels_fired_backoff_refresh(hass, monkeypatch) -> None:
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = _make_coordinator(hass, monkeypatch)
    started = asyncio.Event()
    cancelled = asyncio.Event()
    captured: dict[str, object] = {}

    async def _refresh() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    def _fake_call_later(_hass, _delay, action):
        captured["callback"] = action
        return lambda: None

    coord.async_request_refresh = _refresh  # type: ignore[method-assign]
    monkeypatch.setattr(coord_mod, "async_call_later", _fake_call_later)

    coord._schedule_backoff_timer(1)  # noqa: SLF001
    captured["callback"](dt_util.utcnow())
    task = next(iter(coord._backoff_refresh_tasks))  # noqa: SLF001
    await started.wait()

    coord.cleanup_runtime_state()
    await asyncio.gather(task, return_exceptions=True)

    assert cancelled.is_set()
    assert coord._backoff_refresh_tasks == set()  # noqa: SLF001


@pytest.mark.asyncio
async def test_http_error_issue(hass, monkeypatch):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    from custom_components.enphase_ev.const import ISSUE_CLOUD_ERRORS
    from custom_components.enphase_ev import coordinator_diagnostics as diag_mod

    coord = _make_coordinator(hass, monkeypatch)

    class FailingClient:
        async def status(self):
            raise _client_response_error(503)

    created = []
    deleted = []
    monkeypatch.setattr(
        diag_mod.ir,
        "async_create_issue",
        lambda hass, domain, issue_id, **kwargs: created.append(
            (domain, issue_id, kwargs)
        ),
        raising=False,
    )
    monkeypatch.setattr(
        diag_mod.ir,
        "async_delete_issue",
        lambda hass, domain, issue_id: deleted.append((domain, issue_id)),
        raising=False,
    )

    coord.client = FailingClient()

    for _ in range(3):
        with pytest.raises(UpdateFailed):
            await coord._async_update_data()
        coord._backoff_until = None

    matching = [
        kwargs for _, issue_id, kwargs in created if issue_id == ISSUE_CLOUD_ERRORS
    ]
    assert matching
    latest_payload = matching[-1]
    placeholders = latest_payload["translation_placeholders"]
    assert placeholders["site_id"] == coord.site_id
    metrics = latest_payload["data"]["site_metrics"]
    assert metrics["last_error"]

    class SuccessClient:
        async def status(self):
            return {"evChargerData": []}

    coord.client = SuccessClient()
    coord._backoff_until = None
    data = await coord._async_update_data()
    coord.async_set_updated_data(data)

    assert any(issue_id == ISSUE_CLOUD_ERRORS for _, issue_id in deleted)


@pytest.mark.asyncio
async def test_network_issue_includes_metrics(hass, monkeypatch):
    from homeassistant.helpers.update_coordinator import UpdateFailed
    from custom_components.enphase_ev import coordinator_diagnostics as diag_mod
    from custom_components.enphase_ev.const import ISSUE_NETWORK_UNREACHABLE

    coord = _make_coordinator(hass, monkeypatch)
    coord.site_name = "Garage"

    class StubClient:
        async def status(self):
            raise aiohttp.ClientError("connection reset by peer")

    coord.client = StubClient()

    created: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        diag_mod.ir,
        "async_create_issue",
        lambda hass_, domain, issue_id, **kwargs: created.append((issue_id, kwargs)),
        raising=False,
    )

    for _ in range(3):
        with pytest.raises(UpdateFailed):
            await coord._async_update_data()
        coord._backoff_until = None

    issue_map = {issue_id: kwargs for issue_id, kwargs in created}
    assert ISSUE_NETWORK_UNREACHABLE in issue_map
    payload = issue_map[ISSUE_NETWORK_UNREACHABLE]
    placeholders = payload["translation_placeholders"]
    assert placeholders["site_name"] == "Garage"
    metrics = payload["data"]["site_metrics"]
    assert metrics["network_errors"] >= 3


@pytest.mark.asyncio
async def test_dns_issue_includes_metrics(hass, monkeypatch):
    from homeassistant.helpers.update_coordinator import UpdateFailed
    from custom_components.enphase_ev import coordinator_diagnostics as diag_mod
    from custom_components.enphase_ev.const import ISSUE_DNS_RESOLUTION

    coord = _make_coordinator(hass, monkeypatch)

    class StubClient:
        async def status(self):
            raise aiohttp.ClientError("Temporary failure in name resolution")

    coord.client = StubClient()

    created: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        diag_mod.ir,
        "async_create_issue",
        lambda hass_, domain, issue_id, **kwargs: created.append((issue_id, kwargs)),
        raising=False,
    )

    for _ in range(4):
        with pytest.raises(UpdateFailed):
            await coord._async_update_data()
        coord._backoff_until = None

    issue_map = {issue_id: kwargs for issue_id, kwargs in created}
    assert ISSUE_DNS_RESOLUTION in issue_map
    dns_payload = issue_map[ISSUE_DNS_RESOLUTION]
    placeholders = dns_payload["translation_placeholders"]
    assert placeholders["site_id"] == coord.site_id
    metrics = dns_payload["data"]["site_metrics"]
    assert metrics["dns_errors"] >= 2


@pytest.mark.asyncio
async def test_http_error_description_from_json(hass, monkeypatch):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    coord = _make_coordinator(hass, monkeypatch)
    payload = '{"error":{"details":[{"description":"Too many requests"}]}}'

    class StubClient:
        async def status(self):
            raise _client_response_error(429, message=payload)

    coord.client = StubClient()

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord.last_failure_status == 429
    assert coord.last_failure_description == "Too many requests"
    assert coord.last_failure_response == payload
    assert coord.last_failure_source == "http"


@pytest.mark.asyncio
async def test_http_error_description_plain_text(hass, monkeypatch):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    coord = _make_coordinator(hass, monkeypatch)
    payload = " backend unavailable "

    class StubClient:
        async def status(self):
            raise _client_response_error(500, message=payload)

    coord.client = StubClient()

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord.last_failure_status == 500
    assert coord.last_failure_description == "Internal Server Error"
    assert coord.last_failure_response == "backend unavailable"


@pytest.mark.asyncio
async def test_http_error_description_falls_back_to_status_phrase(hass, monkeypatch):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    coord = _make_coordinator(hass, monkeypatch)

    class StubClient:
        async def status(self):
            raise _client_response_error(503, message=" ")

    coord.client = StubClient()

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord.last_failure_status == 503
    assert coord.last_failure_description == "Service Unavailable"
    assert coord.last_failure_response == ""


def test_collect_site_metrics_and_placeholders(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    now = datetime(2024, 1, 2, tzinfo=timezone.utc)
    coord.site_name = "Garage Site"
    coord.last_success_utc = now
    coord.last_failure_utc = now
    coord.last_failure_status = 503
    coord.last_failure_description = "Service Unavailable"
    coord.last_failure_source = "http"
    coord.last_failure_response = "response"
    coord.latency_ms = 123
    coord._backoff_until = time.monotonic() + 5
    coord.backoff_ends_utc = now
    coord._network_errors = 2
    coord._http_errors = 1
    coord._rate_limit_hits = 1
    coord._dns_failures = 0
    coord._last_error = "unauthorized"
    coord._phase_timings = {"status_s": 0.5}
    coord._last_refresh_cloud_calls = 3
    coord._last_steady_refresh_cloud_calls = 3
    coord._last_fast_refresh_cloud_calls = 7
    coord._session_history_cache_ttl = 300
    coord._hems_devices_using_stale = True
    coord._hems_devices_last_success_mono = time.monotonic() - 12
    coord._hems_devices_last_success_utc = now

    metrics = coord.collect_site_metrics()
    assert metrics["site_id"] == coord.site_id
    assert metrics["site_name"] == "Garage Site"
    assert metrics["last_success"] == now.isoformat()
    assert metrics["backoff_active"] is True
    assert metrics["phase_timings"] == {"status_s": 0.5}
    assert metrics["refresh_performance"] == {
        "total_s": 0.123,
        "total_budget_s": 30.0,
        "total_over_budget": False,
        "timed_stage_count": 1,
        "stage_budget_s": 5.0,
        "over_budget_stages": [],
        "slowest_stage": "status_s",
        "slowest_stage_s": 0.5,
        "cloud_calls": 3,
        "steady_cloud_calls": 3,
        "fast_cloud_calls": 7,
    }
    assert metrics["refresh_performance_history"]["sample_count"] == 0
    assert metrics["hems_devices_data_stale"] is True
    assert metrics["hems_devices_last_success_utc"] == now.isoformat()
    assert metrics["hems_devices_last_success_age_s"] >= 0

    placeholders = coord._issue_translation_placeholders(metrics)
    assert placeholders["site_id"] == coord.site_id
    assert placeholders["site_name"] == "Garage Site"
    assert placeholders["last_error"] == "unauthorized"
    assert placeholders["last_status"] == "503"


def test_refresh_pipeline_records_cloud_call_counts(hass, monkeypatch) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    client = SimpleNamespace(request_count=99)

    def reset_request_count() -> None:
        client.request_count = 0

    client.reset_request_count = reset_request_count
    coord.client = client

    context = coord._start_refresh_pipeline()  # noqa: SLF001
    assert client.request_count == 0
    client.request_count = 4
    coord._finish_refresh_pipeline(context)  # noqa: SLF001
    assert coord._last_refresh_cloud_calls == 4  # noqa: SLF001
    assert coord._last_steady_refresh_cloud_calls == 4  # noqa: SLF001
    assert coord._refresh_performance_history[-1]["cloud_calls"] == 4  # noqa: SLF001
    assert coord._refresh_performance_history[-1]["fast_poll"] is False  # noqa: SLF001

    context = coord._start_refresh_pipeline()  # noqa: SLF001
    context.fast_poll = True
    context.phase_timings["status_s"] = 0.2
    client.request_count = 6
    coord._finish_refresh_pipeline(context)  # noqa: SLF001
    assert coord._last_refresh_cloud_calls == 6  # noqa: SLF001
    assert coord._last_fast_refresh_cloud_calls == 6  # noqa: SLF001
    assert coord._refresh_performance_history[-1]["cloud_calls"] == 6  # noqa: SLF001
    assert coord._refresh_performance_history[-1]["fast_poll"] is True  # noqa: SLF001


def test_refresh_performance_history_summary_reports_percentiles() -> None:
    history = []
    for index in range(1, REFRESH_PERFORMANCE_HISTORY_LIMIT + 3):
        history = record_refresh_performance_sample(
            history,
            {"status_s": index / 10, "total_s": index / 5},
            cloud_calls=index,
            fast_poll=index % 2 == 0,
            first_refresh=index == 1,
            payload_using_stale=index == 3,
            manual_bypass=index == 4,
        )

    summary = refresh_performance_history_summary(history)

    assert len(history) == REFRESH_PERFORMANCE_HISTORY_LIMIT
    assert summary["sample_count"] == REFRESH_PERFORMANCE_HISTORY_LIMIT
    assert summary["fast_poll_count"] == 25
    assert summary["steady_poll_count"] == 25
    assert summary["first_refresh_count"] == 0
    assert summary["payload_using_stale_count"] == 1
    assert summary["manual_bypass_count"] == 1
    assert summary["total_s"]["p50_s"] == 5.4
    assert summary["total_s"]["p95_s"] == 10.0
    assert summary["stages"]["status_s"] == {
        "count": REFRESH_PERFORMANCE_HISTORY_LIMIT,
        "p50_s": 2.7,
        "p95_s": 5.0,
        "max_s": 5.2,
    }
    assert summary["cloud_calls"] == {
        "count": REFRESH_PERFORMANCE_HISTORY_LIMIT,
        "p50": 27.0,
        "p95": 50.0,
        "max": 52.0,
    }


def test_refresh_performance_history_handles_defensive_paths() -> None:
    class BrokenStarted:
        def isoformat(self) -> str:
            raise ValueError("bad timestamp")

        def __str__(self) -> str:
            return "broken-started"

    existing = [{"total_s": 1.0}]

    assert record_refresh_performance_sample(existing, {}, max_samples=1) == existing

    history = record_refresh_performance_sample(
        [{"not": "a timing"}],
        {"status_s": 0.1},
        refresh_started_utc=BrokenStarted(),  # type: ignore[arg-type]
        max_samples=2,
    )
    assert history[-1]["started_utc"] == "broken-started"

    summary = refresh_performance_history_summary(
        [
            {"total_s": "bad", "cloud_calls": "bad", "phase_timings": []},
            {"total_s": 0.5, "cloud_calls": 2, "phase_timings": {"status_s": 0.1}},
        ]
    )

    assert summary["sample_count"] == 2
    assert summary["total_s"]["count"] == 1
    assert summary["cloud_calls"]["count"] == 1
    assert summary["stages"]["status_s"]["count"] == 1


def test_collect_site_metrics_skips_negative_hems_age(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord._hems_devices_last_success_mono = time.monotonic() + 5  # noqa: SLF001

    metrics = coord.collect_site_metrics()

    assert "hems_devices_last_success_age_s" not in metrics


def test_refresh_performance_summary_flags_budget_and_bad_timings() -> None:
    summary = refresh_performance_summary(
        {"total_s": 31, "status_s": 6, "ignored": -1, "bad": "nope"},  # type: ignore[dict-item]
        latency_ms=120,
    )

    assert summary == {
        "total_s": 31.0,
        "total_budget_s": 30.0,
        "total_over_budget": True,
        "timed_stage_count": 1,
        "stage_budget_s": 5.0,
        "over_budget_stages": ["status_s"],
        "slowest_stage": "status_s",
        "slowest_stage_s": 6.0,
        "cloud_calls": None,
        "steady_cloud_calls": None,
        "fast_cloud_calls": None,
    }
    assert refresh_performance_summary({}, latency_ms="bad")["total_s"] is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_handle_client_unauthorized_refresh(monkeypatch, hass):
    from custom_components.enphase_ev import coordinator_diagnostics as diag_mod

    coord = _make_coordinator(hass, monkeypatch)
    coord._attempt_auto_refresh = AsyncMock(return_value=True)
    created: list[tuple[str, dict]] = []
    deleted: list[str] = []

    monkeypatch.setattr(
        diag_mod.ir,
        "async_create_issue",
        lambda *args, **kwargs: created.append((args[2], kwargs)),
        raising=False,
    )
    monkeypatch.setattr(
        diag_mod.ir,
        "async_delete_issue",
        lambda hass_, domain, issue_id: deleted.append(issue_id),
        raising=False,
    )

    result = await coord._handle_client_unauthorized()
    assert result is True
    assert coord._unauth_errors == 0
    assert coord._last_error == "unauthorized"
    assert deleted == [
        "reauth_required",
        "auth_blocked",
        "too_many_active_sessions",
    ]
    assert created == []


@pytest.mark.asyncio
async def test_handle_client_unauthorized_failure(monkeypatch, hass):
    from homeassistant.exceptions import ConfigEntryAuthFailed
    from custom_components.enphase_ev import coordinator_diagnostics as diag_mod

    coord = _make_coordinator(hass, monkeypatch)
    coord.site_name = "Garage Site"
    coord.last_failure_status = 401
    coord.last_failure_description = "Unauthorized"
    coord._last_error = "stale"
    coord._attempt_auto_refresh = AsyncMock(return_value=False)
    coord._unauth_errors = 1

    created: list[tuple[str, dict]] = []
    deleted: list[str] = []

    monkeypatch.setattr(
        diag_mod.ir,
        "async_create_issue",
        lambda hass_, domain, issue_id, **kwargs: created.append((issue_id, kwargs)),
        raising=False,
    )
    monkeypatch.setattr(
        diag_mod.ir,
        "async_delete_issue",
        lambda hass_, domain, issue_id: deleted.append(issue_id),
        raising=False,
    )

    with pytest.raises(ConfigEntryAuthFailed):
        await coord._handle_client_unauthorized()

    assert deleted == [
        "reauth_required",
        "auth_blocked",
        "too_many_active_sessions",
    ]
    assert coord._unauth_errors >= 2
    assert created == []


@pytest.mark.asyncio
async def test_handle_client_unauthorized_reports_auth_block_when_active(
    monkeypatch, hass
):
    from homeassistant.helpers.update_coordinator import UpdateFailed
    from custom_components.enphase_ev import coordinator_diagnostics as diag_mod

    coord = _make_coordinator(hass, monkeypatch)
    coord._attempt_auto_refresh = AsyncMock(return_value=False)
    coord._auth_block_active = MagicMock(return_value=True)  # type: ignore[method-assign]  # noqa: SLF001
    coord._blocked_auth_failure_message = MagicMock(return_value="blocked")  # type: ignore[method-assign]  # noqa: SLF001

    created: list[tuple[str, dict]] = []

    monkeypatch.setattr(
        diag_mod.ir,
        "async_create_issue",
        lambda hass_, domain, issue_id, **kwargs: created.append((issue_id, kwargs)),
        raising=False,
    )
    monkeypatch.setattr(
        diag_mod.ir,
        "async_delete_issue",
        lambda *args, **kwargs: None,
        raising=False,
    )

    with pytest.raises(UpdateFailed, match="blocked"):
        await coord._handle_client_unauthorized()

    assert [issue_id for issue_id, _payload in created] == ["auth_blocked"]


@pytest.mark.asyncio
async def test_handle_client_unauthorized_reports_too_many_sessions_issue(
    monkeypatch, hass
):
    from homeassistant.helpers.update_coordinator import UpdateFailed
    from custom_components.enphase_ev import coordinator_diagnostics as diag_mod

    coord = _make_coordinator(hass, monkeypatch)
    coord._attempt_auto_refresh = AsyncMock(return_value=False)
    coord._auth_block_reason = "too_many_active_sessions"
    coord._auth_block_active = MagicMock(return_value=True)  # type: ignore[method-assign]  # noqa: SLF001
    coord._blocked_auth_failure_message = MagicMock(return_value="blocked")  # type: ignore[method-assign]  # noqa: SLF001

    created: list[tuple[str, dict]] = []
    deleted: list[str] = []

    monkeypatch.setattr(
        diag_mod.ir,
        "async_create_issue",
        lambda hass_, domain, issue_id, **kwargs: created.append((issue_id, kwargs)),
        raising=False,
    )
    monkeypatch.setattr(
        diag_mod.ir,
        "async_delete_issue",
        lambda hass_, domain, issue_id: deleted.append(issue_id),
        raising=False,
    )

    with pytest.raises(UpdateFailed, match="blocked"):
        await coord._handle_client_unauthorized()

    assert [issue_id for issue_id, _payload in created] == ["too_many_active_sessions"]
    assert "auth_blocked" in deleted


@pytest.mark.asyncio
async def test_async_start_stop_trigger_paths(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord.serials = {RANDOM_SERIAL}
    coord.data = {RANDOM_SERIAL: {"charging_level": 18, "plugged": True}}
    coord.last_set_amps = {}

    coord.require_plugged = MagicMock()
    coord.set_last_set_amps = MagicMock()
    coord.set_desired_charging = MagicMock()
    coord.set_charging_expectation = MagicMock()
    coord.kick_fast = MagicMock()
    coord.async_request_refresh = AsyncMock()

    coordinator_data = {RANDOM_SERIAL: {"plugged": False, "charging_level": 20}}
    coord.data = coordinator_data

    async def _trigger_message(sn, message):
        return {"sent": message, "serial": sn}

    coord.client = SimpleNamespace(
        start_charging=AsyncMock(return_value={"status": "ok"}),
        stop_charging=AsyncMock(return_value=None),
        start_live_stream=AsyncMock(
            return_value={"status": "accepted", "duration_s": 900}
        ),
        trigger_message=AsyncMock(side_effect=_trigger_message),
    )

    await coord.async_start_charging(RANDOM_SERIAL, connector_id=None, fallback_amps=24)
    coord.client.start_charging.assert_awaited_once_with(
        RANDOM_SERIAL, 20, 1, include_level=None, strict_preference=False
    )
    coord.set_desired_charging.assert_called_with(RANDOM_SERIAL, True)

    coord.client.start_charging.reset_mock()
    coord.client.start_charging.return_value = {"status": "not_ready"}
    result = await coord.async_start_charging(
        RANDOM_SERIAL, requested_amps=10, connector_id=2, allow_unplugged=True
    )
    assert result == {"status": "not_ready"}

    await coord.async_stop_charging(RANDOM_SERIAL, allow_unplugged=False)
    coord.client.stop_charging.assert_awaited_once_with(RANDOM_SERIAL)
    coord.require_plugged.assert_called()

    reply = await coord.async_trigger_ocpp_message(RANDOM_SERIAL, "Status")
    coord.client.trigger_message.assert_awaited_once_with(RANDOM_SERIAL, "Status")
    assert reply["sent"] == "Status"


@pytest.mark.asyncio
async def test_async_start_charging_manual_mode_sends_requested_amps(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord.serials = {RANDOM_SERIAL}
    coord.data = {
        RANDOM_SERIAL: {
            "plugged": True,
            "charging_level": 26,
            "charge_mode_pref": "MANUAL_CHARGING",
        }
    }
    coord.last_set_amps = {}
    coord.set_charging_expectation = MagicMock()
    coord.kick_fast = MagicMock()
    coord.client = SimpleNamespace(
        start_charging=AsyncMock(return_value={"status": "ok"}),
        stop_charging=AsyncMock(return_value=None),
        set_charge_mode=AsyncMock(return_value={"status": "ok"}),
        start_live_stream=AsyncMock(
            return_value={"status": "accepted", "duration_s": 900}
        ),
    )
    coord.async_request_refresh = AsyncMock()

    await coord.async_start_charging(RANDOM_SERIAL)

    coord.client.start_charging.assert_awaited_once_with(
        RANDOM_SERIAL, 26, 1, include_level=True, strict_preference=False
    )
    coord.client.set_charge_mode.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_start_charging_manual_mode_explicit_request_stays_strict(
    hass, monkeypatch
):
    coord = _make_coordinator(hass, monkeypatch)
    coord.serials = {RANDOM_SERIAL}
    coord.data = {
        RANDOM_SERIAL: {
            "plugged": True,
            "charging_level": 26,
            "charge_mode_pref": "MANUAL_CHARGING",
        }
    }
    coord.last_set_amps = {}
    coord.set_charging_expectation = MagicMock()
    coord.kick_fast = MagicMock()
    coord.client = SimpleNamespace(
        start_charging=AsyncMock(return_value={"status": "ok"}),
        stop_charging=AsyncMock(return_value=None),
        set_charge_mode=AsyncMock(return_value={"status": "ok"}),
        start_live_stream=AsyncMock(
            return_value={"status": "accepted", "duration_s": 900}
        ),
    )
    coord.async_request_refresh = AsyncMock()

    await coord.async_start_charging(RANDOM_SERIAL, requested_amps=24)

    coord.client.start_charging.assert_awaited_once_with(
        RANDOM_SERIAL, 24, 1, include_level=True, strict_preference=True
    )
    coord.client.set_charge_mode.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_start_and_stop_preserve_scheduled_mode(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord.serials = {RANDOM_SERIAL}
    coord.data = {
        RANDOM_SERIAL: {
            "plugged": True,
            "charging_level": 18,
            "charge_mode_pref": "SCHEDULED_CHARGING",
        }
    }
    coord.last_set_amps = {}
    coord.set_charging_expectation = MagicMock()
    coord.kick_fast = MagicMock()
    coord.client = SimpleNamespace(
        start_charging=AsyncMock(return_value={"status": "ok"}),
        stop_charging=AsyncMock(return_value={"status": "ok"}),
        set_charge_mode=AsyncMock(return_value={"status": "ok"}),
        start_live_stream=AsyncMock(
            return_value={"status": "accepted", "duration_s": 900}
        ),
    )
    coord.async_request_refresh = AsyncMock()

    await coord.async_start_charging(RANDOM_SERIAL)
    coord.client.start_charging.assert_awaited_once_with(
        RANDOM_SERIAL, 18, 1, include_level=True, strict_preference=False
    )
    coord.client.set_charge_mode.assert_awaited_once_with(
        RANDOM_SERIAL, "SCHEDULED_CHARGING"
    )

    coord.client.set_charge_mode.reset_mock()
    await coord.async_stop_charging(RANDOM_SERIAL)
    coord.client.set_charge_mode.assert_awaited_once_with(
        RANDOM_SERIAL, "SCHEDULED_CHARGING"
    )


@pytest.mark.asyncio
async def test_async_start_charging_scheduled_mode_explicit_request_stays_strict(
    hass, monkeypatch
):
    coord = _make_coordinator(hass, monkeypatch)
    coord.serials = {RANDOM_SERIAL}
    coord.data = {
        RANDOM_SERIAL: {
            "plugged": True,
            "charging_level": 18,
            "charge_mode_pref": "SCHEDULED_CHARGING",
        }
    }
    coord.last_set_amps = {}
    coord.set_charging_expectation = MagicMock()
    coord.kick_fast = MagicMock()
    coord.client = SimpleNamespace(
        start_charging=AsyncMock(return_value={"status": "ok"}),
        stop_charging=AsyncMock(return_value={"status": "ok"}),
        set_charge_mode=AsyncMock(return_value={"status": "ok"}),
        start_live_stream=AsyncMock(
            return_value={"status": "accepted", "duration_s": 900}
        ),
    )
    coord.async_request_refresh = AsyncMock()

    await coord.async_start_charging(RANDOM_SERIAL, requested_amps=24)

    coord.client.start_charging.assert_awaited_once_with(
        RANDOM_SERIAL, 24, 1, include_level=True, strict_preference=True
    )
    coord.client.set_charge_mode.assert_awaited_once_with(
        RANDOM_SERIAL, "SCHEDULED_CHARGING"
    )


@pytest.mark.asyncio
async def test_async_start_charging_green_mode_omits_amp_payload(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord.serials = {RANDOM_SERIAL}
    coord.data = {
        RANDOM_SERIAL: {
            "plugged": True,
            "charging_level": 30,
            "charge_mode_pref": "GREEN_CHARGING",
        }
    }
    coord.last_set_amps = {}
    coord.set_charging_expectation = MagicMock()
    coord.kick_fast = MagicMock()
    coord.client = SimpleNamespace(
        start_charging=AsyncMock(return_value={"status": "ok"}),
        stop_charging=AsyncMock(return_value=None),
        set_charge_mode=AsyncMock(return_value={"status": "ok"}),
        start_live_stream=AsyncMock(
            return_value={"status": "accepted", "duration_s": 900}
        ),
    )
    coord.async_request_refresh = AsyncMock()

    await coord.async_start_charging(RANDOM_SERIAL)

    coord.client.start_charging.assert_awaited_once_with(
        RANDOM_SERIAL, 30, 1, include_level=False, strict_preference=True
    )
    coord.client.set_charge_mode.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_amp_restart_cancels_existing_task(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    pending = asyncio.Future()
    coord._amp_restart_tasks[RANDOM_SERIAL] = pending

    calls: list[tuple[str, float]] = []

    async def _fake_restart(sn: str, delay: float) -> None:
        calls.append((sn, delay))

    coord._async_restart_after_amp_change = _fake_restart  # type: ignore[assignment]

    tasks: list[asyncio.Task] = []

    def _capture(coro, name=None):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    monkeypatch.setattr(hass, "async_create_task", _capture)

    coord.schedule_amp_restart(RANDOM_SERIAL, delay=12)

    assert pending.cancelled()
    assert tasks, "restart task should be scheduled"
    await tasks[0]
    assert calls == [(RANDOM_SERIAL, 12)]
    assert RANDOM_SERIAL not in coord._amp_restart_tasks


@pytest.mark.asyncio
async def test_schedule_amp_restart_handles_typeerror(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)

    calls: list[tuple[str, float]] = []

    async def _fake_restart(sn: str, delay: float) -> None:
        calls.append((sn, delay))

    coord._async_restart_after_amp_change = _fake_restart  # type: ignore[assignment]

    tasks: list[asyncio.Task] = []

    def _create_task(coro, name=None):
        if name is not None:
            coro.close()
            raise TypeError("name kw not supported")
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    monkeypatch.setattr(hass, "async_create_task", _create_task)

    coord.schedule_amp_restart(RANDOM_SERIAL, delay=8)

    assert tasks, "fallback task should be scheduled without a name kwarg"
    await tasks[0]
    assert calls == [(RANDOM_SERIAL, 8)]


@pytest.mark.asyncio
async def test_async_restart_after_amp_change_flow(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord.async_stop_charging = AsyncMock()
    coord.async_start_charging = AsyncMock()

    sleep_mock = AsyncMock()
    with patch("custom_components.enphase_ev.coordinator.asyncio.sleep", sleep_mock):
        await coord._async_restart_after_amp_change(RANDOM_SERIAL, 5)

    coord.async_stop_charging.assert_awaited_once_with(
        RANDOM_SERIAL, hold_seconds=90.0, fast_seconds=60, allow_unplugged=True
    )
    sleep_mock.assert_awaited_once_with(5.0)
    coord.async_start_charging.assert_awaited_once_with(RANDOM_SERIAL)


@pytest.mark.asyncio
async def test_async_restart_after_amp_change_handles_start_error(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord.async_stop_charging = AsyncMock()
    coord.async_start_charging = AsyncMock(side_effect=ServiceValidationError("oops"))

    sleep_mock = AsyncMock()
    with patch("custom_components.enphase_ev.coordinator.asyncio.sleep", sleep_mock):
        await coord._async_restart_after_amp_change(RANDOM_SERIAL, 0)

    sleep_mock.assert_not_awaited()
    coord.async_start_charging.assert_awaited_once_with(RANDOM_SERIAL)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("translation_key", "expected_reason"),
    [
        ("charger_not_plugged", "not plugged in"),
        ("auth_required", "authentication required"),
    ],
)
async def test_async_restart_after_amp_change_validation_reasons(
    hass, monkeypatch, caplog, translation_key, expected_reason
):
    coord = _make_coordinator(hass, monkeypatch)
    coord.async_stop_charging = AsyncMock()
    try:
        err = ServiceValidationError("oops", translation_key=translation_key)
    except TypeError:
        err = ServiceValidationError("oops")
        err.translation_key = translation_key
    coord.async_start_charging = AsyncMock(side_effect=err)

    caplog.set_level(logging.DEBUG)

    await coord._async_restart_after_amp_change(RANDOM_SERIAL, 0)

    assert f"because {expected_reason}" in caplog.text


@pytest.mark.asyncio
async def test_async_restart_after_amp_change_handles_stop_error(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord.async_stop_charging = AsyncMock(side_effect=RuntimeError("boom"))
    coord.async_start_charging = AsyncMock()

    sleep_mock = AsyncMock()
    with patch("custom_components.enphase_ev.coordinator.asyncio.sleep", sleep_mock):
        await coord._async_restart_after_amp_change(RANDOM_SERIAL, 10)

    coord.async_start_charging.assert_not_awaited()
    sleep_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_restart_after_amp_change_invalid_delay_defaults(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord.async_stop_charging = AsyncMock()
    coord.async_start_charging = AsyncMock()

    sleep_mock = AsyncMock()
    with patch("custom_components.enphase_ev.coordinator.asyncio.sleep", sleep_mock):
        await coord._async_restart_after_amp_change(RANDOM_SERIAL, object())

    coord.async_stop_charging.assert_awaited_once_with(
        RANDOM_SERIAL, hold_seconds=90.0, fast_seconds=60, allow_unplugged=True
    )
    sleep_mock.assert_awaited_once_with(30.0)
    coord.async_start_charging.assert_awaited_once_with(RANDOM_SERIAL)


@pytest.mark.asyncio
async def test_async_restart_after_amp_change_sleep_error(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord.async_stop_charging = AsyncMock()
    coord.async_start_charging = AsyncMock()

    sleep_mock = AsyncMock(side_effect=RuntimeError("timer boom"))
    with patch("custom_components.enphase_ev.coordinator.asyncio.sleep", sleep_mock):
        await coord._async_restart_after_amp_change(RANDOM_SERIAL, 2)

    coord.async_stop_charging.assert_awaited_once()
    sleep_mock.assert_awaited_once_with(2.0)
    coord.async_start_charging.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_restart_after_amp_change_handles_generic_start_error(
    hass, monkeypatch
):
    coord = _make_coordinator(hass, monkeypatch)
    coord.async_stop_charging = AsyncMock()
    coord.async_start_charging = AsyncMock(side_effect=RuntimeError("start boom"))

    sleep_mock = AsyncMock()
    with patch("custom_components.enphase_ev.coordinator.asyncio.sleep", sleep_mock):
        await coord._async_restart_after_amp_change(RANDOM_SERIAL, 3)

    coord.async_stop_charging.assert_awaited_once()
    sleep_mock.assert_awaited_once_with(3.0)
    coord.async_start_charging.assert_awaited_once_with(RANDOM_SERIAL)


@pytest.mark.asyncio
async def test_fast_poll_kicked_on_external_toggle(hass, monkeypatch, load_fixture):
    coord = _make_coordinator(hass, monkeypatch)
    coord.serials = {RANDOM_SERIAL}

    fast_windows: list[int] = []

    def _record_fast(duration=60):
        fast_windows.append(duration)

    coord.kick_fast = _record_fast  # type: ignore[assignment]

    idle_payload = load_fixture("status_idle.json")
    charging_payload = load_fixture("status_charging.json")

    class StubClient:
        def __init__(self, payload):
            self.payload = payload

        async def status(self):
            return copy.deepcopy(self.payload)

    client = StubClient(idle_payload)
    coord.client = client

    await coord._async_update_data()
    assert fast_windows == []

    client.payload = charging_payload
    await coord._async_update_data()
    assert fast_windows == [FAST_TOGGLE_POLL_HOLD_S]

    await coord._async_update_data()
    assert fast_windows == [FAST_TOGGLE_POLL_HOLD_S]

    client.payload = idle_payload
    await coord._async_update_data()
    assert fast_windows == [FAST_TOGGLE_POLL_HOLD_S, FAST_TOGGLE_POLL_HOLD_S]


@pytest.mark.asyncio
async def test_fast_poll_not_triggered_by_expectation_only(
    hass, monkeypatch, load_fixture
):
    coord = _make_coordinator(hass, monkeypatch)
    coord.serials = {RANDOM_SERIAL}

    fast_windows: list[int] = []

    def _record_fast(duration=60):
        fast_windows.append(duration)

    coord.kick_fast = _record_fast  # type: ignore[assignment]

    idle_payload = load_fixture("status_idle.json")

    class StubClient:
        def __init__(self, payload):
            self.payload = payload

        async def status(self):
            return copy.deepcopy(self.payload)

    client = StubClient(idle_payload)
    coord.client = client

    await coord._async_update_data()
    assert fast_windows == []

    coord.set_charging_expectation(RANDOM_SERIAL, True, hold_for=10)
    await coord._async_update_data()
    assert fast_windows == []


def test_record_actual_charging_clears_none_state(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord._last_actual_charging[RANDOM_SERIAL] = True

    fast_windows: list[int] = []

    def _record_fast(duration=60):
        fast_windows.append(duration)

    coord.kick_fast = _record_fast  # type: ignore[assignment]

    coord._record_actual_charging(RANDOM_SERIAL, None)

    assert RANDOM_SERIAL not in coord._last_actual_charging
    assert fast_windows == []


def test_record_actual_charging_ignores_repeated_state(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)

    fast_windows: list[int] = []

    def _record_fast(duration=60):
        fast_windows.append(duration)

    coord.kick_fast = _record_fast  # type: ignore[assignment]

    coord._record_actual_charging(RANDOM_SERIAL, False)
    coord._record_actual_charging(RANDOM_SERIAL, False)

    assert coord._last_actual_charging[RANDOM_SERIAL] is False
    assert fast_windows == []


@pytest.mark.asyncio
async def test_runtime_serial_discovery(hass, monkeypatch, config_entry):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }
    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )

    class DummyClient:
        def __init__(self):
            self._calls = 0

        async def status(self):
            self._calls += 1
            return {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "connectors": [{}],
                        "session_d": {},
                        "sch_d": {},
                        "charging": False,
                    },
                    {
                        "sn": "NEW123456789",
                        "name": "Workshop EV",
                        "connectors": [{}],
                        "session_d": {},
                        "sch_d": {},
                        "charging": False,
                    },
                ]
            }

        async def summary_v2(self):
            return [
                {
                    "serialNumber": RANDOM_SERIAL,
                    "displayName": "Garage EV",
                    "maxCurrent": 48,
                },
                {
                    "serialNumber": "NEW123456789",
                    "displayName": "Workshop EV",
                    "maxCurrent": 32,
                    "hwVersion": "1.2.3",
                    "swVersion": "5.6.7",
                },
            ]

        async def charge_mode(self, sn: str):
            return None

        async def session_history(self, *args, **kwargs):
            return {"data": {"result": [], "hasMore": False}}

    cfg = dict(config_entry.data)
    coord = EnphaseCoordinator(hass, cfg, config_entry=config_entry)
    coord.client = DummyClient()
    await coord.async_refresh()

    assert "NEW123456789" in coord.serials
    assert coord.iter_serials() == [RANDOM_SERIAL, "NEW123456789"]
    assert "NEW123456789" in coord.data
    assert coord.data["NEW123456789"]["display_name"] == "Workshop EV"


@pytest.mark.asyncio
async def test_first_refresh_defers_session_history(hass, monkeypatch, config_entry):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )

    class DummyClient:
        def __init__(self):
            self.history_calls = 0

        async def status(self):
            return {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "connectors": [{}],
                        "session_d": {},
                        "sch_d": {},
                        "charging": False,
                    }
                ]
            }

        async def summary_v2(self):
            return [{"serialNumber": RANDOM_SERIAL, "displayName": "Garage EV"}]

        async def charge_mode(self, sn: str):
            return None

        async def session_history(self, *args, **kwargs):
            self.history_calls += 1
            now = datetime.now(timezone.utc)
            epoch = now.timestamp()
            return {
                "data": {
                    "result": [
                        {
                            "sessionId": "42",
                            "startTime": epoch - 600,
                            "endTime": epoch - 300,
                            "aggEnergyValue": 1.234,
                            "activeChargeTime": 900,
                        }
                    ],
                    "hasMore": False,
                }
            }

    scheduled: list[tuple[tuple[str, ...], datetime]] = []
    original_schedule = coord_mod.EnphaseCoordinator._schedule_session_enrichment

    def capture_schedule(self, serials, day_local):
        scheduled.append((tuple(serials), day_local))
        return original_schedule(self, serials, day_local)

    monkeypatch.setattr(
        coord_mod.EnphaseCoordinator,
        "_schedule_session_enrichment",
        capture_schedule,
        raising=False,
    )

    coord = EnphaseCoordinator(hass, cfg, config_entry=config_entry)
    client = DummyClient()
    coord.client = client

    await coord.async_refresh()

    assert client.history_calls == 0
    assert "status_s" in coord.phase_timings
    assert coord.data[RANDOM_SERIAL]["energy_today_sessions"] == []

    assert len(scheduled) == 1
    scheduled_serials, scheduled_day = scheduled[0]
    assert scheduled_serials == (RANDOM_SERIAL,)
    assert isinstance(scheduled_day, datetime)


@pytest.mark.asyncio
async def test_steady_state_refresh_defers_stale_session_history_when_cached(
    hass, monkeypatch
) -> None:
    await hass.config.async_set_time_zone("UTC")
    coord = _make_coordinator(hass, monkeypatch)
    coord._has_successful_refresh = True  # noqa: SLF001
    coord.data = {RANDOM_SERIAL: {"display_name": "Garage EV"}}

    now_local = datetime(2025, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: now_local)

    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": RANDOM_SERIAL,
                    "name": "Garage EV",
                    "connectors": [{}],
                    "session_d": {},
                    "sch_d": {},
                    "charging": False,
                }
            ]
        }
    )
    coord.client.summary_v2 = AsyncMock(
        return_value=[{"serialNumber": RANDOM_SERIAL, "displayName": "Garage EV"}]
    )
    coord.client.charge_mode = AsyncMock(return_value=None)

    day_key = now_local.strftime("%Y-%m-%d")
    cached_sessions = [{"session_id": "cached", "energy_kwh": 1.25}]
    coord.session_history._cache[(RANDOM_SERIAL, day_key)] = (  # noqa: SLF001
        time.monotonic() - coord.session_history.cache_ttl - 1,
        cached_sessions,
    )
    coord._async_enrich_sessions = AsyncMock(return_value={})  # type: ignore[assignment]  # noqa: SLF001
    coord._schedule_session_enrichment = MagicMock()  # type: ignore[assignment]  # noqa: SLF001

    data = await coord._async_update_data()

    assert data[RANDOM_SERIAL]["energy_today_sessions"] == cached_sessions
    assert data[RANDOM_SERIAL]["energy_today_sessions_kwh"] == pytest.approx(1.25)
    coord._async_enrich_sessions.assert_not_awaited()  # type: ignore[attr-defined]
    coord._schedule_session_enrichment.assert_called_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_active_session_history_refreshes_early_in_background(
    hass, monkeypatch
) -> None:
    await hass.config.async_set_time_zone("UTC")
    coord = _make_coordinator(hass, monkeypatch)
    coord._has_successful_refresh = True  # noqa: SLF001
    coord.data = {RANDOM_SERIAL: {"display_name": "Garage EV"}}

    now_local = datetime(2025, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: now_local)

    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": RANDOM_SERIAL,
                    "name": "Garage EV",
                    "connectors": [{}],
                    "session_d": {},
                    "sch_d": {},
                    "charging": True,
                    "pluggedIn": True,
                }
            ]
        }
    )
    coord.client.summary_v2 = AsyncMock(
        return_value=[{"serialNumber": RANDOM_SERIAL, "displayName": "Garage EV"}]
    )
    coord.client.charge_mode = AsyncMock(return_value=None)

    day_key = now_local.strftime("%Y-%m-%d")
    cached_sessions = [{"session_id": "cached", "energy_kwh": 1.25}]
    coord.session_history._cache[(RANDOM_SERIAL, day_key)] = (  # noqa: SLF001
        time.monotonic() - 180,
        cached_sessions,
    )
    coord._async_enrich_sessions = AsyncMock(return_value={})  # type: ignore[assignment]  # noqa: SLF001
    coord._schedule_session_enrichment = MagicMock()  # type: ignore[assignment]  # noqa: SLF001

    data = await coord._async_update_data()

    assert data[RANDOM_SERIAL]["energy_today_sessions"] == cached_sessions
    coord._async_enrich_sessions.assert_not_awaited()  # type: ignore[attr-defined]
    coord._schedule_session_enrichment.assert_called_once()  # type: ignore[attr-defined]
    assert (
        coord._schedule_session_enrichment.call_args.kwargs["max_cache_age"] == 120.0
    )  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_first_refresh_with_cached_active_session_defers_in_background(
    hass, monkeypatch
) -> None:
    await hass.config.async_set_time_zone("UTC")
    coord = _make_coordinator(hass, monkeypatch)
    coord.data = {RANDOM_SERIAL: {"display_name": "Garage EV"}}

    now_local = datetime(2025, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: now_local)

    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": RANDOM_SERIAL,
                    "name": "Garage EV",
                    "connectors": [{}],
                    "session_d": {},
                    "sch_d": {},
                    "charging": True,
                    "pluggedIn": True,
                }
            ]
        }
    )
    coord.client.summary_v2 = AsyncMock(
        return_value=[{"serialNumber": RANDOM_SERIAL, "displayName": "Garage EV"}]
    )
    coord.client.charge_mode = AsyncMock(return_value=None)

    day_key = now_local.strftime("%Y-%m-%d")
    cached_sessions = [{"session_id": "cached", "energy_kwh": 1.25}]
    coord.session_history._cache[(RANDOM_SERIAL, day_key)] = (  # noqa: SLF001
        time.monotonic() - 180,
        cached_sessions,
    )
    coord._async_enrich_sessions = AsyncMock(return_value={})  # type: ignore[assignment]  # noqa: SLF001
    coord._schedule_session_enrichment = MagicMock()  # type: ignore[assignment]  # noqa: SLF001

    data = await coord._async_update_data()

    assert data[RANDOM_SERIAL]["energy_today_sessions"] == cached_sessions
    coord._async_enrich_sessions.assert_not_awaited()  # type: ignore[attr-defined]
    coord._schedule_session_enrichment.assert_called_once()  # type: ignore[attr-defined]
    assert (
        coord._schedule_session_enrichment.call_args.kwargs["max_cache_age"] == 120.0
    )  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_active_session_history_hard_ttl_refreshes_inline(
    hass, monkeypatch
) -> None:
    await hass.config.async_set_time_zone("UTC")
    coord = _make_coordinator(hass, monkeypatch)
    coord._has_successful_refresh = True  # noqa: SLF001
    coord.data = {RANDOM_SERIAL: {"display_name": "Garage EV"}}

    now_local = datetime(2025, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: now_local)

    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": RANDOM_SERIAL,
                    "name": "Garage EV",
                    "connectors": [{}],
                    "session_d": {},
                    "sch_d": {},
                    "charging": True,
                    "pluggedIn": True,
                }
            ]
        }
    )
    coord.client.summary_v2 = AsyncMock(
        return_value=[{"serialNumber": RANDOM_SERIAL, "displayName": "Garage EV"}]
    )
    coord.client.charge_mode = AsyncMock(return_value=None)

    day_key = now_local.strftime("%Y-%m-%d")
    coord.session_history._cache[(RANDOM_SERIAL, day_key)] = (  # noqa: SLF001
        time.monotonic() - coord.session_history.cache_ttl - 5,
        [{"session_id": "stale", "energy_kwh": 1.0}],
    )
    coord._async_enrich_sessions = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        return_value={RANDOM_SERIAL: [{"session_id": "fresh", "energy_kwh": 2.5}]}
    )
    coord._schedule_session_enrichment = MagicMock()  # type: ignore[assignment]  # noqa: SLF001

    data = await coord._async_update_data()

    assert data[RANDOM_SERIAL]["energy_today_sessions"] == [
        {"session_id": "fresh", "energy_kwh": 2.5}
    ]
    assert data[RANDOM_SERIAL]["energy_today_sessions_kwh"] == pytest.approx(2.5)
    coord._async_enrich_sessions.assert_awaited_once()  # type: ignore[attr-defined]
    assert coord._async_enrich_sessions.await_args.kwargs["max_cache_age"] == 120.0  # type: ignore[attr-defined]
    coord._schedule_session_enrichment.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_recent_unavailable_session_history_without_valid_cache_defers_retry(
    hass, monkeypatch
) -> None:
    await hass.config.async_set_time_zone("UTC")
    coord = _make_coordinator(hass, monkeypatch)
    coord._has_successful_refresh = True  # noqa: SLF001
    coord.data = {RANDOM_SERIAL: {"display_name": "Garage EV"}}

    now_local = datetime(2025, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: now_local)

    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": RANDOM_SERIAL,
                    "name": "Garage EV",
                    "connectors": [{}],
                    "session_d": {},
                    "sch_d": {},
                    "charging": True,
                    "pluggedIn": True,
                }
            ]
        }
    )
    coord.client.summary_v2 = AsyncMock(
        return_value=[{"serialNumber": RANDOM_SERIAL, "displayName": "Garage EV"}]
    )
    coord.client.charge_mode = AsyncMock(return_value=None)

    day_key = now_local.strftime("%Y-%m-%d")
    coord.session_history._cache[(RANDOM_SERIAL, day_key)] = (
        SessionCacheEntry(  # noqa: SLF001
            cached_at_mono=time.monotonic(),
            sessions=[],
            state=SESSION_CACHE_STATE_UNAVAILABLE,
            last_error="down",
            has_valid_cache=False,
        )
    )
    coord._async_enrich_sessions = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        return_value={RANDOM_SERIAL: [{"session_id": "fresh", "energy_kwh": 2.5}]}
    )
    coord._schedule_session_enrichment = MagicMock()  # type: ignore[assignment]  # noqa: SLF001

    data = await coord._async_update_data()

    assert data[RANDOM_SERIAL]["energy_today_sessions"] == []
    coord._async_enrich_sessions.assert_not_awaited()  # type: ignore[attr-defined]
    coord._schedule_session_enrichment.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_aged_unavailable_session_history_without_valid_cache_refreshes_inline(
    hass, monkeypatch
) -> None:
    await hass.config.async_set_time_zone("UTC")
    coord = _make_coordinator(hass, monkeypatch)
    coord._has_successful_refresh = True  # noqa: SLF001
    coord.data = {RANDOM_SERIAL: {"display_name": "Garage EV"}}

    now_local = datetime(2025, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: now_local)

    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": RANDOM_SERIAL,
                    "name": "Garage EV",
                    "connectors": [{}],
                    "session_d": {},
                    "sch_d": {},
                    "charging": True,
                    "pluggedIn": True,
                }
            ]
        }
    )
    coord.client.summary_v2 = AsyncMock(
        return_value=[{"serialNumber": RANDOM_SERIAL, "displayName": "Garage EV"}]
    )
    coord.client.charge_mode = AsyncMock(return_value=None)

    day_key = now_local.strftime("%Y-%m-%d")
    coord.session_history._cache[(RANDOM_SERIAL, day_key)] = (
        SessionCacheEntry(  # noqa: SLF001
            cached_at_mono=time.monotonic()
            - coord._session_history_cache_ttl
            - 1,  # noqa: SLF001
            sessions=[],
            state=SESSION_CACHE_STATE_UNAVAILABLE,
            last_error="down",
            has_valid_cache=False,
        )
    )
    coord._async_enrich_sessions = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        return_value={RANDOM_SERIAL: [{"session_id": "fresh", "energy_kwh": 2.5}]}
    )
    coord._schedule_session_enrichment = MagicMock()  # type: ignore[assignment]  # noqa: SLF001

    data = await coord._async_update_data()

    assert data[RANDOM_SERIAL]["energy_today_sessions"] == [
        {"session_id": "fresh", "energy_kwh": 2.5}
    ]
    coord._async_enrich_sessions.assert_awaited_once()  # type: ignore[attr-defined]
    coord._schedule_session_enrichment.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_missing_session_cache_age_defers_idle_refresh_after_startup(
    hass, monkeypatch
) -> None:
    await hass.config.async_set_time_zone("UTC")
    coord = _make_coordinator(hass, monkeypatch)
    coord._has_successful_refresh = True  # noqa: SLF001
    coord.data = {RANDOM_SERIAL: {"display_name": "Garage EV"}}

    now_local = datetime(2025, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: now_local)

    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": RANDOM_SERIAL,
                    "name": "Garage EV",
                    "connectors": [{}],
                    "session_d": {},
                    "sch_d": {},
                    "charging": False,
                }
            ]
        }
    )
    coord.client.summary_v2 = AsyncMock(
        return_value=[{"serialNumber": RANDOM_SERIAL, "displayName": "Garage EV"}]
    )
    coord.client.charge_mode = AsyncMock(return_value=None)
    coord.session_history.get_cache_view = MagicMock(  # type: ignore[assignment]
        return_value=SessionCacheView(
            sessions=[{"session_id": "cached", "energy_kwh": 1.0}],
            cache_age=None,
            needs_refresh=True,
            blocked=False,
            state="valid",
            has_valid_cache=True,
            last_error=None,
        )
    )
    coord._async_enrich_sessions = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        return_value={RANDOM_SERIAL: [{"session_id": "fresh", "energy_kwh": 2.5}]}
    )
    coord._schedule_session_enrichment = MagicMock()  # type: ignore[assignment]  # noqa: SLF001

    data = await coord._async_update_data()

    assert data[RANDOM_SERIAL]["energy_today_sessions"] == [
        {"session_id": "cached", "energy_kwh": 1.0}
    ]
    coord._async_enrich_sessions.assert_not_awaited()  # type: ignore[attr-defined]
    coord._schedule_session_enrichment.assert_called_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_missing_session_cache_age_refreshes_active_session_inline(
    hass, monkeypatch
) -> None:
    await hass.config.async_set_time_zone("UTC")
    coord = _make_coordinator(hass, monkeypatch)
    coord._has_successful_refresh = True  # noqa: SLF001
    coord.data = {RANDOM_SERIAL: {"display_name": "Garage EV"}}

    now_local = datetime(2025, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: now_local)

    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": RANDOM_SERIAL,
                    "name": "Garage EV",
                    "connectors": [{}],
                    "session_d": {},
                    "sch_d": {},
                    "charging": True,
                    "pluggedIn": True,
                }
            ]
        }
    )
    coord.client.summary_v2 = AsyncMock(
        return_value=[{"serialNumber": RANDOM_SERIAL, "displayName": "Garage EV"}]
    )
    coord.client.charge_mode = AsyncMock(return_value=None)
    coord.session_history.get_cache_view = MagicMock(  # type: ignore[assignment]
        return_value=SessionCacheView(
            sessions=[{"session_id": "cached", "energy_kwh": 1.0}],
            cache_age=None,
            needs_refresh=True,
            blocked=False,
            state="valid",
            has_valid_cache=True,
            last_error=None,
        )
    )
    coord._async_enrich_sessions = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        return_value={RANDOM_SERIAL: [{"session_id": "fresh", "energy_kwh": 2.5}]}
    )
    coord._schedule_session_enrichment = MagicMock()  # type: ignore[assignment]  # noqa: SLF001

    data = await coord._async_update_data()

    assert data[RANDOM_SERIAL]["energy_today_sessions"] == [
        {"session_id": "fresh", "energy_kwh": 2.5}
    ]
    coord._async_enrich_sessions.assert_awaited_once()  # type: ignore[attr-defined]
    coord._schedule_session_enrichment.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_first_refresh_missing_session_cache_age_defers_in_background(
    hass, monkeypatch
) -> None:
    await hass.config.async_set_time_zone("UTC")
    coord = _make_coordinator(hass, monkeypatch)
    coord.data = {RANDOM_SERIAL: {"display_name": "Garage EV"}}

    now_local = datetime(2025, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: now_local)

    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": RANDOM_SERIAL,
                    "name": "Garage EV",
                    "connectors": [{}],
                    "session_d": {},
                    "sch_d": {},
                    "charging": False,
                }
            ]
        }
    )
    coord.client.summary_v2 = AsyncMock(
        return_value=[{"serialNumber": RANDOM_SERIAL, "displayName": "Garage EV"}]
    )
    coord.client.charge_mode = AsyncMock(return_value=None)
    coord.session_history.get_cache_view = MagicMock(  # type: ignore[assignment]
        return_value=SessionCacheView(
            sessions=[{"session_id": "cached", "energy_kwh": 1.0}],
            cache_age=None,
            needs_refresh=True,
            blocked=False,
            state="valid",
            has_valid_cache=True,
            last_error=None,
        )
    )
    coord._async_enrich_sessions = AsyncMock(return_value={})  # type: ignore[assignment]  # noqa: SLF001
    coord._schedule_session_enrichment = MagicMock()  # type: ignore[assignment]  # noqa: SLF001

    data = await coord._async_update_data()

    assert data[RANDOM_SERIAL]["energy_today_sessions"] == [
        {"session_id": "cached", "energy_kwh": 1.0}
    ]
    coord._async_enrich_sessions.assert_not_awaited()  # type: ignore[attr-defined]
    coord._schedule_session_enrichment.assert_called_once()  # type: ignore[attr-defined]


def test_session_history_adaptive_ttl_helpers(hass, monkeypatch) -> None:
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = _make_coordinator(hass, monkeypatch)
    monkeypatch.setattr(time, "time", lambda: 1_000.0)
    original_coerce_optional_int = coord_mod.helper_coerce_optional_int

    class _BadAge:
        def __float__(self) -> float:
            raise ValueError("boom")

    def _coerce_optional_int(value):
        if value == "bad":
            return _BadAge()
        return original_coerce_optional_int(value)

    monkeypatch.setattr(coord_mod, "helper_coerce_optional_int", _coerce_optional_int)

    assert coord._session_history_soft_ttl({"charging": True}) == 120.0  # noqa: SLF001
    assert coord._session_history_hard_ttl({"charging": True}) == 600.0  # noqa: SLF001
    assert (
        coord._session_history_soft_ttl({"charging": False, "session_end": 950})
        == 300.0
    )  # noqa: SLF001
    assert (
        coord._session_history_soft_ttl({"charging": False, "session_end": "bad"})
        == 600.0
    )  # noqa: SLF001
    assert coord._session_history_hard_ttl({"charging": False}) == 900.0  # noqa: SLF001


@pytest.mark.asyncio
async def test_first_refresh_soft_fails_optional_evse_status_endpoint(
    hass, monkeypatch
) -> None:
    from custom_components.enphase_ev import api

    coord = _make_coordinator(hass, monkeypatch)
    coord.client.status = AsyncMock(
        side_effect=api.OptionalEndpointUnavailable(
            "Invalid JSON response (status=200, endpoint=/service/evse_controller/SITE/ev_chargers/status)"
        )
    )

    await coord.async_refresh()

    assert coord.last_update_success is True
    assert coord.data == {}
    assert coord._has_successful_refresh is True  # noqa: SLF001
    assert coord.payload_using_stale is False
    assert coord._status_charger_data_authoritative is False  # noqa: SLF001
    assert coord._active_inventory_evse_serials() is None  # noqa: SLF001


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": {"chargers": ["bad-entry"]}},
        {"evChargerData": {"sn": "bad-entry"}},
        {"evChargerData": ["bad-entry"]},
        {"evChargerData": [{}]},
        {"evChargerData": [{"name": "bad-entry"}]},
    ],
)
async def test_first_refresh_malformed_raw_status_is_not_cleanup_authoritative(
    hass, monkeypatch, payload
) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    coord.discovery_snapshot.apply({"serial_order": ["RESTORED"]})
    coord.discovery_snapshot.schedule_save = Mock()  # type: ignore[method-assign]
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._type_device_buckets = {  # noqa: SLF001
        "envoy": {
            "count": 1,
            "devices": [{"serial_number": "GW-1"}],
        }
    }
    coord._type_device_order = ["envoy"]  # noqa: SLF001
    coord.client.status = AsyncMock(return_value=payload)

    data = await coord._async_update_data()  # noqa: SLF001
    coord.data = data

    assert data == {}
    assert coord._status_charger_data_authoritative is False  # noqa: SLF001
    assert coord._status_charger_data_serials is None  # noqa: SLF001
    assert coord._active_inventory_evse_serials() is None  # noqa: SLF001
    assert coord.iter_serials() == ["RESTORED"]


@pytest.mark.asyncio
async def test_refresh_malformed_nested_status_row_keeps_cleanup_authority(
    hass, monkeypatch
) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    coord.discovery_snapshot.apply({"serial_order": ["RESTORED", "ACTIVE"]})
    coord.discovery_snapshot.schedule_save = Mock()  # type: ignore[method-assign]
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._type_device_buckets = {  # noqa: SLF001
        "iqevse": {
            "count": 2,
            "devices": [
                {"serial_number": "RESTORED"},
                {"serial_number": "ACTIVE"},
            ],
        }
    }
    coord._type_device_order = ["iqevse"]  # noqa: SLF001
    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": "ACTIVE",
                    "connectors": "bad-connectors",
                    "sch_d": {"info": {"type": "scheduled"}},
                    "session_d": "bad-session",
                }
            ],
            "ts": 0,
        }
    )

    data = await coord._async_update_data()  # noqa: SLF001
    coord.data = data

    assert list(data) == ["ACTIVE"]
    assert coord._status_charger_data_authoritative is True  # noqa: SLF001
    assert coord._status_charger_data_serials == ["ACTIVE"]  # noqa: SLF001
    assert coord._active_inventory_evse_serials() == ["ACTIVE"]  # noqa: SLF001
    assert data["ACTIVE"]["connector_reason"] is None
    assert data["ACTIVE"]["schedule_status"] is None
    assert data["ACTIVE"]["schedule_type"] == "scheduled"
    assert data["ACTIVE"]["session_energy_raw"] is None


@pytest.mark.asyncio
async def test_first_refresh_empty_evchargerdata_is_cleanup_authoritative(
    hass, monkeypatch
) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    coord.discovery_snapshot.apply({"serial_order": ["RESTORED"]})
    coord.discovery_snapshot.schedule_save = Mock()  # type: ignore[method-assign]
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._type_device_buckets = {  # noqa: SLF001
        "envoy": {
            "count": 1,
            "devices": [{"serial_number": "GW-1"}],
        }
    }
    coord._type_device_order = ["envoy"]  # noqa: SLF001
    coord.client.status = AsyncMock(return_value={"evChargerData": [], "ts": 0})

    data = await coord._async_update_data()  # noqa: SLF001
    coord.data = data

    assert data == {}
    assert coord._status_charger_data_authoritative is True  # noqa: SLF001
    assert coord._status_charger_data_serials == []  # noqa: SLF001
    assert coord._active_inventory_evse_serials() == []  # noqa: SLF001
    assert coord.iter_serials() == []


@pytest.mark.asyncio
async def test_empty_evchargerdata_keeps_status_refresh_after_serials_pruned(
    hass, monkeypatch
) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    coord.discovery_snapshot.apply({"serial_order": ["RESTORED"]})
    coord.discovery_snapshot.schedule_save = Mock()  # type: ignore[method-assign]
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._type_device_buckets = {  # noqa: SLF001
        "iqevse": {
            "count": 1,
            "devices": [{"serial_number": "RESTORED"}],
        }
    }
    coord._type_device_order = ["iqevse"]  # noqa: SLF001
    coord.client.status = AsyncMock(return_value={"evChargerData": [], "ts": 0})

    data = await coord._async_update_data()  # noqa: SLF001
    coord.data = data

    assert data == {}
    assert coord.serials == set()
    assert coord.iter_serials() == []

    coord.client.status.reset_mock()

    data = await coord._async_update_data()  # noqa: SLF001
    coord.data = data

    assert data == {}
    coord.client.status.assert_awaited_once()
    assert coord._status_charger_data_authoritative is True  # noqa: SLF001
    assert coord._status_charger_data_serials == []  # noqa: SLF001
    assert coord._active_inventory_evse_serials() == []  # noqa: SLF001
    assert coord.iter_serials() == []


@pytest.mark.asyncio
async def test_refresh_topology_uses_fresh_status_serial_cache_before_data_assignment(
    hass, monkeypatch
) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    coord.data = {
        "OLD": {"sn": "OLD"},
        "ACTIVE": {"sn": "ACTIVE"},
    }
    coord.discovery_snapshot.apply({"serial_order": ["OLD", "ACTIVE"]})
    coord.discovery_snapshot.schedule_save = Mock()  # type: ignore[method-assign]
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._type_device_buckets = {  # noqa: SLF001
        "iqevse": {
            "count": 2,
            "devices": [
                {"serial_number": "OLD"},
                {"serial_number": "ACTIVE"},
            ],
        }
    }
    coord._type_device_order = ["iqevse"]  # noqa: SLF001
    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": "ACTIVE",
                    "connectors": [{}],
                }
            ],
            "ts": 0,
        }
    )
    coord._async_resolve_post_status_evse_enrichments = AsyncMock(  # type: ignore[method-assign]  # noqa: SLF001
        return_value=({}, {}, {}, {})
    )
    observed_active_serials: list[list[str] | None] = []
    observed_data_serials: list[set[str]] = []

    def _finish_refresh_pipeline(_context) -> None:
        observed_active_serials.append(
            coord._active_inventory_evse_serials()
        )  # noqa: SLF001
        observed_data_serials.append(set(coord.data))

    coord._finish_refresh_pipeline = Mock(  # type: ignore[method-assign]  # noqa: SLF001
        side_effect=_finish_refresh_pipeline
    )

    data = await coord._async_update_data()  # noqa: SLF001

    assert set(data) == {"ACTIVE"}
    assert observed_data_serials == [{"OLD", "ACTIVE"}]
    assert observed_active_serials == [["ACTIVE"]]
    assert coord._status_charger_data_serials == ["ACTIVE"]  # noqa: SLF001


@pytest.mark.asyncio
async def test_first_refresh_runs_storm_guard_startup_calls_and_defers_warmup_only_calls(
    hass, monkeypatch, config_entry
) -> None:
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev import coordinator as coord_mod

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
        CONF_SITE_ONLY: False,
    }

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        coord_mod,
        "async_call_later",
        lambda *_args, **_kwargs: (lambda: None),
    )

    class DummyClient:
        async def status(self):
            return {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "connectors": [{}],
                        "session_d": {},
                        "sch_d": {},
                        "charging": False,
                    }
                ],
                "ts": 1_700_000_000,
            }

        async def summary_v2(self):
            return [{"serialNumber": RANDOM_SERIAL, "displayName": "Garage EV"}]

    coord = EnphaseCoordinator(hass, cfg, config_entry=config_entry)
    coord.client = DummyClient()

    deferred_methods = {
        "_async_refresh_battery_site_settings": AsyncMock(),
        "_async_refresh_battery_status": AsyncMock(),
        "_async_refresh_battery_backup_history": AsyncMock(),
        "_async_refresh_battery_settings": AsyncMock(),
        "_async_refresh_battery_schedules": AsyncMock(),
        "_async_refresh_storm_guard_profile": AsyncMock(),
        "_async_refresh_storm_alert": AsyncMock(),
        "_async_refresh_grid_control_check": AsyncMock(),
        "_async_refresh_devices_inventory": AsyncMock(),
        "_async_refresh_dry_contact_settings": AsyncMock(),
        "_async_refresh_hems_devices": AsyncMock(),
        "_async_refresh_inverters": AsyncMock(),
        "_async_refresh_current_power_consumption": AsyncMock(),
        "_async_refresh_heatpump_power": AsyncMock(),
        "_async_resolve_charge_modes": AsyncMock(return_value={}),
        "_async_resolve_green_battery_settings": AsyncMock(return_value={}),
        "_async_resolve_auth_settings": AsyncMock(return_value={}),
    }
    for name, mock in deferred_methods.items():
        owner = coord
        attr = name
        if name in {
            "_async_refresh_battery_status",
            "_async_refresh_grid_control_check",
        }:
            owner = coord.battery_runtime
            attr = name.removeprefix("_")
        elif name in {
            "_async_refresh_devices_inventory",
            "_async_refresh_hems_devices",
        }:
            owner = coord.inventory_runtime
        elif name in {
            "_async_resolve_charge_modes",
            "_async_resolve_green_battery_settings",
            "_async_resolve_auth_settings",
        }:
            owner = coord.evse_runtime
            attr = name.removeprefix("_")
        monkeypatch.setattr(owner, attr, mock)

    monkeypatch.setattr(coord.energy, "_async_refresh_site_energy", AsyncMock())
    monkeypatch.setattr(coord.evse_timeseries, "async_refresh", AsyncMock())

    await coord.async_refresh()

    deferred_methods["_async_refresh_battery_site_settings"].assert_awaited_once()
    deferred_methods["_async_refresh_storm_guard_profile"].assert_awaited_once()
    for name, mock in deferred_methods.items():
        if name in {
            "_async_refresh_battery_site_settings",
            "_async_refresh_storm_guard_profile",
        }:
            continue
        mock.assert_not_awaited()
    coord.energy._async_refresh_site_energy.assert_not_awaited()
    coord.evse_timeseries.async_refresh.assert_not_awaited()
    assert "status_s" in coord.bootstrap_phase_timings
    assert "summary_s" in coord.bootstrap_phase_timings
    assert "site_energy_s" not in coord.bootstrap_phase_timings


@pytest.mark.asyncio
async def test_first_refresh_overlaps_startup_followups_with_status(
    hass, monkeypatch
) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    status_started = asyncio.Event()
    followup_started = asyncio.Event()

    async def _status():
        status_started.set()
        await followup_started.wait()
        return {
            "evChargerData": [
                {
                    "sn": RANDOM_SERIAL,
                    "name": "Garage EV",
                    "connectors": [{}],
                    "session_d": {},
                    "sch_d": {},
                    "charging": False,
                }
            ],
            "ts": 1_700_000_000,
        }

    async def _run_first_refresh_calls(phase_timings, *, calls, **_kwargs) -> None:
        followup_started.set()
        await status_started.wait()
        for timing_key, *_rest in calls:
            phase_timings[timing_key] = 0.001

    coord.client.status = AsyncMock(side_effect=_status)
    coord.client.summary_v2 = AsyncMock(
        return_value=[{"serialNumber": RANDOM_SERIAL, "displayName": "Garage EV"}]
    )
    coord.refresh_runner.async_run_refresh_calls = AsyncMock(
        side_effect=_run_first_refresh_calls
    )

    await asyncio.wait_for(coord.async_refresh(), timeout=1)

    assert followup_started.is_set()
    assert coord.refresh_runner.async_run_refresh_calls.await_count == 1
    assert "status_s" in coord.bootstrap_phase_timings
    assert "battery_site_settings_s" in coord.bootstrap_phase_timings
    assert "tariff_s" not in coord.bootstrap_phase_timings


@pytest.mark.asyncio
async def test_first_refresh_populates_storm_guard_state_before_entities(
    hass, monkeypatch, config_entry
) -> None:
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.switch import StormGuardSwitch

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
        CONF_SITE_ONLY: False,
    }

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        coord_mod,
        "async_call_later",
        lambda *_args, **_kwargs: (lambda: None),
    )

    class DummyClient:
        async def status(self):
            return {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "connectors": [{}],
                        "session_d": {},
                        "sch_d": {},
                        "charging": False,
                    }
                ],
                "ts": 1_700_000_000,
            }

        async def summary_v2(self):
            return [{"serialNumber": RANDOM_SERIAL, "displayName": "Garage EV"}]

        async def battery_site_settings(self):
            return {
                "data": {
                    "showStormGuard": True,
                    "hasEncharge": True,
                    "userDetails": {"isOwner": True, "isInstaller": False},
                }
            }

        async def storm_guard_profile(self, **_kwargs):
            return {
                "data": {
                    "stormGuardState": "active",
                    "evseStormEnabled": True,
                }
            }

    coord = EnphaseCoordinator(hass, cfg, config_entry=config_entry)
    coord.client = DummyClient()
    coord.tariff_runtime.async_refresh = AsyncMock()

    await coord.async_refresh()

    assert coord.data[RANDOM_SERIAL]["storm_guard_state"] == "enabled"
    assert coord.data[RANDOM_SERIAL]["storm_evse_enabled"] is True
    assert StormGuardSwitch(coord).available is True


def test_snapshot_helpers_and_discovery_capture_edge_paths(hass, monkeypatch) -> None:
    coord = _make_coordinator(hass, monkeypatch)

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    stamp = datetime(2026, 1, 2, tzinfo=timezone.utc)
    assert coord._snapshot_compatible_value(stamp) == stamp.isoformat()  # noqa: SLF001
    assert coord._snapshot_compatible_value(
        {BadStr(): "skip", "ok": 1}
    ) == {  # noqa: SLF001
        "ok": 1
    }
    assert coord._snapshot_compatible_value(["a", 2]) == ["a", 2]  # noqa: SLF001
    assert coord._snapshot_compatible_value(("a", 2)) == ["a", 2]  # noqa: SLF001
    assert coord._snapshot_compatible_value(BadStr()) is None  # noqa: SLF001

    assert coord._snapshot_bool(None) is None  # noqa: SLF001
    assert coord._snapshot_bool(1) is True  # noqa: SLF001
    assert coord._snapshot_bool(0.0) is False  # noqa: SLF001
    assert coord._snapshot_bool("enabled") is True  # noqa: SLF001
    assert coord._snapshot_bool("disabled") is False  # noqa: SLF001
    assert coord._snapshot_bool("maybe") is None  # noqa: SLF001

    assert coord.discovery_snapshot.site_energy_channel_known(BadStr()) is False
    assert coord.discovery_snapshot.site_energy_channel_known(" ") is False

    coord.energy = None  # type: ignore[assignment]
    assert coord.discovery_snapshot.live_site_energy_channels() == set()

    coord.energy = SimpleNamespace(
        site_energy={BadStr(): 1, "grid_import": 1},
        site_energy_meta={
            "bucket_lengths": {
                BadStr(): 1,
                "": 1,
                "heatpump": 1,
                "water_heater": 1,
                "evse": 1,
                "solar_production": 1,
                "consumption": 1,
                "grid_export": 1,
                "battery_charge": 1,
                "battery_discharge": 1,
                "custom_flow": "truthy",
                "skip_empty": None,
                "ignored": 0,
            }
        },
    )
    channels = coord.discovery_snapshot.live_site_energy_channels()
    assert channels >= {
        "grid_import",
        "heat_pump",
        "water_heater",
        "evse_charging",
        "solar_production",
        "consumption",
        "grid_export",
        "battery_charge",
        "battery_discharge",
        "custom_flow",
    }

    coord.inventory_runtime._hems_group_members = lambda *_args: [  # type: ignore[assignment]  # noqa: SLF001
        None,
        {"device-type": None},
        {"device-type": BadStr()},
        {"device-type": "IQ_GATEWAY"},
        {"device_type": "IQ_ENERGY_ROUTER", "device-uid": "LIVE-1"},
    ]
    live_records = coord.discovery_snapshot.gateway_iq_energy_router_records()
    assert live_records == [{"device_type": "IQ_ENERGY_ROUTER", "device-uid": "LIVE-1"}]

    coord.inventory_runtime._hems_group_members = lambda *_args: []  # type: ignore[assignment]  # noqa: SLF001
    coord.energy = SimpleNamespace(
        site_energy={}, site_energy_meta={"bucket_lengths": {}}
    )
    coord._restored_site_energy_channels = {"heat_pump"}  # noqa: SLF001
    coord._restored_gateway_iq_energy_router_records = [  # noqa: SLF001
        {"device-uid": "REST-1", "device-type": "IQ_ENERGY_ROUTER"}
    ]
    coord._type_device_order = ["envoy"]  # noqa: SLF001
    coord._type_device_buckets = {"envoy": {"count": 1, "devices": []}}  # noqa: SLF001
    coord._battery_storage_order = ["BAT-1"]  # noqa: SLF001
    coord._battery_storage_data = {"BAT-1": {"name": "Battery 1"}}  # noqa: SLF001
    coord._inverter_order = ["INV-1"]  # noqa: SLF001
    coord._inverter_data = {"INV-1": {"name": "Inverter 1"}}  # noqa: SLF001
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_has_enpower = False  # noqa: SLF001
    coord._heatpump_known_present = True  # noqa: SLF001
    coord._type_device_buckets["heatpump"] = {"count": 0, "devices": []}  # noqa: SLF001

    snapshot = coord.discovery_snapshot.capture()
    assert snapshot["heatpump_known_present"] is True
    assert snapshot["site_energy_channels"] == ["heat_pump"]
    assert snapshot["gateway_iq_energy_router_records"] == [
        {"device-uid": "REST-1", "device-type": "IQ_ENERGY_ROUTER"}
    ]

    coord._hems_inventory_ready = True  # noqa: SLF001
    assert coord.inventory_view.gateway_iq_energy_router_records() == []


@pytest.mark.asyncio
async def test_discovery_snapshot_restore_save_and_metrics_edge_paths(
    hass, monkeypatch
) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    from custom_components.enphase_ev.discovery_snapshot import (
        _snapshot_bool,
        _snapshot_compatible_value,
    )

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    class BadKey:
        def __str__(self) -> str:
            raise RuntimeError("key")

    assert _snapshot_compatible_value(datetime(2024, 1, 1, tzinfo=timezone.utc)) == (
        "2024-01-01T00:00:00+00:00"
    )
    assert _snapshot_compatible_value({BadKey(): "drop", "ok": BadStr()}) == {
        "ok": None
    }
    assert _snapshot_compatible_value({BadStr()}) == [None]
    assert _snapshot_bool(None) is None
    assert _snapshot_bool(0) is False
    assert _snapshot_bool("maybe") is None

    ensure_serial = Mock()
    set_buckets = Mock()
    coord._ensure_serial_tracked = ensure_serial  # type: ignore[assignment]  # noqa: SLF001
    coord.inventory_runtime._set_type_device_buckets = set_buckets  # type: ignore[assignment]  # noqa: SLF001

    coord.discovery_snapshot.apply(None)
    coord.discovery_snapshot.apply(
        {
            "serial_order": [None, BadStr(), "REST-1"],
            "type_device_buckets": {
                " ": {"devices": []},
                "envoy": {"devices": "bad", "count": "oops"},
                "bad": "bucket",
            },
            "type_device_order": ["envoy"],
            "battery_storage_order": ["", "BAT-1"],
            "battery_storage_data": {
                "": {},
                "BAT-1": {"name": "Battery"},
                "BAT-2": "bad",
            },
            "inverter_order": ["", "INV-1"],
            "inverter_data": {
                "": {},
                "INV-1": {"name": "Inverter"},
                "INV-2": "bad",
            },
            "battery_has_encharge": "enabled",
            "battery_has_enpower": "disabled",
            "heatpump_known_present": "enabled",
            "site_energy_channels": ["", "heat_pump"],
            "gateway_iq_energy_router_records": [{"device-uid": "REST-1"}, "bad"],
        }
    )

    ensure_serial.assert_not_called()
    assert coord._restored_evse_serial_order == ["REST-1"]  # noqa: SLF001
    assert coord._serial_order == ["REST-1"]  # noqa: SLF001
    assert coord.serials == {"REST-1"}
    set_buckets.assert_called_once()
    assert set_buckets.call_args.kwargs["authoritative"] is False
    assert coord._battery_storage_order == ["BAT-1"]  # noqa: SLF001
    assert coord._inverter_order == ["INV-1"]  # noqa: SLF001
    assert coord.inventory_runtime.iter_inverter_serials() == ["INV-1"]
    assert coord.inventory_runtime.inverter_data("INV-1") == {"name": "Inverter"}
    assert coord._restored_site_energy_channels == {"heat_pump"}  # noqa: SLF001
    assert coord._restored_gateway_iq_energy_router_records == [  # noqa: SLF001
        {"device-uid": "REST-1"}
    ]
    assert coord._heatpump_known_present is True  # noqa: SLF001

    coord = _make_coordinator(hass, monkeypatch)
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {"envoy": {"count": 1, "devices": [{"serial_number": "GW-1"}]}},
        ["envoy"],
        authoritative=False,
    )
    assert coord._devices_inventory_ready is True  # noqa: SLF001

    coord._discovery_snapshot_loaded = True  # noqa: SLF001
    coord.discovery_snapshot._store = SimpleNamespace(  # type: ignore[assignment]  # noqa: SLF001
        async_load=AsyncMock(side_effect=AssertionError("no load"))
    )
    await coord.discovery_snapshot.async_restore_state()

    coord._discovery_snapshot_loaded = False  # noqa: SLF001
    coord.discovery_snapshot._store = SimpleNamespace(  # type: ignore[assignment]  # noqa: SLF001
        async_load=AsyncMock(side_effect=RuntimeError("boom"))
    )
    await coord.discovery_snapshot.async_restore_state()
    assert coord._devices_inventory_ready is False  # noqa: SLF001
    assert coord._hems_inventory_ready is False  # noqa: SLF001
    assert coord._site_energy_discovery_ready is False  # noqa: SLF001

    coord.discovery_snapshot.capture = Mock(return_value={"serial_order": []})  # type: ignore[assignment]
    coord.discovery_snapshot._store = SimpleNamespace(  # type: ignore[assignment]  # noqa: SLF001
        async_save=AsyncMock(side_effect=RuntimeError("boom"))
    )
    await coord.discovery_snapshot.async_save()
    coord.discovery_snapshot._store.async_save.assert_awaited_once_with(  # type: ignore[attr-defined]
        {"serial_order": []}
    )

    coord.discovery_snapshot._store = SimpleNamespace(  # type: ignore[assignment]  # noqa: SLF001
        async_save=AsyncMock()
    )
    await coord.discovery_snapshot.async_save()
    coord.discovery_snapshot._store.async_save.assert_awaited_once_with(  # type: ignore[attr-defined]
        {"serial_order": []}
    )
    await coord.discovery_snapshot.async_save()
    coord.discovery_snapshot._store.async_save.assert_awaited_once()

    create_calls: list[object] = []

    def _capture_create_task(coro, *, name=None):
        create_calls.append(coro)
        coro.close()
        return None

    object.__setattr__(coord.hass, "async_create_task", _capture_create_task)
    scheduled: list = []

    def _capture_call_later(_hass, _delay, callback):
        scheduled.append(callback)
        return lambda: None

    from custom_components.enphase_ev import discovery_snapshot as snapshot_mod

    monkeypatch.setattr(snapshot_mod, "async_call_later", _capture_call_later)
    coord._discovery_snapshot_save_cancel = lambda: None  # type: ignore[assignment]  # noqa: SLF001
    coord.discovery_snapshot.capture = Mock(return_value={"serial_order": []})  # type: ignore[assignment]
    coord.discovery_snapshot.schedule_save()
    assert scheduled == []
    assert coord._discovery_snapshot_pending is False  # noqa: SLF001

    coord.discovery_snapshot.capture = Mock(return_value={"serial_order": ["NEW"]})  # type: ignore[assignment]
    coord.discovery_snapshot.schedule_save()
    assert scheduled == []
    assert coord._discovery_snapshot_pending is True  # noqa: SLF001

    coord._discovery_snapshot_save_cancel = None  # noqa: SLF001
    coord.discovery_snapshot.schedule_save()
    assert len(scheduled) == 1
    coord._discovery_snapshot_pending = False  # noqa: SLF001
    scheduled[0](datetime.now(tz=timezone.utc))
    assert create_calls == []

    coord._discovery_snapshot_pending = True  # noqa: SLF001
    scheduled[0](datetime.now(tz=timezone.utc))
    assert len(create_calls) == 1
    cancelled: list[bool] = []
    coord._discovery_snapshot_save_cancel = lambda: cancelled.append(True)  # type: ignore[assignment]  # noqa: SLF001
    coord.discovery_snapshot.cancel_pending_save()
    assert cancelled == [True]
    assert coord._discovery_snapshot_save_cancel is None  # noqa: SLF001

    coord.energy = None  # type: ignore[assignment]
    coord.discovery_snapshot.sync_site_energy_discovery_state()
    coord.energy = SimpleNamespace(_site_energy_cache_ts=1)
    coord.discovery_snapshot.sync_site_energy_discovery_state()
    assert coord._site_energy_discovery_ready is True  # noqa: SLF001

    coord._system_dashboard_type_summaries = {"envoy": {}}  # noqa: SLF001
    coord._async_refresh_system_dashboard = AsyncMock()  # type: ignore[assignment]  # noqa: SLF001
    await coord.async_ensure_system_dashboard_diagnostics()
    coord._async_refresh_system_dashboard.assert_not_awaited()

    coord._system_dashboard_type_summaries = {}  # noqa: SLF001
    coord._system_dashboard_hierarchy_summary = {}  # noqa: SLF001
    await coord.async_ensure_system_dashboard_diagnostics()
    coord._async_refresh_system_dashboard.assert_awaited_once_with(force=True)

    coord._warmup_in_progress = True  # noqa: SLF001
    coord._warmup_last_error = "boom"  # noqa: SLF001
    coord._bootstrap_phase_timings = {"status_s": 0.1}  # noqa: SLF001
    coord._warmup_phase_timings = {"discovery_s": 0.2}  # noqa: SLF001
    metrics = coord.collect_site_metrics()
    assert metrics["bootstrap_phase_timings"] == {"status_s": 0.1}
    assert metrics["warmup_phase_timings"] == {"discovery_s": 0.2}
    assert metrics["warmup_in_progress"] is True
    assert metrics["warmup_last_error"] == "boom"


@pytest.mark.asyncio
async def test_startup_warmup_runner_and_task_edge_paths(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    coord.async_set_updated_data = Mock(side_effect=RuntimeError("publish"))  # type: ignore[assignment]
    coord.discovery_snapshot.schedule_save = Mock()  # type: ignore[assignment]
    coord._async_refresh_battery_site_settings = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        return_value=None
    )

    await coord.refresh_runner.async_startup_warmup_runner()

    assert coord._warmup_last_error == "publish"  # noqa: SLF001
    assert coord._warmup_in_progress is False  # noqa: SLF001
    assert "discovery_s" in coord._warmup_phase_timings  # noqa: SLF001
    coord.discovery_snapshot.schedule_save.assert_called_once()  # type: ignore[attr-defined]

    coord = coordinator_factory()
    coord.discovery_snapshot.schedule_save = Mock()  # type: ignore[assignment]
    coord._async_refresh_battery_site_settings = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        side_effect=asyncio.CancelledError()
    )
    with pytest.raises(asyncio.CancelledError):
        await coord.refresh_runner.async_startup_warmup_runner()
    assert coord._warmup_in_progress is False  # noqa: SLF001
    coord.discovery_snapshot.schedule_save.assert_called_once()  # type: ignore[attr-defined]

    coord = coordinator_factory()
    coord.discovery_snapshot.schedule_save = Mock()  # type: ignore[assignment]
    coord._async_refresh_heatpump_power = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        side_effect=aiohttp.ClientError("heatpump")
    )
    await coord.refresh_runner.async_startup_warmup_runner()
    assert "heatpump_power_s" in coord._warmup_phase_timings  # noqa: SLF001
    coord.discovery_snapshot.schedule_save.assert_called_once()  # type: ignore[attr-defined]

    coord = coordinator_factory()
    coord.discovery_snapshot.schedule_save = Mock()  # type: ignore[assignment]
    coord._async_refresh_heatpump_runtime_state = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        side_effect=aiohttp.ClientError("runtime")
    )
    coord._async_refresh_heatpump_daily_consumption = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        side_effect=aiohttp.ClientError("daily")
    )
    await coord.refresh_runner.async_startup_warmup_runner()
    assert "heatpump_runtime_s" in coord._warmup_phase_timings  # noqa: SLF001
    assert "heatpump_daily_s" in coord._warmup_phase_timings  # noqa: SLF001
    coord.discovery_snapshot.schedule_save.assert_called_once()  # type: ignore[attr-defined]

    coord = coordinator_factory(data={})
    coord.data = {}
    coord.discovery_snapshot.schedule_save = Mock()  # type: ignore[assignment]
    coord.async_set_updated_data = Mock()  # type: ignore[assignment]
    coord.tariff_runtime.async_refresh = AsyncMock()  # type: ignore[method-assign]
    coord.tariff_last_refresh_utc = datetime.now(timezone.utc)

    await coord.refresh_runner.async_startup_warmup_runner()

    coord.async_set_updated_data.assert_called_once_with({})  # type: ignore[attr-defined]
    coord.discovery_snapshot.schedule_save.assert_called_once()  # type: ignore[attr-defined]

    coord = coordinator_factory(data={})
    coord.data = {}
    coord.energy.site_energy = {"production": []}
    assert coord.refresh_runner._warmup_site_state_available() is True  # noqa: SLF001

    coord = coordinator_factory()
    coord.discovery_snapshot.schedule_save = Mock()  # type: ignore[assignment]
    coord._async_refresh_heatpump_runtime_state = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        side_effect=asyncio.CancelledError()
    )
    with pytest.raises(asyncio.CancelledError):
        await coord.refresh_runner.async_startup_warmup_runner()
    assert coord._warmup_in_progress is False  # noqa: SLF001
    coord.discovery_snapshot.schedule_save.assert_called_once()  # type: ignore[attr-defined]

    coord = coordinator_factory()
    coord.discovery_snapshot.schedule_save = Mock()  # type: ignore[assignment]
    coord._async_refresh_heatpump_daily_consumption = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        side_effect=asyncio.CancelledError()
    )
    with pytest.raises(asyncio.CancelledError):
        await coord.refresh_runner.async_startup_warmup_runner()
    assert coord._warmup_in_progress is False  # noqa: SLF001
    coord.discovery_snapshot.schedule_save.assert_called_once()  # type: ignore[attr-defined]

    coord = coordinator_factory()
    coord.discovery_snapshot.schedule_save = Mock()  # type: ignore[assignment]
    coord._async_refresh_heatpump_power = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        side_effect=asyncio.CancelledError()
    )
    with pytest.raises(asyncio.CancelledError):
        await coord.refresh_runner.async_startup_warmup_runner()
    assert coord._warmup_in_progress is False  # noqa: SLF001
    coord.discovery_snapshot.schedule_save.assert_called_once()  # type: ignore[attr-defined]

    coord = coordinator_factory()
    coord._warmup_task = SimpleNamespace(done=lambda: False)  # type: ignore[assignment]  # noqa: SLF001
    create_calls: list[object] = []

    async def _runner() -> None:
        return None

    coord.refresh_runner.async_startup_warmup_runner = _runner  # type: ignore[method-assign]

    def _create_task(coro, name=None):
        create_calls.append(name)
        coro.close()
        if name is not None:
            raise TypeError("no name support")
        return "task"

    object.__setattr__(coord.hass, "async_create_task", _create_task)
    await coord.async_start_startup_warmup()
    assert create_calls == []

    coord._warmup_task = None  # noqa: SLF001
    await coord.async_start_startup_warmup()
    assert create_calls == [f"{DOMAIN}_warmup_site", None]
    assert coord.site_id not in str(create_calls[0])
    assert coord._warmup_task == "task"  # noqa: SLF001


@pytest.mark.asyncio
async def test_startup_warmup_helper_refreshes_cover_fallback_and_merge_paths(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.data = {RANDOM_SERIAL: {"name": "Garage EV"}}
    set_updated = Mock()
    coord.async_set_updated_data = set_updated  # type: ignore[assignment]

    from custom_components.enphase_ev import refresh_runner as runner_mod

    monkeypatch.setattr(
        runner_mod.dt_util, "as_local", Mock(side_effect=RuntimeError("boom"))
    )

    coord.energy._async_refresh_site_energy = AsyncMock()  # noqa: SLF001
    coord._sync_site_energy_issue = Mock()  # type: ignore[assignment]  # noqa: SLF001
    await coord.refresh_runner.async_refresh_site_energy_for_warmup()
    coord.energy._async_refresh_site_energy.assert_awaited_once()

    coord.evse_timeseries.async_refresh = AsyncMock()
    coord.evse_timeseries.merge_charger_payloads = Mock(
        side_effect=lambda payload, day_local=None: payload[RANDOM_SERIAL].update(
            {"timeseries": True}
        )
    )
    await coord.refresh_runner.async_refresh_evse_timeseries_for_warmup()
    merged_timeseries = set_updated.call_args_list[-1].args[0]
    assert merged_timeseries[RANDOM_SERIAL]["timeseries"] is True

    coord._async_enrich_sessions = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        return_value={
            RANDOM_SERIAL: [{"energy_kwh": 1.5}],
            "missing": [{"energy_kwh": 0.2}],
        }
    )
    coord._sum_session_energy = Mock(return_value=1.5)  # type: ignore[assignment]  # noqa: SLF001
    coord._sync_session_history_issue = Mock()  # type: ignore[assignment]  # noqa: SLF001
    await coord.refresh_runner.async_refresh_session_state_for_warmup()
    merged_sessions = set_updated.call_args_list[-1].args[0]
    assert merged_sessions[RANDOM_SERIAL]["energy_today_sessions"] == [
        {"energy_kwh": 1.5}
    ]
    assert merged_sessions[RANDOM_SERIAL]["energy_today_sessions_kwh"] == 1.5
    coord._sync_session_history_issue.assert_called_once()

    coord._async_enrich_sessions = AsyncMock(return_value={})  # type: ignore[assignment]  # noqa: SLF001
    await coord.refresh_runner.async_refresh_session_state_for_warmup()
    coord.data = None
    await coord.refresh_runner.async_refresh_session_state_for_warmup()
    coord.data = {RANDOM_SERIAL: {"name": "Garage EV"}}

    coord.iter_serials = lambda: [RANDOM_SERIAL, "SECONDARY"]  # type: ignore[assignment]
    coord.evse_runtime.async_resolve_charge_modes = AsyncMock(  # type: ignore[method-assign]  # noqa: SLF001
        return_value={RANDOM_SERIAL: ChargeModeResolution("SCHEDULED", "cache_backoff")}
    )
    coord.evse_runtime.async_resolve_green_battery_settings = AsyncMock(  # type: ignore[method-assign]  # noqa: SLF001
        return_value={RANDOM_SERIAL: (True, True)}
    )
    coord.evse_runtime.async_resolve_auth_settings = AsyncMock(  # type: ignore[method-assign]  # noqa: SLF001
        return_value={RANDOM_SERIAL: (True, False, True, True)}
    )
    coord.evse_runtime.async_resolve_charger_config = AsyncMock(  # type: ignore[method-assign]  # noqa: SLF001
        return_value={
            RANDOM_SERIAL: {
                "phase_switch_config": "auto",
                "DefaultChargeLevel": None,
            }
        }
    )
    await coord.refresh_runner.async_refresh_secondary_evse_state_for_warmup()
    merged_secondary = set_updated.call_args_list[-1].args[0]
    assert merged_secondary[RANDOM_SERIAL]["charge_mode_pref"] == "SCHEDULED"
    assert merged_secondary[RANDOM_SERIAL]["charge_mode_pref_source"] == "cache_backoff"
    assert merged_secondary[RANDOM_SERIAL]["green_battery_enabled"] is True
    assert merged_secondary[RANDOM_SERIAL]["app_auth_supported"] is True
    assert merged_secondary[RANDOM_SERIAL]["rfid_auth_supported"] is True
    assert merged_secondary[RANDOM_SERIAL]["auth_required"] is True
    assert merged_secondary[RANDOM_SERIAL]["phase_switch_config"] == "auto"
    assert merged_secondary[RANDOM_SERIAL]["default_charge_level_supported"] is True
    assert "default_charge_level" in merged_secondary[RANDOM_SERIAL]
    assert merged_secondary[RANDOM_SERIAL]["default_charge_level"] is None

    coord.evse_runtime.async_resolve_charger_config = AsyncMock(  # type: ignore[method-assign]  # noqa: SLF001
        return_value={RANDOM_SERIAL: {"phase_switch_config": "auto"}}
    )
    await coord.refresh_runner.async_refresh_secondary_evse_state_for_warmup()
    merged_secondary = set_updated.call_args_list[-1].args[0]
    assert merged_secondary[RANDOM_SERIAL]["default_charge_level_supported"] is False
    assert (
        merged_secondary[RANDOM_SERIAL]["default_charge_level_supported_source"]
        == "charger_config"
    )

    coord.evse_runtime.async_resolve_charger_config = AsyncMock(  # type: ignore[method-assign]  # noqa: SLF001
        return_value={RANDOM_SERIAL: {}}
    )
    await coord.refresh_runner.async_refresh_secondary_evse_state_for_warmup()
    merged_secondary = set_updated.call_args_list[-1].args[0]
    assert merged_secondary[RANDOM_SERIAL]["default_charge_level_supported"] is False
    assert (
        merged_secondary[RANDOM_SERIAL]["default_charge_level_supported_source"]
        == "charger_config"
    )

    coord.iter_serials = lambda: [""]  # type: ignore[assignment]
    await coord.refresh_runner.async_refresh_secondary_evse_state_for_warmup()
    coord.data = None
    await coord.refresh_runner.async_refresh_secondary_evse_state_for_warmup()


@pytest.mark.asyncio
async def test_charge_mode_lookup_skipped_when_embedded(
    hass, monkeypatch, config_entry
):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )

    charge_mode_called = False

    async def fake_resolve(self, serials):
        nonlocal charge_mode_called
        charge_mode_called = True
        return {}

    monkeypatch.setattr(
        coord_mod.EvseRuntime,
        "async_resolve_charge_modes",
        fake_resolve,
        raising=False,
    )

    class DummyClient:
        async def status(self):
            return {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "chargeMode": "IMMEDIATE",
                        "connectors": [{}],
                        "session_d": {},
                        "sch_d": {},
                        "charging": False,
                    }
                ]
            }

        async def summary_v2(self):
            return []

        async def charge_mode(self, sn: str):
            return "SCHEDULED"

        async def session_history(self, *args, **kwargs):
            return {"data": {"result": [], "hasMore": False}}

    coord = EnphaseCoordinator(hass, cfg, config_entry=config_entry)
    coord.client = DummyClient()

    await coord.async_refresh()

    assert not charge_mode_called


@pytest.mark.asyncio
async def test_restore_discovery_state_applies_snapshot_topology(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord._hems_inventory_ready = False  # noqa: SLF001
    coord.discovery_snapshot._store = SimpleNamespace(  # type: ignore[assignment]  # noqa: SLF001
        async_load=AsyncMock(
            return_value={
                "serial_order": [RANDOM_SERIAL, "RESTORED123"],
                "type_device_order": ["envoy", "heatpump", "microinverter"],
                "type_device_buckets": {
                    "envoy": {
                        "type_label": "Gateway",
                        "count": 1,
                        "devices": [{"serial_number": "GW-1", "name": "Gateway"}],
                    },
                    "heatpump": {
                        "type_label": "Heat Pump",
                        "count": 1,
                        "devices": [{"serial_number": "HP-1", "name": "Heat Pump"}],
                    },
                    "microinverter": {
                        "type_label": "Microinverters",
                        "count": 1,
                        "devices": [{"serial_number": "INV-1", "name": "Inverter"}],
                    },
                },
                "battery_storage_order": ["BAT-1"],
                "battery_storage_data": {
                    "BAT-1": {"serial_number": "BAT-1", "name": "Battery 1"}
                },
                "inverter_order": ["INV-1"],
                "inverter_data": {
                    "INV-1": {"serial_number": "INV-1", "name": "Inverter 1"}
                },
                "battery_has_encharge": True,
                "battery_has_enpower": True,
                "site_energy_channels": ["heat_pump", "water_heater"],
                "gateway_iq_energy_router_records": [
                    {
                        "device-uid": "ROUTER-1",
                        "device-type": "IQ_ENERGY_ROUTER",
                        "name": "IQ Energy Router 1",
                    }
                ],
            }
        )
    )

    await coord.discovery_snapshot.async_restore_state()

    assert coord.iter_serials() == [RANDOM_SERIAL, "RESTORED123"]
    assert coord.inventory_view.type_bucket("heatpump")["count"] == 1
    assert coord._battery_storage_order == ["BAT-1"]  # noqa: SLF001
    assert coord._inverter_order == ["INV-1"]  # noqa: SLF001
    assert coord._devices_inventory_ready is False  # noqa: SLF001
    assert coord.discovery_snapshot.site_energy_channel_known("heat_pump") is True
    assert coord.discovery_snapshot.site_energy_channel_known("water_heater") is True
    router_records = coord.inventory_view.gateway_iq_energy_router_records()
    assert router_records[0]["device-uid"] == "ROUTER-1"

    coord.discovery_snapshot._store = SimpleNamespace(  # type: ignore[assignment]  # noqa: SLF001
        async_save=AsyncMock()
    )
    await coord.discovery_snapshot.async_save()
    coord.discovery_snapshot._store.async_save.assert_not_awaited()  # type: ignore[attr-defined]


def test_restored_site_energy_and_router_hints_expire_after_authoritative_refresh(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord._restored_site_energy_channels = {  # noqa: SLF001
        "heat_pump",
        "water_heater",
    }
    coord._restored_gateway_iq_energy_router_records = [  # noqa: SLF001
        {
            "device-uid": "ROUTER-1",
            "device-type": "IQ_ENERGY_ROUTER",
            "name": "IQ Energy Router 1",
        }
    ]

    assert coord.discovery_snapshot.site_energy_channel_known("heat_pump") is True
    assert (
        coord.inventory_view.gateway_iq_energy_router_records()[0]["device-uid"]
        == "ROUTER-1"
    )

    coord._site_energy_discovery_ready = True  # noqa: SLF001
    coord._hems_inventory_ready = True  # noqa: SLF001

    assert coord.discovery_snapshot.site_energy_channel_known("heat_pump") is False
    assert coord.discovery_snapshot.site_energy_channel_known("water_heater") is False
    assert coord.inventory_view.gateway_iq_energy_router_records() == []


@pytest.mark.asyncio
async def test_http_backoff_respects_configured_slow_interval(hass, monkeypatch):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
        OPT_SLOW_POLL_INTERVAL,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    class DummyEntry:
        def __init__(self):
            self.options = {OPT_SLOW_POLL_INTERVAL: 300}

        def async_on_unload(self, _cb):
            return None

    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(coord_mod.random, "uniform", lambda *_: 1.0)
    scheduled: dict[str, float | object] = {}

    def fake_call_later(_hass, delay, callback):
        scheduled["delay"] = delay
        scheduled["callback"] = callback

        def _cancel():
            scheduled["cancelled"] = True

        return _cancel

    monkeypatch.setattr(coord_mod, "async_call_later", fake_call_later)

    coord = EnphaseCoordinator(hass, cfg, config_entry=DummyEntry())

    class StubRespErr(aiohttp.ClientResponseError):
        def __init__(self):
            req = aiohttp.RequestInfo(
                url=aiohttp.client.URL("https://example"),
                method="GET",
                headers={},
                real_url=aiohttp.client.URL("https://example"),
            )
            super().__init__(
                request_info=req,
                history=(),
                status=503,
                message="",
                headers={},
            )

    class FailingClient:
        async def status(self):
            raise StubRespErr()

    coord.client = FailingClient()

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord._backoff_until is not None
    remaining = coord._backoff_until - time.monotonic()
    assert remaining >= 295
    assert coord._backoff_cancel is not None
    assert scheduled["delay"] >= 300


@pytest.mark.asyncio
async def test_network_backoff_respects_slow_interval(hass, monkeypatch):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
        OPT_SLOW_POLL_INTERVAL,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    class DummyEntry:
        def __init__(self):
            self.options = {OPT_SLOW_POLL_INTERVAL: 200}

        def async_on_unload(self, _cb):
            return None

    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(coord_mod.random, "uniform", lambda *_: 1.0)
    scheduled: dict[str, float | object] = {}

    def fake_call_later(_hass, delay, callback):
        scheduled["delay"] = delay
        scheduled["callback"] = callback

        def _cancel():
            scheduled["cancelled"] = True

        return _cancel

    monkeypatch.setattr(coord_mod, "async_call_later", fake_call_later)

    coord = EnphaseCoordinator(hass, cfg, config_entry=DummyEntry())

    class FailingClient:
        async def status(self):
            raise aiohttp.ClientError()

    coord.client = FailingClient()

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord._backoff_until is not None
    remaining = coord._backoff_until - time.monotonic()
    assert remaining >= 195
    assert coord._backoff_cancel is not None
    assert scheduled["delay"] >= 200


@pytest.mark.asyncio
async def test_dynamic_poll_switch(hass, monkeypatch):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
        MIN_FAST_POLL_INTERVAL,
        MIN_SLOW_POLL_INTERVAL,
        OPT_FAST_POLL_INTERVAL,
        OPT_SLOW_POLL_INTERVAL,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    # no extra imports

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }
    options = {OPT_FAST_POLL_INTERVAL: 5, OPT_SLOW_POLL_INTERVAL: 20}

    class DummyEntry:
        def __init__(self, options):
            self.options = options

        def async_on_unload(self, cb):
            return None

    entry = DummyEntry(options)
    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg, config_entry=entry)

    class StubClient:
        def __init__(self, payload):
            self._payload = payload

        async def status(self):
            return self._payload

    # Charging -> fast
    payload_charging = {
        "evChargerData": [
            {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "charging": True,
                "pluggedIn": True,
            }
        ]
    }
    coord.client = StubClient(payload_charging)
    coord.data = await coord._async_update_data()
    assert int(coord.update_interval.total_seconds()) == MIN_FAST_POLL_INTERVAL

    # Idle -> temporarily stay fast due to recent toggle
    payload_idle = {
        "evChargerData": [
            {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "charging": False,
                "pluggedIn": True,
            }
        ]
    }
    coord.client = StubClient(payload_idle)
    coord.data = await coord._async_update_data()
    assert int(coord.update_interval.total_seconds()) == MIN_FAST_POLL_INTERVAL

    # Once the boost expires, fall back to the configured slow interval
    coord._fast_until = None
    coord.data = await coord._async_update_data()
    assert int(coord.update_interval.total_seconds()) == MIN_SLOW_POLL_INTERVAL

    # Connector status indicates charging even if flag remains false -> treat as active
    payload_conn_only = {
        "evChargerData": [
            {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "charging": False,
                "pluggedIn": True,
                "connectors": [{"connectorStatusType": "CHARGING"}],
            }
        ]
    }
    coord.client = StubClient(payload_conn_only)
    coord.data = await coord._async_update_data()
    assert int(coord.update_interval.total_seconds()) == MIN_FAST_POLL_INTERVAL
    assert coord.data[RANDOM_SERIAL]["charging"] is True

    # EVSE-side suspension should be treated as paused (not charging)
    payload_conn_suspended = {
        "evChargerData": [
            {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "charging": False,
                "pluggedIn": True,
                "connectors": [{"connectorStatusType": "SUSPENDED_EVSE"}],
            }
        ]
    }
    coord.client = StubClient(payload_conn_suspended)
    coord.data = await coord._async_update_data()
    assert int(coord.update_interval.total_seconds()) == MIN_FAST_POLL_INTERVAL
    assert coord.data[RANDOM_SERIAL]["charging"] is False
    assert coord.data[RANDOM_SERIAL]["suspended_by_evse"] is True

    coord._fast_until = None
    coord.data = await coord._async_update_data()
    assert int(coord.update_interval.total_seconds()) == MIN_SLOW_POLL_INTERVAL


@pytest.mark.asyncio
async def test_auto_resume_when_evse_suspended(monkeypatch, hass):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )

    coord = EnphaseCoordinator(hass, cfg)

    class StubClient:
        def __init__(self):
            self.payload = {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "charging": False,
                        "pluggedIn": True,
                        "connectors": [{"connectorStatusType": "SUSPENDED_EVSE"}],
                    }
                ]
            }
            self.start_calls: list[tuple[str, int, int]] = []

        async def status(self):
            return self.payload

        async def summary_v2(self):
            return []

        async def start_charging(
            self,
            sn,
            amps,
            connector_id=1,
            *,
            include_level=None,
            strict_preference=False,
        ):
            self.start_calls.append((sn, amps, connector_id))
            return {"status": "ok"}

    client = StubClient()
    coord.client = client
    coord.async_request_refresh = AsyncMock()

    coord.set_desired_charging(RANDOM_SERIAL, True)
    coord._auto_resume_attempts.clear()

    await coord._async_update_data()
    await hass.async_block_till_done()

    assert client.start_calls == [(RANDOM_SERIAL, 32, 1)]
    assert coord.async_request_refresh.await_count >= 1


@pytest.mark.asyncio
async def test_charging_expectation_hold(monkeypatch, hass):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    class TimeKeeper:
        def __init__(self):
            self.value = 1_000.0

        def monotonic(self):
            return self.value

        def advance(self, seconds: float) -> None:
            self.value += float(seconds)

    tk = TimeKeeper()
    monkeypatch.setattr(coord_mod.time, "monotonic", tk.monotonic)
    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )

    coord = EnphaseCoordinator(hass, cfg)

    class StubClient:
        def __init__(self, payload):
            self.payload = payload

        async def status(self):
            return self.payload

        async def summary_v2(self):
            return []

    payload_charging = {
        "evChargerData": [
            {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "charging": True,
                "pluggedIn": True,
            }
        ]
    }
    payload_idle = {
        "evChargerData": [
            {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "charging": False,
                "pluggedIn": True,
                "connectors": [{"connectorStatusType": "AVAILABLE"}],
            }
        ]
    }

    client = StubClient(payload_charging)
    coord.client = client
    coord.data = await coord._async_update_data()
    assert coord.data[RANDOM_SERIAL]["charging"] is True

    coord.set_charging_expectation(RANDOM_SERIAL, False, hold_for=90)
    tk.advance(1)
    coord.data = await coord._async_update_data()
    assert coord.data[RANDOM_SERIAL]["charging"] is False

    tk.advance(60)
    coord.data = await coord._async_update_data()
    assert coord.data[RANDOM_SERIAL]["charging"] is False

    tk.advance(40)
    coord.data = await coord._async_update_data()
    assert coord.data[RANDOM_SERIAL]["charging"] is True
    assert RANDOM_SERIAL not in coord._pending_charging

    client.payload = payload_idle
    tk.advance(1)
    coord.data = await coord._async_update_data()
    assert coord.data[RANDOM_SERIAL]["charging"] is False

    coord.set_charging_expectation(RANDOM_SERIAL, True, hold_for=90)
    tk.advance(1)
    coord.data = await coord._async_update_data()
    assert coord.data[RANDOM_SERIAL]["charging"] is True
    assert RANDOM_SERIAL in coord._pending_charging

    client.payload = payload_charging
    tk.advance(1)
    coord.data = await coord._async_update_data()
    assert coord.data[RANDOM_SERIAL]["charging"] is True
    assert RANDOM_SERIAL not in coord._pending_charging


@pytest.mark.asyncio
async def test_default_fast_interval_used_when_charging(hass, monkeypatch):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
        DEFAULT_FAST_POLL_INTERVAL,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    class DummyEntry:
        def __init__(self):
            self.options = {}

        def async_on_unload(self, cb):
            return None

    entry = DummyEntry()
    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg, config_entry=entry)

    class StubClient:
        def __init__(self, payload):
            self._payload = payload

        async def status(self):
            return self._payload

        async def summary_v2(self):
            return []

        async def charge_mode(self, sn: str):
            return "IMMEDIATE"

    payload = {
        "evChargerData": [
            {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "charging": True,
                "pluggedIn": True,
                "connectors": [{"connectorStatusType": "AVAILABLE"}],
            }
        ]
    }
    coord.client = StubClient(payload)
    await coord._async_update_data()
    assert int(coord.update_interval.total_seconds()) == DEFAULT_FAST_POLL_INTERVAL


@pytest.mark.asyncio
async def test_summary_refresh_speed_up_when_charging(hass, monkeypatch):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from tests.components.enphase_ev.random_ids import RANDOM_SERIAL, RANDOM_SITE_ID

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    class DummyEntry:
        def __init__(self):
            self.options = {}

        def async_on_unload(self, cb):
            return None

    entry = DummyEntry()
    from custom_components.enphase_ev import coordinator as coord_mod

    current = {"value": 1000.0}

    def fake_monotonic():
        return current["value"]

    monkeypatch.setattr(coord_mod.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )

    coord = EnphaseCoordinator(hass, cfg, config_entry=entry)

    class StubClient:
        def __init__(self):
            self.summary_calls = 0

        async def status(self):
            return {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "charging": True,
                        "pluggedIn": True,
                        "connectors": [{"connectorStatusType": "AVAILABLE"}],
                    }
                ]
            }

        async def summary_v2(self):
            self.summary_calls += 1
            return [
                {
                    "serialNumber": RANDOM_SERIAL,
                    "lifeTimeConsumption": 1000.0,
                    "lastReportedAt": "2025-10-17T12:00:00Z[UTC]",
                }
            ]

        async def charge_mode(self, sn: str):
            return "IMMEDIATE"

    stub = StubClient()
    coord.client = stub

    await coord._async_update_data()
    assert stub.summary_calls == 1

    current["value"] += 15.0
    await coord._async_update_data()
    assert stub.summary_calls == 1

    current["value"] += 15.0
    await coord._async_update_data()
    assert stub.summary_calls == 2

    current["value"] += 70.0
    await coord._async_update_data()
    assert stub.summary_calls == 3


@pytest.mark.asyncio
async def test_streaming_prefers_fast(hass, monkeypatch):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
        MIN_FAST_POLL_INTERVAL,
        OPT_FAST_POLL_INTERVAL,
        OPT_FAST_WHILE_STREAMING,
        OPT_SLOW_POLL_INTERVAL,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    # no extra imports

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }
    options = {
        OPT_FAST_POLL_INTERVAL: 6,
        OPT_SLOW_POLL_INTERVAL: 22,
        OPT_FAST_WHILE_STREAMING: True,
    }

    class DummyEntry:
        def __init__(self, options):
            self.options = options

        def async_on_unload(self, cb):
            return None

    entry = DummyEntry(options)
    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg, config_entry=entry)

    class StubClient:
        def __init__(self, payload):
            self._payload = payload

        async def status(self):
            return self._payload

    payload_idle = {
        "evChargerData": [
            {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "charging": False,
                "pluggedIn": True,
            }
        ]
    }
    coord.client = StubClient(payload_idle)
    coord._streaming = True
    coord._streaming_until = time.monotonic() + 60
    await coord._async_update_data()
    assert int(coord.update_interval.total_seconds()) == MIN_FAST_POLL_INTERVAL


@pytest.mark.asyncio
async def test_streaming_reverts_to_configured_scan_interval(hass, monkeypatch):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
        MIN_FAST_POLL_INTERVAL,
        MIN_SLOW_POLL_INTERVAL,
        OPT_FAST_POLL_INTERVAL,
        OPT_FAST_WHILE_STREAMING,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }
    options = {
        OPT_FAST_POLL_INTERVAL: 6,
        OPT_FAST_WHILE_STREAMING: True,
    }

    class DummyEntry:
        def __init__(self, options):
            self.options = options

        def async_on_unload(self, cb):
            return None

    entry = DummyEntry(options)
    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg, config_entry=entry)

    class StubClient:
        def __init__(self, payload):
            self._payload = payload

        async def status(self):
            return self._payload

    payload_idle = {
        "evChargerData": [
            {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "charging": False,
                "pluggedIn": True,
            }
        ]
    }
    coord.client = StubClient(payload_idle)

    coord._streaming = True
    coord._streaming_until = time.monotonic() + 60
    await coord._async_update_data()
    assert int(coord.update_interval.total_seconds()) == MIN_FAST_POLL_INTERVAL

    coord._streaming = False
    coord._streaming_until = None
    await coord._async_update_data()
    assert int(coord.update_interval.total_seconds()) == MIN_SLOW_POLL_INTERVAL


@pytest.mark.asyncio
async def test_session_history_enrichment(hass, monkeypatch):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    class DummyEntry:
        def __init__(self):
            self.options = {}

        def async_on_unload(self, cb):
            return None

    await hass.config.async_set_time_zone("UTC")
    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg, config_entry=DummyEntry())

    class StubClient:
        async def status(self):
            return {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "charging": False,
                        "pluggedIn": True,
                    }
                ]
            }

        async def summary_v2(self):
            return []

        async def charge_mode(self, sn):
            return None

    coord.client = StubClient()

    async def _fake_sessions(self, sn, *, day_local=None):
        return [
            {
                "session_id": "stub-1",
                "start": "2025-10-16T00:00:00+00:00",
                "end": "2025-10-16T01:00:00+00:00",
                "energy_kwh_total": 4.5,
                "energy_kwh": 4.5,
                "active_charge_time_s": 3600,
                "auth_type": None,
                "auth_identifier": None,
                "auth_token": None,
                "miles_added": 15.0,
                "session_cost": 1.1,
                "avg_cost_per_kwh": 0.24,
                "cost_calculated": True,
                "manual_override": False,
                "session_cost_state": "COST_CALCULATED",
                "charge_profile_stack_level": 0,
            },
            {
                "session_id": "stub-2",
                "start": "2025-10-16T04:00:00+00:00",
                "end": "2025-10-16T05:30:00+00:00",
                "energy_kwh_total": 2.0,
                "energy_kwh": 2.0,
                "active_charge_time_s": 5400,
                "auth_type": "RFID",
                "auth_identifier": "user",
                "auth_token": "token",
                "miles_added": 8.0,
                "session_cost": 0.6,
                "avg_cost_per_kwh": 0.3,
                "cost_calculated": True,
                "manual_override": True,
                "session_cost_state": "COST_CALCULATED",
                "charge_profile_stack_level": 4,
            },
            {
                "session_id": "stub-3",
                "start": "2025-10-15T23:30:00+00:00",
                "end": "2025-10-16T00:30:00+00:00",
                "energy_kwh_total": 4.0,
                "energy_kwh": 2.0,
                "active_charge_time_s": 3600,
                "auth_type": None,
                "auth_identifier": None,
                "auth_token": None,
                "miles_added": 10.0,
                "session_cost": 0.5,
                "avg_cost_per_kwh": 0.25,
                "cost_calculated": True,
                "manual_override": False,
                "session_cost_state": "COST_CALCULATED",
                "charge_profile_stack_level": 2,
            },
        ]

    coord._async_fetch_sessions_today = _fake_sessions.__get__(coord, coord.__class__)

    data = await coord._async_update_data()
    coord.async_set_updated_data(data)
    st = data[RANDOM_SERIAL]
    assert st["energy_today_sessions_kwh"] == 0.0
    assert st["energy_today_sessions"] == []

    data = await coord._async_update_data()
    coord.async_set_updated_data(data)
    st = data[RANDOM_SERIAL]
    assert st["energy_today_sessions_kwh"] == pytest.approx(8.5, abs=1e-3)
    assert len(st["energy_today_sessions"]) == 3
    cross_midnight = st["energy_today_sessions"][2]
    assert cross_midnight["energy_kwh_total"] == pytest.approx(4.0)
    assert cross_midnight["energy_kwh"] == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_session_history_prefers_last_session_day_when_idle(hass, monkeypatch):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    class DummyEntry:
        def __init__(self):
            self.options = {}

        def async_on_unload(self, cb):
            return None

    await hass.config.async_set_time_zone("UTC")
    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )

    now_local = datetime(2025, 12, 31, 10, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: now_local)

    last_end = int((now_local - timedelta(days=1, hours=2)).timestamp())
    last_start = last_end - 3600

    class StubClient:
        async def status(self):
            return {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "charging": False,
                        "pluggedIn": True,
                        "connectors": [{}],
                        "session_d": {
                            "start_time": last_start,
                            "plg_out_at": last_end,
                        },
                    }
                ]
            }

        async def summary_v2(self):
            return []

        async def charge_mode(self, sn):
            return None

    coord = EnphaseCoordinator(hass, cfg, config_entry=DummyEntry())
    coord.client = StubClient()

    captured: dict[str, object] = {}

    def _capture_schedule(serials, day_local):
        captured["serials"] = list(serials)
        captured["day_local"] = day_local

    coord.session_history.schedule_enrichment = _capture_schedule  # type: ignore[assignment]

    await coord._async_update_data()

    expected_day = datetime.fromtimestamp(last_end, tz=timezone.utc).date()
    assert captured["serials"] == [RANDOM_SERIAL]
    assert captured["day_local"].date() == expected_day


@pytest.mark.asyncio
async def test_session_history_day_handles_bad_timestamps(hass, monkeypatch):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    class BadFloat:
        def __float__(self):
            raise ValueError("boom")

    await hass.config.async_set_time_zone("UTC")

    now_local = datetime(2025, 12, 31, 12, 0, 0, tzinfo=timezone.utc)
    day_local_default = coord_mod.dt_util.as_local(now_local)
    target_ts = int((now_local - timedelta(hours=2)).timestamp())
    orig_as_local = coord_mod.dt_util.as_local

    def _fake_as_local(value):
        if isinstance(value, datetime) and abs(value.timestamp() - target_ts) < 1.0:
            raise ValueError("tz boom")
        return orig_as_local(value)

    monkeypatch.setattr(coord_mod.dt_util, "as_local", _fake_as_local)

    bad_payload = {"charging": False, "session_end": BadFloat()}
    assert (
        EnphaseCoordinator._session_history_day(bad_payload, day_local_default)
        == day_local_default
    )

    overflow_payload = {"charging": False, "session_end": 10**20}
    assert (
        EnphaseCoordinator._session_history_day(overflow_payload, day_local_default)
        == day_local_default
    )

    as_local_payload = {"charging": False, "session_end": target_ts}
    result = EnphaseCoordinator._session_history_day(
        as_local_payload, day_local_default
    )
    assert abs(result.timestamp() - target_ts) < 1.0


@pytest.mark.asyncio
@pytest.mark.session_history_real
async def test_session_history_cross_midnight_split(hass, monkeypatch):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    class DummyEntry:
        def __init__(self):
            self.options = {}

        def async_on_unload(self, cb):
            return None

    await hass.config.async_set_time_zone("UTC")
    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg, config_entry=DummyEntry())

    day_now = datetime(2025, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: day_now)

    client = coord.client
    calls: list[dict] = []

    async def fake_session_history(
        self, sn, *, start_date, end_date, offset, limit, **_kwargs
    ):
        calls.append(
            {
                "sn": sn,
                "start_date": start_date,
                "end_date": end_date,
                "offset": offset,
                "limit": limit,
            }
        )
        return {
            "data": {
                "result": [
                    {
                        "sessionId": 1,
                        "startTime": "2025-10-15T23:30:00Z[UTC]",
                        "endTime": "2025-10-16T01:30:00Z[UTC]",
                        "aggEnergyValue": 6.0,
                        "activeChargeTime": 7200,
                    },
                    {
                        "sessionId": 2,
                        "startTime": "2025-10-16T04:00:00Z[UTC]",
                        "endTime": "2025-10-16T05:00:00Z[UTC]",
                        "aggEnergyValue": 3.0,
                        "activeChargeTime": 3600,
                    },
                ],
                "hasMore": False,
                "startDate": start_date,
                "endDate": end_date,
            }
        }

    monkeypatch.setattr(
        client,
        "session_history",
        fake_session_history.__get__(client, client.__class__),
        raising=False,
    )

    async def fake_filter_criteria(self, **_kwargs):
        return {"data": [{"id": RANDOM_SERIAL}]}

    monkeypatch.setattr(
        client,
        "session_history_filter_criteria",
        fake_filter_criteria.__get__(client, client.__class__),
        raising=False,
    )

    sessions = await coord._async_fetch_sessions_today(RANDOM_SERIAL, day_local=day_now)
    assert calls, "session_history should have been called"
    assert len(sessions) == 2
    assert len(calls) == 1

    first = sessions[0]
    assert first["energy_kwh_total"] == pytest.approx(6.0)
    # Only 1.5 hours of a 2 hour session occur within the day -> 75%
    assert first["energy_kwh"] == pytest.approx(4.5)
    assert first["active_charge_time_overlap_s"] == 5400

    second = sessions[1]
    assert second["energy_kwh"] == pytest.approx(3.0)

    # Cached result should be reused
    calls.clear()
    again = await coord._async_fetch_sessions_today(RANDOM_SERIAL, day_local=day_now)
    assert not calls
    assert again == sessions


@pytest.mark.asyncio
@pytest.mark.session_history_real
async def test_session_history_unauthorized_falls_back(hass, monkeypatch):
    from custom_components.enphase_ev.api import Unauthorized
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    class DummyEntry:
        def __init__(self):
            self.options = {}

        def async_on_unload(self, cb):
            return None

    await hass.config.async_set_time_zone("UTC")
    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg, config_entry=DummyEntry())

    class StubClient:
        async def status(self):
            return {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "charging": False,
                        "pluggedIn": True,
                    }
                ],
                "ts": 1757299870275,
            }

        async def summary_v2(self):
            return []

        async def charge_mode(self, sn):
            return None

        async def session_history(self, *args, **kwargs):
            raise Unauthorized()

    coord.client = StubClient()

    data = await coord._async_update_data()
    st = data[RANDOM_SERIAL]
    assert st["energy_today_sessions"] == []
    assert st["energy_today_sessions_kwh"] == 0.0


@pytest.mark.asyncio
@pytest.mark.session_history_real
async def test_session_history_inflight_session_counts_energy(hass, monkeypatch):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    class DummyEntry:
        def __init__(self):
            self.options = {}

        def async_on_unload(self, cb):
            return None

    await hass.config.async_set_time_zone("UTC")
    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg, config_entry=DummyEntry())

    # Fix "now" for the coordinator so the ongoing session overlaps the day
    now_local = datetime(2025, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: now_local)

    class StubClient:
        async def status(self):
            return {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "charging": True,
                        "pluggedIn": True,
                    }
                ],
                "ts": 1757299870275,
            }

        async def summary_v2(self):
            return []

        async def charge_mode(self, sn):
            return None

        async def session_history(self, *args, **kwargs):
            return {
                "data": {
                    "result": [
                        {
                            "sessionId": 99,
                            "startTime": "2025-10-16T09:30:00Z[UTC]",
                            "endTime": None,
                            "aggEnergyValue": 4.0,
                            "activeChargeTime": 7200,
                        }
                    ],
                    "hasMore": False,
                }
            }

    coord.client = StubClient()

    data = await coord._async_update_data()
    coord.async_set_updated_data(data)
    st = data[RANDOM_SERIAL]
    assert not st["energy_today_sessions"]
    assert st["energy_today_sessions_kwh"] == 0.0

    data = await coord._async_update_data()
    coord.async_set_updated_data(data)
    st = data[RANDOM_SERIAL]
    sessions = st["energy_today_sessions"]
    assert sessions and len(sessions) == 1
    inflight = sessions[0]
    assert inflight["energy_kwh_total"] == pytest.approx(4.0)
    assert inflight["energy_kwh"] == pytest.approx(4.0)
    assert inflight["active_charge_time_overlap_s"] > 0
    assert st["energy_today_sessions_kwh"] == pytest.approx(4.0)


@pytest.mark.asyncio
async def test_timeout_backoff_issue_recovery(hass, monkeypatch):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
        DEFAULT_SCAN_INTERVAL,
        ISSUE_NETWORK_UNREACHABLE,
    )
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
    }

    class DummyEntry:
        def __init__(self):
            self.options = {}

        def async_on_unload(self, cb):
            return None

    entry = DummyEntry()
    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        coord_mod,
        "async_call_later",
        lambda *_args, **_kwargs: (lambda: None),
    )

    create_calls: list[tuple[str, str, dict]] = []
    delete_calls: list[tuple[str, str]] = []

    def stub_create_issue(hass_arg, domain, issue_id, **kwargs):
        create_calls.append((domain, issue_id, kwargs))

    def stub_delete_issue(hass_arg, domain, issue_id):
        delete_calls.append((domain, issue_id))

    from custom_components.enphase_ev import coordinator_diagnostics as diag_mod

    monkeypatch.setattr(diag_mod.ir, "async_create_issue", stub_create_issue)
    monkeypatch.setattr(diag_mod.ir, "async_delete_issue", stub_delete_issue)

    coord = EnphaseCoordinator(hass, cfg, config_entry=entry)

    class TimeoutClient:
        async def status(self):
            await asyncio.sleep(0)
            raise asyncio.TimeoutError()

    coord.client = TimeoutClient()

    for idx in range(2):
        with pytest.raises(UpdateFailed):
            await coord._async_update_data()
        assert coord._network_errors == idx + 1
        assert coord._backoff_until is not None
        assert not create_calls
        coord._backoff_until = 0

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()
    assert coord._network_errors == 3
    assert create_calls
    assert create_calls[0][0] == "enphase_ev"
    assert create_calls[0][1] == ISSUE_NETWORK_UNREACHABLE
    assert len(create_calls) == 1
    coord._backoff_until = 0

    class SuccessClient:
        async def status(self):
            return {"evChargerData": []}

    coord.client = SuccessClient()
    await coord._async_update_data()
    assert coord._network_errors == 0
    assert coord._last_error is None
    assert delete_calls
    assert delete_calls[-1][1] == ISSUE_NETWORK_UNREACHABLE
    assert [issue_id for _, issue_id in delete_calls] == [
        "reauth_required",
        "auth_blocked",
        "too_many_active_sessions",
        "rate_limited",
        ISSUE_NETWORK_UNREACHABLE,
    ]
    assert coord._backoff_until is None


@pytest.mark.asyncio
async def test_refresh_battery_backup_history_parses_and_caches(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    payload = {
        "total_records": 4,
        "total_backup": 307,
        "histories": [
            {"start_time": "2025-10-17T14:38:30+11:00", "duration": 121},
            {"start_time": "bad", "duration": 60},
            {"start_time": "2025-10-16T18:30:09+11:00", "duration": 0},
            {"start_time": None, "duration": 74},
        ],
    }
    monkeypatch.setattr(coord, "_redact_battery_payload", lambda value: "raw")
    coord.client.battery_backup_history = AsyncMock(return_value=payload)

    await coord._async_refresh_battery_backup_history(force=True)  # noqa: SLF001

    assert coord._battery_backup_history_payload == {"value": "raw"}  # noqa: SLF001
    events = coord.battery_backup_history_events
    assert len(events) == 1
    assert events[0]["duration_seconds"] == 121
    assert isinstance(events[0]["start"], datetime)
    assert isinstance(events[0]["end"], datetime)
    first_event_end = events[0]["end"]
    assert first_event_end - events[0]["start"] == timedelta(seconds=121)
    assert coord._battery_backup_history_cache_until is not None  # noqa: SLF001

    coord._battery_backup_history_cache_until = time.monotonic() + 60  # noqa: SLF001
    coord.client.battery_backup_history = AsyncMock(
        side_effect=AssertionError("no fetch")
    )
    await coord._async_refresh_battery_backup_history()  # noqa: SLF001


@pytest.mark.asyncio
async def test_refresh_battery_backup_history_stores_redacted_dict_payload(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    payload = {
        "histories": [{"start_time": "2025-10-17T14:38:30+11:00", "duration": 120}]
    }
    monkeypatch.setattr(coord, "_redact_battery_payload", lambda value: {"safe": True})
    coord.client.battery_backup_history = AsyncMock(return_value=payload)

    await coord._async_refresh_battery_backup_history(force=True)  # noqa: SLF001

    assert coord._battery_backup_history_payload == {"safe": True}  # noqa: SLF001


@pytest.mark.asyncio
async def test_refresh_battery_backup_history_keeps_last_good_on_invalid_or_error(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_backup_history_events = [  # noqa: SLF001
        {
            "start": datetime(2025, 10, 17, 3, 38, tzinfo=timezone.utc),
            "end": datetime(2025, 10, 17, 3, 40, tzinfo=timezone.utc),
            "duration_seconds": 120,
        }
    ]
    expected = coord.battery_backup_history_events

    coord.client.battery_backup_history = AsyncMock(return_value={"histories": "bad"})
    await coord._async_refresh_battery_backup_history(force=True)  # noqa: SLF001
    assert coord.battery_backup_history_events == expected
    assert coord._battery_backup_history_cache_until is not None  # noqa: SLF001
    invalid_cache_until = coord._battery_backup_history_cache_until  # noqa: SLF001
    assert invalid_cache_until >= time.monotonic() + (
        BATTERY_BACKUP_HISTORY_FAILURE_CACHE_TTL - 1
    )

    coord.client.battery_backup_history = AsyncMock(
        side_effect=AssertionError("no fetch")
    )
    await coord._async_refresh_battery_backup_history()  # noqa: SLF001

    coord._battery_backup_history_cache_until = None  # noqa: SLF001
    coord.client.battery_backup_history = AsyncMock(side_effect=RuntimeError("boom"))
    await coord._async_refresh_battery_backup_history(force=True)  # noqa: SLF001
    assert coord.battery_backup_history_events == expected
    assert coord._battery_backup_history_cache_until is not None  # noqa: SLF001


def test_parse_battery_backup_history_uses_site_timezone_for_naive_start_time(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_timezone = "Australia/Sydney"  # noqa: SLF001

    parsed = coord._parse_battery_backup_history_payload(  # noqa: SLF001
        {
            "total_records": 1,
            "total_backup": 120,
            "histories": [{"start_time": "2025-10-17T14:38:30", "duration": 120}],
        }
    )

    assert parsed is not None
    assert len(parsed) == 1
    assert parsed[0]["start"].tzinfo == ZoneInfo("Australia/Sydney")
    assert parsed[0]["end"] - parsed[0]["start"] == timedelta(seconds=120)


def test_battery_backup_history_events_property_filters_non_dict(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_backup_history_events = [  # noqa: SLF001
        {"start": datetime(2025, 10, 17, 3, 38, tzinfo=timezone.utc)},
        "bad",
    ]
    events = coord.battery_backup_history_events

    assert len(events) == 1
    assert isinstance(events[0], dict)

    coord._battery_backup_history_events = None  # type: ignore[assignment]  # noqa: SLF001
    assert coord.battery_backup_history_events == []


def test_backup_history_tzinfo_fallback_to_default_timezone(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_timezone = "Invalid/Timezone"  # noqa: SLF001

    assert coord._backup_history_tzinfo() == dt_util.DEFAULT_TIME_ZONE  # noqa: SLF001


def test_backup_history_tzinfo_fallback_to_utc_when_default_missing(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_timezone = "Invalid/Timezone"  # noqa: SLF001
    dt_util.DEFAULT_TIME_ZONE = None
    try:
        assert coord._backup_history_tzinfo() == timezone.utc  # noqa: SLF001
    finally:
        dt_util.DEFAULT_TIME_ZONE = timezone.utc


def test_parse_battery_backup_history_payload_rejects_non_dict(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    assert coord._parse_battery_backup_history_payload(["bad"]) is None  # noqa: SLF001


def test_parse_battery_backup_history_payload_skips_invalid_rows(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    parsed = coord._parse_battery_backup_history_payload(  # noqa: SLF001
        {
            "total_records": 5,
            "total_backup": 120,
            "histories": [
                "bad",
                {"start_time": "2025-10-17T14:38:30+11:00", "duration": "oops"},
                {"start_time": BadStr(), "duration": 60},
                {"start_time": "   ", "duration": 60},
                {"start_time": "2025-10-17T14:38:30+11:00", "duration": 120},
            ],
        }
    )

    assert parsed is not None
    assert len(parsed) == 1
    assert parsed[0]["duration_seconds"] == 120


@pytest.mark.asyncio
async def test_update_data_site_only_refreshes_battery_status(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.site_only = True
    coord.serials = set()
    coord._has_successful_refresh = True  # noqa: SLF001
    coord.energy._async_refresh_site_energy = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_site_settings = AsyncMock()  # noqa: SLF001
    coord.battery_runtime.async_refresh_battery_status = AsyncMock()  # type: ignore[method-assign]  # noqa: SLF001
    coord._async_refresh_battery_backup_history = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_storm_guard_profile = AsyncMock()  # noqa: SLF001
    coord._async_refresh_storm_alert = AsyncMock()  # noqa: SLF001
    coord.inventory_runtime._async_refresh_devices_inventory = AsyncMock()  # type: ignore[method-assign]  # noqa: SLF001
    coord._async_refresh_inverters = AsyncMock()  # noqa: SLF001

    await coord._async_update_data()  # noqa: SLF001

    coord.battery_runtime.async_refresh_battery_status.assert_awaited_once()
    coord._async_refresh_battery_backup_history.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_data_site_only_continues_when_backup_history_refresh_fails(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.site_only = True
    coord.serials = set()
    coord._has_successful_refresh = True  # noqa: SLF001
    coord.energy._async_refresh_site_energy = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_site_settings = AsyncMock()  # noqa: SLF001
    coord.battery_runtime.async_refresh_battery_status = AsyncMock()  # type: ignore[method-assign]  # noqa: SLF001
    coord._async_refresh_battery_backup_history = AsyncMock(  # noqa: SLF001
        side_effect=aiohttp.ClientError("boom")
    )
    coord._async_refresh_battery_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_storm_guard_profile = AsyncMock()  # noqa: SLF001
    coord._async_refresh_storm_alert = AsyncMock()  # noqa: SLF001
    coord.inventory_runtime._async_refresh_devices_inventory = AsyncMock()  # type: ignore[method-assign]  # noqa: SLF001
    coord._async_refresh_inverters = AsyncMock()  # noqa: SLF001

    await coord._async_update_data()  # noqa: SLF001

    coord._async_refresh_battery_backup_history.assert_awaited_once()
    coord._async_refresh_battery_settings.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_data_site_only_ignores_optional_refresh_failures(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.site_only = True
    coord.serials = set()
    coord._has_successful_refresh = True  # noqa: SLF001
    coord.energy._async_refresh_site_energy = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_site_settings = AsyncMock(  # noqa: SLF001
        side_effect=aiohttp.ClientError("site settings")
    )
    coord.battery_runtime.async_refresh_battery_status = AsyncMock()  # type: ignore[method-assign]  # noqa: SLF001
    coord._async_refresh_battery_backup_history = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_schedules = AsyncMock(  # noqa: SLF001
        side_effect=aiohttp.ClientError("schedules")
    )
    coord._async_refresh_storm_guard_profile = AsyncMock(  # noqa: SLF001
        side_effect=aiohttp.ClientError("storm guard")
    )
    coord._async_refresh_storm_alert = AsyncMock(  # noqa: SLF001
        side_effect=aiohttp.ClientError("storm alert")
    )
    coord.battery_runtime.async_refresh_grid_control_check = AsyncMock(  # type: ignore[method-assign]
        side_effect=aiohttp.ClientError("grid")
    )
    coord.inventory_runtime._async_refresh_devices_inventory = AsyncMock(  # type: ignore[method-assign]  # noqa: SLF001
        side_effect=aiohttp.ClientError("inventory")
    )
    coord._async_refresh_dry_contact_settings = AsyncMock(  # noqa: SLF001
        side_effect=aiohttp.ClientError("dry contact")
    )
    coord.inventory_runtime._async_refresh_hems_devices = AsyncMock(  # type: ignore[method-assign]  # noqa: SLF001
        side_effect=aiohttp.ClientError("hems")
    )
    coord._async_refresh_inverters = AsyncMock(  # noqa: SLF001
        side_effect=aiohttp.ClientError("inverters")
    )
    coord._async_refresh_current_power_consumption = AsyncMock()  # noqa: SLF001
    coord._async_refresh_heatpump_power = AsyncMock(  # noqa: SLF001
        side_effect=aiohttp.ClientError("heatpump")
    )
    coord.heatpump_runtime.heatpump_power_refresh_due = lambda: True  # type: ignore[method-assign]

    await coord._async_update_data()  # noqa: SLF001

    assert "site_energy_s" in coord.phase_timings
    assert "total_s" in coord.phase_timings
    coord._async_refresh_heatpump_power.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_data_normal_refreshes_battery_status(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._has_successful_refresh = True  # noqa: SLF001
    coord.client.status = AsyncMock(return_value={"evChargerData": []})
    coord._async_refresh_battery_site_settings = AsyncMock()  # noqa: SLF001
    coord.battery_runtime.async_refresh_battery_status = AsyncMock()  # type: ignore[method-assign]  # noqa: SLF001
    coord._async_refresh_battery_backup_history = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_storm_guard_profile = AsyncMock()  # noqa: SLF001
    coord._async_refresh_storm_alert = AsyncMock()  # noqa: SLF001
    coord.inventory_runtime._async_refresh_devices_inventory = AsyncMock()  # type: ignore[method-assign]  # noqa: SLF001
    coord._async_refresh_inverters = AsyncMock()  # noqa: SLF001
    coord.evse_runtime.async_resolve_green_battery_settings = AsyncMock(  # type: ignore[method-assign]
        return_value={}
    )  # noqa: SLF001
    coord.evse_runtime.async_resolve_auth_settings = AsyncMock(return_value={})  # type: ignore[method-assign]  # noqa: SLF001
    coord._sync_battery_profile_pending_issue = MagicMock()  # noqa: SLF001

    await coord._async_update_data()  # noqa: SLF001

    coord.battery_runtime.async_refresh_battery_status.assert_awaited_once()
    coord._async_refresh_battery_backup_history.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_data_normal_continues_when_backup_history_refresh_fails(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._has_successful_refresh = True  # noqa: SLF001
    coord.client.status = AsyncMock(return_value={"evChargerData": []})
    coord._async_refresh_battery_site_settings = AsyncMock()  # noqa: SLF001
    coord.battery_runtime.async_refresh_battery_status = AsyncMock()  # type: ignore[method-assign]  # noqa: SLF001
    coord._async_refresh_battery_backup_history = AsyncMock(  # noqa: SLF001
        side_effect=aiohttp.ClientError("boom")
    )
    coord._async_refresh_battery_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_storm_guard_profile = AsyncMock()  # noqa: SLF001
    coord._async_refresh_storm_alert = AsyncMock()  # noqa: SLF001
    coord.inventory_runtime._async_refresh_devices_inventory = AsyncMock()  # type: ignore[method-assign]  # noqa: SLF001
    coord._async_refresh_inverters = AsyncMock()  # noqa: SLF001
    coord.evse_runtime.async_resolve_green_battery_settings = AsyncMock(  # type: ignore[method-assign]
        return_value={}
    )  # noqa: SLF001
    coord.evse_runtime.async_resolve_auth_settings = AsyncMock(return_value={})  # type: ignore[method-assign]  # noqa: SLF001
    coord._sync_battery_profile_pending_issue = MagicMock()  # noqa: SLF001

    await coord._async_update_data()  # noqa: SLF001

    coord._async_refresh_battery_backup_history.assert_awaited_once()
    coord._async_refresh_battery_settings.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_data_normal_ignores_optional_refresh_failures(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._has_successful_refresh = True  # noqa: SLF001
    coord._scheduler_available = False  # noqa: SLF001
    coord._scheduler_backoff_active = lambda: False  # type: ignore[assignment]  # noqa: SLF001
    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": RANDOM_SERIAL,
                    "name": "Garage EV",
                    "chargeMode": "IMMEDIATE",
                    "connectors": [{}],
                    "session_d": {},
                    "sch_d": {},
                    "charging": False,
                }
            ],
            "ts": 1_700_000_000,
        }
    )
    coord.energy._async_refresh_site_energy = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord.evse_timeseries.async_refresh = AsyncMock(  # noqa: SLF001
        side_effect=aiohttp.ClientError("timeseries")
    )
    coord._async_refresh_battery_site_settings = AsyncMock()  # noqa: SLF001
    coord.battery_runtime.async_refresh_battery_status = AsyncMock()  # type: ignore[method-assign]  # noqa: SLF001
    coord._async_refresh_battery_backup_history = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_schedules = AsyncMock(  # noqa: SLF001
        side_effect=aiohttp.ClientError("schedules")
    )
    coord._async_refresh_storm_guard_profile = AsyncMock()  # noqa: SLF001
    coord._async_refresh_storm_alert = AsyncMock()  # noqa: SLF001
    coord.battery_runtime.async_refresh_grid_control_check = AsyncMock(  # type: ignore[method-assign]
        side_effect=aiohttp.ClientError("grid")
    )
    coord.inventory_runtime._async_refresh_devices_inventory = AsyncMock(  # type: ignore[method-assign]  # noqa: SLF001
        side_effect=aiohttp.ClientError("inventory")
    )
    coord._async_refresh_dry_contact_settings = AsyncMock(  # noqa: SLF001
        side_effect=aiohttp.ClientError("dry contact")
    )
    coord.inventory_runtime._async_refresh_hems_devices = AsyncMock(  # type: ignore[method-assign]  # noqa: SLF001
        side_effect=aiohttp.ClientError("hems")
    )
    coord._async_refresh_inverters = AsyncMock(  # noqa: SLF001
        side_effect=aiohttp.ClientError("inverters")
    )
    coord._async_refresh_current_power_consumption = AsyncMock()  # noqa: SLF001
    coord._async_refresh_heatpump_power = AsyncMock(  # noqa: SLF001
        side_effect=aiohttp.ClientError("heatpump")
    )
    coord._get_charge_mode = AsyncMock(return_value="IMMEDIATE")  # type: ignore[assignment]  # noqa: SLF001
    coord.evse_runtime.async_resolve_green_battery_settings = AsyncMock(  # type: ignore[method-assign]
        return_value={}
    )  # noqa: SLF001
    coord.evse_runtime.async_resolve_auth_settings = AsyncMock(return_value={})  # type: ignore[method-assign]  # noqa: SLF001
    coord._sync_battery_profile_pending_issue = MagicMock()  # noqa: SLF001
    coord.heatpump_runtime.heatpump_power_refresh_due = lambda: True  # type: ignore[method-assign]

    await coord._async_update_data()  # noqa: SLF001

    assert "battery_schedules_s" in coord.phase_timings
    assert "grid_control_check_s" in coord.phase_timings
    assert "devices_inventory_s" in coord.phase_timings
    assert "dry_contact_settings_s" in coord.phase_timings
    assert "hems_devices_s" in coord.phase_timings
    assert "evse_timeseries_s" in coord.phase_timings
    assert "site_energy_s" in coord.phase_timings
    assert "inverters_s" in coord.phase_timings
    assert "heatpump_power_s" in coord.phase_timings
    coord._get_charge_mode.assert_awaited_once_with(RANDOM_SERIAL)


@pytest.mark.asyncio
async def test_update_data_normal_ignores_merge_charger_payload_failures(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._has_successful_refresh = True  # noqa: SLF001
    coord._scheduler_available = False  # noqa: SLF001
    coord._scheduler_backoff_active = lambda: False  # type: ignore[assignment]  # noqa: SLF001
    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": RANDOM_SERIAL,
                    "name": "Garage EV",
                    "chargeMode": "IMMEDIATE",
                    "connectors": [{}],
                    "session_d": {},
                    "sch_d": {},
                    "charging": False,
                }
            ],
            "ts": 1_700_000_000,
        }
    )
    coord.energy._async_refresh_site_energy = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord.evse_timeseries.async_refresh = AsyncMock(return_value=None)  # noqa: SLF001
    coord.evse_timeseries.merge_charger_payloads = Mock(
        side_effect=RuntimeError("boom")
    )  # noqa: SLF001
    coord._async_refresh_battery_site_settings = AsyncMock()  # noqa: SLF001
    coord.battery_runtime.async_refresh_battery_status = AsyncMock()  # type: ignore[method-assign]  # noqa: SLF001
    coord._async_refresh_battery_backup_history = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_schedules = AsyncMock()  # noqa: SLF001
    coord._async_refresh_storm_guard_profile = AsyncMock()  # noqa: SLF001
    coord._async_refresh_storm_alert = AsyncMock()  # noqa: SLF001
    coord.battery_runtime.async_refresh_grid_control_check = AsyncMock()  # type: ignore[method-assign]  # noqa: SLF001
    coord.inventory_runtime._async_refresh_devices_inventory = AsyncMock()  # type: ignore[method-assign]  # noqa: SLF001
    coord._async_refresh_dry_contact_settings = AsyncMock()  # noqa: SLF001
    coord.inventory_runtime._async_refresh_hems_devices = AsyncMock()  # type: ignore[method-assign]  # noqa: SLF001
    coord._async_refresh_inverters = AsyncMock()  # noqa: SLF001
    coord._async_refresh_current_power_consumption = AsyncMock()  # noqa: SLF001
    coord._async_refresh_heatpump_power = AsyncMock()  # noqa: SLF001
    coord._get_charge_mode = AsyncMock(return_value="IMMEDIATE")  # type: ignore[assignment]  # noqa: SLF001
    coord.evse_runtime.async_resolve_green_battery_settings = AsyncMock(  # type: ignore[method-assign]
        return_value={}
    )  # noqa: SLF001
    coord.evse_runtime.async_resolve_auth_settings = AsyncMock(return_value={})  # type: ignore[method-assign]  # noqa: SLF001
    coord._sync_battery_profile_pending_issue = MagicMock()  # noqa: SLF001

    result = await coord._async_update_data()  # noqa: SLF001

    assert RANDOM_SERIAL in result


@pytest.mark.asyncio
async def test_update_data_site_only_orders_topology_mutations_deterministically(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord.site_only = True
    coord.serials = set()
    coord._has_successful_refresh = True  # noqa: SLF001
    coord.energy._async_refresh_site_energy = AsyncMock(
        return_value=None
    )  # noqa: SLF001

    order: list[str] = []

    async def _record(name: str) -> None:
        order.append(name)

    async def _record_battery_status() -> None:
        await _record("battery_status")

    async def _record_devices_inventory() -> None:
        await _record("devices_inventory")

    async def _record_hems_devices() -> None:
        await _record("hems_devices")

    async def _record_inverters() -> None:
        await _record("inverters")

    async def _record_heatpump_power() -> None:
        await _record("heatpump_power")

    async def _record_heatpump_runtime() -> None:
        await _record("heatpump_runtime")

    async def _record_heatpump_daily() -> None:
        await _record("heatpump_daily")

    coord._async_refresh_battery_site_settings = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_backup_history = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_settings = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_battery_schedules = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_storm_guard_profile = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_storm_alert = AsyncMock(return_value=None)  # noqa: SLF001
    coord.battery_runtime.async_refresh_grid_control_check = AsyncMock(  # type: ignore[method-assign]
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_dry_contact_settings = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_current_power_consumption = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord.battery_runtime.async_refresh_battery_status = AsyncMock(  # type: ignore[method-assign]
        side_effect=_record_battery_status
    )  # noqa: SLF001
    coord.inventory_runtime._async_refresh_devices_inventory = AsyncMock(  # type: ignore[method-assign]  # noqa: SLF001
        side_effect=_record_devices_inventory
    )
    coord.inventory_runtime._async_refresh_hems_devices = AsyncMock(  # type: ignore[method-assign]
        side_effect=_record_hems_devices
    )  # noqa: SLF001
    coord._async_refresh_inverters = AsyncMock(
        side_effect=_record_inverters
    )  # noqa: SLF001
    coord._async_refresh_heatpump_runtime_state = AsyncMock(
        side_effect=_record_heatpump_runtime
    )  # noqa: SLF001
    coord._async_refresh_heatpump_daily_consumption = AsyncMock(
        side_effect=_record_heatpump_daily
    )  # noqa: SLF001
    coord._async_refresh_heatpump_power = AsyncMock(
        side_effect=_record_heatpump_power
    )  # noqa: SLF001
    coord.heatpump_runtime.heatpump_runtime_state_refresh_due = lambda: True  # type: ignore[method-assign]
    coord.heatpump_runtime.heatpump_daily_consumption_refresh_due = lambda: True  # type: ignore[method-assign]
    coord.heatpump_runtime.heatpump_power_refresh_due = lambda: True  # type: ignore[method-assign]
    coord.heatpump_runtime.has_type = lambda _key: True  # type: ignore[method-assign]
    coord.client._hems_site_supported = True  # noqa: SLF001

    await coord._async_update_data()  # noqa: SLF001

    assert order == [
        "battery_status",
        "devices_inventory",
        "hems_devices",
        "inverters",
        "heatpump_power",
    ]


def test_battery_status_helper_edge_cases(coordinator_factory) -> None:
    coord = coordinator_factory()

    class ExplodingFloat(float):
        def __float__(self):  # type: ignore[override]
            raise ValueError("boom")

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    class BadStrip(str):
        def strip(self, chars=None):  # type: ignore[override]
            raise ValueError("boom")

    assert coord._coerce_optional_float(True) == 1.0  # noqa: SLF001
    assert coord._coerce_optional_float(ExplodingFloat(1.0)) is None  # noqa: SLF001
    assert coord._coerce_optional_float(BadStrip("1")) is None  # noqa: SLF001
    assert coord._coerce_optional_float("   ") is None  # noqa: SLF001
    assert coord._coerce_optional_float("1,234.5") == 1234.5  # noqa: SLF001
    assert coord._coerce_optional_float("bad") is None  # noqa: SLF001
    assert coord._coerce_optional_float(object()) is None  # noqa: SLF001
    assert coord._coerce_optional_text(BadStr()) is None  # noqa: SLF001

    assert coord._parse_percent_value(None) is None  # noqa: SLF001
    assert coord._parse_percent_value(True) == 1.0  # noqa: SLF001
    assert coord._parse_percent_value(ExplodingFloat(2.0)) is None  # noqa: SLF001
    assert coord._parse_percent_value(object()) is None  # noqa: SLF001
    assert coord._parse_percent_value(BadStrip("10%")) is None  # noqa: SLF001
    assert coord._parse_percent_value(" ") is None  # noqa: SLF001
    assert coord._parse_percent_value("not-a-number") is None  # noqa: SLF001
    assert coord._parse_percent_value("48%") == 48.0  # noqa: SLF001

    assert coord._normalize_battery_status_text(None) is None  # noqa: SLF001
    assert coord._normalize_battery_status_text(BadStr()) is None  # noqa: SLF001
    assert coord._normalize_battery_status_text("   ") is None  # noqa: SLF001
    assert coord._normalize_battery_status_text("---___") is None  # noqa: SLF001
    assert coord._normalize_battery_status_text("critical") == "error"  # noqa: SLF001
    assert coord._normalize_battery_status_text("warning") == "warning"  # noqa: SLF001
    assert coord._normalize_battery_status_text("abnormal") == "warning"  # noqa: SLF001
    assert (
        coord._normalize_battery_status_text("not reporting") == "warning"
    )  # noqa: SLF001
    assert (
        coord._normalize_battery_status_text("not normal") == "warning"
    )  # noqa: SLF001
    assert coord._normalize_battery_status_text("mystery") == "unknown"  # noqa: SLF001

    assert coord._battery_status_severity_value(None) >= 0  # noqa: SLF001
    assert coord._battery_storage_key({"id": "7"}) == "id_7"  # noqa: SLF001
    assert coord._battery_storage_key({}) is None  # noqa: SLF001
    assert coord._normalize_battery_id(107247437) == "107247437"  # noqa: SLF001
    assert coord._normalize_battery_id(42.0) == "42"  # noqa: SLF001
    assert coord._normalize_battery_id("107,247,437") == "107247437"  # noqa: SLF001
    assert coord._normalize_battery_id(" +42 ") == "+42"  # noqa: SLF001
    assert coord._normalize_battery_id(1.5) is None  # noqa: SLF001
    assert coord._normalize_battery_id(object()) is None  # noqa: SLF001
    assert coord._normalize_battery_id(BadStrip("7")) is None  # noqa: SLF001
    assert coord._normalize_battery_id("   ") is None  # noqa: SLF001
    assert coord._normalize_battery_id(True) is None  # noqa: SLF001
    assert coord._normalize_battery_id("bad-id") is None  # noqa: SLF001


def test_battery_status_property_edge_cases(coordinator_factory) -> None:
    coord = coordinator_factory()

    class BadFloat:
        def __float__(self):
            raise ValueError("boom")

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    coord._battery_aggregate_charge_pct = BadFloat()  # noqa: SLF001
    assert coord.battery_aggregate_charge_pct is None
    coord._battery_aggregate_status = BadStr()  # noqa: SLF001
    assert coord.battery_aggregate_status is None
    coord._battery_aggregate_status_details = "bad"  # noqa: SLF001
    assert coord.battery_aggregate_status_details == {}
    coord._battery_status_payload = "bad"  # noqa: SLF001
    assert coord.battery_status_payload is None

    coord._battery_aggregate_status_details = {"included_count": 1}  # noqa: SLF001
    coord._battery_aggregate_charge_pct = 10  # noqa: SLF001
    coord._battery_aggregate_status = "normal"  # noqa: SLF001
    summary = coord.battery_status_summary
    assert summary["aggregate_charge_pct"] == 10.0
    assert summary["aggregate_status"] == "normal"
    assert summary["battery_order"] == []


def test_battery_serial_and_storage_edge_cases(coordinator_factory) -> None:
    coord = coordinator_factory()

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    coord._battery_storage_order = ("bad",)  # type: ignore[assignment]  # noqa: SLF001
    coord._battery_storage_data = {}  # noqa: SLF001
    assert coord.iter_battery_serials() == []

    coord._battery_storage_order = [BadStr(), "MISSING", "BAT-1"]  # noqa: SLF001
    coord._battery_storage_data = {"BAT-1": {"identity": "BAT-1"}}  # noqa: SLF001
    assert coord.iter_battery_serials() == ["BAT-1"]

    coord._battery_storage_data = None  # type: ignore[assignment]  # noqa: SLF001
    assert coord.battery_storage("BAT-1") is None
    coord._battery_storage_data = {"BAT-1": {"identity": "BAT-1"}}  # noqa: SLF001
    assert coord.battery_storage(BadStr()) is None
    assert coord.battery_storage("   ") is None
