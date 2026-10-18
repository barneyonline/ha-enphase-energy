"""Bounded VPP renewal using only an entry's existing Enlighten session.

JWT claims below are cross-account consistency guards, not signature validation.
The trusted HTTPS token endpoint and Grid Services remain the authenticators.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http.cookies import CookieError, SimpleCookie
from typing import TYPE_CHECKING

import aiohttp
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.util import dt as dt_util
from yarl import URL

from .api_client.common import _decode_jwt_payload, _serialize_cookie_jar
from .api_client.vpp_auth import (
    _expiry_state,
    select_vpp_token,
    strict_account_identity as _identity,
)
from .api_models import AuthTokens
from .const import (
    BASE_URL,
    CONF_ACCESS_TOKEN,
    CONF_COOKIE,
    CONF_EAUTH,
    CONF_SESSION_ID,
    CONF_TOKEN_EXPIRES_AT,
    SELF_TOKEN_URL,
)

if TYPE_CHECKING:
    from .coordinator import EnphaseCoordinator

STATE_KEY = "vpp_session_renewal"
COOLDOWN_S = 900.0
_SESSION_COOKIES = ("_enlighten_4_session", "enlighten_session", "_enlighten_session")
_MANAGER = "enlighten_manager_token_production"
_SUSPENSION_REASONS = (
    "missing_session",
    "ambiguous_account",
    "session_rejected",
    "redirect",
    "non_json_response",
    "token_rejected",
)


def _cookies(header: str) -> SimpleCookie:
    """Reject ambiguous or malformed snapshots instead of guessing a session."""
    cookies: SimpleCookie = SimpleCookie()
    names = []
    for part in header.split(";"):
        name, sep, _ = part.strip().partition("=")
        if not sep or not name or name in names:
            return cookies
        names.append(name)
    try:
        cookies.load(header)
    except CookieError:
        return SimpleCookie()
    if set(cookies) != set(names):
        return SimpleCookie()
    return cookies


def _retry_deadline(value: str | None, now: float) -> float:
    """Respect both HTTP Retry-After forms with a separate fifteen-minute floor."""
    deadline = now + COOLDOWN_S
    if value is None:
        return deadline
    try:
        seconds = float(value)
        if math.isfinite(seconds):
            return max(deadline, now + seconds)
    except ValueError:
        pass
    try:
        return max(deadline, parsedate_to_datetime(value).timestamp())
    except (ValueError, TypeError, OverflowError):
        return deadline


class VppSessionRenewal:
    """Entry-owned single-flight renewal, independent of password authentication."""

    def __init__(self, coordinator: EnphaseCoordinator) -> None:
        self.coordinator = coordinator
        self._lock = asyncio.Lock()
        self._last_attempt_utc: str | None = None
        self._last_http_status: int | None = None
        self._last_outcome = "not_attempted"
        self._last_reason = "not_attempted"
        self._last_check_reason = "not_checked"

    def _skip(self, reason: str) -> bool:
        self._last_check_reason = reason
        return False

    def _result(self, outcome: str, reason: str) -> None:
        self._last_outcome = outcome
        self._last_reason = reason

    def diagnostics(self) -> dict[str, object]:
        """Return fixed reasons and times without credentials or response text.

        Attempt details belong to this runtime; the persisted cooldown survives
        reloads even when no attempt has occurred in the new runtime.
        """
        entry = self.coordinator.config_entry
        raw_state = entry.data.get(STATE_KEY) if entry is not None else None
        state = raw_state if isinstance(raw_state, dict) else {}
        suspended = (
            entry is not None
            and state.get("suspended") is True
            and state.get("auth_identifier")
            == hashlib.sha256(json.dumps(self._credentials()).encode()).hexdigest()
        )
        deadline = state.get("next_attempt")
        next_attempt = None
        if isinstance(deadline, (int, float)) and not isinstance(deadline, bool):
            try:
                if math.isfinite(deadline):
                    next_attempt = datetime.fromtimestamp(deadline, UTC).isoformat()
            except (ValueError, OverflowError, OSError):
                pass
        return {
            "last_attempt_utc": self._last_attempt_utc,
            "last_http_status": self._last_http_status,
            "last_outcome": self._last_outcome,
            "last_reason": self._last_reason,
            "last_check_reason": self._last_check_reason,
            "next_attempt_utc": next_attempt,
            "suspended": suspended,
            "suspension_reason": (
                reason
                if suspended
                and (reason := state.get("suspension_reason")) in _SUSPENSION_REASONS
                else None
            ),
        }

    def _credentials(self) -> tuple[object, ...]:
        coord = self.coordinator
        entry = coord.config_entry
        assert entry is not None
        return (
            entry.data.get(CONF_COOKIE),
            entry.data.get(CONF_EAUTH),
            entry.data.get(CONF_ACCESS_TOKEN),
            coord.client._cookie,
            coord.client._eauth,
            coord._tokens.cookie,
            coord._tokens.access_token,
            entry.data.get(CONF_SESSION_ID),
            entry.data.get(CONF_EMAIL),
            entry.data.get(CONF_PASSWORD),
            coord._tokens.session_id,
        )

    def _store(self, state: dict[str, object], **updates: object) -> bool:
        coord = self.coordinator
        assert coord.config_entry is not None
        data = dict(coord.config_entry.data)
        data.update(updates)
        data[STATE_KEY] = dict(state)
        return coord._async_update_config_entry_data_internal(
            data, reason="vpp_session_renewal"
        )

    def _extend_cooldown(self, deadline: float) -> float:
        """Extend the entry's current cooldown without restoring stale auth state."""
        entry = self.coordinator.config_entry
        assert entry is not None
        raw_state = entry.data.get(STATE_KEY)
        state = dict(raw_state) if isinstance(raw_state, dict) else {}
        current_deadline = state.get("next_attempt")
        if isinstance(current_deadline, (int, float)) and current_deadline >= deadline:
            return current_deadline
        state["next_attempt"] = deadline
        self._store(state)
        return deadline

    async def async_prepare(self) -> bool:
        """Return usable credentials, making at most one bounded session request."""
        async with self._lock:
            coord = self.coordinator
            if not coord.runtime_active:
                return self._skip("runtime_inactive")
            client = coord.client
            # Preflight must not replace the last actual GS request's diagnostics.
            if select_vpp_token(
                client._bearer(),
                client._eauth,
                getattr(client._s, "cookie_jar", None),
                ignored_live_token=client._vpp_ignored_live_token,
            ):
                self._last_check_reason = "usable_token"
                return True
            entry = coord.config_entry
            if entry is None:
                return self._skip("missing_entry")
            snapshot = self._credentials()
            account_credentials = (
                entry.data.get(CONF_EMAIL),
                entry.data.get(CONF_PASSWORD),
            )
            # The entry, live client and coordinator must describe the same snapshot.
            cookie = client._cookie
            access = client._eauth
            if (
                cookie != entry.data.get(CONF_COOKIE, "")
                or cookie != coord._tokens.cookie
                or access != coord._tokens.access_token
                or access
                != (entry.data.get(CONF_EAUTH) or entry.data.get(CONF_ACCESS_TOKEN))
            ):
                return self._skip("credential_snapshot_mismatch")
            binding = hashlib.sha256(json.dumps(snapshot).encode()).hexdigest()
            raw_state = entry.data.get(STATE_KEY)
            state = dict(raw_state) if isinstance(raw_state, dict) else {}
            # Store the private binding under an existing diagnostics redaction key.
            if state.get("auth_identifier") != binding:
                # Credential replacement releases suspension, never the per-entry floor.
                state = {
                    "auth_identifier": binding,
                    "next_attempt": state.get("next_attempt"),
                }
            now = dt_util.utcnow().timestamp()
            deadline = state.get("next_attempt")
            if state.get("suspended") is True:
                return self._skip("suspended")
            if isinstance(deadline, (int, float)) and deadline > now:
                return self._skip("cooldown")
            cookies = _cookies(cookie)
            manager = cookies.get(_MANAGER)
            tokens = [
                token
                for token in (
                    manager.value if manager else None,
                    access,
                    entry.data.get(CONF_EAUTH),
                    entry.data.get(CONF_ACCESS_TOKEN),
                )
                if isinstance(token, str) and token
            ]
            identities = {_identity(token) for token in tokens}
            has_session = any(
                cookies.get(name) and cookies[name].value for name in _SESSION_COOKIES
            )
            if not has_session or len(identities) != 1 or None in identities:
                state["suspended"] = True
                state["suspension_reason"] = (
                    "missing_session" if not has_session else "ambiguous_account"
                )
                self._store(state)
                return self._skip(str(state["suspension_reason"]))
            expected = identities.pop()
            state["next_attempt"] = now + COOLDOWN_S
            # Reserve before I/O so reloads cannot defeat the attempt floor.
            if not self._store(state):
                return self._skip("entry_update_rejected")
            self._last_check_reason = "attempted"
            self._last_attempt_utc = dt_util.utcnow().isoformat()
            self._last_http_status = None
            self._result("pending", "request_started")
            jar = aiohttp.CookieJar()
            jar.update_cookies(cookies, response_url=URL(BASE_URL))
            try:
                async with aiohttp.ClientSession(
                    cookie_jar=jar, trust_env=False
                ) as session:
                    async with session.get(
                        SELF_TOKEN_URL,
                        headers={
                            "Accept": "application/json",
                            "Referer": f"{BASE_URL}/",
                        },
                        allow_redirects=False,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as response:
                        self._last_http_status = response.status
                        retry_after = response.headers.get("Retry-After")
                        deadline = _retry_deadline(
                            retry_after, dt_util.utcnow().timestamp()
                        )
                        # Backoff belongs to the entry even if credentials changed
                        # in flight or reading the response body fails/cancels.
                        if (
                            retry_after is not None
                            and coord.hass.config_entries.async_get_entry(
                                entry.entry_id
                            )
                            is entry
                            and coord.config_entry is entry
                        ):
                            deadline = self._extend_cooldown(deadline)
                        state["next_attempt"] = deadline
                        # Never follow even same-origin redirects into login/MFA.
                        if response.status in (301, 302, 303, 307, 308, 401, 403):
                            state["suspended"] = True
                            self._result(
                                "failed",
                                (
                                    "session_rejected"
                                    if response.status in (401, 403)
                                    else "redirect"
                                ),
                            )
                            state["suspension_reason"] = self._last_reason
                            payload = None
                        elif response.status != 200:
                            self._result("failed", "http_error")
                            payload = None
                        elif response.content_type != "application/json":
                            state["suspended"] = True
                            self._result("failed", "non_json_response")
                            state["suspension_reason"] = self._last_reason
                            payload = None
                        else:
                            self._result("failed", "invalid_payload")
                            payload = await response.json()
                if (
                    not coord.runtime_active
                    or coord.config_entry is not entry
                    or coord.client is not client
                    or self._credentials() != snapshot
                ):
                    self._result("discarded", "credential_snapshot_changed")
                    return False
                if not isinstance(payload, dict):
                    self._store(state)
                    return False
                returned = [
                    payload[key]
                    for key in ("token", "auth_token", "access_token")
                    if key in payload
                ]
                candidates = {
                    value for value in returned if isinstance(value, str) and value
                }
                token = (
                    candidates.pop()
                    if (
                        len(candidates) == 1
                        and all(isinstance(value, str) and value for value in returned)
                    )
                    else None
                )
                cookie_header, cookie_map = _serialize_cookie_jar(
                    jar, (SELF_TOKEN_URL,)
                )
                returned_manager = cookie_map.get(_MANAGER)
                if (
                    token is None
                    or _identity(token) != expected
                    or _expiry_state(token, dt_util.utcnow().timestamp())
                    != "not_expired"
                    or (
                        returned_manager is not None
                        and _identity(returned_manager) != expected
                    )
                ):
                    state["suspended"] = True
                    self._result("failed", "token_rejected")
                    state["suspension_reason"] = self._last_reason
                    self._store(state)
                    return False
                claims = _decode_jwt_payload(token)
                assert claims is not None
                expiry = int(claims["exp"])
                session_id = next(
                    (
                        cookie_map[name]
                        for name in _SESSION_COOKIES
                        if cookie_map.get(name)
                    ),
                    None,
                )
                updates = {
                    CONF_COOKIE: cookie_header,
                    CONF_EAUTH: token,
                    CONF_ACCESS_TOKEN: token,
                    CONF_SESSION_ID: session_id,
                    CONF_TOKEN_EXPIRES_AT: expiry,
                }
                # Bind persisted backoff to the accepted snapshot for reloads.
                accepted = (
                    cookie_header,
                    token,
                    token,
                    cookie_header,
                    token,
                    cookie_header,
                    token,
                    session_id,
                    *account_credentials,
                    session_id,
                )
                state["auth_identifier"] = hashlib.sha256(
                    json.dumps(accepted).encode()
                ).hexdigest()
                if not self._store(state, **updates):
                    self._result("discarded", "entry_update_rejected")
                    return False
                coord._tokens = AuthTokens(
                    cookie=cookie_header,
                    access_token=token,
                    session_id=session_id,
                    token_expires_at=expiry,
                    raw_cookies=cookie_map,
                )
                client.update_credentials(eauth=token, cookie=cookie_header)
                self._result("succeeded", "renewed")
                return True
            except asyncio.CancelledError:
                self._result("cancelled", "request_cancelled")
                raise
            except (aiohttp.ClientError, TimeoutError, ValueError):
                self._result("failed", "request_error")
                # No exception text, response bodies or credentials enter diagnostics.
                if (
                    coord.runtime_active
                    and coord.config_entry is entry
                    and coord.client is client
                    and self._credentials() == snapshot
                ):
                    self._store(state)
                return False
