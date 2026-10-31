"""Tests for helper utilities in the API module."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from aiohttp import CookieJar
from yarl import URL

from custom_components.enphase_ev import api


import pytest


def test_mfa_headers_without_session_cookies():
    from custom_components.enphase_ev.api_client.auth import mfa_headers

    base = {"User-Agent": "Enphase test"}
    assert mfa_headers(
        {},
        base_headers=base,
        cookie_header=lambda cookies: "",
        xsrf_token=lambda cookies: None,
    ) == {"User-Agent": "Enphase test", "Accept": "application/json, text/plain, */*"}
    assert base == {"User-Agent": "Enphase test"}


@pytest.mark.asyncio
async def test_serialize_cookie_jar() -> None:
    """Cookie jar serialization should extract cookies for provided URLs."""
    jar = CookieJar()
    jar.update_cookies({"session": "abc123"}, URL("https://enphase.test"))
    header, mapping = api._serialize_cookie_jar(
        jar, ["https://enphase.test", "https://ignored.invalid"]
    )
    assert header == "session=abc123"
    assert mapping == {"session": "abc123"}


def test_serialize_cookie_jar_ignores_failures() -> None:
    """Defensive branches ignore casting and filter errors."""

    class BadURL:
        def __str__(self) -> str:
            raise RuntimeError("cannot stringify")

    morsel = SimpleNamespace(value="good")
    jar = MagicMock()
    jar.filter_cookies.side_effect = [
        RuntimeError("jar blew up"),
        {"session": morsel},
    ]
    header, mapping = api._serialize_cookie_jar(
        jar,
        [
            URL("https://first.example"),
            BadURL(),
            "https://second.example",
        ],
    )
    assert header == "session=good"
    assert mapping == {"session": "good"}


def test_serialize_cookie_jar_skips_invalid_url() -> None:
    """Invalid URL entries are ignored without raising."""

    class BrokenURL:
        def __str__(self) -> str:
            raise RuntimeError("bad url")

    jar = MagicMock()
    header, mapping = api._serialize_cookie_jar(jar, [BrokenURL()])
    assert header == ""
    assert mapping == {}
    jar.filter_cookies.assert_not_called()


def test_serialize_cookie_jar_filter_failure() -> None:
    """Exceptions from filter_cookies should be swallowed."""
    jar = MagicMock()
    jar.filter_cookies.side_effect = RuntimeError("fail")
    header, mapping = api._serialize_cookie_jar(jar, ["https://example.test"])
    assert header == ""
    assert mapping == {}


def _make_token(payload: dict) -> str:
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    payload_b64 = payload_b64.rstrip("=")
    return f"header.{payload_b64}.sig"


def test_decode_jwt_exp_success() -> None:
    """Successfully decode the exp claim from a JWT payload."""
    token = _make_token({"exp": 1_700_000_000})
    assert api._decode_jwt_exp(token) == 1_700_000_000


def test_decode_jwt_exp_invalid_payload_returns_none() -> None:
    """Invalid payloads are handled defensively."""
    bad_payload = base64.urlsafe_b64encode(b"not-json").decode().rstrip("=")
    token = f"header.{bad_payload}.sig"
    assert api._decode_jwt_exp(token) is None


def test_decode_jwt_exp_non_numeric_exp() -> None:
    """Exp claims that are not numeric should be ignored."""
    token = _make_token({"exp": "tomorrow"})
    assert api._decode_jwt_exp(token) is None


def test_decode_jwt_exp_failure() -> None:
    """Non-JWT strings should produce None."""
    assert api._decode_jwt_exp("not-a-token") is None


def test_jwt_user_id_from_payload() -> None:
    """Extract user_id from a top-level JWT payload."""
    token = _make_token({"user_id": 1234})
    assert api._jwt_user_id(token) == "1234"


def test_jwt_user_id_from_nested_data() -> None:
    """Extract user_id from nested JWT data."""
    token = _make_token({"data": {"user_id": 5678}})
    assert api._jwt_user_id(token) == "5678"


def test_jwt_user_id_missing() -> None:
    """Missing or invalid tokens return None."""
    assert api._jwt_user_id(None) is None
    assert api._jwt_user_id("not-a-token") is None


def test_jwt_user_id_invalid_payload() -> None:
    """Invalid JWT payloads should return None."""
    bad_payload = base64.urlsafe_b64encode(b"not-json").decode().rstrip("=")
    token = f"header.{bad_payload}.sig"
    assert api._jwt_user_id(token) is None


def test_jwt_session_id_from_payload() -> None:
    """Extract session_id from a top-level JWT payload."""
    token = _make_token({"session_id": "sess-1"})
    assert api._jwt_session_id(token) == "sess-1"


def test_jwt_session_id_from_nested_data() -> None:
    """Extract session_id from nested JWT data."""
    token = _make_token({"data": {"session_id": "sess-2"}})
    assert api._jwt_session_id(token) == "sess-2"


def test_jwt_session_id_missing() -> None:
    """Missing or invalid tokens return None."""
    assert api._jwt_session_id(None) is None
    assert api._jwt_session_id("not-a-token") is None


def test_jwt_session_id_missing_in_payload() -> None:
    """JWT payloads without session_id return None."""
    token = _make_token({"data": "no-session"})
    assert api._jwt_session_id(token) is None


def test_extract_xsrf_token() -> None:
    """XSRF token is located case-insensitively."""
    cookies = {"session": "abc", "XSRF-TOKEN": "xsrf-value"}
    assert api._extract_xsrf_token(cookies) == "xsrf-value"


def test_extract_xsrf_token_missing() -> None:
    """Missing tokens return None without raising."""
    assert api._extract_xsrf_token(None) is None
    assert api._extract_xsrf_token({"session": "abc"}) is None


def test_normalize_sites_handles_nested_structures() -> None:
    """Site normalization should handle nested dict responses."""
    payload = {
        "data": [
            {"site_id": 1234, "name": "Garage"},
            {"siteId": "5678", "siteName": "Backup"},
            {"id": 9, "title": "Driveway"},
        ]
    }
    sites = api._normalize_sites(payload)
    assert [site.site_id for site in sites] == ["1234", "5678", "9"]
    assert sites[0].name == "Garage"
    assert sites[1].name == "Backup"
    assert sites[2].name == "Driveway"


def test_normalize_sites_dedupes_and_fills_name() -> None:
    """Duplicate site entries should collapse and retain the first available name."""
    payload = [
        {"id": 1},
        {"id": 1, "title": "Main Site"},
        {"id": 2, "name": "Secondary"},
        {"id": 2, "title": "Ignored"},
    ]
    sites = api._normalize_sites(payload)
    assert [(site.site_id, site.name) for site in sites] == [
        ("1", "Main Site"),
        ("2", "Secondary"),
    ]


def test_normalize_sites_skips_invalid_entries() -> None:
    """Invalid items or missing ids should be ignored."""
    payload = ["not-a-dict", {"id": None}, {"siteId": "42", "name": "Valid"}]
    result = api._normalize_sites(payload)
    assert [site.site_id for site in result] == ["42"]


def test_normalize_sites_with_non_iterable_payload() -> None:
    """Non-iterable payloads produce an empty list."""
    assert api._normalize_sites("unexpected") == []


def test_normalize_chargers_handles_varied_keys() -> None:
    """Charger normalization should account for different payload keys."""
    payload = {
        "data": {
            "chargers": [
                {"serial": "EV123", "name": "Garage"},
                {"serialNumber": "EV456", "displayName": "Driveway"},
                {"sn": "EV789"},
            ]
        }
    }
    chargers = api._normalize_chargers(payload)
    assert [charger.serial for charger in chargers] == ["EV123", "EV456", "EV789"]
    assert chargers[0].name == "Garage"
    assert chargers[1].name == "Driveway"
    assert chargers[2].name is None


def test_normalize_chargers_skips_invalid_entries() -> None:
    """Non-dict items or missing serials are filtered out."""
    payload = {
        "chargers": [
            "not-a-dict",
            {"name": "Missing serial"},
            {"id": 42, "display_name": "Valid"},
        ]
    }
    result = api._normalize_chargers(payload)
    assert [charger.serial for charger in result] == ["42"]
    assert result[0].name == "Valid"


def test_normalize_chargers_with_non_iterable_payload() -> None:
    """Non-iterable payloads result in an empty list."""
    assert api._normalize_chargers({"data": None}) == []


def test_unavailable_helpers_handle_bad_inputs() -> None:
    class BadStr:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    bad = BadStr()
    assert api.is_scheduler_unavailable_error(bad, 500, bad) is False
    assert api.is_session_history_unavailable_error(bad, 500, bad) is False
    assert api.is_site_energy_unavailable_error(bad, 500, bad) is False
    assert api.is_auth_settings_unavailable_error(bad, 500, bad) is False


def test_scheduler_unavailable_detection_paths() -> None:
    assert api.is_scheduler_unavailable_error(
        "Service Unavailable",
        503,
        "https://enphase.test/service/evse_scheduler/api/v1/iqevc/charging-mode/1/2/preference",
    )
    assert api.is_scheduler_unavailable_error(
        "IQEVC Scheduler MS refused to connect", 503, None
    )
    assert api.is_scheduler_unavailable_error("Scheduler MS unavailable", None, None)
    assert api.is_scheduler_unavailable_error("Scheduler unavailable", None, None)
    assert api.is_scheduler_unavailable_error(
        "Service unavailable from schedules/status", 500, None
    )
    assert not api.is_scheduler_unavailable_error(
        "not related", 400, "https://enphase.test/service/evse_controller"
    )


def test_session_history_unavailable_detection_paths() -> None:
    assert api.is_session_history_unavailable_error(
        "Service Unavailable",
        503,
        "https://enphase.test/service/enho_historical_events_ms/1/sessions/2/history",
    )
    assert api.is_session_history_unavailable_error(
        "Service Unavailable",
        550,
        "https://enphase.test/service/enho_historical_events_ms/1/sessions/2/history",
    )
    assert api.is_session_history_unavailable_error(
        "historical_events service unavailable", 500, None
    )
    assert api.is_session_history_unavailable_error(
        "Session history unavailable", 500, None
    )
    assert not api.is_session_history_unavailable_error(
        "not related", 400, "https://enphase.test/other"
    )


def test_site_energy_unavailable_detection_paths() -> None:
    assert api.is_site_energy_unavailable_error(
        "Service Unavailable",
        503,
        "https://enphase.test/pv/systems/123/lifetime_energy",
    )
    assert api.is_site_energy_unavailable_error(
        "lifetime_energy service unavailable", 500, None
    )
    assert not api.is_site_energy_unavailable_error(
        "not related", 404, "https://enphase.test/pv/systems/123/summary"
    )


def test_auth_settings_unavailable_detection_paths() -> None:
    assert api.is_auth_settings_unavailable_error(
        "Service Unavailable",
        503,
        "https://enphase.test/service/evse_controller/api/v1/1/ev_chargers/2/ev_charger_config",
    )
    assert api.is_auth_settings_unavailable_error(
        "ev_charger_config service unavailable", 500, None
    )
    assert not api.is_auth_settings_unavailable_error(
        "not related",
        404,
        "https://enphase.test/service/evse_controller/api/v1/1/ev_chargers/2/other",
    )
