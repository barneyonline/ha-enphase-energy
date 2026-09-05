"""Small structural client contracts used by independent endpoint surfaces."""

from __future__ import annotations

from typing import Any, Protocol

from .errors import InvalidPayloadError


class JsonClient(Protocol):
    """JSON request capability without exposing coordinator or session ownership."""

    _site: str

    async def _json(
        self,
        method: str,
        url: str,
        *,
        mark_payload_success: bool = True,
        log_invalid_payload: bool = True,
        **kwargs: Any,
    ) -> Any: ...


class SiteClient(JsonClient, Protocol):
    """Headers and payload validation needed by site telemetry endpoints."""

    def _systems_json_headers(self) -> dict[str, str]: ...
    def _history_headers(self) -> dict[str, str]: ...
    def _system_dashboard_headers(self) -> dict[str, str]: ...
    def _today_headers(self) -> dict[str, str]: ...

    def _invalid_payload_error(
        self,
        *,
        endpoint: str | None,
        summary: str | None = None,
        status: int | None = None,
        content_type: str | None = None,
        failure_kind: str,
        decode_error: str | None = None,
        payload: object = None,
        log_warning: bool = True,
    ) -> InvalidPayloadError: ...


class VppClient(JsonClient, Protocol):
    """The VPP surface needs only isolated headers and JSON transport."""

    def _vpp_headers(self) -> dict[str, str | None]: ...
