"""Keep EVSE schedule editor form state in sync with scheduler slots."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time as dt_time
from typing import TYPE_CHECKING, Any, Callable, TypeVar, cast
import uuid

from homeassistant.core import CALLBACK_TYPE, callback as ha_callback

from .const import DEFAULT_SCHEDULE_SYNC_ENABLED, OPT_SCHEDULE_SYNC_ENABLED
from .coordinator import EnphaseCoordinator
from .entity import EnphaseBaseEntity
from .labels import evse_schedule_create_label as _evse_schedule_create_label
from .runtime_data import EnphaseConfigEntry, get_runtime_data

if TYPE_CHECKING:  # pragma: no cover

    class _EnphaseBaseEntity:
        """Typed view of the external integration entity base."""

        def __init__(self, coord: EnphaseCoordinator, sn: str) -> None: ...

        async def async_added_to_hass(self) -> None: ...

        async def async_will_remove_from_hass(self) -> None: ...

        def async_write_ha_state(self) -> None: ...

else:
    _EnphaseBaseEntity = EnphaseBaseEntity

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


def default_day_flags() -> dict[str, bool]:
    return {key: False for key, _ in DAY_ORDER}


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


def editor_days_from_list(days: list[int]) -> dict[str, bool]:
    flags = default_day_flags()
    for day in days:
        key = DAY_KEY_BY_INDEX.get(day)
        if key is not None:
            flags[key] = True
    return flags


def days_list_from_editor(flags: dict[str, bool]) -> list[int]:
    return [index for key, index in DAY_ORDER if flags.get(key)]


def evse_scheduler_enabled(entry: EnphaseConfigEntry | None) -> bool:
    if entry is None:
        return False
    return bool(
        getattr(entry, "options", {}).get(
            OPT_SCHEDULE_SYNC_ENABLED,
            DEFAULT_SCHEDULE_SYNC_ENABLED,
        )
    )


def evse_schedule_create_label(*, hass: object | None = None) -> str:
    return _evse_schedule_create_label(hass=hass)


def evse_schedule_editor_active(
    coord: EnphaseCoordinator, entry: EnphaseConfigEntry | None
) -> bool:
    client = getattr(coord, "client", None)
    schedule_sync = getattr(coord, "schedule_sync", None)
    return bool(
        evse_scheduler_enabled(entry)
        and schedule_sync is not None
        and callable(getattr(client, "get_schedules", None))
        and callable(getattr(client, "patch_schedule", None))
        and callable(getattr(client, "create_schedule", None))
        and callable(getattr(client, "delete_schedule", None))
    )


def _slot_limit(slot: dict[str, Any], *, default: int = 32) -> int:
    for key in ("chargingLevelAmp", "chargingLevel"):
        raw = slot.get(key)
        if isinstance(raw, bool):
            continue
        if isinstance(raw, (int, float)):
            return int(raw)
        if isinstance(raw, str):
            try:
                return int(raw.strip())
            except ValueError:
                continue
    return default


def _editor_default_limit(coord: EnphaseCoordinator, sn: str) -> int:
    data: dict[str, object] = {}
    try:
        data = (coord.data or {}).get(sn) or {}
    except Exception:
        data = {}
    for key in ("charging_level", "max_amp", "min_amp"):
        raw = data.get(key)
        if isinstance(raw, bool):
            continue
        if isinstance(raw, (int, float)):
            return int(raw)
        if isinstance(raw, str):
            try:
                return int(raw.strip())
            except ValueError:
                continue
    picker = getattr(coord, "pick_start_amps", None)
    if callable(picker):
        try:
            return int(picker(sn))
        except Exception:
            return 32
    return 32


@dataclass(slots=True)
class EvseScheduleRecord:
    slot_id: str
    schedule_type: str
    start_time: str
    end_time: str
    limit: int
    days: list[int]
    enabled: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "slot_id": self.slot_id,
            "schedule_type": self.schedule_type,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "limit": self.limit,
            "days": list(self.days),
            "enabled": self.enabled,
        }


@dataclass(slots=True)
class EvseScheduleFormState:
    selected_slot_id: str | None = None
    create_mode: bool = False
    start_time: str = "00:00"
    end_time: str = "00:00"
    limit: int = 32
    days: dict[str, bool] = field(default_factory=default_day_flags)

    def reset(self, *, default_limit: int) -> None:
        self.selected_slot_id = None
        self.create_mode = False
        self.start_time = "00:00"
        self.end_time = "00:00"
        self.limit = default_limit
        self.days = default_day_flags()


def evse_schedule_inventory(
    coord: EnphaseCoordinator,
    sn: str,
) -> list[EvseScheduleRecord]:
    schedule_sync = getattr(coord, "schedule_sync", None)
    if schedule_sync is None:
        return []
    slot_map = getattr(schedule_sync, "_slot_cache", {}).get(sn)
    if not isinstance(slot_map, dict):
        return []

    normalized: list[EvseScheduleRecord] = []
    default_limit = _editor_default_limit(coord, sn)
    for slot in slot_map.values():
        if not isinstance(slot, dict):
            continue
        slot_id = str(slot.get("id") or "").strip()
        if not slot_id:
            continue
        schedule_type = str(slot.get("scheduleType") or "CUSTOM").strip().upper()
        if schedule_type == "OFF_PEAK":
            # Off-peak schedules are backend-managed and not editable through the
            # custom schedule form.
            continue
        start_time = slot.get("startTime")
        end_time = slot.get("endTime")
        if start_time is None or end_time is None:
            continue
        normalized.append(
            EvseScheduleRecord(
                slot_id=slot_id,
                schedule_type=schedule_type,
                start_time=_time_to_text(start_time),
                end_time=_time_to_text(end_time),
                limit=_slot_limit(slot, default=default_limit),
                days=_normalize_days(slot.get("days")),
                enabled=bool(slot.get("enabled", True)),
            )
        )

    return sorted(
        normalized, key=lambda item: (item.start_time, item.end_time, item.slot_id)
    )


def evse_schedule_option_label(schedule: EvseScheduleRecord) -> str:
    return f"{schedule.start_time}-{schedule.end_time} ({schedule.limit} A)"


class EvseScheduleEditorManager:
    def __init__(self, coordinator: EnphaseCoordinator) -> None:
        self.coordinator = coordinator
        self.forms: dict[str, EvseScheduleFormState] = {}
        self.schedules_by_serial: dict[str, list[EvseScheduleRecord]] = {}
        self._listeners: list[Callable[[], None]] = []
        self._auto_create_mode: dict[str, bool] = {}

    def _form(self, sn: str) -> EvseScheduleFormState:
        form = self.forms.get(sn)
        if form is None:
            form = EvseScheduleFormState()
            form.reset(default_limit=_editor_default_limit(self.coordinator, sn))
            self.forms[sn] = form
        return form

    def _default_limit(self, sn: str) -> int:
        return _editor_default_limit(self.coordinator, sn)

    def option_label_by_slot_id(self, sn: str) -> dict[str, str]:
        schedules = self.schedules_by_serial.get(sn, [])
        raw_labels = {
            schedule.slot_id: evse_schedule_option_label(schedule)
            for schedule in schedules
        }
        counts: dict[str, int] = {}
        for label in raw_labels.values():
            counts[label] = counts.get(label, 0) + 1
        labels: dict[str, str] = {}
        for schedule in schedules:
            label = raw_labels[schedule.slot_id]
            if counts[label] > 1:
                # Duplicate windows are common, so include a short slot suffix
                # only when the label would otherwise collide.
                labels[schedule.slot_id] = f"{label} [{schedule.slot_id[-6:]}]"
            else:
                labels[schedule.slot_id] = label
        return labels

    def slot_id_for_option_label(self, sn: str, option: str) -> str | None:
        option_text = str(option).strip()
        for slot_id, label in self.option_label_by_slot_id(sn).items():
            if label == option_text:
                return slot_id
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

    def as_dicts(self, sn: str) -> list[dict[str, object]]:
        return [schedule.as_dict() for schedule in self.schedules_by_serial.get(sn, [])]

    def form_state(self, sn: str) -> EvseScheduleFormState:
        return self._form(sn)

    def get_schedule(self, sn: str, slot_id: str | None) -> EvseScheduleRecord | None:
        if not slot_id:
            return None
        for schedule in self.schedules_by_serial.get(sn, []):
            if schedule.slot_id == slot_id:
                return schedule
        return None

    @callback
    def sync_from_coordinator(self) -> None:
        serials = set(self.forms)
        iter_serials = getattr(self.coordinator, "iter_serials", None)
        if callable(iter_serials):
            try:
                serials.update(str(sn) for sn in iter_serials() if sn)
            except Exception:
                pass
        slot_cache = getattr(
            getattr(self.coordinator, "schedule_sync", None), "_slot_cache", {}
        )
        if isinstance(slot_cache, dict):
            serials.update(str(sn) for sn in slot_cache if sn)

        changed = False
        for sn in serials:
            if self._sync_serial(sn):
                changed = True
        if changed:
            self._notify_listeners()

    def _sync_serial(self, sn: str) -> bool:
        next_schedules = evse_schedule_inventory(self.coordinator, sn)
        current_schedules = self.schedules_by_serial.get(sn, [])
        schedules_changed = next_schedules != current_schedules
        self.schedules_by_serial[sn] = next_schedules

        form = self._form(sn)
        if form.create_mode:
            if self._auto_create_mode.get(sn, False) and next_schedules:
                self._apply_schedule_to_form(sn, next_schedules[0])
                return True
            promoted = self._promote_created_schedule_from_form(sn)
            if promoted is not None:
                self._apply_schedule_to_form(sn, promoted)
                return True
            return schedules_changed

        selected = self.get_schedule(sn, form.selected_slot_id)
        if selected is None:
            fallback = self._default_schedule_selection(sn)
            selection_before = (
                form.selected_slot_id,
                form.create_mode,
                form.start_time,
                form.end_time,
                form.limit,
                dict(form.days),
            )
            if fallback == NEW_SCHEDULE_OPTION or fallback is None:
                self._set_create_mode_defaults(sn, auto=True)
            elif isinstance(fallback, EvseScheduleRecord):
                self._apply_schedule_to_form(sn, fallback)
            selection_after = (
                form.selected_slot_id,
                form.create_mode,
                form.start_time,
                form.end_time,
                form.limit,
                dict(form.days),
            )
            return schedules_changed or selection_before != selection_after

        if not schedules_changed:
            return False

        schedule_before = (
            form.start_time,
            form.end_time,
            form.limit,
            dict(form.days),
        )
        self._apply_schedule_to_form(sn, selected)
        schedule_after = (
            form.start_time,
            form.end_time,
            form.limit,
            dict(form.days),
        )
        return schedules_changed or schedule_before != schedule_after

    def _apply_schedule_to_form(self, sn: str, schedule: EvseScheduleRecord) -> None:
        form = self._form(sn)
        form.selected_slot_id = schedule.slot_id
        form.create_mode = False
        self._auto_create_mode[sn] = False
        form.start_time = schedule.start_time
        form.end_time = schedule.end_time
        form.limit = int(schedule.limit)
        form.days = editor_days_from_list(schedule.days)

    def _default_schedule_selection(self, sn: str) -> EvseScheduleRecord | str | None:
        schedules = self.schedules_by_serial.get(sn, [])
        if schedules:
            return schedules[0]
        return NEW_SCHEDULE_OPTION

    def _promote_created_schedule_from_form(self, sn: str) -> EvseScheduleRecord | None:
        form = self._form(sn)
        matches = [
            schedule
            for schedule in self.schedules_by_serial.get(sn, [])
            if schedule.start_time == form.start_time
            and schedule.end_time == form.end_time
            and schedule.days == days_list_from_editor(form.days)
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    @callback
    def _set_create_mode_defaults(self, sn: str, *, auto: bool = False) -> None:
        form = self._form(sn)
        form.reset(default_limit=self._default_limit(sn))
        form.create_mode = True
        self._auto_create_mode[sn] = auto

    def current_selection(self, sn: str) -> str | None:
        form = self._form(sn)
        if form.create_mode:
            return NEW_SCHEDULE_OPTION
        return form.selected_slot_id

    def is_creating(self, sn: str) -> bool:
        return self._form(sn).create_mode

    @callback
    def select_schedule(self, sn: str, slot_id: str) -> None:
        if slot_id == NEW_SCHEDULE_OPTION:
            self._set_create_mode_defaults(sn, auto=False)
            self._notify_listeners()
            return
        schedule = self.get_schedule(sn, slot_id)
        if schedule is None:
            self._form(sn).reset(default_limit=self._default_limit(sn))
            self._auto_create_mode[sn] = False
        else:
            self._apply_schedule_to_form(sn, schedule)
        self._notify_listeners()

    @callback
    def set_edit_time(self, sn: str, key: str, value: dt_time) -> None:
        setattr(self._form(sn), key, value.strftime("%H:%M"))
        self._notify_listeners()

    @callback
    def set_edit_limit(self, sn: str, value: int) -> None:
        self._form(sn).limit = int(value)
        self._notify_listeners()

    @callback
    def set_edit_day(self, sn: str, day_key: str, value: bool) -> None:
        self._form(sn).days[day_key] = bool(value)
        self._notify_listeners()

    def build_slot_payload(
        self, sn: str, *, slot_id: str | None = None
    ) -> dict[str, Any]:
        form = self._form(sn)
        existing_id = slot_id or form.selected_slot_id
        schedule_sync = getattr(self.coordinator, "schedule_sync", None)
        existing_slot = None
        if schedule_sync is not None and existing_id:
            existing_slot = schedule_sync.get_slot(sn, existing_id)
        slot: dict[str, Any] = dict(existing_slot or {})
        # Start from the existing slot so scheduler-owned fields survive edits.
        slot["id"] = existing_id or f"{self.coordinator.site_id}:{sn}:{uuid.uuid4()}"
        slot["startTime"] = form.start_time
        slot["endTime"] = form.end_time
        slot["chargingLevel"] = int(form.limit)
        slot["chargingLevelAmp"] = int(form.limit)
        slot["scheduleType"] = "CUSTOM"
        slot["days"] = days_list_from_editor(form.days)
        slot["enabled"] = bool(slot.get("enabled", True))
        slot.setdefault("remindFlag", False)
        slot.setdefault("remindTime", None)
        slot.setdefault("recurringKind", "Recurring")
        slot.setdefault("chargeLevelType", "Weekly")
        slot.setdefault("sourceType", "SYSTEM")
        slot.setdefault("reminderTimeUtc", None)
        slot.setdefault("serializedDays", None)
        return slot


class EvseScheduleEditorEntity(_EnphaseBaseEntity):
    def __init__(
        self,
        coord: EnphaseCoordinator,
        entry: EnphaseConfigEntry,
        sn: str,
    ) -> None:
        super().__init__(coord, sn)
        self._entry = entry
        self._editor = get_runtime_data(entry).evse_schedule_editor
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
