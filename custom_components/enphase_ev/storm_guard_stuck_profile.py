"""Detect the BatteryConfig profile mismatch after a Storm Guard opt-out."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import time
from typing import TYPE_CHECKING

from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .api_client.common import runtime_read_scope
from .const import BATTERY_PROFILE_LABELS, DOMAIN, STORM_ALERT_INACTIVE_STATUSES
from .parsing_helpers import (
    coerce_optional_bool,
    coerce_optional_float,
    coerce_optional_text,
)
from .repair_issues import entry_issue_kwargs
from .runtime_helpers import coerce_optional_int

if TYPE_CHECKING:  # pragma: no cover
    from .battery_runtime import BatteryRuntime

ISSUE_STORM_GUARD_STUCK_PROFILE = "storm_guard_stuck_profile"
OBSERVATION_MAX_AGE_S = 10 * 60
PERSISTENCE_S = 30 * 60
_REQUEST_KEYS = ("requestedConfig", "requestedConfigMqtt")


@dataclass(slots=True)
class _Read[T]:
    value: T
    started: float = -float("inf")
    unchanged_since: float = -float("inf")
    expires: float = -float("inf")
    revision: int = 0

    def record(
        self, value: T, started: float, *, fresh_until: float = float("inf")
    ) -> None:
        # Forced reads can overlap normal polls. Ignore older requests.
        if started < self.started:
            return
        if value != self.value or not self.is_recent(started):
            self.unchanged_since = started
        self.started = started
        self.value = value
        self.revision += 1
        self.expires = min(started + OBSERVATION_MAX_AGE_S, fresh_until)

    def is_recent(self, now: float) -> bool:
        return now <= self.expires


@dataclass(frozen=True, slots=True)
class _Profile:
    mode: str
    matches: bool
    idle: bool
    busy: bool


def _profile_observation(payload: object) -> _Profile | None:
    """Classify a single response without borrowing retained profile fields."""
    if not isinstance(payload, dict):
        return None
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return None
    busy = coerce_optional_bool(data.get("systemTask"))
    profile = data.get("profile")
    if (
        busy is None
        or not isinstance(profile, str)
        or profile not in BATTERY_PROFILE_LABELS
    ):
        return None
    for key in ("isBatteryChangePending", "drEventActive"):
        active = coerce_optional_bool(data.get(key, False))
        if active is None:
            return None
        if active:
            return _Profile(profile, matches=False, idle=False, busy=busy)

    for key in _REQUEST_KEYS:
        requests = data.get(key, {})
        if not isinstance(requests, dict):
            return None
        if requests:
            return _Profile(profile, matches=False, idle=False, busy=busy)

    status = data.get("siteProfileStatus")
    if not busy:
        # Normal profile details can omit siteProfileStatus.
        idle = "siteProfileStatus" not in data or status == "READY"
        return _Profile(profile, matches=False, idle=idle, busy=False)
    if profile != "backup_only" or status == "READY":
        return _Profile(profile, matches=False, idle=False, busy=busy)
    if status != "PROFILE_SWITCH_PENDING" or not all(
        key in data for key in _REQUEST_KEYS
    ):
        return None
    return _Profile(profile, matches=True, idle=False, busy=True)


class StormGuardStuckProfile:
    """Report a sustained post-opt-out mismatch without changing write state."""

    def __init__(self, runtime: BatteryRuntime) -> None:
        self.runtime = runtime
        self._profile = _Read[_Profile | None](None)
        self._live = _Read[str | None](None)
        self._live_samples: dict[str, tuple[float, str]] = {}
        self._settings_pending = _Read[bool | None](None)
        self._opt_out = _Read[dict[str, float]]({})
        self._eligible_since: float | None = None
        self._reported = False
        self._check_lock = asyncio.Lock()

    def note_profile(self, payload: object, started: float) -> bool | None:
        """Record Profile and return its busy flag when supported by the response."""
        observation = _profile_observation(payload)
        self._profile.record(observation, started)
        if observation is None or not (observation.busy or observation.idle):
            return None
        return observation.busy

    def note_settings(self, payload: object, started: float) -> None:
        """Record a sibling BatteryConfig endpoint's pending-work signal."""
        if not isinstance(payload, dict):
            return
        data = payload.get("data", payload)
        if not isinstance(data, dict):
            return
        pending = coerce_optional_bool(data.get("isBatteryChangePending"))
        if any(
            isinstance(requests := data.get(key), dict) and requests
            for key in _REQUEST_KEYS
        ):
            pending = True
        if pending is None or started < self._settings_pending.started:
            return
        self._settings_pending.record(pending, started)
        if pending:
            self._eligible_since = None

    def note_live(self, payload: object, started: float) -> None:
        mismatch = self.runtime.profile_mismatch
        if not isinstance(payload, dict) or not isinstance(
            storages := payload.get("storages"), list
        ):
            self._live.record(None, started)
            return
        now_utc = dt_util.utcnow().timestamp()
        oldest_report = now_utc
        recent_reports = {
            key: sample
            for key, sample in self._live_samples.items()
            if now_utc - sample[0] <= OBSERVATION_MAX_AGE_S
        }
        seen_keys: set[str] = set()
        inconsistent = False
        excluded_count = 0
        for storage in storages:
            # An HTTP response may still contain cached battery telemetry.
            if not isinstance(storage, dict):
                inconsistent = True
                continue
            if coerce_optional_bool(storage.get("excluded")) is True:
                excluded_count += 1
            reported = coerce_optional_float(storage.get("last_report"))
            if reported is None or not 0 <= now_utc - reported <= OBSERVATION_MAX_AGE_S:
                inconsistent = True
                continue
            oldest_report = min(oldest_report, reported)
            key = self.runtime._battery_storage_key(storage)
            if key is not None:
                if key in seen_keys:
                    inconsistent = True
                seen_keys.add(key)
                previous = recent_reports.get(key)
                mode = (
                    coerce_optional_text(storage.get("battery_mode")) or ""
                ).casefold()
                if previous is None or reported > previous[0]:
                    recent_reports[key] = (reported, mode)
                elif reported < previous[0]:
                    inconsistent = True
                elif mode != previous[1]:
                    # Conflicting modes at the same timestamp need a newer sample.
                    recent_reports[key] = (reported, "")
                    inconsistent = True
        # Partial responses cannot confirm recovery, but keep each usable
        # sample so a later replay cannot hide a known newer battery report.
        self._live_samples = recent_reports
        for key, actual_count in (
            ("included_count", len(storages) - excluded_count),
            ("excluded_count", excluded_count),
        ):
            count = coerce_optional_int(payload.get(key))
            if count is not None and count != actual_count:
                inconsistent = True
        if inconsistent or mismatch.live not in BATTERY_PROFILE_LABELS:
            self._live.record(None, started)
            return
        self._live.record(
            mismatch.live,
            started,
            fresh_until=time.monotonic()
            + OBSERVATION_MAX_AGE_S
            - (now_utc - oldest_report),
        )

    def note_alert(self, payload: object, started: float) -> None:
        self._opt_out.record(self._current_opt_outs(payload), started)

    def _current_opt_outs(self, payload: object) -> dict[str, float]:
        if (
            not isinstance(payload, dict)
            or coerce_optional_bool(payload.get("criticalAlertActive")) is not False
            or not isinstance(alerts := payload.get("stormAlerts"), list)
        ):
            return {}
        opted_out: dict[str, float] = {}
        for alert in alerts:
            if not isinstance(alert, dict) or not isinstance(
                status := alert.get("status"), str
            ):
                return {}
            if status.strip().lower().replace(
                "_", "-"
            ) not in STORM_ALERT_INACTIVE_STATUSES or (
                "active" in alert and coerce_optional_bool(alert["active"]) is not False
            ):
                return {}
            if self.runtime.storm_alert_can_opt_in(alert):
                alert_id = alert.get("id")
                ends = coerce_optional_float(alert.get("endTime"))
                if isinstance(alert_id, str) and alert_id.strip() and ends is not None:
                    opted_out[alert_id] = ends
        return opted_out

    def _has_current_opt_out(self, now: float) -> bool:
        return (
            self._opt_out.is_recent(now)
            and max(self._opt_out.value.values(), default=0)
            > dt_util.utcnow().timestamp() * 1000
        )

    def _pending_work(self, now: float) -> bool:
        coord = self.runtime.coordinator
        control_updates = coord.control_updates
        return (
            (
                self._settings_pending.is_recent(now)
                and self._settings_pending.value is True
            )
            or coord.battery_profile_pending
            or coord.battery_settings_write_pending
            or self.runtime.ev_preferences.update_in_progress
            or control_updates.pending("storm_alert_opt_in")
            or control_updates.pending("storm_alert_opt_out")
        )

    def _sources_agree_on_idle(self, now: float) -> bool:
        profile = self._profile.value
        return (
            profile is not None
            and profile.idle
            and self._profile.is_recent(now)
            and self._live.is_recent(now)
            and self._live.value == profile.mode
            and self._live.started >= self._profile.unchanged_since
            and not self.runtime.settings_read_in_flight
            and not self._pending_work(now)
        )

    def _matches_reported_mismatch(self, now: float) -> bool:
        coord = self.runtime.coordinator
        profile = self._profile.value
        return (
            profile is not None
            and profile.matches
            and self._profile.is_recent(now)
            and self._live.is_recent(now)
            and self._live.value == "self-consumption"
            and coord.battery_system_task is True
            and coord.storm_alert_active is not True
            and not self._pending_work(now)
        )

    async def async_check(self) -> bool:
        """Read both battery sources once before reconciling the warning."""
        if self._check_lock.locked():
            return False
        coord = self.runtime.coordinator
        async with self._check_lock, self.runtime.repair_profile_context():
            if not coord.endpoint_family_can_refresh_readback("battery_status"):
                return False
            applied_revisions = []
            for refresh, observation in (
                (
                    self.runtime.async_refresh_storm_guard_profile,
                    self._profile,
                ),
                (
                    self.runtime.async_refresh_battery_status,
                    self._live,
                ),
            ):
                revision = observation.revision
                try:
                    with runtime_read_scope(lambda: coord.runtime_active):
                        refreshed = await refresh(repair=True)
                except asyncio.CancelledError:
                    if coord.runtime_active:
                        raise
                    return False
                if (
                    refreshed is not True
                    or observation.revision == revision
                    or observation.value is None
                ):
                    return False
                applied_revisions.append(observation.revision)
            now = time.monotonic()
            if (
                applied_revisions != [self._profile.revision, self._live.revision]
                or not self._profile.is_recent(now)
                or not self._live.is_recent(now)
            ):
                return False
            if self._sources_agree_on_idle(now):
                if not self.runtime.confirm_repair_profile_idle():
                    return False
                confirmed = True
            else:
                confirmed = self._matches_reported_mismatch(now)
        self.sync()
        return confirmed

    def sync(self) -> None:
        """Reconcile the issue after the coordinator refreshes existing sources."""
        coord = self.runtime.coordinator
        entry = coord.config_entry
        entry_id = getattr(entry, "entry_id", None)
        if not coord.runtime_active or entry_id is None or self._check_lock.locked():
            return
        now = time.monotonic()
        issue_id = f"{ISSUE_STORM_GUARD_STUCK_PROFILE}_{entry_id}"
        if (
            coord.battery_system_task is False
            and self._sources_agree_on_idle(now)
            and self.runtime.settings_allow_idle_profile(self._profile.started)
        ):
            ir.async_delete_issue(coord.hass, DOMAIN, issue_id)
            self._reported = False
        if (
            not self._matches_reported_mismatch(now)
            or not self._has_current_opt_out(now)
            or min(self._profile.started, self._live.started)
            < self._opt_out.unchanged_since
        ):
            # A failed or inconclusive read breaks the dwell window. Keep an
            # existing warning until both battery sources confirm recovery.
            self._eligible_since = None
            return
        if self._eligible_since is None:
            self._eligible_since = now
        since = max(
            self._eligible_since,
            self._profile.unchanged_since,
            self._live.unchanged_since,
            self._opt_out.unchanged_since,
        )
        if now - since < PERSISTENCE_S or self._reported:
            return
        ir.async_create_issue(
            coord.hass,
            DOMAIN,
            issue_id,
            **entry_issue_kwargs(entry),
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_STORM_GUARD_STUCK_PROFILE,
            translation_placeholders={"site_id": coord.site_id},
            data={"entry_id": entry_id},
        )
        self._reported = True

    def diagnostics(self) -> dict[str, object]:
        """Describe the evidence without including advisory details or raw data."""
        now = time.monotonic()
        profile = self._profile.value
        live = self._live.value
        return {
            "profile_matches": (
                profile.matches
                if profile is not None and self._profile.is_recent(now)
                else None
            ),
            "live_self_consumption": (
                live == "self-consumption"
                if live is not None and self._live.is_recent(now)
                else None
            ),
            "fresh_opt_out": self._has_current_opt_out(now),
            "repair_reported": self._reported,
        }
