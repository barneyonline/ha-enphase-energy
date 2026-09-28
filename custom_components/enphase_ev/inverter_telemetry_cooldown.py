"""Persist only the microinverter telemetry rate-limit deadline across lifecycles."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime
from typing import Any, cast

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, OPT_MICROINVERTER_POWER_ENABLED

FAMILY = "inverter_parameter_telemetry"
_LOGGER = logging.getLogger(__name__)


def _store(coord: Any) -> Store[dict[str, str]]:
    store = coord.__dict__.get("_inverter_telemetry_cooldown_store")
    if store is None:
        entry = getattr(coord, "config_entry", None)
        identity = getattr(entry, "entry_id", coord.site_id)
        store = Store(coord.hass, 1, f"{DOMAIN}.inverter_telemetry_cooldown.{identity}")
        coord.__dict__["_inverter_telemetry_cooldown_store"] = store
    return cast(Store[dict[str, str]], store)


async def async_restore_inverter_telemetry_cooldown(coord: Any) -> None:
    """Restore a UTC deadline before polling; never persist monotonic clocks."""
    lock = coord.__dict__.setdefault(
        "_inverter_telemetry_cooldown_lock", asyncio.Lock()
    )
    async with lock:
        if coord.__dict__.get("_inverter_telemetry_cooldown_loaded"):
            return
        try:
            saved = await _store(coord).async_load()
        except Exception:
            _LOGGER.warning("Unable to restore microinverter telemetry retry deadline")
            return
        coord.__dict__["_inverter_telemetry_cooldown_loaded"] = True
        value = saved.get("next_retry_utc") if isinstance(saved, dict) else None
        if not isinstance(value, str):
            return
        deadline = dt_util.parse_datetime(value)
        if deadline is None or deadline.tzinfo is None:
            return
        remaining = (deadline - dt_util.utcnow()).total_seconds()
        if remaining <= 0:
            return
        health = coord._endpoint_family_state(FAMILY)
        if (
            isinstance(health.next_retry_utc, datetime)
            and health.next_retry_utc >= deadline
        ):
            return
        health.next_retry_utc = deadline
        health.next_retry_mono = time.monotonic() + remaining
        health.cooldown_active = True
        health.last_status = 429
        health.last_error = "Enphase rate limit (HTTP 429)"


async def async_save_inverter_telemetry_cooldown(coord: Any) -> None:
    """Durably record a backend rate limit before leaving the optional refresh."""
    health = coord._endpoint_family_state(FAMILY)
    deadline = health.next_retry_utc
    if not (
        health.last_status == 429
        and health.cooldown_active
        and isinstance(deadline, datetime)
    ):
        return
    try:
        await _store(coord).async_save({"next_retry_utc": deadline.isoformat()})
    except Exception:
        _LOGGER.warning("Unable to save microinverter telemetry retry deadline")


def _power_available(coord: Any) -> bool:
    telemetry = getattr(coord, "_inverter_parameter_telemetry", {})
    freshness = getattr(coord, "_inverter_parameter_success_mono", {})
    policy = coord._endpoint_family_policy(FAMILY)
    stale_after = getattr(policy, "stale_after_s", None) or 1800
    now = time.monotonic()
    for serial, snapshot in telemetry.items():
        power = snapshot.get("power")
        stamp = freshness.get(serial, {}).get("power")
        if (
            type(power) in (int, float)
            and math.isfinite(power)
            and type(stamp) in (int, float)
            and 0 <= now - stamp <= stale_after
        ):
            return True
    return False


def inverter_telemetry_status_attributes(coord: Any) -> dict[str, object]:
    """Expose polling status on inventory entities even before power discovery."""
    entry = getattr(coord, "config_entry", None)
    enabled = bool(
        getattr(entry, "options", {}).get(OPT_MICROINVERTER_POWER_ENABLED, False)
    )
    health = getattr(coord, "_endpoint_family_health", {}).get(FAMILY)
    deadline = getattr(health, "next_retry_utc", None)
    rate_limited = bool(
        getattr(health, "last_status", None) == 429
        and getattr(health, "cooldown_active", False)
        and isinstance(deadline, datetime)
        and deadline > dt_util.utcnow()
    )
    return {
        "power_telemetry_status": (
            "disabled"
            if not enabled
            else (
                "rate_limited"
                if rate_limited
                else ("ready" if _power_available(coord) else "pending")
            )
        ),
        "power_telemetry_next_retry": (
            cast(datetime, deadline).isoformat() if rate_limited else None
        ),
    }
