"""Shared pytest fixtures for the Enphase Energy custom integration."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_socket
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from unittest.mock import AsyncMock, patch

try:
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_EMAIL,
        CONF_PASSWORD,
        CONF_REMEMBER_PASSWORD,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SESSION_ID,
        CONF_SITE_ID,
        CONF_SITE_NAME,
        CONF_SITE_ONLY,
        CONF_TOKEN_EXPIRES_AT,
        DOMAIN,
    )
except ModuleNotFoundError:  # pragma: no cover - fallback for local test runs
    ROOT = Path(__file__).resolve().parents[3]
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_EMAIL,
        CONF_PASSWORD,
        CONF_REMEMBER_PASSWORD,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SESSION_ID,
        CONF_SITE_ID,
        CONF_SITE_NAME,
        CONF_SITE_ONLY,
        CONF_TOKEN_EXPIRES_AT,
        DOMAIN,
    )

from .random_ids import RANDOM_SERIAL, RANDOM_SITE_ID

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _seed_default_type_buckets(coord: Any) -> None:
    """Populate default type buckets for coordinator stubs in tests."""
    setter = getattr(coord, "_set_type_device_buckets", None)
    if not callable(setter):
        inventory_runtime = getattr(coord, "inventory_runtime", None)
        setter = getattr(inventory_runtime, "_set_type_device_buckets", None)
    site_id = str(getattr(coord, "site_id", "site"))
    serials = [str(sn) for sn in (getattr(coord, "serials", set()) or set()) if sn]
    if not callable(setter):
        return
    iqevse_devices = [{"serial_number": sn, "name": f"Charger {sn}"} for sn in serials]
    grouped = {
        "envoy": {
            "type_key": "envoy",
            "type_label": "Gateway",
            "count": 1,
            "devices": [{"serial_number": f"GW-{site_id}", "name": "IQ Gateway"}],
        },
        "encharge": {
            "type_key": "encharge",
            "type_label": "Battery",
            "count": 1,
            "devices": [{"serial_number": f"BAT-{site_id}", "name": "IQ Battery"}],
        },
        "iqevse": {
            "type_key": "iqevse",
            "type_label": "EV Chargers",
            "count": len(iqevse_devices),
            "devices": iqevse_devices,
        },
    }
    if not serials:
        grouped.pop("iqevse")
    setter(grouped, list(grouped))


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup() -> None:
    """Reset socket monkeypatching before HA plugin reapplies restrictions.

    In this test environment, ``pytest_homeassistant_custom_component`` invokes
    ``pytest_socket.disable_socket()`` during setup, but pytest-socket teardown
    hooks may not be active. Ensuring sockets are re-enabled first prevents
    recursive GuardedSocket subclass wrapping across long test runs.
    """
    pytest_socket.enable_socket()


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef, request):
    """Enable custom integrations whenever the HA fixture is actually used.

    Making this an autouse fixture with a ``hass`` dependency constructed a full
    Home Assistant instance for every pure unit test in this package. Wrapping
    setup keeps the behavior for HA tests without adding ``hass`` to unrelated
    fixture closures.
    """
    outcome = yield
    if fixturedef.argname != "hass":
        return

    hass = outcome.get_result()
    try:
        from homeassistant import loader

        hass.data.pop(loader.DATA_CUSTOM_COMPONENTS, None)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def ensure_hass_stopped(stop_hass):
    """Ensure manually constructed Home Assistant instances are stopped cleanly."""
    yield


@pytest.fixture
def load_fixture() -> Callable[[str], dict[str, Any]]:
    """Return helper for loading JSON fixtures with anonymised IDs."""

    def _load(name: str) -> dict[str, Any]:
        raw = (FIXTURE_DIR / name).read_text(encoding="utf-8")
        scrubbed = raw.replace("499900000001", RANDOM_SERIAL).replace(
            "7812456", RANDOM_SITE_ID
        )
        return json.loads(scrubbed)

    return _load


@pytest.fixture
def config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Provide a fully-populated config entry added to hass."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: RANDOM_SITE_ID,
            CONF_SITE_NAME: "Garage Site",
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_REMEMBER_PASSWORD: True,
            CONF_SERIALS: [RANDOM_SERIAL],
            CONF_SCAN_INTERVAL: 15,
            CONF_COOKIE: "cookie=1",
            CONF_EAUTH: "token123",
            CONF_SESSION_ID: "session-123",
            CONF_TOKEN_EXPIRES_AT: 1_700_000_000,
            CONF_SITE_ONLY: False,
        },
        title="Garage Site",
        unique_id=RANDOM_SITE_ID,
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture(autouse=True)
def stub_coordinator_session_history(monkeypatch, request) -> None:
    """Avoid network lookups for session history unless explicitly requested."""
    from custom_components.enphase_ev import coordinator as coord_mod

    if request.node.get_closest_marker("session_history_real"):
        return

    async def _fake_sessions(self, sn: str, *, day_local=None):
        return []

    if hasattr(coord_mod.EnphaseCoordinator, "_async_fetch_sessions_today"):
        monkeypatch.setattr(
            coord_mod.EnphaseCoordinator,
            "_async_fetch_sessions_today",
            _fake_sessions,
        )
    else:
        from custom_components.enphase_ev import session_history as sh_mod

        monkeypatch.setattr(
            sh_mod.SessionHistoryManager,
            "_async_fetch_sessions_today",
            _fake_sessions,
        )


@pytest.fixture
def mock_issue_registry(monkeypatch) -> SimpleNamespace:
    """Capture coordinator issue registry calls without touching HA."""
    from homeassistant.helpers import issue_registry as issue_registry_mod

    created: list[tuple[str, str, dict[str, Any]]] = []
    deleted: list[tuple[str, str]] = []

    monkeypatch.setattr(
        issue_registry_mod,
        "async_create_issue",
        lambda hass, domain, issue_id, **kwargs: created.append(
            (domain, issue_id, kwargs)
        ),
        raising=False,
    )
    monkeypatch.setattr(
        issue_registry_mod,
        "async_delete_issue",
        lambda hass, domain, issue_id: deleted.append((domain, issue_id)),
        raising=False,
    )
    return SimpleNamespace(created=created, deleted=deleted)


@pytest.fixture(autouse=True)
def mock_clientsession(monkeypatch):
    """Stub out aiohttp client session factory used by the integration."""
    session = object()

    class StatelessSession:
        closed = False

        def detach(self) -> None:
            self.closed = True

    stateless_session = StatelessSession()
    create_calls = []
    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.async_get_clientsession",
        lambda *args, **kwargs: session,
        raising=False,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.config_flow.async_get_clientsession",
        AsyncMock(return_value=session),
        raising=False,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.options_flow.async_get_clientsession",
        AsyncMock(return_value=session),
        raising=False,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.async_create_clientsession",
        lambda *args, **kwargs: create_calls.append((args, kwargs))
        or stateless_session,
    )
    return SimpleNamespace(
        shared=session,
        stateless=stateless_session,
        create_calls=create_calls,
    )


@pytest.fixture
def coordinator_factory(hass, mock_clientsession, mock_issue_registry, monkeypatch):
    """Return a factory that builds a patched EnphaseCoordinator instance."""
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    monkeypatch.setattr(
        coord_mod,
        "async_call_later",
        lambda *_args, **_kwargs: (lambda: None),
    )

    def _factory(
        *,
        config: dict[str, Any] | None = None,
        serials: list[str] | None = None,
        client: Any | None = None,
        data: dict[str, Any] | None = None,
    ) -> EnphaseCoordinator:
        active_serials = [RANDOM_SERIAL] if serials is None else serials
        cfg = (
            config
            if config is not None
            else {
                CONF_SITE_ID: RANDOM_SITE_ID,
                CONF_SERIALS: active_serials,
                CONF_EAUTH: "EAUTH",
                CONF_COOKIE: "COOKIE",
                CONF_SCAN_INTERVAL: 15,
                CONF_SITE_ONLY: False,
            }
        )
        coord = EnphaseCoordinator(hass, cfg)
        # Most legacy coordinator tests exercise Grid Toggle behavior directly.
        # Individual opt-in tests override this to validate the production default.
        coord.serials = set(active_serials)
        coord.data = (
            data
            if data is not None
            else {sn: {"sn": sn, "name": f"Charger {sn}"} for sn in coord.serials}
        )
        _seed_default_type_buckets(coord)
        coord.last_set_amps = getattr(coord, "last_set_amps", {}) or {}
        if client is not None:
            coord.client = client
        else:
            coord.client.system_dashboard_events = AsyncMock(
                return_value={"events": []}
            )
            coord.client.system_dashboard_standing_alarms = AsyncMock(
                return_value={"alarms": []}
            )
        if not hasattr(coord.client, "storm_guard_profile"):
            coord.client.storm_guard_profile = AsyncMock(return_value={"data": {}})
        if not hasattr(coord.client, "storm_guard_alert"):
            coord.client.storm_guard_alert = AsyncMock(
                return_value={"criticalAlertActive": False, "stormAlerts": []}
            )
        if not hasattr(coord.client, "set_storm_guard"):
            coord.client.set_storm_guard = AsyncMock(return_value={"status": "ok"})
        coord.client.pv_system_today = AsyncMock(
            return_value={"stats": [{"heatpump": [0.0]}]}
        )
        if client is None:
            coord.client.battery_site_settings = AsyncMock(
                return_value={
                    "data": {"userDetails": {"isOwner": True, "isInstaller": False}}
                }
            )
        return coord

    return _factory


@pytest.fixture
def setup_integration(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_clientsession,
    mock_issue_registry,
):
    """Return helper to fully set up the integration via HA config entries."""

    async def _setup(
        *,
        client: Any | None = None,
        first_refresh_result: Any = None,
    ) -> dict[str, Any]:
        forward_calls: list[list[str]] = []
        unload_mock = AsyncMock(return_value=True)
        with ExitStack() as stack:
            if client is not None:
                stack.enter_context(
                    patch(
                        "custom_components.enphase_ev.coordinator.EnphaseEVClient",
                        return_value=client,
                    )
                )

            async def _fake_first_refresh(self):
                _seed_default_type_buckets(self)
                return first_refresh_result

            stack.enter_context(
                patch(
                    "custom_components.enphase_ev.coordinator.EnphaseCoordinator.async_config_entry_first_refresh",
                    new=_fake_first_refresh,
                )
            )
            stack.enter_context(
                patch(
                    "custom_components.enphase_ev.discovery_snapshot.DiscoverySnapshotManager.async_restore_state",
                    new=AsyncMock(return_value=None),
                )
            )
            stack.enter_context(
                patch(
                    "custom_components.enphase_ev.refresh_runner.CoordinatorRefreshRunner.async_start_startup_warmup",
                    new=AsyncMock(return_value=None),
                )
            )
            stack.enter_context(
                patch.object(
                    hass.config_entries,
                    "async_forward_entry_setups",
                    AsyncMock(
                        side_effect=lambda entry, platforms: forward_calls.append(
                            list(platforms)
                        )
                    ),
                )
            )
            stack.enter_context(
                patch.object(
                    hass.config_entries,
                    "async_unload_platforms",
                    unload_mock,
                )
            )
            assert await hass.config_entries.async_setup(config_entry.entry_id)
            await hass.async_block_till_done()
        entry_data = getattr(config_entry, "runtime_data", None)
        return {
            "entry_data": entry_data,
            "forwarded": forward_calls,
            "unload_mock": unload_mock,
        }

    return _setup
