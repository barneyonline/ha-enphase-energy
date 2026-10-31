"""Tests for typed API parser helpers."""

from __future__ import annotations

import pytest

from custom_components.enphase_ev import api_parsers
from custom_components.enphase_ev.api import EnphaseEVClient


def test_discovery_without_wrapper_lists():
    assert api_parsers.normalize_sites({"sites": None}) == []
    chargers = api_parsers.normalize_chargers([{"serial": "EV123"}])
    assert [charger.serial for charger in chargers] == ["EV123"]


def test_latest_power_without_sample_time():
    sample = api_parsers.parse_latest_power_payload(
        {"latest_power": {"value": 10, "units": "W"}}
    )
    assert sample is not None
    assert sample.time is None


def test_timeseries_date_accepts_unix_seconds():
    assert api_parsers.parse_evse_timeseries_date_key(1700000000) == "2023-11-14"


@pytest.mark.parametrize(
    "payload",
    [
        {"energy_kwh": None},
        {"values": [None, {"value": "bad"}, {"other": 1}]},
        {"values": [{"energy_kwh": "bad", "value": None}]},
    ],
)
def test_lifetime_timeseries_with_no_usable_energy_is_ignored(payload):
    assert api_parsers.parse_evse_lifetime_entry("EV123", payload) is None


@pytest.mark.parametrize("daily", [True, False])
def test_timeseries_list_ignores_records_without_energy(daily):
    assert (
        api_parsers.normalize_evse_timeseries_payload(
            [{"serial": "EV123"}], daily=daily
        )
        == {}
    )


@pytest.mark.parametrize("consumption", [None, [{"solar": 1, "details": None}]])
def test_hems_consumption_omits_missing_detail_lists(consumption):
    entry = api_parsers.parse_hems_daily_consumption_entry(
        {"device_uid": "HP", "consumption": consumption}
    )
    assert entry is not None
    assert entry.to_dict()["consumption"] == (
        []
        if consumption is None
        else [{"solar": 1.0, "battery": None, "grid": None, "details": []}]
    )


def test_hems_consumption_ignores_non_mapping_devices():
    result = api_parsers.normalize_hems_energy_consumption_payload(
        {"heat-pump": [None]}
    )
    assert result is not None
    assert result["data"]["heat-pump"] == []


def test_pv_today_without_statistics_has_empty_statistics():
    assert api_parsers.normalize_pv_system_today_payload({"stats": None}) == {
        "stats": []
    }


def test_discovery_parsers_return_typed_models() -> None:
    """Discovery payloads normalize into API boundary model instances."""

    sites = api_parsers.normalize_sites(
        {"systems": [{"system_id": 123, "displayName": "Garage"}]}
    )
    chargers = api_parsers.normalize_chargers(
        {"evChargerData": [{"serialNumber": "EV123", "display_name": "Driveway"}]}
    )

    assert [(site.site_id, site.name) for site in sites] == [("123", "Garage")]
    assert [(charger.serial, charger.name) for charger in chargers] == [
        ("EV123", "Driveway")
    ]


def test_discovery_parsers_preserve_legacy_field_priority() -> None:
    """Parser extraction keeps the old field precedence for mixed payloads."""

    sites = api_parsers.normalize_sites(
        {
            "sites": [
                {
                    "site_id": "123",
                    "site_name": "Legacy Name",
                    "title": "Title Name",
                    "displayName": "Display Name",
                }
            ]
        }
    )
    chargers = api_parsers.normalize_chargers(
        {
            "data": [{"serial": "DATA-SN", "name": "Data Charger"}],
            "chargers": [{"serial": "TOP-SN", "name": "Top Charger"}],
        }
    )

    assert [(site.site_id, site.name) for site in sites] == [("123", "Legacy Name")]
    assert [(charger.serial, charger.name) for charger in chargers] == [
        ("DATA-SN", "Data Charger")
    ]


def test_latest_power_parser_result_to_dict() -> None:
    """Latest power parsing exposes a typed result before dict conversion."""

    sample = api_parsers.parse_latest_power_payload(
        {
            "data": {
                "latest_power": {
                    "value": "752.5",
                    "units": "W",
                    "precision": "1",
                    "time": "1773207600000",
                }
            }
        }
    )

    assert sample is not None
    assert sample.value == pytest.approx(752.5)
    assert sample.to_dict() == {
        "value": 752.5,
        "units": "W",
        "precision": 1,
        "time": 1_773_207_600,
    }


def test_client_non_boolean_number_wrapper_delegates_to_parser() -> None:
    """Compatibility wrapper keeps rejecting booleans while accepting numbers."""

    assert EnphaseEVClient._coerce_non_boolean_number("12.5") == pytest.approx(12.5)
    assert EnphaseEVClient._coerce_non_boolean_number(True) is None


def test_evse_parser_result_objects_to_existing_payload_shapes() -> None:
    """EVSE parsers use typed entries while preserving existing dict shapes."""

    daily = api_parsers.parse_evse_daily_entry(
        "EV123",
        {
            "days": [
                {"date": "2026-03-10", "energy_wh": 1200},
                {"date": "2026-03-11", "energy_kwh": "2.5"},
            ],
            "intervalMinutes": "1440",
        },
    )
    lifetime = api_parsers.parse_evse_lifetime_entry(
        "EV123",
        {"lifetime_energy_wh": 45600, "lastReportDate": "2026-03-11"},
    )

    assert daily is not None
    assert daily.to_dict() == {
        "serial": "EV123",
        "day_values_kwh": {
            "2026-03-10": 1.2,
            "2026-03-11": 2.5,
        },
        "energy_kwh": 2.5,
        "current_value_kwh": None,
        "interval_minutes": 1440.0,
    }
    assert lifetime is not None
    assert lifetime.to_dict() == {
        "serial": "EV123",
        "energy_kwh": 45.6,
        "last_report_date": "2026-03-11",
    }


def test_hems_parser_result_objects_to_existing_payload_shapes() -> None:
    """HEMS parsers use typed entries while preserving existing dict shapes."""

    state = api_parsers.parse_hems_heatpump_state_payload(
        {
            "type": "hems-heatpump-details",
            "timestamp": "2026-03-20T08:19:17Z",
            "data": {
                "device-uid": "HP-1",
                "heatpump-status": "RUNNING",
                "sg-ready-mode": "MODE_3",
            },
        }
    )
    entry = api_parsers.parse_hems_daily_consumption_entry(
        {
            "device-uid": "HP-1",
            "device-name": "Heat Pump",
            "consumption": [{"solar": "1.0", "grid": "2.5", "details": [3, "bad"]}],
        }
    )

    assert state is not None
    assert state.to_dict()["sg_ready_mode_label"] == "Recommended"
    assert entry is not None
    assert entry.to_dict() == {
        "device_uid": "HP-1",
        "device_name": "Heat Pump",
        "consumption": [
            {
                "solar": 1.0,
                "battery": None,
                "grid": 2.5,
                "details": [3.0, None],
            }
        ],
    }
