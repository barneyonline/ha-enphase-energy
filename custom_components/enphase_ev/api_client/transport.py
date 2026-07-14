"""Injected-session authentication transport for Enphase cloud requests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
import json
from typing import Any

import aiohttp


async def request_json(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    timeout: int,
    headers: dict[str, str] | None,
    data: Any | None,
    json_data: Any | None,
    request_guard: Callable[[str, str], AbstractAsyncContextManager[None]],
    request_label: Callable[[object, object], str],
    safe_error_message: Callable[..., str],
    is_session_limit: Callable[[object], bool],
    unavailable_error: type[Exception],
    session_limit_error: type[Exception],
) -> Any:
    """Perform an HTTP request returning JSON with timeout handling."""

    req_kwargs: dict[str, Any] = {}
    if headers is not None:
        req_kwargs["headers"] = headers
    if data is not None:
        req_kwargs["data"] = data
    if json_data is not None:
        req_kwargs["json"] = json_data

    async with asyncio.timeout(timeout):
        async with request_guard(method, url):
            async with session.request(
                method, url, allow_redirects=True, **req_kwargs
            ) as response:
                if response.status >= 500:
                    raise unavailable_error(
                        f"Server error {response.status} at {request_label(method, url)}"
                    )
                if response.status >= 400:
                    try:
                        body_text = await response.text()
                    except Exception:  # noqa: BLE001 - best-effort diagnostics
                        body_text = ""
                    if is_session_limit(body_text):
                        raise session_limit_error("Too many active Enlighten sessions")
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "")
                if "json" not in content_type:
                    text = await response.text()
                    if is_session_limit(text):
                        raise session_limit_error("Too many active Enlighten sessions")
                    raise unavailable_error(
                        safe_error_message(
                            status=int(response.status),
                            reason="Unexpected non-JSON response",
                            headers=response.headers,
                            body_text=text,
                        )
                    )
                payload = await response.json()
                if is_session_limit(payload):
                    raise session_limit_error("Too many active Enlighten sessions")
                return payload


async def request_mfa_json(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    timeout: int,
    headers: dict[str, str] | None,
    data: Any | None,
    request_label: Callable[[object, object], str],
    safe_error_message: Callable[..., str],
    is_session_limit: Callable[[object], bool],
    unavailable_error: type[Exception],
    session_limit_error: type[Exception],
) -> Any:
    """Perform an MFA HTTP request with tolerant JSON parsing."""

    req_kwargs: dict[str, Any] = {}
    if headers is not None:
        req_kwargs["headers"] = headers
    if data is not None:
        req_kwargs["data"] = data

    async with asyncio.timeout(timeout):
        async with session.request(
            method, url, allow_redirects=True, **req_kwargs
        ) as response:
            if response.status >= 500:
                raise unavailable_error(
                    f"Server error {response.status} at {request_label(method, url)}"
                )
            if response.status in (204, 205):
                return {}
            if response.status >= 400:
                try:
                    body_text = await response.text()
                except Exception:  # noqa: BLE001 - best-effort diagnostics
                    body_text = ""
                if is_session_limit(body_text):
                    raise session_limit_error("Too many active Enlighten sessions")
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "")
            if "json" in content_type:
                payload = await response.json()
                if is_session_limit(payload):
                    raise session_limit_error("Too many active Enlighten sessions")
                return payload
            text = await response.text()
            if is_session_limit(text):
                raise session_limit_error("Too many active Enlighten sessions")
            if not text.strip():
                return {}
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as err:
                raise unavailable_error(
                    safe_error_message(
                        status=int(response.status),
                        reason="Unexpected MFA response",
                        headers=response.headers,
                        body_text=text,
                    )
                ) from err
            if is_session_limit(payload):
                raise session_limit_error("Too many active Enlighten sessions")
            return payload
