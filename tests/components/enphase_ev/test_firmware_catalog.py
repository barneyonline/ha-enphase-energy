from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import time
from types import SimpleNamespace

import pytest

from custom_components.enphase_ev import firmware_catalog


class _FakeResponse:
    def __init__(self, *, status: int, payload, headers=None):
        self.status = status
        self._payload = payload
        self.headers = headers

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self, content_type=None):  # noqa: ARG002
        return self._payload


class _FakeSession:
    def __init__(self, actions):
        self._actions = list(actions)

    def get(self, _url, timeout):  # noqa: ARG002
        if not self._actions:
            raise RuntimeError("no fake actions left")
        action = self._actions.pop(0)
        if isinstance(action, Exception):
            raise action
        return action


@pytest.mark.asyncio
async def test_catalog_manager_caches_and_uses_stale_on_error(monkeypatch) -> None:
    payload = {
        "schema_version": 1,
        "generated_at": "2026-03-01T00:00:00Z",
        "devices": {"envoy": {}},
    }
    fake_session = _FakeSession(
        [
            _FakeResponse(status=200, payload=payload),
            RuntimeError("site_id=12345 email=user@example.com ip=10.0.0.2"),
        ]
    )
    monkeypatch.setattr(
        firmware_catalog,
        "async_get_clientsession",
        lambda _hass: fake_session,
    )

    manager = firmware_catalog.FirmwareCatalogManager(SimpleNamespace())

    first = await manager.async_get_catalog(force_refresh=True)
    assert first == payload

    cached = await manager.async_get_catalog()
    assert cached == payload

    stale = await manager.async_get_catalog(force_refresh=True)
    assert stale == payload

    status = manager.status_snapshot()
    assert status["using_stale"] is True
    assert status["last_error"] == "site_id=[site] email=[redacted] ip=[redacted]"
    assert status["catalog_generated_at"] == "2026-03-01T00:00:00Z"
    assert manager.cached_catalog == payload


@pytest.mark.asyncio
async def test_catalog_manager_handles_http_error_and_validation_errors(
    monkeypatch,
) -> None:
    bad_payload = {"schema_version": 2, "devices": {}}
    fake_session = _FakeSession(
        [
            _FakeResponse(status=503, payload={}),
            _FakeResponse(status=200, payload=bad_payload),
        ]
    )
    monkeypatch.setattr(
        firmware_catalog,
        "async_get_clientsession",
        lambda _hass: fake_session,
    )
    manager = firmware_catalog.FirmwareCatalogManager(SimpleNamespace(), ttl_seconds=1)

    assert await manager.async_get_catalog(force_refresh=True) is None
    assert manager.status_snapshot()["last_error"] == "HTTP 503"

    assert await manager.async_get_catalog(force_refresh=True) is None
    assert "unsupported schema_version" in (
        manager.status_snapshot()["last_error"] or ""
    )


@pytest.mark.asyncio
async def test_catalog_manager_cold_start_failure_honors_backoff(monkeypatch) -> None:
    fake_session = _FakeSession([RuntimeError("email=user@example.com")])
    monkeypatch.setattr(
        firmware_catalog,
        "async_get_clientsession",
        lambda _hass: fake_session,
    )

    manager = firmware_catalog.FirmwareCatalogManager(
        SimpleNamespace(),
        retry_backoff_seconds=1800,
    )

    assert await manager.async_get_catalog(force_refresh=True) is None
    assert await manager.async_get_catalog() is None
    assert manager.status_snapshot()["last_error"] == "email=[redacted]"


def test_catalog_manager_uses_env_override(monkeypatch) -> None:
    monkeypatch.setenv(
        firmware_catalog.FIRMWARE_CATALOG_URL_ENV,
        "http://127.0.0.1:8123/local/catalog/v1/runtime_catalog.json",
    )

    manager = firmware_catalog.FirmwareCatalogManager(SimpleNamespace())

    assert manager.status_snapshot()["url"] == (
        "http://127.0.0.1:8123/local/catalog/v1/runtime_catalog.json"
    )


