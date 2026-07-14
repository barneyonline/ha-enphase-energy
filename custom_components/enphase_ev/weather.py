"""Expose optional Enphase site weather as a Home Assistant weather entity."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
import logging
import math
import re
from typing import Any, cast

import aiohttp
from homeassistant.components.weather import WeatherEntity
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import (
    InvalidPayloadError,
    Unauthorized,
    enlighten_optional_read_scope,
)
from .const import DOMAIN, OPT_WEATHER_ENABLED
from .device_info_helpers import _cloud_device_info
from .log_redaction import redact_site_id, redact_text
from .runtime_data import EnphaseConfigEntry, get_runtime_data

_LOGGER = logging.getLogger(__name__)

_WEATHER_UPDATE_INTERVAL = timedelta(minutes=15)
_WEATHER_DISCOVERY_BACKOFF_S = (60.0, 300.0, 900.0, 1800.0)
_WEATHER_UNSUPPORTED_STATUSES = frozenset({404, 405, 410})
_VALID_CONDITIONS = frozenset(
    {
        "clear-night",
        "cloudy",
        "exceptional",
        "fog",
        "hail",
        "lightning",
        "lightning-rainy",
        "partlycloudy",
        "pouring",
        "rainy",
        "snowy",
        "snowy-rainy",
        "sunny",
        "windy",
        "windy-variant",
    }
)
_CONDITION_ALIASES = {
    "clear": "sunny",
    "clearday": "sunny",
    "clearnight": "clear-night",
    "fair": "sunny",
    "mostlyclear": "sunny",
    "mostlycloudy": "cloudy",
    "overcast": "cloudy",
    "partlycloudy": "partlycloudy",
    "scatteredclouds": "partlycloudy",
    "drizzle": "rainy",
    "rain": "rainy",
    "showers": "rainy",
    "heavyrain": "pouring",
    "snow": "snowy",
    "sleet": "snowy-rainy",
    "thunderstorm": "lightning-rainy",
}


@dataclass(frozen=True, slots=True)
class EnphaseWeatherData:
    """Normalized weather values returned by Enlighten."""

    condition: str | None
    temperature: float
    temperature_unit: str


class WeatherEndpointUnsupported(Exception):
    """Signal that Enlighten does not provide weather for this site."""


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:  # noqa: BLE001
        return None
    return text or None


def _condition(value: object) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    direct = text.lower().replace("_", "-").replace(" ", "-")
    if direct in _VALID_CONDITIONS:
        return direct
    compact = re.sub(r"[^a-z0-9]", "", text.lower())
    return _CONDITION_ALIASES.get(compact)


def _temperature_unit(temperature: dict[str, object]) -> str:
    display = _optional_text(temperature.get("display")) or ""
    unit = _optional_text(temperature.get("unit")) or ""
    marker = f"{display} {unit}".upper()
    if "°F" in marker or "FAHRENHEIT" in marker or marker.strip() == "F":
        return cast(str, UnitOfTemperature.FAHRENHEIT)
    return cast(str, UnitOfTemperature.CELSIUS)


def _normalize_weather(payload: object) -> EnphaseWeatherData | None:
    if not isinstance(payload, dict):
        return None
    temperature = payload.get("temperature")
    if not isinstance(temperature, dict):
        return None
    current = _number(temperature.get("value"))
    if current is None:
        return None
    raw_condition = _optional_text(payload.get("string"))
    normalized_condition = _condition(payload.get("code")) or _condition(raw_condition)
    return EnphaseWeatherData(
        condition=normalized_condition,
        temperature=current,
        temperature_unit=_temperature_unit(temperature),
    )


class EnphaseWeatherCoordinator(
    DataUpdateCoordinator[EnphaseWeatherData],  # type: ignore[misc]
):
    """Poll the optional Enlighten weather endpoint independently."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: Any,
        *,
        locale: str,
        site_id: str | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_weather",
            update_interval=_WEATHER_UPDATE_INTERVAL,
        )
        self._client = client
        self._locale = locale
        self._site_id = site_id
        self._discovery_state = "pending"
        self._discovery_failures = 0

    async def _async_update_data(self) -> EnphaseWeatherData:
        try:
            with enlighten_optional_read_scope():
                payload = await self._client.weather(locale=self._locale)
        except aiohttp.ClientResponseError as err:
            if err.status in _WEATHER_UNSUPPORTED_STATUSES:
                raise WeatherEndpointUnsupported from err
            raise UpdateFailed(str(err) or err.__class__.__name__) from err
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            InvalidPayloadError,
            Unauthorized,
        ) as err:
            raise UpdateFailed(str(err) or err.__class__.__name__) from err
        normalized = _normalize_weather(payload)
        if normalized is None:
            raise UpdateFailed("Weather payload is missing a valid temperature")
        return normalized

    async def async_probe(self) -> bool:
        """Seed the coordinator only when the optional endpoint succeeds."""

        try:
            data = await self._async_update_data()
        except UpdateFailed:
            self._discovery_state = "retrying"
            self._discovery_failures += 1
            return False
        self.async_set_updated_data(data)
        self._discovery_state = "available"
        return True

    def mark_unsupported(self) -> None:
        """Record that the optional endpoint is unsupported for this site."""

        self._discovery_state = "unsupported"

    def mark_stopped(self) -> None:
        """Record explicit config-entry lifecycle shutdown."""

        self._discovery_state = "stopped"

    def diagnostics(self) -> dict[str, object]:
        """Return a redacted health snapshot for config-entry diagnostics."""

        last_exception = getattr(self, "last_exception", None)
        safe_error = None
        if last_exception is not None:
            safe_error = redact_text(
                last_exception,
                site_ids=((self._site_id,) if self._site_id else ()),
            )
        return {
            "role": "child_coordinator",
            "discovery_state": self._discovery_state,
            "discovery_failures": self._discovery_failures,
            "last_update_success": bool(self.last_update_success),
            "last_error": safe_error,
            "update_interval_seconds": int(_WEATHER_UPDATE_INTERVAL.total_seconds()),
        }


