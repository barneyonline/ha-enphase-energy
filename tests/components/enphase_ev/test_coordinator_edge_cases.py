from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from pytest_homeassistant_custom_component.common import MockConfigEntry
from homeassistant.exceptions import ServiceValidationError

from custom_components.enphase_ev.const import (
    CONF_AUTH_BLOCK_REASON,
    CONF_AUTH_BLOCKED_UNTIL,
    CONF_AUTH_REFRESH_SUSPENDED_UNTIL,
    CONF_COOKIE,
    CONF_EAUTH,
    CONF_SCAN_INTERVAL,
    CONF_SERIALS,
    CONF_SITE_ID,
    CONF_SITE_ONLY,
    CONF_SESSION_ID,
    AUTH_REFRESH_REJECTED_SUSPEND_THRESHOLD,
    DEFAULT_SLOW_POLL_INTERVAL,
    DOMAIN,
    OPT_API_TIMEOUT,
    OPT_NOMINAL_VOLTAGE,
    OPT_SLOW_POLL_INTERVAL,
)
from custom_components.enphase_ev.evse_runtime import EvseRuntime

from homeassistant.helpers.update_coordinator import UpdateFailed


class _BadStr:
    def __str__(self) -> str:
        raise RuntimeError("boom")


def _attach_evse_runtime(coord):
    coord.evse_runtime = EvseRuntime(coord)
    return coord


def _client_response_error(
    status: int, *, message: str = "", headers: dict[str, str] | None = None
) -> aiohttp.ClientResponseError:
    req = aiohttp.RequestInfo(
        url=aiohttp.client.URL("https://example.invalid"),
        method="GET",
        headers={},
        real_url=aiohttp.client.URL("https://example.invalid"),
    )
    return aiohttp.ClientResponseError(
        request_info=req,
        history=(),
        status=status,
        message=message,
        headers=headers or {},
    )


def _make_entry(
    hass, data_override: dict | None = None, *, options: dict | None = None
):
    data = {
        CONF_SITE_ID: "111111",
        CONF_SERIALS: ["EV123"],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }
    if data_override:
        data.update(data_override)

    entry = MockConfigEntry(domain=DOMAIN, data=data, options=options or {})
    entry.add_to_hass(hass)
    return entry


def _make_battery_ready_coordinator(hass, monkeypatch):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    entry = _make_entry(hass)

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        coord_mod,
        "async_call_later",
        lambda *_args, **_kwargs: (lambda: None),
    )

    coord = EnphaseCoordinator(hass, entry.data, config_entry=entry)
    now = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
    coord.last_update_success = True
    coord.last_success_utc = now
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._selected_type_keys = {"encharge"}  # noqa: SLF001
    coord._type_device_buckets = {  # noqa: SLF001
        "encharge": {"type_key": "encharge", "count": 2, "devices": [{}, {}]}
    }
    coord._type_device_order = ["encharge"]  # noqa: SLF001
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_cfg_control_show = True  # noqa: SLF001
    coord._battery_cfg_control_schedule_supported = True  # noqa: SLF001
    coord._battery_cfg_control_force_schedule_supported = True  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_charge_from_grid_schedule_enabled = True  # noqa: SLF001
    coord._battery_cfg_schedule_id = "cfg-1"  # noqa: SLF001
    coord._battery_cfg_schedule_limit = 90  # noqa: SLF001
    coord._battery_charge_begin_time = 60  # noqa: SLF001
    coord._battery_charge_end_time = 120  # noqa: SLF001
    coord._battery_dtg_schedule_id = "dtg-1"  # noqa: SLF001
    coord._battery_dtg_begin_time = 180  # noqa: SLF001
    coord._battery_dtg_end_time = 240  # noqa: SLF001
    coord._battery_dtg_schedule_limit = 80  # noqa: SLF001
    coord._battery_rbd_schedule_id = "rbd-1"  # noqa: SLF001
    coord._battery_rbd_begin_time = 300  # noqa: SLF001
    coord._battery_rbd_end_time = 360  # noqa: SLF001
    coord._battery_rbd_schedule_limit = 70  # noqa: SLF001
    coord._battery_very_low_soc = 10  # noqa: SLF001
    coord._battery_envoy_supports_vls = True  # noqa: SLF001
    coord._battery_aggregate_charge_pct = 25.0  # noqa: SLF001
    coord._battery_aggregate_status = "normal"  # noqa: SLF001
    coord._battery_aggregate_status_details = {  # noqa: SLF001
        "site_available_energy_kwh": 2.5,
        "site_available_power_kw": 7.68,
    }
    return coord


@pytest.mark.asyncio
async def test_coordinator_init_handles_bad_scalar_serial_and_legacy_super(
    hass, monkeypatch
):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    entry = _make_entry(
        hass,
        data_override={
            CONF_SERIALS: _BadStr(),
            OPT_API_TIMEOUT: 10,
        },
    )

    init_calls: list[dict] = []

    async def _fake_reauth_cb(*_):
        return None

    def fake_coord_init(self, hass_arg, logger, **kwargs):
        init_calls.append(dict(kwargs))
        # Mimic Coordinator init side effects used later
        self.hass = hass_arg
        self.logger = logger
        self.update_interval = kwargs.get("update_interval")

    monkeypatch.setattr(
        coord_mod.DataUpdateCoordinator, "__init__", fake_coord_init, raising=False
    )
    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        coord_mod,
        "EnphaseEVClient",
        lambda *args, **kwargs: SimpleNamespace(
            set_reauth_callback=lambda *_: _fake_reauth_cb()
        ),
    )

    coord = EnphaseCoordinator(hass, entry.data, config_entry=entry)

    assert coord.serials == set()
    assert coord._serial_order == []
    assert len(init_calls) == 1
    assert "config_entry" in init_calls[0]


def test_collect_site_metrics_handles_unfriendly_datetime(hass):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord.site_id = "SITE"
    coord.site_name = "Garage"
    coord._backoff_until = 10.0
    coord.backoff_ends_utc = None
    coord._network_errors = 1
    coord._http_errors = 2
    coord._rate_limit_hits = 3
    coord._dns_failures = 4
    coord._summary_ttl = 60.0
    coord._phase_timings = {"status_s": 0.5}
    coord._last_error = "timeout"
    coord.last_failure_source = "http"
    coord.last_failure_response = "body"
    coord.last_failure_status = 503
    coord.last_failure_description = "Service Unavailable"
    coord.latency_ms = 120
    coord.last_success_utc = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
    coord.last_failure_utc = datetime(2025, 1, 1, 11, 0, tzinfo=timezone.utc)
    coord._auth_refresh_rejected_count = 2
    coord._auth_refresh_suspended_until_utc = datetime(
        2025, 1, 1, 12, 30, tzinfo=timezone.utc
    )
    coord._auth_blocked_until_utc = datetime(2025, 1, 1, 13, 0, tzinfo=timezone.utc)
    coord._auth_block_reason = "login_wall_after_refresh_reject"

    class BadIso:
        def isoformat(self):
            raise ValueError("nope")

        def __str__(self):
            return "bad-date"

    coord.backoff_ends_utc = BadIso()

    metrics = coord.collect_site_metrics()

    assert metrics["site_id"] == "SITE"
    assert metrics["auth_refresh_suspended_active"] is False
    assert metrics["auth_refresh_suspended_until"] is None
    assert metrics["auth_refresh_rejected_count"] == 0
    assert metrics["auth_blocked_active"] is False
    assert metrics["auth_blocked_until"] is None
    assert metrics["auth_block_reason"] is None
    assert metrics["site_name"] == "Garage"
    assert metrics["backoff_active"] is False
    assert metrics["backoff_ends_utc"] == "bad-date"
    assert metrics["last_failure_status"] == 503
    assert metrics["network_errors"] == 1
    assert metrics["session_cache_ttl_s"] is None
    assert "battery_controls_available" in metrics
    assert "battery_profile_selection_available" in metrics
    assert "battery_reserve_editable" in metrics
    assert "battery_shutdown_level_available" in metrics
    assert "charge_from_grid_control_available" in metrics
    assert "charge_from_grid_schedule_supported" in metrics
    assert "charge_from_grid_schedule_available" in metrics
    assert "charge_from_grid_force_schedule_supported" in metrics
    assert "charge_from_grid_force_schedule_available" in metrics
    assert "discharge_to_grid_schedule_supported" in metrics
    assert "discharge_to_grid_schedule_available" in metrics
    assert "restrict_battery_discharge_schedule_supported" in metrics
    assert "restrict_battery_discharge_schedule_available" in metrics


def test_collect_site_metrics_reports_battery_entity_availability_flags(
    hass, monkeypatch
):
    coord = _make_battery_ready_coordinator(hass, monkeypatch)

    metrics = coord.collect_site_metrics()

    assert metrics["battery_type_available_for_entities"] is True
    assert metrics["battery_write_access_confirmed"] is True
    assert metrics["battery_controls_available"] is True
    assert metrics["battery_profile_selection_available"] is True
    assert metrics["battery_reserve_editable"] is True
    assert metrics["battery_shutdown_level_available"] is True
    assert metrics["charge_from_grid_control_available"] is True
    assert metrics["charge_from_grid_schedule_supported"] is True
    assert metrics["charge_from_grid_schedule_available"] is True
    assert metrics["charge_from_grid_force_schedule_supported"] is True
    assert metrics["charge_from_grid_force_schedule_available"] is True
    assert metrics["discharge_to_grid_schedule_supported"] is True
    assert metrics["discharge_to_grid_schedule_available"] is True
    assert metrics["restrict_battery_discharge_schedule_supported"] is True
    assert metrics["restrict_battery_discharge_schedule_available"] is True
    assert metrics["battery_overall_charge_sensor_available"] is True
    assert metrics["battery_overall_status_sensor_available"] is True
    assert metrics["battery_cfg_schedule_status_sensor_available"] is True
    assert metrics["battery_available_energy_sensor_available"] is True
    assert metrics["battery_available_power_sensor_available"] is True
    assert metrics["battery_reserve_number_available"] is True
    assert metrics["battery_shutdown_level_number_available"] is True
    assert metrics["battery_cfg_schedule_limit_number_available"] is True
    assert metrics["battery_dtg_schedule_limit_number_available"] is True
    assert metrics["battery_rbd_schedule_limit_number_available"] is True
    assert metrics["charge_from_grid_switch_available"] is True
    assert metrics["charge_from_grid_schedule_switch_available"] is True
    assert metrics["discharge_to_grid_schedule_switch_available"] is True
    assert metrics["restrict_battery_discharge_schedule_switch_available"] is True


