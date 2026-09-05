"""Dashboard surface for the stable Enphase client facade."""

from __future__ import annotations

import asyncio
import uuid
from builtins import ExceptionGroup
from datetime import date, datetime, timezone
from functools import partial
from typing import TYPE_CHECKING, Any, cast

import aiohttp
from yarl import URL

from ..api_models import (
    TextResponse,
)
from ..const import (
    BASE_URL,
)
from ..log_redaction import (
    redact_site_id,
    redact_text,
)
from . import site_surface as api_site_surface
from .errors import (
    EnphaseLoginWallUnauthorized,
    EVSETimeseriesUnavailable,
    InvalidPayloadError,
    OptionalEndpointUnavailable,
    SessionHistoryUnavailable,
    SiteEnergyUnavailable,
    Unauthorized,
    _is_hems_invalid_site_error,
    _is_optional_html_payload,
    _is_optional_non_json_payload,
)

if TYPE_CHECKING:
    from ..api import EnphaseEVClient

from .common import (
    _LOGGER,
    _SYSTEM_ALARMS_MAX_PAGES,
    _SYSTEM_ALARMS_PAGE_SIZE,
    _SYSTEM_EVENTS_MAX_PAGES,
    _SYSTEM_EVENTS_PAGE_SIZE,
    JsonDict,
    _system_dashboard_query_type,
    is_evse_timeseries_unavailable_error,
    is_session_history_unavailable_error,
    is_site_energy_unavailable_error,
)


async def _system_dashboard_get(
    self: EnphaseEVClient,
    modern_url: str,
    legacy_url: str,
) -> JsonDict | None:
    """Fetch a system dashboard payload from the modern route with fallback."""

    headers = self._system_dashboard_headers
    for url in (modern_url, legacy_url):
        try:
            data = await self._json("GET", url, headers=headers)
        except Exception as err:  # noqa: BLE001
            if self._system_dashboard_is_optional_error(err):
                continue
            raise
        return data if isinstance(data, dict) else None

    return None


async def site_tariff_billing_details(self: EnphaseEVClient) -> JsonDict:
    """Return site tariff billing-cycle details."""

    url = f"{BASE_URL}/service/tariff/tariff-ms/systems/{self._site}/billing-details"
    return cast(JsonDict, await self._json("GET", url, headers=self._tariff_headers))


async def site_tariff_billing_update(
    self: EnphaseEVClient,
    payload: dict[str, Any],
    *,
    request_date: date | datetime | str | None = None,
) -> JsonDict:
    """Update site tariff billing-cycle details."""

    if request_date is None:
        request_date_text = date.today().isoformat()
    elif isinstance(request_date, datetime):
        request_date_text = request_date.date().isoformat()
    elif isinstance(request_date, date):
        request_date_text = request_date.isoformat()
    else:
        request_date_text = str(request_date)
    url = f"{BASE_URL}/service/tariff/tariff-ms/systems/{self._site}/billing-details"
    return cast(
        JsonDict,
        await self._json(
            "POST",
            url,
            json=payload,
            params={"date": request_date_text},
            headers=lambda: self._tariff_headers(write=True),
        ),
    )


async def site_tariff(self: EnphaseEVClient) -> JsonDict:
    """Return site import/export tariff configuration."""

    url = f"{BASE_URL}/service/tariff/tariff-ms/systems/{self._site}/tariff"
    return cast(
        JsonDict,
        await self._json(
            "GET",
            url,
            params={"include-site-details": "true"},
            headers=self._tariff_headers,
        ),
    )


async def site_tariff_rates(
    self: EnphaseEVClient,
    *,
    rate_type: str,
    request_date: date | datetime | str | None = None,
) -> JsonDict:
    """Return dated tariff rates for a site tariff branch."""

    if request_date is None:
        request_date_text = date.today().isoformat()
    elif isinstance(request_date, datetime):
        request_date_text = request_date.date().isoformat()
    elif isinstance(request_date, date):
        request_date_text = request_date.isoformat()
    else:
        request_date_text = str(request_date)
    url = f"{BASE_URL}/service/tariff/tariff-ms/systems/{self._site}/tariffs"
    return cast(
        JsonDict,
        await self._json(
            "GET",
            url,
            params={
                "rateType": str(rate_type).upper(),
                "date": request_date_text,
                "includeUtility": "",
            },
            headers=self._tariff_headers,
        ),
    )


async def site_tariff_bundle(self: EnphaseEVClient) -> tuple[JsonDict, JsonDict]:
    """Return billing details and tariff configuration for the site."""

    try:
        async with asyncio.TaskGroup() as task_group:
            billing_task = task_group.create_task(
                self.site_tariff_billing_details(),
                name="enphase_ev_site_tariff_billing",
            )
            tariff_task = task_group.create_task(
                self.site_tariff(),
                name="enphase_ev_site_tariff_config",
            )
    except ExceptionGroup as err:
        raise err.exceptions[0] from err
    return billing_task.result(), tariff_task.result()


async def site_tariff_update(
    self: EnphaseEVClient, payload: dict[str, Any]
) -> JsonDict:
    """Update site import/export tariff configuration."""

    _token, user_id = self._battery_config_auth_context()
    url = f"{BASE_URL}/service/tariff/tariff-ms/systems/{self._site}/tariff"
    params = {"user-id": user_id} if user_id else None
    return cast(
        JsonDict,
        await self._json(
            "PUT",
            url,
            json=payload,
            params=params,
            headers=lambda: self._tariff_headers(write=True),
        ),
    )


async def notify_tariff_change(self: EnphaseEVClient) -> JsonDict:
    """Notify the EVSE scheduler service that site tariff data changed."""

    url = (
        f"{BASE_URL}/service/evse_scheduler/api/v1/siteConfig/"
        f"{self._site}/tariff_change"
    )
    return cast(
        JsonDict,
        await self._json(
            "PUT",
            url,
            json=None,
            headers=lambda: self._tariff_headers(write=True),
        ),
    )


