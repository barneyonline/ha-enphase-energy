"""Keep battery schedule editor form state in sync with BatteryConfig payloads."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time as dt_time
from typing import TYPE_CHECKING, Callable, TypeVar, cast

from homeassistant.core import CALLBACK_TYPE, callback as ha_callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DEFAULT_BATTERY_SCHEDULES_ENABLED,
    OPT_BATTERY_SCHEDULES_ENABLED,
)
from .labels import battery_schedule_type_label
from .runtime_data import EnphaseConfigEntry, get_runtime_data

if TYPE_CHECKING:  # pragma: no cover
    from .coordinator import EnphaseCoordinator

    class _CoordinatorEntityBase:
        """Typed view of the external coordinator entity base."""

        def __init__(self, coordinator: object) -> None: ...

        async def async_added_to_hass(self) -> None: ...

        async def async_will_remove_from_hass(self) -> None: ...

        def async_write_ha_state(self) -> None: ...

else:
    _CoordinatorEntityBase = CoordinatorEntity

_CallbackT = TypeVar("_CallbackT", bound=Callable[..., object])


def callback(func: _CallbackT) -> _CallbackT:
    """Apply Home Assistant's callback marker with its identity type preserved."""

    return cast(_CallbackT, ha_callback(func))


DAY_ORDER: list[tuple[str, int]] = [
    ("mon", 1),
    ("tue", 2),
    ("wed", 3),
    ("thu", 4),
    ("fri", 5),
    ("sat", 6),
    ("sun", 7),
]
DAY_KEY_BY_INDEX = {index: key for key, index in DAY_ORDER}
NEW_SCHEDULE_OPTION = "new_schedule"
SCHEDULE_TYPE_KEYS: tuple[str, ...] = ("cfg", "dtg", "rbd")
_DAY_MINUTES = 24 * 60


def default_day_flags() -> dict[str, bool]:
    return {key: True for key, _ in DAY_ORDER}


def _time_to_text(value: object, *, default: str = "00:00") -> str:
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


def _normalize_days(raw: object) -> list[int]:
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


def _minutes_of_day(value: object) -> int | None:
    if isinstance(value, dt_time):
        return value.hour * 60 + value.minute
    if isinstance(value, (int, float)):
        minutes = int(value)
        if 0 <= minutes < _DAY_MINUTES:
            return minutes
        return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) < 2:
        return None
    try:
        hours = int(parts[0])
        minutes = int(parts[1])
    except ValueError:
        return None
    if not 0 <= hours < 24 or not 0 <= minutes < 60:
        return None
    return hours * 60 + minutes


def _weekly_schedule_segments(
    days: list[int], start_minutes: int, end_minutes: int
) -> list[tuple[int, int]]:
    if start_minutes == end_minutes:
        return []
    segments: list[tuple[int, int]] = []
    for day in _normalize_days(days):
        offset = (day - 1) * _DAY_MINUTES
        if start_minutes < end_minutes:
            segments.append((offset + start_minutes, offset + end_minutes))
            continue
        # Cross-midnight schedules occupy the selected day and the following
        # day's early window.
        segments.append((offset + start_minutes, offset + _DAY_MINUTES))
        next_day_offset = (day % 7) * _DAY_MINUTES
        segments.append((next_day_offset, next_day_offset + end_minutes))
    return segments


def _segments_overlap(
    left: list[tuple[int, int]], right: list[tuple[int, int]]
) -> bool:
    return any(
        left_start < right_end and right_start < left_end
        for left_start, left_end in left
        for right_start, right_end in right
    )


def battery_schedule_overlap_record(
    coord: EnphaseCoordinator,
    *,
    start_time: object,
    end_time: object,
    days: list[int],
    exclude_schedule_id: str | None = None,
) -> BatteryScheduleRecord | None:
    candidate_days = _normalize_days(days)
    candidate_start = _minutes_of_day(start_time)
    candidate_end = _minutes_of_day(end_time)
    if not candidate_days or candidate_start is None or candidate_end is None:
        return None
    candidate_segments = _weekly_schedule_segments(
        candidate_days,
        candidate_start,
        candidate_end,
    )
    if not candidate_segments:
        return None
    for schedule in battery_schedule_inventory(coord):
        if exclude_schedule_id is not None and schedule.schedule_id == str(
            exclude_schedule_id
        ):
            continue
        existing_start = _minutes_of_day(schedule.start_time)
        existing_end = _minutes_of_day(schedule.end_time)
        if existing_start is None or existing_end is None:
            continue
        existing_segments = _weekly_schedule_segments(
            schedule.days,
            existing_start,
            existing_end,
        )
        if existing_segments and _segments_overlap(
            candidate_segments, existing_segments
        ):
            return schedule
    return None


