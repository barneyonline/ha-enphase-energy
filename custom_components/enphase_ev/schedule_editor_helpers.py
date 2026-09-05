"""Pure formatting shared by battery and EVSE schedule editors."""

from __future__ import annotations

from datetime import time as dt_time


def time_to_text(value: object, *, default: str = "00:00") -> str:
    if isinstance(value, dt_time):
        return value.strftime("%H:%M")
    if value is None:
        return default
    if isinstance(value, (int, float)):
        total_minutes = int(value)
        return f"{(total_minutes // 60) % 24:02d}:{total_minutes % 60:02d}"
    text = str(value).strip()
    if len(text) >= 5 and text[2] == ":":
        return text[:5]
    return default


def normalize_days(raw: object) -> list[int]:
    if not isinstance(raw, list):
        return []
    days: list[int] = []
    for value in raw:
        try:
            day = int(value)
        except (TypeError, ValueError):
            continue
        if 1 <= day <= 7 and day not in days:
            days.append(day)
    return sorted(days)
