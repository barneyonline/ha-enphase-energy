from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path
import sys
import urllib.error

import pytest


@pytest.fixture(scope="module")
def service_status_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "service_status.py"
    spec = importlib.util.spec_from_file_location("service_status", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["service_status"] = module
    spec.loader.exec_module(module)
    return module


def _jwt(payload: dict[str, object]) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode(
        "utf-8"
    )
    return f"header.{encoded.rstrip('=')}.sig"


def test_build_history_samples_prunes_and_dedupes(service_status_module) -> None:
    previous_samples = [
        {
            "checked_at": "2026-01-01T00:00:00Z",
            "status": "Down",
            "checks_failed": 2,
            "failed_check_names": ["auth_login", "charger_status"],
        },
        {
            "checked_at": "2026-03-01T08:00:00Z",
            "status": "Down",
            "checks_failed": 1,
            "failed_check_names": ["charger_status"],
        },
        {
            "checked_at": "2026-03-06T08:00:00Z",
            "status": "Degraded",
            "checks_failed": 1,
            "failed_check_names": ["site_energy"],
        },
    ]
    current_payload = {
        "checked_at": "2026-03-07T08:00:00Z",
        "status": "Fully Operational",
        "summary": {"checks_failed": 0},
        "checks": [],
    }

    samples = service_status_module._build_history_samples(
        previous_samples,
        current_payload,
        retention_days=30,
    )

    assert [sample["checked_at"] for sample in samples] == [
        "2026-03-01T08:00:00Z",
        "2026-03-06T08:00:00Z",
        "2026-03-07T08:00:00Z",
    ]
    assert samples[-1]["status"] == "Fully Operational"
    assert samples[-1]["checks_failed"] == 0


def test_build_incidents_merges_and_splits(service_status_module) -> None:
    samples = [
        {
            "checked_at": "2026-03-01T00:00:00Z",
            "status": "Fully Operational",
            "checks_failed": 0,
            "failed_check_names": [],
        },
        {
            "checked_at": "2026-03-01T01:00:00Z",
            "status": "Degraded",
            "checks_failed": 1,
            "failed_check_names": ["site_energy"],
        },
        {
            "checked_at": "2026-03-01T02:00:00Z",
            "status": "Degraded",
            "checks_failed": 2,
            "failed_check_names": ["auth_settings", "site_energy"],
        },
        {
            "checked_at": "2026-03-01T03:00:00Z",
            "status": "Down",
            "checks_failed": 1,
            "failed_check_names": ["charger_status"],
        },
        {
            "checked_at": "2026-03-01T04:00:00Z",
            "status": "Fully Operational",
            "checks_failed": 0,
            "failed_check_names": [],
        },
    ]

    incidents = service_status_module._build_incidents(samples)

    assert incidents == [
        {
            "status": "Degraded",
            "started_at": "2026-03-01T01:00:00Z",
            "ended_at": "2026-03-01T03:00:00Z",
            "last_seen_at": "2026-03-01T02:00:00Z",
            "duration_minutes": 120,
            "active": False,
            "failed_checks": ["auth_settings", "site_energy"],
        },
        {
            "status": "Down",
            "started_at": "2026-03-01T03:00:00Z",
            "ended_at": "2026-03-01T04:00:00Z",
            "last_seen_at": "2026-03-01T03:00:00Z",
            "duration_minutes": 60,
            "active": False,
            "failed_checks": ["charger_status"],
        },
    ]


def test_build_incidents_splits_same_status_when_samples_are_missing(
    service_status_module,
) -> None:
    samples = [
        {
            "checked_at": "2026-03-01T01:00:00Z",
            "status": "Degraded",
            "checks_failed": 1,
            "failed_check_names": ["site_energy"],
        },
        {
            "checked_at": "2026-03-01T05:00:00Z",
            "status": "Degraded",
            "checks_failed": 1,
            "failed_check_names": ["site_energy"],
        },
    ]

    incidents = service_status_module._build_incidents(samples)

    assert len(incidents) == 2
    assert incidents[0]["started_at"] == "2026-03-01T01:00:00Z"
    assert incidents[0]["ended_at"] is None
    assert incidents[0]["active"] is False
    assert incidents[1]["started_at"] == "2026-03-01T05:00:00Z"
    assert incidents[1]["ended_at"] is None
    assert incidents[1]["active"] is True


def test_load_history_samples_handles_missing_and_corrupt(
    service_status_module, tmp_path: Path
) -> None:
    missing = service_status_module._load_history_samples(
        str(tmp_path / "missing.json")
    )
    assert missing == []

    corrupt_path = tmp_path / "history.json"
    corrupt_path.write_text("{not-json\n", encoding="utf-8")

    corrupt = service_status_module._load_history_samples(str(corrupt_path))
    assert corrupt == []


def test_render_wiki_page_with_no_incidents(service_status_module) -> None:
    payload = {
        "checked_at": "2026-03-07T08:00:00Z",
        "status": "Fully Operational",
        "summary": {"checks_failed": 0},
        "checks": [],
    }

    content = service_status_module._render_wiki_page(
        payload=payload,
        history_samples=[
            {
                "checked_at": "2026-03-07T08:00:00Z",
                "status": "Fully Operational",
                "checks_failed": 0,
                "failed_check_names": [],
            }
        ],
        incidents=[],
        raw_base_url="https://raw.githubusercontent.com/barneyonline/ha-enphase-energy/service-status",
    )

    assert "# Service Status History" in content
    assert "No incidents observed" in content
    assert "```mermaid" in content
    assert "history.json" in content
    assert "incidents.json" in content


def test_render_incident_table_marks_active_incidents(service_status_module) -> None:
    content = service_status_module._render_incident_table(
        [
            {
                "status": "Degraded",
                "started_at": "2026-03-07T08:00:00Z",
                "ended_at": None,
                "last_seen_at": "2026-03-07T08:00:00Z",
                "duration_minutes": 0,
                "active": True,
                "failed_checks": ["auth_settings"],
            }
        ]
    )

    assert "Ongoing (last seen 2026-03-07 08:00 UTC)" in content
    assert "Observed at latest check" in content


def test_render_mermaid_timeline_uses_colon_safe_labels(service_status_module) -> None:
    content = service_status_module._render_mermaid_timeline(
        [
            {
                "status": "Degraded",
                "started_at": "2026-03-12T16:50:43Z",
                "duration_minutes": 60,
            }
        ],
        checked_at="2026-03-12T17:50:43Z",
    )

    assert "Degraded 1 (2026-03-12 1650 UTC) :active" in content
    assert "Degraded 1 (2026-03-12 16:50 UTC)" not in content


def test_render_mermaid_timeline_anchors_to_30_day_window(
    service_status_module,
) -> None:
    content = service_status_module._render_mermaid_timeline(
        [
            {
                "status": "Degraded",
                "started_at": "2026-03-12T16:50:43Z",
                "duration_minutes": 60,
            }
        ],
        checked_at="2026-03-18T23:23:31Z",
    )

    assert "Window start :vert, window-start, 2026-02-16T23:23:31, 0ms" in content
    assert "Window end :vert, window-end, 2026-03-18T23:23:31, 0ms" in content
    assert "section Window" not in content


def test_nested_payload_and_timeline_helpers(
    service_status_module, monkeypatch
) -> None:
    payload = {
        "devices": [
            "bad",
            {"type": "heat_pump", "device_uid": "HP-1"},
            {"device_type": "iq_er", "uid": "ER-1"},
        ]
    }

    assert (
        service_status_module._first_device_uid(
            payload, type_tokens=("heat_pump", "heatpump")
        )
        == "HP-1"
    )
    assert service_status_module._first_device_uid({}, type_tokens=("missing",)) is None
    assert service_status_module._extract_summary_hems_flag("bad") is None
    assert service_status_module._extract_summary_hems_flag({"is_hems": True}) is True

    window_start, window_end = service_status_module._timeline_window(checked_at="bad")
    assert window_start < window_end

    monkeypatch.setattr(service_status_module, "DEFAULT_HISTORY_DAYS", -1)
    window_start, window_end = service_status_module._timeline_window(
        checked_at="2026-03-18T23:23:31Z"
    )
    assert window_start == "2026-03-18T22:23:31"
    assert window_end == "2026-03-18T23:23:31"


def test_write_outputs_generates_status_history_and_wiki(
    service_status_module, tmp_path: Path
) -> None:
    previous_history = tmp_path / "previous-history.json"
    previous_history.write_text(
        json.dumps(
            {
                "samples": [
                    {
                        "checked_at": "2026-03-06T08:00:00Z",
                        "status": "Degraded",
                        "checks_failed": 1,
                        "failed_check_names": ["site_energy"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    payload = {
        "checked_at": "2026-03-07T08:00:00Z",
        "status": "Fully Operational",
        "summary": {"checks_failed": 0},
        "checks": [],
        "endpoints": [],
    }

    service_status_module._write_outputs(
        str(tmp_path / "out"),
        status="Fully Operational",
        payload=payload,
        previous_history_file=str(previous_history),
        raw_base_url="https://example.invalid/service-status",
    )

    history_payload = json.loads((tmp_path / "out" / "history.json").read_text())
    incidents_payload = json.loads((tmp_path / "out" / "incidents.json").read_text())
    wiki_text = (tmp_path / "out" / "wiki" / "Service-Status-History.md").read_text()

    assert (tmp_path / "out" / "status.svg").exists()
    badge_payload = json.loads((tmp_path / "out" / "status_badge.json").read_text())
    assert badge_payload == {
        "schemaVersion": 1,
        "label": "Enphase Service Status",
        "message": "Fully Operational",
        "color": "#4c1",
    }
    assert history_payload["samples"][-1]["checked_at"] == "2026-03-07T08:00:00Z"
    assert incidents_payload["incidents"][0]["status"] == "Degraded"
    assert "Current status.json" in wiki_text
    assert "https://example.invalid/service-status/history.json" in wiki_text


def test_request_and_jwt_helpers(service_status_module) -> None:
    token = _jwt({"data": {"userId": "user-1", "session_id": "session-2"}})

    assert service_status_module._jwt_user_id(token) == "user-1"
    assert service_status_module._jwt_session_id(token) == "session-2"
    assert service_status_module._decode_jwt_payload("not-a-jwt") is None

    class ErrorOpener:
        def open(self, request, timeout=0):  # noqa: ARG002
            raise urllib.error.URLError("boom")

    status, headers, body, error = service_status_module._request(
        ErrorOpener(),
        "GET",
        "https://example.invalid",
    )
    assert status is None
    assert headers == {}
    assert body == b""
    assert "boom" in str(error)


def test_helper_edge_branches(
    service_status_module, tmp_path: Path, monkeypatch
) -> None:
    assert service_status_module._parse_iso_utc(None) is None
    assert service_status_module._parse_iso_utc("not-iso") is None
    assert service_status_module._format_utc(None) == "-"
    assert service_status_module._format_mermaid_label_utc(None) == "-"
    assert (
        service_status_module._format_mermaid_label_utc("2026-03-07T08:05:00Z")
        == "2026-03-07 0805 UTC"
    )
    assert len(service_status_module._mermaid_datetime(None)) == 19
    assert service_status_module._duration_label(0) == "0m"
    assert service_status_module._duration_label(65) == "1h 5m"
    assert service_status_module._duration_label(120) == "2h"
    assert service_status_module._duration_label(5) == "5m"
    assert service_status_module._safe_slug("Hello, world!") == "hello-world"
    assert service_status_module._safe_slug("!!!") == "item"
    assert service_status_module._reason_failure_name(None) is None
    assert service_status_module._reason_failure_name("unknown") is None

    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    assert (
        service_status_module._default_raw_base_url()
        == "https://raw.githubusercontent.com/owner/repo/service-status"
    )
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)

    assert service_status_module._decode_jwt_payload("bad.token") is None
    assert service_status_module._decode_jwt_payload("header.bm90LWpzb24.sig") is None
    assert service_status_module._jwt_user_id(None) is None
    assert service_status_module._jwt_user_id("not-a-jwt") is None
    assert service_status_module._jwt_user_id(_jwt({"foo": "bar"})) is None
    assert service_status_module._jwt_session_id(None) is None
    assert service_status_module._jwt_session_id("not-a-jwt") is None
    assert service_status_module._jwt_session_id(_jwt({"foo": "bar"})) is None
    assert service_status_module._jwt_user_id(_jwt({"userid": "user-2"})) == "user-2"
    assert (
        service_status_module._jwt_session_id(_jwt({"session": "session-3"}))
        == "session-3"
    )

    class FailingJar:
        def add_cookie_header(self, request):  # noqa: ARG002
            raise RuntimeError("nope")

    assert (
        service_status_module._cookie_header_for(
            "https://example.invalid", FailingJar()
        )
        == ""
    )

    class NoCookieJar:
        def add_cookie_header(self, request):  # noqa: ARG002
            return None

    assert (
        service_status_module._cookie_header_for(
            "https://example.invalid", NoCookieJar()
        )
        == ""
    )
    assert service_status_module._cookie_map("foo; a=1; XSRF-TOKEN=x") == {
        "a": "1",
        "XSRF-TOKEN": "x",
    }
    assert service_status_module._extract_xsrf_token({"foo": "bar"}) is None
    assert service_status_module._extract_bearer({}) is None

    class HeaderResp:
        def __init__(self):
            self.headers = {"x-header": "value"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def getcode(self):
            return 201

        def read(self):
            return b'{"ok":true}'

    class SuccessOpener:
        def open(self, request, timeout=0):  # noqa: ARG002
            assert request.data == b'{"a": 1}'
            assert request.headers["Content-type"] == "application/json"
            return HeaderResp()

    status, headers, body, error = service_status_module._request(
        SuccessOpener(),
        "POST",
        "https://example.invalid",
        json_body={"a": 1},
    )
    assert (status, headers["x-header"], body, error) == (
        201,
        "value",
        b'{"ok":true}',
        None,
    )

    class SuccessFormOpener:
        def open(self, request, timeout=0):  # noqa: ARG002
            assert request.data == b"a=1"
            assert (
                request.headers["Content-type"]
                == "application/x-www-form-urlencoded; charset=UTF-8"
            )
            return HeaderResp()

    status, _, _, _ = service_status_module._request(
        SuccessFormOpener(),
        "POST",
        "https://example.invalid",
        form={"a": 1},
    )
    assert status == 201

    class ReadFailHttpError(urllib.error.HTTPError):
        def read(self):
            raise RuntimeError("boom")

    class HttpErrorOpener:
        def open(self, request, timeout=0):  # noqa: ARG002
            raise ReadFailHttpError(
                "https://example.invalid", 500, "bad", hdrs={}, fp=None
            )

    status, _, body, error = service_status_module._request(
        HttpErrorOpener(),
        "GET",
        "https://example.invalid",
    )
    assert status == 500
    assert body == b""
    assert error == "HTTP 500"

    assert service_status_module._parse_json(b"") is None
    assert service_status_module._parse_json(b"{bad") is None
    assert service_status_module._read_json_file(tmp_path / "missing.json") is None
    assert (
        service_status_module._with_query("https://example.invalid")
        == "https://example.invalid"
    )
    assert (
        service_status_module._with_query("https://example.invalid", {})
        == "https://example.invalid"
    )
    monkeypatch.setattr(
        service_status_module.urllib.parse, "urlencode", lambda params: ""
    )
    assert (
        service_status_module._with_query("https://example.invalid", {"a": "b"})
        == "https://example.invalid"
    )
    assert service_status_module._normalize_sites(
        {"items": ["bad", {"id": "site-1"}]}
    ) == ["site-1"]
    assert service_status_module._normalize_chargers(
        {"chargers": ["bad", {"serialNumber": "serial-1"}]}
    ) == ["serial-1"]

    status, details = service_status_module._evaluate_status(
        [
            {
                "name": "alt",
                "group": "other",
                "ok": False,
                "affects_status": True,
                "check_group": "bundle",
                "check_mode": "any",
            },
            {
                "name": "primary",
                "group": "other",
                "ok": True,
                "affects_status": True,
                "check_group": "bundle",
                "check_mode": "any",
            },
        ]
    )
    assert status == "Fully Operational"
    assert details["summary"]["checks_ok"] == 1

    summary, checks = service_status_module._derive_payload_details(
        results=[],
        status="Down",
        summary=None,
        checks=None,
        reason="Missing ENPHASE_EMAIL or ENPHASE_PASSWORD",
    )
    assert summary["checks_failed"] == 1
    assert checks[0]["name"] == "missing_credentials"
    summary, checks = service_status_module._derive_payload_details(
        results=[],
        status="Fully Operational",
        summary=None,
        checks=None,
        reason=None,
    )
    assert summary["checks_total"] == 0
    assert checks == []

    sample = service_status_module._history_sample_from_payload(
        {
            "checked_at": "2026-03-07T08:00:00Z",
            "status": "Down",
            "summary": {},
            "checks": [{"name": "a", "ok": False}],
        }
    )
    assert sample["checks_failed"] == 1

    assert service_status_module._normalize_history_sample("bad") is None
    assert (
        service_status_module._normalize_history_sample(
            {"checked_at": "bad", "status": "Down"}
        )
        is None
    )
    normalized = service_status_module._normalize_history_sample(
        {
            "checked_at": "2026-03-07T08:00:00Z",
            "status": "Down",
            "checks_failed": "bad",
            "failed_check_names": "bad",
        }
    )
    assert normalized["checks_failed"] == 0
    assert normalized["failed_check_names"] == []

    payload_path = tmp_path / "history.json"
    payload_path.write_text(json.dumps({"samples": "bad"}), encoding="utf-8")
    assert service_status_module._load_history_samples(str(payload_path)) == []
    service_status_module._write_json_file(
        tmp_path / "nested" / "payload.json", {"a": 1}
    )
    assert json.loads((tmp_path / "nested" / "payload.json").read_text()) == {"a": 1}
    assert (
        service_status_module._build_incidents(
            [{"checked_at": "bad", "status": "Down", "failed_check_names": []}]
        )
        == []
    )


def test_endpoint_result_and_status_evaluation(service_status_module) -> None:
    spec = service_status_module.EndpointSpec(
        name="charger_status",
        method="GET",
        url="https://example.invalid/service/SITE/SERIAL?site=SITE&serial=SERIAL",
        group="main",
        category="evse_runtime",
    )
    endpoint = service_status_module._endpoint_result(
        spec,
        500,
        "HTTP 500",
        site_id="SITE",
        serial="SERIAL",
    )
    assert (
        endpoint["url"] == "/service/{site_id}/{serial}?site={site_id}&serial={serial}"
    )
    optional_spec = service_status_module.EndpointSpec(
        name="battery_status",
        method="GET",
        url="https://example.invalid/app-api/SITE/battery_status.json",
        group="degraded",
        category="battery_runtime",
        affects_status=False,
    )
    optional_endpoint = service_status_module._endpoint_result(
        optional_spec,
        500,
        "HTTP 500",
        site_id="SITE",
        serial="SERIAL",
    )
    assert optional_endpoint["affects_status"] is False
    assert optional_endpoint["affects_broad_outage"] is True

    status, details = service_status_module._evaluate_status(
        [
            {
                "name": "charger_status",
                "group": "main",
                "ok": False,
                "affects_status": True,
                "check_group": None,
                "check_mode": "all",
            },
            {
                "name": "battery_status",
                "group": "degraded",
                "ok": True,
                "affects_status": False,
                "check_group": None,
                "check_mode": "all",
            },
        ]
    )
    assert status == "Down"
    assert details["summary"]["checks_failed"] == 1
    assert details["summary"]["checks_failed_affecting"] == 1


def test_evaluate_status_keeps_multiple_degraded_failures_degraded(
    service_status_module,
) -> None:
    status, details = service_status_module._evaluate_status(
        [
            {
                "name": "site_energy",
                "group": "degraded",
                "ok": False,
                "affects_status": True,
                "check_group": None,
                "check_mode": "all",
            },
            {
                "name": "auth_settings",
                "group": "degraded",
                "ok": False,
                "affects_status": True,
                "check_group": None,
                "check_mode": "all",
            },
            {
                "name": "charger_status",
                "group": "main",
                "ok": True,
                "affects_status": True,
                "check_group": None,
                "check_mode": "all",
            },
        ]
    )

    assert status == "Degraded"
    assert details["summary"]["checks_failed"] == 2
    assert details["summary"]["checks_failed_affecting"] == 2


def test_evaluate_status_keeps_broad_non_affecting_outage_degraded(
    service_status_module,
) -> None:
    status, details = service_status_module._evaluate_status(
        [
            {
                "name": "charger_status",
                "group": "main",
                "ok": True,
                "affects_status": True,
                "check_group": None,
                "check_mode": "all",
            },
            {
                "name": "battery_config",
                "group": "degraded",
                "ok": False,
                "affects_status": False,
                "affects_broad_outage": True,
                "check_group": None,
                "check_mode": "all",
            },
            {
                "name": "battery_runtime",
                "group": "degraded",
                "ok": False,
                "affects_status": False,
                "affects_broad_outage": True,
                "check_group": None,
                "check_mode": "all",
            },
            {
                "name": "microinverters",
                "group": "degraded",
                "ok": False,
                "affects_status": False,
                "affects_broad_outage": True,
                "check_group": None,
                "check_mode": "all",
            },
        ]
    )

    assert status == "Degraded"
    assert details["summary"]["checks_failed"] == 3
    assert details["summary"]["checks_failed_affecting"] == 0
    assert details["summary"]["checks_failed_non_affecting"] == 3
    assert details["summary"]["checks_failed_broad_outage"] == 3


def test_evaluate_status_keeps_healthy_runtime_with_failed_subservices_degraded(
    service_status_module,
) -> None:
    status, details = service_status_module._evaluate_status(
        [
            {
                "name": "auth",
                "group": "other",
                "ok": True,
                "affects_status": True,
                "affects_broad_outage": True,
                "check_group": None,
                "check_mode": "all",
            },
            {
                "name": "discovery",
                "group": "other",
                "ok": True,
                "affects_status": True,
                "affects_broad_outage": True,
                "check_group": None,
                "check_mode": "all",
            },
            {
                "name": "charger_status",
                "group": "main",
                "ok": True,
                "affects_status": True,
                "affects_broad_outage": True,
                "check_group": None,
                "check_mode": "all",
            },
            {
                "name": "evse_scheduler",
                "group": "degraded",
                "ok": False,
                "affects_status": True,
                "affects_broad_outage": True,
                "check_group": None,
                "check_mode": "all",
            },
            {
                "name": "session_history",
                "group": "degraded",
                "ok": False,
                "affects_status": True,
                "affects_broad_outage": True,
                "check_group": None,
                "check_mode": "all",
            },
            {
                "name": "battery_config",
                "group": "degraded",
                "ok": False,
                "affects_status": False,
                "affects_broad_outage": True,
                "check_group": None,
                "check_mode": "all",
            },
            {
                "name": "auth_settings",
                "group": "degraded",
                "ok": False,
                "affects_status": True,
                "affects_broad_outage": True,
                "check_group": None,
                "check_mode": "all",
            },
        ]
    )

    assert status == "Degraded"
    assert details["summary"]["checks_failed"] == 4
    assert details["summary"]["checks_failed_affecting"] == 3
    assert details["summary"]["checks_failed_broad_outage"] == 4


def test_evaluate_status_ignores_non_affecting_non_broad_outage_failures(
    service_status_module,
) -> None:
    status, details = service_status_module._evaluate_status(
        [
            {
                "name": "charger_status",
                "group": "main",
                "ok": True,
                "affects_status": True,
                "check_group": None,
                "check_mode": "all",
            },
            {
                "name": "optional_a",
                "group": "degraded",
                "ok": False,
                "affects_status": False,
                "affects_broad_outage": False,
                "check_group": None,
                "check_mode": "all",
            },
            {
                "name": "optional_b",
                "group": "degraded",
                "ok": False,
                "affects_status": False,
                "affects_broad_outage": False,
                "check_group": None,
                "check_mode": "all",
            },
            {
                "name": "optional_c",
                "group": "degraded",
                "ok": False,
                "affects_status": False,
                "affects_broad_outage": False,
                "check_group": None,
                "check_mode": "all",
            },
        ]
    )

    assert status == "Fully Operational"
    assert details["summary"]["checks_failed"] == 3
    assert details["summary"]["checks_failed_broad_outage"] == 0


def test_evaluate_status_keeps_single_degraded_failure_degraded(
    service_status_module,
) -> None:
    status, details = service_status_module._evaluate_status(
        [
            {
                "name": "site_energy",
                "group": "degraded",
                "ok": False,
                "affects_status": True,
                "check_group": None,
                "check_mode": "all",
            },
            {
                "name": "battery_config",
                "group": "degraded",
                "ok": False,
                "affects_status": False,
                "check_group": None,
                "check_mode": "all",
            },
            {
                "name": "charger_status",
                "group": "main",
                "ok": True,
                "affects_status": True,
                "check_group": None,
                "check_mode": "all",
            },
        ]
    )

    assert status == "Degraded"
    assert details["summary"]["checks_failed"] == 2
    assert details["summary"]["checks_failed_affecting"] == 1


def test_render_table_unknown_gap_and_incident_duration_default(
    service_status_module,
) -> None:
    assert (
        service_status_module._incident_mermaid_duration({"duration_minutes": "bad"})
        == service_status_module.MIN_VISIBLE_INCIDENT_MINUTES
    )
    content = service_status_module._render_incident_table(
        [
            {
                "status": "Down",
                "started_at": "2026-03-07T08:00:00Z",
                "ended_at": None,
                "last_seen_at": "2026-03-07T09:00:00Z",
                "duration_minutes": 60,
                "active": False,
                "failed_checks": [],
            }
        ]
    )
    assert "Unknown after last seen 2026-03-07 09:00 UTC" in content
    assert "Observed 1h" in content
    content = service_status_module._render_incident_table(
        [
            {
                "status": "Down",
                "started_at": "2026-03-07T08:00:00Z",
                "ended_at": None,
                "last_seen_at": "2026-03-07T10:00:00Z",
                "duration_minutes": 120,
                "active": True,
                "failed_checks": [],
            }
        ]
    )
    assert "Observed 2h" in content


def test_main_missing_credentials_generates_history(
    service_status_module, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("ENPHASE_EMAIL", raising=False)
    monkeypatch.delenv("ENPHASE_PASSWORD", raising=False)
    monkeypatch.delenv("ENPHASE_SITE_ID", raising=False)
    monkeypatch.delenv("ENPHASE_SERIAL", raising=False)
    monkeypatch.delenv("ENPHASE_LOCALE", raising=False)

    argv = [
        "service_status.py",
        "--output-dir",
        str(tmp_path / "out"),
    ]
    old_argv = sys.argv
    sys.argv = argv
    try:
        result = service_status_module.main()
    finally:
        sys.argv = old_argv

    status_payload = json.loads((tmp_path / "out" / "status.json").read_text())
    history_payload = json.loads((tmp_path / "out" / "history.json").read_text())

    assert result == 0
    assert status_payload["status"] == "Down"
    assert "Missing ENPHASE_EMAIL" in status_payload["reason"]
    assert history_payload["samples"][0]["status"] == "Down"
    assert history_payload["samples"][0]["failed_check_names"] == [
        "missing_credentials"
    ]


def test_main_mfa_required_generates_synthetic_failure(
    service_status_module, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ENPHASE_EMAIL", "user@example.com")
    monkeypatch.setenv("ENPHASE_PASSWORD", "secret")

    def fake_request(opener, method, url, **kwargs):  # noqa: ARG001
        if url == service_status_module.LOGIN_URL:
            return 200, {}, b'{"requires_mfa":true}', None
        raise AssertionError(f"Unexpected request: {method} {url}")

    monkeypatch.setattr(service_status_module, "_request", fake_request)

    argv = [
        "service_status.py",
        "--output-dir",
        str(tmp_path / "out"),
    ]
    old_argv = sys.argv
    sys.argv = argv
    try:
        result = service_status_module.main()
    finally:
        sys.argv = old_argv

    status_payload = json.loads((tmp_path / "out" / "status.json").read_text())
    history_payload = json.loads((tmp_path / "out" / "history.json").read_text())

    assert result == 0
    assert status_payload["status"] == "Down"
    assert status_payload["summary"]["checks_failed"] == 1
    assert {check["name"] for check in status_payload["checks"]} == {
        "auth",
        "auth_mfa_required",
    }
    assert history_payload["samples"][0]["failed_check_names"] == ["auth_mfa_required"]


@pytest.mark.parametrize(
    ("login_body", "expected_reason", "expected_failed_check"),
    [
        ('{"success":false}', "Login rejected", "login_rejected"),
        ('{"isBlocked":true}', "Account is blocked", "account_blocked"),
    ],
)
def test_main_login_rejected_paths_generate_synthetic_failures(
    service_status_module,
    tmp_path: Path,
    monkeypatch,
    login_body: str,
    expected_reason: str,
    expected_failed_check: str,
) -> None:
    monkeypatch.setenv("ENPHASE_EMAIL", "user@example.com")
    monkeypatch.setenv("ENPHASE_PASSWORD", "secret")

    def fake_request(opener, method, url, **kwargs):  # noqa: ARG001
        if url == service_status_module.LOGIN_URL:
            return 200, {}, login_body.encode("utf-8"), None
        raise AssertionError(f"Unexpected request: {method} {url}")

    monkeypatch.setattr(service_status_module, "_request", fake_request)

    argv = [
        "service_status.py",
        "--output-dir",
        str(tmp_path / "out"),
    ]
    old_argv = sys.argv
    sys.argv = argv
    try:
        result = service_status_module.main()
    finally:
        sys.argv = old_argv

    status_payload = json.loads((tmp_path / "out" / "status.json").read_text())
    history_payload = json.loads((tmp_path / "out" / "history.json").read_text())

    assert result == 0
    assert status_payload["reason"] == expected_reason
    assert history_payload["samples"][0]["failed_check_names"] == [
        expected_failed_check
    ]


def test_main_login_failed_status_code_generates_synthetic_failure(
    service_status_module, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ENPHASE_EMAIL", "user@example.com")
    monkeypatch.setenv("ENPHASE_PASSWORD", "secret")

    def fake_request(opener, method, url, **kwargs):  # noqa: ARG001
        if url == service_status_module.LOGIN_URL:
            return 401, {}, b"{}", "HTTP 401"
        raise AssertionError(f"Unexpected request: {method} {url}")

    monkeypatch.setattr(service_status_module, "_request", fake_request)

    argv = [
        "service_status.py",
        "--output-dir",
        str(tmp_path / "out"),
    ]
    old_argv = sys.argv
    sys.argv = argv
    try:
        result = service_status_module.main()
    finally:
        sys.argv = old_argv

    history_payload = json.loads((tmp_path / "out" / "history.json").read_text())

    assert result == 0
    assert history_payload["samples"][0]["failed_check_names"] == ["auth"]


def test_main_site_discovery_and_serial_discovery_failures(
    service_status_module, tmp_path: Path, monkeypatch
) -> None:
    token = _jwt({"user_id": "user-1"})
    monkeypatch.setenv("ENPHASE_EMAIL", "user@example.com")
    monkeypatch.setenv("ENPHASE_PASSWORD", "secret")
    monkeypatch.delenv("ENPHASE_SITE_ID", raising=False)
    monkeypatch.delenv("ENPHASE_SERIAL", raising=False)
    monkeypatch.setattr(
        service_status_module,
        "_cookie_header_for",
        lambda url, jar: (
            f"XSRF-TOKEN=xsrf-token; enlighten_manager_token_production={token}"
            if "enlighten" in url
            else ""
        ),
    )

    def fake_request_discovery(opener, method, url, **kwargs):  # noqa: ARG001
        if url == service_status_module.LOGIN_URL:
            return 200, {}, b'{"success":true}', None
        if url == service_status_module.SITE_SEARCH_URL:
            return 200, {}, b"[]", None
        raise AssertionError(f"Unexpected request: {method} {url}")

    monkeypatch.setattr(service_status_module, "_request", fake_request_discovery)

    argv = [
        "service_status.py",
        "--output-dir",
        str(tmp_path / "discovery-out"),
    ]
    old_argv = sys.argv
    sys.argv = argv
    try:
        result = service_status_module.main()
    finally:
        sys.argv = old_argv

    discovery_payload = json.loads(
        (tmp_path / "discovery-out" / "history.json").read_text()
    )
    assert result == 0
    assert discovery_payload["samples"][0]["failed_check_names"] == ["auth"]

    def fake_request_serial(opener, method, url, **kwargs):  # noqa: ARG001
        if url == service_status_module.LOGIN_URL:
            return 200, {}, b'{"session_id":"session-1","success":true}', None
        if url == f"{service_status_module.ENTREZ_URL}/tokens":
            return 200, {}, json.dumps({"token": token}).encode("utf-8"), None
        if url == service_status_module.SITE_SEARCH_URL:
            return 200, {}, b'[{"site_id":"SITE"}]', None
        if "ev_chargers/summary" in url:
            return 200, {}, b'{"data":[]}', None
        if url.endswith("/ev_chargers/status"):
            return 200, {}, b'{"evChargerData":[]}', None
        raise AssertionError(f"Unexpected request: {method} {url}")

    monkeypatch.setattr(service_status_module, "_request", fake_request_serial)

    argv = [
        "service_status.py",
        "--output-dir",
        str(tmp_path / "serial-out"),
    ]
    old_argv = sys.argv
    sys.argv = argv
    try:
        result = service_status_module.main()
    finally:
        sys.argv = old_argv

    serial_payload = json.loads((tmp_path / "serial-out" / "history.json").read_text())
    assert result == 0
    assert serial_payload["samples"][0]["failed_check_names"] == ["serial_discovery"]


def test_main_site_id_preseed_and_status_payload_serial_discovery(
    service_status_module, tmp_path: Path, monkeypatch
) -> None:
    token = _jwt({"user_id": "user-1", "session_id": "session-1"})
    monkeypatch.setenv("ENPHASE_EMAIL", "user@example.com")
    monkeypatch.setenv("ENPHASE_PASSWORD", "secret")
    monkeypatch.setenv("ENPHASE_SITE_ID", "SITE")
    monkeypatch.delenv("ENPHASE_SERIAL", raising=False)
    seen_referers: list[str] = []
    monkeypatch.setattr(
        service_status_module,
        "_cookie_header_for",
        lambda url, jar: (
            f"XSRF-TOKEN=xsrf-token; enlighten_manager_token_production={token}"
            if "enlighten" in url
            else ""
        ),
    )

    def fake_request(opener, method, url, **kwargs):  # noqa: ARG001
        headers = kwargs.get("headers") or {}
        if "Referer" in headers:
            seen_referers.append(headers["Referer"])
        if url == service_status_module.LOGIN_URL:
            return 200, {}, b'{"session_id":"session-1","success":true}', None
        if url == f"{service_status_module.ENTREZ_URL}/tokens":
            return 200, {}, json.dumps({"token": token}).encode("utf-8"), None
        if url == service_status_module.SITE_SEARCH_URL:
            return 200, {}, b'[{"site_id":"SITE"}]', None
        if "ev_chargers/summary" in url:
            return 200, {}, b'{"data":[]}', None
        if url.endswith("/ev_chargers/status"):
            return 200, {}, b'{"evChargerData":[{"serial":"SERIAL"}]}', None
        return 200, {}, b"{}", None

    monkeypatch.setattr(service_status_module, "_request", fake_request)

    argv = [
        "service_status.py",
        "--output-dir",
        str(tmp_path / "out"),
    ]
    old_argv = sys.argv
    sys.argv = argv
    try:
        result = service_status_module.main()
    finally:
        sys.argv = old_argv

    status_payload = json.loads((tmp_path / "out" / "status.json").read_text())
    assert result == 0
    assert status_payload["status"] == "Fully Operational"
    assert any(
        referer.endswith("/pv/systems/SITE/summary") for referer in seen_referers
    )


def test_main_success_generates_history_and_wiki(
    service_status_module, tmp_path: Path, monkeypatch
) -> None:
    token = _jwt({"user_id": "user-1", "session_id": "session-1"})
    previous_history = tmp_path / "previous-history.json"
    previous_history.write_text(
        json.dumps(
            {
                "samples": [
                    {
                        "checked_at": "2026-03-06T08:00:00Z",
                        "status": "Fully Operational",
                        "checks_failed": 0,
                        "failed_check_names": [],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("ENPHASE_EMAIL", "user@example.com")
    monkeypatch.setenv("ENPHASE_PASSWORD", "secret")
    monkeypatch.delenv("ENPHASE_SITE_ID", raising=False)
    monkeypatch.delenv("ENPHASE_SERIAL", raising=False)
    monkeypatch.setattr(
        service_status_module,
        "_cookie_header_for",
        lambda url, jar: (
            f"XSRF-TOKEN=xsrf-token; enlighten_manager_token_production={token}"
            if "enlighten" in url
            else ""
        ),
    )

    def fake_request(opener, method, url, **kwargs):  # noqa: ARG001
        if url == service_status_module.LOGIN_URL:
            return 200, {}, b'{"session_id":"session-1","success":true}', None
        if url == f"{service_status_module.ENTREZ_URL}/tokens":
            return 200, {}, json.dumps({"token": token}).encode("utf-8"), None
        if url == service_status_module.SITE_SEARCH_URL:
            return 200, {}, b'[{"site_id":"SITE"}]', None
        if "ev_chargers/summary" in url:
            return 200, {}, b'{"data":[{"serial":"SERIAL"}]}', None
        if url.endswith("/ev_chargers/status"):
            return 200, {}, b'{"evChargerData":[{"serial":"SERIAL"}]}', None
        if "auth_settings" in url or "ev_charger_config" in url:
            return 500, {}, b"{}", "HTTP 500"
        return 200, {}, b"{}", None

    monkeypatch.setattr(service_status_module, "_request", fake_request)

    argv = [
        "service_status.py",
        "--output-dir",
        str(tmp_path / "out"),
        "--previous-history-file",
        str(previous_history),
        "--raw-base-url",
        "https://raw.example.invalid/service-status",
    ]
    old_argv = sys.argv
    sys.argv = argv
    try:
        result = service_status_module.main()
    finally:
        sys.argv = old_argv

    status_payload = json.loads((tmp_path / "out" / "status.json").read_text())
    history_payload = json.loads((tmp_path / "out" / "history.json").read_text())
    incidents_payload = json.loads((tmp_path / "out" / "incidents.json").read_text())
    wiki_text = (tmp_path / "out" / "wiki" / "Service-Status-History.md").read_text()

    assert result == 0
    assert status_payload["status"] == "Degraded"
    assert status_payload["summary"]["checks_failed"] == 1
    assert history_payload["samples"][-1]["status"] == "Degraded"
    assert incidents_payload["incidents"][-1]["status"] == "Degraded"
    assert incidents_payload["incidents"][-1]["active"] is True
    assert incidents_payload["incidents"][-1]["ended_at"] is None
    assert "evse_control" in wiki_text
    assert "Ongoing" in wiki_text
    expected_label = service_status_module._format_mermaid_label_utc(
        incidents_payload["incidents"][-1]["started_at"]
    )
    assert f"Degraded 1 ({expected_label}) :active" in wiki_text
    assert (
        f"Degraded 1 ({service_status_module._format_utc(incidents_payload['incidents'][-1]['started_at'])})"
        not in wiki_text
    )
    assert "raw.example.invalid/service-status/history.json" in wiki_text


def test_main_treats_optional_probe_4xx_responses_as_operational(
    service_status_module, tmp_path: Path, monkeypatch
) -> None:
    token = _jwt({"user_id": "user-1", "session_id": "session-1"})

    monkeypatch.setenv("ENPHASE_EMAIL", "user@example.com")
    monkeypatch.setenv("ENPHASE_PASSWORD", "secret")
    monkeypatch.setenv("ENPHASE_SITE_ID", "SITE")
    monkeypatch.delenv("ENPHASE_SERIAL", raising=False)
    monkeypatch.setattr(
        service_status_module,
        "_cookie_header_for",
        lambda url, jar: (
            f"XSRF-TOKEN=xsrf-token; enlighten_manager_token_production={token}"
            if "enlighten" in url
            else ""
        ),
    )

    def fake_request(opener, method, url, **kwargs):  # noqa: ARG001
        if url == service_status_module.LOGIN_URL:
            return 200, {}, b'{"session_id":"session-1","success":true}', None
        if url == f"{service_status_module.ENTREZ_URL}/tokens":
            return 400, {}, b"{}", "HTTP 400"
        if url == service_status_module.SITE_SEARCH_URL:
            return 200, {}, b'[{"site_id":"SITE"}]', None
        if "ev_chargers/summary" in url:
            return 200, {}, b'{"data":[{"serial":"SERIAL"}]}', None
        if url.endswith("/ev_chargers/status"):
            return 200, {}, b'{"evChargerData":[{"serial":"SERIAL"}]}', None
        if "/service/evse_scheduler/" in url:
            return 400, {}, b"{}", "HTTP 400"
        if "/service/enho_historical_events_ms/" in url and method == "POST":
            return 404, {}, b"{}", "HTTP 404"
        if url.endswith("/schedules/isValid"):
            return 403, {}, b"{}", "HTTP 403"
        if "ev_charger_config" in url:
            return 404, {}, b"{}", "HTTP 404"
        return 200, {}, b"{}", None

    monkeypatch.setattr(service_status_module, "_request", fake_request)

    argv = [
        "service_status.py",
        "--output-dir",
        str(tmp_path / "out"),
    ]
    old_argv = sys.argv
    sys.argv = argv
    try:
        result = service_status_module.main()
    finally:
        sys.argv = old_argv

    status_payload = json.loads((tmp_path / "out" / "status.json").read_text())

    assert result == 0
    assert status_payload["status"] == "Fully Operational"
    assert status_payload["summary"]["checks_failed"] == 0
    assert status_payload["summary"]["endpoints_failed"] == 0


def test_main_checks_hems_endpoints_when_inventory_reports_hems(
    service_status_module, tmp_path: Path, monkeypatch
) -> None:
    token = _jwt({"user_id": "user-1", "session_id": "session-1"})
    seen_urls: list[str] = []

    monkeypatch.setenv("ENPHASE_EMAIL", "user@example.com")
    monkeypatch.setenv("ENPHASE_PASSWORD", "secret")
    monkeypatch.setenv("ENPHASE_SITE_ID", "SITE")
    monkeypatch.setenv("ENPHASE_SERIAL", "SERIAL")
    monkeypatch.setattr(
        service_status_module,
        "_cookie_header_for",
        lambda url, jar: (
            f"XSRF-TOKEN=xsrf-token; enlighten_manager_token_production={token}"
            if "enlighten" in url
            else ""
        ),
    )

    def fake_request(opener, method, url, **kwargs):  # noqa: ARG001
        seen_urls.append(url)
        if url == service_status_module.LOGIN_URL:
            return 200, {}, b'{"session_id":"session-1","success":true}', None
        if url == f"{service_status_module.ENTREZ_URL}/tokens":
            return 200, {}, json.dumps({"token": token}).encode("utf-8"), None
        if url.endswith("/ev_chargers/status"):
            return 200, {}, b'{"evChargerData":[{"serial":"SERIAL"}]}', None
        if url.endswith("/devices.json"):
            return 200, {}, b'{"devices":[{"kind":"hems"}]}', None
        if "/system_dashboard/api_internal/cs/sites/" in url:
            return 500, {}, b"{}", "HTTP 500"
        if "hems-devices" in url:
            return (
                200,
                {},
                json.dumps(
                    {
                        "devices": [
                            {"type": "heat_pump", "device_uid": "HP-1"},
                            {"device_type": "iq_er", "uid": "ER-1"},
                        ]
                    }
                ).encode("utf-8"),
                None,
            )
        return 200, {}, b"{}", None

    monkeypatch.setattr(service_status_module, "_request", fake_request)

    argv = [
        "service_status.py",
        "--output-dir",
        str(tmp_path / "out"),
    ]
    old_argv = sys.argv
    sys.argv = argv
    try:
        result = service_status_module.main()
    finally:
        sys.argv = old_argv

    status_payload = json.loads((tmp_path / "out" / "status.json").read_text())
    endpoint_names = {endpoint["name"] for endpoint in status_payload["endpoints"]}

    assert result == 0
    assert status_payload["status"] == "Fully Operational"
    assert "hems_devices" in endpoint_names
    assert "hems_heatpump_state" in endpoint_names
    assert "hems_power_timeseries" in endpoint_names
    assert "heat_pump_events" in endpoint_names
    assert "iq_er_events" in endpoint_names
    assert any("hems_consumption_lifetime" in url for url in seen_urls)
    assert any("energy-consumption" in url for url in seen_urls)
