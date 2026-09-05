"""Evse surface for the stable Enphase client facade."""

from __future__ import annotations

import asyncio
import json
from functools import partial
from typing import TYPE_CHECKING, Any, Iterable, cast

import aiohttp

from ..const import (
    AUTH_APP_SETTING,
    AUTH_RFID_SETTING,
    BASE_URL,
    DEFAULT_CHARGE_LEVEL_SETTING,
    GREEN_BATTERY_SETTING,
)
from ..log_redaction import (
    redact_identifier,
    redact_text,
)
from .errors import (
    AuthSettingsUnavailable,
    ChargerConfigUnavailable,
    InvalidPayloadError,
    OptionalEndpointUnavailable,
    SchedulerUnavailable,
    Unauthorized,
    _is_optional_html_payload,
    _is_optional_non_json_payload,
    _scheduler_error_code,
)

if TYPE_CHECKING:
    from ..api import EnphaseEVClient

from .common import (
    _LOGGER,
    JsonDict,
    is_auth_settings_unavailable_error,
    is_scheduler_unavailable_error,
    validate_ocpp_trigger_message,
)


async def status(self: EnphaseEVClient) -> JsonDict:
    url = f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/status"
    endpoint = f"/service/evse_controller/{self._site}/ev_chargers/status"
    try:
        data = await self._json("GET", url, headers=self._today_headers)
    except InvalidPayloadError as err:
        if _is_optional_non_json_payload(err) or _is_optional_html_payload(err):
            raise OptionalEndpointUnavailable(err.summary) from err
        raise
    if not isinstance(data, dict):
        raise self._invalid_payload_error(
            endpoint=endpoint,
            summary="EVSE status payload must be an object",
            failure_kind="shape",
            payload=data,
        )

    # If response is { data: { chargers: [...] } }, map to evChargerData
    try:
        inner = data.get("data") if isinstance(data, dict) else None
        chargers = inner.get("chargers") if isinstance(inner, dict) else None
        if isinstance(chargers, list) and chargers:
            out = []
            for c in chargers:
                conn = (c.get("connectors") or [{}])[0]
                raw_session = c.get("session_d")
                sess = dict(raw_session) if isinstance(raw_session, dict) else {}
                connectors = c.get("connectors")
                if not connectors:
                    connectors = [conn] if conn else []
                # Derive start_time in seconds (strt_chrg appears in ms)
                start_raw = sess.get("start_time")
                from_strt_chrg = False
                if start_raw is None:
                    start_raw = sess.get("strt_chrg")
                    from_strt_chrg = start_raw is not None
                start_sec: int | None = None
                if isinstance(start_raw, (int, float)):
                    try:
                        start_val = int(start_raw)
                        if from_strt_chrg:
                            start_val = int(start_val / 1000)
                        elif start_val > 10**12:
                            start_val = start_val // 1000
                        start_sec = start_val
                    except Exception:
                        start_sec = None
                elif isinstance(start_raw, str):
                    text = start_raw.strip()
                    if text.isdigit():
                        try:
                            start_val = int(text)
                            if from_strt_chrg:
                                start_val = int(start_val / 1000)
                            elif start_val > 10**12:
                                start_val = start_val // 1000
                            start_sec = start_val
                        except Exception:
                            start_sec = None
                if start_sec is not None and sess.get("start_time") is None:
                    sess["start_time"] = start_sec
                sch_raw = c.get("sch_d")
                sch = dict(sch_raw) if isinstance(sch_raw, dict) else {}
                smart_ev = c.get("smartEV")
                if not isinstance(smart_ev, dict):
                    smart_ev = {}
                out.append(
                    {
                        "sn": c.get("sn"),
                        "name": c.get("name"),
                        "displayName": c.get("displayName"),
                        "connected": bool(c.get("connected")),
                        "pluggedIn": bool(c.get("pluggedIn") or conn.get("pluggedIn")),
                        "charging": bool(c.get("charging")),
                        "faulted": bool(c.get("faulted")),
                        "commissioned": c.get("commissioned"),
                        "mode": c.get("mode"),
                        "offGrid": c.get("offGrid"),
                        "offlineAt": c.get("offlineAt"),
                        "evManufacturerName": c.get("evManufacturerName"),
                        "isEVDetailsSet": c.get("isEVDetailsSet"),
                        "smartEV": smart_ev,
                        "sch_d": sch,
                        "chargingLevel": c.get("chargingLevel"),
                        "connectorStatusType": conn.get("connectorStatusType"),
                        "connectors": connectors,
                        "session_d": sess,
                    }
                )
            return {
                "evChargerData": out,
                "ts": data.get("meta", {}).get("serverTimeStamp"),
            }
    except Exception:
        # If mapping fails, fall back to raw
        pass

    return data


