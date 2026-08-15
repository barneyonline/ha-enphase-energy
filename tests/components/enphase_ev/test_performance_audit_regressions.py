from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from homeassistant.util import dt as dt_util

from custom_components.enphase_ev import (
    async_create_clientsession as lazy_create_clientsession,
)
from custom_components.enphase_ev.config_flow import (
    async_get_clientsession as lazy_get_clientsession,
)
from custom_components.enphase_ev.const import (
    DEFAULT_CHARGE_LEVEL_SETTING,
    PHASE_SWITCH_CONFIG_SETTING,
)
from custom_components.enphase_ev.session_history import SessionCacheView

from tests.components.enphase_ev.random_ids import RANDOM_SERIAL


@pytest.mark.parametrize(
    "target",
    (
        "custom_components.enphase_ev",
        "custom_components.enphase_ev.config_flow",
    ),
)
def test_import_keeps_heavy_modules_lazy(target: str) -> None:
    script = f"""
import importlib
import json
import sys

importlib.import_module({target!r})
print(json.dumps({{
    name: name in sys.modules
    for name in (
        {target!r},
        "custom_components.enphase_ev.services",
        "homeassistant.components.recorder",
        "homeassistant.components.recorder.statistics",
        "homeassistant.helpers.aiohttp_client",
        "homeassistant.components.zeroconf",
        "hass_nabucasa",
        "boto3",
    )
}}))
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        check=True,
        text=True,
    )
    modules = json.loads(result.stdout)

    assert modules.pop(target) is True
    assert not any(modules.values())


@pytest.mark.asyncio
async def test_lazy_clientsession_wrappers_delegate(monkeypatch) -> None:
    from homeassistant.helpers import aiohttp_client

    get_session = MagicMock(return_value=object())
    create_session = MagicMock(return_value=object())
    monkeypatch.setattr(aiohttp_client, "async_get_clientsession", get_session)
    monkeypatch.setattr(aiohttp_client, "async_create_clientsession", create_session)
    hass = MagicMock()
    hass.async_add_import_executor_job = AsyncMock(side_effect=lambda job: job())

    assert await lazy_get_clientsession(hass) is get_session.return_value
    assert (
        lazy_create_clientsession(hass, auto_cleanup=True)
        is create_session.return_value
    )
    hass.async_add_import_executor_job.assert_awaited_once()
    get_session.assert_called_once_with(hass)
    create_session.assert_called_once_with(hass, auto_cleanup=True)


def _prepare_refresh_target(
    coord,
    monkeypatch,
    *,
    charging: bool,
    first_refresh: bool,
    session_view: SessionCacheView,
) -> None:
    now_local = datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: now_local)

    coord._has_successful_refresh = not first_refresh  # noqa: SLF001
    coord._scheduler_available = True  # noqa: SLF001
    coord.data = {RANDOM_SERIAL: {"display_name": "Garage EV"}}
    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": RANDOM_SERIAL,
                    "name": "Garage EV",
                    "charging": charging,
                    "pluggedIn": charging,
                    "charge_mode": "IMMEDIATE",
                    "connectors": [{}],
                    "session_d": {},
                    "sch_d": {},
                }
            ],
            "ts": 1_700_000_000,
        }
    )
    coord.summary = SimpleNamespace(
        prepare_refresh=lambda **_kwargs: False,
        async_fetch=AsyncMock(
            return_value=[{"serialNumber": RANDOM_SERIAL, "displayName": "Garage EV"}]
        ),
        invalidate=lambda: None,
    )
    coord.evse_timeseries = SimpleNamespace(
        refresh_due=MagicMock(return_value=False),
        async_refresh=AsyncMock(),
        merge_charger_payloads=MagicMock(),
        diagnostics=lambda: {},
    )
    coord._async_run_post_status_refresh_pipeline = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        return_value=None
    )
    coord._async_run_post_session_refresh_pipeline = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        return_value=None
    )
    coord._async_resolve_green_battery_settings = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        return_value={}
    )
    coord._async_resolve_auth_settings = AsyncMock(return_value={})  # noqa: SLF001
    coord._async_resolve_charger_config = AsyncMock(return_value={})  # noqa: SLF001
    coord.session_history.get_cache_view = MagicMock(return_value=session_view)  # type: ignore[assignment]
    coord._async_enrich_sessions = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        return_value={RANDOM_SERIAL: [{"session_id": "fresh", "energy_kwh": 2.0}]}
    )
    coord._schedule_session_enrichment = MagicMock()  # type: ignore[assignment]  # noqa: SLF001
    coord._prune_runtime_caches = MagicMock()  # type: ignore[assignment]  # noqa: SLF001
    coord.energy._async_refresh_site_energy = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._sync_site_energy_issue = MagicMock()  # noqa: SLF001
    coord._sync_battery_profile_pending_issue = MagicMock()  # noqa: SLF001


def test_topology_refresh_reuses_summary_caches_for_unchanged_sources(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=["EV1"])
    runtime = coord.inventory_runtime
    runtime._build_gateway_inventory_summary = MagicMock(  # type: ignore[method-assign]
        return_value={"gateway": "summary"}
    )
    runtime._build_microinverter_inventory_summary = MagicMock(  # type: ignore[method-assign]
        return_value={"micro": "summary"}
    )
    runtime._build_heatpump_inventory_summary = MagicMock(  # type: ignore[method-assign]
        return_value={"heatpump": "summary"}
    )
    runtime._build_heatpump_type_summaries = MagicMock(  # type: ignore[method-assign]
        return_value={"HPWH": {"member_count": 1}}
    )
    runtime.gateway_iq_energy_router_records = MagicMock(  # type: ignore[method-assign]
        return_value=[]
    )
    runtime._gateway_iq_energy_router_summary_records = MagicMock(  # type: ignore[method-assign]
        return_value=[]
    )

    assert runtime._refresh_cached_topology() is True  # noqa: SLF001
    assert runtime._refresh_cached_topology() is False  # noqa: SLF001

    runtime._build_gateway_inventory_summary.assert_called_once()
    runtime._build_microinverter_inventory_summary.assert_called_once()
    runtime._build_heatpump_inventory_summary.assert_called_once()
    runtime._build_heatpump_type_summaries.assert_called_once()
    runtime._gateway_iq_energy_router_summary_records.assert_called_once()

    coord._hems_devices_payload = {"changed": True}  # noqa: SLF001

    assert runtime._refresh_cached_topology() is False  # noqa: SLF001

    runtime._build_gateway_inventory_summary.assert_called_once()
    runtime._build_microinverter_inventory_summary.assert_called_once()
    assert runtime._build_heatpump_inventory_summary.call_count == 2
    assert runtime._build_heatpump_type_summaries.call_count == 2
    assert runtime._gateway_iq_energy_router_summary_records.call_count == 2


def test_session_history_apply_updates_skips_unchanged_publish(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=["EV1", "EV2"])
    sessions = [{"session_id": "cached", "energy_kwh": 1.25}]
    coord.data = {
        "EV1": {
            "display_name": "Garage EV",
            "energy_today_sessions": sessions,
            "energy_today_sessions_kwh": 1.25,
        },
        "EV2": {"display_name": "Driveway EV"},
    }
    publish = MagicMock()
    coord.session_history._publish_callback = publish  # noqa: SLF001

    coord.session_history._apply_updates({"EV1": list(sessions)})  # noqa: SLF001

    publish.assert_not_called()

    fresh_sessions = [{"session_id": "fresh", "energy_kwh": 2.0}]

    coord.session_history._apply_updates({"EV1": fresh_sessions})  # noqa: SLF001

    publish.assert_called_once()
    merged = publish.call_args.args[0]
    assert merged["EV1"]["energy_today_sessions"] == fresh_sessions
    assert merged["EV1"]["energy_today_sessions_kwh"] == 2.0
    assert merged["EV2"] is coord.data["EV2"]


def test_coordinator_summary_wrappers_cache_empty_summaries(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.inventory_runtime._gateway_inventory_summary_marker = MagicMock(  # type: ignore[method-assign]
        return_value=("gateway",)
    )
    coord.inventory_runtime._microinverter_inventory_summary_marker = MagicMock(  # type: ignore[method-assign]
        return_value=("micro",)
    )
    coord.inventory_runtime._heatpump_inventory_summary_marker = MagicMock(  # type: ignore[method-assign]
        return_value=("heatpump",)
    )
    coord.inventory_runtime._build_gateway_inventory_summary = MagicMock(  # type: ignore[method-assign]
        return_value={}
    )
    coord.inventory_runtime._build_microinverter_inventory_summary = MagicMock(  # type: ignore[method-assign]
        return_value={}
    )
    coord.inventory_runtime._build_heatpump_inventory_summary = MagicMock(  # type: ignore[method-assign]
        return_value={}
    )

    assert coord.gateway_inventory_summary() == {}
    assert coord.gateway_inventory_summary() == {}
    assert coord.microinverter_inventory_summary() == {}
    assert coord.microinverter_inventory_summary() == {}
    assert coord.heatpump_inventory_summary() == {}
    assert coord.heatpump_inventory_summary() == {}

    coord.inventory_runtime._build_gateway_inventory_summary.assert_called_once()
    coord.inventory_runtime._build_microinverter_inventory_summary.assert_called_once()
    coord.inventory_runtime._build_heatpump_inventory_summary.assert_called_once()


@pytest.mark.asyncio
async def test_post_status_evse_enrichment_uses_hot_caches_without_followup_io(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=["EV1"])
    now = time.monotonic()
    coord._green_battery_cache["EV1"] = (True, True, now)  # noqa: SLF001
    coord._auth_settings_cache["EV1"] = (True, False, True, True, now)  # noqa: SLF001
    coord._charger_config_cache["EV1"] = (  # noqa: SLF001
        {
            DEFAULT_CHARGE_LEVEL_SETTING: 32,
            PHASE_SWITCH_CONFIG_SETTING: "three_phase",
        },
        now,
    )
    coord.evse_runtime.async_get_green_battery_setting = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("green battery lookup should stay cached")
    )
    coord.evse_runtime.async_get_auth_settings = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("auth settings lookup should stay cached")
    )
    coord.client.charger_config = AsyncMock(
        side_effect=AssertionError("charger config lookup should stay cached")
    )

    charge_modes, green_settings, auth_settings, charger_config = (
        await coord._async_resolve_post_status_evse_enrichments(  # noqa: SLF001
            {},
            records=[("EV1", {"sn": "EV1"})],
            charge_mode_candidates=[],
            first_refresh=False,
        )
    )

    assert charge_modes == {}
    assert green_settings == {"EV1": (True, True)}
    assert auth_settings == {"EV1": (True, False, True, True)}
    assert charger_config == {
        "EV1": {
            DEFAULT_CHARGE_LEVEL_SETTING: 32,
            PHASE_SWITCH_CONFIG_SETTING: "three_phase",
        }
    }
    coord.evse_runtime.async_get_green_battery_setting.assert_not_awaited()
    coord.evse_runtime.async_get_auth_settings.assert_not_awaited()
    coord.client.charger_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_get_charger_config_returns_none_during_backoff_without_cache(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=["EV1"])
    coord._charger_config_backoff_until["EV1"] = time.monotonic() + 60  # noqa: SLF001
    coord.client.charger_config = AsyncMock(
        side_effect=AssertionError("charger config lookup should be backoff-gated")
    )

    result = await coord.evse_runtime.async_get_charger_config(
        "EV1",
        keys=[DEFAULT_CHARGE_LEVEL_SETTING],
    )

    assert result is None
    coord.client.charger_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_hems_refresh_skips_preflight_when_device_cache_is_hot(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    runtime._hems_devices_cache_until = time.monotonic() + 60  # noqa: SLF001
    coord.heatpump_runtime.async_refresh_hems_support_preflight = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        side_effect=AssertionError("preflight should be skipped")
    )
    coord.client.hems_devices = AsyncMock(
        side_effect=AssertionError("device refresh should be skipped")
    )

    await runtime._async_refresh_hems_devices()

    coord.heatpump_runtime.async_refresh_hems_support_preflight.assert_not_awaited()
    coord.client.hems_devices.assert_not_awaited()


@pytest.mark.asyncio
async def test_hems_refresh_reuses_preflight_cache_across_due_device_refreshes(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    coord.client._hems_site_supported = None  # noqa: SLF001
    coord.client.system_dashboard_summary = AsyncMock(return_value={"is_hems": True})
    coord.client.hems_devices = AsyncMock(
        return_value={"data": {"hems-devices": {"heat-pump": []}}}
    )

    await runtime._async_refresh_hems_devices()

    assert runtime._hems_support_preflight_cache_until is not None  # noqa: SLF001
    coord.client._hems_site_supported = None  # noqa: SLF001
    runtime._hems_devices_cache_until = None  # noqa: SLF001

    await runtime._async_refresh_hems_devices()

    coord.client.system_dashboard_summary.assert_awaited_once()
    assert coord.client.hems_devices.await_count == 2


@pytest.mark.parametrize(
    ("first_refresh", "cache_age", "expected_inline", "expected_background"),
    [
        (True, 180.0, False, True),
        (False, 180.0, False, True),
        (False, 10_000.0, True, False),
    ],
)
@pytest.mark.asyncio
async def test_session_history_refresh_classifies_inline_vs_background_paths(
    coordinator_factory,
    monkeypatch,
    first_refresh: bool,
    cache_age: float,
    expected_inline: bool,
    expected_background: bool,
) -> None:
    coord = coordinator_factory()
    _prepare_refresh_target(
        coord,
        monkeypatch,
        charging=True,
        first_refresh=first_refresh,
        session_view=SessionCacheView(
            sessions=[{"session_id": "cached", "energy_kwh": 1.25}],
            cache_age=cache_age,
            needs_refresh=True,
            blocked=False,
            state="valid",
            has_valid_cache=True,
            last_error=None,
        ),
    )

    data = await coord._async_update_data()  # noqa: SLF001

    if expected_inline:
        coord._async_enrich_sessions.assert_awaited_once()  # type: ignore[attr-defined]  # noqa: SLF001
        coord._schedule_session_enrichment.assert_not_called()  # type: ignore[attr-defined]  # noqa: SLF001
        assert data[RANDOM_SERIAL]["energy_today_sessions"] == [
            {"session_id": "fresh", "energy_kwh": 2.0}
        ]
        assert (
            coord._async_enrich_sessions.await_args.kwargs["max_cache_age"]  # type: ignore[attr-defined]  # noqa: SLF001
            == 120.0
        )
        return

    if expected_background:
        coord._async_enrich_sessions.assert_not_awaited()  # type: ignore[attr-defined]  # noqa: SLF001
        coord._schedule_session_enrichment.assert_called_once()  # type: ignore[attr-defined]  # noqa: SLF001
        assert data[RANDOM_SERIAL]["energy_today_sessions"] == [
            {"session_id": "cached", "energy_kwh": 1.25}
        ]
        assert (
            coord._schedule_session_enrichment.call_args.kwargs["max_cache_age"]  # type: ignore[attr-defined]  # noqa: SLF001
            == 120.0
        )


@pytest.mark.asyncio
async def test_immediate_session_history_buckets_refresh_concurrently(
    coordinator_factory,
    monkeypatch,
) -> None:
    coord = coordinator_factory(serials=[RANDOM_SERIAL, "EV2"])
    await coord.hass.config.async_set_time_zone("UTC")
    now_local = datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)
    previous_day_epoch = int(
        datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc).timestamp()
    )
    monkeypatch.setattr(dt_util, "now", lambda: now_local)
    coord._has_successful_refresh = True  # noqa: SLF001
    coord._scheduler_available = True  # noqa: SLF001
    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": RANDOM_SERIAL,
                    "name": "Garage EV",
                    "charging": True,
                    "pluggedIn": True,
                    "charge_mode": "IMMEDIATE",
                    "connectors": [{}],
                    "session_d": {},
                    "sch_d": {},
                },
                {
                    "sn": "EV2",
                    "name": "Driveway EV",
                    "charging": False,
                    "pluggedIn": False,
                    "charge_mode": "IMMEDIATE",
                    "connectors": [{}],
                    "session_d": {"plg_out_at": previous_day_epoch},
                    "sch_d": {},
                },
            ],
            "ts": int(now_local.timestamp()),
        }
    )
    coord.summary = SimpleNamespace(
        prepare_refresh=lambda **_kwargs: False,
        async_fetch=AsyncMock(
            return_value=[
                {"serialNumber": RANDOM_SERIAL, "displayName": "Garage EV"},
                {"serialNumber": "EV2", "displayName": "Driveway EV"},
            ]
        ),
        invalidate=lambda: None,
    )
    coord.evse_timeseries = SimpleNamespace(
        refresh_due=MagicMock(return_value=False),
        async_refresh=AsyncMock(),
        merge_charger_payloads=MagicMock(),
        diagnostics=lambda: {},
    )
    coord._async_run_post_status_refresh_pipeline = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        return_value=None
    )
    coord._async_run_post_session_refresh_pipeline = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        return_value=None
    )
    coord._async_resolve_green_battery_settings = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        return_value={}
    )
    coord._async_resolve_auth_settings = AsyncMock(return_value={})  # noqa: SLF001
    coord._async_resolve_charger_config = AsyncMock(return_value={})  # noqa: SLF001
    coord._schedule_session_enrichment = MagicMock()  # type: ignore[assignment]  # noqa: SLF001
    coord._prune_runtime_caches = MagicMock()  # type: ignore[assignment]  # noqa: SLF001

    def _cache_view(sn: str, _day_key: str, _now_mono: float) -> SessionCacheView:
        return SessionCacheView(
            sessions=[{"session_id": f"cached-{sn}", "energy_kwh": 1.0}],
            cache_age=1300.0 if sn == "EV2" else 950.0,
            needs_refresh=True,
            blocked=False,
            state="valid",
            has_valid_cache=True,
            last_error=None,
        )

    coord.session_history.get_cache_view = MagicMock(side_effect=_cache_view)  # type: ignore[assignment]
    started: list[str] = []
    both_started = asyncio.Event()

    async def _enrich(serials, _day_local, **_kwargs):  # noqa: ANN001
        serial = serials[0]
        started.append(serial)
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)
        return {serial: [{"session_id": f"fresh-{serial}", "energy_kwh": 2.0}]}

    coord._async_enrich_sessions = AsyncMock(side_effect=_enrich)  # type: ignore[assignment]  # noqa: SLF001

    data = await coord._async_update_data()  # noqa: SLF001

    assert set(started) == {RANDOM_SERIAL, "EV2"}
    assert data[RANDOM_SERIAL]["energy_today_sessions"] == [
        {"session_id": f"fresh-{RANDOM_SERIAL}", "energy_kwh": 2.0}
    ]
    assert data["EV2"]["energy_today_sessions"] == [
        {"session_id": "fresh-EV2", "energy_kwh": 2.0}
    ]
    coord._schedule_session_enrichment.assert_not_called()  # type: ignore[attr-defined]  # noqa: SLF001
