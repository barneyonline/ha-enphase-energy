"""Site-owned battery-to-EV preferences with serialized, verified updates."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .const import DOMAIN

if TYPE_CHECKING:
    from .battery_runtime import BatteryRuntime


@dataclass(frozen=True)
class EVBatteryPreference:
    enabled: bool
    limit: int
    minimum: int


def parse_preference(payload: object) -> EVBatteryPreference | None:
    """Accept only complete EV fields; never infer bounds from backup reserve."""
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    devices = data.get("devices") if isinstance(data, dict) else None
    evse = devices.get("iqEvse") if isinstance(devices, dict) else None
    if not isinstance(evse, dict):
        return None
    enabled = evse.get("useBatteryForEVSE")
    limit = evse.get("batteryLimit")
    minimum = evse.get("minBatteryLimit")
    if type(enabled) is not bool or type(limit) is not int or type(minimum) is not int:
        return None
    if not 0 <= minimum <= 100 or not 0 <= limit <= 100:
        return None
    if limit < minimum and (enabled or limit != 0):
        return None
    return EVBatteryPreference(enabled, limit, minimum)


class EVBatteryPreferences:
    """Keep site ownership once discovered, including during failed reads."""

    def __init__(self, runtime: BatteryRuntime) -> None:
        self.runtime = runtime
        self.value: EVBatteryPreference | None = None
        self.seen = False
        self.supported: bool | None = None
        self.generation = 0
        self._lock = asyncio.Lock()

    def observe(self, payload: object) -> None:
        self.value = parse_preference(payload)
        if isinstance(payload, dict):
            data = payload.get("data")
            devices = data.get("devices") if isinstance(data, dict) else None
            evse = devices.get("iqEvse") if isinstance(devices, dict) else None
            if isinstance(evse, dict) and "useBatteryForEVSE" in evse:
                self._mark_seen()

    def observe_capabilities(self, data: dict[str, object]) -> None:
        flag = data.get("isUseBatteryForEVSESupported")
        if type(flag) is bool:
            self.supported = flag
            if flag:
                self._mark_seen()

    def _mark_seen(self) -> None:
        if self.seen:
            return
        self.seen = True
        snapshot = getattr(self.runtime.coordinator, "discovery_snapshot", None)
        if snapshot is not None:
            snapshot.schedule_save()

    @property
    def available(self) -> bool:
        coord = self.runtime.coordinator
        return bool(
            self.value is not None
            and self.supported is not False
            and coord.battery_write_access_confirmed
            and coord.battery_system_task is not True
        )

    async def async_update(
        self, *, enabled: bool | None = None, limit: float | None = None
    ) -> None:
        """Read companions inside the lock and expose only authoritative state."""
        async with self._lock:
            coord = self.runtime.coordinator
            await self.runtime.async_ensure_battery_write_access_confirmed()
            self.generation += 1
            try:
                payload = await coord.client.battery_settings_details()
                self.runtime._apply_battery_permission_payload(payload)
                self.observe(payload)
                if not self.available or self.value is None:
                    raise ServiceValidationError(
                        "Battery-to-EV preferences are unavailable.",
                        translation_domain=DOMAIN,
                        translation_key="battery_settings_updates_unavailable",
                    )
                current = self.value
                target_enabled = current.enabled if enabled is None else enabled
                target_limit = current.limit if limit is None else limit
                # The captured disabled sentinel is not an enabled threshold.
                if enabled is True and limit is None and current.limit == 0:
                    target_limit = current.minimum
                if (
                    isinstance(target_limit, bool)
                    or not float(target_limit).is_integer()
                    or not 0 <= target_limit <= 100
                    or (
                        target_limit < current.minimum
                        and (target_enabled or limit is not None)
                    )
                ):
                    raise ServiceValidationError(
                        f"EV battery threshold must be a whole percentage between {current.minimum} and 100.",
                        translation_domain=DOMAIN,
                        translation_key="ev_battery_limit_range",
                        translation_placeholders={
                            "minimum": str(current.minimum),
                            "maximum": "100",
                        },
                    )
                expected = EVBatteryPreference(
                    target_enabled, int(target_limit), current.minimum
                )
                response = await coord.client.set_ev_battery_preference(
                    enabled=expected.enabled, limit=expected.limit
                )
                data = response.get("data")
                if (
                    response.get("type") != "iqevse-battery-preference"
                    or not isinstance(data, dict)
                    or data.get("message") != "success"
                ):
                    raise HomeAssistantError(
                        "Enphase did not acknowledge the battery-to-EV update.",
                        translation_domain=DOMAIN,
                        translation_key="ev_battery_update_unconfirmed",
                    )
                self.observe(await coord.client.battery_settings_details())
                if self.value is None or (self.value.enabled, self.value.limit) != (
                    expected.enabled,
                    expected.limit,
                ):
                    raise HomeAssistantError(
                        "Battery-to-EV update was not confirmed by fresh settings. Refresh before retrying.",
                        translation_domain=DOMAIN,
                        translation_key="ev_battery_update_unconfirmed",
                    )
            except ServiceValidationError:
                raise
            except (Exception, asyncio.CancelledError):
                # A failure can follow an accepted write. Do not report cached success.
                self.value = None
                raise
            finally:
                self.generation += 1
                coord.publish_runtime_state_update("ev_battery_preferences")


def preferences_for(coord: object) -> EVBatteryPreferences | None:
    """Compatibility accessor for coordinators without this optional capability."""
    runtime = getattr(coord, "battery_runtime", None)
    preferences = getattr(runtime, "ev_preferences", None)
    return preferences if isinstance(preferences, EVBatteryPreferences) else None
