from __future__ import annotations

from custom_components.enphase_ev.log_redaction import (
    _key_kind,
    redact_identifier,
    redact_site_id,
    redact_text,
    truncate_identifier,
)


def test_identifier_helpers_redact_values() -> None:
    assert truncate_identifier("SERIAL-12345678") == "SERI...5678"
    assert truncate_identifier("   ") is None
    assert redact_identifier("ABCD") == "A...D"
    assert redact_identifier(None) == "[redacted]"
    assert redact_site_id("123456789") == "[site]"


def test_redact_text_scrubs_common_sensitive_values() -> None:
    text = (
        "site_id=12345&source=evse serialNumber=SERIAL-12345678 uid=DEVICE-UID-9999 "
        "email=user@example.com ip=10.0.0.2 mac=AA:BB:CC:DD:EE:FF "
        "next=private:cursor:795:-1 plain 12345"
    )

    redacted = redact_text(
        text,
        site_ids=("12345",),
        identifiers=("SERIAL-12345678", "DEVICE-UID-9999"),
    )

    assert "12345" not in redacted
    assert "SERIAL-12345678" not in redacted
    assert "DEVICE-UID-9999" not in redacted
    assert "user@example.com" not in redacted
    assert "10.0.0.2" not in redacted
    assert "AA:BB:CC:DD:EE:FF" not in redacted
    assert "private:cursor:795:-1" not in redacted
    assert "site_id=[site]" in redacted
    assert "source=evse" in redacted
    assert "serialNumber=SERI...5678" in redacted
    assert "uid=DEVI...9999" in redacted
    assert "next=[redacted]" in redacted
    assert redacted.count("[redacted]") >= 3


def test_redact_text_scrubs_site_ids_from_enphase_url_paths() -> None:
    text = (
        "GET /systems/9990001/devices?status=active "
        "GET /service/system_dashboard/api_internal/dashboard/sites/9990001/devices-tree "
        "GET /app-api/9990001/devices.json "
        "GET /service/evse_controller/api/v2/9990001/ev_chargers/summary "
        "GET /service/enho_historical_events_ms/9990001/filter_criteria "
        "GET /service/batteryConfig/api/v1/siteSettings/9990001 "
        "POST /service/batteryConfig/api/v1/batterySettings/acceptDisclaimer/9990001 "
        "PUT /service/batteryConfig/api/v1/battery/sites/9990001/schedules "
        "POST /service/batteryConfig/api/v1/stormGuard/toggle/9990001 "
        "POST /service/batteryConfig/api/v1/cancel/profile/9990001 "
        "PUT /service/evse_controller/api/v1/9990001/ev_chargers/EV1/ev_charger_config "
        "POST /service/evse_scheduler/api/v1/iqevc/charging-mode/9990001/EV1/preference "
        "PUT /service/evse_scheduler/api/v1/iqevc/charging-mode/GREEN_CHARGING/9990001/EV1/settings "
        "PATCH /service/evse_scheduler/api/v1/iqevc/charging-mode/SCHEDULED_CHARGING/9990001/EV1/schedules "
        "POST /pv/settings/9990001/battery_status.json"
    )

    redacted = redact_text(text, max_length=2000)

    assert "9990001" not in redacted
    assert "/systems/[site]/devices?status=active" in redacted
    assert "/sites/[site]/devices-tree" in redacted
    assert "/app-api/[site]/devices.json" in redacted
    assert "/evse_controller/api/v2/[site]/ev_chargers/summary" in redacted
    assert "/enho_historical_events_ms/[site]/filter_criteria" in redacted
    assert "/siteSettings/[site]" in redacted
    assert "/batterySettings/acceptDisclaimer/[site]" in redacted
    assert "/battery/sites/[site]/schedules" in redacted
    assert "/stormGuard/toggle/[site]" in redacted
    assert "/cancel/profile/[site]" in redacted
    assert "/evse_controller/api/v1/[site]/ev_chargers/EV1" in redacted
    assert "/charging-mode/[site]/EV1/preference" in redacted
    assert "/charging-mode/GREEN_CHARGING/[site]/EV1/settings" in redacted
    assert "/charging-mode/SCHEDULED_CHARGING/[site]/EV1/schedules" in redacted
    assert "/pv/settings/[site]/battery_status.json" in redacted


def test_identifier_helpers_cover_bad_string_inputs() -> None:
    class _BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert truncate_identifier(_BadStr()) is None


def test_redact_text_covers_key_kinds_and_truncation() -> None:
    class _BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    redacted = redact_text(
        (
            "entityId=sensor.foo site=123 sitename=Home token=secret host=router "
            "serial=SERIAL-12345678 chargerId=ABCDEFGH user=tester"
        ),
        site_ids=("123", _BadStr()),
        identifiers=("SERIAL-12345678", _BadStr()),
    )

    assert "entityId=sensor.foo" in redacted
    assert "site=[site]" in redacted
    assert "sitename=[site]" in redacted
    assert "token=[redacted]" in redacted
    assert "host=[redacted]" in redacted
    assert "serial=SERI...5678" in redacted
    assert "chargerId=A...H" in redacted
    assert "user=[redacted]" in redacted
    assert redact_text("x" * 40, max_length=10) == ("x" * 10) + "..."
    assert redact_text(_BadStr()) == ""


def test_key_kind_covers_non_stringable_and_empty_compact_keys() -> None:
    class _BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert _key_kind(_BadStr()) == "text"
    assert _key_kind("---") == "text"
