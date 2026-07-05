#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
import http.cookiejar
import socket
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://enlighten.enphaseenergy.com"
ENTREZ_URL = "https://entrez.enphaseenergy.com"
LOGIN_URL = f"{BASE_URL}/login/login.json"
SITE_SEARCH_URL = f"{BASE_URL}/app-api/search_sites.json?searchText=&favourite=false"

DEFAULT_TIMEOUT = 10
DEFAULT_HISTORY_DAYS = 30
DEFAULT_WIKI_PAGE = "Service-Status-History.md"
MIN_VISIBLE_INCIDENT_MINUTES = 60
MAX_INCIDENT_SAMPLE_GAP_MINUTES = 90
DEFAULT_REPOSITORY = "barneyonline/ha-enphase-energy"
BROAD_OUTAGE_FAILED_CHECK_THRESHOLD = 3
BROAD_OUTAGE_ENDPOINT_CATEGORIES = frozenset(
    {
        "battery_config",
        "battery_runtime",
        "evse_management",
        "evse_timeseries",
        "hems",
        "inventory",
        "microinverters",
        "site_live",
        "system_dashboard",
    }
)


@dataclass(frozen=True)
class EndpointSpec:
    name: str
    method: str
    url: str
    group: str
    category: str
    headers: dict[str, str] | None = None
    form: dict[str, Any] | None = None
    json_body: Any | None = None
    affects_status: bool = True
    affects_broad_outage: bool | None = None
    check_group: str | None = None
    check_mode: str = "all"  # "all" or "any"
    ok_statuses: tuple[int, ...] = (200,)


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso_utc(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except ValueError:
        return None


def _format_utc(value: str | None) -> str:
    dt = _parse_iso_utc(value)
    if dt is None:
        return "-"
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _format_mermaid_label_utc(value: str | None) -> str:
    dt = _parse_iso_utc(value)
    if dt is None:
        return "-"
    # Mermaid Gantt uses ":" as the task metadata separator, so labels must not
    # include raw HH:MM timestamps.
    return dt.strftime("%Y-%m-%d %H%M UTC")


def _mermaid_datetime(value: str | None) -> str:
    dt = _parse_iso_utc(value)
    if dt is None:
        dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _duration_label(minutes: int) -> str:
    if minutes <= 0:
        return "0m"
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


def _safe_slug(text: str) -> str:
    cleaned = []
    for char in text.lower():
        if char.isalnum():
            cleaned.append(char)
        elif cleaned and cleaned[-1] != "-":
            cleaned.append("-")
    return "".join(cleaned).strip("-") or "item"


def _reason_failure_name(reason: str | None) -> str | None:
    if not reason:
        return None
    mapping = {
        "Missing ENPHASE_EMAIL or ENPHASE_PASSWORD": "missing_credentials",
        "Login failed": "auth_login",
        "Account requires MFA; workflow cannot continue": "auth_mfa_required",
        "Account is blocked": "account_blocked",
        "Login rejected": "login_rejected",
        "Site discovery failed": "site_discovery",
        "Serial discovery failed": "serial_discovery",
    }
    return mapping.get(reason)


def _default_raw_base_url() -> str:
    repository = os.environ.get("GITHUB_REPOSITORY") or DEFAULT_REPOSITORY
    return f"https://raw.githubusercontent.com/{repository}/service-status"


def _text_width(text: str) -> int:
    return max(30, 6 * len(text) + 10)


def _badge_svg(label: str, value: str, color: str) -> str:
    label_w = _text_width(label)
    value_w = _text_width(value)
    total_w = label_w + value_w
    label_x = label_w // 2
    value_x = label_w + (value_w // 2)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total_w}" height="20" '
        f'role="img" aria-label="{label}: {value}">'
        f'<linearGradient id="s" x2="0" y2="100%">'
        f'<stop offset="0" stop-color="#bbb" stop-opacity=".1"/>'
        f'<stop offset="1" stop-opacity=".1"/></linearGradient>'
        f'<rect width="{label_w}" height="20" fill="#555"/>'
        f'<rect x="{label_w}" width="{value_w}" height="20" fill="{color}"/>'
        f'<rect width="{total_w}" height="20" fill="url(#s)"/>'
        f'<g fill="#fff" text-anchor="middle" '
        f'font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11">'
        f'<text x="{label_x}" y="14">{label}</text>'
        f'<text x="{value_x}" y="14">{value}</text>'
        f"</g></svg>"
    )


def _shields_badge_payload(label: str, value: str, color: str) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "label": label,
        "message": value,
        "color": color,
    }


def _decode_jwt_payload(token: str) -> dict[str, Any] | None:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _jwt_user_id(token: str | None) -> str | None:
    if not token:
        return None
    payload = _decode_jwt_payload(token)
    if not payload:
        return None
    for key in ("user_id", "userId", "userid"):
        value = payload.get(key)
        if value is not None:
            return str(value)
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("user_id", "userId", "userid"):
            value = data.get(key)
            if value is not None:
                return str(value)
    return None


def _jwt_session_id(token: str | None) -> str | None:
    if not token:
        return None
    payload = _decode_jwt_payload(token)
    if not payload:
        return None
    for key in ("session_id", "sessionId", "session"):
        value = payload.get(key)
        if value is not None:
            return str(value)
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("session_id", "sessionId", "session"):
            value = data.get(key)
            if value is not None:
                return str(value)
    return None


def _cookie_header_for(url: str, jar: http.cookiejar.CookieJar) -> str:
    req = urllib.request.Request(url)
    try:
        jar.add_cookie_header(req)
    except Exception:
        return ""
    return req.get_header("Cookie") or ""