def test_auth_block_datetime_helpers_cover_fallback_branches(monkeypatch):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    naive = datetime(2025, 1, 1, 12, 0)
    assert EnphaseCoordinator._coerce_utc_datetime(naive) == naive.replace(
        tzinfo=timezone.utc
    )

    aware = datetime(2025, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=11)))
    assert EnphaseCoordinator._coerce_utc_datetime(aware) == aware.astimezone(
        timezone.utc
    )

    class _BadValue:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert EnphaseCoordinator._coerce_utc_datetime(_BadValue()) is None
    assert EnphaseCoordinator._coerce_utc_datetime("  ") is None

    monkeypatch.setattr(
        coord_mod.dt_util,
        "parse_datetime",
        lambda _value: datetime(2025, 1, 2, 3, 4, 5),
    )
    assert EnphaseCoordinator._coerce_utc_datetime("2025-01-02T03:04:05") == datetime(
        2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc
    )

    monkeypatch.setattr(coord_mod.dt_util, "parse_datetime", lambda _value: None)
    assert EnphaseCoordinator._coerce_utc_datetime("not-a-date") is None

    assert EnphaseCoordinator._format_auth_blocked_until("not-a-datetime") is None

    class _IsoFallbackDateTime(datetime):
        def astimezone(self, tz=None):
            raise ValueError("bad-astimezone")

        def isoformat(self, sep="T", timespec="auto"):
            return "2025-01-03T00:00:00+00:00"

    assert (
        EnphaseCoordinator._format_auth_blocked_until(
            _IsoFallbackDateTime(2025, 1, 3, 0, 0, tzinfo=timezone.utc)
        )
        == "2025-01-03T00:00:00+00:00"
    )

    class _StrFallbackDateTime(datetime):
        def astimezone(self, tz=None):
            raise ValueError("bad-astimezone")

        def isoformat(self, sep="T", timespec="auto"):
            raise ValueError("bad-isoformat")

        def __str__(self) -> str:
            return "bad-date"

    assert (
        EnphaseCoordinator._format_auth_blocked_until(
            _StrFallbackDateTime(2025, 1, 4, 0, 0, tzinfo=timezone.utc)
        )
        == "bad-date"
    )


def test_persist_auth_block_state_removes_cleared_fields(hass, monkeypatch):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    entry = _make_entry(
        hass,
        data_override={
            CONF_AUTH_BLOCKED_UNTIL: "2025-01-01T12:00:00+00:00",
            CONF_AUTH_BLOCK_REASON: "login_wall_after_refresh_reject",
        },
    )
    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord.config_entry = entry
    coord._auth_blocked_until_utc = None
    coord._auth_block_reason = None

    captured: list[dict] = []

    def _update_entry(entry_obj, data=None, **kwargs):
        assert entry_obj is entry
        captured.append(dict(data))

    monkeypatch.setattr(hass.config_entries, "async_update_entry", _update_entry)

    coord._persist_auth_block_state()

    assert captured
    assert CONF_AUTH_BLOCKED_UNTIL not in captured[-1]
    assert CONF_AUTH_BLOCK_REASON not in captured[-1]


def test_persist_auth_refresh_suspension_state_removes_cleared_field(hass, monkeypatch):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    entry = _make_entry(
        hass,
        data_override={
            CONF_AUTH_REFRESH_SUSPENDED_UNTIL: "2025-01-01T12:00:00+00:00",
        },
    )
    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord.config_entry = entry
    coord._auth_refresh_suspended_until_utc = None

    captured: list[dict] = []

    def _update_entry(entry_obj, data=None, **kwargs):
        assert entry_obj is entry
        captured.append(dict(data))

    monkeypatch.setattr(hass.config_entries, "async_update_entry", _update_entry)

    coord._persist_auth_refresh_suspension_state()

    assert captured
    assert CONF_AUTH_REFRESH_SUSPENDED_UNTIL not in captured[-1]


def test_persist_auth_refresh_suspension_state_stores_field(hass, monkeypatch):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    entry = _make_entry(hass)
    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord.config_entry = entry
    coord._auth_refresh_suspended_until_utc = datetime(
        2026, 5, 1, 12, 0, tzinfo=timezone.utc
    )

    captured: list[dict] = []

    def _update_entry(entry_obj, data=None, **kwargs):
        assert entry_obj is entry
        captured.append(dict(data))

    monkeypatch.setattr(hass.config_entries, "async_update_entry", _update_entry)

    coord._persist_auth_refresh_suspension_state()

    assert captured
    assert (
        captured[-1][CONF_AUTH_REFRESH_SUSPENDED_UNTIL] == "2026-05-01T12:00:00+00:00"
    )


def test_persist_auth_refresh_suspension_state_marks_internal_update(hass, monkeypatch):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    entry = _make_entry(hass)
    object.__setattr__(
        entry, "runtime_data", SimpleNamespace(reload_suppression_count=0)
    )
    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord.config_entry = entry
    coord._auth_refresh_suspended_until_utc = datetime(
        2026, 5, 1, 12, 0, tzinfo=timezone.utc
    )

    def _update_entry(entry_obj, data=None, **kwargs):
        assert entry_obj is entry
        return True

    monkeypatch.setattr(hass.config_entries, "async_update_entry", _update_entry)

    coord._persist_auth_refresh_suspension_state()

    assert entry.runtime_data.reload_suppression_count == 1


def test_clear_auth_refresh_rejection_state_resets_counter_and_cooldown(hass):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._auth_refresh_rejected_count = 2
    coord._auth_refresh_rejected_until = time.monotonic() + 60
    coord._auth_refresh_rejected_ends_utc = datetime.now(timezone.utc) + timedelta(
        seconds=60
    )

    coord._clear_auth_refresh_rejection_state()

    assert coord._auth_refresh_rejected_count == 0
    assert coord._auth_refresh_rejected_until is None
    assert coord._auth_refresh_rejected_ends_utc is None


