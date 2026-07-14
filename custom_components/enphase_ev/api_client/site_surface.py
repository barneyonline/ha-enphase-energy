"""Site telemetry and livestream HTTP surface for the Enphase cloud client."""

from __future__ import annotations

import logging
from typing import Any, cast

import aiohttp
from yarl import URL

from .. import api_parsers
from ..log_redaction import redact_site_id

_LOGGER = logging.getLogger(__name__)


async def weather(client: Any, *, base_url: str, locale: str) -> dict[str, Any]:
    """Return and validate current weather for a client site."""

    endpoint = f"/systems/{client._site}/weather.json"
    url = URL(f"{base_url}{endpoint}").with_query({"locale": str(locale)})
    data = await client._json(
        "GET",
        str(url),
        headers=client._systems_json_headers(),
    )
    if not isinstance(data, dict):
        raise client._invalid_payload_error(
            endpoint=endpoint,
            summary="Weather payload must be an object",
            failure_kind="shape",
            payload=data,
        )
    return cast(dict[str, Any], data)


def normalize_latest_power(payload: object) -> dict[str, object] | None:
    """Normalize app-api latest-power payloads into a stable shape."""

    return cast(
        dict[str, object] | None,
        api_parsers.normalize_latest_power_payload(payload),
    )


async def latest_power(client: Any, *, base_url: str) -> dict[str, object] | None:
    """Return the latest normalized site power sample."""

    url = f"{base_url}/app-api/{client._site}/get_latest_power"
    data = await client._json("GET", url, headers=client._history_headers())
    normalized = normalize_latest_power(data)
    if normalized is not None:
        return normalized

    top_level_keys: list[str] = []
    nested_keys: list[str] = []
    payload_type = type(data).__name__
    if isinstance(data, dict):
        top_level_keys = sorted(str(key) for key in data)
        nested = data.get("latest_power")
        if not isinstance(nested, dict):
            candidate = data.get("data")
            if isinstance(candidate, dict):
                nested = candidate.get("latest_power")
                if not isinstance(nested, dict):
                    nested = candidate
        if isinstance(nested, dict):
            nested_keys = sorted(str(key) for key in nested)

    _LOGGER.debug(
        "Invalid latest power payload for site %s (payload_type=%s, top_level_keys=%s, nested_keys=%s)",
        redact_site_id(client._site),
        payload_type,
        top_level_keys,
        nested_keys,
    )
    return None


async def show_livestream(
    client: Any,
    *,
    base_url: str,
    allow_reauth: bool,
    unauthorized_error: type[Exception],
    invalid_payload_error: type[Exception],
    optional_non_json: Any,
) -> dict[str, object] | None:
    """Return site live-status capability flags when available."""

    url = f"{base_url}/app-api/{client._site}/show_livestream"
    try:
        data = await client._json(
            "GET",
            url,
            headers=client._system_dashboard_headers(),
            allow_reauth=allow_reauth,
        )
    except unauthorized_error:
        if not allow_reauth:
            raise
        return None
    except invalid_payload_error as err:
        if optional_non_json(err):
            return None
        raise
    except aiohttp.ClientResponseError as err:
        if err.status in (401, 403, 404):
            if not allow_reauth and err.status in (401, 403):
                raise
            return None
        raise
    return data if isinstance(data, dict) else None


async def livestream_authorizer(
    client: Any,
    serial_num: str,
    *,
    base_url: str,
    live_debug: bool,
    allow_reauth: bool,
    unauthorized_error: type[Exception],
    invalid_payload_error: type[Exception],
    optional_non_json: Any,
) -> dict[str, object] | None:
    """Return signed AWS IoT connection details for a site's live stream."""

    query: dict[str, str] = {"serial_num": str(serial_num)}
    if live_debug:
        query["live_debug"] = "true"
    endpoint = (
        "/service/system_dashboard/api_internal/cs/sites/livestream"
        if live_debug
        else "/pv/aws_sigv4/livestream.json"
    )
    url = URL(f"{base_url}{endpoint}").with_query(query)
    headers = client._today_headers()
    headers["X-Requested-With"] = "XMLHttpRequest"
    try:
        data = await client._json(
            "GET",
            str(url),
            headers=headers,
            allow_reauth=allow_reauth,
        )
    except unauthorized_error:
        if not allow_reauth:
            raise
        return None
    except invalid_payload_error as err:
        if optional_non_json(err):
            return None
        raise
    except aiohttp.ClientResponseError as err:
        if err.status in (401, 403, 404):
            if not allow_reauth and err.status in (401, 403):
                raise
            return None
        raise
    return data if isinstance(data, dict) else None
