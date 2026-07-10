"""Dry contact settings parsing helpers for battery runtime."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

CoerceBool = Callable[[object], bool | None]
CoerceInt = Callable[[object], int | None]
CoerceText = Callable[[object], str | None]


@dataclass(frozen=True)
class DryContactSettingsParseResult:
    """Normalized dry contact settings payload result."""

    entries: list[dict[str, object]]
    unmatched: list[dict[str, object]]
    supported: bool


DRY_CONTACT_ENTRY_KEYS = (
    "serial_number",
    "serial",
    "serialNumber",
    "device_uid",
    "device-uid",
    "deviceUid",
    "uid",
    "contact_id",
    "contactId",
    "id",
    "channel_type",
    "channelType",
    "meter_type",
    "name",
    "displayName",
    "configuredName",
    "overrideSupported",
    "overrideActive",
    "controlMode",
    "pollingInterval",
    "pollingIntervalSeconds",
    "socThreshold",
    "socThresholdMin",
    "socThresholdMax",
    "scheduleWindows",
    "schedule_windows",
    "schedule",
    "schedules",
    "windows",
)


def _first_present(source: dict[str, object], *keys: str) -> object:
    """Return the first present, non-None value for a known set of aliases."""

    for key in keys:
        value = source.get(key)
        if value is not None:
            return value
    return None


def copy_dry_contact_settings_entry(entry: dict[str, object]) -> dict[str, object]:
    """Return a shallow copy that also detaches nested dict/list containers."""

    copied: dict[str, object] = {}
    for key, value in entry.items():
        if isinstance(value, dict):
            copied[key] = dict(value)
        elif isinstance(value, list):
            copied[key] = [
                dict(item) if isinstance(item, dict) else item for item in value
            ]
        else:
            copied[key] = value
    return copied


def dry_contact_settings_looks_like_entry(entry: object) -> bool:
    """Return whether a payload node resembles a dry-contact settings entry."""

    return isinstance(entry, dict) and any(
        key in entry for key in DRY_CONTACT_ENTRY_KEYS
    )


def normalize_dry_contact_schedule_windows(
    windows: object, coerce_text: CoerceText
) -> list[dict[str, object]]:
    """Normalize dry-contact schedule windows from known payload aliases."""

    if isinstance(windows, list):
        candidates = [item for item in windows if isinstance(item, dict)]
    elif isinstance(windows, dict):
        candidates = [windows]
    else:
        return []
    normalized_windows: list[dict[str, object]] = []
    seen: set[tuple[str | None, str | None]] = set()
    for item in candidates:
        start = coerce_text(
            _first_present(
                item,
                "start",
                "startTime",
                "begin",
                "beginTime",
                "from",
                "windowStart",
            )
        )
        end = coerce_text(
            _first_present(
                item,
                "end",
                "endTime",
                "finish",
                "finishTime",
                "to",
                "windowEnd",
            )
        )
        if start is None and end is None:
            continue
        dedupe_key = (start, end)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        normalized: dict[str, object] = {}
        if start is not None:
            normalized["start"] = start
        if end is not None:
            normalized["end"] = end
        normalized_windows.append(normalized)
    return normalized_windows


def dry_contact_identity_candidates(
    value: dict[str, object], coerce_text: CoerceText
) -> list[tuple[str, str]]:
    """Build normalized identity candidates from known dry-contact aliases."""

    candidates: list[tuple[str, str]] = []

    def _add(identity_key: str, raw_value: object) -> None:
        text = coerce_text(raw_value)
        if text is None:
            return
        candidates.append((identity_key, text.casefold()))

    _add(
        "serial_number",
        _first_present(value, "serial_number", "serial", "serialNumber"),
    )
    _add(
        "device_uid",
        _first_present(
            value, "device_uid", "device-uid", "deviceUid", "iqer_uid", "iqer-uid"
        ),
    )
    _add("uid", value.get("uid"))
    _add("contact_id", _first_present(value, "contact_id", "contactId", "id"))
    _add(
        "channel_type",
        _first_present(value, "channel_type", "channelType", "meter_type", "type"),
    )
    _add(
        "name",
        _first_present(
            value,
            "configured_name",
            "display_name",
            "name",
            "displayName",
            "configuredName",
            "label",
        ),
    )
    return candidates


def dry_contact_identity_map(
    value: dict[str, object], coerce_text: CoerceText
) -> dict[str, str]:
    """Build a dry-contact identity map from known aliases."""

    return dict(dry_contact_identity_candidates(value, coerce_text))


def dry_contact_member_dedupe_key(
    identities: dict[str, str], index: int
) -> tuple[tuple[str, str], ...]:
    """Return the most specific stable dedupe key for a dry-contact member."""

    for keys in (
        ("device_uid", "contact_id"),
        ("device_uid", "channel_type"),
        ("uid", "contact_id"),
        ("uid", "channel_type"),
        ("contact_id", "channel_type"),
        ("serial_number", "channel_type"),
        ("serial_number", "contact_id"),
        ("contact_id",),
        ("channel_type",),
        ("serial_number",),
        ("device_uid",),
        ("uid",),
        ("name",),
    ):
        if all(identities.get(key) is not None for key in keys):
            return tuple((key, identities[key]) for key in keys)
    return (("idx", str(index)),)


def dry_contact_match_conflicts(
    member_identities: dict[str, str],
    entry_identities: dict[str, str],
) -> bool:
    """Return whether two partially matched dry-contact identities conflict."""

    for key in (
        "contact_id",
        "channel_type",
        "serial_number",
        "device_uid",
        "uid",
    ):
        member_value = member_identities.get(key)
        entry_value = entry_identities.get(key)
        if member_value is None or entry_value is None:
            continue
        if member_value != entry_value:
            return True
    return False


def dry_contact_member_is_dry_contact(member: object, coerce_text: CoerceText) -> bool:
    """Return whether an inventory member appears to be a dry-contact relay."""

    if not isinstance(member, dict):
        return False
    for key in ("channel_type", "channelType", "meter_type", "type", "name"):
        value = coerce_text(member.get(key))
        if value is None:
            continue
        compact = value.casefold().replace("-", "").replace("_", "").replace(" ", "")
        if compact in {"nc1", "nc2", "no1", "no2"}:
            return True
        if "drycontact" in compact:
            return True
        if "relay" in compact and any(token in compact for token in ("nc", "no")):
            return True
    return False


def match_dry_contact_settings(
    members: list[dict[str, object]],
    *,
    settings_entries: list[dict[str, object]],
    coerce_text: CoerceText,
) -> tuple[list[dict[str, object] | None], list[dict[str, object]]]:
    """Match normalized dry-contact settings entries to inventory members."""

    members_list = [dict(member) for member in members if isinstance(member, dict)]
    member_identity_maps = [
        dry_contact_identity_map(member, coerce_text) for member in members_list
    ]
    index_by_key: dict[str, dict[str, list[int]]] = {
        key: {}
        for key in (
            "contact_id",
            "channel_type",
            "serial_number",
            "device_uid",
            "uid",
            "name",
        )
    }
    for index, identities in enumerate(member_identity_maps):
        for key, mapping in index_by_key.items():
            value = identities.get(key)
            if value is None:
                continue
            mapping.setdefault(value, []).append(index)

    entries = [
        copy_dry_contact_settings_entry(entry)
        for entry in settings_entries
        if isinstance(entry, dict)
    ]
    matches: list[dict[str, object] | None] = [None] * len(members_list)
    unmatched: list[dict[str, object]] = []
    used_member_indexes: set[int] = set()

    for entry in entries:
        entry_identities = dry_contact_identity_map(entry, coerce_text)
        matched_member_index: int | None = None
        for key in (
            "contact_id",
            "channel_type",
            "serial_number",
            "device_uid",
            "uid",
            "name",
        ):
            value = entry_identities.get(key)
            if value is None:
                continue
            candidate_indexes = [
                index
                for index in index_by_key[key].get(value, [])
                if index not in used_member_indexes
            ]
            if len(candidate_indexes) != 1:
                continue
            candidate_index = candidate_indexes[0]
            if dry_contact_match_conflicts(
                member_identity_maps[candidate_index], entry_identities
            ):
                continue
            matched_member_index = candidate_index
            break
        if matched_member_index is None:
            unmatched.append(entry)
            continue
        used_member_indexes.add(matched_member_index)
        matches[matched_member_index] = entry
    return matches, unmatched


def parse_dry_contact_settings_payload(
    payload: object,
    *,
    members: list[dict[str, object]],
    coerce_bool: CoerceBool,
    coerce_int: CoerceInt,
    coerce_text: CoerceText,
) -> DryContactSettingsParseResult:
    """Parse and normalize dry-contact settings from an Enphase payload."""

    if not isinstance(payload, dict):
        return DryContactSettingsParseResult([], [], False)
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload
    raw_entries: list[dict[str, object]] = []
    visited: set[int] = set()

    def _visit(node: object, depth: int = 0) -> None:
        if depth > 4:
            return
        if isinstance(node, dict):
            node_id = id(node)
            if node_id in visited:
                return
            visited.add(node_id)
            if dry_contact_settings_looks_like_entry(node):
                raw_entries.append(node)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    _visit(value, depth + 1)
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, (dict, list)):
                    _visit(item, depth + 1)

    _visit(data)
    entries: list[dict[str, object]] = []
    seen_signatures: set[tuple[object, ...]] = set()
    for entry in raw_entries:
        normalized = _normalize_dry_contact_settings_entry(
            entry,
            coerce_bool=coerce_bool,
            coerce_int=coerce_int,
            coerce_text=coerce_text,
        )
        if not normalized:
            continue
        signature = _dry_contact_settings_signature(normalized)
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        entries.append(normalized)
    _matches, unmatched = match_dry_contact_settings(
        members,
        settings_entries=entries,
        coerce_text=coerce_text,
    )
    return DryContactSettingsParseResult(entries, unmatched, True)


def _normalize_dry_contact_settings_entry(
    entry: dict[str, object],
    *,
    coerce_bool: CoerceBool,
    coerce_int: CoerceInt,
    coerce_text: CoerceText,
) -> dict[str, object]:
    normalized: dict[str, object] = {}

    text_fields: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("serial_number", ("serial_number", "serial", "serialNumber", "deviceSerial")),
        (
            "device_uid",
            ("device_uid", "device-uid", "deviceUid", "iqer_uid", "iqer-uid"),
        ),
        ("uid", ("uid",)),
        ("contact_id", ("contact_id", "contactId", "id")),
        ("channel_type", ("channel_type", "channelType", "meter_type", "type")),
        (
            "configured_name",
            (
                "configured_name",
                "configuredName",
                "display_name",
                "displayName",
                "name",
                "label",
            ),
        ),
        ("control_mode", ("control_mode", "controlMode", "mode", "operatingMode")),
    )
    for target_key, source_keys in text_fields:
        value = coerce_text(_first_present(entry, *source_keys))
        if value is not None:
            normalized[target_key] = value

    bool_fields: tuple[tuple[str, tuple[str, ...]], ...] = (
        (
            "override_supported",
            (
                "override_supported",
                "overrideSupported",
                "isOverrideSupported",
                "supportsOverride",
                "allowOverride",
                "canOverride",
            ),
        ),
        (
            "override_active",
            (
                "override_active",
                "overrideActive",
                "override",
                "isOverrideActive",
                "manualOverride",
            ),
        ),
    )
    for target_key, source_keys in bool_fields:
        bool_value = coerce_bool(_first_present(entry, *source_keys))
        if bool_value is not None:
            normalized[target_key] = bool_value

    int_fields: tuple[tuple[str, tuple[str, ...]], ...] = (
        (
            "polling_interval_seconds",
            (
                "polling_interval_seconds",
                "pollingIntervalSeconds",
                "pollingInterval",
                "polling_interval",
            ),
        ),
        (
            "soc_threshold",
            (
                "soc_threshold",
                "socThreshold",
                "thresholdSoc",
                "targetSoc",
                "setPointSoc",
                "soc",
            ),
        ),
        (
            "soc_threshold_min",
            (
                "soc_threshold_min",
                "socThresholdMin",
                "minimumSoc",
                "minSoc",
                "minSocThreshold",
            ),
        ),
        (
            "soc_threshold_max",
            (
                "soc_threshold_max",
                "socThresholdMax",
                "maximumSoc",
                "maxSoc",
                "maxSocThreshold",
            ),
        ),
    )
    for target_key, source_keys in int_fields:
        int_value = coerce_int(_first_present(entry, *source_keys))
        if int_value is not None:
            normalized[target_key] = int_value

    schedule_windows = normalize_dry_contact_schedule_windows(
        _first_present(
            entry,
            "schedule_windows",
            "scheduleWindows",
            "schedule",
            "schedules",
            "windows",
            "window",
        ),
        coerce_text,
    )
    if not schedule_windows:
        fallback_start = coerce_text(
            _first_present(
                entry, "scheduleStart", "schedule_start", "windowStart", "startTime"
            )
        )
        fallback_end = coerce_text(
            _first_present(entry, "scheduleEnd", "schedule_end", "windowEnd", "endTime")
        )
        if fallback_start is not None or fallback_end is not None:
            schedule_window: dict[str, object] = {}
            if fallback_start is not None:
                schedule_window["start"] = fallback_start
            if fallback_end is not None:
                schedule_window["end"] = fallback_end
            schedule_windows = [schedule_window]
    if schedule_windows:
        normalized["schedule_windows"] = schedule_windows
    return normalized


def _dry_contact_settings_signature(
    normalized: dict[str, object],
) -> tuple[object, ...]:
    raw_schedule_windows = normalized.get("schedule_windows")
    schedule_windows = (
        raw_schedule_windows if isinstance(raw_schedule_windows, list) else []
    )
    return (
        normalized.get("serial_number"),
        normalized.get("device_uid"),
        normalized.get("uid"),
        normalized.get("contact_id"),
        normalized.get("channel_type"),
        normalized.get("configured_name"),
        normalized.get("override_supported"),
        normalized.get("override_active"),
        normalized.get("control_mode"),
        normalized.get("polling_interval_seconds"),
        normalized.get("soc_threshold"),
        normalized.get("soc_threshold_min"),
        normalized.get("soc_threshold_max"),
        tuple(
            (
                window.get("start") if isinstance(window, dict) else None,
                window.get("end") if isinstance(window, dict) else None,
            )
            for window in schedule_windows
            if isinstance(window, dict)
        ),
    )
