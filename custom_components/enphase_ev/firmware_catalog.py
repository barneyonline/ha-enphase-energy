from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .log_redaction import redact_text
from .runtime_helpers import (
    iso_or_none as _iso_or_none,
    monotonic_deadline_to_utc_iso as _mono_to_utc_iso,
)

_LOGGER = logging.getLogger(__name__)
_DOC_CENTER_PRODUCT_TYPE = "216"

DEFAULT_FIRMWARE_CATALOG_URL = (
    "https://raw.githubusercontent.com/barneyonline/ha-enphase-energy/"
    "firmware-catalog/catalog/v1/runtime_catalog.json"
)
FIRMWARE_CATALOG_URL_ENV = "ENPHASE_EV_FIRMWARE_CATALOG_URL"
FIRMWARE_CATALOG_CACHE_TTL_SECONDS = 12 * 60 * 60
FIRMWARE_CATALOG_RETRY_BACKOFF_SECONDS = 30 * 60
FIRMWARE_CATALOG_FETCH_TIMEOUT_SECONDS = 20


@dataclass(slots=True)
class CatalogSelection:
    entry: dict[str, Any] | None
    locale_used: str
    country_used: str | None
    source_scope: str | None


class FirmwareCatalogManager:
    """Cached runtime catalog manager with stale-on-error behavior."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        url: str | None = None,
        ttl_seconds: int = FIRMWARE_CATALOG_CACHE_TTL_SECONDS,
        retry_backoff_seconds: int = FIRMWARE_CATALOG_RETRY_BACKOFF_SECONDS,
        fetch_timeout_seconds: int = FIRMWARE_CATALOG_FETCH_TIMEOUT_SECONDS,
    ) -> None:
        self._hass = hass
        self._url = str(url or _firmware_catalog_url())
        self._ttl_seconds = max(300, int(ttl_seconds))
        self._retry_backoff_seconds = max(60, int(retry_backoff_seconds))
        self._fetch_timeout_seconds = max(5, int(fetch_timeout_seconds))

        self._catalog: dict[str, Any] | None = None
        self._expires_mono: float = 0.0
        self._last_fetch_utc: datetime | None = None
        self._last_success_utc: datetime | None = None
        self._last_error: str | None = None
        self._using_stale = False
        self._lock = asyncio.Lock()

    @property
    def cached_catalog(self) -> dict[str, Any] | None:
        return self._catalog

    async def async_get_catalog(
        self, *, force_refresh: bool = False
    ) -> dict[str, Any] | None:
        now = time.monotonic()
        if not force_refresh and now < self._expires_mono:
            return self._catalog

        async with self._lock:
            now = time.monotonic()
            if not force_refresh and now < self._expires_mono:
                return self._catalog

            self._last_fetch_utc = dt_util.utcnow()
            session = async_get_clientsession(self._hass)
            try:
                async with session.get(
                    self._url,
                    timeout=self._fetch_timeout_seconds,
                ) as response:
                    if response.status >= 400:
                        raise RuntimeError(f"HTTP {response.status}")
                    payload = await response.json(content_type=None)
                catalog = _validate_catalog(payload)
            except Exception as err:  # noqa: BLE001
                self._last_error = redact_text(err)
                self._using_stale = self._catalog is not None
                backoff = self._retry_backoff_seconds
                self._expires_mono = time.monotonic() + backoff
                _LOGGER.debug("Firmware catalog refresh failed: %s", self._last_error)
                return self._catalog

            self._catalog = catalog
            self._last_success_utc = dt_util.utcnow()
            self._last_error = None
            self._using_stale = False
            self._expires_mono = time.monotonic() + self._ttl_seconds
            return self._catalog

    def status_snapshot(self) -> dict[str, Any]:
        generated_at = _catalog_generated_at(self._catalog)
        source_age_seconds = _source_age_seconds(generated_at)
        return {
            "url": self._url,
            "cache_ttl_seconds": self._ttl_seconds,
            "cache_expires_utc": _mono_to_utc_iso(self._expires_mono),
            "last_fetch_utc": _iso_or_none(self._last_fetch_utc),
            "last_success_utc": _iso_or_none(self._last_success_utc),
            "last_error": self._last_error,
            "using_stale": self._using_stale,
            "catalog_generated_at": generated_at,
            "catalog_source_age_seconds": source_age_seconds,
        }


def _firmware_catalog_url() -> str:
    override = os.getenv(FIRMWARE_CATALOG_URL_ENV)
    if isinstance(override, str) and override.strip():
        return override.strip()
    return DEFAULT_FIRMWARE_CATALOG_URL


def _validate_catalog(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("catalog payload must be an object")
    if int(payload.get("schema_version", 0) or 0) != 1:
        raise ValueError("unsupported schema_version")
    devices = payload.get("devices")
    if not isinstance(devices, dict):
        raise ValueError("catalog devices missing")
    return payload


def _catalog_generated_at(catalog: dict[str, Any] | None) -> str | None:
    if not isinstance(catalog, dict):
        return None
    value = catalog.get("generated_at")
    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:  # noqa: BLE001
        return None
    return text or None


def _source_age_seconds(generated_at: str | None) -> float | None:
    if not generated_at:
        return None
    parsed = _parse_iso_datetime(generated_at)
    if parsed is None:
        return None
    age = (dt_util.utcnow() - parsed).total_seconds()
    if age < 0:
        return 0.0
    return float(round(age, 1))


def _parse_iso_datetime(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.UTC)
    return parsed


def normalize_locale(value: Any) -> str:
    if value is None:
        return "en"
    try:
        text = str(value).strip().lower().replace("_", "-")
    except Exception:  # noqa: BLE001
        return "en"
    return text or "en"


def normalize_country(value: Any) -> str | None:
    if value is None:
        return None
    try:
        text = str(value).strip().upper()
    except Exception:  # noqa: BLE001
        return None
    if len(text) == 2 and text.isalpha():
        return text
    return None


def resolve_country_and_locale(
    coord: object, hass: HomeAssistant
) -> tuple[str | None, str]:
    raw_battery_locale = getattr(coord, "battery_locale", None)
    raw_hass_locale = getattr(getattr(hass, "config", None), "language", None)
    battery_locale = (
        normalize_locale(raw_battery_locale) if raw_battery_locale is not None else None
    )
    hass_locale = (
        normalize_locale(raw_hass_locale) if raw_hass_locale is not None else None
    )

    resolved_locale = battery_locale or hass_locale or "en"

    country_candidates = [
        normalize_country(getattr(coord, "battery_country_code", None)),
        normalize_country(getattr(getattr(hass, "config", None), "country", None)),
    ]
    resolved_country = next((item for item in country_candidates if item), None)
    return resolved_country, resolved_locale


def normalize_version_token(value: Any) -> str | None:
    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:  # noqa: BLE001
        return None
    if not text:
        return None

    # Keep conservative: only normalized dotted numeric versions are comparable.
    dotted_match = re.search(r"(?<!\d)(\d+(?:\.\d+)+)(?!\d)", text)
    if dotted_match:
        return dotted_match.group(1)

    return None


def compare_versions(latest: str | None, installed: str | None) -> bool | None:
    latest_parts = _parse_version_parts(latest)
    installed_parts = _parse_version_parts(installed)
    if latest_parts is None or installed_parts is None:
        return None
    max_len = max(len(latest_parts), len(installed_parts))
    latest_parts += (0,) * (max_len - len(latest_parts))
    installed_parts += (0,) * (max_len - len(installed_parts))
    return latest_parts > installed_parts


def _parse_version_parts(value: str | None) -> tuple[int, ...] | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    segments = text.split(".")
    parts: list[int] = []
    for segment in segments:
        if not segment.isdigit():
            return None
        parts.append(int(segment))
    return tuple(parts)


def select_catalog_entry(
    catalog: dict[str, Any] | None,
    *,
    device_type: str,
    country: str | None,
    locale: str,
    prefer_global: bool = False,
) -> CatalogSelection:
    normalized_locale = normalize_locale(locale)
    country_code = normalize_country(country)

    if not isinstance(catalog, dict):
        return CatalogSelection(None, normalized_locale, country_code, None)

    devices = catalog.get("devices")
    if not isinstance(devices, dict):
        return CatalogSelection(None, normalized_locale, country_code, None)

    device_payload = devices.get(device_type)
    if not isinstance(device_payload, dict):
        return CatalogSelection(None, normalized_locale, country_code, None)

    entry = None
    source_scope = None

    by_locale = device_payload.get("latest_by_locale")
    if not prefer_global:
        if country_code:
            by_country = device_payload.get("latest_by_country")
            if isinstance(by_country, dict):
                candidate = by_country.get(country_code)
                if isinstance(candidate, dict):
                    entry = candidate
                    source_scope = "country"
                    locale_candidate = _exact_locale_entry(by_locale, normalized_locale)
                    if _same_catalog_release(entry, locale_candidate):
                        entry = _with_merged_locale_urls(entry, locale_candidate)

    if entry is None:
        latest_global = device_payload.get("latest_global")
        if _is_worldwide_catalog_entry(latest_global):
            entry = latest_global
            source_scope = "global"

    if entry is None:
        return CatalogSelection(None, normalized_locale, country_code, source_scope)

    entry = _repair_legacy_release_urls(device_payload, entry)

    urls_by_locale = entry.get("urls_by_locale")
    locale_used = normalized_locale
    if isinstance(urls_by_locale, dict) and urls_by_locale:
        if normalized_locale in urls_by_locale:
            locale_used = normalized_locale
        else:
            base_language = normalized_locale.split("-", 1)[0]
            fallback = next(
                (
                    key
                    for key in urls_by_locale
                    if str(key).split("-", 1)[0] == base_language
                ),
                None,
            )
            if fallback:
                locale_used = str(fallback)
            elif "en" in urls_by_locale:
                locale_used = "en"
            else:
                locale_used = str(next(iter(urls_by_locale.keys())))

    return CatalogSelection(entry, locale_used, country_code, source_scope)


def _exact_locale_entry(
    by_locale: object, normalized_locale: str
) -> dict[str, Any] | None:
    if not isinstance(by_locale, dict):
        return None
    candidate = by_locale.get(normalized_locale)
    return candidate if isinstance(candidate, dict) else None


def _same_catalog_release(
    entry: dict[str, Any] | None, candidate: dict[str, Any] | None
) -> bool:
    if not isinstance(entry, dict) or not isinstance(candidate, dict):
        return False
    entry_media_id = _text_or_none(entry.get("media_id"))
    candidate_media_id = _text_or_none(candidate.get("media_id"))
    if entry_media_id and candidate_media_id:
        return entry_media_id == candidate_media_id
    entry_version = normalize_version_token(entry.get("version"))
    candidate_version = normalize_version_token(candidate.get("version"))
    return entry_version is not None and entry_version == candidate_version


def _with_merged_locale_urls(
    entry: dict[str, Any], locale_entry: dict[str, Any] | None
) -> dict[str, Any]:
    if not isinstance(locale_entry, dict):
        return entry
    urls = entry.get("urls_by_locale")
    locale_urls = locale_entry.get("urls_by_locale")
    if not isinstance(locale_urls, dict) or not locale_urls:
        return entry
    merged = dict(urls) if isinstance(urls, dict) else {}
    merged.update(locale_urls)
    cloned = dict(entry)
    cloned["urls_by_locale"] = merged
    return cloned


def _is_worldwide_catalog_entry(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    countries_text = _text_or_none(entry.get("countries_text"))
    if countries_text is None:
        return False
    lowered = countries_text.lower()
    if "except" in lowered:
        return False
    return (
        "worldwide" in lowered
        or "global" in lowered
        or lowered.strip() == "all countries"
    )


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:  # noqa: BLE001
        return None
    return text or None


def _repair_legacy_release_urls(
    device_payload: dict[str, Any], entry: dict[str, Any]
) -> dict[str, Any]:
    urls_by_locale = entry.get("urls_by_locale")
    if not isinstance(urls_by_locale, dict) or not urls_by_locale:
        return entry

    if not any(_is_legacy_release_url(value) for value in urls_by_locale.values()):
        return entry

    topic_id = _as_int(entry.get("document_topic_id")) or _as_int(
        device_payload.get("document_topic_id")
    )
    product_media_name_id = _as_int(entry.get("product_media_name_id")) or _as_int(
        device_payload.get("product_media_name_id")
    )
    product_type = str(
        entry.get("product_type")
        or device_payload.get("product_type")
        or _DOC_CENTER_PRODUCT_TYPE
    )
    if topic_id is None or product_media_name_id is None:
        return entry

    repaired_urls: dict[str, str] = {}
    for locale_key, raw_url in urls_by_locale.items():
        locale = normalize_locale(locale_key)
        if not _is_legacy_release_url(raw_url):
            repaired_urls[str(locale_key)] = str(raw_url)
            continue
        repaired_urls[str(locale_key)] = _build_release_url(
            raw_url=str(raw_url),
            locale=locale,
            product_type=product_type,
            topic_id=topic_id,
            product_media_name_id=product_media_name_id,
        )

    cloned = dict(entry)
    cloned["urls_by_locale"] = repaired_urls
    return cloned


def _is_legacy_release_url(value: Any) -> bool:
    if value is None:
        return False
    try:
        text = str(value)
    except Exception:  # noqa: BLE001
        return False
    return "media_id=" in text and "langcode=" in text


def _build_release_url(
    *,
    raw_url: str,
    locale: str,
    product_type: str,
    topic_id: int,
    product_media_name_id: int,
) -> str:
    split = urlsplit(raw_url)
    query = urlencode(
        {
            "product_type": product_type,
            "f[0]": f"document:{topic_id}",
            "f[1]": f"product_media_name:{product_media_name_id}",
            "search_api_language": locale,
            "field_alternative_language": locale,
        }
    )
    return urlunsplit((split.scheme, split.netloc, split.path, query, ""))


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value))
    except Exception:  # noqa: BLE001
        return None