@pytest.mark.asyncio
async def test_post_status_first_refresh_clears_auth_refresh_rejection(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import RefreshPipelineContext

    coord = coordinator_factory()

    async def _run_first_refresh_calls(phase_timings, *, calls, **_kwargs) -> None:
        durations = {
            "battery_site_settings_s": 0.234,
            "storm_guard_s": 0.345,
        }
        for timing_key, *_rest in calls:
            phase_timings[timing_key] = durations[timing_key]

    coord.refresh_runner.async_run_refresh_calls = AsyncMock(
        side_effect=_run_first_refresh_calls
    )
    coord._auth_refresh_rejected_count = 2
    coord._auth_refresh_rejected_until = time.monotonic() + 60
    coord._auth_refresh_rejected_ends_utc = datetime.now(timezone.utc) + timedelta(
        seconds=60
    )
    context = RefreshPipelineContext(
        started_mono=time.monotonic(),
        refresh_started_utc=datetime.now(timezone.utc),
        phase_timings={},
        fallback_data={},
        first_refresh=True,
        auth_refresh_rejected_count_at_start=2,
    )

    await coord._async_run_post_status_refresh_pipeline(context)

    coord.refresh_runner.async_run_refresh_calls.assert_awaited_once()
    calls = coord.refresh_runner.async_run_refresh_calls.await_args.kwargs["calls"]
    assert [call[0] for call in calls] == [
        "battery_site_settings_s",
        "storm_guard_s",
    ]
    assert context.phase_timings["battery_site_settings_s"] == 0.234
    assert context.phase_timings["storm_guard_s"] == 0.345
    assert coord._auth_refresh_rejected_count == 0
    assert coord._auth_refresh_rejected_until is None
    assert coord._auth_refresh_rejected_ends_utc is None


@pytest.mark.asyncio
async def test_post_status_preserves_new_auth_refresh_rejection(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import RefreshPipelineContext

    coord = coordinator_factory()

    async def _reject_during_followup(phase_timings, *, calls, **_kwargs) -> None:
        coord._auth_refresh_rejected_count = 1
        coord._auth_refresh_rejected_until = time.monotonic() + 60
        coord._auth_refresh_rejected_ends_utc = datetime.now(timezone.utc) + timedelta(
            seconds=60
        )
        for timing_key, *_rest in calls:
            phase_timings[timing_key] = 0.123

    coord.refresh_runner.async_run_refresh_calls = AsyncMock(
        side_effect=_reject_during_followup
    )
    coord._auth_refresh_rejected_count = 0
    coord._auth_refresh_rejected_until = None
    coord._auth_refresh_rejected_ends_utc = None
    context = RefreshPipelineContext(
        started_mono=time.monotonic(),
        refresh_started_utc=datetime.now(timezone.utc),
        phase_timings={},
        fallback_data={},
        first_refresh=True,
        auth_refresh_rejected_count_at_start=0,
    )

    await coord._async_run_post_status_refresh_pipeline(context)

    coord.refresh_runner.async_run_refresh_calls.assert_awaited_once()
    assert coord._auth_refresh_rejected_count == 1
    assert coord._auth_refresh_rejected_until is not None
    assert coord._auth_refresh_rejected_ends_utc is not None


@pytest.mark.parametrize(
    ("selected", "site_only", "serials", "has_encharge", "expected"),
    [
        ({"iqevse"}, False, {"EV123"}, None, True),
        ({"microinverter"}, False, {"EV123"}, None, False),
        ({"envoy"}, True, set(), None, True),
        ({"encharge"}, True, set(), None, False),
        (None, False, {"EV123"}, False, False),
    ],
)
def test_first_refresh_storm_guard_followups_are_gated(
    coordinator_factory,
    selected,
    site_only,
    serials,
    has_encharge,
    expected,
) -> None:
    coord = coordinator_factory()
    coord._selected_type_keys = selected  # noqa: SLF001
    coord.site_only = site_only
    coord.serials = serials
    coord._battery_has_encharge = has_encharge  # noqa: SLF001

    assert (
        coord._first_refresh_storm_guard_followups_needed() is expected
    )  # noqa: SLF001


def test_clear_auth_refresh_rejection_state_if_unchanged_preserves_suspension(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import RefreshPipelineContext

    coord = coordinator_factory()
    coord._auth_refresh_rejected_count = 3
    coord._auth_refresh_rejected_until = None
    coord._auth_refresh_rejected_ends_utc = None
    coord._auth_refresh_suspended_until_utc = datetime.now(timezone.utc) + timedelta(
        seconds=60
    )
    context = RefreshPipelineContext(
        started_mono=time.monotonic(),
        refresh_started_utc=datetime.now(timezone.utc),
        phase_timings={},
        fallback_data={},
        first_refresh=False,
        auth_refresh_rejected_count_at_start=3,
    )

    coord._clear_auth_refresh_rejection_state_if_unchanged(context)

    assert coord._auth_refresh_rejected_count == 3
    assert coord._auth_refresh_suspended_until_utc is not None


def test_coordinator_init_restores_auth_refresh_state(hass, monkeypatch):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    entry = _make_entry(
        hass,
        data_override={
            CONF_AUTH_REFRESH_SUSPENDED_UNTIL: "2026-05-01T12:00:00+00:00",
            CONF_AUTH_BLOCKED_UNTIL: "2026-05-01T13:00:00+00:00",
            CONF_AUTH_BLOCK_REASON: "login_wall_after_refresh_reject",
        },
    )

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        coord_mod,
        "async_call_later",
        lambda *_args, **_kwargs: (lambda: None),
    )

    coord = EnphaseCoordinator(hass, entry.data, config_entry=entry)

    assert coord._auth_refresh_suspended_until_utc == datetime(
        2026, 5, 1, 12, 0, tzinfo=timezone.utc
    )
    assert coord._auth_blocked_until_utc == datetime(
        2026, 5, 1, 13, 0, tzinfo=timezone.utc
    )
    assert coord._auth_block_reason == "login_wall_after_refresh_reject"


def test_collect_site_metrics_matches_battery_energy_power_sensor_parse_rules(
    hass, monkeypatch
):
    coord = _make_battery_ready_coordinator(hass, monkeypatch)
    coord._battery_aggregate_status_details = {  # noqa: SLF001
        "site_available_energy_kwh": "N/A",
        "site_available_power_kw": "bad",
    }

    metrics = coord.collect_site_metrics()

    assert metrics["battery_available_energy_sensor_available"] is False
    assert metrics["battery_available_power_sensor_available"] is False


def test_collect_site_metrics_handles_missing_and_raising_type_checker(
    hass, monkeypatch
):
    coord = _make_battery_ready_coordinator(hass, monkeypatch)

    monkeypatch.setattr(coord.inventory_view, "has_type_for_entities", None)
    metrics = coord.collect_site_metrics()
    assert metrics["battery_type_available_for_entities"] is False
    assert metrics["battery_overall_charge_sensor_available"] is False

    def _raise(_type_key):
        raise RuntimeError("boom")

    monkeypatch.setattr(coord.inventory_view, "has_type_for_entities", _raise)
    metrics = coord.collect_site_metrics()
    assert metrics["battery_type_available_for_entities"] is False
    assert metrics["charge_from_grid_switch_available"] is False


def test_collect_site_metrics_write_access_fallback_and_cfg_schedule_fallbacks(
    hass, monkeypatch
):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _make_battery_ready_coordinator(hass, monkeypatch)

    monkeypatch.setattr(
        EnphaseCoordinator,
        "battery_write_access_confirmed",
        property(lambda _self: (_ for _ in ()).throw(RuntimeError("boom"))),
    )
    metrics = coord.collect_site_metrics()
    assert metrics["battery_write_access_confirmed"] is True

    with monkeypatch.context() as m:
        m.setattr(
            EnphaseCoordinator,
            "charge_from_grid_schedule_available",
            property(lambda _self: False),
        )
        m.setattr(
            EnphaseCoordinator,
            "charge_from_grid_schedule_supported",
            property(lambda _self: False),
        )
        metrics = coord.collect_site_metrics()
        assert metrics["battery_cfg_schedule_limit_number_available"] is False

    with monkeypatch.context() as m:
        m.setattr(
            EnphaseCoordinator,
            "charge_from_grid_schedule_available",
            property(lambda _self: False),
        )
        m.setattr(
            EnphaseCoordinator,
            "charge_from_grid_schedule_supported",
            property(lambda _self: True),
        )
        metrics = coord.collect_site_metrics()
        assert metrics["battery_cfg_schedule_limit_number_available"] is True

    with monkeypatch.context() as m:
        m.setattr(
            EnphaseCoordinator,
            "charge_from_grid_schedule_available",
            property(lambda _self: False),
        )
        m.setattr(
            EnphaseCoordinator,
            "charge_from_grid_schedule_supported",
            property(lambda _self: True),
        )
        m.setattr(
            EnphaseCoordinator,
            "battery_charge_from_grid_start_time",
            property(lambda _self: None),
        )
        m.setattr(
            EnphaseCoordinator,
            "battery_charge_from_grid_end_time",
            property(lambda _self: None),
        )
        metrics = coord.collect_site_metrics()
        assert metrics["battery_cfg_schedule_limit_number_available"] is True

    coord._battery_cfg_schedule_id = None  # noqa: SLF001
    with monkeypatch.context() as m:
        m.setattr(
            EnphaseCoordinator,
            "charge_from_grid_schedule_available",
            property(lambda _self: False),
        )
        m.setattr(
            EnphaseCoordinator,
            "charge_from_grid_schedule_supported",
            property(lambda _self: True),
        )
        metrics = coord.collect_site_metrics()
        assert metrics["battery_cfg_schedule_limit_number_available"] is False


def test_collect_site_metrics_dtg_and_rbd_schedule_fallbacks(hass, monkeypatch):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _make_battery_ready_coordinator(hass, monkeypatch)

    with monkeypatch.context() as m:
        m.setattr(
            EnphaseCoordinator,
            "discharge_to_grid_schedule_available",
            property(lambda _self: False),
        )
        metrics = coord.collect_site_metrics()
        assert metrics["battery_dtg_schedule_limit_number_available"] is True

    with monkeypatch.context() as m:
        m.setattr(
            EnphaseCoordinator,
            "restrict_battery_discharge_schedule_available",
            property(lambda _self: False),
        )
        metrics = coord.collect_site_metrics()
        assert metrics["battery_rbd_schedule_limit_number_available"] is True

    coord._battery_dtg_begin_time = None  # noqa: SLF001
    coord._battery_dtg_end_time = None  # noqa: SLF001
    coord._battery_dtg_control_begin_time = 180  # noqa: SLF001
    coord._battery_dtg_control_end_time = 240  # noqa: SLF001
    with monkeypatch.context() as m:
        m.setattr(
            EnphaseCoordinator,
            "discharge_to_grid_schedule_available",
            property(lambda _self: False),
        )
        m.setattr(
            EnphaseCoordinator,
            "discharge_to_grid_schedule_supported",
            property(lambda _self: True),
        )
        m.setattr(
            EnphaseCoordinator,
            "battery_discharge_to_grid_start_time",
            property(lambda _self: None),
        )
        m.setattr(
            EnphaseCoordinator,
            "battery_discharge_to_grid_end_time",
            property(lambda _self: None),
        )
        metrics = coord.collect_site_metrics()
        assert metrics["battery_dtg_schedule_limit_number_available"] is True

    coord._battery_rbd_begin_time = None  # noqa: SLF001
    coord._battery_rbd_end_time = None  # noqa: SLF001
    coord._battery_rbd_control_begin_time = 300  # noqa: SLF001
    coord._battery_rbd_control_end_time = 360  # noqa: SLF001
    with monkeypatch.context() as m:
        m.setattr(
            EnphaseCoordinator,
            "restrict_battery_discharge_schedule_available",
            property(lambda _self: False),
        )
        m.setattr(
            EnphaseCoordinator,
            "restrict_battery_discharge_schedule_supported",
            property(lambda _self: True),
        )
        m.setattr(
            EnphaseCoordinator,
            "battery_restrict_battery_discharge_start_time",
            property(lambda _self: None),
        )
        m.setattr(
            EnphaseCoordinator,
            "battery_restrict_battery_discharge_end_time",
            property(lambda _self: None),
        )
        metrics = coord.collect_site_metrics()
        assert metrics["battery_rbd_schedule_limit_number_available"] is True


@pytest.mark.asyncio
async def test_http_error_retry_after_date_triggers_rate_limit_issue(hass, monkeypatch):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev import coordinator_diagnostics as diag_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    entry = _make_entry(hass)

    time_keeper = SimpleNamespace(value=1_000.0)

    def monotonic():
        return time_keeper.value

    def fake_call_later(hass_obj, delay, callback):
        scheduled["delay"] = delay
        scheduled["callback"] = callback

        def _cancel():
            scheduled["cancelled"] = True

        return _cancel

    scheduled: dict[str, object] = {}
    issue_calls: list[tuple] = []

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(coord_mod.time, "monotonic", monotonic)
    monkeypatch.setattr(coord_mod.random, "uniform", lambda *args, **kwargs: 2.0)
    monkeypatch.setattr(coord_mod, "async_call_later", fake_call_later)
    monkeypatch.setattr(
        coord_mod.dt_util,
        "utcnow",
        lambda: datetime(2025, 5, 1, 12, 0, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        diag_mod.ir,
        "async_create_issue",
        lambda *args, **kwargs: issue_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(diag_mod.ir, "async_delete_issue", lambda *args, **kwargs: None)

    class StubClient:
        async def status(self):
            raise _client_response_error(
                429,
                message="Too Many Requests",
                headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"},
            )

        async def summary_v2(self):
            return []

    coord = EnphaseCoordinator(hass, entry.data, config_entry=entry)
    coord.client = StubClient()
    coord._rate_limit_hits = 1  # ensure branch triggers issue creation

    with pytest.raises(UpdateFailed) as exc_info:
        await coord._async_update_data()

    assert coord.last_failure_status == 429
    assert coord.last_failure_source == "http"
    assert issue_calls, "Expected rate limit issue creation"
    assert scheduled["delay"] > 0
    assert exc_info.value.retry_after == pytest.approx(scheduled["delay"])
    assert coord._backoff_until == pytest.approx(time_keeper.value + scheduled["delay"])


@pytest.mark.asyncio
async def test_http_server_errors_raise_cloud_issue_and_clear_on_success(
    hass, monkeypatch
):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev import coordinator_diagnostics as diag_mod
    from custom_components.enphase_ev.const import (
        ISSUE_CLOUD_ERRORS,
        ISSUE_DNS_RESOLUTION,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    entry = _make_entry(hass)

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(coord_mod.time, "monotonic", lambda: 2_000.0)
    monkeypatch.setattr(coord_mod.random, "uniform", lambda *args, **kwargs: 1.5)

    created: list[tuple] = []
    deleted: list[tuple] = []

    monkeypatch.setattr(
        diag_mod.ir,
        "async_create_issue",
        lambda *args, **kwargs: created.append((args, kwargs)),
    )
    monkeypatch.setattr(
        diag_mod.ir,
        "async_delete_issue",
        lambda *args, **kwargs: deleted.append((args, kwargs)),
    )
    monkeypatch.setattr(
        coord_mod, "async_call_later", lambda *args, **kwargs: lambda: None
    )
    monkeypatch.setattr(coord_mod.dt_util, "utcnow", lambda: datetime.now(timezone.utc))

    class ErrorClient:
        async def status(self):
            raise _client_response_error(503, message="Service Unavailable")

        async def summary_v2(self):
            return []

    class SuccessClient:
        async def status(self):
            return {"evChargerData": [], "ts": None}

        async def summary_v2(self):
            return []

    coord = EnphaseCoordinator(hass, entry.data, config_entry=entry)
    coord.client = ErrorClient()
    coord._http_errors = 2  # first increment -> 3 to trigger issue

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert any(
        call[0][2] == ISSUE_CLOUD_ERRORS for call in created
    ), "Cloud issue should be created"

    coord.client = SuccessClient()
    coord._cloud_issue_reported = True
    coord._dns_issue_reported = True
    coord._backoff_until = None
    coord.backoff_ends_utc = None
    created.clear()

    await coord._async_update_data()

    assert coord._cloud_issue_reported is False
    assert any(
        call[0][2] == ISSUE_CLOUD_ERRORS for call in deleted
    ), "Cloud issue should be cleared"
    assert any(
        call[0][2] == ISSUE_DNS_RESOLUTION for call in deleted
    ), "DNS issue should be cleared on success"


@pytest.mark.asyncio
async def test_network_error_dns_issue_reporting(hass, monkeypatch):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev import coordinator_diagnostics as diag_mod
    from custom_components.enphase_ev.const import ISSUE_DNS_RESOLUTION
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    entry = _make_entry(hass)

    issue_calls: list[tuple] = []

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(coord_mod.time, "monotonic", lambda: 3_000.0)
    monkeypatch.setattr(coord_mod.random, "uniform", lambda *args, **kwargs: 1.25)
    monkeypatch.setattr(
        diag_mod.ir,
        "async_create_issue",
        lambda *args, **kwargs: issue_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(diag_mod.ir, "async_delete_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        coord_mod, "async_call_later", lambda *args, **kwargs: lambda: None
    )
    monkeypatch.setattr(coord_mod.dt_util, "utcnow", lambda: datetime.now(timezone.utc))

    class DnsClient:
        async def status(self):
            raise aiohttp.ClientConnectionError("Temporary failure in name resolution")

        async def summary_v2(self):
            return []

    coord = EnphaseCoordinator(hass, entry.data, config_entry=entry)
    coord.client = DnsClient()

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord._dns_failures == 1
    assert coord._dns_issue_reported is False

    coord._backoff_until = None
    coord.backoff_ends_utc = None

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord._dns_failures == 2
    assert coord._dns_issue_reported is True
    assert any(
        call[0][2] == ISSUE_DNS_RESOLUTION for call in issue_calls
    ), "DNS resolution issue should be raised"


@pytest.mark.asyncio
async def test_attempt_auto_refresh_success(monkeypatch, hass):
    from custom_components.enphase_ev import auth_refresh_runtime as arr_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.api import AuthTokens

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._email = "user@example.com"
    coord._remember_password = True
    coord._stored_password = "secret"
    coord._refresh_lock = asyncio.Lock()
    coord.client = SimpleNamespace(update_credentials=MagicMock())
    coord._persist_tokens = MagicMock()
    coord._tokens = AuthTokens(
        cookie="", session_id=None, access_token="", token_expires_at=None
    )

    new_tokens = AuthTokens(
        cookie="cookie",
        session_id="sess",
        access_token="token",
        token_expires_at=12345,
    )

    monkeypatch.setattr(
        arr_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        arr_mod, "async_authenticate", AsyncMock(return_value=(new_tokens, {}))
    )

    result = await coord._attempt_auto_refresh()

    assert result is True
    coord.client.update_credentials.assert_called_once_with(
        eauth="token", cookie="cookie"
    )
    coord._persist_tokens.assert_called_once_with(new_tokens)
    assert coord._tokens == new_tokens


@pytest.mark.asyncio
async def test_attempt_auto_refresh_coalesces_concurrent_calls(monkeypatch, hass):
    from custom_components.enphase_ev import auth_refresh_runtime as arr_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.api import AuthTokens

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._email = "user@example.com"
    coord._remember_password = True
    coord._stored_password = "secret"
    coord._refresh_lock = asyncio.Lock()
    coord._auth_refresh_task = None
    coord._auth_refresh_rejected_until = None
    coord._auth_refresh_rejected_ends_utc = None
    coord.diagnostics = SimpleNamespace(
        create_auth_block_issue=MagicMock(),
        clear_auth_block_issue=MagicMock(),
    )
    coord.client = SimpleNamespace(update_credentials=MagicMock())
    coord._persist_tokens = MagicMock()

    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0
    new_tokens = AuthTokens(
        cookie="cookie",
        session_id="sess",
        access_token="token",
        token_expires_at=12345,
    )

    monkeypatch.setattr(
        arr_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )

    async def _authenticate(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return new_tokens, {}

    monkeypatch.setattr(arr_mod, "async_authenticate", _authenticate)

    first = asyncio.create_task(coord._attempt_auto_refresh())
    await started.wait()
    second = asyncio.create_task(coord._attempt_auto_refresh())
    await asyncio.sleep(0)
    release.set()

    assert await first is True
    assert await second is True
    assert calls == 1
    coord.client.update_credentials.assert_called_once_with(
        eauth="token", cookie="cookie"
    )
    coord._persist_tokens.assert_called_once_with(new_tokens)
    assert coord._auth_refresh_task is None


@pytest.mark.asyncio
async def test_attempt_auto_refresh_invalid_credentials_enter_cooldown(
    monkeypatch, hass
):
    from custom_components.enphase_ev import auth_refresh_runtime as arr_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.api import EnlightenAuthInvalidCredentials

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._email = "user@example.com"
    coord._remember_password = True
    coord._stored_password = "secret"
    coord._refresh_lock = asyncio.Lock()
    coord._auth_refresh_task = None
    coord._auth_refresh_rejected_until = None
    coord._auth_refresh_rejected_ends_utc = None
    coord.diagnostics = SimpleNamespace(create_auth_block_issue=MagicMock())
    coord.client = SimpleNamespace(update_credentials=MagicMock())
    coord._persist_tokens = MagicMock()

    calls = 0

    monkeypatch.setattr(
        arr_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )

    async def _authenticate(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise EnlightenAuthInvalidCredentials()

    monkeypatch.setattr(arr_mod, "async_authenticate", _authenticate)

    assert await coord._attempt_auto_refresh() is False
    assert await coord._attempt_auto_refresh() is False
    assert calls == 1
    assert coord._auth_refresh_rejected_active() is True
    coord.client.update_credentials.assert_not_called()
    coord._persist_tokens.assert_not_called()


@pytest.mark.asyncio
async def test_attempt_auto_refresh_too_many_sessions_enters_auth_block(
    monkeypatch, hass
):
    from custom_components.enphase_ev import auth_refresh_runtime as arr_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.api import EnlightenAuthTooManySessions

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._email = "user@example.com"
    coord._remember_password = True
    coord._stored_password = "secret"
    coord._refresh_lock = asyncio.Lock()
    coord._auth_refresh_task = None
    coord._auth_refresh_rejected_until = None
    coord._auth_refresh_rejected_ends_utc = None
    coord.diagnostics = SimpleNamespace(create_auth_block_issue=MagicMock())
    coord.client = SimpleNamespace(update_credentials=MagicMock())
    coord._persist_tokens = MagicMock()

    monkeypatch.setattr(
        arr_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        arr_mod,
        "async_authenticate",
        AsyncMock(side_effect=EnlightenAuthTooManySessions()),
    )

    assert await coord._attempt_auto_refresh() is False
    assert coord._auth_block_active() is True
    assert coord._auth_block_reason == "too_many_active_sessions"
    coord.client.update_credentials.assert_not_called()
    coord._persist_tokens.assert_not_called()


@pytest.mark.asyncio
async def test_attempt_auto_refresh_reuses_recent_success(monkeypatch, hass):
    from custom_components.enphase_ev import auth_refresh_runtime as arr_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.api import AuthTokens

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._email = "user@example.com"
    coord._remember_password = True
    coord._stored_password = "secret"
    coord._refresh_lock = asyncio.Lock()
    coord._auth_refresh_task = None
    coord._auth_refresh_rejected_until = None
    coord._auth_refresh_rejected_ends_utc = None
    coord._auth_refresh_last_success_mono = None
    coord.client = SimpleNamespace(update_credentials=MagicMock())
    coord._persist_tokens = MagicMock()

    calls = 0
    new_tokens = AuthTokens(
        cookie="cookie",
        session_id="sess",
        access_token="token",
        token_expires_at=12345,
    )

    monkeypatch.setattr(
        arr_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )

    async def _authenticate(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return new_tokens, {}

    monkeypatch.setattr(arr_mod, "async_authenticate", _authenticate)

    assert await coord._attempt_auto_refresh() is True
    assert await coord._attempt_auto_refresh() is True
    assert calls == 1
    coord.client.update_credentials.assert_called_once_with(
        eauth="token", cookie="cookie"
    )
    coord._persist_tokens.assert_called_once_with(new_tokens)


@pytest.mark.asyncio
async def test_attempt_auto_refresh_rechecks_rejected_cooldown_inside_lock(hass):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._email = "user@example.com"
    coord._remember_password = True
    coord._stored_password = "secret"
    coord._refresh_lock = asyncio.Lock()
    coord._auth_refresh_task = None

    rejected_states = iter([False, True])
    arr = coord.auth_refresh_runtime
    arr.auth_refresh_rejected_active = lambda: next(rejected_states)  # type: ignore[method-assign]
    arr.auth_refresh_recent_success_active = lambda: False  # type: ignore[method-assign]
    arr.async_run_auto_refresh = AsyncMock(return_value=True)

    assert await coord._attempt_auto_refresh() is False
    arr.async_run_auto_refresh.assert_not_called()


@pytest.mark.asyncio
async def test_attempt_auto_refresh_rechecks_suspension_inside_lock(hass):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._email = "user@example.com"
    coord._remember_password = True
    coord._stored_password = "secret"
    coord._refresh_lock = asyncio.Lock()
    coord._auth_refresh_task = None

    suspended_states = iter([False, True])
    arr = coord.auth_refresh_runtime
    coord._auth_refresh_suspended_active = lambda: next(suspended_states)  # type: ignore[method-assign]
    arr.auth_refresh_rejected_active = lambda: False  # type: ignore[method-assign]
    arr.auth_refresh_recent_success_active = lambda: False  # type: ignore[method-assign]
    arr.async_run_auto_refresh = AsyncMock(return_value=True)

    assert await coord._attempt_auto_refresh() is False
    arr.async_run_auto_refresh.assert_not_called()


@pytest.mark.asyncio
async def test_attempt_auto_refresh_rechecks_recent_success_inside_lock(hass):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._email = "user@example.com"
    coord._remember_password = True
    coord._stored_password = "secret"
    coord._refresh_lock = asyncio.Lock()
    coord._auth_refresh_task = None

    recent_states = iter([False, True])
    arr = coord.auth_refresh_runtime
    arr.auth_refresh_recent_success_active = lambda: next(recent_states)  # type: ignore[method-assign]
    arr.auth_refresh_rejected_active = lambda: False  # type: ignore[method-assign]
    arr.async_run_auto_refresh = AsyncMock(return_value=True)

    assert await coord._attempt_auto_refresh() is True
    arr.async_run_auto_refresh.assert_not_called()


def test_auth_refresh_rejected_active_clears_expired_window(hass):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._auth_refresh_rejected_until = time.monotonic() - 1
    coord._auth_refresh_rejected_ends_utc = datetime.now(timezone.utc)

    assert coord._auth_refresh_rejected_active() is False
    assert coord._auth_refresh_rejected_until is None
    assert coord._auth_refresh_rejected_ends_utc is None


def test_note_auth_refresh_rejected_handles_utcnow_error(monkeypatch, hass):
    from custom_components.enphase_ev import auth_refresh_runtime as arr_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._auth_refresh_last_success_mono = 10.0
    coord._auth_refresh_rejected_until = None
    coord._auth_refresh_rejected_ends_utc = "sentinel"

    monkeypatch.setattr(
        arr_mod.dt_util,
        "utcnow",
        MagicMock(side_effect=RuntimeError("boom")),
    )

    coord._note_auth_refresh_rejected("invalid")

    assert coord._auth_refresh_last_success_mono is None
    assert coord._auth_refresh_rejected_until is not None
    assert coord._auth_refresh_rejected_ends_utc is None


def test_auth_refresh_suspended_active_clears_expired_window(hass, monkeypatch):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._auth_refresh_rejected_count = AUTH_REFRESH_REJECTED_SUSPEND_THRESHOLD
    coord._auth_refresh_suspended_until_utc = datetime.now(timezone.utc) - timedelta(
        seconds=1
    )
    coord._persist_auth_refresh_suspension_state = MagicMock()

    assert coord._auth_refresh_suspended_active() is False
    assert coord._auth_refresh_rejected_count == 0
    assert coord._auth_refresh_suspended_until_utc is None
    coord._persist_auth_refresh_suspension_state.assert_called_once_with()


def test_note_auth_refresh_suspended_clears_custom_reauth_issue(hass):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._persist_auth_refresh_suspension_state = MagicMock()
    coord.diagnostics = SimpleNamespace(
        clear_reauth_issue=MagicMock(),
        create_reauth_issue=MagicMock(),
    )
    suspended_until = datetime.now(timezone.utc) + timedelta(hours=1)

    coord._note_auth_refresh_suspended(suspended_until=suspended_until)

    assert coord._auth_refresh_suspended_until_utc == suspended_until
    coord._persist_auth_refresh_suspension_state.assert_called_once_with()
    coord.diagnostics.clear_reauth_issue.assert_called_once_with()
    coord.diagnostics.create_reauth_issue.assert_not_called()


def test_clear_auth_repair_issues_on_success_clears_reauth_when_not_suspended(hass):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._auth_refresh_suspended_until_utc = None
    coord.diagnostics = SimpleNamespace(
        clear_reauth_issue=MagicMock(),
        clear_auth_block_issue=MagicMock(),
    )

    coord._clear_auth_repair_issues_on_success()

    coord.diagnostics.clear_reauth_issue.assert_called_once_with()
    coord.diagnostics.clear_auth_block_issue.assert_not_called()


def test_clear_auth_repair_issues_on_success_without_diagnostics(hass):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord.diagnostics = None

    coord._clear_auth_repair_issues_on_success()


def test_clear_auth_repair_issues_on_success_preserves_reauth_when_suspended(hass):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._auth_refresh_suspended_until_utc = datetime.now(timezone.utc) + timedelta(
        hours=1
    )
    coord.diagnostics = SimpleNamespace(
        clear_reauth_issue=MagicMock(),
        clear_auth_block_issue=MagicMock(),
    )

    coord._clear_auth_repair_issues_on_success()

    coord.diagnostics.clear_reauth_issue.assert_not_called()
    coord.diagnostics.clear_auth_block_issue.assert_called_once_with()


@pytest.mark.asyncio
async def test_attempt_auto_refresh_suspends_after_repeated_rejections(
    monkeypatch, hass
):
    from custom_components.enphase_ev import auth_refresh_runtime as arr_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.api import EnlightenAuthInvalidCredentials

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._email = "user@example.com"
    coord._remember_password = True
    coord._stored_password = "secret"
    coord._refresh_lock = asyncio.Lock()
    coord._auth_refresh_task = None
    coord._auth_refresh_rejected_until = None
    coord._auth_refresh_rejected_ends_utc = None
    coord._auth_refresh_rejected_count = 0
    coord._auth_refresh_suspended_until_utc = None
    coord.client = SimpleNamespace(update_credentials=MagicMock())
    coord._persist_tokens = MagicMock()
    coord.diagnostics = SimpleNamespace(
        clear_reauth_issue=MagicMock(),
        create_reauth_issue=MagicMock(),
    )

    calls = 0

    monkeypatch.setattr(
        arr_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )

    async def _authenticate(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise EnlightenAuthInvalidCredentials()

    monkeypatch.setattr(arr_mod, "async_authenticate", _authenticate)

    for attempt in range(AUTH_REFRESH_REJECTED_SUSPEND_THRESHOLD):
        assert await coord._attempt_auto_refresh() is False
        assert coord._auth_refresh_rejected_count == attempt + 1
        if attempt + 1 < AUTH_REFRESH_REJECTED_SUSPEND_THRESHOLD:
            assert coord._auth_refresh_rejected_active() is True
            coord._auth_refresh_rejected_until = None
            coord._auth_refresh_rejected_ends_utc = None

    assert calls == AUTH_REFRESH_REJECTED_SUSPEND_THRESHOLD
    assert coord._auth_refresh_rejected_until is None
    assert coord._auth_refresh_rejected_ends_utc is None
    assert coord._auth_refresh_suspended_until_utc is not None
    coord.diagnostics.clear_reauth_issue.assert_called_once_with()
    coord.diagnostics.create_reauth_issue.assert_not_called()

    assert await coord._attempt_auto_refresh() is False
    assert calls == AUTH_REFRESH_REJECTED_SUSPEND_THRESHOLD
    coord.client.update_credentials.assert_not_called()
    coord._persist_tokens.assert_not_called()


@pytest.mark.asyncio
async def test_attempt_auto_refresh_success_clears_suspension(monkeypatch, hass):
    from custom_components.enphase_ev import auth_refresh_runtime as arr_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.api import AuthTokens

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._email = "user@example.com"
    coord._remember_password = True
    coord._stored_password = "secret"
    coord._refresh_lock = asyncio.Lock()
    coord._auth_refresh_rejected_count = AUTH_REFRESH_REJECTED_SUSPEND_THRESHOLD
    coord._auth_refresh_suspended_until_utc = datetime.now(timezone.utc) + timedelta(
        hours=1
    )
    coord.client = SimpleNamespace(update_credentials=MagicMock())
    coord._persist_tokens = MagicMock()
    coord._tokens = AuthTokens(
        cookie="", session_id=None, access_token="", token_expires_at=None
    )

    new_tokens = AuthTokens(
        cookie="cookie",
        session_id="sess",
        access_token="token",
        token_expires_at=12345,
    )

    monkeypatch.setattr(
        arr_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        arr_mod, "async_authenticate", AsyncMock(return_value=(new_tokens, {}))
    )

    assert await coord._async_run_auto_refresh() is True

    assert coord._auth_refresh_rejected_count == 0
    assert coord._auth_refresh_suspended_until_utc is None
    coord.client.update_credentials.assert_called_once_with(
        eauth="token", cookie="cookie"
    )
    coord._persist_tokens.assert_called_once_with(new_tokens)


def test_note_auth_refresh_rejected_threshold_handles_utcnow_error(monkeypatch, hass):
    from custom_components.enphase_ev import auth_refresh_runtime as arr_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._auth_refresh_rejected_count = AUTH_REFRESH_REJECTED_SUSPEND_THRESHOLD - 1
    coord._auth_refresh_rejected_until = time.monotonic() + 60
    coord._auth_refresh_rejected_ends_utc = datetime.now(timezone.utc) + timedelta(
        seconds=60
    )
    coord.diagnostics = SimpleNamespace(
        clear_reauth_issue=MagicMock(),
        create_reauth_issue=MagicMock(),
    )

    monkeypatch.setattr(
        arr_mod.dt_util,
        "utcnow",
        MagicMock(side_effect=RuntimeError("boom")),
    )

    coord._note_auth_refresh_rejected("invalid")

    assert coord._auth_refresh_rejected_count == AUTH_REFRESH_REJECTED_SUSPEND_THRESHOLD
    assert coord._auth_refresh_rejected_until is None
    assert coord._auth_refresh_rejected_ends_utc is None
    assert coord._auth_refresh_suspended_until_utc is not None
    coord.diagnostics.clear_reauth_issue.assert_called_once_with()
    coord.diagnostics.create_reauth_issue.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc_type",
    [
        "invalid",
        "mfa",
        "unavailable",
        "unexpected",
    ],
)
async def test_attempt_auto_refresh_failures(monkeypatch, hass, exc_type):
    from custom_components.enphase_ev import auth_refresh_runtime as arr_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.api import (
        AuthTokens,
        EnlightenAuthInvalidCredentials,
        EnlightenAuthMFARequired,
        EnlightenAuthUnavailable,
    )

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._email = "user@example.com"
    coord._remember_password = True
    coord._stored_password = "secret"
    coord._refresh_lock = asyncio.Lock()
    coord.client = SimpleNamespace(update_credentials=MagicMock())
    coord._persist_tokens = MagicMock()
    coord._tokens = AuthTokens(
        cookie="", session_id=None, access_token="", token_expires_at=None
    )

    monkeypatch.setattr(
        arr_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )

    if exc_type == "invalid":
        side_effect = EnlightenAuthInvalidCredentials()
    elif exc_type == "mfa":
        side_effect = EnlightenAuthMFARequired()
    elif exc_type == "unavailable":
        side_effect = EnlightenAuthUnavailable()
    else:
        side_effect = RuntimeError("boom")

    monkeypatch.setattr(
        arr_mod, "async_authenticate", AsyncMock(side_effect=side_effect)
    )

    result = await coord._attempt_auto_refresh()

    assert result is False
    coord.client.update_credentials.assert_not_called()
    coord._persist_tokens.assert_not_called()


@pytest.mark.asyncio
async def test_attempt_auto_refresh_requires_credentials(hass):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._email = None
    coord._remember_password = False
    coord._stored_password = None

    result = await coord._attempt_auto_refresh()
    assert result is False


@pytest.mark.asyncio
async def test_attempt_auto_refresh_skips_when_auth_block_active(hass):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._email = "user@example.com"
    coord._remember_password = True
    coord._stored_password = "secret"
    coord._auth_blocked_until_utc = datetime.now(timezone.utc) + timedelta(hours=1)

    assert await coord._attempt_auto_refresh() is False


@pytest.mark.asyncio
async def test_manual_auth_refresh_bypasses_block_and_clears_on_success(
    monkeypatch, hass
):
    from custom_components.enphase_ev import auth_refresh_runtime as arr_mod
    from custom_components.enphase_ev.api import AuthTokens
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._email = "user@example.com"
    coord._remember_password = True
    coord._stored_password = "secret"
    coord._refresh_lock = asyncio.Lock()
    coord._auth_refresh_task = None
    coord._auth_refresh_rejected_until = time.monotonic() + 300
    coord._auth_refresh_rejected_ends_utc = datetime.now(timezone.utc) + timedelta(
        minutes=5
    )
    coord._auth_refresh_rejected_count = 1
    coord._auth_refresh_suspended_until_utc = datetime.now(timezone.utc) + timedelta(
        hours=1
    )
    coord._auth_blocked_until_utc = datetime.now(timezone.utc) + timedelta(hours=1)
    coord._auth_block_reason = "login_wall_after_refresh_reject"
    coord.client = SimpleNamespace(update_credentials=MagicMock())
    coord.diagnostics = SimpleNamespace(clear_auth_block_issue=MagicMock())
    coord._persist_tokens = MagicMock()
    coord._tokens = AuthTokens(
        cookie="", session_id=None, access_token="", token_expires_at=None
    )

    new_tokens = AuthTokens(
        cookie="cookie",
        session_id="sess",
        access_token="token",
        token_expires_at=12345,
    )
    monkeypatch.setattr(
        arr_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        arr_mod, "async_authenticate", AsyncMock(return_value=(new_tokens, {}))
    )

    result = await coord.async_try_reauth_now()
    assert result.success is True
    assert result.reason is None

    coord.client.update_credentials.assert_called_once_with(
        eauth="token", cookie="cookie"
    )
    coord._persist_tokens.assert_called_once_with(new_tokens)
    assert coord._auth_blocked_until_utc is None
    assert coord._auth_block_reason is None
    assert coord._auth_refresh_suspended_until_utc is None


@pytest.mark.asyncio
async def test_manual_auth_refresh_requires_stored_credentials(hass):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._email = "user@example.com"
    coord._remember_password = True
    coord._stored_password = None

    result = await coord.async_try_reauth_now()
    assert result.success is False
    assert result.reason == "stored_credentials_unavailable"
    assert result.retry_after_seconds is None


@pytest.mark.asyncio
async def test_manual_auth_refresh_reuses_recent_success(hass):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._email = "user@example.com"
    coord._remember_password = True
    coord._stored_password = "secret"
    coord._auth_refresh_last_success_mono = time.monotonic()
    coord._auth_refresh_manual_retry_until = None
    coord._auth_blocked_until_utc = None
    coord._auth_block_reason = None
    coord._auth_refresh_suspended_until_utc = None
    coord._hems_auth_backoff_until = None
    coord._hems_auth_backoff_ends_utc = None
    coord._hems_auth_failure_count = 0
    coord._hems_auth_last_endpoint = None
    coord._hems_auth_last_status = None
    coord._hems_auth_last_reason = None
    coord.auth_refresh_runtime.async_run_auto_refresh = AsyncMock(return_value=True)

    result = await coord.async_try_reauth_now()
    assert result.success is True
    assert result.reason is None
    coord.auth_refresh_runtime.async_run_auto_refresh.assert_not_called()
    assert coord._auth_blocked_until_utc is None
    assert coord._auth_block_reason is None
    assert coord._auth_refresh_suspended_until_utc is None
    assert coord._hems_auth_backoff_until is None
    assert coord._hems_auth_last_endpoint is None
    assert coord._hems_auth_last_status is None
    assert coord._hems_auth_last_reason is None
    assert coord._hems_auth_failure_count == 0


@pytest.mark.asyncio
async def test_manual_auth_refresh_does_not_reuse_recent_success_during_active_block(
    hass,
):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._email = "user@example.com"
    coord._remember_password = True
    coord._stored_password = "secret"
    coord._refresh_lock = asyncio.Lock()
    coord._auth_refresh_task = None
    coord._auth_refresh_last_success_mono = time.monotonic()
    coord._auth_refresh_manual_retry_until = None
    coord._auth_blocked_until_utc = datetime.now(timezone.utc) + timedelta(hours=1)
    coord._auth_block_reason = "too_many_active_sessions"
    coord._auth_refresh_suspended_until_utc = None
    coord._hems_auth_backoff_until = time.monotonic() + 60
    coord._hems_auth_backoff_ends_utc = datetime.now(timezone.utc) + timedelta(
        minutes=15
    )
    coord._hems_auth_failure_count = 1
    coord._hems_auth_last_endpoint = "hems_devices"
    coord._hems_auth_last_status = 401
    coord._hems_auth_last_reason = "unauthorized"
    coord.auth_refresh_runtime.async_run_auto_refresh = AsyncMock(return_value=True)

    result = await coord.async_try_reauth_now()

    assert result.success is True
    assert result.performed_refresh is True
    coord.auth_refresh_runtime.async_run_auto_refresh.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_manual_auth_refresh_failed_attempt_enters_short_cooldown(hass):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._email = "user@example.com"
    coord._remember_password = True
    coord._stored_password = "secret"
    coord._refresh_lock = asyncio.Lock()
    coord._auth_refresh_task = None
    coord._auth_refresh_last_success_mono = None
    coord._auth_refresh_manual_retry_until = None
    coord.auth_refresh_runtime.async_run_auto_refresh = AsyncMock(return_value=False)

    result = await coord.async_try_reauth_now()
    assert result.success is False
    assert result.reason == "reauth_failed"
    assert result.retry_after_seconds is None
    cooldown_until = coord._auth_refresh_manual_retry_until
    assert isinstance(cooldown_until, float)
    assert cooldown_until > time.monotonic()

    result = await coord.async_try_reauth_now()
    assert result.success is False
    assert result.reason == "manual_retry_cooldown_active"
    assert result.retry_after_seconds is not None
    coord.auth_refresh_runtime.async_run_auto_refresh.assert_awaited_once_with()
    assert coord._auth_refresh_manual_retry_until == cooldown_until


def test_manual_auth_refresh_clears_expired_retry_cooldown(hass):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._auth_refresh_manual_retry_until = time.monotonic() - 1

    assert coord.auth_refresh_runtime.manual_refresh_retry_active() is False
    assert coord._auth_refresh_manual_retry_until is None


@pytest.mark.asyncio
async def test_manual_auth_refresh_rechecks_recent_success_inside_lock(hass):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._email = "user@example.com"
    coord._remember_password = True
    coord._stored_password = "secret"
    coord._refresh_lock = asyncio.Lock()
    coord._auth_refresh_task = None

    recent_states = iter([False, True])
    arr = coord.auth_refresh_runtime
    arr.auth_refresh_recent_success_active = lambda: next(recent_states)  # type: ignore[method-assign]
    arr.manual_refresh_retry_after_seconds = lambda: None  # type: ignore[method-assign]
    arr.async_run_auto_refresh = AsyncMock(return_value=True)

    result = await coord.async_try_reauth_now()
    assert result.success is True
    assert result.reason is None
    arr.async_run_auto_refresh.assert_not_called()


@pytest.mark.asyncio
async def test_manual_auth_refresh_rechecks_retry_cooldown_inside_lock(hass):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._email = "user@example.com"
    coord._remember_password = True
    coord._stored_password = "secret"
    coord._refresh_lock = asyncio.Lock()
    coord._auth_refresh_task = None

    retry_states = iter([False, True])
    arr = coord.auth_refresh_runtime
    arr.auth_refresh_recent_success_active = lambda: False  # type: ignore[method-assign]
    arr.manual_refresh_retry_after_seconds = lambda: 60 if next(retry_states) else None  # type: ignore[method-assign]
    arr.async_run_auto_refresh = AsyncMock(return_value=True)

    result = await coord.async_try_reauth_now()
    assert result.success is False
    assert result.reason == "manual_retry_cooldown_active"
    assert result.retry_after_seconds == 60
    arr.async_run_auto_refresh.assert_not_called()


@pytest.mark.asyncio
async def test_manual_auth_refresh_shares_in_flight_refresh_task(hass):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._email = "user@example.com"
    coord._remember_password = True
    coord._stored_password = "secret"
    task = asyncio.create_task(asyncio.sleep(0, result=True))
    coord._auth_refresh_task = task

    result = await coord.async_try_reauth_now()
    assert result.success is True
    assert result.reason is None


@pytest.mark.asyncio
async def test_attempt_auto_refresh_skips_when_auth_refresh_suspended(hass):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._email = "user@example.com"
    coord._remember_password = True
    coord._stored_password = "secret"
    coord._auth_refresh_suspended_until_utc = datetime.now(timezone.utc) + timedelta(
        hours=1
    )

    assert await coord._attempt_auto_refresh() is False


@pytest.mark.asyncio
async def test_activate_auth_block_from_login_wall_persists_state(monkeypatch, hass):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.api import EnphaseLoginWallUnauthorized

    entry = _make_entry(hass)
    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        coord_mod,
        "async_call_later",
        lambda *_args, **_kwargs: (lambda: None),
    )
    coord = EnphaseCoordinator(hass, entry.data, config_entry=entry)
    coord._auth_refresh_rejected_until = time.monotonic() + 60
    coord._auth_refresh_rejected_ends_utc = datetime.now(timezone.utc) + timedelta(
        seconds=60
    )
    coord._auth_blocked_until_utc = None
    coord._auth_block_reason = None
    coord._last_error = None

    captured: list[dict] = []

    def _update_entry(entry_obj, data=None, **kwargs):
        assert entry_obj is entry
        captured.append(dict(data))

    monkeypatch.setattr(hass.config_entries, "async_update_entry", _update_entry)

    err = EnphaseLoginWallUnauthorized(
        endpoint="/service/test",
        request_label="GET /service/test",
        status=200,
        content_type="application/json; charset=utf-8",
        body_preview_redacted="<!DOCTYPE html>",
    )

    assert coord._activate_auth_block_from_login_wall(err) is True
    assert coord._auth_block_reason == "login_wall_after_refresh_reject"
    assert coord._auth_blocked_until_utc is not None
    assert captured
    assert captured[-1][CONF_AUTH_BLOCK_REASON] == "login_wall_after_refresh_reject"
    assert CONF_AUTH_BLOCKED_UNTIL in captured[-1]


@pytest.mark.asyncio
async def test_activate_auth_block_from_login_wall_requires_refresh_rejection(hass):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.api import EnphaseLoginWallUnauthorized

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._auth_refresh_rejected_until = None

    err = EnphaseLoginWallUnauthorized(
        endpoint="/service/test",
        request_label="GET /service/test",
        status=200,
        content_type="text/html; charset=utf-8",
        body_preview_redacted="<!DOCTYPE html>",
    )

    assert coord._activate_auth_block_from_login_wall(err) is False


@pytest.mark.asyncio
async def test_activate_auth_block_from_login_wall_reuses_existing_block(hass):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.api import EnphaseLoginWallUnauthorized

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._auth_refresh_rejected_until = time.monotonic() + 60
    coord._auth_blocked_until_utc = datetime.now(timezone.utc) + timedelta(hours=1)
    coord._auth_block_reason = "login_wall_after_refresh_reject"
    coord.diagnostics = SimpleNamespace(create_auth_block_issue=MagicMock())
    coord.auth_refresh_runtime = SimpleNamespace(
        note_login_wall_block=MagicMock(),
        auth_refresh_rejected_active=MagicMock(return_value=True),
    )

    err = EnphaseLoginWallUnauthorized(
        endpoint="/service/test",
        request_label="GET /service/test",
        status=200,
        content_type="text/html; charset=utf-8",
        body_preview_redacted="<!DOCTYPE html>",
    )

    assert coord._activate_auth_block_from_login_wall(err) is True
    coord.diagnostics.create_auth_block_issue.assert_called_once()
    coord.auth_refresh_runtime.note_login_wall_block.assert_not_called()


@pytest.mark.asyncio
async def test_async_update_data_fails_fast_while_auth_blocked(monkeypatch, hass):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from homeassistant.helpers.update_coordinator import UpdateFailed

    entry = _make_entry(hass)
    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        coord_mod,
        "async_call_later",
        lambda *_args, **_kwargs: (lambda: None),
    )
    coord = EnphaseCoordinator(hass, entry.data, config_entry=entry)
    coord.client.status = AsyncMock()
    coord._auth_blocked_until_utc = datetime.now(timezone.utc) + timedelta(hours=1)
    coord._auth_block_reason = "login_wall_after_refresh_reject"

    with pytest.raises(UpdateFailed, match="temporarily blocked") as exc_info:
        await coord._async_update_data()

    assert 3590 <= exc_info.value.retry_after <= 3600
    coord.client.status.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_update_data_fails_fast_while_site_only_auth_blocked(
    monkeypatch, hass
):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    entry = _make_entry(
        hass,
        {
            CONF_SERIALS: [],
            CONF_SITE_ONLY: True,
            CONF_AUTH_BLOCKED_UNTIL: (
                datetime.now(timezone.utc) + timedelta(hours=1)
            ).isoformat(),
            CONF_AUTH_BLOCK_REASON: "login_wall_after_refresh_reject",
        },
    )
    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        coord_mod,
        "async_call_later",
        lambda *_args, **_kwargs: (lambda: None),
    )
    coord = EnphaseCoordinator(hass, entry.data, config_entry=entry)
    coord.energy._async_refresh_site_energy = AsyncMock()  # noqa: SLF001

    with pytest.raises(UpdateFailed, match="temporarily blocked") as exc_info:
        await coord._async_update_data()

    assert 3590 <= exc_info.value.retry_after <= 3600
    coord.energy._async_refresh_site_energy.assert_not_awaited()  # noqa: SLF001


@pytest.mark.asyncio
async def test_async_update_data_login_wall_during_refresh_cooldown_blocks(
    monkeypatch, hass
):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.api import EnphaseLoginWallUnauthorized
    from homeassistant.helpers.update_coordinator import UpdateFailed

    entry = _make_entry(hass)
    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        coord_mod,
        "async_call_later",
        lambda *_args, **_kwargs: (lambda: None),
    )
    coord = EnphaseCoordinator(hass, entry.data, config_entry=entry)
    coord._auth_refresh_rejected_until = time.monotonic() + 60
    coord._auth_refresh_rejected_ends_utc = datetime.now(timezone.utc) + timedelta(
        seconds=60
    )
    coord.client.status = AsyncMock(
        side_effect=EnphaseLoginWallUnauthorized(
            endpoint="/service/test",
            request_label="GET /service/test",
            status=200,
            content_type="text/html; charset=utf-8",
            body_preview_redacted="<!DOCTYPE html>",
        )
    )

    with pytest.raises(UpdateFailed, match="temporarily blocked") as exc_info:
        await coord._async_update_data()

    assert coord._auth_block_reason == "login_wall_after_refresh_reject"
    assert coord._auth_blocked_until_utc is not None
    assert 86390 <= exc_info.value.retry_after <= 86400


@pytest.mark.asyncio
async def test_handle_client_unauthorized_refreshes_tokens(monkeypatch, hass):
    from custom_components.enphase_ev import coordinator_diagnostics as diag_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._unauth_errors = 0
    coord._last_error = None
    coord._attempt_auto_refresh = AsyncMock(return_value=True)
    coord.site_id = "SITE"
    coord.site_name = "Garage"
    coord._backoff_until = None
    coord.backoff_ends_utc = None
    coord.last_success_utc = None
    coord.last_failure_utc = None
    coord.last_failure_status = None
    coord.last_failure_description = None
    coord.last_failure_source = None
    coord.last_failure_response = None
    coord.latency_ms = 0
    coord._network_errors = 0
    coord._http_errors = 0
    coord._rate_limit_hits = 0
    coord._dns_failures = 0
    coord._phase_timings = {}

    deleted: list[tuple] = []
    monkeypatch.setattr(
        diag_mod.ir,
        "async_delete_issue",
        lambda *args, **kwargs: deleted.append((args, kwargs)),
    )
    monkeypatch.setattr(diag_mod.ir, "async_create_issue", lambda *args, **kwargs: None)

    result = await coord._handle_client_unauthorized()

    assert result is True
    assert coord._unauth_errors == 0
    assert deleted and deleted[0][0][2] == "reauth_required"


@pytest.mark.asyncio
async def test_handle_client_unauthorized_creates_issue_after_failures(
    monkeypatch, hass
):
    from custom_components.enphase_ev import coordinator_diagnostics as diag_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._unauth_errors = 1
    coord._last_error = None
    coord._attempt_auto_refresh = AsyncMock(return_value=False)
    coord.site_id = "SITE"
    coord.site_name = "Garage"
    coord._backoff_until = None
    coord.backoff_ends_utc = None
    coord.last_success_utc = None
    coord.last_failure_utc = None
    coord.last_failure_status = None
    coord.last_failure_description = None
    coord.last_failure_source = None
    coord.last_failure_response = None
    coord.latency_ms = 0
    coord._network_errors = 0
    coord._http_errors = 0
    coord._rate_limit_hits = 0
    coord._dns_failures = 0
    coord._phase_timings = {}

    created: list[tuple] = []
    monkeypatch.setattr(diag_mod.ir, "async_delete_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        diag_mod.ir,
        "async_create_issue",
        lambda *args, **kwargs: created.append((args, kwargs)),
    )

    from homeassistant.exceptions import ConfigEntryAuthFailed

    with pytest.raises(ConfigEntryAuthFailed):
        await coord._handle_client_unauthorized()

    assert coord._unauth_errors == 2
    assert created == []


def test_blocked_auth_failure_message_handles_missing_timestamp():
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord._auth_blocked_until_utc = None

    assert coord._blocked_auth_failure_message().endswith("retry later.")


def test_auth_block_retry_after_returns_none_when_expired():
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord._auth_blocked_until_utc = datetime.now(timezone.utc) - timedelta(seconds=1)

    assert coord._auth_block_retry_after_s() is None


def test_persist_tokens_updates_entry(hass, monkeypatch):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.api import AuthTokens

    entry = _make_entry(hass)
    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord.config_entry = entry
    coord._auth_refresh_rejected_count = AUTH_REFRESH_REJECTED_SUSPEND_THRESHOLD
    coord._auth_refresh_suspended_until_utc = datetime.now(timezone.utc) + timedelta(
        hours=1
    )
    coord._auth_blocked_until_utc = datetime.now(timezone.utc) + timedelta(hours=1)
    coord._auth_block_reason = "login_wall_after_refresh_reject"

    captured: list[tuple] = []

    def fake_update_entry(entry_obj, data=None, **kwargs):
        captured.append((entry_obj, data))

    monkeypatch.setattr(hass.config_entries, "async_update_entry", fake_update_entry)

    tokens = AuthTokens(
        cookie="cookie",
        session_id="sess",
        access_token="token",
        token_expires_at=123,
    )
    coord._persist_tokens(tokens)

    assert captured
    updated_entry, payload = captured[0]
    assert updated_entry is entry
    assert payload[CONF_COOKIE] == "cookie"
    assert payload[CONF_EAUTH] == "token"
    assert CONF_AUTH_REFRESH_SUSPENDED_UNTIL not in payload
    assert CONF_AUTH_BLOCKED_UNTIL not in payload
    assert CONF_AUTH_BLOCK_REASON not in payload
    assert payload[CONF_SESSION_ID] == "sess"
    assert coord._auth_refresh_rejected_count == 0
    assert coord._auth_refresh_suspended_until_utc is None


def test_seed_nominal_voltage_option_from_api_updates_missing_option(hass, monkeypatch):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    entry = _make_entry(hass, options={})
    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord.config_entry = entry
    coord._operating_v = {"EV1": 230}
    coord._nominal_v = 120

    captured: list[dict] = []

    def fake_update_entry(entry_obj, **kwargs):
        assert entry_obj is entry
        captured.append(kwargs.get("options", {}))

    monkeypatch.setattr(hass.config_entries, "async_update_entry", fake_update_entry)

    coord._seed_nominal_voltage_option_from_api()

    assert coord._nominal_v == 230
    assert captured and captured[0][OPT_NOMINAL_VOLTAGE] == 230


def test_seed_nominal_voltage_option_from_api_keeps_user_option(hass, monkeypatch):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    entry = _make_entry(hass, options={OPT_NOMINAL_VOLTAGE: 220})
    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord.config_entry = entry
    coord._operating_v = {"EV1": 230}
    coord._nominal_v = 120

    called = {"value": False}

    def fake_update_entry(*_args, **_kwargs):
        called["value"] = True

    monkeypatch.setattr(hass.config_entries, "async_update_entry", fake_update_entry)

    coord._seed_nominal_voltage_option_from_api()

    assert coord._nominal_v == 220
    assert called["value"] is False


def test_preferred_nominal_voltage_uses_config_when_no_api_voltage(hass):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._operating_v = {}
    coord._nominal_v = 220

    assert coord.preferred_nominal_voltage() == 220


def test_preferred_nominal_voltage_prefers_api_voltage(hass):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._operating_v = {"EV1": 230, "EV2": 230, "EV3": 120}
    coord._nominal_v = 120

    assert coord.preferred_nominal_voltage() == 230


def test_seed_nominal_voltage_option_from_api_handles_update_failure(hass, monkeypatch):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    entry = _make_entry(hass, options={})
    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord.config_entry = entry
    coord._operating_v = {"EV1": 230}
    coord._nominal_v = 120

    def fail_update(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(hass.config_entries, "async_update_entry", fail_update)

    coord._seed_nominal_voltage_option_from_api()
    assert coord._nominal_v == 230


def test_kick_fast_handles_invalid_input(monkeypatch, hass):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._fast_until = None

    monkeypatch.setattr(coord_mod.time, "monotonic", lambda: 500.0)

    coord.kick_fast("bad-input")

    assert coord._fast_until == pytest.approx(560.0)


def test_set_charging_expectation_handles_hold(monkeypatch, hass):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._pending_charging = {}

    monkeypatch.setattr(coord_mod.time, "monotonic", lambda: 1_000.0)

    coord.set_charging_expectation("EV1", True, hold_for=10)
    assert coord._pending_charging["EV1"] == (True, 1_010.0)

    coord.set_charging_expectation("EV1", False, hold_for=0)
    assert "EV1" not in coord._pending_charging


def test_slow_interval_floor_with_invalid_option(hass):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    entry = _make_entry(hass, options={OPT_SLOW_POLL_INTERVAL: "not-int"})
    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.config_entry = entry
    coord.update_interval = timedelta(seconds=5)

    result = coord._slow_interval_floor()
    assert result == DEFAULT_SLOW_POLL_INTERVAL


def test_clear_backoff_timer_handles_exception():
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    called = {"cancelled": False}

    def canceller():
        called["cancelled"] = True
        raise RuntimeError("fail")

    coord._backoff_cancel = canceller
    coord.backoff_ends_utc = object()

    coord._clear_backoff_timer()

    assert called["cancelled"] is True
    assert coord._backoff_cancel is None
    assert coord.backoff_ends_utc is None


def test_schedule_backoff_timer_zero_delay(monkeypatch, hass):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.hass = hass
    coord._backoff_cancel = None
    coord._backoff_until = 123.0
    coord.async_request_refresh = AsyncMock()

    monkeypatch.setattr(
        coord_mod, "async_call_later", lambda *args, **kwargs: lambda: None
    )

    coord._schedule_backoff_timer(0)

    coord.async_request_refresh.assert_called_once()
    assert coord._backoff_until is None
    assert coord.backoff_ends_utc is None


def test_schedule_backoff_timer_sets_callback(monkeypatch, hass):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.hass = hass
    coord.async_request_refresh = AsyncMock()
    callbacks: list = []

    monkeypatch.setattr(
        coord_mod,
        "async_call_later",
        lambda _hass, _delay, cb: callbacks.append(cb) or (lambda: None),
    )
    monkeypatch.setattr(
        coord_mod.dt_util, "utcnow", lambda: datetime(2025, 1, 1, tzinfo=timezone.utc)
    )

    coord._backoff_cancel = None

    coord._schedule_backoff_timer(5.0)

    assert coord.backoff_ends_utc is not None
    assert callbacks, "callback should be registered"


def test_coerce_and_apply_amp_helpers(hass):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.data = {"EV1": {"min_amp": "10", "max_amp": "8"}}
    coord.last_set_amps = {}

    assert EnphaseCoordinator._coerce_amp(None) is None
    assert EnphaseCoordinator._coerce_amp(" 12 ") == 12
    assert EnphaseCoordinator._coerce_amp("bad") is None

    min_amp, max_amp = coord._amp_limits("EV1")
    assert min_amp == 10
    assert max_amp == 10  # inverted bounds clamp

    assert coord._apply_amp_limits("EV1", 40) == 10
    assert coord._apply_amp_limits("EV1", None) == 10

    coord.data["EV1"]["charging_level"] = " 16 "
    coord.data["EV1"]["session_charge_level"] = " 14 "
    coord.last_set_amps["EV1"] = 20
    assert coord.pick_start_amps("EV1", requested="18") == 10
    coord.data = {}
    coord.last_set_amps = {}
    assert coord.pick_start_amps("EV1", requested=None, fallback=24) == 24


@pytest.mark.asyncio
async def test_get_charge_mode_uses_cache_and_client(monkeypatch, hass):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord._charge_mode_cache = {"EV1": ("MANUAL_CHARGING", coord_mod.time.monotonic())}
    coord.client = SimpleNamespace(
        charge_mode=AsyncMock(return_value="SCHEDULED_CHARGING")
    )

    cached = await coord._get_charge_mode("EV1")
    assert cached == "MANUAL_CHARGING"

    coord._charge_mode_cache["EV1"] = (
        "MANUAL_CHARGING",
        coord_mod.time.monotonic() - 1_000,
    )
    result = await coord._get_charge_mode("EV1")
    assert result == "SCHEDULED_CHARGING"
    assert coord._charge_mode_cache["EV1"][0] == "SCHEDULED_CHARGING"

    coord.client.charge_mode = AsyncMock(side_effect=RuntimeError("fail"))
    result = await coord._get_charge_mode("EV2")
    assert result is None


def test_set_charge_mode_cache_updates(monkeypatch, hass):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord._charge_mode_cache = {}

    coord.set_charge_mode_cache("EV1", "SCHEDULED")
    value, ts = coord._charge_mode_cache["EV1"]
    assert value == "SCHEDULED_CHARGING"
    assert ts >= 0


def test_set_last_set_amps_and_require_plugged(hass):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.hass = hass
    coord.last_set_amps = {}
    coord.data = {
        "EV1": {"min_amp": 10, "max_amp": 40, "plugged": False, "name": "Garage"}
    }

    coord.set_last_set_amps("EV1", 50)
    assert coord.last_set_amps["EV1"] == 40

    with pytest.raises(ServiceValidationError):
        coord.require_plugged("EV1")

    coord.data["EV1"]["plugged"] = True
    coord.require_plugged("EV1")  # should not raise


@pytest.mark.asyncio
async def test_async_update_data_handles_complex_payload(monkeypatch, hass):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    entry = _make_entry(hass)

    time_keeper = SimpleNamespace(monotonic=1_000.0)

    def monotonic():
        return time_keeper.monotonic

    def advance(seconds: float):
        time_keeper.monotonic += seconds

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(coord_mod.time, "monotonic", monotonic)
    monkeypatch.setattr(coord_mod.time, "time", lambda: 1_700_000_000)
    monkeypatch.setattr(
        coord_mod.dt_util,
        "utcnow",
        lambda: datetime(2025, 5, 1, 12, 0, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        coord_mod.dt_util,
        "now",
        lambda: datetime(2025, 5, 1, 12, 0, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(hass, "async_create_task", lambda coro: None)

    class BadDisplay:
        def __str__(self) -> str:
            raise ValueError("boom")

    class BadAuth:
        def __str__(self) -> str:
            raise ValueError("bad-auth")

    class ComplexClient:
        def __init__(self):
            self.payload = {
                "ts": "2025-05-01T12:00:00Z[UTC]",
                "evChargerData": [
                    {
                        "sn": "EV1",
                        "name": "Main Charger",
                        "displayName": BadDisplay(),
                        "connected": " yes ",
                        "pluggedIn": " TRUE ",
                        "charging": True,
                        "chargingLevel": " 32 ",
                        "faulted": "0",
                        "offGrid": "ON_GRID",
                        "evManufacturerName": "Example OEM",
                        "smartEV": {"hasToken": True, "hasEVDetails": False},
                        "connectors": [
                            {
                                "connectorStatusType": "Charging",
                                "connectorStatusReason": "OK",
                            }
                        ],
                        "session_d": {
                            "e_c": 500,
                            "charge_level": " 18 ",
                            "session_cost": " 3.4567 ",
                            "miles": " 12.3456 ",
                            "start_time": "1714550000000",
                            "auth_status": 1,
                            "auth_type": "APP",
                            "auth_id": BadAuth(),
                        },
                        "sch_d": {
                            "status": "enabled",
                            "info": [
                                {
                                    "type": "eco",
                                    "startTime": "06:00",
                                    "endTime": "08:00",
                                    "days": [1, "bad", 2],
                                    "remindFlag": None,
                                    "reminderEnabled": "true",
                                }
                            ],
                        },
                    },
                    {
                        "sn": "EV2",
                        "name": "Second Charger",
                        "connected": 1,
                        "pluggedIn": True,
                        "charging": False,
                        "connectors": [{"connectorStatusType": "SUSPENDED_EVSE"}],
                        "session_d": {"plg_out_at": "1714553600000"},
                    },
                ],
            }

        async def status(self):
            return self.payload

        async def summary_v2(self):
            advance(0.1)
            return [
                {
                    "serialNumber": "EV1",
                    "displayName": "Driveway",
                    "maxCurrent": 48,
                    "chargeLevelDetails": {"min": " 6 ", "max": " 40 "},
                    "phaseMode": "single",
                    "status": "READY",
                    "activeConnection": " ethernet ",
                    "networkConfig": '[{"ipaddr":"192.168.1.20","connectionStatus":"1","mac_addr":"00:11:22:33:44:55"},{"ipaddr":"192.168.1.21","connectionStatus":"0","mac_addr":""}]',
                    "reportingInterval": " 300 ",
                    "dlbEnabled": "true",
                    "commissioningStatus": True,
                    "lastReportedAt": "2025-05-01T11:59:00Z",
                    "timezone": "Region/City",
                    "isConnected": "yes",
                    "isLocallyConnected": "0",
                    "hoControl": "on",
                    "operatingVoltage": "240.5",
                    "lifeTimeConsumption": "12345.6",
                    "chargeLevel": "28",
                    "warrantyStartDate": "2025-01-01T00:00:00Z[UTC]",
                    "warrantyDueDate": "2030-01-01T00:00:00Z[UTC]",
                    "warrantyPeriod": 5,
                    "breakerRating": "48",
                    "ratedCurrent": "40",
                    "phaseCount": "1",
                    "gridType": "2",
                    "wifiConfig": "status=connected",
                    "cellularConfig": "status=disconnected",
                    "defaultRoute": "interface=mlan0",
                    "wiringConfiguration": {"L1": "L1"},
                    "kernelVersion": "6.6.23",
                    "bootloaderVersion": "2024.04",
                    "createdAt": "2025-01-01T00:00:00Z[UTC]",
                    "functionalValDetails": {
                        "state": 1,
                        "lastUpdatedTimestamp": 1_714_550_000_000,
                    },
                    "gatewayConnectivityDetails": [
                        "bad",
                        {
                            "gwConnStatus": 0,
                            "gwConnFailureReason": 0,
                            "lastConnTime": 1_714_550_000_000,
                        },
                        {
                            "gwConnStatus": 1,
                            "gwConnFailureReason": 7,
                            "lastConnTime": 1_714_550_600_000,
                        },
                    ],
                },
                {
                    "serialNumber": "EV2",
                    "networkConfig": '[{"ipaddr":"10.0.0.2","connectionStatus":"1","mac_addr":""}]',
                },
            ]

        async def charge_mode(self, sn: str):
            return {"EV2": "REMOTE"}.get(sn)

    client = ComplexClient()
    coord = EnphaseCoordinator(hass, entry.data, config_entry=entry)
    coord.client = client
    coord._async_resolve_charge_modes = AsyncMock(
        return_value={"EV1": None, "EV2": "REMOTE"}
    )
    coord._pending_charging = {"EV1": (False, time_keeper.monotonic + 5)}
    coord._last_charging = {"EV2": True}
    coord._session_end_fix = {}
    coord._operating_v = {}
    coord.last_set_amps = {}
    coord.set_last_set_amps = MagicMock(side_effect=RuntimeError("bad"))
    coord._summary_cache = None
    coord._schedule_session_enrichment = MagicMock()

    data = await coord._async_update_data()

    assert data["EV1"]["charge_mode"] == "IDLE"
    assert data["EV1"]["session_kwh"] == pytest.approx(0.5)
    assert data["EV1"]["session_charge_level"] == 18
    assert data["EV1"]["session_cost"] == pytest.approx(3.457)
    assert data["EV1"]["session_miles"] == pytest.approx(12.346)
    assert data["EV1"]["connector_status"] == "Charging"
    assert data["EV1"]["charging"] is False
    assert data["EV1"]["display_name"] == "Driveway"
    assert data["EV1"]["operating_v"] == 240
    assert data["EV1"]["reporting_interval"] == 300
    assert data["EV1"]["dlb_enabled"] is True
    assert data["EV1"]["ip_address"] == "192.168.1.20"
    assert data["EV1"]["mac_address"] == "00:11:22:33:44:55"
    assert data["EV1"]["network_interface_count"] == 1
    assert data["EV1"]["off_grid_state"] == "ON_GRID"
    assert data["EV1"]["ev_manufacturer_name"] == "Example OEM"
    assert data["EV1"]["smart_ev_has_token"] is True
    assert data["EV1"]["session_auth_status"] == 1
    assert data["EV1"]["session_auth_type"] == "APP"
    assert data["EV1"]["session_auth_identifier"] is None
    assert data["EV1"]["schedule_days"] == [1, 2]
    assert data["EV1"]["schedule_reminder_enabled"] is True
    assert data["EV1"]["warranty_period_years"] == 5
    assert data["EV1"]["breaker_rating"] == 48
    assert data["EV1"]["rated_current"] == 40
    assert data["EV1"]["phase_count"] == 1
    assert data["EV1"]["grid_type"] == 2
    assert data["EV1"]["wifi_config"] == "status=connected"
    assert data["EV1"]["cellular_config"] == "status=disconnected"
    assert data["EV1"]["default_route"] == "interface=mlan0"
    assert data["EV1"]["wiring_configuration"] == {"L1": "L1"}
    assert data["EV1"]["kernel_version"] == "6.6.23"
    assert data["EV1"]["bootloader_version"] == "2024.04"
    assert data["EV1"]["created_at"] == "2025-01-01T00:00:00Z[UTC]"
    assert data["EV1"]["charger_timezone"] == "Region/City"
    assert data["EV1"]["is_connected"] is True
    assert data["EV1"]["is_locally_connected"] is False
    assert data["EV1"]["ho_control"] is True
    assert data["EV1"]["functional_validation_state"] == 1
    assert data["EV1"]["functional_validation_updated_at"] == 1714550000
    assert data["EV1"]["gateway_connection_count"] == 3
    assert data["EV1"]["gateway_connected_count"] == 1
    assert data["EV1"]["gateway_last_connection_at"] == 1714550600
    assert data["EV1"]["gateway_connectivity_details"] == [
        {"status": 0, "failure_reason": 0, "last_connection_at": 1714550000},
        {"status": 1, "failure_reason": 7, "last_connection_at": 1714550600},
    ]

    assert data["EV2"]["suspended_by_evse"] is True
    assert isinstance(data["EV2"]["session_end"], int)
    assert data["EV2"]["ip_address"] == "10.0.0.2"
    assert "mac_address" not in data["EV2"]