def _payload_has_level(payload: JsonDict | None) -> bool:
    """Return True when a payload explicitly includes a charging level."""

    if not isinstance(payload, dict):
        return False
    return any(key in payload for key in ("chargingLevel", "charging_level"))


def _start_charging_candidates(
    self: EnphaseEVClient, sn: str, level: int, connector_id: int
) -> list[tuple[str, str, JsonDict | None]]:
    return [
        (
            "POST",
            f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/{sn}/start_charging",
            {"chargingLevel": level, "connectorId": connector_id},
        ),
        (
            "PUT",
            f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/{sn}/start_charging",
            {"chargingLevel": level, "connectorId": connector_id},
        ),
        (
            "POST",
            f"{BASE_URL}/service/evse_controller/{self._site}/ev_charger/{sn}/start_charging",
            {"chargingLevel": level, "connectorId": connector_id},
        ),
        (
            "POST",
            f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/{sn}/start_charging",
            {"charging_level": level, "connector_id": connector_id},
        ),
        (
            "POST",
            f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/{sn}/start_charging",
            {"connectorId": connector_id},
        ),
        (
            "POST",
            f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/{sn}/start_charging",
            None,
        ),
        (
            "POST",
            f"{BASE_URL}/service/evse_controller/{self._site}/ev_charger/{sn}/start_charging",
            None,
        ),
        (
            "POST",
            f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/{sn}/start_charging",
            {"chargingLevel": level},
        ),
    ]