def battery_schedule_overlap_message(
    schedule: BatteryScheduleRecord, *, hass: object | None = None
) -> str:
    label = battery_schedule_overlap_placeholders(schedule, hass=hass)["schedule_label"]
    if not label.endswith("schedule"):
        label = f"{label} schedule"
    return (
        f"Schedule overlaps with the existing {label}. "
        "Adjust or disable that schedule first."
    )


def battery_schedule_overlap_placeholders(
    schedule: BatteryScheduleRecord, *, hass: object | None = None
) -> dict[str, str]:
    schedule_type = battery_schedule_type_label(schedule.schedule_type, hass=hass)
    if not schedule_type:
        schedule_type = str(schedule.schedule_type).strip() or "battery"
    return {"schedule_label": schedule_type.lower()}


def editor_days_from_list(days: list[int]) -> dict[str, bool]:
    flags = {key: False for key, _ in DAY_ORDER}
    for day in days:
        key = DAY_KEY_BY_INDEX.get(day)
        if key is not None:
            flags[key] = True
    return flags


def days_list_from_editor(flags: dict[str, bool]) -> list[int]:
    return [index for key, index in DAY_ORDER if flags.get(key)]


def _family_control_enabled(
    coord: EnphaseCoordinator, schedule_type: str
) -> bool | None:
    normalized = str(schedule_type).lower()
    attr_name = {
        "cfg": "battery_charge_from_grid_schedule_enabled",
        "dtg": "battery_dtg_control_enabled",
        "rbd": "battery_rbd_control_enabled",
    }.get(normalized, "")
    value = getattr(coord, attr_name, None)
    return value if isinstance(value, bool) else None


def battery_scheduler_enabled(entry: EnphaseConfigEntry | None) -> bool:
    if entry is None:
        return False
    return bool(
        getattr(entry, "options", {}).get(
            OPT_BATTERY_SCHEDULES_ENABLED,
            DEFAULT_BATTERY_SCHEDULES_ENABLED,
        )
    )


def battery_schedule_type_options(
    *, hass: object | None = None
) -> list[tuple[str, str]]:
    return [
        (key, battery_schedule_type_label(key, hass=hass) or key.upper())
        for key in SCHEDULE_TYPE_KEYS
    ]


def battery_schedule_option_label(
    schedule: BatteryScheduleRecord, *, hass: object | None = None
) -> str:
    window = f"{schedule.start_time}-{schedule.end_time}"
    schedule_type = battery_schedule_type_label(schedule.schedule_type, hass=hass)
    if schedule_type is None:
        schedule_type = str(schedule.schedule_type).strip() or "Schedule"
    return f"{schedule_type} {window}"


@dataclass(slots=True)
class BatteryScheduleRecord:
    schedule_id: str
    schedule_type: str
    start_time: str
    end_time: str
    limit: int | None
    days: list[int]
    timezone: str | None
    enabled: bool | None
    schedule_status: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "schedule_id": self.schedule_id,
            "schedule_type": self.schedule_type,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "limit": self.limit,
            "days": list(self.days),
            "timezone": self.timezone,
            "enabled": self.enabled,
            "schedule_status": self.schedule_status,
        }


@dataclass(slots=True)
class BatteryScheduleFormState:
    selected_schedule_id: str | None = None
    create_mode: bool = False
    schedule_type: str = "cfg"
    start_time: str = "00:00"
    end_time: str = "00:00"
    limit: int = 100
    days: dict[str, bool] = field(default_factory=default_day_flags)

    def reset(self) -> None:
        self.selected_schedule_id = None
        self.create_mode = False
        self.schedule_type = "cfg"
        self.start_time = "00:00"
        self.end_time = "00:00"
        self.limit = 100
        self.days = default_day_flags()


