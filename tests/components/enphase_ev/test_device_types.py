from __future__ import annotations

import pytest

from custom_components.enphase_ev.device_types import (
    active_type_serials_from_inventory,
    active_type_keys_from_inventory,
    is_dry_contact_type_key,
    member_is_retired,
    normalize_type_key,
    parse_type_identifier,
    sanitize_member,
    type_display_label,
    type_identifier,
)
import custom_components.enphase_ev.device_types as device_types_mod


def test_normalize_type_key_handles_aliases_and_unknown_tokens() -> None:
    assert normalize_type_key(" IQEVSE ") == "iqevse"
    assert normalize_type_key("EV Chargers") == "iqevse"
    assert normalize_type_key("inverter") == "microinverter"
    assert normalize_type_key("inverters") == "microinverter"
    assert normalize_type_key("microinverters") == "microinverter"
    assert normalize_type_key("meter") == "envoy"
    assert normalize_type_key("enpower") == "envoy"
    assert normalize_type_key("systemcontroller") == "envoy"
    assert normalize_type_key("storage") == "encharge"
    assert normalize_type_key("storages") == "encharge"
    assert normalize_type_key("AC Battery") is None
    assert normalize_type_key("acbattery") is None
    assert normalize_type_key("heat-pump") == "heatpump"
    assert normalize_type_key("heat_pump") == "heatpump"
    assert normalize_type_key("Heat Pump") == "heatpump"
    assert normalize_type_key("NC1") == "dry_contact"
    assert normalize_type_key("Drycontactloads") == "dry_contact"
    assert normalize_type_key("GatewayDryContactLoad1") == "dry_contact"
    assert normalize_type_key("wind-turbine") == "wind_turbine"
    assert normalize_type_key("___") is None
    assert normalize_type_key("") is None
    assert normalize_type_key(None) is None


def test_normalize_type_key_handles_bad_string_conversion() -> None:
    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert normalize_type_key(BadStr()) is None


def test_type_display_label_uses_known_and_title_case_defaults() -> None:
    assert type_display_label("envoy") == "Gateway"
    assert type_display_label("ac_battery") is None
    assert type_display_label("heatpump") == "Heat Pump"
    assert type_display_label("dry_contact") == "Dry Contacts"
    assert type_display_label("wind_turbine") == "Wind Turbine"
    assert type_display_label(None) is None


def test_is_dry_contact_type_key_handles_relay_aliases() -> None:
    assert is_dry_contact_type_key(None) is False
    assert is_dry_contact_type_key("dry_contact") is True
    assert is_dry_contact_type_key("Dry Contact 1") is True
    assert is_dry_contact_type_key("Dry Contacts") is True
    assert is_dry_contact_type_key("Load-control relay NC1") is True
    assert is_dry_contact_type_key("NO2") is True
    assert is_dry_contact_type_key("GatewayDryContactLoad1") is True
    assert is_dry_contact_type_key("envoy") is False


def test_is_dry_contact_type_key_handles_empty_bad_and_unknown_values(
    monkeypatch,
) -> None:
    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert is_dry_contact_type_key(BadStr()) is False
    assert is_dry_contact_type_key("   ") is False

    monkeypatch.setattr(device_types_mod, "normalize_type_key", lambda _value: None)
    assert is_dry_contact_type_key("not-a-contact") is False

    monkeypatch.setattr(
        device_types_mod, "normalize_type_key", lambda _value: "dry_contacts_extra"
    )
    assert is_dry_contact_type_key("plain-text") is True


def test_known_type_order_places_heatpump_after_iqevse() -> None:
    assert device_types_mod.KNOWN_TYPE_ORDER.index(
        "iqevse"
    ) < device_types_mod.KNOWN_TYPE_ORDER.index("heatpump")


def test_onboarding_supported_type_keys_include_heatpump() -> None:
    assert "heatpump" in device_types_mod.ONBOARDING_SUPPORTED_TYPE_KEYS
    assert "ac_battery" not in device_types_mod.ONBOARDING_SUPPORTED_TYPE_KEYS


def test_type_identifier_round_trip_parsing() -> None:
    identifier = type_identifier("SITE123", "evse")
    assert identifier == ("enphase_ev", "type:SITE123:iqevse")
    assert parse_type_identifier(identifier[1]) == ("SITE123", "iqevse")
    assert parse_type_identifier("type:SITE123:meter") == ("SITE123", "envoy")
    assert parse_type_identifier("type:SITE123:enpower") == ("SITE123", "envoy")
    assert parse_type_identifier("type::iqevse") is None
    assert parse_type_identifier("site:SITE123") is None


def test_type_identifier_handles_bad_site_value() -> None:
    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert type_identifier(BadStr(), "iqevse") is None
    assert type_identifier("   ", "iqevse") is None
    assert type_identifier("site", None) is None


def test_type_display_label_empty_word_path(monkeypatch) -> None:
    monkeypatch.setattr(device_types_mod, "normalize_type_key", lambda _value: "_")
    assert device_types_mod.type_display_label("anything") is None


def test_parse_type_identifier_handles_bad_input_shapes() -> None:
    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert parse_type_identifier(None) is None
    assert parse_type_identifier(BadStr()) is None
    assert parse_type_identifier("type:only-two-parts") is None


def test_member_is_retired_detects_status_variants() -> None:
    assert member_is_retired({"status": "retired"}) is True
    assert member_is_retired({"statusText": "Retired"}) is True
    assert member_is_retired({"status_text": "RETIRED"}) is True
    assert member_is_retired({"isRetired": True}) is True
    assert member_is_retired({"status": "normal"}) is False


def test_member_is_retired_handles_non_dict_and_bad_status_value() -> None:
    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert member_is_retired("not-a-dict") is False
    assert member_is_retired({"status": BadStr()}) is False