async def lifetime_energy(self: EnphaseEVClient) -> JsonDict | None:
    """Return lifetime energy buckets for the configured site.

    GET /pv/systems/<site_id>/lifetime_energy
    """
    url = f"{BASE_URL}/pv/systems/{self._site}/lifetime_energy"
    try:
        data = await self._json("GET", url, headers=self._layout_headers)
    except aiohttp.ClientResponseError as err:
        if is_site_energy_unavailable_error(err.message, err.status, url):
            raise SiteEnergyUnavailable(str(err)) from err
        raise
    return self._normalize_lifetime_energy_payload(data)


async def weather(self: EnphaseEVClient, *, locale: str) -> JsonDict:
    """Return the current weather reported for the configured site.

    GET /systems/<site_id>/weather.json?locale=<locale>
    """

    return await api_site_surface.weather(self, base_url=BASE_URL, locale=locale)


async def latest_power(self: EnphaseEVClient) -> dict[str, object] | None:
    """Return the latest site power sample for the configured site.

    GET /app-api/<site_id>/get_latest_power
    """

    return await api_site_surface.latest_power(self, base_url=BASE_URL)


async def show_livestream(
    self: EnphaseEVClient, *, allow_reauth: bool = True
) -> dict[str, object] | None:
    """Return live-status/vitals capability flags when available."""

    return await api_site_surface.show_livestream(
        self,
        base_url=BASE_URL,
        allow_reauth=allow_reauth,
        unauthorized_error=Unauthorized,
        invalid_payload_error=InvalidPayloadError,
        optional_non_json=_is_optional_non_json_payload,
    )


async def site_livestream_authorizer(
    self: EnphaseEVClient,
    serial_num: str,
    *,
    live_debug: bool = False,
    allow_reauth: bool = True,
) -> dict[str, object] | None:
    """Return signed AWS IoT connection details for the site live stream."""

    return await api_site_surface.livestream_authorizer(
        self,
        serial_num,
        base_url=BASE_URL,
        live_debug=live_debug,
        allow_reauth=allow_reauth,
        unauthorized_error=Unauthorized,
        invalid_payload_error=InvalidPayloadError,
        optional_non_json=_is_optional_non_json_payload,
    )


async def site_livestream_payload(
    self: EnphaseEVClient,
    serial_num: str,
    *,
    live_debug: bool = False,
    timeout_s: float = 15.0,
    allow_reauth: bool = True,
) -> dict[str, object] | None:
    """Read and decode one MQTT payload from the signed site live stream."""

    authorizer = await self.site_livestream_authorizer(
        serial_num,
        live_debug=live_debug,
        allow_reauth=allow_reauth,
    )
    if not isinstance(authorizer, dict):
        return None
    topic_key = "live_debug_topic" if live_debug else "live_stream_topic"
    topic = self._coerce_text(authorizer.get(topic_key))
    endpoint = self._coerce_text(authorizer.get("aws_iot_endpoint"))
    username = self._site_livestream_mqtt_username(authorizer)
    if topic is None or endpoint is None or username is None:
        return None
    payload = await self._read_mqtt_websocket_payload(
        endpoint,
        topic,
        username,
        timeout_s=timeout_s,
    )
    decoded = self._decode_site_livestream_payload(payload)
    return decoded if isinstance(decoded, dict) else None


async def evse_timeseries_daily_energy(
    self: EnphaseEVClient,
    *,
    start_date: str | date | datetime | None = None,
    request_id: str | None = None,
    username: str | None = None,
) -> dict[str, dict[str, object]] | None:
    """Return EVSE daily timeseries keyed by charger serial."""

    request_id = request_id or str(uuid.uuid4())
    if username is None:
        username = self._session_history_username()
    start_date_key = self._parse_evse_timeseries_date_key(start_date)
    if start_date_key is None:
        start_date_key = datetime.now(timezone.utc).date().isoformat()
    query = {
        "site_id": self._site,
        "source": "evse",
        "requestId": request_id,
        "start_date": start_date_key,
    }
    if username:
        query["username"] = username
    url = URL(f"{BASE_URL}/service/timeseries/evse/timeseries/daily_energy").with_query(
        query
    )
    headers = partial(self._evse_timeseries_headers, request_id, username)
    try:
        data = await self._json("GET", str(url), headers=headers)
    except aiohttp.ClientResponseError as err:
        if is_evse_timeseries_unavailable_error(err.message, err.status, url):
            raise EVSETimeseriesUnavailable(str(err)) from err
        raise
    return self._normalize_evse_timeseries_payload(data, daily=True)


async def evse_timeseries_lifetime_energy(
    self: EnphaseEVClient,
    *,
    request_id: str | None = None,
    username: str | None = None,
) -> dict[str, dict[str, object]] | None:
    """Return EVSE lifetime timeseries keyed by charger serial."""

    request_id = request_id or str(uuid.uuid4())
    if username is None:
        username = self._session_history_username()
    query = {"site_id": self._site, "source": "evse", "requestId": request_id}
    if username:
        query["username"] = username
    url = URL(
        f"{BASE_URL}/service/timeseries/evse/timeseries/lifetime_energy"
    ).with_query(query)
    headers = partial(self._evse_timeseries_headers, request_id, username)
    try:
        data = await self._json("GET", str(url), headers=headers)
    except aiohttp.ClientResponseError as err:
        if is_evse_timeseries_unavailable_error(err.message, err.status, url):
            raise EVSETimeseriesUnavailable(str(err)) from err
        raise
    return self._normalize_evse_timeseries_payload(data, daily=False)


async def hems_consumption_lifetime(self: EnphaseEVClient) -> JsonDict | None:
    """Return HEMS lifetime consumption buckets when available.

    GET /systems/<site_id>/hems_consumption_lifetime
    """

    url = f"{BASE_URL}/systems/{self._site}/hems_consumption_lifetime"
    try:
        data = await self._json(
            "GET",
            url,
            headers=self._systems_json_headers,
            log_invalid_payload=False,
            allow_reauth=False,
        )
        self._hems_site_supported = True
    except Unauthorized:
        _LOGGER.debug(
            "HEMS lifetime endpoint unavailable for site %s (unauthorized)",
            redact_site_id(self._site),
        )
        raise
    except InvalidPayloadError as err:
        if _is_optional_non_json_payload(err):
            _LOGGER.debug(
                "HEMS lifetime endpoint unavailable for site %s (%s)",
                redact_site_id(self._site),
                redact_text(err.summary, site_ids=(self._site,)),
            )
            return None
        self._log_invalid_payload(err)
        raise
    except aiohttp.ClientResponseError as err:
        if err.status in (401, 403):
            _LOGGER.debug(
                "HEMS lifetime endpoint auth failure for site %s (status=%s)",
                redact_site_id(self._site),
                err.status,
            )
            raise
        if err.status == 404 or _is_hems_invalid_site_error(err):
            if _is_hems_invalid_site_error(err):
                self._hems_site_supported = False
            _LOGGER.debug(
                "HEMS lifetime endpoint unavailable for site %s (status=%s)",
                redact_site_id(self._site),
                err.status,
            )
            return None
        raise
    return self._normalize_lifetime_energy_payload(data)


