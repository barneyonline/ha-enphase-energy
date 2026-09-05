"""Refresh Enlighten tokens from stored credentials with cooldown safeguards."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone as _tz

from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .api import (
    EnlightenAuthInvalidCredentials,
    EnlightenAuthMFARequired,
    EnlightenAuthTooManySessions,
    EnlightenAuthUnavailable,
    async_authenticate,
)
from .auth_refresh_protocol import AuthRefreshHost
from .auth_refresh_state import AuthRefreshSnapshot, AuthRefreshState
from .const import (
    AUTH_BLOCKED_COOLDOWN_S,
    AUTH_REFRESH_MANUAL_RETRY_COOLDOWN_S,
    AUTH_REFRESH_REJECTED_COOLDOWN_S,
    AUTH_REFRESH_REJECTED_SUSPEND_THRESHOLD,
    AUTH_REFRESH_SUSPENDED_COOLDOWN_S,
    AUTH_REFRESH_SUCCESS_REUSE_WINDOW_S,
)
from .log_redaction import redact_text

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ManualAuthRefreshResult:
    """Result of a user-requested stored-credential auth refresh."""

    success: bool
    reason: str | None = None
    retry_after_seconds: int | None = None
    performed_refresh: bool = False


class AuthRefreshRuntime:
    """Stored-credential Enlighten token refresh with coalescing and cooldown."""

    def __init__(self, coordinator: AuthRefreshHost) -> None:
        self.coordinator = coordinator
        self.state = AuthRefreshState()
        # Preserve supported early construction/legacy field writes while making
        # the runtime the sole state owner after attachment.
        for state_field in fields(self.state):
            if state_field.name in coordinator.__dict__:
                setattr(
                    self.state,
                    state_field.name,
                    coordinator.__dict__.pop(state_field.name),
                )
        coordinator.__dict__["auth_state"] = self.state
        self._task_lock = asyncio.Lock()

    @property
    def snapshot(self) -> AuthRefreshSnapshot:
        """Return recovery metadata used by aggregate publication equality."""

        state = self.state
        return AuthRefreshSnapshot(
            rejected_count=state._auth_refresh_rejected_count,
            failure_reason=state._auth_refresh_last_failure_reason,
            blocked_until=state._auth_blocked_until_utc,
            block_reason=state._auth_block_reason,
            suspended_until=state._auth_refresh_suspended_until_utc,
        )

    async def attempt_auto_refresh(self) -> bool:
        """Attempt to refresh authentication using stored credentials."""

        coord = self.coordinator
        if not self.stored_credentials_available():
            return False

        if coord._auth_block_active():
            return False

        if coord._auth_refresh_suspended_active():
            return False

        if self.auth_refresh_recent_success_active():
            return True

        if self.auth_refresh_rejected_active():
            return False

        task = self.state._auth_refresh_task
        if task is not None and not task.done():
            # Concurrent 401 handlers share the same refresh attempt so Enphase
            # does not see a burst of password logins.
            return await asyncio.shield(task)

        async with self._task_lock:
            if coord._auth_refresh_suspended_active():
                return False

            if self.auth_refresh_rejected_active():
                return False

            if self.auth_refresh_recent_success_active():
                return True

            task = self.state._auth_refresh_task
            if task is None or task.done():
                task = asyncio.create_task(
                    self.async_run_auto_refresh(),
                    name="enphase_ev_auth_refresh",
                )
                self.state._auth_refresh_task = task
                task.add_done_callback(self.clear_auth_refresh_task)

        return await asyncio.shield(task)

    def stored_credentials_available(self) -> bool:
        """Return True when stored credentials can be used for reauthentication."""

        coord = self.coordinator
        return bool(
            coord._email and coord._remember_password and coord._stored_password
        )

    async def attempt_manual_refresh(self) -> ManualAuthRefreshResult:
        """Run one user-requested stored-credential refresh attempt.

        Manual retries intentionally bypass automatic auth-block and rejection
        cooldown checks, but still require stored credentials and share any
        in-flight auth task.
        """

        if not self.stored_credentials_available():
            return ManualAuthRefreshResult(
                success=False, reason="stored_credentials_unavailable"
            )

        retry_after = self.manual_refresh_retry_after_seconds()
        if retry_after is not None:
            return ManualAuthRefreshResult(
                success=False,
                reason="manual_retry_cooldown_active",
                retry_after_seconds=retry_after,
            )

        if self._recent_manual_success_reusable():
            return self._reuse_recent_manual_success()

        task = self.state._auth_refresh_task
        if task is not None and not task.done():
            return await self._await_manual_refresh_task(task)

        async with self._task_lock:
            retry_after = self.manual_refresh_retry_after_seconds()
            if retry_after is not None:
                return ManualAuthRefreshResult(
                    success=False,
                    reason="manual_retry_cooldown_active",
                    retry_after_seconds=retry_after,
                )

            if self._recent_manual_success_reusable():
                return self._reuse_recent_manual_success()

            task = self.state._auth_refresh_task
            if task is None or task.done():
                task = asyncio.create_task(
                    self.async_run_auto_refresh(),
                    name="enphase_ev_auth_refresh",
                )
                self.state._auth_refresh_task = task
                task.add_done_callback(self.clear_auth_refresh_task)

        return await self._await_manual_refresh_task(task)

    def _recent_manual_success_reusable(self) -> bool:
        """Return True when recent success can satisfy a manual retry safely."""

        coord = self.coordinator
        if not self.auth_refresh_recent_success_active():
            return False
        return not (
            coord._auth_block_active()
            or coord._auth_refresh_suspended_active()
            or coord._hems_auth_circuit_active()
        )

    def _reuse_recent_manual_success(self) -> ManualAuthRefreshResult:
        """Apply manual-success cleanup when a recent refresh is still valid."""

        coord = self.coordinator
        self.state._auth_refresh_manual_retry_until = None
        coord._clear_auth_block(persist=True)
        coord._clear_auth_refresh_suspension(persist=True)
        coord._clear_hems_auth_circuit(persist=True, reset_failure_count=True)
        return ManualAuthRefreshResult(success=True, performed_refresh=False)

    def manual_refresh_retry_active(self) -> bool:
        """Return True while a failed manual retry is cooling down."""

        return self.manual_refresh_retry_after_seconds() is not None

    def manual_refresh_retry_after_seconds(self) -> int | None:
        """Return remaining seconds for a failed manual retry cooldown."""

        cooldown_until = self.state._auth_refresh_manual_retry_until
        if not isinstance(cooldown_until, (int, float)):
            return None
        remaining = float(cooldown_until) - time.monotonic()
        if remaining > 0:
            return max(1, math.ceil(remaining))
        self.state._auth_refresh_manual_retry_until = None
        return None

    async def _await_manual_refresh_task(
        self, task: asyncio.Task[bool]
    ) -> ManualAuthRefreshResult:
        """Await a manual refresh task and throttle only failed manual attempts."""

        result = await asyncio.shield(task)
        if result:
            self.state._auth_refresh_manual_retry_until = None
            return ManualAuthRefreshResult(success=True, performed_refresh=True)
        else:
            self.state._auth_refresh_manual_retry_until = (
                time.monotonic() + AUTH_REFRESH_MANUAL_RETRY_COOLDOWN_S
            )
            return ManualAuthRefreshResult(success=False, reason="reauth_failed")

    def clear_auth_refresh_task(self, task: asyncio.Task[bool]) -> None:
        """Clear the shared auth-refresh task once it completes."""

        if self.state._auth_refresh_task is task:
            self.state._auth_refresh_task = None

    def auth_refresh_rejected_active(self) -> bool:
        """Return True while stored-credential refresh is in cooldown."""

        cooldown_until = self.state._auth_refresh_rejected_until
        if not isinstance(cooldown_until, (int, float)):
            return False
        if time.monotonic() < float(cooldown_until):
            return True
        self.state._auth_refresh_rejected_until = None
        self.state._auth_refresh_rejected_ends_utc = None
        return False

    def note_auth_refresh_rejected(self, message: str) -> None:
        """Start a cooldown after stored credentials are rejected."""

        coord = self.coordinator
        self.state._auth_refresh_rejected_count = (
            int(self.state._auth_refresh_rejected_count) + 1
        )
        delay = float(AUTH_REFRESH_REJECTED_COOLDOWN_S)
        self.state._auth_refresh_last_success_mono = None
        if self.state._auth_refresh_rejected_count >= int(
            AUTH_REFRESH_REJECTED_SUSPEND_THRESHOLD
        ):
            # Repeated credential rejections are treated as durable auth
            # failures and suspended longer than transient service errors.
            self.state._auth_refresh_rejected_until = None
            self.state._auth_refresh_rejected_ends_utc = None
            try:
                suspended_until = dt_util.utcnow() + timedelta(
                    seconds=AUTH_REFRESH_SUSPENDED_COOLDOWN_S
                )
            except Exception:
                suspended_until = datetime.now(_tz.utc) + timedelta(
                    seconds=AUTH_REFRESH_SUSPENDED_COOLDOWN_S
                )
            coord._note_auth_refresh_suspended(suspended_until=suspended_until)
            _LOGGER.warning(
                "Stored-credential automatic reauthentication has been suspended after %s consecutive rejections; reauthenticate via the integration options",
                self.state._auth_refresh_rejected_count,
            )
            return
        self.state._auth_refresh_rejected_until = time.monotonic() + delay
        try:
            self.state._auth_refresh_rejected_ends_utc = dt_util.utcnow() + timedelta(
                seconds=delay
            )
        except Exception:
            self.state._auth_refresh_rejected_ends_utc = None
        _LOGGER.warning(message)

    def auth_refresh_recent_success_active(self) -> bool:
        """Return True when a recent successful refresh can satisfy stale 401s."""

        last_success = self.state._auth_refresh_last_success_mono
        if not isinstance(last_success, (int, float)):
            return False
        return (time.monotonic() - float(last_success)) <= float(
            AUTH_REFRESH_SUCCESS_REUSE_WINDOW_S
        )

    def note_login_wall_block(self, *, reason: str) -> None:
        """Persist a long auth block after Enphase starts serving the login wall."""

        coord = self.coordinator
        coord._note_auth_blocked(
            blocked_until=dt_util.utcnow() + timedelta(seconds=AUTH_BLOCKED_COOLDOWN_S),
            reason=reason,
        )

    async def async_run_auto_refresh(self) -> bool:
        """Run one stored-credential refresh attempt for all concurrent waiters."""

        coord = self.coordinator
        session = async_get_clientsession(coord.hass)
        self.state._auth_refresh_last_attempt_utc = dt_util.utcnow()
        email = coord._email
        password = coord._stored_password
        if not isinstance(email, str) or not isinstance(password, str):
            return False
        try:
            tokens, _ = await async_authenticate(session, email, password)
        except EnlightenAuthInvalidCredentials:
            self.state._auth_refresh_last_failure_reason = "invalid_credentials"
            self.note_auth_refresh_rejected(
                "Stored Enlighten credentials were rejected; reauthenticate via the integration options"
            )
            return False
        except EnlightenAuthMFARequired:
            self.state._auth_refresh_last_failure_reason = "mfa_required"
            self.note_auth_refresh_rejected(
                "Enphase account requires multi-factor authentication; complete MFA in the browser and reauthenticate"
            )
            return False
        except EnlightenAuthTooManySessions:
            self.state._auth_refresh_last_failure_reason = "too_many_active_sessions"
            self.note_login_wall_block(reason="too_many_active_sessions")
            _LOGGER.warning(
                "Enphase rejected stored-credential reauthentication because too many account sessions are active; automatic retries are paused for %s seconds",
                int(AUTH_BLOCKED_COOLDOWN_S),
            )
            return False
        except EnlightenAuthUnavailable:
            self.state._auth_refresh_last_failure_reason = "auth_service_unavailable"
            _LOGGER.debug(
                "Auth service unavailable while refreshing tokens; will retry later"
            )
            return False
        except Exception as err:  # noqa: BLE001
            self.state._auth_refresh_last_failure_reason = err.__class__.__name__
            _LOGGER.debug(
                "Unexpected error refreshing Enlighten auth: %s",
                redact_text(err),
            )
            return False

        self.state._auth_refresh_rejected_until = None
        self.state._auth_refresh_rejected_ends_utc = None
        self.state._auth_refresh_manual_retry_until = None
        coord._clear_auth_refresh_rejection_state()
        self.state._auth_refresh_suspended_until_utc = None
        self.state._auth_refresh_last_success_mono = time.monotonic()
        self.state._auth_refresh_last_success_utc = dt_util.utcnow()
        self.state._auth_refresh_last_failure_reason = None
        coord._clear_auth_block(persist=False)
        coord._clear_hems_auth_circuit(persist=False, reset_failure_count=True)
        coord._tokens = tokens
        coord.client.update_credentials(
            eauth=tokens.access_token,
            cookie=tokens.cookie,
        )
        coord._persist_tokens(tokens)
        return True