async def start_charging(
    self: EnphaseEVClient,
    sn: str,
    amps: int,
    connector_id: int = 1,
    *,
    include_level: bool | None = None,
    strict_preference: bool = False,
) -> JsonDict:
    """Start charging or set the charging level.

    The Enlighten API has variations across deployments (method, path, and payload keys).
    We try a sequence of known variants until one succeeds.
    When ``include_level`` is provided, variants that explicitly send the charging
    amps are preferred (include_level=True) or avoided (include_level=False).
    """
    level = int(amps)
    candidates = self._start_charging_candidates(sn, level, connector_id)
    if not candidates:
        raise aiohttp.ClientError("start_charging has no request candidates")

    indices = list(range(len(candidates)))
    level_indices = [
        idx for idx in indices if self._payload_has_level(candidates[idx][2])
    ]
    no_level_indices = [idx for idx in indices if idx not in level_indices]

    def _cache_for_preference() -> int | None:
        if include_level is True:
            return self._start_variant_idx_with_level
        if include_level is False:
            return self._start_variant_idx_no_level
        return self._start_variant_idx

    if include_level is True:
        order = list(level_indices)
        if not order and strict_preference:
            raise aiohttp.ClientError(
                "No start_charging variants support charging level payloads"
            )
        if not strict_preference:
            order += no_level_indices
    elif include_level is False:
        order = list(no_level_indices)
        if not order and strict_preference:
            raise aiohttp.ClientError(
                "No start_charging variants omit charging level payloads"
            )
        if not strict_preference:
            order += level_indices
    else:
        order = indices

    if not order:
        raise aiohttp.ClientError("No start_charging request candidates available")

    cache_idx = _cache_for_preference()
    if cache_idx is not None and cache_idx in order:
        order.remove(cache_idx)
        order.insert(0, cache_idx)

    def _record_variant(idx: int) -> None:
        payload = candidates[idx][2]
        has_level = self._payload_has_level(payload)
        if include_level is True and has_level:
            self._start_variant_idx_with_level = idx
            return
        if include_level is False and not has_level:
            self._start_variant_idx_no_level = idx
            return
        if include_level is None:
            self._start_variant_idx = idx
            return
        # Fallback: remember last working variant for general calls
        self._start_variant_idx = idx

    def _interpret_start_error(message: str) -> JsonDict | None:
        """Return a benign response when backend reports non-fatal errors."""

        if not message:
            return None
        text = message.strip()
        if not text:
            return None
        lower = text.lower()
        if "already in charging state" in lower:
            return {"status": "already_charging"}
        if "not plugged" in lower:
            return {"status": "not_ready"}

        def _load_payload(raw: str) -> Any:
            try:
                return json.loads(raw)
            except Exception:
                stripped = raw.strip("\"'")
                if stripped == raw:
                    raise
                return json.loads(stripped)

        try:
            parsed = _load_payload(text)
        except Exception:
            return None
        if not isinstance(parsed, dict):
            return None
        error_obj = parsed.get("error") or parsed

        def _extract_code(obj: Any) -> str | None:
            if isinstance(obj, dict):
                candidate = obj.get("errorMessageCode") or obj.get("code")
                if isinstance(candidate, str):
                    return candidate.lower()
            return None

        def _extract_message(obj: Any) -> str | None:
            if not isinstance(obj, dict):
                return None
            for key in ("displayMessage", "errorMessage", "message"):
                val = obj.get(key)
                if isinstance(val, str):
                    return val
            return None

        for candidate in (error_obj, parsed):
            code = _extract_code(candidate)
            if code == "iqevc_ms-10012":
                return {"status": "already_charging"}
            if code == "iqevc_ms-10008":
                return {"status": "not_ready"}
            display = _extract_message(candidate)
            if isinstance(display, str):
                disp_lower = display.lower()
                if "already in charging state" in disp_lower:
                    return {"status": "already_charging"}
                if "not plugged" in disp_lower:
                    return {"status": "not_ready"}
        return None

    last_exc: Exception | None = None
    variant_failures: list[dict[str, Any]] = []
    base_headers = self._control_request_headers(self._today_json_headers)
    for idx in order:
        method, url, payload = candidates[idx]
        headers = partial(self._control_request_headers, self._today_json_headers)
        try:
            if payload is None:
                result = await self._json(method, url, headers=headers)
            else:
                result = await self._json(method, url, json=payload, headers=headers)
            # Cache the working variant index for future calls
            _record_variant(idx)
            return cast(JsonDict, result)
        except aiohttp.ClientResponseError as e:
            if e.status >= 500:
                raise
            # 409/422 (and similar) often indicate not plugged in or not ready.
            # Treat these as benign no-ops instead of surfacing as errors.
            if e.status in (409, 422):
                _record_variant(idx)
                return {"status": "not_ready"}
            if e.status == 400:
                interpreted = _interpret_start_error(e.message or "")
                if interpreted is not None:
                    _record_variant(idx)
                    status = interpreted.get("status")
                    _LOGGER.debug(
                        "start_charging treated as benign status %s for charger %s: %s %s payload=%s; response=%s",
                        status,
                        redact_identifier(sn),
                        method,
                        redact_text(
                            url,
                            site_ids=(self._site,),
                            identifiers=(sn,),
                        ),
                        (
                            self._debug_error_message(payload, device_uid=sn)
                            if payload is not None
                            else "<no-body>"
                        ),
                        self._debug_error_message(e.message, device_uid=sn),
                    )
                    return interpreted
                variant_failures.append(
                    {
                        "idx": idx,
                        "method": method,
                        "url": url,
                        "payload": payload if payload is not None else "<no-body>",
                        "response": e.message or "",
                        "headers": self._redact_headers(base_headers),
                    }
                )
            # 400/404/405 variations likely indicate method/path mismatch; try next.
            last_exc = e
            continue
    if last_exc:
        if (
            isinstance(last_exc, aiohttp.ClientResponseError)
            and last_exc.status == 400
            and variant_failures
        ):
            sample = variant_failures[0]
            attempted = ", ".join(
                f"{item['method']} idx {item['idx']}" for item in variant_failures[1:]
            )
            attempt_suffix = f"; other variants tried: {attempted}" if attempted else ""
            _LOGGER.warning(
                "start_charging rejected (400) for charger %s: %s %s payload=%s; headers=%s; response=%s%s",
                redact_identifier(sn),
                sample["method"],
                redact_text(
                    sample["url"],
                    site_ids=(self._site,),
                    identifiers=(sn,),
                ),
                self._debug_error_message(sample["payload"], device_uid=sn),
                sample["headers"],
                self._debug_error_message(sample["response"], device_uid=sn),
                attempt_suffix,
            )
        raise last_exc
    # Should not happen, but keep static analyzer happy
    raise aiohttp.ClientError(
        "start_charging failed with all variants"
    )  # pragma: no cover