async def hems_heatpump_state(
    self: EnphaseEVClient, device_uid: str, *, timezone: str | None = None
) -> JsonDict | None:
    """Return HEMS heat-pump runtime state when available."""

    device_uid = str(device_uid or "").strip()
    if not device_uid:
        return None
    url = URL(
        f"https://hems-integration.enphaseenergy.com/api/v1/hems/{self._site}/heatpump/{device_uid}/state"
    )
    if timezone:
        url = url.update_query({"timezone": str(timezone).strip()})
    try:
        data = await self._json(
            "GET",
            str(url),
            headers=self._hems_headers,
            allow_reauth=False,
        )
        self._hems_site_supported = True
    except Unauthorized:
        _LOGGER.debug(
            "HEMS heat pump state endpoint unavailable for site %s (unauthorized)",
            redact_site_id(self._site),
        )
        raise
    except InvalidPayloadError as err:
        if _is_optional_non_json_payload(err):
            _LOGGER.debug(
                "HEMS heat pump state endpoint unavailable for site %s (%s)",
                redact_site_id(self._site),
                redact_text(err.summary, site_ids=(self._site,)),
            )
            return None
        raise
    except aiohttp.ClientResponseError as err:
        if err.status in (401, 403):
            _LOGGER.debug(
                "HEMS heat pump state endpoint auth failure for site %s (status=%s)",
                redact_site_id(self._site),
                err.status,
            )
            raise
        if err.status == 404 or _is_hems_invalid_site_error(err):
            if _is_hems_invalid_site_error(err):
                self._hems_site_supported = False
            _LOGGER.debug(
                "HEMS heat pump state endpoint unavailable for site %s (status=%s)",
                redact_site_id(self._site),
                err.status,
            )
            return None
        raise
    return self._normalize_hems_heatpump_state_payload(data)


async def hems_energy_consumption(
    self: EnphaseEVClient,
    *,
    start_at: str,
    end_at: str,
    timezone: str,
    step: str = "P1D",
) -> JsonDict | None:
    """Return HEMS daily device energy-consumption buckets when available."""

    url = str(
        URL(
            f"https://hems-integration.enphaseenergy.com/api/v1/hems/{self._site}/energy-consumption"
        ).update_query(
            {
                "from": start_at,
                "to": end_at,
                "timezone": timezone,
                "step": step,
            }
        )
    )
    try:
        data = await self._json(
            "GET",
            url,
            headers=self._hems_headers,
            allow_reauth=False,
        )
        self._hems_site_supported = True
    except Unauthorized:
        _LOGGER.debug(
            "HEMS energy consumption endpoint unavailable for site %s (unauthorized)",
            redact_site_id(self._site),
        )
        raise
    except InvalidPayloadError as err:
        if _is_optional_non_json_payload(err):
            _LOGGER.debug(
                "HEMS energy consumption endpoint unavailable for site %s (%s)",
                redact_site_id(self._site),
                redact_text(err.summary, site_ids=(self._site,)),
            )
            return None
        raise
    except aiohttp.ClientResponseError as err:
        if err.status in (401, 403):
            _LOGGER.debug(
                "HEMS energy consumption endpoint auth failure for site %s (status=%s)",
                redact_site_id(self._site),
                err.status,
            )
            raise
        if err.status == 404 or _is_hems_invalid_site_error(err):
            if _is_hems_invalid_site_error(err):
                self._hems_site_supported = False
            _LOGGER.debug(
                "HEMS energy consumption endpoint unavailable for site %s (status=%s)",
                redact_site_id(self._site),
                err.status,
            )
            return None
        raise
    return self._normalize_hems_energy_consumption_payload(data)


async def pv_system_today(
    self: EnphaseEVClient, *, allow_reauth: bool = True
) -> JsonDict | None:
    """Return the site today payload when available."""

    url = f"{BASE_URL}/pv/systems/{self._site}/today"
    try:
        data = await self._json(
            "GET",
            url,
            headers=self._today_json_headers,
            allow_reauth=allow_reauth,
        )
    except Unauthorized:
        if not allow_reauth:
            raise
        _LOGGER.debug(
            "PV site today endpoint unavailable for site %s (unauthorized)",
            redact_site_id(self._site),
        )
        return None
    except InvalidPayloadError as err:
        if _is_optional_non_json_payload(err):
            _LOGGER.debug(
                "PV site today endpoint unavailable for site %s (%s)",
                redact_site_id(self._site),
                redact_text(err.summary, site_ids=(self._site,)),
            )
            return None
        raise
    except aiohttp.ClientResponseError as err:
        if err.status in (401, 403, 404):
            if not allow_reauth and err.status in (401, 403):
                raise
            _LOGGER.debug(
                "PV site today endpoint unavailable for site %s (status=%s)",
                redact_site_id(self._site),
                err.status,
            )
            return None
        raise
    return self._normalize_pv_system_today_payload(data)


