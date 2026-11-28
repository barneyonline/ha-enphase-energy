"""Home Assistant storage for state that must survive before publishing."""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util.file import WriteError
from homeassistant.util.json import SerializationError


class AcknowledgedStore(Store[dict[str, Any]]):  # type: ignore[misc]
    """Acknowledge atomic writes and finish them before releasing the caller."""

    def __init__(
        self, hass: HomeAssistant, version: int, key: str, *, private: bool = False
    ) -> None:
        super().__init__(hass, version, key, private=private, atomic_writes=True)
        self._written = False

    async def async_save(self, data: dict[str, Any]) -> None:
        task = asyncio.create_task(self._async_save_checked(data))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _async_save_checked(self, data: dict[str, Any]) -> None:
        self._written = False
        await super().async_save(data)
        if not self._written:
            # Core may defer a save until final_write during shutdown.
            await self._async_handle_write_data()
        if not self._written:
            raise OSError(f"Storage write for {self.key} was not acknowledged")

    async def _async_write_data(self, data: dict[str, Any]) -> None:
        try:
            await super()._async_write_data(data)
        except (WriteError, SerializationError) as err:
            # Core logs these exceptions and returns without reporting failure.
            raise OSError(f"Could not persist {self.key}") from err
        self._written = True