def _cookie_map(cookie_header: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in cookie_header.split(";"):
        if "=" not in part:
            continue
        key, value = part.strip().split("=", 1)
        if key:
            out[key] = value
    return out


def _extract_xsrf_token(cookies: dict[str, str]) -> str | None:
    for key, value in cookies.items():
        if key.lower() == "xsrf-token":
            return value
    return None


def _extract_bearer(cookies: dict[str, str]) -> str | None:
    return cookies.get("enlighten_manager_token_production")


def _request(
    opener: urllib.request.OpenerDirector,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    form: dict[str, Any] | None = None,
    json_body: Any | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[int | None, dict[str, str], bytes, str | None]:
    req_headers = dict(headers or {})
    data: bytes | None = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        req_headers.setdefault(
            "Content-Type", "application/x-www-form-urlencoded; charset=UTF-8"
        )

    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with opener.open(req, timeout=timeout) as resp:
            status = resp.getcode()
            body = resp.read()
            return status, dict(resp.headers), body, None
    except urllib.error.HTTPError as err:
        try:
            body = err.read()
        except Exception:
            body = b""
        return err.code, dict(err.headers), body, f"HTTP {err.code}"
    except (urllib.error.URLError, socket.timeout) as err:
        return None, {}, b"", str(err)


def _parse_json(body: bytes) -> Any | None:
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return None


def _read_json_file(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_json_file(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"{json.dumps(payload, indent=2, sort_keys=True)}\n", encoding="utf-8"
    )


def _with_query(url: str, params: dict[str, Any] | None = None) -> str:
    if not params:
        return url
    encoded = urllib.parse.urlencode(params)
    if not encoded:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{encoded}"


def _normalize_sites(payload: Any) -> list[str]:
    if isinstance(payload, dict):
        for key in ("sites", "data", "items"):
            nested = payload.get(key)
            if isinstance(nested, list):
                payload = nested
                break
    items = payload if isinstance(payload, list) else []
    sites: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        site_id = (
            item.get("site_id")
            or item.get("siteId")
            or item.get("site")
            or item.get("id")
        )
        if site_id:
            sites.append(str(site_id))
    return sites


def _normalize_chargers(payload: Any) -> list[str]:
    if isinstance(payload, dict):
        payload = (
            payload.get("data")
            or payload.get("chargers")
            or payload.get("evChargerData")
            or payload
        )
    items = payload if isinstance(payload, list) else []
    chargers: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        serial = (
            item.get("serial")
            or item.get("serialNumber")
            or item.get("sn")
            or item.get("id")
        )
        if serial:
            chargers.append(str(serial))
    return chargers


def _walk_payload(payload: Any):
    """Yield nested payload members depth-first."""

    if isinstance(payload, dict):
        yield payload
        for value in payload.values():
            yield from _walk_payload(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _walk_payload(item)


def _first_device_uid(payload: Any, *, type_tokens: tuple[str, ...]) -> str | None:
    """Return the first matching HEMS device UID for the requested type tokens."""

    tokens = tuple(token.lower() for token in type_tokens)
    for item in _walk_payload(payload):
        type_candidates = [
            item.get("type"),
            item.get("device_type"),
            item.get("device-type"),
        ]
        matched = False
        for candidate in type_candidates:
            if candidate is None:
                continue
            text = str(candidate).strip().lower()
            if text and any(token in text for token in tokens):
                matched = True
                break
        if not matched:
            continue
        for key in ("device_uid", "device-uid", "uid"):
            value = item.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
    return None


def _extract_summary_hems_flag(payload: Any) -> bool | None:
    """Return the dashboard HEMS capability flag when available."""

    if not isinstance(payload, dict):
        return None
    value = payload.get("is_hems")
    if isinstance(value, bool):
        return value
    return None


def _status_group_rank(group: str | None) -> int:
    """Return a rank for service-status severity groups."""

    return {"other": 0, "degraded": 1, "main": 2}.get(str(group or "other"), 0)


def _merge_status_group(current: str | None, new_value: str | None) -> str:
    """Return the highest-impact status group across grouped endpoints."""

    return (
        current
        if _status_group_rank(current) >= _status_group_rank(new_value)
        else str(new_value or "other")
    )


def _endpoint_result(
    spec: EndpointSpec,
    status: int | None,
    error: str | None,
    *,
    site_id: str | None = None,
    serial: str | None = None,
) -> dict[str, Any]:
    def _safe_url(url: str) -> str:
        parsed = urllib.parse.urlsplit(url)
        path = parsed.path or ""
        query = parsed.query or ""
        if path and (site_id or serial):
            parts = path.split("/")
            for idx, part in enumerate(parts):
                if site_id and part == site_id:
                    parts[idx] = "{site_id}"
                if serial and part == serial:
                    parts[idx] = "{serial}"
            path = "/".join(parts)
        if query:
            if site_id:
                query = query.replace(site_id, "{site_id}")
            if serial:
                query = query.replace(serial, "{serial}")
        if query:
            return f"{path}?{query}"
        return path

    ok_statuses = spec.ok_statuses or (200,)
    ok = status in ok_statuses if status is not None else False
    if ok:
        error = None
    affects_broad_outage = (
        spec.affects_broad_outage
        if spec.affects_broad_outage is not None
        else spec.affects_status or spec.category in BROAD_OUTAGE_ENDPOINT_CATEGORIES
    )

    return {
        "name": spec.name,
        "method": spec.method,
        "url": _safe_url(spec.url),
        "group": spec.group,
        "category": spec.category,
        "status": status,
        "ok": ok,
        "error": error,
        "check_group": spec.check_group or spec.category,
        "check_mode": spec.check_mode,
        "affects_status": spec.affects_status,
        "affects_broad_outage": affects_broad_outage,
    }


def _evaluate_status(results: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for item in results:
        key = item.get("check_group") or item.get("category") or item["name"]
        entry = groups.get(key)
        if not entry:
            entry = {
                "name": key,
                "mode": item.get("check_mode") or "all",
                "group": item.get("group"),
                "category": item.get("category") or key,
                "affects": bool(item.get("affects_status", True)),
                "affects_broad_outage": bool(
                    item.get("affects_broad_outage", item.get("affects_status", True))
                ),
                "endpoints": [],
            }
            groups[key] = entry
        else:
            entry["group"] = _merge_status_group(entry.get("group"), item.get("group"))
            entry["affects"] = bool(entry.get("affects")) or bool(
                item.get("affects_status", True)
            )
            entry["affects_broad_outage"] = bool(
                entry.get("affects_broad_outage")
            ) or bool(
                item.get("affects_broad_outage", item.get("affects_status", True))
            )
        entry["endpoints"].append(item)

    checks: list[dict[str, Any]] = []
    for key, entry in groups.items():
        oks = [bool(ep.get("ok")) for ep in entry["endpoints"]]
        if entry["mode"] == "any":
            ok = any(oks)
        else:
            ok = all(oks) if oks else False
        checks.append(
            {
                "name": key,
                "group": entry["group"],
                "category": entry["category"],
                "ok": ok,
                "affects": entry["affects"],
                "affects_broad_outage": entry["affects_broad_outage"],
                "endpoints": [ep["name"] for ep in entry["endpoints"]],
            }
        )

    failed_checks = [c for c in checks if not c["ok"]]
    affecting = [c for c in checks if c["affects"]]
    failed_affecting = [c for c in affecting if not c["ok"]]
    all_down = bool(affecting) and all(not c["ok"] for c in affecting)
    main_down = any(c["group"] == "main" for c in failed_affecting)
    degraded_down = any(c["group"] in ("degraded", "other") for c in failed_affecting)
    failed_broad_outage_checks = [
        c for c in failed_checks if c.get("affects_broad_outage")
    ]
    broad_outage = (
        len(failed_broad_outage_checks) >= BROAD_OUTAGE_FAILED_CHECK_THRESHOLD
    )

    if main_down or all_down:
        status = "Down"
    elif degraded_down or broad_outage:
        status = "Degraded"
    else:
        status = "Fully Operational"

    summary = {
        "checks_total": len(checks),
        "checks_ok": sum(1 for c in checks if c["ok"]),
        "checks_failed": sum(1 for c in checks if not c["ok"]),
        "endpoints_total": len(results),
        "endpoints_ok": sum(1 for r in results if r["ok"]),
        "endpoints_failed": sum(1 for r in results if not r["ok"]),
        "checks_failed_affecting": len(failed_affecting),
        "checks_failed_non_affecting": len(failed_checks) - len(failed_affecting),
        "checks_failed_broad_outage": len(failed_broad_outage_checks),
    }
    return status, {"checks": checks, "summary": summary}


def _build_synthetic_failure_check(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "group": "other",
        "category": name,
        "ok": False,
        "affects": True,
        "endpoints": [name],
    }


def _summary_from_checks_and_results(
    checks: list[dict[str, Any]], results: list[dict[str, Any]]
) -> dict[str, int]:
    return {
        "checks_total": len(checks),
        "checks_ok": sum(1 for check in checks if check.get("ok")),
        "checks_failed": sum(1 for check in checks if not check.get("ok")),
        "endpoints_total": len(results),
        "endpoints_ok": sum(1 for result in results if result.get("ok")),
        "endpoints_failed": sum(1 for result in results if not result.get("ok")),
    }


def _derive_payload_details(
    *,
    results: list[dict[str, Any]],
    status: str,
    summary: dict[str, Any] | None,
    checks: list[dict[str, Any]] | None,
    reason: str | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    derived_summary = dict(summary or {})
    derived_checks = list(checks or [])

    if not derived_checks and results:
        _derived_status, details = _evaluate_status(results)
        derived_checks = list(details["checks"])
        derived_summary = dict(details["summary"])

    failure_name = _reason_failure_name(reason)
    if status != "Fully Operational" and failure_name:
        has_failed_check = any(check.get("ok") is False for check in derived_checks)
        if not has_failed_check:
            derived_checks.append(_build_synthetic_failure_check(failure_name))
            derived_summary = _summary_from_checks_and_results(derived_checks, results)

    if not derived_summary:
        derived_summary = _summary_from_checks_and_results(derived_checks, results)

    return derived_summary, derived_checks


def _history_sample_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    failed_check_names = sorted(
        str(check["name"])
        for check in payload.get("checks", [])
        if isinstance(check, dict) and check.get("ok") is False and check.get("name")
    )
    checks_failed = payload.get("summary", {}).get("checks_failed")
    if not isinstance(checks_failed, int):
        checks_failed = len(failed_check_names)
    return {
        "checked_at": str(payload.get("checked_at") or _iso_utc_now()),
        "status": str(payload.get("status") or "Down"),
        "checks_failed": checks_failed,
        "failed_check_names": failed_check_names,
    }


def _normalize_history_sample(sample: Any) -> dict[str, Any] | None:
    if not isinstance(sample, dict):
        return None
    checked_at = sample.get("checked_at")
    status = sample.get("status")
    if _parse_iso_utc(checked_at) is None or not isinstance(status, str):
        return None
    failed_names = sample.get("failed_check_names") or []
    if not isinstance(failed_names, list):
        failed_names = []
    normalized_names = sorted(
        {str(name) for name in failed_names if isinstance(name, str) and name.strip()}
    )
    checks_failed = sample.get("checks_failed")
    if not isinstance(checks_failed, int):
        checks_failed = len(normalized_names)
    return {
        "checked_at": checked_at,
        "status": status,
        "checks_failed": checks_failed,
        "failed_check_names": normalized_names,
    }


def _load_history_samples(previous_history_file: str | None) -> list[dict[str, Any]]:
    if not previous_history_file:
        return []
    payload = _read_json_file(Path(previous_history_file))
    if isinstance(payload, dict):
        payload = payload.get("samples", [])
    if not isinstance(payload, list):
        return []
    samples: list[dict[str, Any]] = []
    for item in payload:
        normalized = _normalize_history_sample(item)
        if normalized is not None:
            samples.append(normalized)
    return samples


def _build_history_samples(
    previous_samples: list[dict[str, Any]],
    current_payload: dict[str, Any],
    *,
    retention_days: int,
) -> list[dict[str, Any]]:
    samples_by_timestamp: dict[str, dict[str, Any]] = {}
    for sample in previous_samples:
        normalized = _normalize_history_sample(sample)
        if normalized is not None:
            samples_by_timestamp[normalized["checked_at"]] = normalized

    current_sample = _history_sample_from_payload(current_payload)
    samples_by_timestamp[current_sample["checked_at"]] = current_sample

    current_dt = _parse_iso_utc(current_sample["checked_at"]) or datetime.now(
        timezone.utc
    )
    cutoff = current_dt - timedelta(days=retention_days)

    kept = [
        sample
        for sample in samples_by_timestamp.values()
        if (_parse_iso_utc(sample["checked_at"]) or current_dt) >= cutoff
    ]
    kept.sort(key=lambda item: item["checked_at"])
    return kept


def _close_incident(
    current: dict[str, Any],
    ended_at: datetime | None,
    *,
    active: bool,
) -> dict[str, Any]:
    started_at = current["started_at"]
    final_time = ended_at or current["last_seen_at"]
    duration = int((final_time - started_at).total_seconds() // 60)
    return {
        "status": current["status"],
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "ended_at": (
            ended_at.isoformat().replace("+00:00", "Z")
            if ended_at is not None
            else None
        ),
        "last_seen_at": current["last_seen_at"].isoformat().replace("+00:00", "Z"),
        "duration_minutes": max(0, duration),
        "active": active,
        "failed_checks": sorted(current["failed_checks"]),
    }


def _build_incidents(
    samples: list[dict[str, Any]],
    *,
    max_gap_minutes: int = MAX_INCIDENT_SAMPLE_GAP_MINUTES,
) -> list[dict[str, Any]]:
    incidents: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for sample in samples:
        status = sample["status"]
        checked_at = _parse_iso_utc(sample["checked_at"])
        if checked_at is None:
            continue
        failed_checks = {
            str(name)
            for name in sample.get("failed_check_names", [])
            if isinstance(name, str) and name.strip()
        }

        if status == "Fully Operational":
            if current is not None:
                gap_minutes = int(
                    (checked_at - current["last_seen_at"]).total_seconds() // 60
                )
                incidents.append(
                    _close_incident(
                        current,
                        checked_at if gap_minutes <= max_gap_minutes else None,
                        active=False,
                    )
                )
                current = None
            continue

        if current is None:
            current = {
                "status": status,
                "started_at": checked_at,
                "last_seen_at": checked_at,
                "failed_checks": set(failed_checks),
            }
            continue

        gap_minutes = int((checked_at - current["last_seen_at"]).total_seconds() // 60)
        if gap_minutes > max_gap_minutes:
            incidents.append(_close_incident(current, None, active=False))
            current = {
                "status": status,
                "started_at": checked_at,
                "last_seen_at": checked_at,
                "failed_checks": set(failed_checks),
            }
            continue

        if current["status"] != status:
            incidents.append(_close_incident(current, checked_at, active=False))
            current = {
                "status": status,
                "started_at": checked_at,
                "last_seen_at": checked_at,
                "failed_checks": set(failed_checks),
            }
            continue

        current["last_seen_at"] = checked_at
        current["failed_checks"].update(failed_checks)

    if current is not None:
        incidents.append(_close_incident(current, None, active=True))

    return incidents


def _incident_mermaid_duration(incident: dict[str, Any]) -> int:
    duration = incident.get("duration_minutes")
    if not isinstance(duration, int):
        duration = 0
    return max(duration, MIN_VISIBLE_INCIDENT_MINUTES)


def _timeline_window(
    *,
    checked_at: str | None,
) -> tuple[str, str]:
    end_dt = _parse_iso_utc(checked_at)
    if end_dt is None:
        end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=DEFAULT_HISTORY_DAYS)
    if start_dt >= end_dt:
        start_dt = end_dt - timedelta(hours=1)

    return (
        start_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        end_dt.strftime("%Y-%m-%dT%H:%M:%S"),
    )


def _render_mermaid_timeline(
    incidents: list[dict[str, Any]],
    *,
    checked_at: str | None,
) -> str:
    timeline_start, timeline_end = _timeline_window(checked_at=checked_at)
    lines = [
        "```mermaid",
        "gantt",
        "    title Enphase Service Status Incident Timeline (Last 30 Days)",
        "    dateFormat  YYYY-MM-DDTHH:mm:ss",
        "    axisFormat  %b %d",
        f"    Window start :vert, window-start, {timeline_start}, 0ms",
        f"    Window end :vert, window-end, {timeline_end}, 0ms",
    ]

    grouped = {
        "Down": [incident for incident in incidents if incident["status"] == "Down"],
        "Degraded": [
            incident for incident in incidents if incident["status"] == "Degraded"
        ],
    }

    if not incidents:
        lines.extend(
            [
                "    section Summary",
                (
                    "    No incidents observed :done, "
                    f"{_mermaid_datetime(checked_at)}, 1m"
                ),
            ]
        )
    else:
        for section_name in ("Down", "Degraded"):
            section_incidents = grouped[section_name]
            if not section_incidents:
                continue
            lines.append(f"    section {section_name}")
            for idx, incident in enumerate(section_incidents, start=1):
                task_label = (
                    f"{section_name} {idx} "
                    f"({_format_mermaid_label_utc(incident['started_at'])})"
                )
                task_id = f"{_safe_slug(section_name)}-{idx}"
                style = "crit" if section_name == "Down" else "active"
                lines.append(
                    "    "
                    f"{task_label} :{style}, {task_id}, "
                    f"{_mermaid_datetime(incident['started_at'])}, "
                    f"{_incident_mermaid_duration(incident)}m"
                )

    lines.append("```")
    return "\n".join(lines)


def _render_incident_table(incidents: list[dict[str, Any]]) -> str:
    if not incidents:
        return "No degraded or down incidents observed in the last 30 days."

    lines = [
        "| Status | Started (UTC) | Ended (UTC) | Duration | Failed checks |",
        "| --- | --- | --- | --- | --- |",
    ]
    for incident in incidents:
        failed_checks = ", ".join(incident["failed_checks"]) or "-"
        ended_display = _format_utc(incident["ended_at"])
        duration_display = _duration_label(int(incident["duration_minutes"]))
        if incident.get("active"):
            ended_display = (
                f"Ongoing (last seen {_format_utc(incident['last_seen_at'])})"
            )
            if int(incident["duration_minutes"]) == 0:
                duration_display = "Observed at latest check"
            else:
                duration_display = f"Observed {duration_display}"
        elif incident.get("ended_at") is None:
            ended_display = (
                f"Unknown after last seen {_format_utc(incident['last_seen_at'])}"
            )
            duration_display = f"Observed {duration_display}"
        lines.append(
            "| "
            f"{incident['status']} | "
            f"{_format_utc(incident['started_at'])} | "
            f"{ended_display} | "
            f"{duration_display} | "
            f"{failed_checks} |"
        )
    return "\n".join(lines)


def _render_wiki_page(
    *,
    payload: dict[str, Any],
    history_samples: list[dict[str, Any]],
    incidents: list[dict[str, Any]],
    raw_base_url: str,
) -> str:
    checked_at = str(payload.get("checked_at") or _iso_utc_now())
    current_status = str(payload.get("status") or "Down")
    checks_failed = payload.get("summary", {}).get("checks_failed", 0)
    failed_names = ", ".join(
        str(check["name"])
        for check in payload.get("checks", [])
        if isinstance(check, dict) and check.get("ok") is False and check.get("name")
    )
    if not failed_names:
        failed_names = "None"

    raw_status_url = f"{raw_base_url}/status.json"
    raw_history_url = f"{raw_base_url}/history.json"
    raw_incidents_url = f"{raw_base_url}/incidents.json"

    sections = [
        "# Service Status History",
        "",
        f"- Current status: **{current_status}**",
        f"- Last updated: `{_format_utc(checked_at)}`",
        f"- Failed checks in latest run: `{checks_failed}`",
        f"- Latest failed checks: {failed_names}",
        f"- Retained hourly samples: `{len(history_samples)}`",
        f"- Incident windows in last 30 days: `{len(incidents)}`",
        "",
        "This page is generated from hourly synthetic checks against Enphase cloud"
        " endpoints. It may miss incidents that begin and recover between checks.",
        "",
        "## Incident Timeline",
        "",
        _render_mermaid_timeline(incidents, checked_at=checked_at),
        "",
        "## Incident Summary",
        "",
        _render_incident_table(incidents),
        "",
        "## Raw Artifacts",
        "",
        f"- [Current status.json]({raw_status_url})",
        f"- [30-day history.json]({raw_history_url})",
        f"- [30-day incidents.json]({raw_incidents_url})",
        "",
    ]
    return "\n".join(sections)


def _write_outputs(
    output_dir: str,
    *,
    status: str,
    payload: dict[str, Any],
    previous_history_file: str | None = None,
    retention_days: int = DEFAULT_HISTORY_DAYS,
    raw_base_url: str | None = None,
    wiki_page_name: str = DEFAULT_WIKI_PAGE,
) -> None:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    _write_json_file(output_path / "status.json", payload)

    color_map = {
        "Fully Operational": "#4c1",
        "Degraded": "#dfb317",
        "Down": "#e05d44",
    }
    svg = _badge_svg("Enphase Service Status", status, color_map.get(status, "#9f9f9f"))
    (output_path / "status.svg").write_text(f"{svg}\n", encoding="utf-8")
    _write_json_file(
        output_path / "status_badge.json",
        _shields_badge_payload(
            "Enphase Service Status",
            status,
            color_map.get(status, "#9f9f9f"),
        ),
    )

    history_samples = _build_history_samples(
        _load_history_samples(previous_history_file),
        payload,
        retention_days=retention_days,
    )
    incidents = _build_incidents(history_samples)

    history_payload = {
        "current_status": status,
        "generated_at": str(payload.get("checked_at") or _iso_utc_now()),
        "retention_days": retention_days,
        "samples": history_samples,
    }
    incidents_payload = {
        "current_status": status,
        "generated_at": str(payload.get("checked_at") or _iso_utc_now()),
        "retention_days": retention_days,
        "incidents": incidents,
    }
    _write_json_file(output_path / "history.json", history_payload)
    _write_json_file(output_path / "incidents.json", incidents_payload)

    page_name = (
        wiki_page_name if wiki_page_name.endswith(".md") else f"{wiki_page_name}.md"
    )
    wiki_path = output_path / "wiki" / page_name
    wiki_path.parent.mkdir(parents=True, exist_ok=True)
    wiki_path.write_text(
        _render_wiki_page(
            payload=payload,
            history_samples=history_samples,
            incidents=incidents,
            raw_base_url=(raw_base_url or _default_raw_base_url()).rstrip("/"),
        )
        + "\n",
        encoding="utf-8",
    )


def _finalize_outputs(
    output_dir: str,
    *,
    status: str,
    payload: dict[str, Any],
    previous_history_file: str | None,
    retention_days: int,
    raw_base_url: str | None,
    wiki_page_name: str,
) -> int:
    _write_outputs(
        output_dir,
        status=status,
        payload=payload,
        previous_history_file=previous_history_file,
        retention_days=retention_days,
        raw_base_url=raw_base_url,
        wiki_page_name=wiki_page_name,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check Enphase Energy service endpoints."
    )
    parser.add_argument("--output-dir", default="status-out")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--site-id")
    parser.add_argument("--serial")
    parser.add_argument("--locale")
    parser.add_argument("--previous-history-file")
    parser.add_argument("--history-days", type=int, default=DEFAULT_HISTORY_DAYS)
    parser.add_argument("--raw-base-url")
    parser.add_argument("--wiki-page-name", default=DEFAULT_WIKI_PAGE)
    args = parser.parse_args()

    email = (os.environ.get("ENPHASE_EMAIL") or "").strip()
    password = (os.environ.get("ENPHASE_PASSWORD") or "").strip()
    site_id = (args.site_id or os.environ.get("ENPHASE_SITE_ID") or "").strip()
    serial = (args.serial or os.environ.get("ENPHASE_SERIAL") or "").strip()
    locale = (args.locale or os.environ.get("ENPHASE_LOCALE") or "en-US").strip()

    results: list[dict[str, Any]] = []
    started_at = _iso_utc_now()

    def _return_payload(status: str, **extra: Any) -> int:
        reason = extra.get("reason")
        summary, checks = _derive_payload_details(
            results=results,
            status=status,
            summary=extra.pop("summary", None),
            checks=extra.pop("checks", None),
            reason=reason if isinstance(reason, str) else None,
        )
        payload = {
            "status": status,
            "checked_at": started_at,
            "summary": summary,
            "checks": checks,
            "endpoints": extra.pop("endpoints", results),
        }
        payload.update(extra)
        return _finalize_outputs(
            args.output_dir,
            status=status,
            payload=payload,
            previous_history_file=args.previous_history_file,
            retention_days=args.history_days,
            raw_base_url=args.raw_base_url,
            wiki_page_name=args.wiki_page_name,
        )

    if not email or not password:
        return _return_payload(
            "Down",
            reason="Missing ENPHASE_EMAIL or ENPHASE_PASSWORD",
            summary={},
            checks=[],
            endpoints=[],
        )

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    login_spec = EndpointSpec(
        name="auth_login",
        method="POST",
        url=LOGIN_URL,
        group="other",
        category="auth",
        form={"user[email]": email, "user[password]": password},
    )
    status_code, _headers, body, error = _request(
        opener,
        login_spec.method,
        login_spec.url,
        headers={"Accept": "application/json, text/plain, */*"},
        form=login_spec.form,
        timeout=args.timeout,
    )
    results.append(
        _endpoint_result(login_spec, status_code, error, site_id=site_id, serial=serial)
    )
    payload_json = _parse_json(body) or {}

    if status_code != 200:
        return _return_payload("Down", reason="Login failed")

    if isinstance(payload_json, dict) and payload_json.get("requires_mfa"):
        return _return_payload(
            "Down", reason="Account requires MFA; workflow cannot continue"
        )

    if isinstance(payload_json, dict) and payload_json.get("isBlocked") is True:
        return _return_payload("Down", reason="Account is blocked")

    if isinstance(payload_json, dict) and payload_json.get("success") is False:
        return _return_payload("Down", reason="Login rejected")

    session_id = None
    if isinstance(payload_json, dict):
        session_id = (
            payload_json.get("session_id")
            or payload_json.get("sessionId")
            or payload_json.get("session")
        )

    cookie_header = "; ".join(
        header
        for header in (
            _cookie_header_for(BASE_URL, jar),
            _cookie_header_for(ENTREZ_URL, jar),
        )
        if header
    )
    cookie_map = _cookie_map(cookie_header)
    xsrf = _extract_xsrf_token(cookie_map)

    eauth = None
    if session_id:
        token_spec = EndpointSpec(
            name="auth_token",
            method="POST",
            url=f"{ENTREZ_URL}/tokens",
            group="other",
            category="auth",
            json_body={"session_id": session_id, "email": email},
            ok_statuses=(200, 400, 404, 422, 429),
        )
        status_code, _headers, body, error = _request(
            opener,
            token_spec.method,
            token_spec.url,
            headers={"Accept": "application/json"},
            json_body=token_spec.json_body,
            timeout=args.timeout,
        )
        results.append(
            _endpoint_result(
                token_spec, status_code, error, site_id=site_id, serial=serial
            )
        )
        token_payload = _parse_json(body)
        if isinstance(token_payload, dict):
            token = (
                token_payload.get("token")
                or token_payload.get("auth_token")
                or token_payload.get("access_token")
            )
            if token:
                eauth = str(token)
    else:
        token_spec = EndpointSpec(
            name="auth_token",
            method="POST",
            url=f"{ENTREZ_URL}/tokens",
            group="other",
            category="auth",
            affects_status=False,
        )
        results.append(
            _endpoint_result(
                token_spec, None, "missing session_id", site_id=site_id, serial=serial
            )
        )

    base_headers = {
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
    }
    if site_id:
        base_headers["Referer"] = f"{BASE_URL}/pv/systems/{site_id}/summary"
    else:
        base_headers["Referer"] = f"{BASE_URL}/"
    if cookie_header:
        base_headers["Cookie"] = cookie_header
    if xsrf:
        base_headers["X-CSRF-Token"] = xsrf
    if eauth:
        base_headers["e-auth-token"] = eauth

    bearer = _extract_bearer(cookie_map) or eauth
    if bearer:
        base_headers["Authorization"] = f"Bearer {bearer}"
    history_bearer = eauth or _extract_bearer(cookie_map)
    user_id = _jwt_user_id(history_bearer)
    request_id = str(uuid.uuid4())

    discovery_sites: list[str] = []
    for idx, url in enumerate((SITE_SEARCH_URL,), start=1):
        spec = EndpointSpec(
            name=f"site_discovery_{idx}",
            method="GET",
            url=url,
            group="other",
            category="discovery",
            headers=dict(base_headers),
        )
        status_code, _headers, body, error = _request(
            opener, spec.method, spec.url, headers=spec.headers, timeout=args.timeout
        )
        results.append(
            _endpoint_result(spec, status_code, error, site_id=site_id, serial=serial)
        )
        if status_code == 200 and not discovery_sites:
            discovery_sites = _normalize_sites(_parse_json(body))

    if not site_id and discovery_sites:
        site_id = discovery_sites[0]

    if not site_id:
        status, details = _evaluate_status(results)
        return _return_payload(
            "Down",
            reason="Site discovery failed",
            summary=details["summary"],
            checks=details["checks"],
        )

    base_headers["Referer"] = f"{BASE_URL}/pv/systems/{site_id}/summary"
    control_headers = dict(base_headers)

    history_headers = dict(control_headers)
    if history_bearer:
        history_headers["Authorization"] = f"Bearer {history_bearer}"
    history_session_id = _jwt_session_id(history_bearer)
    if history_session_id:
        history_headers["e-auth-token"] = history_session_id
    else:
        history_headers.pop("e-auth-token", None)
    if request_id:
        history_headers["requestid"] = request_id
    if user_id:
        history_headers["username"] = user_id

    summary_spec = EndpointSpec(
        name="charger_summary_v2",
        method="GET",
        url=(
            f"{BASE_URL}/service/evse_controller/api/v2/{site_id}/ev_chargers/summary"
            "?filter_retired=true"
        ),
        group="other",
        category="discovery",
        headers=dict(base_headers),
    )
    status_code, _headers, body, error = _request(
        opener,
        summary_spec.method,
        summary_spec.url,
        headers=summary_spec.headers,
        timeout=args.timeout,
    )
    results.append(
        _endpoint_result(
            summary_spec, status_code, error, site_id=site_id, serial=serial
        )
    )
    if not serial and status_code == 200:
        chargers = _normalize_chargers(_parse_json(body))
        if chargers:
            serial = chargers[0]

    status_spec = EndpointSpec(
        name="charger_status",
        method="GET",
        url=f"{BASE_URL}/service/evse_controller/{site_id}/ev_chargers/status",
        group="main",
        category="evse_runtime",
        headers=dict(control_headers),
    )
    status_code, _headers, body, error = _request(
        opener,
        status_spec.method,
        status_spec.url,
        headers=status_spec.headers,
        timeout=args.timeout,
    )
    results.append(
        _endpoint_result(
            status_spec, status_code, error, site_id=site_id, serial=serial
        )
    )
    if not serial and status_code == 200:
        payload = _parse_json(body)
        chargers = _normalize_chargers(
            payload.get("evChargerData") if isinstance(payload, dict) else payload
        )
        if chargers:
            serial = chargers[0]

    if not serial:
        status, details = _evaluate_status(results)
        return _return_payload(
            "Down",
            reason="Serial discovery failed",
            summary=details["summary"],
            checks=details["checks"],
        )

    today = datetime.now().strftime("%d-%m-%Y")
    now_utc = datetime.now(timezone.utc)
    yesterday_utc = now_utc - timedelta(days=1)
    day_start_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    inverter_date_query = {
        "start_date": yesterday_utc.strftime("%Y-%m-%d"),
        "end_date": now_utc.strftime("%Y-%m-%d"),
    }
    timeseries_daily_query = {
        "site_id": site_id,
        "source": "evse",
        "requestId": request_id,
        "start_date": now_utc.strftime("%Y-%m-%d"),
    }
    if user_id:
        timeseries_daily_query["username"] = user_id
    timeseries_lifetime_query = {
        "site_id": site_id,
        "source": "evse",
        "requestId": request_id,
    }
    if user_id:
        timeseries_lifetime_query["username"] = user_id
    criteria_query = {"source": "evse", "requestId": request_id}
    if user_id:
        criteria_query["username"] = user_id
    criteria_url = (
        f"{BASE_URL}/service/enho_historical_events_ms/{site_id}/filter_criteria"
        f"?{urllib.parse.urlencode(criteria_query)}"
    )
    battery_common_params: dict[str, str] = {}
    if user_id:
        battery_common_params["userId"] = user_id
    battery_profile_params = dict(battery_common_params)
    battery_profile_params["source"] = "enho"
    if locale:
        battery_profile_params["locale"] = locale
    battery_settings_params = dict(battery_common_params)
    battery_settings_params["source"] = "enho"
    hems_headers = dict(base_headers)
    if bearer:
        hems_headers["Authorization"] = f"Bearer {bearer}"
    if user_id:
        hems_headers["username"] = user_id
    hems_headers["requestId"] = request_id
    dashboard_headers = dict(control_headers)

    battery_headers = dict(control_headers)
    if user_id:
        battery_headers["Username"] = user_id
    battery_headers["Origin"] = "https://battery-profile-ui.enphaseenergy.com"
    battery_headers["Referer"] = "https://battery-profile-ui.enphaseenergy.com/"

    safe_specs = [
        EndpointSpec(
            name="scheduler_charge_mode",
            method="GET",
            url=(
                f"{BASE_URL}/service/evse_scheduler/api/v1/iqevc/charging-mode/"
                f"{site_id}/{serial}/preference"
            ),
            group="degraded",
            category="evse_scheduler",
            headers=dict(control_headers),
            ok_statuses=(200, 400, 401, 403, 404, 422),
        ),
        EndpointSpec(
            name="scheduler_green_settings",
            method="GET",
            url=(
                f"{BASE_URL}/service/evse_scheduler/api/v1/iqevc/charging-mode/"
                f"GREEN_CHARGING/{site_id}/{serial}/settings"
            ),
            group="degraded",
            category="evse_scheduler",
            headers=dict(control_headers),
            ok_statuses=(200, 400, 401, 403, 404, 422),
        ),
        EndpointSpec(
            name="scheduler_schedules",
            method="GET",
            url=(
                f"{BASE_URL}/service/evse_scheduler/api/v1/iqevc/charging-mode/"
                f"SCHEDULED_CHARGING/{site_id}/{serial}/schedules"
            ),
            group="degraded",
            category="evse_scheduler",
            headers=dict(control_headers),
            ok_statuses=(200, 400, 401, 403, 404, 422),
        ),
        EndpointSpec(
            name="session_history_criteria",
            method="GET",
            url=criteria_url,
            group="degraded",
            category="session_history",
            headers=dict(history_headers),
            ok_statuses=(200, 400, 401, 403, 404, 422),
        ),
        EndpointSpec(
            name="session_history",
            method="POST",
            url=f"{BASE_URL}/service/enho_historical_events_ms/{site_id}/sessions/{serial}/history",
            group="degraded",
            category="session_history",
            headers=dict(history_headers),
            json_body={
                "source": "evse",
                "params": {
                    "offset": 0,
                    "limit": 1,
                    "startDate": today,
                    "endDate": today,
                    "timezone": "UTC",
                },
            },
            ok_statuses=(200, 400, 401, 403, 404, 422),
        ),
        EndpointSpec(
            name="site_energy",
            method="GET",
            url=f"{BASE_URL}/pv/systems/{site_id}/lifetime_energy",
            group="degraded",
            category="site_energy",
            headers=dict(base_headers),
        ),
        EndpointSpec(
            name="site_latest_power",
            method="GET",
            url=f"{BASE_URL}/app-api/{site_id}/get_latest_power",
            group="degraded",
            category="site_live",
            headers=dict(base_headers),
            affects_status=False,
        ),
        EndpointSpec(
            name="site_show_livestream",
            method="GET",
            url=f"{BASE_URL}/app-api/{site_id}/show_livestream",
            group="degraded",
            category="site_live",
            headers=dict(base_headers),
            affects_status=False,
            ok_statuses=(200, 401, 403, 404),
        ),
        EndpointSpec(
            name="evse_timeseries_daily_energy",
            method="GET",
            url=_with_query(
                f"{BASE_URL}/service/timeseries/evse/timeseries/daily_energy",
                timeseries_daily_query,
            ),
            group="degraded",
            category="evse_timeseries",
            headers=dict(history_headers),
            affects_status=False,
        ),
        EndpointSpec(
            name="evse_timeseries_lifetime_energy",
            method="GET",
            url=_with_query(
                f"{BASE_URL}/service/timeseries/evse/timeseries/lifetime_energy",
                timeseries_lifetime_query,
            ),
            group="degraded",
            category="evse_timeseries",
            headers=dict(history_headers),
            affects_status=False,
        ),
        EndpointSpec(
            name="evse_fw_details",
            method="GET",
            url=f"{BASE_URL}/service/evse_management/fwDetails/{site_id}",
            group="degraded",
            category="evse_management",
            headers=dict(base_headers),
            affects_status=False,
            ok_statuses=(200, 403, 404),
        ),
        EndpointSpec(
            name="evse_feature_flags",
            method="GET",
            url=_with_query(
                f"{BASE_URL}/service/evse_management/api/v1/config/feature-flags",
                {"site_id": site_id},
            ),
            group="degraded",
            category="evse_management",
            headers=dict(base_headers),
            affects_status=False,
            ok_statuses=(200, 401, 403, 404),
        ),
        EndpointSpec(
            name="battery_site_settings",
            method="GET",
            url=_with_query(
                f"{BASE_URL}/service/batteryConfig/api/v1/siteSettings/{site_id}",
                battery_common_params,
            ),
            group="degraded",
            category="battery_config",
            headers=dict(battery_headers),
            affects_status=False,
            ok_statuses=(200, 400, 404, 422),
        ),
        EndpointSpec(
            name="battery_profile",
            method="GET",
            url=_with_query(
                f"{BASE_URL}/service/batteryConfig/api/v1/profile/{site_id}",
                battery_profile_params,
            ),
            group="degraded",
            category="battery_config",
            headers=dict(battery_headers),
            affects_status=False,
            ok_statuses=(200, 400, 404, 422),
        ),
        EndpointSpec(
            name="battery_settings",
            method="GET",
            url=_with_query(
                f"{BASE_URL}/service/batteryConfig/api/v1/batterySettings/{site_id}",
                battery_settings_params,
            ),
            group="degraded",
            category="battery_config",
            headers=dict(battery_headers),
            affects_status=False,
            ok_statuses=(200, 400, 404, 422),
        ),
        EndpointSpec(
            name="battery_storm_guard_alert",
            method="GET",
            url=f"{BASE_URL}/service/batteryConfig/api/v1/stormGuard/{site_id}/stormAlert",
            group="degraded",
            category="battery_config",
            headers=dict(battery_headers),
            affects_status=False,
            ok_statuses=(200, 404),
        ),
        EndpointSpec(
            name="battery_schedules",
            method="GET",
            url=(
                f"{BASE_URL}/service/batteryConfig/api/v1/battery/sites/"
                f"{site_id}/schedules"
            ),
            group="degraded",
            category="battery_config",
            headers=dict(battery_headers),
            affects_status=False,
            ok_statuses=(200, 404),
        ),
        EndpointSpec(
            name="battery_schedule_validation",
            method="POST",
            url=(
                f"{BASE_URL}/service/batteryConfig/api/v1/battery/sites/"
                f"{site_id}/schedules/isValid"
            ),
            group="degraded",
            category="battery_config",
            headers=dict(battery_headers),
            json_body={"scheduleType": "cfg", "forceScheduleOpted": True},
            affects_status=False,
            ok_statuses=(200, 400, 401, 403, 404, 422),
        ),
        EndpointSpec(
            name="devices_inventory",
            method="GET",
            url=f"{BASE_URL}/app-api/{site_id}/devices.json",
            group="degraded",
            category="inventory",
            headers=dict(base_headers),
            affects_status=False,
            ok_statuses=(200, 404),
        ),
        EndpointSpec(
            name="system_dashboard_summary",
            method="GET",
            url=(
                f"{BASE_URL}/service/system_dashboard/api_internal/cs/sites/"
                f"{site_id}/summary"
            ),
            group="degraded",
            category="system_dashboard",
            headers=dict(dashboard_headers),
            affects_status=False,
            ok_statuses=(200, 401, 403, 404),
        ),
        EndpointSpec(
            name="devices_tree_modern",
            method="GET",
            url=(
                f"{BASE_URL}/service/system_dashboard/api_internal/dashboard/sites/"
                f"{site_id}/devices-tree"
            ),
            group="degraded",
            category="system_dashboard",
            headers=dict(dashboard_headers),
            affects_status=False,
            check_group="system_dashboard_tree",
            check_mode="any",
            ok_statuses=(200, 401, 403, 404),
        ),
        EndpointSpec(
            name="devices_tree_legacy",
            method="GET",
            url=f"{BASE_URL}/pv/systems/{site_id}/system_dashboard/devices-tree",
            group="degraded",
            category="system_dashboard",
            headers=dict(dashboard_headers),
            affects_status=False,
            check_group="system_dashboard_tree",
            check_mode="any",
            ok_statuses=(200, 401, 403, 404),
        ),
        EndpointSpec(
            name="devices_details_modern",
            method="GET",
            url=_with_query(
                f"{BASE_URL}/service/system_dashboard/api_internal/dashboard/sites/{site_id}/devices_details",
                {"type": "inverters"},
            ),
            group="degraded",
            category="system_dashboard",
            headers=dict(dashboard_headers),
            affects_status=False,
            check_group="system_dashboard_details",
            check_mode="any",
            ok_statuses=(200, 401, 403, 404),
        ),
        EndpointSpec(
            name="devices_details_legacy",
            method="GET",
            url=_with_query(
                f"{BASE_URL}/pv/systems/{site_id}/system_dashboard/devices_details",
                {"type": "inverters"},
            ),
            group="degraded",
            category="system_dashboard",
            headers=dict(dashboard_headers),
            affects_status=False,
            check_group="system_dashboard_details",
            check_mode="any",
            ok_statuses=(200, 401, 403, 404),
        ),
        EndpointSpec(
            name="grid_control_check",
            method="GET",
            url=f"{BASE_URL}/app-api/{site_id}/grid_control_check.json",
            group="degraded",
            category="battery_runtime",
            headers=dict(base_headers),
            affects_status=False,
            ok_statuses=(200, 404),
        ),
        EndpointSpec(
            name="battery_backup_history",
            method="GET",
            url=f"{BASE_URL}/app-api/{site_id}/battery_backup_history.json",
            group="degraded",
            category="battery_runtime",
            headers=dict(base_headers),
            affects_status=False,
            ok_statuses=(200, 404),
        ),
        EndpointSpec(
            name="battery_status",
            method="GET",
            url=f"{BASE_URL}/pv/settings/{site_id}/battery_status.json",
            group="degraded",
            category="battery_runtime",
            headers=dict(base_headers),
            affects_status=False,
            ok_statuses=(200, 404),
        ),
        EndpointSpec(
            name="dry_contacts",
            method="GET",
            url=f"{BASE_URL}/pv/settings/{site_id}/dry_contacts",
            group="degraded",
            category="battery_runtime",
            headers=dict(base_headers),
            affects_status=False,
            ok_statuses=(200, 404),
        ),
        EndpointSpec(
            name="inverters_inventory",
            method="GET",
            url=_with_query(
                f"{BASE_URL}/app-api/{site_id}/inverters.json",
                {"limit": 1000, "offset": 0, "search": ""},
            ),
            group="degraded",
            category="microinverters",
            headers=dict(base_headers),
            affects_status=False,
            ok_statuses=(200, 404),
        ),
        EndpointSpec(
            name="inverter_status",
            method="GET",
            url=f"{BASE_URL}/systems/{site_id}/inverter_status_x.json",
            group="degraded",
            category="microinverters",
            headers=dict(base_headers),
            affects_status=False,
            ok_statuses=(200, 404),
        ),
        EndpointSpec(
            name="inverter_production",
            method="GET",
            url=_with_query(
                f"{BASE_URL}/systems/{site_id}/inverter_data_x/energy.json",
                inverter_date_query,
            ),
            group="degraded",
            category="microinverters",
            headers=dict(base_headers),
            affects_status=False,
            ok_statuses=(200, 404),
        ),
        EndpointSpec(
            name="auth_settings",
            method="POST",
            url=(
                f"{BASE_URL}/service/evse_controller/api/v1/{site_id}/"
                f"ev_chargers/{serial}/ev_charger_config"
            ),
            group="degraded",
            category="evse_control",
            headers=dict(control_headers),
            json_body=[
                {"key": "rfidSessionAuthentication"},
                {"key": "sessionAuthentication"},
            ],
            ok_statuses=(200, 400, 401, 403, 404, 422),
        ),
    ]

    hems_supported: bool | None = None
    hems_devices_body: bytes | None = None

    for spec in safe_specs:
        status_code, _headers, body, error = _request(
            opener,
            spec.method,
            spec.url,
            headers=spec.headers,
            json_body=spec.json_body,
            timeout=args.timeout,
        )
        results.append(
            _endpoint_result(spec, status_code, error, site_id=site_id, serial=serial)
        )
        if spec.name == "system_dashboard_summary" and status_code in spec.ok_statuses:
            hems_supported = _extract_summary_hems_flag(_parse_json(body))
        elif spec.name == "devices_inventory" and hems_supported is None:
            inventory_payload = _parse_json(body)
            inventory_text = (
                json.dumps(inventory_payload).lower() if inventory_payload else ""
            )
            if any(
                token in inventory_text
                for token in (
                    "hems",
                    "heat_pump",
                    "heatpump",
                    "iq_er",
                    "iq energy router",
                )
            ):
                hems_supported = True
    if hems_supported:
        hems_devices_spec = EndpointSpec(
            name="hems_devices",
            method="GET",
            url=_with_query(
                f"https://hems-integration.enphaseenergy.com/api/v1/hems/{site_id}/hems-devices",
                {"refreshData": "false"},
            ),
            group="degraded",
            category="hems",
            headers=dict(hems_headers),
            affects_status=False,
            ok_statuses=(200, 401, 403, 404),
        )
        status_code, _headers, hems_devices_body, error = _request(
            opener,
            hems_devices_spec.method,
            hems_devices_spec.url,
            headers=hems_devices_spec.headers,
            timeout=args.timeout,
        )
        results.append(
            _endpoint_result(
                hems_devices_spec,
                status_code,
                error,
                site_id=site_id,
                serial=serial,
            )
        )

        hems_specs: list[EndpointSpec] = [
            EndpointSpec(
                name="hems_consumption_lifetime",
                method="GET",
                url=f"{BASE_URL}/systems/{site_id}/hems_consumption_lifetime",
                group="degraded",
                category="hems",
                headers=dict(hems_headers),
                affects_status=False,
                ok_statuses=(200, 401, 403, 404),
            ),
            EndpointSpec(
                name="hems_energy_consumption",
                method="GET",
                url=_with_query(
                    f"https://hems-integration.enphaseenergy.com/api/v1/hems/{site_id}/energy-consumption",
                    {
                        "from": day_start_utc.isoformat().replace("+00:00", "Z"),
                        "to": now_utc.isoformat().replace("+00:00", "Z"),
                        "timezone": "UTC",
                        "step": "P1D",
                    },
                ),
                group="degraded",
                category="hems",
                headers=dict(hems_headers),
                affects_status=False,
                ok_statuses=(200, 401, 403, 404),
            ),
        ]

        hems_devices_payload = _parse_json(hems_devices_body)
        heat_pump_uid = _first_device_uid(
            hems_devices_payload,
            type_tokens=("heat_pump", "heatpump", "heat-pump"),
        )
        iq_er_uid = _first_device_uid(
            hems_devices_payload,
            type_tokens=("iq_er", "iq energy router", "iqenergyrouter"),
        )
        if heat_pump_uid:
            hems_specs.extend(
                [
                    EndpointSpec(
                        name="hems_heatpump_state",
                        method="GET",
                        url=_with_query(
                            f"https://hems-integration.enphaseenergy.com/api/v1/hems/{site_id}/heatpump/{heat_pump_uid}/state",
                            {"timezone": "UTC"},
                        ),
                        group="degraded",
                        category="hems",
                        headers=dict(hems_headers),
                        affects_status=False,
                        ok_statuses=(200, 401, 403, 404),
                    ),
                    EndpointSpec(
                        name="hems_power_timeseries",
                        method="GET",
                        url=_with_query(
                            f"{BASE_URL}/systems/{site_id}/hems_power_timeseries",
                            {
                                "device-uid": heat_pump_uid,
                                "date": now_utc.strftime("%Y-%m-%d"),
                            },
                        ),
                        group="degraded",
                        category="hems",
                        headers=dict(hems_headers),
                        affects_status=False,
                        ok_statuses=(200, 401, 403, 404, 422),
                    ),
                    EndpointSpec(
                        name="heat_pump_events",
                        method="GET",
                        url=f"{BASE_URL}/systems/{site_id}/heat_pump/{heat_pump_uid}/events.json",
                        group="degraded",
                        category="hems",
                        headers=dict(hems_headers),
                        affects_status=False,
                        ok_statuses=(200, 401, 403, 404),
                    ),
                ]
            )
        if iq_er_uid:
            hems_specs.append(
                EndpointSpec(
                    name="iq_er_events",
                    method="GET",
                    url=f"{BASE_URL}/systems/{site_id}/iq_er/{iq_er_uid}/events.json",
                    group="degraded",
                    category="hems",
                    headers=dict(hems_headers),
                    affects_status=False,
                    ok_statuses=(200, 401, 403, 404),
                )
            )

        for spec in hems_specs:
            status_code, _headers, _body, error = _request(
                opener,
                spec.method,
                spec.url,
                headers=spec.headers,
                timeout=args.timeout,
            )
            results.append(
                _endpoint_result(
                    spec, status_code, error, site_id=site_id, serial=serial
                )
            )

    status, details = _evaluate_status(results)
    return _return_payload(
        status,
        summary=details["summary"],
        checks=details["checks"],
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