@pytest.mark.asyncio
async def test_catalog_manager_lock_recheck_returns_cached_without_fetch(
    monkeypatch,
) -> None:
    payload = {
        "schema_version": 1,
        "generated_at": "2026-03-01T00:00:00Z",
        "devices": {"envoy": {}},
    }
    manager = firmware_catalog.FirmwareCatalogManager(SimpleNamespace())

    class _RaceLock:
        async def __aenter__(self_inner):
            manager._catalog = payload
            manager._expires_mono = time.monotonic() + 60
            return None

        async def __aexit__(self_inner, exc_type, exc, tb):
            return False

    manager._lock = _RaceLock()
    monkeypatch.setattr(
        firmware_catalog,
        "async_get_clientsession",
        lambda _hass: (_ for _ in ()).throw(AssertionError("must not fetch")),
    )

    result = await manager.async_get_catalog()
    assert result == payload


@pytest.mark.asyncio
async def test_shared_catalog_acquisition_is_coalesced_and_detached(
    hass, monkeypatch
) -> None:
    payload = {"schema_version": 1, "devices": {"envoy": {"versions": ["1.0"]}}}
    entered, release = asyncio.Event(), asyncio.Event()

    class Response(_FakeResponse):
        async def json(self, content_type=None):
            entered.set()
            await release.wait()
            return await super().json(content_type)

    session = _FakeSession([Response(status=200, payload=payload)])
    monkeypatch.setattr(firmware_catalog, "async_get_clientsession", lambda _h: session)
    first = firmware_catalog.get_firmware_catalog_manager(hass)
    second = firmware_catalog.get_firmware_catalog_manager(hass)
    assert first is second
    request = asyncio.create_task(first.async_get_catalog(force_refresh=True))
    await entered.wait()
    joined = asyncio.create_task(second.async_get_catalog(force_refresh=True))
    await asyncio.sleep(0)
    release.set()
    left, right = await asyncio.gather(request, joined)
    assert left == right == payload
    assert not session._actions
    left["devices"]["envoy"]["versions"].clear()
    payload["devices"].clear()
    assert right["devices"]["envoy"]["versions"] == ["1.0"]
    cached = first.cached_catalog
    cached["devices"].clear()
    assert (await second.async_get_catalog())["devices"]["envoy"]["versions"] == ["1.0"]


def test_shared_catalog_is_scoped_to_home_assistant_and_url(hass) -> None:
    first = firmware_catalog.get_firmware_catalog_manager(hass, url="https://a.invalid")
    assert first is firmware_catalog.get_firmware_catalog_manager(
        hass, url="https://a.invalid"
    )
    assert first is not firmware_catalog.get_firmware_catalog_manager(
        hass, url="https://b.invalid"
    )
    other = SimpleNamespace(data={})
    assert first is not firmware_catalog.get_firmware_catalog_manager(
        other, url="https://a.invalid"
    )
    assert (
        firmware_catalog.get_firmware_catalog_manager(SimpleNamespace()).cached_catalog
        is None
    )