async def heat_pump_events_json(
    self: EnphaseEVClient, device_uid: str
) -> JsonDict | list[Any] | None:
    """Return per-device HEMS heat-pump events payload when available."""

    if not str(device_uid or "").strip():
        return None
    url = str(
        URL(f"{BASE_URL}/systems/{self._site}/heat_pump/{device_uid}/events.json")
    )
    try:
        data = await self._json(
            "GET",
            url,
            headers=self._systems_json_headers,
            log_invalid_payload=False,
            allow_reauth=False,
        )
    except Unauthorized:
        _LOGGER.debug(
            "Heat pump events endpoint unavailable for site %s (unauthorized)",
            redact_site_id(self._site),
        )
        raise
    except InvalidPayloadError as err:
        if _is_optional_non_json_payload(err):
            _LOGGER.debug(
                "Heat pump events endpoint unavailable for site %s (%s)",
                redact_site_id(self._site),
                redact_text(err.summary, site_ids=(self._site,)),
            )
            return None
        if _is_optional_html_payload(err):
            raise OptionalEndpointUnavailable(err.summary) from err
        self._log_invalid_payload(err)
        raise
    except aiohttp.ClientResponseError as err:
        if err.status in (401, 403):
            _LOGGER.debug(
                "Heat pump events endpoint auth failure for site %s (status=%s)",
                redact_site_id(self._site),
                err.status,
            )
            raise
        if err.status == 404 or _is_hems_invalid_site_error(err):
            _LOGGER.debug(
                "Heat pump events endpoint unavailable for site %s (status=%s)",
                redact_site_id(self._site),
                err.status,
            )
            return None
        raise
    if isinstance(data, (dict, list)):
        return data
    return None


async def iq_er_events_json(
    self: EnphaseEVClient, device_uid: str
) -> JsonDict | list[Any] | None:
    """Return per-device HEMS IQ Energy Router events payload when available."""

    if not str(device_uid or "").strip():
        return None
    url = str(URL(f"{BASE_URL}/systems/{self._site}/iq_er/{device_uid}/events.json"))
    try:
        data = await self._json(
            "GET",
            url,
            headers=self._systems_json_headers,
            log_invalid_payload=False,
            allow_reauth=False,
        )
    except Unauthorized:
        _LOGGER.debug(
            "IQ Energy Router events endpoint unavailable for site %s (unauthorized)",
            redact_site_id(self._site),
        )
        raise
    except InvalidPayloadError as err:
        if _is_optional_non_json_payload(err):
            _LOGGER.debug(
                "IQ Energy Router events endpoint unavailable for site %s (%s)",
                redact_site_id(self._site),
                redact_text(err.summary, site_ids=(self._site,)),
            )
            return None
        if _is_optional_html_payload(err):
            raise OptionalEndpointUnavailable(err.summary) from err
        self._log_invalid_payload(err)
        raise
    except aiohttp.ClientResponseError as err:
        if err.status in (401, 403):
            _LOGGER.debug(
                "IQ Energy Router events endpoint auth failure for site %s (status=%s)",
                redact_site_id(self._site),
                err.status,
            )
            raise
        if err.status == 404 or _is_hems_invalid_site_error(err):
            _LOGGER.debug(
                "IQ Energy Router events endpoint unavailable for site %s (status=%s)",
                redact_site_id(self._site),
                err.status,
            )
            return None
        raise
    if isinstance(data, (dict, list)):
        return data
    return None


async def summary_v2(self: EnphaseEVClient) -> list[JsonDict] | None:
    """Fetch charger summary v2 list.

    GET /service/evse_controller/api/v2/<site_id>/ev_chargers/summary?filter_retired=true
    Returns a list of charger objects with serialNumber and other properties.
    """
    url = f"{BASE_URL}/service/evse_controller/api/v2/{self._site}/ev_chargers/summary?filter_retired=true"
    try:
        data = await self._json("GET", url, headers=self._today_headers)
    except InvalidPayloadError as err:
        if _is_optional_non_json_payload(err) or _is_optional_html_payload(err):
            raise OptionalEndpointUnavailable(err.summary) from err
        raise
    try:
        rows = data.get("data")
        return cast(list[JsonDict], rows) if isinstance(rows, list) else []
    except Exception:
        return None


async def evse_fw_details(self: EnphaseEVClient) -> list[dict[str, Any]] | None:
    """Fetch EVSE firmware details for the current site.

    GET /service/evse_management/fwDetails/<site_id>
    Returns a list of charger firmware-detail objects keyed by serialNumber.
    """

    url = f"{BASE_URL}/service/evse_management/fwDetails/{self._site}"
    try:
        data = await self._json(
            "GET",
            url,
            headers=self._today_headers,
            mark_payload_success=False,
        )
    except Unauthorized:
        _LOGGER.debug(
            "EVSE firmware details endpoint unavailable for site %s (unauthorized)",
            redact_site_id(self._site),
        )
        return None
    except aiohttp.ClientResponseError as err:
        if err.status in (403, 404):
            _LOGGER.debug(
                "EVSE firmware details endpoint unavailable for site %s (status=%s)",
                redact_site_id(self._site),
                err.status,
            )
            return None
        raise

    endpoint = f"/service/evse_management/fwDetails/{self._site}"
    if data is None:
        self._mark_payload_healthy(endpoint)
        return []
    if isinstance(data, list):
        self._mark_payload_healthy(endpoint)
        return [item for item in data if isinstance(item, dict)]
    raise self._invalid_payload_error(
        endpoint=endpoint,
        summary="EVSE firmware details payload must be a list",
        failure_kind="shape",
        payload=data,
    )


async def evse_feature_flags(
    self: EnphaseEVClient, *, country: str | None = None
) -> JsonDict | None:
    """Return EVSE feature flags and UI gating details for the site.

    GET /service/evse_management/api/v1/config/feature-flags?site_id=<site_id>[&country=<country>]
    """

    url = str(
        URL(
            f"{BASE_URL}/service/evse_management/api/v1/config/feature-flags"
        ).update_query(
            {
                key: value
                for key, value in {
                    "site_id": self._site,
                    "country": country,
                }.items()
                if value is not None
            }
        )
    )
    try:
        data = await self._json("GET", url, headers=self._today_headers)
    except EnphaseLoginWallUnauthorized:
        raise
    except Unauthorized:
        _LOGGER.debug(
            "EVSE feature flags endpoint unavailable for site %s (unauthorized)",
            redact_site_id(self._site),
        )
        return None
    except aiohttp.ClientResponseError as err:
        if err.status in (401, 403, 404):
            _LOGGER.debug(
                "EVSE feature flags endpoint unavailable for site %s (status=%s)",
                redact_site_id(self._site),
                err.status,
            )
            return None
        raise
    return data if isinstance(data, dict) else None