def battery_schedule_inventory(
    coord: EnphaseCoordinator,
) -> list[BatteryScheduleRecord]:
    payload = getattr(coord, "_battery_schedules_payload", None)
    normalized: list[BatteryScheduleRecord] = []

    if isinstance(payload, dict):
        # BatteryConfig exposes each schedule family separately; normalize them
        # into one list for editor controls.
        for schedule_type in ("cfg", "dtg", "rbd"):
            family_payload = payload.get(schedule_type)
            if not isinstance(family_payload, dict):
                continue
            family_status = family_payload.get("scheduleStatus")
            family_status_text = (
                str(family_status).strip().lower()
                if isinstance(family_status, str) and family_status.strip()
                else None
            )
            details = family_payload.get("details")
            if not isinstance(details, list):
                continue
            for item in details:
                if not isinstance(item, dict):
                    continue
                schedule_id = item.get("scheduleId")
                if schedule_id is None:
                    continue
                limit_raw = item.get("limit")
                limit = None
                if isinstance(limit_raw, (int, float)):
                    limit = int(limit_raw)
                enabled_raw = item.get("isEnabled")
                enabled = _family_control_enabled(coord, schedule_type)
                if enabled is None and isinstance(enabled_raw, bool):
                    enabled = enabled_raw
                status_raw = item.get("scheduleStatus")
                status = (
                    str(status_raw).strip().lower()
                    if isinstance(status_raw, str) and status_raw.strip()
                    else family_status_text
                )
                timezone_raw = item.get("timezone")
                timezone = (
                    str(timezone_raw).strip()
                    if isinstance(timezone_raw, str) and timezone_raw.strip()
                    else None
                )
                normalized.append(
                    BatteryScheduleRecord(
                        schedule_id=str(schedule_id),
                        schedule_type=schedule_type,
                        start_time=_time_to_text(item.get("startTime")),
                        end_time=_time_to_text(item.get("endTime")),
                        limit=limit,
                        days=_normalize_days(item.get("days")),
                        timezone=timezone,
                        enabled=enabled,
                        schedule_status=status,
                    )
                )

    if normalized:
        return normalized

    # Fall back to coordinator scalar fields populated by older BatteryConfig
    # responses so existing schedule entities remain usable.
    fallback_specs = (
        (
            "cfg",
            getattr(coord, "_battery_cfg_schedule_id", None),
            getattr(coord, "battery_charge_from_grid_start_time", None),
            getattr(coord, "battery_charge_from_grid_end_time", None),
            getattr(coord, "_battery_cfg_schedule_limit", None),
            getattr(coord, "_battery_cfg_schedule_days", None),
            getattr(coord, "_battery_cfg_schedule_timezone", None),
            getattr(coord, "_battery_charge_from_grid_schedule_enabled", None),
            getattr(coord, "_battery_cfg_schedule_status", None),
        ),
        (
            "dtg",
            getattr(coord, "_battery_dtg_schedule_id", None),
            getattr(coord, "battery_discharge_to_grid_start_time", None),
            getattr(coord, "battery_discharge_to_grid_end_time", None),
            getattr(coord, "_battery_dtg_schedule_limit", None),
            getattr(coord, "_battery_dtg_schedule_days", None),
            getattr(coord, "_battery_dtg_schedule_timezone", None),
            getattr(coord, "_battery_dtg_schedule_enabled", None),
            getattr(coord, "_battery_dtg_schedule_status", None),
        ),
        (
            "rbd",
            getattr(coord, "_battery_rbd_schedule_id", None),
            getattr(coord, "battery_restrict_battery_discharge_start_time", None),
            getattr(coord, "battery_restrict_battery_discharge_end_time", None),
            getattr(coord, "_battery_rbd_schedule_limit", None),
            getattr(coord, "_battery_rbd_schedule_days", None),
            getattr(coord, "_battery_rbd_schedule_timezone", None),
            getattr(coord, "_battery_rbd_schedule_enabled", None),
            getattr(coord, "_battery_rbd_schedule_status", None),
        ),
    )
    for (
        schedule_type,
        schedule_id,
        start_time,
        end_time,
        limit,
        days,
        timezone,
        enabled,
        schedule_status,
    ) in fallback_specs:
        if schedule_id is None:
            continue
        normalized.append(
            BatteryScheduleRecord(
                schedule_id=str(schedule_id),
                schedule_type=schedule_type,
                start_time=_time_to_text(start_time),
                end_time=_time_to_text(end_time),
                limit=int(limit) if isinstance(limit, (int, float)) else None,
                days=_normalize_days(days),
                timezone=str(timezone).strip() if isinstance(timezone, str) else None,
                enabled=enabled if isinstance(enabled, bool) else None,
                schedule_status=(
                    str(schedule_status).strip().lower()
                    if isinstance(schedule_status, str) and schedule_status.strip()
                    else None
                ),
            )
        )
    return normalized


