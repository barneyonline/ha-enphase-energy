"""Define runtime objects stored on Home Assistant config entries."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry

from .const import DOMAIN

if TYPE_CHECKING:  # pragma: no cover
    from homeassistant.core import HomeAssistant

    from .battery_schedule_editor import BatteryScheduleEditorManager
    from .coordinator import EnphaseCoordinator
    from .evse_schedule_editor import EvseScheduleEditorManager
    from .evse_firmware import EvseFirmwareDetailsManager
    from .firmware_catalog import FirmwareCatalogManager
    from .gateway_software_update import GatewaySoftwareUpdateManager
    from .weather import EnphaseWeatherCoordinator


@dataclass(slots=True)
class EnphaseRuntimeData:
    """Runtime objects attached to a loaded config entry."""

    coordinator: EnphaseCoordinator
    firmware_catalog: FirmwareCatalogManager | None = None
    evse_firmware_details: EvseFirmwareDetailsManager | None = None
    gateway_software_update: GatewaySoftwareUpdateManager | None = None
    battery_schedule_editor: BatteryScheduleEditorManager | None = None
    evse_schedule_editor: EvseScheduleEditorManager | None = None
    weather_coordinator: EnphaseWeatherCoordinator | None = None
    weather_discovery_task: asyncio.Task[None] | None = None
    reload_suppression_count: int = 0

    async def async_stop_weather(self) -> None:
        """Stop and release the optional weather child coordinator."""

        task = self.weather_discovery_task
        self.weather_discovery_task = None
        if isinstance(task, asyncio.Task) and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        coordinator = self.weather_coordinator
        self.weather_coordinator = None
        if coordinator is not None:
            coordinator.mark_stopped()


type EnphaseConfigEntry = ConfigEntry[EnphaseRuntimeData]


def get_runtime_data(entry: EnphaseConfigEntry) -> EnphaseRuntimeData:
    """Return runtime data for a loaded config entry."""

    runtime_data = getattr(entry, "runtime_data", None)
    if isinstance(runtime_data, EnphaseRuntimeData):
        return runtime_data

    # Home Assistant only populates runtime_data while the config entry is loaded.
    raise RuntimeError(f"Missing runtime data for entry {entry.entry_id}")


def iter_coordinators(
    hass: HomeAssistant, *, site_ids: set[str] | None = None
) -> list[EnphaseCoordinator]:
    """Return coordinators from loaded config entries."""

    coordinators: list[EnphaseCoordinator] = []
    seen: set[str] = set()
    for entry in hass.config_entries.async_entries(DOMAIN):
        runtime_data = getattr(entry, "runtime_data", None)
        if not isinstance(runtime_data, EnphaseRuntimeData):
            continue
        coord = runtime_data.coordinator
        site_id = str(getattr(coord, "site_id", ""))
        if site_ids and site_id not in site_ids:
            continue
        # Multiple entries can reference the same Enphase site during reload workflows.
        if site_id in seen:
            continue
        seen.add(site_id)
        coordinators.append(coord)
    return coordinators