async def devices_inventory(self: EnphaseEVClient) -> JsonDict:
    """Return site device inventory grouped by hardware type.

    GET /app-api/<site_id>/devices.json
    """
    url = f"{BASE_URL}/app-api/{self._site}/devices.json"
    data = await self._json("GET", url, headers=self._history_headers)
    if isinstance(data, dict):
        return data
    return {}


async def phase_map_multiple_envoy(self: EnphaseEVClient) -> JsonDict | None:
    """Return per-gateway phase and topology metadata for the site.

    GET /app-api/<site_id>/phase_map_multiple_envoy
    """

    url = f"{BASE_URL}/app-api/{self._site}/phase_map_multiple_envoy"
    try:
        data = await self._json("GET", url, headers=self._history_headers)
    except InvalidPayloadError as err:
        if _is_optional_non_json_payload(err) or _is_optional_html_payload(err):
            raise OptionalEndpointUnavailable(err.summary) from err
        raise
    return data if isinstance(data, dict) else None


async def devices_tree(self: EnphaseEVClient) -> JsonDict | None:
    """Return the system dashboard device hierarchy when available.

    GET /service/system_dashboard/api_internal/dashboard/sites/<site_id>/devices-tree
    Fallback: GET /pv/systems/<site_id>/system_dashboard/devices-tree
    """

    modern_url = (
        f"{BASE_URL}/service/system_dashboard/api_internal/dashboard/sites/"
        f"{self._site}/devices-tree"
    )
    legacy_url = f"{BASE_URL}/pv/systems/{self._site}/system_dashboard/devices-tree"
    return await self._system_dashboard_get(modern_url, legacy_url)


async def system_dashboard_summary(
    self: EnphaseEVClient, *, allow_reauth: bool = True
) -> JsonDict | None:
    """Return the system dashboard capability summary when available.

    GET /service/system_dashboard/api_internal/cs/sites/<site_id>/summary
    """

    url = (
        f"{BASE_URL}/service/system_dashboard/api_internal/cs/sites/"
        f"{self._site}/summary"
    )
    headers = self._system_dashboard_headers
    try:
        data = await self._json(
            "GET",
            url,
            headers=headers,
            allow_reauth=allow_reauth,
        )
    except Exception as err:  # noqa: BLE001
        if not allow_reauth and (
            isinstance(err, Unauthorized)
            or (
                isinstance(err, aiohttp.ClientResponseError)
                and err.status in (401, 403)
            )
        ):
            raise
        if self._system_dashboard_is_optional_error(err):
            return None
        raise

    if not isinstance(data, dict):
        return None

    self._system_dashboard_summary_payload = dict(data)
    is_hems = data.get("is_hems")
    if isinstance(is_hems, bool):
        self._hems_site_supported = is_hems

    return data


async def system_dashboard_events(self: EnphaseEVClient) -> JsonDict | None:
    """Return current System Dashboard event rows and lookup catalogs.

    GET /service/system_dashboard/api_internal/cs/sites/<site_id>/events
    """

    base_url = URL(
        f"{BASE_URL}/service/system_dashboard/api_internal/cs/sites/"
        f"{self._site}/events"
    )
    query = {
        "range": "today",
        "cassandra_toggle": "false",
        "filter_columns": (
            "serial_number,device_type,event_date,cleared_date,"
            "event_type,event_state,details,updated_at,alarm_id"
        ),
        "serial_numbers": "",
        "type": "table",
        "event_state": "default",
        "per_page": str(_SYSTEM_EVENTS_PAGE_SIZE),
    }
    merged: JsonDict | None = None
    merged_events: list[object] = []
    last_page_full = False
    for page in range(1, _SYSTEM_EVENTS_MAX_PAGES + 1):
        url = str(base_url.update_query({**query, "page": str(page)}))
        try:
            data = await self._json(
                "GET",
                url,
                headers=self._system_dashboard_headers,
            )
        except Exception as err:  # noqa: BLE001
            if self._system_dashboard_is_optional_error(err):
                return None
            raise
        if not isinstance(data, dict):
            return None
        events = data.get("events")
        if not isinstance(events, list):
            return None
        if merged is None:
            merged = dict(data)
        else:
            for key in ("event_types", "event_states", "event_severities"):
                if key not in merged and key in data:
                    merged[key] = data[key]
        merged_events.extend(events)
        last_page_full = len(events) >= _SYSTEM_EVENTS_PAGE_SIZE
        if not last_page_full:
            break
    if merged is None:  # pragma: no cover - loop always executes
        return None
    merged["events"] = merged_events
    merged["_enphase_ev_truncated"] = (
        page == _SYSTEM_EVENTS_MAX_PAGES and last_page_full
    )
    return merged


async def homeowner_events_page(
    self: EnphaseEVClient,
    *,
    next_cursor: str = "start",
    page_size: int = 200,
    locale: str = "en",
) -> JsonDict | None:
    """Return one cursor-paginated homeowner event-history page.

    GET /service/events-platform-service/v1.0/<site_id>/events/homeowner
    """

    url = str(
        URL(
            f"{BASE_URL}/service/events-platform-service/v1.0/"
            f"{self._site}/events/homeowner"
        ).update_query(
            {
                "next": str(next_cursor or "start"),
                "page_size": str(max(1, min(int(page_size), 200))),
                "locale": str(locale or "en"),
            }
        )
    )
    try:
        data = await self._json(
            "GET",
            url,
            headers=self._homeowner_events_headers,
        )
    except Exception as err:  # noqa: BLE001
        if self._system_dashboard_is_optional_error(err) or isinstance(
            err, InvalidPayloadError
        ):
            return None
        raise
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        return None
    cursor = data.get("next")
    if cursor is not None and (
        isinstance(cursor, (dict, list, tuple, set, bool)) or not str(cursor).strip()
    ):
        return None
    return data


