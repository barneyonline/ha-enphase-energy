"""Read and normalize static solar nameplate metadata without storing settings HTML."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any

from .api_client.errors import Unauthorized

_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .inventory_runtime import InventoryRuntime

CAPACITY_CACHE_SECONDS = 21600.0
CAPACITY_RETRY_SECONDS = 3600.0


def positive_number(value: object) -> float | None:
    """Accept finite positive numeric values, never booleans or unit strings."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) and number > 0 else None


class PanelRatingsParser(HTMLParser):
    """Extract only selected array panel ratings from the settings form."""

    def __init__(self) -> None:
        super().__init__()
        self.ratings: dict[str, float | None] = {}
        self._array: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "select":
            identifier = values.get("id") or ""
            self._array = (
                identifier.removeprefix("pv_module_model_")
                if identifier.startswith("pv_module_model_")
                else None
            )
        if tag == "option" and self._array and "selected" in values:
            self.ratings[self._array] = positive_number(values.get("data-stc-rating"))

    def handle_endtag(self, tag: str) -> None:
        if tag == "select":
            self._array = None


def capacity_snapshot(
    builder: object, ratings: dict[str, float | None]
) -> dict[str, Any]:
    """Require complete unique membership for totals; keep unknown arrays explicit."""
    if not isinstance(builder, dict) or not isinstance(builder.get("arrays"), list):
        raise ValueError("Invalid array inventory")
    inventory = builder.get("inventory_details")
    if not isinstance(inventory, list):
        raise ValueError("Invalid inverter capacity inventory")
    by_serial: dict[str, float | None] = {}
    for item in inventory:
        if not isinstance(item, dict) or not isinstance(item.get("serialNum"), str):
            raise ValueError("Invalid inverter capacity record")
        if item.get("count") != 1 or isinstance(item.get("count"), bool):
            raise ValueError("Unsupported inverter capacity count")
        serial = item["serialNum"].strip()
        if not serial or serial in by_serial:
            raise ValueError("Ambiguous inverter capacity record")
        by_serial[serial] = positive_number(item.get("capacity"))
    dc: dict[str, float | None] = {}
    ac: dict[str, float | None] = {}
    seen: set[str] = set()
    array_ids: set[str] = set()
    for array in builder["arrays"]:
        if not isinstance(array, dict) or not isinstance(array.get("modules"), list):
            raise ValueError("Invalid array record")
        identifier = str(array.get("id") or "")
        label = array.get("label")
        if (
            not identifier
            or identifier in array_ids
            or not isinstance(label, str)
            or not label.strip()
        ):
            raise ValueError("Missing or ambiguous array identity")
        array_ids.add(identifier)
        label = label.strip()
        modules = array["modules"]
        serials: set[str] = set()
        for module in modules:
            if not isinstance(module, dict):
                raise ValueError("Invalid array module")
            serial = module.get("serial_num")
            if not isinstance(serial, str) or not serial.strip():
                raise ValueError("Unassigned array module")
            serial = serial.strip()
            if serial in seen:
                raise ValueError("Duplicate array inverter membership")
            seen.add(serial)
            serials.add(serial)
        rating = ratings.get(identifier)
        panel_watts = len(modules) * rating if rating is not None else None
        capacities = [by_serial.get(serial) for serial in serials]
        inverter_va = (
            sum(value for value in capacities if value is not None)
            if all(value is not None for value in capacities)
            else None
        )
        for target, value in ((dc, panel_watts), (ac, inverter_va)):
            previous = target.get(label, 0.0)
            target[label] = (
                previous + value if previous is not None and value is not None else None
            )
    # Inventory not represented in the layout must not silently disappear from totals.
    complete = bool(seen) and seen == set(by_serial)
    result: dict[str, Any] = {}
    for key, arrays in (("array_size", dc), ("inverter_capacity", ac)):
        result[key] = {
            "value": (
                round(
                    sum(value for value in arrays.values() if value is not None) / 1000,
                    6,
                )
                if complete and all(value is not None for value in arrays.values())
                else None
            ),
            "arrays": {
                name: round(value / 1000, 6) if value is not None else None
                for name, value in sorted(arrays.items())
            },
        }
    return result


def _access_denied(error: Exception) -> bool:
    """Recognize optional endpoint permission failures without exposing error bodies."""
    return isinstance(error, Unauthorized) or getattr(error, "status", None) in (
        401,
        403,
    )


async def async_refresh_array_capacity(runtime: InventoryRuntime) -> None:
    """Refresh optional static metadata at most every six hours, with bounded retries."""
    state = runtime.inventory_state
    if not runtime.include_inverters:
        state.array_capacity = {}
        state.array_capacity_next_refresh = 0.0
        return
    now = time.monotonic()
    if now < state.array_capacity_next_refresh:
        return
    builder_fetch = getattr(runtime.client, "array_builder_inventory", None)
    ratings_fetch = getattr(runtime.client, "array_panel_ratings", None)
    if not callable(builder_fetch) or not callable(ratings_fetch):
        return
    state.array_capacity_next_refresh = now + CAPACITY_RETRY_SECONDS
    try:
        async with asyncio.timeout(15):
            builder = await builder_fetch()
        try:
            async with asyncio.timeout(15):
                ratings = await ratings_fetch()
        except Exception as err:  # noqa: BLE001 - AC remains independently usable
            if _access_denied(err):
                state.array_capacity_next_refresh = now + CAPACITY_CACHE_SECONDS
                _LOGGER.debug("Array panel ratings unavailable: access denied")
            ratings = {}
        snapshot = capacity_snapshot(builder, ratings)
    except Exception as err:  # noqa: BLE001 - isolate optional metadata
        if _access_denied(err):
            state.array_capacity_next_refresh = now + CAPACITY_CACHE_SECONDS
            _LOGGER.debug("Array capacity inventory unavailable: access denied")
        state.array_capacity = {}
        return
    state.array_capacity = snapshot
    if all(item["value"] is not None for item in snapshot.values()):
        state.array_capacity_next_refresh = now + CAPACITY_CACHE_SECONDS
