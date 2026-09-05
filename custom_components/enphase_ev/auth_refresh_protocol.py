"""Coordinator capabilities required by stored-credential recovery."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from homeassistant.core import HomeAssistant

from .api import EnphaseEVClient
from .api_models import AuthTokens


class AuthRefreshHost(Protocol):
    """Explicit legacy coordinator services; mutable recovery state is runtime-owned."""

    hass: HomeAssistant
    client: EnphaseEVClient
    _email: str | None
    _remember_password: bool
    _stored_password: str | None
    _tokens: AuthTokens

    def _auth_block_active(self) -> bool: ...
    def _auth_refresh_suspended_active(self) -> bool: ...
    def _hems_auth_circuit_active(self) -> bool: ...
    def _clear_auth_block(self, *, persist: bool = True) -> None: ...
    def _clear_auth_refresh_suspension(self, *, persist: bool = True) -> None: ...
    def _clear_hems_auth_circuit(
        self, *, persist: bool = True, reset_failure_count: bool = True
    ) -> None: ...
    def _note_auth_refresh_suspended(self, *, suspended_until: datetime) -> None: ...
    def _note_auth_blocked(self, *, blocked_until: datetime, reason: str) -> None: ...
    def _clear_auth_refresh_rejection_state(self) -> None: ...
    def _persist_tokens(self, tokens: AuthTokens) -> None: ...