def _stop_charging_candidates(
    self: EnphaseEVClient, sn: str
) -> list[tuple[str, str, JsonDict | None]]:
    return [
        (
            "PUT",
            f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/{sn}/stop_charging",
            None,
        ),
        (
            "POST",
            f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/{sn}/stop_charging",
            None,
        ),
        (
            "POST",
            f"{BASE_URL}/service/evse_controller/{self._site}/ev_charger/{sn}/stop_charging",
            None,
        ),
    ]


def _is_routing_not_found(message: str | None) -> bool:
    """Return True when a 404 is a routing miss rather than action state."""

    if not message:
        return False
    text = message.strip()
    lower = text.lower()
    if "no static resource" in lower:
        return True
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return False
    if not isinstance(parsed, dict):
        return False
    detail_parts = [
        parsed.get("detail"),
        parsed.get("title"),
        parsed.get("message"),
        parsed.get("error"),
    ]
    return any(
        isinstance(part, str) and "no static resource" in part.lower()
        for part in detail_parts
    )


def _is_invalid_charge_level_error(message: str | None) -> bool:
    """Return True when a response reports an invalid charge level."""

    if not message:
        return False
    return "invalid charge level" in message.lower()


async def stop_charging(self: EnphaseEVClient, sn: str) -> JsonDict:
    """Stop charging; try multiple endpoint variants."""
    candidates = self._stop_charging_candidates(sn)
    order = list(range(len(candidates)))
    if self._stop_variant_idx is not None and 0 <= self._stop_variant_idx < len(
        candidates
    ):
        order.remove(self._stop_variant_idx)
        order.insert(0, self._stop_variant_idx)

    last_exc: Exception | None = None
    for idx in order:
        method, url, payload = candidates[idx]
        headers = partial(self._control_request_headers, self._today_json_headers)
        try:
            if payload is None:
                result = await self._json(method, url, headers=headers)
            else:
                result = await self._json(method, url, json=payload, headers=headers)
            self._stop_variant_idx = idx
            return cast(JsonDict, result)
        except aiohttp.ClientResponseError as e:
            if e.status >= 500:
                raise
            if e.status == 404 and (
                getattr(e, "enphase_routing_not_found", False)
                or self._is_routing_not_found(e.message)
            ):
                last_exc = e
                continue
            # If charger is not plugged in or already stopped, some backends
            # respond with 400/404/409. Treat these as benign no-ops.
            if e.status in (400, 404, 409, 422):
                return {"status": "not_active"}
            last_exc = e
            continue
    if last_exc:
        raise last_exc
    raise aiohttp.ClientError("stop_charging failed with all variants")


async def trigger_message(
    self: EnphaseEVClient, sn: str, requested_message: str
) -> JsonDict:
    url = f"{BASE_URL}/service/evse_controller/{self._site}/ev_charger/{sn}/trigger_message"
    payload = {"requestedMessage": validate_ocpp_trigger_message(requested_message)}
    headers = partial(self._control_request_headers, self._today_json_headers)
    return cast(JsonDict, await self._json("POST", url, json=payload, headers=headers))