async def system_dashboard_standing_alarms(self: EnphaseEVClient) -> JsonDict | None:
    """Return current System Dashboard standing alarms.

    GET /service/system_dashboard/api_internal/dashboard/sites/<site_id>/alarms
    """

    base_url = URL(
        f"{BASE_URL}/service/system_dashboard/api_internal/dashboard/sites/"
        f"{self._site}/alarms"
    )
    query = {
        "range": "today",
        "filter_columns": ("id,severity,type,serial_num,description,first_set"),
        "type": "table",
        "per_page": str(_SYSTEM_ALARMS_PAGE_SIZE),
    }
    merged: JsonDict | None = None
    merged_alarms: list[object] = []
    last_page_full = False
    for page in range(1, _SYSTEM_ALARMS_MAX_PAGES + 1):
        url = str(base_url.update_query({**query, "page": str(page)}))
        try:
            data = await self._json(
                "GET",
                url,
                headers=self._system_dashboard_headers,
            )
        except Exception as err:  # noqa: BLE001
            if self._system_dashboard_is_optional_error(err):
                return None
            raise
        if not isinstance(data, dict):
            return None
        alarms = data.get("alarms")
        if not isinstance(alarms, list):
            return None
        if merged is None:
            merged = dict(data)
        merged_alarms.extend(alarms)
        last_page_full = len(alarms) >= _SYSTEM_ALARMS_PAGE_SIZE
        if not last_page_full:
            break
    if merged is None:  # pragma: no cover - loop always executes
        return None
    merged["alarms"] = merged_alarms
    merged["_enphase_ev_truncated"] = (
        page == _SYSTEM_ALARMS_MAX_PAGES and last_page_full
    )
    return merged


async def devices_details(self: EnphaseEVClient, type_key: str) -> JsonDict | None:
    """Return system dashboard per-type device details when available.

    GET /service/system_dashboard/api_internal/dashboard/sites/<site_id>/devices_details?type=<observed_type>
    Fallback: GET /pv/systems/<site_id>/system_dashboard/devices_details?type=<observed_type>
    """

    normalized = _system_dashboard_query_type(type_key)
    if not normalized:
        return None
    modern_url = str(
        URL(
            f"{BASE_URL}/service/system_dashboard/api_internal/dashboard/sites/{self._site}/devices_details"
        ).update_query({"type": normalized})
    )
    legacy_url = str(
        URL(
            f"{BASE_URL}/pv/systems/{self._site}/system_dashboard/devices_details"
        ).update_query({"type": normalized})
    )
    return await self._system_dashboard_get(modern_url, legacy_url)


async def system_dashboard_master_data(self: EnphaseEVClient) -> JsonDict | None:
    """Return the system-dashboard device and parameter catalogs.

    GET /service/system_dashboard/api_internal/cs/sites/<site_id>/data/master-data
    """

    url = (
        f"{BASE_URL}/service/system_dashboard/api_internal/cs/sites/"
        f"{self._site}/data/master-data"
    )
    try:
        data = await self._json("GET", url, headers=self._system_dashboard_headers)
    except Exception as err:  # noqa: BLE001
        if self._system_dashboard_is_optional_error(err):
            return None
        raise
    return data if isinstance(data, dict) else None


async def system_dashboard_envoy_inverters(
    self: EnphaseEVClient, gateway_serial: str
) -> JsonDict | None:
    """Return flattened microinverter inventory for one gateway."""

    serial = str(gateway_serial).strip()
    if not serial:
        return None
    url = str(
        URL(
            f"{BASE_URL}/service/system_dashboard/api_internal/dashboard/sites/"
            f"{self._site}/envoy_inverters"
        ).update_query({"serial_number": serial})
    )
    try:
        data = await self._json("GET", url, headers=self._system_dashboard_headers)
    except Exception as err:  # noqa: BLE001
        if self._system_dashboard_is_optional_error(err):
            return None
        raise
    return data if isinstance(data, dict) else None


async def system_dashboard_data_columns(
    self: EnphaseEVClient, gateway_serial: str
) -> JsonDict | None:
    """Return device-level parameter column metadata for one gateway."""

    serial = str(gateway_serial).strip()
    if not serial:
        return None
    url = str(
        URL(
            f"{BASE_URL}/service/system_dashboard/api_internal/cs/sites/"
            f"{self._site}/data/columns"
        ).update_query({"serial_num": serial, "type": "device_level"})
    )
    try:
        data = await self._json("GET", url, headers=self._system_dashboard_headers)
    except Exception as err:  # noqa: BLE001
        if self._system_dashboard_is_optional_error(err):
            return None
        raise
    return data if isinstance(data, dict) else None


async def system_dashboard_parameter_view(
    self: EnphaseEVClient,
    serial_numbers: list[str] | tuple[str, ...],
    parameter_id: str,
    *,
    per_page: int = 500,
    page: int = 1,
    range_name: str = "today",
    start_date: str = "",
    end_date: str = "",
    sort_by_date: str = "desc",
) -> JsonDict | None:
    """Return one parameter for many devices in a single dashboard request."""

    serials = tuple(
        dict.fromkeys(
            str(serial).strip() for serial in serial_numbers if str(serial).strip()
        )
    )
    parameter = str(parameter_id).strip()
    if not serials or not parameter:
        return None
    url = str(
        URL(
            f"{BASE_URL}/service/system_dashboard/api_internal/cs/sites/"
            f"{self._site}/data/parameter-view"
        ).update_query(
            {
                "serial_numbers": ",".join(serials),
                "per_page": max(1, int(per_page)),
                "page": max(1, int(page)),
                "range": str(range_name),
                "parameter_id": parameter,
                "start_date": str(start_date),
                "end_date": str(end_date),
                "sort_by_date": str(sort_by_date),
            }
        )
    )
    try:
        data = await self._json("GET", url, headers=self._system_dashboard_headers)
    except Exception as err:  # noqa: BLE001
        if self._system_dashboard_is_optional_error(err):
            return None
        raise
    return data if isinstance(data, dict) else None


