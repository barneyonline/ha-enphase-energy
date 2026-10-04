"""Exercise backoff retries through Home Assistant's coordinator scheduler."""

import math
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.enphase_ev import coordinator as coord_mod


@pytest.mark.parametrize("remaining", [0.001, 0.25, 1.0, 61.13])
@pytest.mark.parametrize("phase", [0.05, 0.5, 0.95])
@pytest.mark.asyncio
async def test_backoff_retry_schedules_in_future(
    coordinator_factory, hass, monkeypatch, remaining, phase
):
    """A guarded refresh must not schedule another callback in the past."""
    coord = coordinator_factory()
    coord.client.status = AsyncMock()
    now = math.floor(hass.loop.time()) + phase
    monkeypatch.setattr(coord_mod, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(hass.loop, "time", lambda: now)
    coord._microsecond = 0.05
    coord._backoff_until = now + remaining
    remove_listener = coord.async_add_listener(lambda: None)
    try:
        # Use HA's UpdateFailed handling and _schedule_refresh, including call_at.
        await coord.async_refresh()
        assert isinstance(coord.last_exception, UpdateFailed)
        handle = coord._unsub_refresh.__self__
        assert handle.when() > hass.loop.time()
        assert coord.last_exception.retry_after == pytest.approx(max(1.0, remaining))
        coord.client.status.assert_not_called()
        assert coord._backoff_until == now + remaining
    finally:
        remove_listener()
        coord._debounced_refresh.async_cancel()
