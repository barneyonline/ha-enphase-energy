"""VPP-only credential selection without login or cookie transmission."""

from __future__ import annotations

import math

from aiohttp.abc import AbstractCookieJar
from homeassistant.util import dt as dt_util
from yarl import URL

from ..const import BASE_URL
from .common import _decode_jwt_payload


def _account_identity(token: str | None) -> str | None:
    """Use only scalar account identifiers, never malformed decoded claims."""

    if not token:
        return None
    payload = _decode_jwt_payload(token)
    if payload is None:
        return None
    for container in (payload, payload.get("data")):
        if not isinstance(container, dict):
            continue
        for key in ("user_id", "userId", "userid"):
            value = container.get(key)
            if value is not None:
                if isinstance(value, (str, int)) and not isinstance(value, bool):
                    return str(value).strip() or None
                return None
    return None


def strict_account_identity(token: str) -> str | None:
    """Require every supported identity claim to agree, including nested claims."""
    payload = _decode_jwt_payload(token)
    if not payload:
        return None
    identities = set()
    for container in (payload, payload.get("data")):
        if not isinstance(container, dict):
            continue
        for key in ("user_id", "userId", "userid"):
            if key not in container:
                continue
            value = container[key]
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                return None
            identity = str(value).strip()
            if not identity:
                return None
            identities.add(identity)
    return identities.pop() if len(identities) == 1 else None


def _expiry_state(token: str | None, now: float) -> str:
    """Describe expiry without retaining claims or changing opaque-token policy."""

    if not token:
        return "absent"
    payload = _decode_jwt_payload(token)
    expiry = payload.get("exp") if payload is not None else None
    if isinstance(expiry, int) and not isinstance(expiry, bool):
        return "not_expired" if expiry > now else "expired"
    if isinstance(expiry, float) and math.isfinite(expiry):
        return "not_expired" if expiry > now else "expired"
    return "unknown"


def credential_diagnostics(snapshot: object) -> dict[str, object]:
    """Export only bounded categories from the last request-time selection."""

    data = snapshot if isinstance(snapshot, dict) else {}
    source = data.get("source")
    account_match = data.get("account_match")
    candidates = data.get("candidates")
    candidates = candidates if isinstance(candidates, dict) else {}
    safe_candidates = {}
    for category in ("stored_manager", "stored_access", "live_manager"):
        candidate = candidates.get(category)
        candidate = candidate if isinstance(candidate, dict) else {}
        expiry = candidate.get("expiry")
        safe_candidates[category] = {
            "expiry": (
                expiry
                if expiry in ("absent", "expired", "not_expired", "unknown")
                else "not_evaluated"
            ),
            "eligible": candidate.get("eligible") is True,
        }
    return {
        "source": (
            source
            if source in ("live_manager", "stored_manager", "stored_access", "none")
            else "not_attempted"
        ),
        "account_match": (
            account_match
            if account_match in ("match", "mismatch", "unknown", "conflicting_context")
            else "not_evaluated"
        ),
        "retained_candidate_excluded": data.get("retained_candidate_excluded") is True,
        "fallback_attempted": data.get("fallback_attempted") is True,
        "candidates": safe_candidates,
    }


def live_manager_token(jar: AbstractCookieJar | None) -> str | None:
    """Read the Enlighten manager without mutating the shared session jar."""

    if jar is None:
        return None
    try:
        morsel = jar.filter_cookies(URL(BASE_URL)).get(
            "enlighten_manager_token_production"
        )
        value = morsel.value if morsel is not None else None
        return value if isinstance(value, str) else None
    except Exception:  # noqa: BLE001 - keep stored credentials on jar failure
        return None


def select_vpp_token(
    manager: str | None,
    access: str | None,
    jar: AbstractCookieJar | None,
    *,
    ignored_live_token: str | None = None,
    diagnostics: dict[str, object] | None = None,
) -> str | None:
    """Prefer a same-account live manager, then eligible stored credentials.

    The HA session jar is shared. Decoded identity must match a consistent entry
    credential context before adopting a jar token; claims are not authentication
    proof, and the Grid Services server still validates the selected credential.
    A token retained in the jar across an explicit credential update cannot
    override that update. A different eligible jar token can be adopted later.
    """

    now = dt_util.utcnow().timestamp()
    identities = {
        identity.strip()
        for token in (manager, access)
        if (identity := _account_identity(token)) and identity.strip()
    }
    candidates = {
        category: {
            "expiry": (expiry := _expiry_state(token, now)),
            "eligible": expiry in ("not_expired", "unknown"),
        }
        for category, token in (("stored_manager", manager), ("stored_access", access))
    }
    candidates["live_manager"] = {"expiry": "not_evaluated", "eligible": False}
    state: dict[str, object] = {
        "source": "none",
        "account_match": "conflicting_context" if len(identities) > 1 else "unknown",
        "retained_candidate_excluded": False,
        "candidates": candidates,
    }
    if diagnostics is not None:
        diagnostics.clear()
        diagnostics.update(state)
    if jar is not None and len(identities) == 1:
        candidate = live_manager_token(jar)
        candidate_identity = _account_identity(candidate)
        retained = bool(candidate and candidate == ignored_live_token)
        matches = (
            candidate_identity is not None and candidate_identity.strip() in identities
        )
        expiry = _expiry_state(candidate, now)
        eligible = (
            candidate_identity is not None
            and not retained
            and matches
            and expiry in ("not_expired", "unknown")
        )
        candidates["live_manager"] = {"expiry": expiry, "eligible": eligible}
        if diagnostics is not None:
            diagnostics["account_match"] = (
                "unknown"
                if candidate_identity is None
                else "match" if matches else "mismatch"
            )
            diagnostics["retained_candidate_excluded"] = retained
        if eligible:
            if diagnostics is not None:
                diagnostics["source"] = "live_manager"
            return candidate
    for category, token in (("stored_manager", manager), ("stored_access", access)):
        if candidates[category]["eligible"]:
            if diagnostics is not None:
                diagnostics["source"] = category
            return token
    return None


def select_vpp_fallback(
    manager: str | None,
    access: str | None,
    jar: AbstractCookieJar | None,
    *,
    rejected_token: str | None,
    ignored_live_token: str | None,
    diagnostics: dict[str, object],
) -> str | None:
    """Select one distinct, unexpired, account-bound token after a GS 401.

    Unlike the initial compatibility policy, opaque/ambiguous account context
    and unknown expiry cannot authorize an additional request.
    """
    account = strict_account_identity(rejected_token or "")
    context = [token for token in (manager, access) if token]
    if (
        account is None
        or not context
        or any(strict_account_identity(token) != account for token in context)
    ):
        return None
    now = dt_util.utcnow().timestamp()
    live = live_manager_token(jar)
    for source, token in (
        ("live_manager", live if live != ignored_live_token else None),
        ("stored_manager", manager),
        ("stored_access", access),
    ):
        if (
            token
            and token != rejected_token
            and strict_account_identity(token) == account
            and _expiry_state(token, now) == "not_expired"
        ):
            diagnostics["source"] = source
            diagnostics["fallback_attempted"] = True
            previous = diagnostics.get("candidates")
            candidates = dict(previous) if isinstance(previous, dict) else {}
            candidates[source] = {"expiry": "not_expired", "eligible": True}
            diagnostics["candidates"] = candidates
            if source == "live_manager":
                diagnostics["account_match"] = "match"
                diagnostics["retained_candidate_excluded"] = False
            return token
    return None
