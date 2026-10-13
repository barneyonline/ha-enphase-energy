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


def _usable_token(token: str | None, now: float) -> bool:
    """Exclude known-expired JWTs while retaining opaque-token compatibility."""

    if not token:
        return False
    payload = _decode_jwt_payload(token)
    expiry = payload.get("exp") if payload is not None else None
    if isinstance(expiry, int) and not isinstance(expiry, bool):
        return expiry > now
    if isinstance(expiry, float) and math.isfinite(expiry):
        return expiry > now
    return True


def select_vpp_token(
    manager: str | None, access: str | None, jar: AbstractCookieJar | None
) -> str | None:
    """Prefer a same-account live manager, then eligible stored credentials.

    The HA session jar is shared. Decoded identity must match a consistent entry
    credential context before adopting a jar token; claims are not authentication
    proof, and the Grid Services server still validates the selected credential.
    """

    now = dt_util.utcnow().timestamp()
    identities = {
        identity.strip()
        for token in (manager, access)
        if (identity := _account_identity(token)) and identity.strip()
    }
    if jar is not None and len(identities) == 1:
        try:
            morsel = jar.filter_cookies(URL(BASE_URL)).get(
                "enlighten_manager_token_production"
            )
            value = morsel.value if morsel is not None else None
            candidate = value if isinstance(value, str) else None
        except Exception:  # noqa: BLE001 - keep stored credentials on jar failure
            candidate = None
        candidate_identity = _account_identity(candidate)
        if (
            candidate_identity is not None
            and candidate_identity.strip() in identities
            and _usable_token(candidate, now)
        ):
            return candidate
    return next(
        (token for token in (manager, access) if _usable_token(token, now)), None
    )