async def hems_devices(
    self: EnphaseEVClient, *, refresh_data: bool = False
) -> JsonDict | None:
    """Return dedicated HEMS device inventory when available.

    GET https://hems-integration.enphaseenergy.com/api/v1/hems/<site_id>/hems-devices
    """

    url = str(
        URL(
            f"https://hems-integration.enphaseenergy.com/api/v1/hems/{self._site}/hems-devices"
        ).update_query({"refreshData": str(bool(refresh_data)).lower()})
    )
    try:
        data = await self._json(
            "GET",
            url,
            headers=self._hems_headers,
            allow_reauth=False,
        )
        self._hems_site_supported = True
    except Unauthorized:
        _LOGGER.debug(
            "HEMS devices endpoint unavailable for site %s (unauthorized)",
            redact_site_id(self._site),
        )
        raise
    except InvalidPayloadError as err:
        if _is_optional_non_json_payload(err):
            _LOGGER.debug(
                "HEMS devices endpoint unavailable for site %s (%s)",
                redact_site_id(self._site),
                redact_text(err.summary, site_ids=(self._site,)),
            )
            return None
        raise
    except aiohttp.ClientResponseError as err:
        if err.status in (401, 403):
            _LOGGER.debug(
                "HEMS devices endpoint auth failure for site %s (status=%s)",
                redact_site_id(self._site),
                err.status,
            )
            raise
        if err.status == 404 or _is_hems_invalid_site_error(err):
            if _is_hems_invalid_site_error(err):
                self._hems_site_supported = False
            _LOGGER.debug(
                "HEMS devices endpoint unavailable for site %s (status=%s)",
                redact_site_id(self._site),
                err.status,
            )
            return None
        raise
    return data if isinstance(data, dict) else None


async def grid_control_check(self: EnphaseEVClient) -> JsonDict:
    """Return site-level grid control eligibility guard flags.

    GET /app-api/<site_id>/grid_control_check.json
    """

    url = f"{BASE_URL}/app-api/{self._site}/grid_control_check.json"
    data = await self._json("GET", url, headers=self._history_headers)
    if isinstance(data, dict):
        return data
    return {}


async def off_grid_due_to_grid_outage(self: EnphaseEVClient) -> JsonDict:
    """Return live grid-outage/off-grid context for the site.

    GET /app-api/<site_id>/off_grid_due_to_grid_outage
    """

    url = f"{BASE_URL}/app-api/{self._site}/off_grid_due_to_grid_outage"
    data = await self._json("GET", url, headers=self._history_headers)
    if isinstance(data, dict):
        return data
    return {}


async def request_grid_toggle_otp(self: EnphaseEVClient) -> JsonDict:
    """Request OTP delivery for a site grid-mode toggle.

    GET /app-api/<site_id>/grid_toggle_otp.json
    """

    url = f"{BASE_URL}/app-api/{self._site}/grid_toggle_otp.json"
    headers = partial(self._control_request_headers, self._history_headers)
    data = await self._json("GET", url, headers=headers)
    if isinstance(data, dict):
        return data
    return {}


async def validate_grid_toggle_otp(self: EnphaseEVClient, otp: str) -> bool:
    """Validate a grid-mode OTP for the configured site.

    POST /app-api/grid_toggle_otp.json
    """

    url = f"{BASE_URL}/app-api/grid_toggle_otp.json"
    headers = partial(self._control_request_headers, self._history_form_headers)
    payload = {"otp": str(otp), "site_id": str(self._site)}
    data = await self._json("POST", url, data=payload, headers=headers)
    if not isinstance(data, dict):
        return False
    return data.get("valid") is True


async def set_grid_state(
    self: EnphaseEVClient, envoy_serial_number: str, state: int
) -> JsonDict:
    """Submit a grid relay state-change request.

    POST /pv/settings/grid_state.json
    """

    url = f"{BASE_URL}/pv/settings/grid_state.json"
    headers = partial(self._control_request_headers, self._history_form_headers)
    payload = {
        "envoy_serial_number": str(envoy_serial_number),
        "state": int(state),
    }
    data = await self._json("POST", url, data=payload, headers=headers)
    if isinstance(data, dict):
        return data
    return {}


async def log_grid_change(
    self: EnphaseEVClient,
    envoy_serial_number: str,
    old_state: str,
    new_state: str,
) -> JsonDict:
    """Write grid relay transition audit metadata.

    POST /pv/settings/log_grid_change.json
    """

    url = f"{BASE_URL}/pv/settings/log_grid_change.json"
    headers = partial(self._control_request_headers, self._history_form_headers)
    payload = {
        "envoy_serial_number": str(envoy_serial_number),
        "old_state": str(old_state),
        "new_state": str(new_state),
    }
    data = await self._json("POST", url, data=payload, headers=headers)
    if isinstance(data, dict):
        return data
    return {}


async def battery_backup_history(self: EnphaseEVClient) -> JsonDict:
    """Return battery backup outage history for the site.

    GET /app-api/<site_id>/battery_backup_history.json
    """

    url = f"{BASE_URL}/app-api/{self._site}/battery_backup_history.json"
    data = await self._json("GET", url, headers=self._history_headers)
    if isinstance(data, dict):
        return data
    return {}


async def battery_status(self: EnphaseEVClient) -> JsonDict:
    """Return battery status payload used by the Enlighten battery card.

    GET /pv/settings/<site_id>/battery_status.json
    """

    url = f"{BASE_URL}/pv/settings/{self._site}/battery_status.json"
    data = await self._json("GET", url, headers=self._history_headers)
    if isinstance(data, dict):
        return data
    return {}


async def ac_battery_devices_page(
    self: EnphaseEVClient, *, status: str = "active"
) -> str:
    """Return the AC Battery devices page HTML for the site."""

    url = str(
        URL(f"{BASE_URL}/systems/{self._site}/devices").update_query({"status": status})
    )
    headers = partial(
        self._systems_html_headers,
        f"{BASE_URL}/systems/{self._site}/devices?status={status}",
    )
    return await self._text("GET", url, headers=headers)


async def ac_battery_detail_page(self: EnphaseEVClient, battery_id: str) -> str:
    """Return the AC Battery detail page HTML."""

    url = f"{BASE_URL}/systems/{self._site}/ac_batteries/{battery_id}"
    headers = partial(
        self._systems_html_headers,
        f"{BASE_URL}/systems/{self._site}/devices?status=active",
    )
    return await self._text("GET", url, headers=headers)


async def ac_battery_events_page(self: EnphaseEVClient, battery_id: str) -> str:
    """Return the AC Battery events page HTML."""

    url = f"{BASE_URL}/systems/{self._site}/ac_batteries/{battery_id}/events"
    headers = partial(
        self._systems_html_headers,
        f"{BASE_URL}/systems/{self._site}/ac_batteries/{battery_id}",
    )
    return await self._text("GET", url, headers=headers)