class BatteryScheduleEditorManager:
    def __init__(self, coordinator: EnphaseCoordinator) -> None:
        self.coordinator = coordinator
        self.edit = BatteryScheduleFormState()
        self.schedules: list[BatteryScheduleRecord] = []
        self._listeners: list[Callable[[], None]] = []
        self._auto_create_mode = False

    def option_label_by_schedule_id(
        self, *, hass: object | None = None
    ) -> dict[str, str]:
        raw_labels = {
            schedule.schedule_id: battery_schedule_option_label(schedule, hass=hass)
            for schedule in self.schedules
        }
        counts: dict[str, int] = {}
        for label in raw_labels.values():
            counts[label] = counts.get(label, 0) + 1
        labels: dict[str, str] = {}
        for schedule in self.schedules:
            label = raw_labels[schedule.schedule_id]
            if counts[label] > 1:
                # Multiple battery schedule families can share the same time
                # window, so include a short ID only for ambiguous labels.
                labels[schedule.schedule_id] = f"{label} [{schedule.schedule_id[:8]}]"
            else:
                labels[schedule.schedule_id] = label
        return labels

    def schedule_id_for_option_label(
        self, option: str, *, hass: object | None = None
    ) -> str | None:
        option_text = str(option).strip()
        for schedule_id, label in self.option_label_by_schedule_id(hass=hass).items():
            if label == option_text:
                return schedule_id
        return None

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        self._listeners.append(listener)

        def _unsub() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _unsub

    @callback
    def _notify_listeners(self) -> None:
        for listener in list(self._listeners):
            listener()

    def as_dicts(self) -> list[dict[str, object]]:
        return [schedule.as_dict() for schedule in self.schedules]

    def get_schedule(self, schedule_id: str | None) -> BatteryScheduleRecord | None:
        if not schedule_id:
            return None
        for schedule in self.schedules:
            if schedule.schedule_id == schedule_id:
                return schedule
        return None

    @callback
    def sync_from_coordinator(self) -> None:
        next_schedules = battery_schedule_inventory(self.coordinator)
        schedules_changed = next_schedules != self.schedules
        self.schedules = next_schedules

        if self.edit.create_mode:
            if self._auto_create_mode and self.schedules:
                self._apply_schedule_to_form(self.schedules[0])
                self._notify_listeners()
                return
            promoted = self._promote_created_schedule_from_form()
            if promoted is not None:
                self._apply_schedule_to_form(promoted)
                self._notify_listeners()
                return
            if schedules_changed:
                self._notify_listeners()
            return

        selected = self.get_schedule(self.edit.selected_schedule_id)
        if selected is None:
            fallback = self._default_schedule_selection()
            selection_before = (
                self.edit.selected_schedule_id,
                self.edit.create_mode,
                self.edit.schedule_type,
                self.edit.start_time,
                self.edit.end_time,
                self.edit.limit,
                self.edit.days,
            )
            if fallback == NEW_SCHEDULE_OPTION or fallback is None:
                self._set_create_mode_defaults(auto=True)
            elif isinstance(fallback, BatteryScheduleRecord):
                self._apply_schedule_to_form(fallback)
            selection_after = (
                self.edit.selected_schedule_id,
                self.edit.create_mode,
                self.edit.schedule_type,
                self.edit.start_time,
                self.edit.end_time,
                self.edit.limit,
                self.edit.days,
            )
            if schedules_changed or selection_before != selection_after:
                self._notify_listeners()
            return

        if not schedules_changed:
            return

        schedule_before = (
            self.edit.schedule_type,
            self.edit.start_time,
            self.edit.end_time,
            self.edit.limit,
            self.edit.days,
        )
        self._apply_schedule_to_form(selected)
        schedule_after = (
            self.edit.schedule_type,
            self.edit.start_time,
            self.edit.end_time,
            self.edit.limit,
            self.edit.days,
        )
        if schedules_changed or schedule_before != schedule_after:
            self._notify_listeners()

    def _apply_schedule_to_form(self, schedule: BatteryScheduleRecord) -> None:
        self.edit.selected_schedule_id = schedule.schedule_id
        self.edit.create_mode = False
        self._auto_create_mode = False
        self.edit.schedule_type = schedule.schedule_type
        self.edit.start_time = schedule.start_time
        self.edit.end_time = schedule.end_time
        self.edit.limit = int(schedule.limit) if schedule.limit is not None else 100
        self.edit.days = editor_days_from_list(schedule.days)

    def _default_schedule_selection(self) -> BatteryScheduleRecord | str | None:
        if self.schedules:
            return self.schedules[0]
        return NEW_SCHEDULE_OPTION

    def _promote_created_schedule_from_form(self) -> BatteryScheduleRecord | None:
        matches = [
            schedule
            for schedule in self.schedules
            if schedule.schedule_type == self.edit.schedule_type
            and schedule.start_time == self.edit.start_time
            and schedule.end_time == self.edit.end_time
            and (schedule.limit if schedule.limit is not None else 100)
            == self.edit.limit
            and schedule.days == days_list_from_editor(self.edit.days)
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    @callback
    def _set_create_mode_defaults(self, *, auto: bool = False) -> None:
        self.edit.reset()
        self.edit.create_mode = True
        self._auto_create_mode = auto

    @property
    def current_selection(self) -> str | None:
        if self.edit.create_mode:
            return NEW_SCHEDULE_OPTION
        return self.edit.selected_schedule_id

    @property
    def is_creating(self) -> bool:
        return self.edit.create_mode

    @callback
    def select_schedule(self, schedule_id: str) -> None:
        if schedule_id == NEW_SCHEDULE_OPTION:
            self._set_create_mode_defaults(auto=False)
            self._notify_listeners()
            return
        schedule = self.get_schedule(schedule_id)
        if schedule is None:
            self.edit.reset()
            self._auto_create_mode = False
        else:
            self._apply_schedule_to_form(schedule)
        self._notify_listeners()

    @callback
    def set_edit_time(self, key: str, value: dt_time) -> None:
        setattr(self.edit, key, value.strftime("%H:%M"))
        self._notify_listeners()

    @callback
    def set_edit_limit(self, value: int) -> None:
        self.edit.limit = int(value)
        self._notify_listeners()

    @callback
    def set_edit_day(self, day_key: str, value: bool) -> None:
        self.edit.days[day_key] = bool(value)
        self._notify_listeners()

    @callback
    def set_new_schedule_type(self, schedule_type: str) -> None:
        self.edit.schedule_type = schedule_type
        self._notify_listeners()


class BatteryScheduleEditorEntity(_CoordinatorEntityBase):
    def __init__(self, coord: EnphaseCoordinator, entry: EnphaseConfigEntry) -> None:
        super().__init__(coord)
        self._coord = coord
        self._entry = entry
        self._editor = get_runtime_data(entry).battery_schedule_editor
        self._editor_unsub: CALLBACK_TYPE | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._editor is not None:
            self._editor_unsub = self._editor.async_add_listener(
                self._handle_editor_update
            )

    async def async_will_remove_from_hass(self) -> None:
        if self._editor_unsub is not None:
            self._editor_unsub()
            self._editor_unsub = None
        await super().async_will_remove_from_hass()

    @callback
    def _handle_editor_update(self) -> None:
        self.async_write_ha_state()
