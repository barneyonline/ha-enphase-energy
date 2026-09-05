"""Bounded inverter discovery shared by onboarding and the inventory runtime."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class InverterInventoryResult:
    """A normalized listing with explicit authority to infer device absence."""

    payload: dict[str, object]
    complete: bool
    reason: str | None = None


def inverter_page(payload: object) -> tuple[list[dict[str, object]], int] | None:
    """Read root or wrapped inventory without guessing malformed pages are empty."""

    if not isinstance(payload, dict):
        return None
    source = payload
    if not isinstance(source.get("inverters"), list):
        wrapped = source.get("result")
        if not isinstance(wrapped, dict):
            return None
        source = wrapped
    rows = source.get("inverters")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return None
    items = [dict(row) for row in rows]
    raw_total = payload.get("total", source.get("total", len(items)))
    try:
        total = int(str(raw_total))
    except (TypeError, ValueError):
        return None
    if total < len(items) or total < 0:
        return None
    return items, total


async def async_fetch_inverter_pages(
    fetch_page: Callable[[int], Awaitable[object]],
    *,
    max_pages: int = 100,
    max_items: int = 100_000,
) -> InverterInventoryResult:
    """Fetch bounded pages; transport errors and cancellation remain caller-owned."""

    payload: dict[str, object] = {}
    items: list[dict[str, object]] = []
    seen: set[str] = set()
    offset = 0
    expected = 0
    for page_index in range(max_pages):
        raw = await fetch_page(offset)
        parsed = inverter_page(raw)
        if parsed is None:
            return InverterInventoryResult(
                {**payload, "inverters": items}, False, "invalid_page"
            )
        if page_index == 0 and isinstance(raw, dict):
            wrapped = raw.get("result")
            payload = dict(wrapped) if isinstance(wrapped, dict) else {}
            payload.update(raw)
            payload.pop("result", None)
        rows, total = parsed
        expected = max(expected, total)
        if expected > max_items or len(items) + len(rows) > max_items:
            return InverterInventoryResult(
                {**payload, "inverters": items}, False, "item_limit"
            )
        new_items = 0
        for row in rows:
            identity = str(
                row.get("serial_number")
                or row.get("inverter_id")
                or row.get("id")
                or repr(sorted(row.items()))
            )
            if identity not in seen:
                seen.add(identity)
                items.append(row)
                new_items += 1
        offset += len(rows)
        normalized = {**payload, "inverters": items, "total": expected}
        if len(items) >= expected:
            return InverterInventoryResult(normalized, True)
        if not new_items:
            return InverterInventoryResult(normalized, False, "no_progress")
    return InverterInventoryResult(
        {**payload, "inverters": items, "total": expected}, False, "page_limit"
    )
