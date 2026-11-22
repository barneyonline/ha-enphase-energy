"""Entry ownership, refresh cancellation and legacy repair migration."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from homeassistant.helpers import issue_registry as ir
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant.common import MockConfigEntry

from custom_components.enphase_ev import async_remove_entry
from custom_components.enphase_ev import repair_issues
from custom_components.enphase_ev.const import (
    DOMAIN,
    ISSUE_AUTH_BLOCKED,
    ISSUE_CLOUD_ERRORS,
    ISSUE_REAUTH_REQUIRED,
    ISSUE_TOO_MANY_ACTIVE_SESSIONS,
    OPT_SYSTEM_EVENT_REPAIR_ISSUES,
)
from custom_components.enphase_ev.coordinator import EnphaseCoordinator
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from custom_components.enphase_ev.services import async_setup_services
from custom_components.enphase_ev.system_events import SystemEventsRuntime

from .test_system_events import _runtime_coordinator, _event_payload


def create_issue(hass, issue_id, **kwargs):
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="active_system_event",
        **kwargs,
    )


async def test_scoped_diagnostics_preserve_ignore_and_clearing_targets_entry(
    hass, config_entry
):
    coord = EnphaseCoordinator(hass, dict(config_entry.data), config_entry=config_entry)
    entry = config_entry
    entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    entry._async_set_state(hass, ConfigEntryState.LOADED, None)
    create_issue(
        hass, ISSUE_CLOUD_ERRORS, data={"site_metrics": {"site_id": coord.site_id}}
    )
    ir.async_ignore_issue(hass, DOMAIN, ISSUE_CLOUD_ERRORS, True)
    coord.diagnostics.report_cloud_issue()
    registry = ir.async_get(hass)
    issue_id = coord.diagnostics._repair_issue_id(ISSUE_CLOUD_ERRORS)
    issue = registry.async_get_issue(DOMAIN, issue_id)
    assert issue.config_entry_id == entry.entry_id
    assert issue.dismissed_version is not None
    assert registry.async_get_issue(DOMAIN, ISSUE_CLOUD_ERRORS) is None
    assert coord.diagnostics._repair_issue_id("unrelated") == "unrelated"
    other = MockConfigEntry(domain=DOMAIN, data={"site_id": "another"})
    other.add_to_hass(hass)
    create_issue(hass, "foreign", config_entry_id=other.entry_id)
    coord.diagnostics._delete_issue("foreign")
    await async_remove_entry(hass, entry)
    assert registry.async_get_issue(DOMAIN, "foreign") is not None
    coord.diagnostics.create_reauth_issue()
    reauth_id = coord.diagnostics._repair_issue_id(ISSUE_REAUTH_REQUIRED)
    create_issue(hass, "other_reauth", config_entry_id=other.entry_id)
    async_setup_services(hass)
    await hass.services.async_call(
        DOMAIN, "clear_reauth_issue", {"site_id": "unrelated"}, blocking=True
    )
    assert registry.async_get_issue(DOMAIN, reauth_id) is not None
    await hass.services.async_call(
        DOMAIN, "clear_reauth_issue", {"site_id": coord.site_id}, blocking=True
    )
    assert registry.async_get_issue(DOMAIN, reauth_id) is None
    assert registry.async_get_issue(DOMAIN, "other_reauth") is not None
    entry._async_set_state(hass, ConfigEntryState.NOT_LOADED, None)


async def test_alarm_ownership_updates_unowned_checkpoint(hass, config_entry):
    hass.config_entries.async_update_entry(
        config_entry, options={OPT_SYSTEM_EVENT_REPAIR_ISSUES: True}
    )
    coord = _runtime_coordinator(hass, payload=_event_payload())
    coord.config_entry = config_entry
    runtime = SystemEventsRuntime(coord)
    await runtime.async_refresh()
    registry = ir.async_get(hass)
    issue_id = next(iter(runtime._reported_issue_ids))
    issue = registry.async_get_issue(DOMAIN, issue_id)
    create_issue(hass, issue_id, data=issue.data)
    ir.async_ignore_issue(hass, DOMAIN, issue_id, True)
    await runtime.async_refresh()
    issue = registry.async_get_issue(DOMAIN, issue_id)
    assert issue.config_entry_id == config_entry.entry_id
    assert issue.dismissed_version is not None
    await runtime.async_refresh()
    assert registry.async_get_issue(DOMAIN, issue_id).dismissed_version is not None


@pytest.mark.parametrize(
    "state",
    [
        ConfigEntryState.NOT_LOADED,
        ConfigEntryState.SETUP_RETRY,
        ConfigEntryState.SETUP_ERROR,
    ],
)
@pytest.mark.parametrize(
    "kind", [ISSUE_REAUTH_REQUIRED, ISSUE_AUTH_BLOCKED, ISSUE_TOO_MANY_ACTIVE_SESSIONS]
)
async def test_clear_owned_auth_repairs_without_loaded_runtime(
    hass, config_entry, state, kind
):
    coord = EnphaseCoordinator(hass, dict(config_entry.data), config_entry=config_entry)
    if kind == ISSUE_REAUTH_REQUIRED:
        coord.diagnostics.create_reauth_issue()
    else:
        coord._auth_block_reason = (
            "too_many_active_sessions"
            if kind == ISSUE_TOO_MANY_ACTIVE_SESSIONS
            else None
        )
        coord.diagnostics.create_auth_block_issue()
    issue_id = coord.diagnostics._repair_issue_id(kind)
    coord.diagnostics.report_cloud_issue()
    cloud_id = coord.diagnostics._repair_issue_id(ISSUE_CLOUD_ERRORS)
    coord.mark_runtime_stopped()
    config_entry.runtime_data = None
    config_entry._async_set_state(hass, state, None)
    other = MockConfigEntry(domain=DOMAIN, data={"site_id": "another"})
    other.add_to_hass(hass)
    ir.async_create_issue(
        hass,
        DOMAIN,
        "other_auth",
        config_entry_id=other.entry_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=kind,
    )
    ir.async_create_issue(
        hass,
        "other",
        "foreign_domain",
        config_entry_id=config_entry.entry_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=kind,
    )
    registry = ir.async_get(hass)
    assert (
        registry.async_get_issue(DOMAIN, issue_id).config_entry_id
        == config_entry.entry_id
    )
    async_setup_services(hass)
    await hass.services.async_call(
        DOMAIN, "clear_reauth_issue", {"site_id": coord.site_id}, blocking=True
    )
    assert registry.async_get_issue(DOMAIN, issue_id) is None
    assert registry.async_get_issue(DOMAIN, cloud_id) is not None
    assert registry.async_get_issue(DOMAIN, "other_auth") is not None
    assert registry.async_get_issue("other", "foreign_domain") is not None
    config_entry._async_set_state(hass, ConfigEntryState.NOT_LOADED, None)


async def test_clear_auth_repairs_targets_owned_and_legacy_issues(hass, config_entry):
    site_id = str(config_entry.data["site_id"])
    other = MockConfigEntry(domain=DOMAIN, data={"site_id": "another"})
    other.add_to_hass(hass)
    owned = {}
    for entry in (config_entry, other):
        coord = EnphaseCoordinator(hass, dict(entry.data), config_entry=entry)
        coord.diagnostics.create_reauth_issue()
        owned[entry.entry_id] = coord.diagnostics._repair_issue_id(
            ISSUE_REAUTH_REQUIRED
        )
    create_issue(hass, "unrelated", config_entry_id=config_entry.entry_id)
    create_issue(hass, "unrelated_legacy")
    create_issue(
        hass, ISSUE_REAUTH_REQUIRED, data={"site_metrics": {"site_id": "another"}}
    )
    create_issue(hass, ISSUE_AUTH_BLOCKED, data={"site_metrics": {"site_id": site_id}})
    ir.async_ignore_issue(hass, DOMAIN, ISSUE_REAUTH_REQUIRED, True)
    registry = ir.async_get(hass)
    legacy = registry.async_get_issue(DOMAIN, ISSUE_REAUTH_REQUIRED)
    async_setup_services(hass)

    await hass.services.async_call(
        DOMAIN, "clear_reauth_issue", target={"entity_id": "none"}, blocking=True
    )
    assert all(registry.async_get_issue(DOMAIN, name) for name in owned.values())
    assert registry.async_get_issue(DOMAIN, ISSUE_REAUTH_REQUIRED) is legacy

    await hass.services.async_call(
        DOMAIN, "clear_reauth_issue", {"site_id": site_id}, blocking=True
    )
    assert registry.async_get_issue(DOMAIN, owned[config_entry.entry_id]) is None
    assert registry.async_get_issue(DOMAIN, ISSUE_AUTH_BLOCKED) is None
    assert registry.async_get_issue(DOMAIN, owned[other.entry_id]) is not None
    assert registry.async_get_issue(DOMAIN, ISSUE_REAUTH_REQUIRED) is legacy

    await hass.services.async_call(DOMAIN, "clear_reauth_issue", blocking=True)
    assert registry.async_get_issue(DOMAIN, owned[other.entry_id]) is None
    assert registry.async_get_issue(DOMAIN, ISSUE_REAUTH_REQUIRED) is None
    assert registry.async_get_issue(DOMAIN, "unrelated") is not None
    assert registry.async_get_issue(DOMAIN, "unrelated_legacy") is not None


@pytest.mark.parametrize("failed", [False, True])
async def test_refresh_keeps_ignored_and_isolates_stale_set(hass, failed):
    for name in ("current", "stale", "unrelated"):
        create_issue(hass, name)
    ir.async_ignore_issue(hass, DOMAIN, "current", True)
    registry = ir.async_get(hass)
    ignored = registry.async_get_issue(DOMAIN, "current").dismissed_version
    try:
        with repair_issues.async_refresh_issue_set(
            hass, {"current", "stale"}, {"current"}
        ):
            if failed:
                raise ValueError("incomplete check")
    except ValueError:
        pass
    assert (registry.async_get_issue(DOMAIN, "stale") is not None) == failed
    assert registry.async_get_issue(DOMAIN, "unrelated") is not None
    assert registry.async_get_issue(DOMAIN, "current").dismissed_version == ignored


async def test_entry_removal_clears_legacy_repairs_only_for_that_entry(
    hass, config_entry
):
    raw = config_entry.entry_id
    scoped = raw.lower()
    ids = [
        f"battery_profile_mismatch_{raw}",
        f"cloud_service_unavailable_{scoped}",
        f"active_system_event_{scoped}_abc",
    ]
    for name in [*ids, "export_limit_pending_other"]:
        create_issue(hass, name)
    create_issue(
        hass,
        ISSUE_REAUTH_REQUIRED,
        data={"site_metrics": {"site_id": config_entry.data["site_id"]}},
    )
    create_issue(
        hass, ISSUE_CLOUD_ERRORS, data={"site_metrics": {"site_id": "another"}}
    )
    create_issue(hass, "no_context", data={"site_metrics": None})
    ir.async_create_issue(
        hass,
        "other",
        ids[0],
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="unrelated",
    )
    await async_remove_entry(hass, config_entry)
    registry = ir.async_get(hass)
    assert all(registry.async_get_issue(DOMAIN, name) is None for name in ids)
    assert registry.async_get_issue(DOMAIN, "export_limit_pending_other") is not None
    assert registry.async_get_issue("other", ids[0]) is not None
    assert registry.async_get_issue(DOMAIN, ISSUE_REAUTH_REQUIRED) is None
    assert registry.async_get_issue(DOMAIN, ISSUE_CLOUD_ERRORS) is not None
    assert registry.async_get_issue(DOMAIN, "no_context") is not None


async def test_removal_without_site_context_keeps_unscoped_legacy_issue(hass):
    create_issue(hass, ISSUE_CLOUD_ERRORS, data={"site_metrics": {"site_id": None}})
    await async_remove_entry(hass, SimpleNamespace(entry_id="entry", data={}))
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_CLOUD_ERRORS) is not None


async def test_native_diagnostic_ownership_migration_and_entry_removal(
    hass, config_entry
):
    coord = EnphaseCoordinator(hass, dict(config_entry.data), config_entry=config_entry)
    create_issue(
        hass, ISSUE_CLOUD_ERRORS, data={"site_metrics": {"site_id": coord.site_id}}
    )
    ir.async_ignore_issue(hass, DOMAIN, ISSUE_CLOUD_ERRORS, True)
    diagnostics = coord.diagnostics
    diagnostics.report_cloud_issue()
    issue_id = diagnostics._repair_issue_id(ISSUE_CLOUD_ERRORS)
    registry = ir.async_get(hass)
    issue = registry.async_get_issue(DOMAIN, issue_id)
    assert issue.config_entry_id == config_entry.entry_id
    assert issue.dismissed_version is not None
    assert registry.async_get_issue(DOMAIN, ISSUE_CLOUD_ERRORS) is None
    diagnostics.create_reauth_issue()
    reauth_id = diagnostics._repair_issue_id(ISSUE_REAUTH_REQUIRED)
    assert (
        registry.async_get_issue(DOMAIN, reauth_id).config_entry_id
        == config_entry.entry_id
    )
    diagnostics.clear_reauth_issue()
    assert registry.async_get_issue(DOMAIN, reauth_id) is None
    other = MockConfigEntry(domain=DOMAIN, data={"site_id": "another"})
    other.add_to_hass(hass)
    create_issue(hass, "unrelated", config_entry_id=other.entry_id)
    assert await hass.config_entries.async_remove(config_entry.entry_id)
    assert registry.async_get_issue(DOMAIN, issue_id) is None
    assert registry.async_get_issue(DOMAIN, "unrelated") is not None


async def test_native_alarm_refresh_migrates_checkpoint_preserves_ignore_and_cancels(
    hass, config_entry, monkeypatch
):
    hass.config_entries.async_update_entry(
        config_entry, options={OPT_SYSTEM_EVENT_REPAIR_ISSUES: True}
    )
    coord = _runtime_coordinator(hass, payload=_event_payload())
    coord.config_entry = config_entry
    runtime = SystemEventsRuntime(coord)
    await runtime.async_refresh()
    registry = ir.async_get(hass)
    issue_id = next(iter(runtime._reported_issue_ids))
    issue = registry.async_get_issue(DOMAIN, issue_id)
    assert issue.config_entry_id == config_entry.entry_id
    ir.async_ignore_issue(hass, DOMAIN, issue_id, True)
    ignored = registry.async_get_issue(DOMAIN, issue_id).dismissed_version
    await runtime.async_refresh()
    assert registry.async_get_issue(DOMAIN, issue_id).dismissed_version == ignored
    # Upgrade an unowned checkpoint without waiting for the checkpoint interval.
    create_issue(hass, issue_id, data=issue.data)
    assert registry.async_get_issue(DOMAIN, issue_id).config_entry_id is None
    await runtime.async_refresh()
    assert (
        registry.async_get_issue(DOMAIN, issue_id).config_entry_id
        == config_entry.entry_id
    )
    coord.client.system_dashboard_standing_alarms.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        await runtime.async_refresh()
    assert registry.async_get_issue(DOMAIN, issue_id).dismissed_version == ignored
    # Native refresh cancellation must also leave a not-yet-checked issue alone.
    with pytest.raises(ValueError), repair_issues.async_refresh_issue_set(
        hass, {issue_id}, set()
    ):
        raise ValueError("check interrupted")
    assert registry.async_get_issue(DOMAIN, issue_id) is not None
    # A new runtime restores the persisted last-seen checkpoint. An incomplete
    # snapshot cannot expire it; a complete check past the grace period can.
    coord.client.system_dashboard_standing_alarms.side_effect = None
    coord.client.system_dashboard_standing_alarms.return_value = {
        "alarms": [],
        "_enphase_ev_truncated": True,
    }
    observed = runtime._last_success_utc + timedelta(hours=7)
    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.dt_util.utcnow", lambda: observed
    )
    replacement = SystemEventsRuntime(coord)
    await replacement.async_refresh()
    assert registry.async_get_issue(DOMAIN, issue_id) is not None
    coord.client.system_dashboard_standing_alarms.return_value = {"alarms": []}
    await replacement.async_refresh()
    assert registry.async_get_issue(DOMAIN, issue_id) is None


@pytest.mark.parametrize(
    "legacy_data",
    [None, {}, {"site_metrics": {}}, {"site_metrics": {"site_id": "another"}}],
)
async def test_legacy_issue_dismissal_does_not_leak_between_sites(
    hass, config_entry, legacy_data
):
    coord = EnphaseCoordinator(hass, dict(config_entry.data), config_entry=config_entry)
    create_issue(hass, ISSUE_CLOUD_ERRORS, data=legacy_data)
    ir.async_ignore_issue(hass, DOMAIN, ISSUE_CLOUD_ERRORS, True)
    registry = ir.async_get(hass)
    legacy = registry.async_get_issue(DOMAIN, ISSUE_CLOUD_ERRORS)

    assert not coord.diagnostics._reported_issue_is_active(ISSUE_CLOUD_ERRORS)
    coord.diagnostics.report_cloud_issue()

    own_id = coord.diagnostics._repair_issue_id(ISSUE_CLOUD_ERRORS)
    assert registry.async_get_issue(DOMAIN, own_id).dismissed_version is None
    assert registry.async_get_issue(DOMAIN, ISSUE_CLOUD_ERRORS) is legacy
    coord.diagnostics._delete_issue(ISSUE_CLOUD_ERRORS)
    assert registry.async_get_issue(DOMAIN, own_id) is None
    assert registry.async_get_issue(DOMAIN, ISSUE_CLOUD_ERRORS) is legacy
