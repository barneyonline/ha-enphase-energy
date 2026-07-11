"""Tests for the optional Enphase weather platform."""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Coroutine
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from homeassistant.const import UnitOfTemperature

from custom_components.enphase_ev.const import OPT_WEATHER_ENABLED
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from custom_components.enphase_ev import weather as weather_module
from custom_components.enphase_ev.weather import (
    EnphaseSiteWeather,
    EnphaseWeatherCoordinator,
    _async_discover_weather,
    _condition,
    _normalize_weather,
    _number,
    _optional_text,
    async_setup_entry,
)
from tests.components.enphase_ev.random_ids import RANDOM_SITE_ID


def _payload(
    *,
    code: str = "cloudy",
    description: str = "MostlyCloudy",
    display: str = "8°C",
) -> dict[str, object]:
    return {
        "string": description,
        "code": code,
        "temperature": {
            "value": 8,
            "min": 6,
            "max": 11,
            "display": display,
        },
    }


def test_normalize_weather_payload_and_condition_aliases() -> None:
    data = _normalize_weather(_payload())

    assert data is not None
    assert data.condition == "cloudy"
    assert data.temperature == 8
    assert data.temperature_unit == UnitOfTemperature.CELSIUS
    assert _condition("clear_night") == "clear-night"
    assert _condition("Heavy Rain") == "pouring"
    assert _condition("not-observed") is None
    assert _condition(None) is None


def test_normalize_weather_supports_fahrenheit_and_description_fallback() -> None:
    data = _normalize_weather(
        {
            "string": "PartlyCloudy",
            "code": "unknown-code",
            "temperature": {
                "value": "72.5",
                "min": "bad",
                "max": None,
                "unit": "fahrenheit",
                "display": "72.5",
            },
        }
    )

    assert data is not None
    assert data.condition == "partlycloudy"
    assert data.temperature == 72.5
    assert data.temperature_unit == UnitOfTemperature.FAHRENHEIT


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"temperature": []},
        {"temperature": {"value": True}},
        {"temperature": {"value": float("inf")}},
    ],
)
def test_normalize_weather_rejects_invalid_payloads(payload: object) -> None:
    assert _normalize_weather(payload) is None


def test_weather_scalar_helpers_handle_invalid_values() -> None:
    class BadString:
        def __str__(self) -> str:
            raise RuntimeError

    assert _number("bad") is None
    assert _number(None) is None
    assert _number(False) is None
    assert _optional_text(BadString()) is None
    assert _optional_text("  ") is None


@pytest.mark.asyncio
async def test_setup_skips_weather_when_option_disabled(hass, config_entry) -> None:
    client = SimpleNamespace(weather=AsyncMock())
    coordinator = SimpleNamespace(site_id=RANDOM_SITE_ID, client=client)
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coordinator)
    added: list[EnphaseSiteWeather] = []

    await async_setup_entry(hass, config_entry, added.extend)

    assert added == []
    client.weather.assert_not_awaited()