@pytest.mark.asyncio
async def test_catalog_rate_limit_preserves_cache_and_cooldown_even_when_forced(
    monkeypatch,
) -> None:
    clock = [100.0]
    monkeypatch.setattr(
        firmware_catalog, "time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    payload = {"schema_version": 1, "devices": {"envoy": {}}}
    session = _FakeSession(
        [
            _FakeResponse(status=200, payload=payload),
            _FakeResponse(status=429, payload={}, headers={"Retry-After": "3600"}),
            _FakeResponse(status=200, payload=payload),
        ]
    )
    monkeypatch.setattr(firmware_catalog, "async_get_clientsession", lambda _h: session)
    manager = firmware_catalog.FirmwareCatalogManager(SimpleNamespace())
    await manager.async_get_catalog()
    assert await manager.async_get_catalog(force_refresh=True) == payload
    assert manager._expires_mono == 3700.0
    assert await manager.async_get_catalog(force_refresh=True) == payload
    assert len(session._actions) == 1
    clock[0] = 3701.0
    assert await manager.async_get_catalog(force_refresh=True) == payload
    assert manager.status_snapshot()["using_stale"] is False


def test_resolve_country_and_locale_priority() -> None:
    coord = SimpleNamespace(
        battery_country_code="AU",
        battery_locale="en-AU",
    )
    hass = SimpleNamespace(config=SimpleNamespace(country="US", language="fr-fr"))

    country, locale = firmware_catalog.resolve_country_and_locale(coord, hass)
    assert country == "AU"
    assert locale == "en-au"

    coord = SimpleNamespace(
        battery_country_code=None,
        battery_locale=None,
    )
    country, locale = firmware_catalog.resolve_country_and_locale(coord, hass)
    assert country == "US"
    assert locale == "fr-fr"

    coord = SimpleNamespace(
        battery_country_code=None,
        battery_locale="en-AU",
    )
    hass = SimpleNamespace(config=SimpleNamespace(country=None, language="fr-ca"))
    country, locale = firmware_catalog.resolve_country_and_locale(coord, hass)
    assert country is None
    assert locale == "en-au"


def test_version_normalization_and_comparison() -> None:
    assert firmware_catalog.normalize_version_token("v04.30.32") == "04.30.32"
    assert firmware_catalog.normalize_version_token("firmware 8.2.4401") == "8.2.4401"
    assert firmware_catalog.normalize_version_token("unknown") is None
    assert firmware_catalog.normalize_version_token("   ") is None

    assert firmware_catalog.compare_versions("8.2.4401", "8.2.4300") is True
    assert firmware_catalog.compare_versions("8.2.4401", "8.2.4401") is False
    assert firmware_catalog.compare_versions("8.2.4401", None) is None


def test_select_catalog_entry_country_and_locale_fallback() -> None:
    catalog = {
        "schema_version": 1,
        "generated_at": "2026-03-01T00:00:00Z",
        "devices": {
            "envoy": {
                "latest_by_locale": {
                    "fr-fr": {
                        "version": "8.2.4500",
                        "urls_by_locale": {"fr-fr": "https://example.com/fr"},
                    }
                },
                "latest_by_country": {
                    "AU": {
                        "version": "8.2.4401",
                        "urls_by_locale": {
                            "en": "https://example.com/en",
                            "fr-fr": "https://example.com/fr",
                        },
                    }
                },
                "latest_global": {
                    "version": "8.2.4300",
                    "countries_text": "Worldwide",
                    "urls_by_locale": {"en": "https://example.com/global"},
                },
            }
        },
    }

    selected = firmware_catalog.select_catalog_entry(
        catalog,
        device_type="envoy",
        country="AU",
        locale="fr-fr",
    )
    assert selected.source_scope == "country"
    assert selected.locale_used == "fr-fr"
    assert selected.entry and selected.entry["version"] == "8.2.4401"

    exact_locale_does_not_override_country = firmware_catalog.select_catalog_entry(
        {
            "schema_version": 1,
            "generated_at": "2026-03-01T00:00:00Z",
            "devices": {
                "envoy": {
                    "latest_by_locale": {
                        "en-au": {
                            "version": "8.3.5427",
                            "urls_by_locale": {
                                "en": "https://example.com/global-en",
                                "en-au": "https://example.com/global-au",
                            },
                        }
                    },
                    "latest_by_country": {
                        "AU": {
                            "version": "8.3.5232",
                            "urls_by_locale": {
                                "en-au": "https://example.com/au-regional"
                            },
                        }
                    },
                    "latest_global": {
                        "version": "8.3.5427",
                        "urls_by_locale": {"en": "https://example.com/global"},
                    },
                }
            },
        },
        device_type="envoy",
        country="AU",
        locale="en-au",
    )
    assert exact_locale_does_not_override_country.source_scope == "country"
    assert exact_locale_does_not_override_country.locale_used == "en-au"
    assert (
        exact_locale_does_not_override_country.entry
        and exact_locale_does_not_override_country.entry["version"] == "8.3.5232"
    )

    same_release_locale_urls = firmware_catalog.select_catalog_entry(
        {
            "schema_version": 1,
            "generated_at": "2026-03-01T00:00:00Z",
            "devices": {
                "envoy": {
                    "latest_by_locale": {
                        "fr-be": {
                            "version": "8.3.5286",
                            "media_id": "regional-8-3",
                            "urls_by_locale": {"fr-be": "https://example.com/be-fr"},
                        }
                    },
                    "latest_by_country": {
                        "BE": {
                            "version": "8.3.5286",
                            "media_id": "regional-8-3",
                            "urls_by_locale": {"nl-be": "https://example.com/be-nl"},
                        }
                    },
                }
            },
        },
        device_type="envoy",
        country="BE",
        locale="fr-be",
    )
    assert same_release_locale_urls.source_scope == "country"
    assert same_release_locale_urls.locale_used == "fr-be"
    assert same_release_locale_urls.entry
    assert same_release_locale_urls.entry["urls_by_locale"] == {
        "nl-be": "https://example.com/be-nl",
        "fr-be": "https://example.com/be-fr",
    }

    locale_only_catalog = {
        "schema_version": 1,
        "generated_at": "2026-03-01T00:00:00Z",
        "devices": {
            "envoy": {
                "latest_by_locale": {
                    "fr-fr": {
                        "version": "8.2.4500",
                        "urls_by_locale": {"fr-fr": "https://example.com/fr"},
                    }
                },
                "latest_by_country": {},
                "latest_global": {
                    "version": "8.2.4300",
                    "urls_by_locale": {"en": "https://example.com/global"},
                },
            }
        },
    }
    locale_base_fallback = firmware_catalog.select_catalog_entry(
        locale_only_catalog,
        device_type="envoy",
        country="AU",
        locale="fr-ca",
    )
    assert locale_base_fallback.source_scope is None
    assert locale_base_fallback.entry is None

    country_preferred_over_locale_base = firmware_catalog.select_catalog_entry(
        catalog,
        device_type="envoy",
        country="AU",
        locale="en-nz",
    )
    assert country_preferred_over_locale_base.source_scope == "country"
    assert country_preferred_over_locale_base.entry
    assert country_preferred_over_locale_base.entry["version"] == "8.2.4401"

    locale_scope_without_urls = firmware_catalog.select_catalog_entry(
        {
            "schema_version": 1,
            "devices": {
                "envoy": {
                    "latest_by_locale": {"fr-fr": {"version": "1.2.3"}},
                    "latest_by_country": {},
                    "latest_global": None,
                }
            },
        },
        device_type="envoy",
        country="FR",
        locale="fr-fr",
    )
    assert locale_scope_without_urls.source_scope is None
    assert locale_scope_without_urls.locale_used == "fr-fr"
    assert locale_scope_without_urls.entry is None

    fallback = firmware_catalog.select_catalog_entry(
        catalog,
        device_type="envoy",
        country="AU",
        locale="es-es",
    )
    assert fallback.source_scope == "country"
    assert fallback.locale_used == "en"

    global_fallback = firmware_catalog.select_catalog_entry(
        catalog,
        device_type="envoy",
        country="US",
        locale="en-us",
    )
    assert global_fallback.source_scope == "global"
    assert global_fallback.entry and global_fallback.entry["version"] == "8.2.4300"

    preferred_global = firmware_catalog.select_catalog_entry(
        catalog,
        device_type="envoy",
        country="AU",
        locale="fr-fr",
        prefer_global=True,
    )
    assert preferred_global.source_scope == "global"
    assert preferred_global.locale_used == "en"
    assert preferred_global.entry and preferred_global.entry["version"] == "8.2.4300"

    legacy_catalog = {
        "schema_version": 1,
        "generated_at": "2026-03-01T00:00:00Z",
        "devices": {
            "envoy": {
                "latest_by_country": {
                    "AU": {
                        "version": "8.2.4401",
                        "urls_by_locale": {"en": "https://example.com/en"},
                    }
                },
                "latest_global": {
                    "version": "8.2.4300",
                    "countries_text": "Worldwide",
                    "urls_by_locale": {"en": "https://example.com/global"},
                },
            }
        },
    }
    legacy_selection = firmware_catalog.select_catalog_entry(
        legacy_catalog,
        device_type="envoy",
        country="AU",
        locale="fr-fr",
    )
    assert legacy_selection.source_scope == "country"
    assert legacy_selection.entry and legacy_selection.entry["version"] == "8.2.4401"

    repair_selection = firmware_catalog.select_catalog_entry(
        {
            "schema_version": 1,
            "devices": {
                "envoy": {
                    "document_topic_id": 217,
                    "product_media_name_id": 5002,
                    "latest_by_country": {
                        "AU": {
                            "version": "8.2.4401",
                            "urls_by_locale": {
                                "en-au": (
                                    "https://enphase.com/en-au/installers/resources/"
                                    "documentation/apps?media_id=22469&langcode=und"
                                )
                            },
                        }
                    },
                }
            },
        },
        device_type="envoy",
        country="AU",
        locale="en-au",
    )
    assert repair_selection.entry is not None
    assert repair_selection.entry["urls_by_locale"]["en-au"] == (
        "https://enphase.com/en-au/installers/resources/documentation/apps?"
        "product_type=216&f%5B0%5D=document%3A217&"
        "f%5B1%5D=product_media_name%3A5002&"
        "search_api_language=en-au&field_alternative_language=en-au"
    )


def test_source_age_seconds_handles_future_and_invalid() -> None:
    now = datetime.now(firmware_catalog.dt_util.UTC)
    future = (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    assert firmware_catalog._source_age_seconds(future) == 0.0
    assert firmware_catalog._source_age_seconds("not-a-date") is None
    assert firmware_catalog._source_age_seconds(None) is None


def test_legacy_release_url_helper_edge_paths() -> None:
    entry = {
        "urls_by_locale": {
            "en-au": "https://example.test/envoy?media_id=123&langcode=und",
            "fr-fr": "https://example.test/envoy/fr",
        }
    }
    device_payload = {"document_topic_id": None, "product_media_name_id": 5002}
    assert firmware_catalog._repair_legacy_release_urls(device_payload, entry) is entry

    device_payload = {"document_topic_id": 217, "product_media_name_id": 5002}
    assert (
        firmware_catalog._repair_legacy_release_urls(device_payload, entry) is not entry
    )

    non_legacy_entry = {
        "document_topic_id": 217,
        "product_media_name_id": 5002,
        "urls_by_locale": {"en-au": "https://example.test/envoy/fr"},
    }
    assert (
        firmware_catalog._repair_legacy_release_urls(device_payload, non_legacy_entry)
        is non_legacy_entry
    )

    class _BadStr:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    assert firmware_catalog._is_legacy_release_url(None) is False
    assert firmware_catalog._is_legacy_release_url(_BadStr()) is False
    assert firmware_catalog._as_int(_BadStr()) is None


def test_coordinator_metrics_include_firmware_catalog_status(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.firmware_catalog_manager = SimpleNamespace(
        status_snapshot=lambda: {
            "last_fetch_utc": "2026-03-01T00:00:00+00:00",
            "last_success_utc": "2026-03-01T00:00:00+00:00",
            "last_error": None,
            "using_stale": False,
            "catalog_generated_at": "2026-03-01T00:00:00Z",
            "catalog_source_age_seconds": 30.0,
        }
    )

    metrics = coord.collect_site_metrics()

    assert metrics["firmware_catalog_last_fetch_utc"] == "2026-03-01T00:00:00+00:00"
    assert metrics["firmware_catalog_last_success_utc"] == "2026-03-01T00:00:00+00:00"
    assert metrics["firmware_catalog_last_error"] is None
    assert metrics["firmware_catalog_using_stale"] is False
    assert metrics["firmware_catalog_generated_at"] == "2026-03-01T00:00:00Z"
    assert metrics["firmware_catalog_source_age_seconds"] == 30.0


def test_coordinator_metrics_handles_firmware_catalog_status_error(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.firmware_catalog_manager = SimpleNamespace(
        status_snapshot=lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    metrics = coord.collect_site_metrics()

    assert metrics["firmware_catalog_last_fetch_utc"] is None
    assert metrics["firmware_catalog_last_error"] is None


def test_helper_edge_branches() -> None:
    class _BadStr:
        def __str__(self):
            raise ValueError("boom")

    with pytest.raises(ValueError):
        firmware_catalog._validate_catalog("bad")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        firmware_catalog._validate_catalog({"schema_version": 2, "devices": {}})
    with pytest.raises(ValueError):
        firmware_catalog._validate_catalog({"schema_version": 1, "devices": []})

    assert firmware_catalog._catalog_generated_at(None) is None
    assert firmware_catalog._catalog_generated_at({"generated_at": None}) is None
    assert firmware_catalog._catalog_generated_at({"generated_at": _BadStr()}) is None

    assert firmware_catalog._iso_or_none(None) is None
    assert firmware_catalog._mono_to_utc_iso(time.monotonic() - 1) is None
    assert firmware_catalog._mono_to_utc_iso(time.monotonic() + 1) is not None
    assert firmware_catalog._parse_iso_datetime("") is None
    assert (
        firmware_catalog._parse_iso_datetime("2026-03-01T00:00:00").tzinfo is not None
    )

    assert firmware_catalog.normalize_locale(None) == "en"
    assert firmware_catalog.normalize_locale(_BadStr()) == "en"
    assert firmware_catalog.normalize_country("AUS") is None
    assert firmware_catalog.normalize_country(_BadStr()) is None
    assert firmware_catalog.normalize_version_token(None) is None
    assert firmware_catalog.normalize_version_token(_BadStr()) is None
    assert firmware_catalog._text_or_none(None) is None
    assert firmware_catalog._text_or_none(_BadStr()) is None
    assert firmware_catalog._exact_locale_entry([], "en") is None
    assert firmware_catalog._exact_locale_entry({"en": "bad"}, "en") is None
    assert not firmware_catalog._same_catalog_release(None, {})
    assert firmware_catalog._same_catalog_release(
        {"version": "1.0", "media_id": "same"},
        {"version": "2.0", "media_id": "same"},
    )
    assert not firmware_catalog._same_catalog_release(
        {"version": "unknown"},
        {"version": None},
    )
    assert firmware_catalog._with_merged_locale_urls({"version": "1.0"}, None) == {
        "version": "1.0"
    }
    assert firmware_catalog._with_merged_locale_urls(
        {"version": "1.0"}, {"urls_by_locale": []}
    ) == {"version": "1.0"}
    assert not firmware_catalog._is_worldwide_catalog_entry(None)
    assert not firmware_catalog._is_worldwide_catalog_entry({"countries_text": None})
    assert not firmware_catalog._is_worldwide_catalog_entry(
        {"countries_text": "All countries except Ireland"}
    )
    assert firmware_catalog._is_worldwide_catalog_entry({"countries_text": "Worldwide"})
    assert firmware_catalog._is_worldwide_catalog_entry(
        {"countries_text": "All countries"}
    )
    assert firmware_catalog._repair_legacy_release_urls({}, {}) == {}
    assert firmware_catalog._parse_version_parts("") is None
    assert firmware_catalog._parse_version_parts("1.a") is None

    empty_selection = firmware_catalog.select_catalog_entry(
        None,
        device_type="envoy",
        country="AU",
        locale="en-au",
    )
    assert empty_selection.entry is None

    no_devices_selection = firmware_catalog.select_catalog_entry(
        {"schema_version": 1, "devices": []},
        device_type="envoy",
        country="AU",
        locale="en-au",
    )
    assert no_devices_selection.entry is None

    no_device_payload = firmware_catalog.select_catalog_entry(
        {"schema_version": 1, "devices": {"envoy": []}},
        device_type="envoy",
        country="AU",
        locale="en-au",
    )
    assert no_device_payload.entry is None

    no_entry_selection = firmware_catalog.select_catalog_entry(
        {
            "schema_version": 1,
            "devices": {"envoy": {"latest_by_country": {}, "latest_global": None}},
        },
        device_type="envoy",
        country="AU",
        locale="en-au",
    )
    assert no_entry_selection.entry is None

    fallback_first_locale = firmware_catalog.select_catalog_entry(
        {
            "schema_version": 1,
            "devices": {
                "envoy": {
                    "latest_by_country": {
                        "AU": {
                            "version": "1.0.0",
                            "urls_by_locale": {
                                "fr-ca": "https://example.com/fr",
                                "de-de": "https://example.com/de",
                            },
                        }
                    },
                    "latest_global": {},
                }
            },
        },
        device_type="envoy",
        country="AU",
        locale="es-es",
    )
    assert fallback_first_locale.locale_used == "fr-ca"
