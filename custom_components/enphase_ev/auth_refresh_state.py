"""Authentication runtime state and immutable publication metadata."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class AuthRefreshState:
    """Credential recovery state owned by AuthRefreshRuntime."""

    _auth_refresh_task: asyncio.Task[bool] | None = None
    _auth_refresh_rejected_count: int = 0
    _auth_refresh_rejected_until: float | None = None
    _auth_refresh_rejected_ends_utc: datetime | None = None
    _auth_refresh_manual_retry_until: float | None = None
    _auth_refresh_last_success_mono: float | None = None
    _auth_refresh_last_attempt_utc: datetime | None = None
    _auth_refresh_last_success_utc: datetime | None = None
    _auth_refresh_last_failure_reason: str | None = None
    _auth_refresh_suspended_until_utc: datetime | None = None
    _auth_blocked_until_utc: datetime | None = None
    _auth_block_reason: str | None = None
    _auth_block_issue_reported: bool = False


@dataclass(frozen=True, slots=True)
class AuthRefreshSnapshot:
    """Observable recovery state, excluding acquisition timestamps."""

    rejected_count: int = 0
    failure_reason: str | None = None
    blocked_until: datetime | None = None
    block_reason: str | None = None
    suspended_until: datetime | None = None