class EnphaseSiteWeather(
    CoordinatorEntity[EnphaseWeatherCoordinator],  # type: ignore[misc]
    WeatherEntity,  # type: ignore[misc]
):
    """Current weather observed by Enphase for the configured site."""

    _attr_has_entity_name = True
    _attr_translation_key = "site_weather"

    def __init__(
        self,
        coordinator: EnphaseWeatherCoordinator,
        *,
        site_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._weather_coordinator = coordinator
        self._site_id = site_id
        self._attr_unique_id = f"{DOMAIN}_site_{site_id}_weather"

    @property
    def _weather_data(self) -> EnphaseWeatherData:
        return cast(EnphaseWeatherData, self._weather_coordinator.data)

    @property
    def condition(self) -> str | None:
        return self._weather_data.condition

    @property
    def native_temperature(self) -> float:
        return self._weather_data.temperature

    @property
    def native_temperature_unit(self) -> str:
        return self._weather_data.temperature_unit

    @property
    def device_info(self) -> Any:
        return _cloud_device_info(self._site_id)


async def _async_discover_weather(
    coordinator: EnphaseWeatherCoordinator,
    *,
    site_id: str,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Retry optional weather discovery until it succeeds or is unsupported."""

    failures = 0
    while True:
        try:
            available = await coordinator.async_probe()
        except WeatherEndpointUnsupported:
            coordinator.mark_unsupported()
            _LOGGER.debug(
                "Weather endpoint unsupported for site %s; discovery stopped",
                redact_site_id(site_id),
            )
            return
        if available:
            _LOGGER.debug(
                "Weather endpoint available for site %s",
                redact_site_id(site_id),
            )
            async_add_entities([EnphaseSiteWeather(coordinator, site_id=site_id)])
            return

        delay = _WEATHER_DISCOVERY_BACKOFF_S[
            min(failures, len(_WEATHER_DISCOVERY_BACKOFF_S) - 1)
        ]
        failures += 1
        _LOGGER.debug(
            "Weather endpoint unavailable for site %s; retrying in %s seconds",
            redact_site_id(site_id),
            int(delay),
        )
        await asyncio.sleep(delay)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create weather only after an enabled endpoint probe succeeds."""

    runtime_data = get_runtime_data(entry)
    main_coordinator = runtime_data.coordinator
    site_id = str(main_coordinator.site_id)
    if not bool(entry.options.get(OPT_WEATHER_ENABLED, False)):
        await runtime_data.async_stop_weather()
        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "weather",
            DOMAIN,
            f"{DOMAIN}_site_{site_id}_weather",
        )
        if entity_id is not None:
            ent_reg.async_remove(entity_id)
        return
    locale = str(getattr(hass.config, "language", "en") or "en")
    coordinator = EnphaseWeatherCoordinator(
        hass,
        main_coordinator.client,
        locale=locale,
        site_id=site_id,
    )
    runtime_data.weather_coordinator = coordinator
    task = entry.async_create_background_task(
        hass,
        _async_discover_weather(
            coordinator,
            site_id=site_id,
            async_add_entities=async_add_entities,
        ),
        f"{DOMAIN}_weather_discovery",
    )
    track_background_task = getattr(
        main_coordinator, "track_entry_background_task", None
    )
    if callable(track_background_task):
        track_background_task(task)
    runtime_data.weather_discovery_task = task