async def start_live_stream(self: EnphaseEVClient) -> JsonDict:
    url = (
        f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/start_live_stream"
    )
    headers = partial(self._control_request_headers, self._today_headers)
    return cast(JsonDict, await self._json("GET", url, headers=headers))


async def stop_live_stream(self: EnphaseEVClient) -> JsonDict:
    url = (
        f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/stop_live_stream"
    )
    headers = partial(self._control_request_headers, self._today_headers)
    return cast(JsonDict, await self._json("GET", url, headers=headers))


async def charge_mode(self: EnphaseEVClient, sn: str) -> str | None:
    """Fetch the current charge mode via scheduler API.

    GET /service/evse_scheduler/api/v1/iqevc/charging-mode/<site>/<sn>/preference
    Requires Authorization: Bearer <jwt> in addition to existing cookies.
    Returns one of: SMART_CHARGING, GREEN_CHARGING, SCHEDULED_CHARGING,
    MANUAL_CHARGING when enabled.
    """
    url = f"{BASE_URL}/service/evse_scheduler/api/v1/iqevc/charging-mode/{self._site}/{sn}/preference"
    headers = partial(self._control_request_headers, self._today_json_headers)
    try:
        data = await self._json("GET", url, headers=headers)
    except aiohttp.ClientResponseError as err:
        if is_scheduler_unavailable_error(err.message, err.status, url):
            raise SchedulerUnavailable(str(err)) from err
        raise
    try:
        modes = (data.get("data") or {}).get("modes") or {}
        # Prefer the mode whose 'enabled' is true
        for key in (
            "smartCharging",
            "greenCharging",
            "scheduledCharging",
            "manualCharging",
        ):
            m = modes.get(key)
            if isinstance(m, dict) and m.get("enabled"):
                return m.get("chargingMode")
    except Exception:
        return None
    return None


async def set_charge_mode(
    self: EnphaseEVClient, sn: str, mode: str, *, previous_mode: str | None = None
) -> JsonDict:
    """Set the charging mode via scheduler API.

    PUT /service/evse_scheduler/api/v1/iqevc/charging-mode/<site>/<sn>/preference
    Body: { "mode": "MANUAL_CHARGING" | "SCHEDULED_CHARGING" |
    "GREEN_CHARGING" | "SMART_CHARGING" }
    """
    url = f"{BASE_URL}/service/evse_scheduler/api/v1/iqevc/charging-mode/{self._site}/{sn}/preference"
    headers = partial(self._control_request_headers, self._today_json_headers)
    normalized_mode = str(mode)
    observed_previous_mode = str(previous_mode) if previous_mode else None
    if observed_previous_mode is None:
        try:
            observed_previous_mode = await self.charge_mode(sn)
        except SchedulerUnavailable:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError):
            observed_previous_mode = None
    if observed_previous_mode == normalized_mode:
        return {
            "status": "already_set",
            "mode": normalized_mode,
        }
    payload = {"mode": normalized_mode}
    try:
        return cast(
            JsonDict, await self._json("PUT", url, json=payload, headers=headers)
        )
    except aiohttp.ClientResponseError as err:
        if is_scheduler_unavailable_error(err.message, err.status, url):
            raise SchedulerUnavailable(str(err)) from err
        if (
            err.status == 400
            and observed_previous_mode is not None
            and observed_previous_mode != normalized_mode
            and _scheduler_error_code(err) != "iqevc_sch_10031"
            and await self._charge_mode_write_landed(sn, normalized_mode)
        ):
            return {
                "status": "accepted",
                "mode": normalized_mode,
                "verified_after_error": True,
            }
        raise


async def _charge_mode_write_landed(self: EnphaseEVClient, sn: str, mode: str) -> bool:
    """Return True if a failed preference write is visible on read-back."""

    for attempt in range(6):
        if attempt:
            await asyncio.sleep(2)
        try:
            current_mode = await self.charge_mode(sn)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return False
        if current_mode == mode:
            return True
    return False