@pytest.mark.asyncio
async def test_setup_schedules_discovery_and_creates_cloud_weather_after_success(
    hass, config_entry, monkeypatch
) -> None:
    client = SimpleNamespace(weather=AsyncMock(return_value=_payload()))
    coordinator = SimpleNamespace(site_id=RANDOM_SITE_ID, client=client)
    object.__setattr__(config_entry, "options", {OPT_WEATHER_ENABLED: True})
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coordinator)
    hass.config.language = "en-AU"
    added: list[EnphaseSiteWeather] = []
    scheduled: list[tuple[Coroutine[Any, Any, None], str]] = []

    def _capture_background_task(
        _hass,
        target: Coroutine[Any, Any, None],
        name: str,
        eager_start: bool = True,
    ):
        assert eager_start is True
        scheduled.append((target, name))
        return MagicMock()

    monkeypatch.setattr(
        config_entry, "async_create_background_task", _capture_background_task
    )

    await async_setup_entry(hass, config_entry, added.extend)

    assert added == []
    client.weather.assert_not_awaited()
    assert len(scheduled) == 1
    assert scheduled[0][1] == "enphase_ev_weather_discovery"

    await scheduled[0][0]

    assert len(added) == 1
    entity = added[0]
    assert entity.unique_id == f"enphase_ev_site_{RANDOM_SITE_ID}_weather"
    assert entity.condition == "cloudy"
    assert entity.native_temperature == 8
    assert entity.native_temperature_unit == UnitOfTemperature.CELSIUS
    entity.hass = hass
    state_attributes = entity.state_attributes
    assert state_attributes["temperature"] == 8
    assert state_attributes["temperature_unit"] == UnitOfTemperature.CELSIUS
    assert "temperature_min" not in state_attributes
    assert "temperature_max" not in state_attributes
    assert "enphase_condition" not in state_attributes
    assert entity.device_info["identifiers"] == {
        ("enphase_ev", f"type:{RANDOM_SITE_ID}:cloud")
    }
    client.weather.assert_awaited_once_with(locale="en-AU")


@pytest.mark.asyncio
async def test_discovery_retries_transient_failures_with_bounded_backoff(
    hass, monkeypatch
) -> None:
    server_error = aiohttp.ClientResponseError(
        MagicMock(real_url="https://example.invalid/weather"),
        (),
        status=500,
        message="server error",
    )
    client = SimpleNamespace(
        weather=AsyncMock(
            side_effect=[
                server_error,
                aiohttp.ClientError("offline"),
                TimeoutError(),
                {"temperature": {"value": "bad"}},
                aiohttp.ClientError("offline"),
                _payload(),
            ]
        )
    )
    coordinator = EnphaseWeatherCoordinator(hass, client, locale="en")
    added: list[EnphaseSiteWeather] = []
    sleep = AsyncMock()
    monkeypatch.setattr(weather_module.asyncio, "sleep", sleep)

    await _async_discover_weather(
        coordinator,
        site_id=RANDOM_SITE_ID,
        async_add_entities=added.extend,
    )

    assert len(added) == 1
    assert client.weather.await_count == 6
    assert [call.args[0] for call in sleep.await_args_list] == [
        60.0,
        300.0,
        900.0,
        1800.0,
        1800.0,
    ]


@pytest.mark.asyncio
async def test_discovery_stops_when_endpoint_is_unsupported(hass, monkeypatch) -> None:
    unsupported = aiohttp.ClientResponseError(
        MagicMock(real_url="https://example.invalid/weather"),
        (),
        status=404,
        message="not found",
    )
    client = SimpleNamespace(weather=AsyncMock(side_effect=unsupported))
    coordinator = EnphaseWeatherCoordinator(hass, client, locale="en")
    added: list[EnphaseSiteWeather] = []
    sleep = AsyncMock()
    monkeypatch.setattr(weather_module.asyncio, "sleep", sleep)

    await _async_discover_weather(
        coordinator,
        site_id=RANDOM_SITE_ID,
        async_add_entities=added.extend,
    )

    assert added == []
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_weather_coordinator_uses_optional_scope_and_retains_data_on_failure(
    hass, monkeypatch
) -> None:
    optional_scope_active = False

    @contextmanager
    def _optional_scope():
        nonlocal optional_scope_active
        optional_scope_active = True
        try:
            yield
        finally:
            optional_scope_active = False

    async def _weather(*, locale: str):
        assert locale == "en"
        assert optional_scope_active is True
        if client.weather.await_count == 1:
            return _payload()
        raise aiohttp.ClientError("offline")

    monkeypatch.setattr(
        weather_module, "enlighten_optional_read_scope", _optional_scope
    )
    client = SimpleNamespace(weather=AsyncMock(side_effect=_weather))
    coordinator = EnphaseWeatherCoordinator(hass, client, locale="en")

    assert await coordinator.async_probe() is True
    original = coordinator.data
    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert coordinator.data is original
