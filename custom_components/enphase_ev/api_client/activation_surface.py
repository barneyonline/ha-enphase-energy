"""Activation surface for the stable Enphase client facade."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

import aiohttp
from yarl import URL

from ..const import (
    BASE_URL,
)
from ..log_redaction import (
    redact_site_id,
    redact_text,
)
from .errors import (
    ActivationAccessDenied,
    EnphaseLoginWallUnauthorized,
    InvalidPayloadError,
    OptionalEndpointUnavailable,
    Unauthorized,
)

if TYPE_CHECKING:
    from ..api import EnphaseEVClient

from .common import (
    _LOGGER,
    JsonDict,
    _activation_context_from_settings_html,
    _activation_grid_profiles_from_settings_html,
    _cookie_header_from_map,
    _cookie_map_from_header,
    _decode_jwt_exp,
)


def _activation_reference_headers(self: EnphaseEVClient) -> dict[str, str | None]:
    """Return headers for Activation reference-data calls."""

    token = self._activation_auth_token()
    return {
        "Accept": "application/json, text/plain, */*",
        "Cookie": self._activation_cookie(token),
        "enlm-token": token,
        "Referer": self._activation_referer
        or f"{BASE_URL}/app/activation_ui/?system_id={self._site}",
        "X-Requested-With": None,
    }


def _activation_headers(
    self: EnphaseEVClient, *, write: bool = False
) -> dict[str, str | None]:
    """Return cloud Activation API headers."""

    token = self._activation_auth_token()
    referer = self._activation_referer or (
        f"{BASE_URL}/app/activation_ui/?system_id={self._site}"
    )
    headers: dict[str, str | None] = {
        "Accept": "application/json, text/plain, */*",
        "Authorization": f"Bearer {token}" if token else None,
        "Cookie": self._activation_cookie(token),
        "Referer": referer,
        "X-Requested-With": None,
        "e-auth-token": None,
    }
    if write:
        headers["Content-Type"] = "application/json"
        headers["Origin"] = str(URL(referer).origin())
    return headers


def _activation_auth_token(self: EnphaseEVClient) -> str | None:
    """Return the settings-page Activation token, with stored-auth fallback."""

    token = self._activation_token
    if token:
        expires_at = _decode_jwt_exp(token)
        if (
            expires_at is None
            or expires_at > int(datetime.now(timezone.utc).timestamp()) + 60
        ):
            return token
        self._clear_activation_auth_context()
    return self._battery_config_single_auth_token()


def _activation_cookie(self: EnphaseEVClient, token: str | None) -> str | None:
    """Return session cookies with the Activation Manager token synchronized."""

    cookies = _cookie_map_from_header(self._cookie)
    if token and token == self._activation_token:
        cookies["enlighten_manager_token_production"] = token
    return _cookie_header_from_map(cookies) or None


async def async_prepare_activation_auth(
    self: EnphaseEVClient, *, force: bool = False
) -> bool:
    """Bootstrap the same Activation JWT embedded by the Enlighten settings UI."""

    if (
        not force
        and self._activation_token
        and self._activation_auth_token() == self._activation_token
    ):
        return True
    url = f"{BASE_URL}/systems/{self._site}/details"
    try:
        payload = await self._text(
            "GET",
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Referer": f"{BASE_URL}/systems/{self._site}",
                "X-Requested-With": None,
            },
        )
    except (
        aiohttp.ClientError,
        asyncio.TimeoutError,
        EnphaseLoginWallUnauthorized,
        Unauthorized,
    ) as err:
        _LOGGER.debug(
            "Activation auth bootstrap unavailable for site %s: %s",
            redact_site_id(self._site),
            redact_text(err, site_ids=(self._site,)),
        )
        return False
    self._activation_settings_grid_profiles = (
        _activation_grid_profiles_from_settings_html(payload)
    )
    context = _activation_context_from_settings_html(payload)
    if context is None:
        _LOGGER.debug(
            "Activation UI token was not present in settings for site %s",
            redact_site_id(self._site),
        )
        return False
    self._activation_token, self._activation_referer = context
    return True


async def _activation_payload(
    self: EnphaseEVClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str | None] | Callable[[], dict[str, str | None]],
    **kwargs: Any,
) -> object:
    """Return Activation JSON, mapping denied access to optional unavailable."""

    auth_retry_attempted = False
    while True:
        request_headers = headers() if callable(headers) else headers
        try:
            return await self._json(
                method,
                url,
                headers=request_headers,
                allow_reauth=False,
                use_cookie_header_only=True,
                **kwargs,
            )
        except EnphaseLoginWallUnauthorized as err:
            raise ActivationAccessDenied("Activation login wall") from err
        except Unauthorized as err:
            if not auth_retry_attempted and self._activation_token is not None:
                auth_retry_attempted = True
                self._clear_activation_auth_context()
                await self.async_prepare_activation_auth(force=True)
                continue
            raise ActivationAccessDenied("Activation access denied") from err
        except InvalidPayloadError as err:
            raise OptionalEndpointUnavailable("Activation payload unavailable") from err
        except aiohttp.ClientResponseError as err:
            if (
                err.status in {401, 403}
                and not auth_retry_attempted
                and self._activation_token is not None
            ):
                auth_retry_attempted = True
                self._clear_activation_auth_context()
                await self.async_prepare_activation_auth(force=True)
                continue
            if err.status in {401, 403, 404}:
                raise ActivationAccessDenied("Activation access denied") from err
            raise


async def _activation_json(
    self: EnphaseEVClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str | None] | Callable[[], dict[str, str | None]],
    **kwargs: Any,
) -> JsonDict:
    """Return Activation object JSON."""

    result = await self._activation_payload(
        method,
        url,
        headers=headers,
        **kwargs,
    )
    if not isinstance(result, dict):
        raise OptionalEndpointUnavailable("Activation payload was not an object")
    return result


async def async_get_activation_reference_data(self: EnphaseEVClient) -> JsonDict:
    """Return Activation country/region reference data."""

    url = f"{BASE_URL}/service/activation_service/api/details/reference_data"
    return await self._activation_json(
        "GET",
        url,
        headers=self._activation_reference_headers,
    )


async def async_get_activation_record(self: EnphaseEVClient) -> JsonDict:
    """Return the cloud Activation record for this site."""

    url = (
        f"{BASE_URL}/service/activation_backend/api/gateway/v4/"
        f"activations/{self._site}"
    )
    return await self._activation_json(
        "GET",
        url,
        params={"expand": "owner,host"},
        headers=self._activation_headers,
    )


async def async_get_activation_device_list(self: EnphaseEVClient) -> JsonDict:
    """Return Activation device inventory and current grid-profile status."""

    url = (
        f"{BASE_URL}/service/activation_backend/api/gateway/v4/"
        f"systems/{self._site}/devices/list"
    )
    result = await self._activation_payload(
        "GET",
        url,
        headers=self._activation_headers,
    )
    if isinstance(result, dict):
        return result
    if isinstance(result, list):
        return {"devices": result}
    raise OptionalEndpointUnavailable("Activation payload was not an object")


async def async_get_grid_profiles_filtered(
    self: EnphaseEVClient,
    *,
    country: str,
    state: str,
    commonly_used: bool = True,
) -> JsonDict:
    """Return grid profiles for a country/region from Activation."""

    url = (
        f"{BASE_URL}/service/activation_backend/api/gateway/v4/"
        f"systems/{self._site}/grid_profiles_filtered"
    )
    return await self._activation_json(
        "POST",
        url,
        json={
            "commonly_used": bool(commonly_used),
            "country": country,
            "state": state,
        },
        headers=lambda: self._activation_headers(write=True),
    )


async def async_apply_grid_profile(
    self: EnphaseEVClient,
    *,
    gateway_serial: str,
    part_num: str | None,
    ensemble_envoy: bool,
    profile_id: str,
) -> JsonDict:
    """Apply a cloud Activation grid profile to a Gateway."""

    url = (
        f"{BASE_URL}/service/activation_backend/api/gateway/v4/"
        f"systems/{self._site}/envoys"
    )
    envoy_payload: dict[str, object] = {
        "grid_profile_id": profile_id,
        "serial_num": gateway_serial,
        "ensemble_envoy": ensemble_envoy,
    }
    if part_num:
        envoy_payload["part_num"] = part_num
    result = await self._activation_payload(
        "PUT",
        url,
        json=[envoy_payload],
        headers=lambda: self._activation_headers(write=True),
        allow_empty_success=True,
    )
    if isinstance(result, dict):
        return result
    if isinstance(result, list):
        return {"envoys": result}
    raise OptionalEndpointUnavailable("Activation payload was not an object")