async def green_charging_settings(
    self: EnphaseEVClient, sn: str
) -> list[dict[str, Any]]:
    """Return green charging settings for the charger.

    GET /service/evse_scheduler/api/v1/iqevc/charging-mode/GREEN_CHARGING/<site>/<sn>/settings
    """
    url = (
        f"{BASE_URL}/service/evse_scheduler/api/v1/iqevc/charging-mode/"
        f"GREEN_CHARGING/{self._site}/{sn}/settings"
    )
    headers = partial(self._control_request_headers, self._today_json_headers)
    try:
        payload = await self._json("GET", url, headers=headers)
    except aiohttp.ClientResponseError as err:
        if is_scheduler_unavailable_error(err.message, err.status, url):
            raise SchedulerUnavailable(str(err)) from err
        raise
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


async def set_green_battery_setting(
    self: EnphaseEVClient, sn: str, *, enabled: bool
) -> JsonDict:
    """Toggle green charging battery support.

    PUT /service/evse_scheduler/api/v1/iqevc/charging-mode/GREEN_CHARGING/<site>/<sn>/settings
    Body: {
      "chargerSettingList": [
        { "chargerSettingName": "USE_BATTERY_FOR_SELF_CONSUMPTION", "enabled": true }
      ]
    }
    """
    url = (
        f"{BASE_URL}/service/evse_scheduler/api/v1/iqevc/charging-mode/"
        f"GREEN_CHARGING/{self._site}/{sn}/settings"
    )
    headers = partial(self._control_request_headers, self._today_json_headers)
    payload: JsonDict = {
        "chargerSettingList": [
            {
                "chargerSettingName": GREEN_BATTERY_SETTING,
                "enabled": bool(enabled),
                "value": None,
                "loader": False,
            }
        ]
    }
    try:
        return cast(
            JsonDict, await self._json("PUT", url, json=payload, headers=headers)
        )
    except aiohttp.ClientResponseError as err:
        if is_scheduler_unavailable_error(err.message, err.status, url):
            raise SchedulerUnavailable(str(err)) from err
        raise


async def charger_auth_settings(self: EnphaseEVClient, sn: str) -> list[dict[str, Any]]:
    """Return authentication settings for the charger.

    POST /service/evse_controller/api/v1/<site>/<sn>/ev_charger_config
    Body: [{ "key": "rfidSessionAuthentication" }, { "key": "sessionAuthentication" }]
    """
    url = (
        f"{BASE_URL}/service/evse_controller/api/v1/{self._site}/ev_chargers/"
        f"{sn}/ev_charger_config"
    )
    try:
        return await self.charger_config(
            sn,
            [AUTH_RFID_SETTING, AUTH_APP_SETTING],
        )
    except aiohttp.ClientResponseError as err:
        if is_auth_settings_unavailable_error(err.message, err.status, url):
            raise AuthSettingsUnavailable(str(err)) from err
        raise


async def charger_config(
    self: EnphaseEVClient,
    sn: str,
    keys: Iterable[str],
) -> list[dict[str, Any]]:
    """Return raw charger config entries for the requested keys."""

    normalized_keys: list[str] = []
    seen: set[str] = set()
    for key in keys:
        try:
            key_text = str(key).strip()
        except Exception:
            continue
        if not key_text or key_text in seen:
            continue
        seen.add(key_text)
        normalized_keys.append(key_text)
    if not normalized_keys:
        return []

    url = (
        f"{BASE_URL}/service/evse_controller/api/v1/{self._site}/ev_chargers/"
        f"{sn}/ev_charger_config"
    )
    headers = partial(self._control_request_headers, self._today_json_headers)
    payload = [{"key": key} for key in normalized_keys]

    async def _retry_without_control_auth() -> dict[str, Any]:
        def retry_headers() -> dict[str, str | None]:
            return {
                **self._today_json_headers(),
                "Authorization": None,
                "e-auth-token": None,
            }

        return cast(
            JsonDict,
            await self._json(
                "POST",
                url,
                json=payload,
                headers=retry_headers,
            ),
        )

    try:
        response = await self._json("POST", url, json=payload, headers=headers)
    except Unauthorized:
        if headers().get("Authorization"):
            response = await _retry_without_control_auth()
        else:
            raise
    except aiohttp.ClientResponseError as err:
        if err.status == 403 and headers().get("Authorization"):
            response = await _retry_without_control_auth()
        else:
            raise
    if not isinstance(response, dict):
        return []
    data = response.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