async def ac_battery_show_stat_data(self: EnphaseEVClient, battery_id: str) -> str:
    """Return the AC Battery telemetry HTML fragment."""

    url = f"{BASE_URL}/systems/{self._site}/ac_batteries/{battery_id}/show_stat_data"

    def headers() -> dict[str, str]:
        return {
            **self._layout_headers(),
            "Accept": "*/*",
            "Referer": f"{BASE_URL}/systems/{self._site}/ac_batteries/{battery_id}",
        }

    return await self._text("GET", url, headers=headers)


async def set_ac_battery_sleep(
    self: EnphaseEVClient, battery_id: str, sleep_min_soc: int
) -> TextResponse:
    """Request AC Battery sleep mode using the Enlighten web route."""

    url = str(
        URL(
            f"{BASE_URL}/systems/{self._site}/ac_batteries/{battery_id}/sleep"
        ).update_query({"sleep_min_soc": int(sleep_min_soc)})
    )
    headers = partial(
        self._systems_html_headers,
        f"{BASE_URL}/systems/{self._site}/devices?status=active",
    )
    return await self._text_response(
        "GET",
        url,
        headers=headers,
        allow_redirects=False,
        expected_statuses=(302,),
    )


async def set_ac_battery_wake(self: EnphaseEVClient, battery_id: str) -> TextResponse:
    """Request AC Battery wake/cancel using the Enlighten web route."""

    url = f"{BASE_URL}/systems/{self._site}/ac_batteries/{battery_id}/wake"
    headers = partial(
        self._systems_html_headers,
        f"{BASE_URL}/systems/{self._site}/devices?status=active",
    )
    return await self._text_response(
        "GET",
        url,
        headers=headers,
        allow_redirects=False,
        expected_statuses=(302,),
    )


async def dry_contacts_settings(self: EnphaseEVClient) -> JsonDict:
    """Return dry-contact settings payload used by site settings views.

    GET /pv/settings/<site_id>/dry_contacts
    """

    url = f"{BASE_URL}/pv/settings/{self._site}/dry_contacts"
    data = await self._json("GET", url, headers=self._history_headers)
    if isinstance(data, dict):
        return data
    return {}


async def inverters_inventory(
    self: EnphaseEVClient,
    *,
    limit: int = 1000,
    offset: int = 0,
    search: str = "",
) -> JsonDict:
    """Return site inverter inventory used by legacy microinverter views.

    GET /app-api/<site_id>/inverters.json
    """

    url = URL(f"{BASE_URL}/app-api/{self._site}/inverters.json").with_query(
        {
            "limit": int(limit),
            "offset": int(offset),
            "search": str(search),
        }
    )
    data = await self._json("GET", str(url), headers=self._history_headers)
    if not isinstance(data, dict):
        return {}
    return data


async def inverter_status(self: EnphaseEVClient) -> dict[str, dict[str, Any]]:
    """Return inverter status map keyed by inverter id.

    GET /systems/<site_id>/inverter_status_x.json
    """

    url = f"{BASE_URL}/systems/{self._site}/inverter_status_x.json"
    data = await self._json("GET", url, headers=self._layout_headers)
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        key_text = str(key).strip()
        if not key_text:
            continue
        out[key_text] = dict(value)
    return out


async def inverter_production(
    self: EnphaseEVClient,
    *,
    start_date: str,
    end_date: str,
) -> JsonDict:
    """Return inverter production totals for a date range.

    GET /systems/<site_id>/inverter_data_x/energy.json?start_date=...&end_date=...
    """

    url = URL(
        f"{BASE_URL}/systems/{self._site}/inverter_data_x/energy.json"
    ).with_query({"start_date": str(start_date), "end_date": str(end_date)})
    data = await self._json("GET", str(url), headers=self._layout_headers)
    if not isinstance(data, dict):
        return {}
    production_raw = data.get("production")
    production: dict[str, float] = {}
    if isinstance(production_raw, dict):
        for key, value in production_raw.items():
            key_text = str(key).strip()
            if not key_text:
                continue
            try:
                production[key_text] = float(value)
            except (TypeError, ValueError):
                continue
    return {
        "production": production,
        "start_date": data.get("start_date"),
        "end_date": data.get("end_date"),
    }


async def session_history_filter_criteria(
    self: EnphaseEVClient,
    *,
    request_id: str | None = None,
    username: str | None = None,
) -> JsonDict:
    """Fetch session history filter criteria for a site."""

    request_id = request_id or str(uuid.uuid4())
    if username is None:
        username = self._session_history_username()
    query = {"source": "evse", "requestId": request_id}
    if username:
        query["username"] = username
    url = URL(
        f"{BASE_URL}/service/enho_historical_events_ms/{self._site}/filter_criteria"
    ).with_query(query)
    headers = partial(self._session_history_headers, request_id, username)
    return cast(JsonDict, await self._json("GET", str(url), headers=headers))


async def session_history(
    self: EnphaseEVClient,
    sn: str,
    *,
    start_date: str,
    end_date: str | None = None,
    offset: int = 0,
    limit: int = 20,
    timezone: str | None = None,
    request_id: str | None = None,
    username: str | None = None,
) -> JsonDict:
    """Fetch charging sessions for a charger between the provided dates.

    POST /service/enho_historical_events_ms/<site_id>/sessions/<sn>/history
    Dates must be formatted as DD-MM-YYYY in the site locale.
    """
    url = f"{BASE_URL}/service/enho_historical_events_ms/{self._site}/sessions/{sn}/history"
    request_id = request_id or str(uuid.uuid4())
    if username is None:
        username = self._session_history_username()
    payload: dict[str, Any] = {
        "source": "evse",
        "params": {
            "offset": int(offset),
            "limit": int(limit),
            "startDate": start_date,
            "endDate": end_date or start_date,
        },
    }
    if timezone:
        payload["params"]["timezone"] = timezone
    headers = partial(self._session_history_headers, request_id, username)
    try:
        return cast(
            JsonDict, await self._json("POST", url, json=payload, headers=headers)
        )
    except aiohttp.ClientResponseError as err:
        if is_session_history_unavailable_error(err.message, err.status, url):
            raise SessionHistoryUnavailable(str(err)) from err
        raise