def test_sanitize_member_keeps_scalars_and_stable_order() -> None:
    member = {
        "name": " Battery 1 ",
        "serial_number": "BAT-1",
        "status": "Normal",
        "nested": {"skip": True},
        "ports": [1, 2],
        "custom_flag": True,
        "watts": 12.5,
    }

    out = sanitize_member(member)

    assert out["name"] == "Battery 1"
    assert out["serial_number"] == "BAT-1"
    assert out["status"] == "Normal"
    assert out["custom_flag"] is True
    assert out["watts"] == 12.5
    assert "nested" not in out
    assert "ports" not in out


def test_sanitize_member_handles_non_dict_and_non_scalar_values() -> None:
    out = sanitize_member(
        {
            "name": None,
            "status": {"nested": "ignored"},
            "serial_number": "BAT-2",
            "extra": {"nested": "ignored"},
        }
    )
    assert out["name"] is None
    assert out["serial_number"] == "BAT-2"
    assert "status" not in out
    assert "extra" not in out
    assert sanitize_member("not-a-dict") == {}


def test_active_type_keys_from_inventory_handles_payload_shapes() -> None:
    assert active_type_keys_from_inventory("bad") == []
    assert active_type_keys_from_inventory({"result": {}}) == []
    assert active_type_keys_from_inventory([{"type": "envoy", "devices": [{}]}]) == [
        "envoy"
    ]
    assert active_type_keys_from_inventory(
        {"result": [{"deviceType": "inverters", "items": [{}]}]}
    ) == ["microinverter"]
    assert active_type_keys_from_inventory(
        {"result": [{"device_type": "microinverter", "members": [{}]}]}
    ) == ["microinverter"]


def test_active_type_keys_from_inventory_filters_and_orders() -> None:
    payload = {
        "result": [
            "bad",
            {"type": "envoy", "devices": [{}]},
            {"type": "generator", "devices": [{}]},
            {"type": "encharge", "devices": [{}]},
            {"type": "iqevse", "devices": "bad"},
            {"type": "microinverter", "devices": [{"status": "retired"}]},
            {"type": "wind_turbine", "devices": [{}]},
        ]
    }

    keys = active_type_keys_from_inventory(
        payload,
        allowed_type_keys=["envoy", "generator", "microinverter", "wind_turbine"],
    )

    assert keys == ["envoy", "generator", "wind_turbine"]


def test_active_type_keys_from_inventory_maps_storage_to_encharge() -> None:
    payload = {"result": [{"type": "storage", "devices": [{"serial_number": "BAT-1"}]}]}

    assert active_type_keys_from_inventory(payload) == ["encharge"]


def test_active_type_keys_from_inventory_detects_heatpump_in_hems_bucket() -> None:
    payload = {
        "result": [
            {
                "type": "hemsDevices",
                "devices": [
                    "bad-group",
                    {
                        "heat-pump": [
                            {"device-uid": "HP-1", "statusText": "Normal"},
                            {"device-uid": "HP-2", "status": "retired"},
                        ]
                    },
                ],
            }
        ]
    }

    keys = active_type_keys_from_inventory(
        payload,
        allowed_type_keys=["envoy", "heatpump", "iqevse"],
    )

    assert keys == ["heatpump"]


def test_active_type_serials_from_inventory_extracts_active_serials() -> None:
    payload = {
        "result": [
            {
                "type": "iqevse",
                "devices": [
                    {"serial_number": "EV1"},
                    {"serial": "EV2"},
                    {"serialNumber": "EV3"},
                    {"device_sn": "EV4"},
                    {"serial_number": "EV1"},
                    {"serial_number": "EV5", "status": "retired"},
                ],
            }
        ]
    }
    assert active_type_serials_from_inventory(payload, type_key="iqevse") == [
        "EV1",
        "EV2",
        "EV3",
        "EV4",
    ]
    assert active_type_serials_from_inventory(
        {
            "result": [
                {
                    "device_type": "inverters",
                    "members": [{"serialNumber": "INV1"}],
                }
            ]
        },
        type_key="microinverter",
    ) == ["INV1"]


def test_active_type_serials_from_inventory_handles_invalid_payload() -> None:
    assert active_type_serials_from_inventory("bad", type_key="iqevse") == []
    assert active_type_serials_from_inventory({"result": {}}, type_key="iqevse") == []


def test_active_type_serials_from_inventory_covers_remaining_branches() -> None:
    class BadSerial:
        def __str__(self) -> str:
            raise ValueError("boom")

    payload = [
        "bad-bucket",
        {"type": "envoy", "devices": [{"serial_number": "GW1"}]},
        {"type": "iqevse", "devices": "bad-members"},
        {
            "type": "iqevse",
            "devices": [{"serial_number": BadSerial()}, {"serial_number": "EV6"}],
        },
    ]

    assert active_type_serials_from_inventory(payload, type_key="iqevse") == ["EV6"]
    assert active_type_serials_from_inventory(payload, type_key=None) == []


@pytest.mark.parametrize(
    "legacy_type",
    ["AC Battery", "AC Batteries", "ac_battery", "ac_batteries", "acbattery"],
)
def test_retired_battery_family_is_ignored_in_inventory_and_selection(legacy_type):
    from custom_components.enphase_ev.config_selection import (
        normalize_selected_type_keys,
    )

    payload = {
        "result": [
            {"type": legacy_type, "devices": [{"serial_number": "AC-1"}]},
            {"type": "encharge", "devices": [{"serial_number": "IQ-1"}]},
        ]
    }
    assert active_type_keys_from_inventory(payload) == ["encharge"]
    assert normalize_selected_type_keys([legacy_type, "encharge"]) == ["encharge"]