async def set_app_authentication(
    self: EnphaseEVClient, sn: str, *, enabled: bool
) -> JsonDict:
    """Enable or disable session authentication via app.

    PUT /service/evse_controller/api/v1/<site>/<sn>/ev_charger_config
    Body: [{ "key": "sessionAuthentication", "value": "enabled" | "disabled" }]
    """
    url = (
        f"{BASE_URL}/service/evse_controller/api/v1/{self._site}/ev_chargers/"
        f"{sn}/ev_charger_config"
    )
    headers = partial(self._control_request_headers, self._today_json_headers)
    payload = [
        {
            "key": AUTH_APP_SETTING,
            "value": "enabled" if enabled else "disabled",
        }
    ]
    try:
        return cast(
            JsonDict, await self._json("PUT", url, json=payload, headers=headers)
        )
    except aiohttp.ClientResponseError as err:
        if is_auth_settings_unavailable_error(err.message, err.status, url):
            raise AuthSettingsUnavailable(str(err)) from err
        raise


async def set_default_charge_level(
    self: EnphaseEVClient, sn: str, amps: int
) -> JsonDict:
    """Set the charger's stored default charge level.

    PUT /service/evse_controller/api/v1/<site>/<sn>/ev_charger_config
    Body: [{ "key": "DefaultChargeLevel", "value": <amps> }]
    """
    url = (
        f"{BASE_URL}/service/evse_controller/api/v1/{self._site}/ev_chargers/"
        f"{sn}/ev_charger_config"
    )
    headers = partial(self._control_request_headers, self._today_json_headers)
    payload = [{"key": DEFAULT_CHARGE_LEVEL_SETTING, "value": int(amps)}]
    try:
        response = await self._json("PUT", url, json=payload, headers=headers)
    except aiohttp.ClientResponseError as err:
        if is_auth_settings_unavailable_error(err.message, err.status, url):
            raise ChargerConfigUnavailable(str(err)) from err
        raise
    return response if isinstance(response, dict) else {}


async def get_schedules(self: EnphaseEVClient, sn: str) -> JsonDict:
    """Return scheduler config and slots for the charger.

    GET /service/evse_scheduler/api/v1/iqevc/charging-mode/SCHEDULED_CHARGING/<site>/<sn>/schedules
    """
    url = (
        f"{BASE_URL}/service/evse_scheduler/api/v1/iqevc/charging-mode/"
        f"SCHEDULED_CHARGING/{self._site}/{sn}/schedules"
    )
    headers = partial(self._control_request_headers, self._today_json_headers)
    try:
        payload = await self._json("GET", url, headers=headers)
    except aiohttp.ClientResponseError as err:
        if is_scheduler_unavailable_error(err.message, err.status, url):
            raise SchedulerUnavailable(str(err)) from err
        raise
    if not isinstance(payload, dict):
        return {"meta": None, "config": None, "slots": []}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return {
        "meta": payload.get("meta"),
        "config": (data or {}).get("config"),
        "slots": (data or {}).get("slots") or [],
    }


async def patch_schedules(
    self: EnphaseEVClient, sn: str, *, server_timestamp: str, slots: list[JsonDict]
) -> JsonDict:
    """Patch the scheduler slots for the charger.

    PATCH /service/evse_scheduler/api/v1/iqevc/charging-mode/SCHEDULED_CHARGING/<site>/<sn>/schedules
    """
    url = (
        f"{BASE_URL}/service/evse_scheduler/api/v1/iqevc/charging-mode/"
        f"SCHEDULED_CHARGING/{self._site}/{sn}/schedules"
    )
    headers = partial(self._control_request_headers, self._today_json_headers)
    payload = {
        "meta": {"serverTimeStamp": server_timestamp, "rowCount": len(slots)},
        "data": slots,
    }
    try:
        return cast(
            JsonDict,
            await self._json("PATCH", url, json=payload, headers=headers),
        )
    except aiohttp.ClientResponseError as err:
        if is_scheduler_unavailable_error(err.message, err.status, url):
            raise SchedulerUnavailable(str(err)) from err
        raise


