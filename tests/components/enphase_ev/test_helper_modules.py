"""Tests for helper modules used by the Enphase Energy coordinator."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from aiohttp.client_reqrep import RequestInfo
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from custom_components.enphase_ev import session_history as sh_mod
from custom_components.enphase_ev import runtime_helpers, system_dashboard_helpers
from custom_components.enphase_ev import summary as summary_mod
from custom_components.enphase_ev.api import (
    InvalidPayloadError,
    SessionHistoryUnavailable,
    Unauthorized,
)
from custom_components.enphase_ev.const import MAX_POLL_INTERVAL
from custom_components.enphase_ev.evse_timeseries import EVSETimeseriesManager
from custom_components.enphase_ev.session_history import (
    MIN_SESSION_HISTORY_CACHE_TTL,
    SESSION_CACHE_STATE_STALE_REUSED,
    SESSION_CACHE_STATE_UNAVAILABLE,
    SESSION_CACHE_STATE_VALID,
    SessionCacheEntry,
    SessionCacheView,
    SessionHistoryManager,
)
from custom_components.enphase_ev.summary import (
    SummaryStore,
    SUMMARY_ACTIVE_MIN_TTL,
    SUMMARY_IDLE_TTL,
)
from custom_components.enphase_ev.runtime_helpers import normalize_poll_intervals


def test_runtime_helpers_cover_parsing_dates_and_redaction(monkeypatch) -> None:
    class BadStr:
        def __str__(self) -> str:
            raise ValueError("bad")

    assert runtime_helpers.coerce_int(True) == 1
    assert runtime_helpers.coerce_int(" 7 ") == 7
    assert runtime_helpers.coerce_int("bad", default=9) == 9
    assert runtime_helpers.coerce_optional_int(" 5 ") == 5
    assert runtime_helpers.coerce_optional_int(BadStr()) is None
    assert normalize_poll_intervals("1", "2") == (30, 60)
    assert normalize_poll_intervals("45", "30") == (45, 60)
    assert normalize_poll_intervals("9999", "9999") == (
        MAX_POLL_INTERVAL,
        MAX_POLL_INTERVAL,
    )
    assert runtime_helpers.normalize_iso_date("2026-03-29") == "2026-03-29"
    assert runtime_helpers.normalize_iso_date("   ") is None
    assert runtime_helpers.normalize_iso_date(BadStr()) is None
    assert (
        runtime_helpers.resolve_inverter_start_date(
            {"start_date": "2022-08-10"},
            {},
        )
        == "2022-08-10"
    )
    assert (
        runtime_helpers.resolve_inverter_start_date(
            {},
            {
                "INV-B": {"lifetime_query_start_date": "2023-01-01"},
                "INV-A": {"lifetime_query_start_date": "2022-08-10"},
            },
        )
        == "2022-08-10"
    )
    assert runtime_helpers.resolve_inverter_start_date({}, {"INV-A": "bad"}) is None

    assert (
        runtime_helpers.resolve_site_timezone_name("Europe/Berlin") == "Europe/Berlin"
    )
    assert runtime_helpers.resolve_site_timezone_name("Bad/Zone") == "UTC"
    assert (
        runtime_helpers.resolve_site_local_current_date(
            {"curr_date_site": "2026-03-29"},
            None,
        )
        == "2026-03-29"
    )
    assert (
        runtime_helpers.resolve_site_local_current_date(
            {"result": ["bad", {"curr_date_site": "2026-03-28"}]},
            None,
        )
        == "2026-03-28"
    )

    monkeypatch.setattr(
        "custom_components.enphase_ev.runtime_helpers.dt_util.now",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    fallback_date = runtime_helpers.resolve_site_local_current_date({}, "Bad/Zone")
    assert fallback_date == datetime.now(timezone.utc).date().isoformat()

    original = {"a": [1, {"b": 2}], "token": "secret"}
    copied = runtime_helpers.copy_diagnostics_value(original)
    assert copied == original
    assert copied is not original
    assert copied["a"] is not original["a"]
    assert runtime_helpers.redact_battery_payload(original) == {
        "a": [1, {"b": 2}],
        "token": "[redacted]",
    }
    assert runtime_helpers.coerce_optional_text("  value  ") == "value"
    assert runtime_helpers.coerce_optional_text(BadStr()) is None
    assert runtime_helpers.evse_session_energy_uses_wh("bad-source") is False
    assert runtime_helpers.evse_session_energy_uses_wh({"modelId": BadStr()}) is False
    assert runtime_helpers.evse_session_energy_uses_wh(
        {"session_d": {"plg_in_at": "2026-06-13T17:06:43Z"}}
    )
    assert runtime_helpers.normalize_evse_session_energy("140.038", wh_hint=True) == (
        0.14,
        140.038,
        "Wh",
    )
    assert runtime_helpers.normalize_evse_session_energy("3.52") == (
        3.52,
        3520.0,
        "kWh",
    )
    with monkeypatch.context() as ctx:
        ctx.setattr(
            "builtins.round",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("boom")),
        )
        assert runtime_helpers.normalize_evse_session_energy(250) == (
            None,
            None,
            "Wh",
        )
        assert runtime_helpers.normalize_evse_session_energy(2) == (
            None,
            None,
            "kWh",
        )

    inventory_view = MagicMock(
        has_type_for_entities=MagicMock(return_value=True),
        type_device_info=MagicMock(return_value={"device": "envoy"}),
    )
    coord = MagicMock(inventory_view=inventory_view)
    assert runtime_helpers.inventory_type_available(coord, "envoy") is True
    assert runtime_helpers.inventory_type_device_info(coord, "envoy") == {
        "device": "envoy"
    }
    assert (
        runtime_helpers.inventory_type_device_info(
            MagicMock(inventory_view=MagicMock(type_device_info=None)),
            "envoy",
        )
        is None
    )


def test_system_dashboard_helpers_cover_core_paths(monkeypatch) -> None:
    class BadText:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert (
        system_dashboard_helpers.dashboard_key_token("System Controller")
        == "system_controller"
    )
    assert system_dashboard_helpers.dashboard_key_token("   ") == ""
    assert system_dashboard_helpers.dashboard_key_matches("meter_type", "meter")
    assert system_dashboard_helpers.dashboard_key_matches("   ", "meter") is False
    assert system_dashboard_helpers.dashboard_simple_value({"a": [1, "x", None]}) == {
        "a": [1, "x"]
    }
    assert system_dashboard_helpers.dashboard_simple_value(True) is True
    assert system_dashboard_helpers.dashboard_simple_value(BadText()) is None
    assert list(
        system_dashboard_helpers.iter_dashboard_mappings(
            [{"status": "ok"}, {"nested": {"mode": "dhcp"}}]
        )
    ) == [{"status": "ok"}, {"nested": {"mode": "dhcp"}}, {"mode": "dhcp"}]
    assert (
        system_dashboard_helpers.dashboard_parent_id({"parentId": "PARENT"}) == "PARENT"
    )
    assert system_dashboard_helpers.system_dashboard_type_key("meters") == "envoy"
    assert (
        system_dashboard_helpers.system_dashboard_type_key("inverters")
        == "microinverter"
    )
    assert system_dashboard_helpers.system_dashboard_battery_detail_subset(None) == {}
    assert (
        system_dashboard_helpers.system_dashboard_meter_kind({"meter_type": " "})
        is None
    )
    assert system_dashboard_helpers.system_dashboard_detail_records(
        {"envoys": {"envoys": {"devices": ["bad", {"id": "dup"}, {"id": "dup"}]}}},
        "envoys",
    ) == [{"id": "dup"}]

    with monkeypatch.context() as nested:
        nested.setattr(
            "custom_components.enphase_ev.system_dashboard_helpers.dashboard_first_mapping",
            lambda payload, *keys: "bad",
        )
        assert (
            system_dashboard_helpers.system_dashboard_microinverter_summary(
                {}, {}, None
            )
            == {}
        )

    tree_payload = {
        "devices": [
            {
                "device_uid": "GW-1",
                "type": "envoy",
                "name": "Gateway",
                "serial_number": "GW-1",
                "children": [
                    {"device_uid": "BAT-1", "type": "encharge", "name": "Battery"}
                ],
            }
        ]
    }
    details_payloads = {
        "envoy": {
            "envoys": {
                "envoys": [
                    {
                        "device_uid": "GW-1",
                        "status": "online",
                        "network": {"mode": "dhcp"},
                    }
                ]
            },
            "meters": {
                "meters": [{"id": "1", "name": "M1", "meter_type": "consumption"}]
            },
        },
        "encharge": {
            "encharges": {
                "encharges": [
                    {
                        "device_uid": "BAT-1",
                        "serial_number": "BAT-1",
                        "app_version": "1.2.3",
                        "status": "ok",
                    }
                ]
            }
        },
        "microinverter": {
            "inverters": {
                "inverters": {
                    "total": 2,
                    "not_reporting": 1,
                    "items": [{"name": "IQ8", "count": 2}],
                }
            }
        },
    }

    type_summaries, hierarchy_summary, hierarchy_index = (
        system_dashboard_helpers.build_system_dashboard_summaries(
            tree_payload, details_payloads
        )
    )
    assert hierarchy_summary["total_nodes"] == 3
    assert hierarchy_index["GW-1"]["type_key"] == "envoy"
    assert type_summaries["envoy"]["meters"] == [
        {"name": "M1", "meter_type": "consumption"}
    ]
    assert type_summaries["encharge"]["batteries"][0]["app_version"] == "1.2.3"
    assert type_summaries["microinverter"]["connectivity"] == "degraded"
    assert (
        system_dashboard_helpers._format_inverter_model_summary(  # noqa: SLF001
            {"": 1, "IQ7": "bad", "IQ8": 0, "IQ8M": 2}
        )
        == "IQ8M x2"
    )
    merged_index = system_dashboard_helpers.index_dashboard_nodes(
        [
            {"device_uid": "GW-1", "serial_number": "GW-1", "name": "Gateway"},
            {"serial_number": "GW-1", "name": None, "type": "envoy"},
        ]
    )
    assert merged_index["GW-1"]["name"] == "Gateway"
    assert system_dashboard_helpers.index_dashboard_nodes("bad") == {}
    with monkeypatch.context() as nested:
        original_node_entry = system_dashboard_helpers.dashboard_node_entry

        def _fake_node_entry(payload, **kwargs):
            entry = original_node_entry(payload, **kwargs)
            if entry is not None and payload.get("serial_number") == "GW-1-ALIAS":
                entry["name"] = None
            return entry

        nested.setattr(
            system_dashboard_helpers, "dashboard_node_entry", _fake_node_entry
        )
        merged_with_none = system_dashboard_helpers.index_dashboard_nodes(
            [
                {"device_uid": "GW-1", "serial_number": "GW-1", "name": "Gateway"},
                {"serial_number": "GW-1-ALIAS", "id": "GW-1", "name": "Alias"},
            ]
        )
        assert merged_with_none["GW-1"]["name"] == "Gateway"


def test_system_dashboard_helpers_cover_field_and_summary_paths() -> None:
    payload = {
        "wrapper": {"meter_type": "consumption", "network": {"mode": "dhcp"}},
        "serialNumber": "GW-1",
        "id": "node-1",
        "device_type": "envoy",
        "children": [{"id": "child-1"}],
    }
    assert (
        system_dashboard_helpers.dashboard_first_value(payload, "meter_type")
        == "consumption"
    )
    assert system_dashboard_helpers.dashboard_first_mapping(payload, "network") == {
        "mode": "dhcp"
    }
    assert system_dashboard_helpers.dashboard_field(payload, "meter_type") == (
        "consumption"
    )
    assert system_dashboard_helpers.dashboard_field_map(
        payload,
        {"serial": ("serial_number", "serialNumber")},
    ) == {"serial": "GW-1"}
    assert system_dashboard_helpers.dashboard_aliases(payload) == ["GW-1", "node-1"]
    assert system_dashboard_helpers.dashboard_primary_id({"id": "node-1"}) == "node-1"
    assert system_dashboard_helpers.dashboard_raw_type({"device_type": "envoy"}) == (
        "envoy"
    )
    assert system_dashboard_helpers.dashboard_child_containers(payload) == [
        ([{"id": "child-1"}], "envoy")
    ]

    payloads = {
        "envoys": {"envoys": [{"device_uid": "GW-1", "network": {"mode": "dhcp"}}]},
        "encharges": {
            "encharges": [
                {
                    "device_uid": "BAT-1",
                    "serial_number": "BAT-1",
                    "app_version": "1.2.3",
                }
            ]
        },
    }
    envoy_index = system_dashboard_helpers.index_dashboard_nodes(
        [{"device_uid": "GW-1", "type": "envoy"}]
    )
    encharge_index = system_dashboard_helpers.index_dashboard_nodes(
        [{"device_uid": "BAT-1", "type": "encharge"}]
    )
    assert (
        system_dashboard_helpers.system_dashboard_type_hierarchy(
            "envoy",
            envoy_index,
            None,
        )["count"]
        == 1
    )
    assert (
        system_dashboard_helpers.system_dashboard_envoy_summary(
            payloads,
            envoy_index,
            None,
        )["network"]["mode"]
        == "dhcp"
    )
    assert (
        system_dashboard_helpers.system_dashboard_encharge_summary(
            payloads,
            encharge_index,
            None,
        )["batteries"][0]["serial_number"]
        == "BAT-1"
    )
    assert system_dashboard_helpers.system_dashboard_meter_summaries(
        {
            "meters": {
                "meters": [
                    {
                        "id": "meter-1",
                        "name": "Consumption Meter",
                        "meter_type": "consumption",
                        "status": "normal",
                        "meter_state": "enabled",
                        "configuration": {
                            "phase_mode": "split",
                            "wiring_type": "ct",
                            "meter_mode": "net",
                            "measurement_type": "consumption",
                            "enabled": True,
                        },
                    },
                    {
                        "id": "meter-2",
                        "name": "Consumption Meter",
                        "meter_type": "consumption",
                    },
                ]
            }
        }
    ) == [
        {
            "name": "Consumption Meter",
            "meter_type": "consumption",
            "status": "normal",
            "meter_state": "enabled",
            "config": {
                "phase": "split",
                "wiring": "ct",
                "mode": "split",
                "role": "consumption",
                "enabled": True,
            },
        }
    ]
    assert (
        system_dashboard_helpers.system_dashboard_microinverter_summary(
            {"inverters": {"inverters": {"total": 0, "not_reporting": 0}}},
            {},
            None,
        ).get("connectivity")
        is None
    )
    assert (
        system_dashboard_helpers.system_dashboard_microinverter_summary(
            {"inverters": {"inverters": {"total": 2, "not_reporting": 0}}},
            {},
            None,
        )["connectivity"]
        == "online"
    )
    assert (
        system_dashboard_helpers.system_dashboard_microinverter_summary(
            {"inverters": {"inverters": {"total": 2, "not_reporting": 2}}},
            {},
            None,
        )["connectivity"]
        == "offline"
    )


class _DummySummaryClient:
    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    async def summary_v2(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        return [{"serialNumber": "EV-01"}]


@pytest.mark.asyncio
async def test_summary_store_caches_and_handles_errors() -> None:
    """SummaryStore should cache responses, adjust TTL, and reuse data on errors."""
    client = _DummySummaryClient()
    store = SummaryStore(lambda: client)

    # First refresh should require a fetch since cache is empty.
    assert store.prepare_refresh(want_fast=False, target_interval=None) is True
    data = await store.async_fetch()
    assert data == [{"serialNumber": "EV-01"}]
    assert client.calls == 1

    # Cached response should be reused without new client calls.
    cached = await store.async_fetch()
    assert cached == data
    assert client.calls == 1

    # Force refresh while the client raises: cache is still served.
    store.prepare_refresh(want_fast=True, target_interval=5)
    client.fail = True
    again = await store.async_fetch(force=True)
    assert again == data
    assert client.calls == 2

    # With no cache and a failing client, return an empty list gracefully.
    store.invalidate()
    empty = await store.async_fetch(force=True)
    assert empty == []
    assert client.calls == 2
    assert store.diagnostics()["using_stale"] is False

    # Missing client should behave the same.
    store_no_client = SummaryStore(lambda: None)
    assert await store_no_client.async_fetch(force=True) == []


@pytest.mark.asyncio
async def test_summary_store_redacts_configured_site_from_errors() -> None:
    client = _DummySummaryClient()
    client.summary_v2 = AsyncMock(
        side_effect=RuntimeError(
            "GET /service/evse_controller/api/v2/9990001/ev_chargers/summary"
        )
    )
    store = SummaryStore(lambda: client, site_id_getter=lambda: "9990001")

    assert await store.async_fetch(force=True) == []

    last_error = store.diagnostics()["last_error"]
    assert "9990001" not in str(last_error)
    assert "/api/v2/[site]/ev_chargers/summary" in str(last_error)


def test_summary_store_redaction_ignores_site_getter_errors() -> None:
    def bad_site_id():
        raise RuntimeError("bad site")

    store = SummaryStore(lambda: None, site_id_getter=bad_site_id)

    assert store._redact_error("summary failed") == "summary failed"  # noqa: SLF001


@pytest.mark.asyncio
async def test_summary_store_records_invalid_payload_stale_diagnostics() -> None:
    client = _DummySummaryClient()
    store = SummaryStore(lambda: client)

    first = await store.async_fetch(force=True)
    assert first == [{"serialNumber": "EV-01"}]

    payload_error = InvalidPayloadError(
        "Invalid JSON response (status=200, endpoint=/ivp/summary)",
        status=200,
        endpoint="/ivp/summary",
        content_type="application/json",
        failure_kind="json_decode",
        decode_error="JSONDecodeError",
        body_length=12,
        body_sha256="abc123",
        body_preview_redacted='{"bad":true}',
    )
    client.summary_v2 = AsyncMock(side_effect=payload_error)

    reused = await store.async_fetch(force=True)
    diag = store.diagnostics()
    assert reused == first
    assert diag["available"] is False
    assert diag["using_stale"] is True
    assert diag["failures"] == 1
    assert diag["backoff_active"] is True
    assert diag["last_payload_signature"]["endpoint"] == "/ivp/summary"
    assert diag["last_payload_signature"]["failure_kind"] == "json_decode"


@pytest.mark.asyncio
async def test_summary_store_respects_backoff_without_cache() -> None:
    client = _DummySummaryClient()
    err = aiohttp.ClientResponseError(
        _make_request_info(),
        (),
        status=429,
        message="rate limited",
        headers={"Retry-After": "120"},
    )
    client.summary_v2 = AsyncMock(side_effect=err)
    store = SummaryStore(lambda: client)

    first = await store.async_fetch(force=True)
    second = await store.async_fetch(force=True)
    diag = store.diagnostics()

    assert first == []
    assert second == []
    assert client.summary_v2.await_count == 1
    assert diag["available"] is False
    assert diag["backoff_active"] is True
    assert diag["backoff_until"] is not None
    assert diag["backoff_ends_utc"] is not None


@pytest.mark.asyncio
async def test_summary_store_parses_retry_after_date_header(monkeypatch) -> None:
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    client = _DummySummaryClient()
    err = aiohttp.ClientResponseError(
        _make_request_info(),
        (),
        status=429,
        message="rate limited",
        headers={"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"},
    )
    client.summary_v2 = AsyncMock(side_effect=err)
    store = SummaryStore(lambda: client)

    monkeypatch.setattr(
        "custom_components.enphase_ev.summary.dt_util.utcnow", lambda: now
    )

    await store.async_fetch(force=True)
    diag = store.diagnostics()

    assert diag["backoff_active"] is True
    assert diag["backoff_ends_utc"] is not None


@pytest.mark.asyncio
async def test_summary_store_handles_backoff_utc_failure(monkeypatch) -> None:
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    utcnow_calls = iter([now, RuntimeError("boom")])
    client = _DummySummaryClient()
    client.summary_v2 = AsyncMock(side_effect=RuntimeError("boom"))
    store = SummaryStore(lambda: client)

    def _fake_utcnow():
        value = next(utcnow_calls)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(
        "custom_components.enphase_ev.summary.dt_util.utcnow", _fake_utcnow
    )

    await store.async_fetch(force=True)
    diag = store.diagnostics()

    assert diag["last_failure_utc"] == now.isoformat()
    assert diag["backoff_ends_utc"] is None


def test_summary_store_retry_after_helper_handles_naive_dates(monkeypatch) -> None:
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    err = aiohttp.ClientResponseError(
        _make_request_info(),
        (),
        status=429,
        message="rate limited",
        headers={"Retry-After": "ignored"},
    )
    store = SummaryStore(lambda: _DummySummaryClient())

    monkeypatch.setattr(
        summary_mod, "parsedate_to_datetime", lambda _: datetime(2025, 1, 2)
    )
    monkeypatch.setattr(summary_mod.dt_util, "utcnow", lambda: now)

    delay = store._failure_backoff_delay(err, 1)  # noqa: SLF001

    assert delay > 0


def test_summary_store_retry_after_helper_handles_bad_dates(monkeypatch) -> None:
    err = aiohttp.ClientResponseError(
        _make_request_info(),
        (),
        status=429,
        message="rate limited",
        headers={"Retry-After": "ignored"},
    )
    store = SummaryStore(lambda: _DummySummaryClient())

    def _raise(_value: str) -> datetime:
        raise ValueError("bad date")

    monkeypatch.setattr(summary_mod, "parsedate_to_datetime", _raise)

    delay = store._failure_backoff_delay(err, 1)  # noqa: SLF001

    assert delay == SUMMARY_IDLE_TTL


def test_summary_store_cache_helpers() -> None:
    """Exercise helper branches in _get_cache and _as_list."""
    store = SummaryStore(lambda: _DummySummaryClient())
    assert store.ttl == SUMMARY_IDLE_TTL

    # len == 2 tuple falls back to default TTL
    store._cache = (time.monotonic(), [{"serialNumber": "A"}])
    cache = store._get_cache()
    assert cache and cache[2] == SUMMARY_IDLE_TTL

    # TTL mismatch should be rewritten when refreshing
    store._cache = (
        time.monotonic(),
        [{"serialNumber": "B"}],
        SUMMARY_ACTIVE_MIN_TTL,
    )
    store.prepare_refresh(want_fast=False, target_interval=None)
    cache = store._get_cache()
    assert cache and cache[2] == SUMMARY_IDLE_TTL

    # _as_list conversions
    assert store._as_list({"data": [1, 2]}) == [1, 2]
    assert store._as_list(({"x": 1}, {"y": 2})) == [{"x": 1}, {"y": 2}]
    assert store._as_list(None) == []
    assert store._as_list(object()) == []

    store._cache = "invalid"
    assert store._get_cache() is None


class _RaceSummaryStore(SummaryStore):
    def __init__(self, client_getter):
        super().__init__(client_getter)
        self._race_cache: tuple[float, list[dict], float] | None = None
        self._calls = 0

    def _get_cache(self):
        self._calls += 1
        if self._calls == 1:
            return None
        return self._race_cache


@pytest.mark.asyncio
async def test_summary_store_async_fetch_handles_race_condition() -> None:
    """Second cache check inside the lock should return without hitting the client."""
    client = _DummySummaryClient()
    store = _RaceSummaryStore(lambda: client)
    store._race_cache = (time.monotonic(), [{"serialNumber": "RACE"}], SUMMARY_IDLE_TTL)

    data = await store.async_fetch()
    assert data == [{"serialNumber": "RACE"}]
    assert client.calls == 0


class _DummySessionClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, int, int]] = []

    async def session_history(
        self,
        sn: str,
        *,
        start_date: str,
        end_date: str,
        offset: int,
        limit: int,
        **_kwargs,
    ) -> dict:
        self.calls.append((sn, start_date, end_date, offset, limit))
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
            }
        }


@pytest.mark.asyncio
async def test_session_history_fetch_caches_and_override(hass) -> None:
    """SessionHistoryManager should cache results and allow overrides."""
    await hass.config.async_set_time_zone("UTC")
    client = _DummySessionClient()
    manager = SessionHistoryManager(
        hass,
        lambda: client,
        cache_ttl=600,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )

    day = datetime(2025, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    sessions = await manager._async_fetch_sessions_today("EV-01", day_local=day)
    assert len(sessions) == 2
    assert len(client.calls) == 1

    # Cached result should be reused.
    again = await manager._async_fetch_sessions_today("EV-01", day_local=day)
    assert len(again) == 2
    assert len(client.calls) == 1

    # Override fetch logic (used by legacy tests) and ensure it is honored.
    async def _override(_sn: str, *_args, **_kwargs):
        return [{"session_id": "override", "energy_kwh": 1.0}]

    manager._cache.clear()
    manager.set_fetch_override(_override)
    override = await manager._async_fetch_sessions_today("EV-02", day_local=day)
    assert override == [{"session_id": "override", "energy_kwh": 1.0}]
    manager.set_fetch_override(None)


@pytest.mark.asyncio
async def test_session_history_fetch_calls_filter_criteria(hass) -> None:
    class _CriteriaClient:
        def __init__(self) -> None:
            self.criteria_calls = 0
            self.history_calls = 0

        async def session_history_filter_criteria(self, **_kwargs):
            self.criteria_calls += 1
            return {"data": [{"id": "EV-01"}]}

        async def session_history(self, *_args, **_kwargs) -> dict:
            self.history_calls += 1
            return {"data": {"result": [], "hasMore": False}}

    await hass.config.async_set_time_zone("UTC")
    client = _CriteriaClient()
    manager = SessionHistoryManager(
        hass,
        lambda: client,
        cache_ttl=600,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )
    day = datetime(2025, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    await manager._async_fetch_sessions_today("EV-01", day_local=day)
    assert client.criteria_calls == 1
    assert client.history_calls == 1


@pytest.mark.asyncio
async def test_session_history_valid_empty_results_cache_as_valid(hass) -> None:
    await hass.config.async_set_time_zone("UTC")

    class _EmptyClient:
        def __init__(self) -> None:
            self.calls = 0

        async def session_history(self, *_args, **_kwargs) -> dict:
            self.calls += 1
            return {"data": {"result": [], "hasMore": False}}

    client = _EmptyClient()
    manager = SessionHistoryManager(
        hass,
        lambda: client,
        cache_ttl=600,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )
    day = datetime(2025, 10, 16, 12, 0, 0, tzinfo=timezone.utc)

    first = await manager._async_fetch_sessions_today("EV-EMPTY", day_local=day)
    second = await manager._async_fetch_sessions_today("EV-EMPTY", day_local=day)
    entry = manager._cache[("EV-EMPTY", "2025-10-16")]

    assert first == []
    assert second == []
    assert client.calls == 1
    assert entry.state == SESSION_CACHE_STATE_VALID
    assert entry.has_valid_cache is True
    assert entry.sessions == []


@pytest.mark.asyncio
async def test_session_history_fetch_shares_filter_criteria_across_serials(
    hass,
) -> None:
    class _CriteriaClient:
        def __init__(self) -> None:
            self.criteria_calls = 0
            self.history_calls = 0

        async def session_history_filter_criteria(self, **_kwargs):
            self.criteria_calls += 1
            await asyncio.sleep(0)
            return {"data": [{"id": "EV-01"}]}

        async def session_history(self, *_args, **_kwargs) -> dict:
            self.history_calls += 1
            await asyncio.sleep(0)
            return {"data": {"result": [], "hasMore": False}}

    await hass.config.async_set_time_zone("UTC")
    client = _CriteriaClient()
    manager = SessionHistoryManager(
        hass,
        lambda: client,
        cache_ttl=600,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )
    day = datetime(2025, 10, 16, 12, 0, 0, tzinfo=timezone.utc)

    await manager.async_enrich(["EV-01", "EV-02", "EV-03"], day, in_background=False)

    assert client.criteria_calls == 1
    assert client.history_calls == 3


@pytest.mark.asyncio
async def test_session_history_filter_criteria_short_circuits_when_recent(
    hass,
) -> None:
    called = False

    async def _criteria_fetcher(**_kwargs):
        nonlocal called
        called = True
        return {"data": []}

    manager = SessionHistoryManager(
        hass,
        lambda: None,
        cache_ttl=600,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )
    manager._criteria_checked_mono = time.monotonic()

    await manager._async_refresh_filter_criteria(_criteria_fetcher)  # noqa: SLF001

    assert called is False


def test_session_history_apply_updates_merges_data(hass) -> None:
    """Applying updates should merge sessions and compute totals."""
    published: list[dict] = []
    base = {"EV-01": {"sn": "EV-01"}}
    manager = SessionHistoryManager(
        hass,
        lambda: None,
        cache_ttl=60,
        data_supplier=lambda: base,
        publish_callback=lambda data: published.append(data),
    )

    manager._apply_updates({"EV-01": [{"energy_kwh": 1.5}]})
    assert published
    merged = published[-1]
    assert merged["EV-01"]["energy_today_sessions_kwh"] == 1.5


def test_session_history_apply_updates_reuses_untouched_payloads(hass) -> None:
    """Applying updates should only clone payloads that actually change."""
    published: list[dict] = []
    untouched = {"sn": "EV-02"}
    base = {"EV-01": {"sn": "EV-01"}, "EV-02": untouched}
    manager = SessionHistoryManager(
        hass,
        lambda: None,
        cache_ttl=60,
        data_supplier=lambda: base,
        publish_callback=lambda data: published.append(data),
    )

    manager._apply_updates({"EV-01": [{"energy_kwh": 1.5}]})

    merged = published[-1]
    assert merged["EV-01"] is not base["EV-01"]
    assert merged["EV-02"] is untouched


def test_session_history_cache_view_states(hass) -> None:
    """Verify cache view bookkeeping for refresh, block, and reuse."""
    manager = SessionHistoryManager(
        hass,
        lambda: None,
        cache_ttl=10,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )
    cache_key = ("EV-01", "2025-10-16")
    manager._cache[cache_key] = (time.monotonic(), [{"session_id": "cached"}])

    fresh = manager.get_cache_view("EV-01", "2025-10-16", now_mono=time.monotonic())
    assert isinstance(fresh, SessionCacheView)
    assert fresh.needs_refresh is False
    assert fresh.state == SESSION_CACHE_STATE_VALID
    assert fresh.has_valid_cache is True
    assert fresh.last_error is None

    # Force expiration and block
    manager._cache[cache_key] = (time.monotonic() - 120, [{"session_id": "old"}])
    manager._block_until["EV-01"] = time.monotonic() + 100
    blocked = manager.get_cache_view("EV-01", "2025-10-16")
    assert blocked.needs_refresh is True
    assert blocked.blocked is True
    assert blocked.state == SESSION_CACHE_STATE_VALID
    assert blocked.has_valid_cache is True


def test_session_history_cache_view_marks_unavailable_without_valid_cache(hass) -> None:
    manager = SessionHistoryManager(
        hass,
        lambda: None,
        cache_ttl=10,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )
    manager._cache[("EV-01", "2025-10-16")] = SessionCacheEntry(
        cached_at_mono=time.monotonic(),
        sessions=[],
        state=SESSION_CACHE_STATE_UNAVAILABLE,
        last_error="down",
        has_valid_cache=False,
    )

    view = manager.get_cache_view("EV-01", "2025-10-16")

    assert view.sessions == []
    assert view.needs_refresh is True
    assert view.state == SESSION_CACHE_STATE_UNAVAILABLE
    assert view.has_valid_cache is False
    assert view.last_error == "down"


def test_session_history_cache_state_counts_handles_legacy_and_invalid_entries(
    hass,
) -> None:
    manager = SessionHistoryManager(
        hass,
        lambda: None,
        cache_ttl=10,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )
    manager._cache[("EV-VALID", "2025-10-16")] = ("bad-ts", [{"session_id": "cached"}])
    manager._cache[("EV-STALE", "2025-10-16")] = SessionCacheEntry(
        cached_at_mono=time.monotonic(),
        sessions=[{"session_id": "stale"}],
        state=SESSION_CACHE_STATE_STALE_REUSED,
        last_error="down",
        has_valid_cache=True,
    )
    manager._cache[("EV-BAD", "2025-10-16")] = "invalid"

    counts = manager.cache_state_counts()

    assert counts == {
        SESSION_CACHE_STATE_VALID: 1,
        SESSION_CACHE_STATE_STALE_REUSED: 1,
        SESSION_CACHE_STATE_UNAVAILABLE: 0,
    }
    coerced = manager._cache[("EV-VALID", "2025-10-16")]  # noqa: SLF001
    assert isinstance(coerced, SessionCacheEntry)
    assert coerced.cached_at_mono is None


def test_session_history_entry_error_text_handles_empty_values() -> None:
    manager = SessionHistoryManager(
        MagicMock(config=MagicMock(time_zone="UTC"), async_create_task=MagicMock()),
        lambda: None,
        cache_ttl=10,
    )
    assert manager._entry_error_text(None) is None  # noqa: SLF001
    assert manager._entry_error_text("   ") is None  # noqa: SLF001


def test_session_history_redaction_ignores_site_getter_errors(hass) -> None:
    def bad_site_id():
        raise RuntimeError("bad site")

    manager = SessionHistoryManager(
        hass,
        lambda: None,
        cache_ttl=10,
        site_id_getter=bad_site_id,
    )

    assert manager._redact_error("history failed") == "history failed"  # noqa: SLF001


def _make_request_info() -> RequestInfo:
    """Return a minimal request info for client errors."""
    return RequestInfo(
        URL("http://example.com"),
        "GET",
        CIMultiDictProxy(CIMultiDict()),
        None,
    )


@pytest.mark.asyncio
async def test_session_history_async_fetch_handles_errors(hass) -> None:
    """Cover unauthorized, server, client-less, and generic error paths."""
    await hass.config.async_set_time_zone("UTC")
    day = datetime(2025, 10, 16, tzinfo=timezone.utc)

    async def _invoke(exc):
        manager = SessionHistoryManager(
            hass,
            lambda: type("C", (), {"session_history": AsyncMock(side_effect=exc)})(),
            cache_ttl=60,
            data_supplier=lambda: {},
            publish_callback=lambda _: None,
        )
        sessions = await manager._async_fetch_sessions_today("EV-ERR", day_local=day)
        return manager, sessions

    manager, sessions = await _invoke(Unauthorized("bad"))
    assert sessions == []
    entry = manager._cache[("EV-ERR", day.strftime("%Y-%m-%d"))]
    assert entry.state == SESSION_CACHE_STATE_UNAVAILABLE
    assert entry.has_valid_cache is False

    response_error = aiohttp.ClientResponseError(
        _make_request_info(),
        (),
        status=503,
        message="unavailable",
    )
    manager, sessions = await _invoke(response_error)
    assert sessions == []
    entry = manager._cache[("EV-ERR", day.strftime("%Y-%m-%d"))]
    assert entry.state == SESSION_CACHE_STATE_UNAVAILABLE
    assert entry.has_valid_cache is False

    assert (
        await SessionHistoryManager(
            hass,
            lambda: None,
            cache_ttl=60,
            data_supplier=lambda: {},
            publish_callback=lambda _: None,
        )._async_fetch_sessions_today("EV-NULL", day_local=day)
    ) == []

    manager, sessions = await _invoke(RuntimeError("boom"))
    assert sessions == []
    entry = manager._cache[("EV-ERR", day.strftime("%Y-%m-%d"))]
    assert entry.state == SESSION_CACHE_STATE_UNAVAILABLE
    assert entry.has_valid_cache is False


@pytest.mark.asyncio
async def test_session_history_schedule_enrichment_runs(hass) -> None:
    """Background enrichment should call through to the manager."""
    await hass.config.async_set_time_zone("UTC")
    published: list[dict] = []
    manager = SessionHistoryManager(
        hass,
        lambda: _DummySessionClient(),
        cache_ttl=60,
        data_supplier=lambda: {"EV-01": {"sn": "EV-01"}},
        publish_callback=lambda data: published.append(data),
    )
    updates = {"EV-01": [{"energy_kwh": 2.0}]}
    manager._async_enrich_sessions = AsyncMock(return_value=updates)  # type: ignore[method-assign]

    manager.schedule_enrichment(
        ["EV-01", "EV-01", "EV-02"],
        datetime.now(tz=timezone.utc),
    )
    await hass.async_block_till_done()
    assert published and published[-1]["EV-01"]["energy_today_sessions_kwh"] == 2.0

    manager.schedule_enrichment([], datetime.now(tz=timezone.utc))


@pytest.mark.asyncio
async def test_session_history_max_cache_age_forces_early_refresh(hass, monkeypatch):
    """max_cache_age should bypass the default TTL when the caller wants fresher data."""
    await hass.config.async_set_time_zone("UTC")
    now = datetime(2025, 10, 16, tzinfo=timezone.utc)
    client = _DummySessionClient()
    manager = SessionHistoryManager(
        hass,
        lambda: client,
        cache_ttl=600,
        data_supplier=lambda: {"EV-01": {"sn": "EV-01"}},
        publish_callback=lambda _: None,
    )
    monkeypatch.setattr(sh_mod.dt_util, "now", lambda: now)
    monkeypatch.setattr(sh_mod.dt_util, "as_local", lambda value: value)

    day_key = now.strftime("%Y-%m-%d")
    manager._cache[("EV-01", day_key)] = (time.monotonic() - 180, ["cached"])
    sessions = await manager._async_fetch_sessions_today(
        "EV-01",
        day_local=now,
        max_cache_age=120,
    )

    assert len(sessions) == 2
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_session_history_invalid_max_cache_age_falls_back_to_default_ttl(
    hass, monkeypatch
):
    """Invalid max_cache_age values should preserve the default cache TTL."""
    await hass.config.async_set_time_zone("UTC")
    now = datetime(2025, 10, 16, tzinfo=timezone.utc)
    client = _DummySessionClient()
    manager = SessionHistoryManager(
        hass,
        lambda: client,
        cache_ttl=600,
        data_supplier=lambda: {"EV-01": {"sn": "EV-01"}},
        publish_callback=lambda _: None,
    )
    monkeypatch.setattr(sh_mod.dt_util, "now", lambda: now)
    monkeypatch.setattr(sh_mod.dt_util, "as_local", lambda value: value)

    day_key = now.strftime("%Y-%m-%d")
    manager._cache[("EV-01", day_key)] = (time.monotonic() - 180, ["cached"])

    sessions = await manager._async_fetch_sessions_today(
        "EV-01",
        day_local=now,
        max_cache_age="bad",
    )

    assert sessions == ["cached"]
    assert client.calls == []


@pytest.mark.asyncio
async def test_session_history_async_enrich_handles_failures(hass, monkeypatch) -> None:
    """Ensure async_enrich collects updates even when some refreshes fail."""
    await hass.config.async_set_time_zone("UTC")
    manager = SessionHistoryManager(
        hass,
        lambda: _DummySessionClient(),
        cache_ttl=60,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )

    async def _fake_fetch(sn: str, *, day_local=None):
        if sn == "bad":
            raise RuntimeError("boom")
        return [{"session_id": sn, "energy_kwh": 1.0}]

    monkeypatch.setattr(manager, "_async_fetch_sessions_today", _fake_fetch)

    updates = await manager.async_enrich(
        ["good", "bad", "good"], datetime.now(tz=timezone.utc), in_background=False
    )
    assert updates == {"good": [{"session_id": "good", "energy_kwh": 1.0}]}


@pytest.mark.asyncio
async def test_session_history_async_enrich_handles_special_exceptions(
    hass, monkeypatch
) -> None:
    """Unauthorized fetches and unexpected task failures should be ignored safely."""
    await hass.config.async_set_time_zone("UTC")
    manager = SessionHistoryManager(
        hass,
        lambda: _DummySessionClient(),
        cache_ttl=60,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )

    async def _fake_fetch(sn: str, *, day_local=None):
        if sn == "unauth":
            raise Unauthorized("denied")
        if sn == "cancel":
            raise asyncio.CancelledError()
        return [{"session_id": sn, "energy_kwh": 0.25}]

    monkeypatch.setattr(
        manager,
        "_async_fetch_sessions_today",
        AsyncMock(side_effect=_fake_fetch),
    )

    day = datetime(2025, 10, 16, tzinfo=timezone.utc)
    updates = await manager._async_enrich_sessions(
        ["ok", "unauth", "cancel"], day_local=day
    )
    assert updates == {"ok": [{"session_id": "ok", "energy_kwh": 0.25}]}


def test_session_history_apply_updates_noop_without_inputs(hass) -> None:
    """_apply_updates should return immediately when required hooks are missing."""
    manager = SessionHistoryManager(
        hass,
        lambda: None,
        cache_ttl=60,
        data_supplier=None,
        publish_callback=None,
    )
    manager._apply_updates(None)
    manager._apply_updates({})


@pytest.mark.asyncio
async def test_session_history_async_enrich_no_valid_serials(hass) -> None:
    """Empty serial list should short-circuit."""
    manager = SessionHistoryManager(
        hass,
        lambda: _DummySessionClient(),
        cache_ttl=60,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )
    result = await manager._async_enrich_sessions(
        ["", None], day_local=datetime.now(tz=timezone.utc)
    )
    assert result == {}


@pytest.mark.asyncio
async def test_session_history_async_enrich_in_background(hass, monkeypatch) -> None:
    """in_background=True should still apply updates."""
    await hass.config.async_set_time_zone("UTC")
    published: list[dict] = []
    manager = SessionHistoryManager(
        hass,
        lambda: _DummySessionClient(),
        cache_ttl=60,
        data_supplier=lambda: {"EV-01": {"sn": "EV-01"}},
        publish_callback=lambda data: published.append(data),
    )

    async def _fake_fetch(sn: str, *, day_local=None):
        return [{"session_id": sn, "energy_kwh": 0.5}]

    monkeypatch.setattr(manager, "_async_fetch_sessions_today", _fake_fetch)
    updates = await manager.async_enrich(
        ["EV-01"],
        datetime.now(tz=timezone.utc),
        in_background=True,
    )
    assert updates == {"EV-01": [{"session_id": "EV-01", "energy_kwh": 0.5}]}
    assert published


def test_session_history_cache_ttl_accessor(hass) -> None:
    """cache_ttl setter and getters should enforce sane bounds."""
    manager = SessionHistoryManager(
        hass,
        lambda: None,
        cache_ttl=120,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )
    assert manager.cache_ttl == max(MIN_SESSION_HISTORY_CACHE_TTL, 120)
    manager.cache_ttl = None
    assert manager.cache_ttl == MIN_SESSION_HISTORY_CACHE_TTL
    manager.cache_ttl = "5"
    assert manager.cache_ttl == MIN_SESSION_HISTORY_CACHE_TTL
    manager.cache_ttl = "bad"
    assert manager.cache_ttl == MIN_SESSION_HISTORY_CACHE_TTL
    assert manager.cache_key_count == 0
    assert manager.in_progress == 0


def test_session_history_prune_bounds_cache_and_serial_state(hass) -> None:
    manager = SessionHistoryManager(
        hass,
        lambda: None,
        cache_ttl=60,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )
    manager._cache = {
        ("EV-01", "2020-01-01"): (1.0, [{"session_id": "old"}]),
        ("EV-01", "2020-01-02"): (2.0, [{"session_id": "keep"}]),
        ("EV-OLD", "2020-01-02"): (3.0, [{"session_id": "stale-serial"}]),
    }
    manager._block_until = {
        "EV-01": time.monotonic() - 1,
        "EV-OLD": time.monotonic() + 60,
    }
    manager._criteria_checked_mono = 1.0
    manager._refresh_in_progress = {"EV-01", "EV-OLD"}

    manager.prune(active_serials={"EV-01"}, keep_day_keys={"2020-01-02"})

    assert manager._cache == {("EV-01", "2020-01-02"): (2.0, [{"session_id": "keep"}])}
    assert "EV-OLD" not in manager._block_until
    assert "EV-01" not in manager._block_until
    assert manager._criteria_checked_mono == 1.0
    assert manager._refresh_in_progress == {"EV-01"}


@pytest.mark.asyncio
async def test_session_history_clear_cancels_tasks_and_clears_state(hass) -> None:
    manager = SessionHistoryManager(
        hass,
        lambda: None,
        cache_ttl=60,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )
    manager._cache[("EV-01", "2020-01-02")] = (time.monotonic(), [])
    manager._block_until["EV-01"] = time.monotonic() + 60
    manager._criteria_checked_mono = time.monotonic()
    manager._refresh_in_progress.add("EV-01")
    task = asyncio.create_task(asyncio.sleep(30))
    manager._enrichment_tasks.add(task)

    manager.clear()
    await asyncio.sleep(0)

    assert task.cancelled()
    assert manager._cache == {}
    assert manager._block_until == {}
    assert manager._criteria_checked_mono is None
    assert manager._refresh_in_progress == set()
    assert manager._enrichment_tasks == set()


def test_session_history_prune_helpers_cover_edge_paths(monkeypatch, hass) -> None:
    manager = SessionHistoryManager(
        hass,
        lambda: None,
        cache_ttl=60,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )

    class BadSerial:
        def __str__(self) -> str:
            raise RuntimeError("bad")

    assert manager._normalize_serials(None) is None
    assert manager._normalize_serials([None, BadSerial(), " EV1 "]) == {"EV1"}

    def _raise_supplier():
        raise RuntimeError("boom")

    manager._data_supplier = _raise_supplier
    assert manager._active_serials_from_data_supplier() is None

    monkeypatch.setattr(
        sh_mod.dt_util,
        "now",
        MagicMock(side_effect=RuntimeError("boom")),
    )
    retained = manager._retained_day_keys({"2025-01-01"})
    assert "2025-01-01" in retained


@pytest.mark.asyncio
async def test_session_history_async_fetch_handles_invalid_payload(hass) -> None:
    """Non-list payload should be ignored safely."""
    await hass.config.async_set_time_zone("UTC")

    class BadClient:
        async def session_history(self, *_args, **_kwargs):
            return {"data": {"result": "not-a-list", "hasMore": False}}

    manager = SessionHistoryManager(
        hass,
        lambda: BadClient(),
        cache_ttl=60,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )
    sessions = await manager._async_fetch_sessions_today(
        "EV-BAD", day_local=datetime(2025, 10, 16, tzinfo=timezone.utc)
    )
    assert sessions == []


@pytest.mark.asyncio
async def test_session_history_reuses_cached_data_on_invalid_payload(hass) -> None:
    await hass.config.async_set_time_zone("UTC")
    day = datetime(2025, 10, 16, tzinfo=timezone.utc)

    class BadClient:
        async def session_history_filter_criteria(self, **_kwargs):
            return {"data": [{"id": "EV-BAD"}]}

        async def session_history(self, *_args, **_kwargs):
            raise InvalidPayloadError(
                "Invalid JSON response (status=200, endpoint=/session_history)",
                status=200,
                endpoint="/session_history",
                content_type="application/json",
                failure_kind="json_decode",
                decode_error="JSONDecodeError",
                body_length=10,
                body_sha256="deadbeef",
                body_preview_redacted='{"bad":1}',
            )

    manager = SessionHistoryManager(
        hass,
        lambda: BadClient(),
        cache_ttl=60,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )
    day_key = day.strftime("%Y-%m-%d")
    manager._cache[("EV-BAD", day_key)] = (
        time.monotonic() - 120,
        [{"session_id": "cached"}],
    )

    sessions = await manager._async_fetch_sessions_today("EV-BAD", day_local=day)

    assert sessions == [{"session_id": "cached"}]
    assert manager.service_available is False
    assert manager.service_using_stale is True
    assert manager.service_last_error is not None
    entry = manager._cache[("EV-BAD", day_key)]
    assert entry.state == SESSION_CACHE_STATE_STALE_REUSED
    assert entry.has_valid_cache is True
    assert manager._service_last_payload_signature == {
        "endpoint": "/session_history",
        "status": 200,
        "content_type": "application/json",
        "failure_kind": "json_decode",
        "decode_error": "JSONDecodeError",
        "body_length": 10,
        "body_sha256": "deadbeef",
        "body_preview_redacted": '{"bad":1}',
    }


@pytest.mark.asyncio
async def test_session_history_reuses_cached_data_when_criteria_unavailable(
    hass,
) -> None:
    await hass.config.async_set_time_zone("UTC")
    day = datetime(2025, 10, 16, tzinfo=timezone.utc)

    class BadClient:
        async def session_history_filter_criteria(self, **_kwargs):
            raise SessionHistoryUnavailable("criteria down")

    manager = SessionHistoryManager(
        hass,
        lambda: BadClient(),
        cache_ttl=60,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )
    day_key = day.strftime("%Y-%m-%d")
    manager._cache[("EV-BAD", day_key)] = (
        time.monotonic() - 120,
        [{"session_id": "cached-criteria"}],
    )

    sessions = await manager._async_fetch_sessions_today("EV-BAD", day_local=day)

    assert sessions == [{"session_id": "cached-criteria"}]
    assert manager.service_using_stale is True
    entry = manager._cache[("EV-BAD", day_key)]
    assert entry.state == SESSION_CACHE_STATE_STALE_REUSED
    assert entry.has_valid_cache is True


@pytest.mark.asyncio
async def test_session_history_reuses_cached_data_when_criteria_fails_generically(
    hass,
) -> None:
    await hass.config.async_set_time_zone("UTC")
    day = datetime(2025, 10, 16, tzinfo=timezone.utc)

    class BadClient:
        async def session_history_filter_criteria(self, **_kwargs):
            raise RuntimeError("criteria boom")

    manager = SessionHistoryManager(
        hass,
        lambda: BadClient(),
        cache_ttl=60,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )
    day_key = day.strftime("%Y-%m-%d")
    manager._cache[("EV-BAD", day_key)] = (
        time.monotonic() - 120,
        [{"session_id": "cached-criteria-generic"}],
    )

    sessions = await manager._async_fetch_sessions_today("EV-BAD", day_local=day)

    assert sessions == [{"session_id": "cached-criteria-generic"}]
    assert manager.service_using_stale is True
    entry = manager._cache[("EV-BAD", day_key)]
    assert entry.state == SESSION_CACHE_STATE_STALE_REUSED
    assert entry.has_valid_cache is True


@pytest.mark.asyncio
async def test_session_history_reuses_cached_data_when_service_unavailable(
    hass,
) -> None:
    await hass.config.async_set_time_zone("UTC")
    day = datetime(2025, 10, 16, tzinfo=timezone.utc)

    class BadClient:
        async def session_history_filter_criteria(self, **_kwargs):
            return {"data": [{"id": "EV-BAD"}]}

        async def session_history(self, *_args, **_kwargs):
            raise SessionHistoryUnavailable("service down")

    manager = SessionHistoryManager(
        hass,
        lambda: BadClient(),
        cache_ttl=60,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )
    day_key = day.strftime("%Y-%m-%d")
    manager._cache[("EV-BAD", day_key)] = (
        time.monotonic() - 120,
        [{"session_id": "cached-page"}],
    )

    sessions = await manager._async_fetch_sessions_today("EV-BAD", day_local=day)

    assert sessions == [{"session_id": "cached-page"}]
    assert manager.service_using_stale is True
    entry = manager._cache[("EV-BAD", day_key)]
    assert entry.state == SESSION_CACHE_STATE_STALE_REUSED
    assert entry.has_valid_cache is True


@pytest.mark.asyncio
async def test_session_history_fetch_handles_empty_serial_and_block(
    hass, monkeypatch
) -> None:
    """Empty serials and active block entries should short-circuit fetches."""
    await hass.config.async_set_time_zone("UTC")

    class DummyClient:
        async def session_history(self, *_args, **_kwargs):
            return {"data": {"result": [], "hasMore": False}}

    manager = SessionHistoryManager(
        hass,
        lambda: DummyClient(),
        cache_ttl=60,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )

    day = datetime(2025, 10, 16, tzinfo=timezone.utc)
    assert await manager._async_fetch_sessions_today("", day_local=day) == []

    naive_now = datetime(2025, 10, 17, 8, 0, 0)
    monkeypatch.setattr(sh_mod.dt_util, "now", lambda: naive_now)
    orig_as_local = sh_mod.dt_util.as_local
    calls = {"count": 0}

    def _fake_as_local(value):
        if calls["count"] == 0:
            calls["count"] += 1
            raise ValueError("tz boom")
        return orig_as_local(value)

    monkeypatch.setattr(sh_mod.dt_util, "as_local", _fake_as_local)

    assert await manager._async_fetch_sessions_today("EV-ALPHA", day_local=None) == []

    day_local = orig_as_local(naive_now.replace(tzinfo=timezone.utc))
    day_key = day_local.strftime("%Y-%m-%d")
    cache_key = ("EV-ALPHA", day_key)
    manager._cache[cache_key] = (time.monotonic(), ["cached"])
    manager._block_until["EV-ALPHA"] = time.monotonic() + 30
    cached = await manager._async_fetch_sessions_today("EV-ALPHA", day_local=day_local)
    assert cached == ["cached"]


def test_session_history_blocked_without_valid_cache_remains_unavailable(hass) -> None:
    manager = SessionHistoryManager(
        hass,
        lambda: None,
        cache_ttl=60,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )
    day_key = "2025-10-16"
    manager._cache[("EV-ALPHA", day_key)] = SessionCacheEntry(
        cached_at_mono=time.monotonic(),
        sessions=[],
        state=SESSION_CACHE_STATE_UNAVAILABLE,
        last_error="down",
        has_valid_cache=False,
    )
    manager._block_until["EV-ALPHA"] = time.monotonic() + 30

    view = manager.get_cache_view("EV-ALPHA", day_key)

    assert view.blocked is True
    assert view.state == SESSION_CACHE_STATE_UNAVAILABLE
    assert view.has_valid_cache is False


@pytest.mark.asyncio
async def test_session_history_fetch_handles_page_unauthorized(hass) -> None:
    """Unauthorized responses during pagination should mark the entry unavailable."""

    class FailingClient:
        async def session_history(self, *_args, **_kwargs):
            raise Unauthorized("blocked")

    await hass.config.async_set_time_zone("UTC")
    manager = SessionHistoryManager(
        hass,
        lambda: FailingClient(),
        cache_ttl=60,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )

    day = datetime(2025, 10, 16, tzinfo=timezone.utc)
    sessions = await manager._async_fetch_sessions_today("EV-DENY", day_local=day)
    assert sessions == []
    day_key = day.strftime("%Y-%m-%d")
    entry = manager._cache[("EV-DENY", day_key)]
    assert entry.sessions == []
    assert entry.state == SESSION_CACHE_STATE_UNAVAILABLE
    assert entry.has_valid_cache is False


def test_session_history_normalise_handles_parse_failures(hass, monkeypatch) -> None:
    """Normalisation should survive malformed timestamps and metrics."""
    manager = SessionHistoryManager(
        hass,
        lambda: None,
        cache_ttl=60,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )
    local_dt = datetime(2025, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    orig_as_local = sh_mod.dt_util.as_local
    flags = {"outer": True, "inner": True}

    def _fake_as_local(value):
        if value is local_dt and flags["outer"]:
            flags["outer"] = False
            raise ValueError("outer")
        if (
            isinstance(value, datetime)
            and value.year == 2025
            and value.month == 10
            and value.day == 16
            and value.hour == 2
            and flags["inner"]
        ):
            flags["inner"] = False
            raise ValueError("inner")
        return orig_as_local(value)

    monkeypatch.setattr(sh_mod.dt_util, "as_local", _fake_as_local)

    orig_datetime = sh_mod.datetime

    class _FakeDatetime:
        @staticmethod
        def fromtimestamp(val, tz=None):
            if int(val) == 24601:
                raise OverflowError
            return orig_datetime.fromtimestamp(val, tz=tz)

        @staticmethod
        def fromisoformat(value):
            return orig_datetime.fromisoformat(value)

    monkeypatch.setattr(sh_mod, "datetime", _FakeDatetime)

    entries = [
        None,
        {
            "sessionId": "bad-numeric",
            "startTime": 24601,
            "endTime": 24700,
            "aggEnergyValue": "bad",
            "activeChargeTime": "bad",
            "costCalculated": "yes",
        },
        {
            "sessionId": "bad-iso",
            "startTime": "not-a-date",
            "endTime": "2025-10-16T00:30:00Z",
            "aggEnergyValue": 0.5,
            "manualOverridden": 1,
        },
        {
            "sessionId": "naive",
            "startTime": "2025-10-16T02:00:00",
            "endTime": "2025-10-16T02:10:00",
            "aggEnergyValue": 1.5,
            "activeChargeTime": True,
        },
        {
            "sessionId": "window",
            "startTime": "2025-10-16T03:00:00Z",
            "endTime": "2025-10-16T02:30:00Z",
            "aggEnergyValue": 2.0,
            "activeChargeTime": 60,
        },
        {
            "sessionId": "outside",
            "startTime": "2025-10-15T00:00:00Z",
            "endTime": "2025-10-15T00:10:00Z",
            "aggEnergyValue": 1.0,
            "activeChargeTime": 600,
        },
        {
            "sessionId": "good",
            "startTime": "2025-10-16T05:00:00Z",
            "endTime": None,
            "aggEnergyValue": 1.0,
            "activeChargeTime": 1200,
            "costCalculated": True,
            "manualOverridden": False,
            "milesAdded": "12.5",
            "sessionCost": "3.5",
            "avgCostPerUnitEnergy": "0.2",
            "sessionCostState": "estimated",
            "chargeProfileStackLevel": "2",
        },
    ]
    sessions = manager._normalise_sessions_for_day(local_dt=local_dt, results=entries)
    assert any(entry["session_id"] == "good" for entry in sessions)


@pytest.mark.asyncio
async def test_evse_timeseries_diagnostics_reports_stale_invalid_payload(hass) -> None:
    class BadClient:
        async def evse_timeseries_lifetime_energy(self):
            raise InvalidPayloadError(
                "Invalid JSON response (status=200, endpoint=/evse/lifetime)",
                status=200,
                endpoint="/evse/lifetime",
                content_type="application/json",
                failure_kind="json_decode",
                decode_error="JSONDecodeError",
                body_length=9,
                body_sha256="cafefeed",
                body_preview_redacted='{"bad":2}',
            )

        async def evse_timeseries_daily_energy(self, *, start_date):
            assert start_date is not None
            raise InvalidPayloadError(
                "Invalid JSON response (status=200, endpoint=/evse/daily)",
                status=200,
                endpoint="/evse/daily",
                content_type="application/json",
                failure_kind="json_decode",
                decode_error="JSONDecodeError",
                body_length=11,
                body_sha256="feedcafe",
                body_preview_redacted='{"bad":22}',
            )

    manager = EVSETimeseriesManager(hass, lambda: BadClient())
    manager._lifetime_cache = (
        time.monotonic() - 120,
        {"EV-01": {"energy_kwh": 123.4}},
    )
    manager._daily_cache["2025-10-16"] = (
        time.monotonic() - 120,
        {"EV-01": {"energy_kwh": 5.6}},
    )

    await manager.async_refresh(
        day_local=datetime(2025, 10, 16, tzinfo=timezone.utc),
        force=True,
    )

    diag = manager.diagnostics()
    assert diag["using_stale"] is True
    assert diag["daily"]["using_stale"] is True
    assert diag["lifetime"]["using_stale"] is True
    assert diag["daily"]["last_payload_signature"]["endpoint"] == "/evse/daily"
    assert diag["lifetime"]["last_payload_signature"]["endpoint"] == "/evse/lifetime"


def test_session_history_normalise_adjusts_windows(hass) -> None:
    """Ensure window adjustments handle inverted timestamps and range clipping."""
    manager = SessionHistoryManager(
        hass,
        lambda: None,
        cache_ttl=60,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )
    local_dt = datetime(2025, 10, 16, 0, 0, 0, tzinfo=timezone.utc)
    entries = [
        {
            "sessionId": "inverted",
            "startTime": "2025-10-16T05:00:00Z",
            "endTime": "2025-10-16T04:00:00Z",
            "aggEnergyValue": 1.0,
            "activeChargeTime": 0,
        },
        {
            "sessionId": "overlap",
            "startTime": "2025-10-15T23:30:00Z",
            "endTime": "2025-10-16T01:00:00Z",
            "aggEnergyValue": 2.0,
            "activeChargeTime": 1800,
        },
    ]
    sessions = manager._normalise_sessions_for_day(local_dt=local_dt, results=entries)
    assert len(sessions) == 2


def test_session_history_sum_energy_handles_invalid_entries(hass) -> None:
    """sum_energy should ignore entries that cannot be coerced to floats."""
    manager = SessionHistoryManager(
        hass,
        lambda: None,
        cache_ttl=60,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )

    class BadNumber:
        def __float__(self):
            raise ValueError("boom")

    total = manager._sum_session_energy(
        [{"energy_kwh": 1.5}, {"energy_kwh": BadNumber()}]
    )
    assert total == pytest.approx(1.5)


@pytest.mark.asyncio
async def test_session_history_normalise_various_paths(hass) -> None:
    """Exercise the different normalization branches."""
    await hass.config.async_set_time_zone("UTC")
    manager = SessionHistoryManager(
        hass,
        lambda: None,
        cache_ttl=60,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )
    local_dt = datetime(2025, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    entries = [
        {
            "sessionId": "cross-midnight",
            "startTime": "2025-10-15T23:30:00Z[UTC]",
            "endTime": "2025-10-16T01:30:00Z[UTC]",
            "aggEnergyValue": 6.0,
            "activeChargeTime": 7200,
        },
        {
            "sessionId": "timestamp",
            "startTime": None,
            "endTime": (
                datetime(2025, 10, 16, 4, 0, 0, tzinfo=timezone.utc)
            ).timestamp(),
            "aggEnergyValue": 3.0,
            "activeChargeTime": 0,
        },
        {
            "sessionId": "skip",
            "startTime": None,
            "endTime": None,
        },
    ]
    sessions = manager._normalise_sessions_for_day(local_dt=local_dt, results=entries)
    assert len(sessions) == 2
    assert sessions[0]["session_id"] == "cross-midnight"
    assert sessions[1]["session_id"] == "timestamp"


def test_session_history_sum_energy_helper(hass) -> None:
    """sum_energy wrapper should aggregate numeric values."""
    manager = SessionHistoryManager(
        hass,
        lambda: None,
        cache_ttl=60,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )
    total = manager.sum_energy(
        [{"energy_kwh": 1.5}, {"energy_kwh": "2.5"}, {"energy_kwh": None}]
    )
    assert total == pytest.approx(1.5, abs=1e-6)