async def patch_schedule_states(
    self: EnphaseEVClient, sn: str, *, slot_states: dict[str, bool]
) -> JsonDict:
    """Patch schedule slot enabled states for the charger.

    PATCH /service/evse_scheduler/api/v1/iqevc/charging-mode/SCHEDULED_CHARGING/<site>/<sn>/schedules
    """
    url = (
        f"{BASE_URL}/service/evse_scheduler/api/v1/iqevc/charging-mode/"
        f"SCHEDULED_CHARGING/{self._site}/{sn}/schedules"
    )
    headers = partial(self._control_request_headers, self._today_json_headers)
    payload = {
        str(slot_id): "ENABLED" if enabled else "DISABLED"
        for slot_id, enabled in slot_states.items()
    }
    try:
        return cast(
            JsonDict,
            await self._json("PATCH", url, json=payload, headers=headers),
        )
    except aiohttp.ClientResponseError as err:
        if is_scheduler_unavailable_error(err.message, err.status, url):
            raise SchedulerUnavailable(str(err)) from err
        raise


async def patch_schedule(
    self: EnphaseEVClient, sn: str, slot_id: str, slot: JsonDict
) -> JsonDict:
    """Patch a single schedule slot for the charger.

    PATCH /service/evse_scheduler/api/v1/iqevc/charging-mode/SCHEDULED_CHARGING/<site>/<sn>/schedule/<slot_id>
    """
    url = (
        f"{BASE_URL}/service/evse_scheduler/api/v1/iqevc/charging-mode/"
        f"SCHEDULED_CHARGING/{self._site}/{sn}/schedule/{slot_id}"
    )
    headers = partial(self._control_request_headers, self._today_json_headers)
    try:
        return cast(
            JsonDict, await self._json("PATCH", url, json=slot, headers=headers)
        )
    except aiohttp.ClientResponseError as err:
        if is_scheduler_unavailable_error(err.message, err.status, url):
            raise SchedulerUnavailable(str(err)) from err
        raise


async def create_schedule(self: EnphaseEVClient, sn: str, slot: JsonDict) -> JsonDict:
    """Create a single schedule slot for the charger.

    POST /service/evse_scheduler/api/v1/iqevc/charging-mode/SCHEDULED_CHARGING/<site>/<sn>/schedule
    """
    url = (
        f"{BASE_URL}/service/evse_scheduler/api/v1/iqevc/charging-mode/"
        f"SCHEDULED_CHARGING/{self._site}/{sn}/schedule"
    )
    headers = partial(self._control_request_headers, self._today_json_headers)
    try:
        return cast(JsonDict, await self._json("POST", url, json=slot, headers=headers))
    except aiohttp.ClientResponseError as err:
        if is_scheduler_unavailable_error(err.message, err.status, url):
            raise SchedulerUnavailable(str(err)) from err
        raise


async def delete_schedule(self: EnphaseEVClient, sn: str, slot_id: str) -> JsonDict:
    """Delete a single schedule slot for the charger.

    DELETE /service/evse_scheduler/api/v1/iqevc/charging-mode/SCHEDULED_CHARGING/<site>/<sn>/schedule/<slot_id>
    """
    url = (
        f"{BASE_URL}/service/evse_scheduler/api/v1/iqevc/charging-mode/"
        f"SCHEDULED_CHARGING/{self._site}/{sn}/schedule/{slot_id}"
    )
    headers = partial(self._control_request_headers, self._today_json_headers)
    try:
        return cast(JsonDict, await self._json("DELETE", url, headers=headers))
    except aiohttp.ClientResponseError as err:
        if is_scheduler_unavailable_error(err.message, err.status, url):
            raise SchedulerUnavailable(str(err)) from err
        raise
