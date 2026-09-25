"""Gateway connection methods use reported interfaces, never IP-based guesses."""

from types import SimpleNamespace

import pytest

from custom_components.enphase_ev.sensor_gateway import (
    EnphaseGatewayConnectivityStatusSensor,
    _gateway_connection_method,
)


@pytest.mark.parametrize(
    ("details", "expected"),
    [
        ({"ethernet": True, "wifi": None, "cellular": False}, "Ethernet"),
        ({"wifi": True}, "Wi-Fi"),
        ({"cellular": True}, "Cellular"),
        ({"ethernet": True, "wifi": True}, "Ethernet, Wi-Fi"),
        ({"ethernet": False, "wifi": None}, None),
        ({"ethernet": "false", "wifi": "true", "cellular": 1}, None),
        ({"interface_ip": {"ethernet": "192.0.2.10"}}, None),
        ({}, None),
        (None, None),
        ("invalid", None),
    ],
)
def test_gateway_connection_method_attributes(coordinator_factory, details, expected):
    coord = coordinator_factory()
    coord.inventory_runtime._set_type_device_buckets(
        {
            "envoy": {
                "count": 1,
                "devices": [
                    {
                        "name": "IQ Gateway",
                        "ip": "192.0.2.10",
                        "connection_details": details,
                    }
                ],
            }
        },
        ["envoy"],
    )
    attrs = EnphaseGatewayConnectivityStatusSensor(coord).extra_state_attributes
    assert attrs["ip_address"] == "192.0.2.10"
    assert attrs["connection_method"] == expected


def test_gateway_connection_method_dashboard_fallback_and_gateway_selection():
    dashboard = {
        "name": "IQ Gateway",
        "ip": "192.0.2.10",
        "connection_details": {"wifi": True},
    }
    coord = SimpleNamespace(
        inventory_view=SimpleNamespace(
            type_bucket=lambda _: {
                "devices": [
                    None,
                    {
                        "name": "Production Meter",
                        "connection_details": {"cellular": True},
                    },
                    {
                        "name": "IQ Gateway",
                        "ip": "192.0.2.11",
                        "connection_details": {"ethernet": True},
                    },
                    {"name": "IQ Gateway", "ip": "192.0.2.10"},
                ]
            }
        ),
        system_dashboard_envoy_detail=lambda: dashboard,
    )
    assert _gateway_connection_method(coord, "192.0.2.10") == "Wi-Fi"
    assert _gateway_connection_method(coord, "192.0.2.12") is None
    dashboard["connection_details"] = {"ethernet": True}
    assert _gateway_connection_method(coord, "192.0.2.10") == "Ethernet"


@pytest.mark.parametrize("devices", [None, [], "invalid"])
def test_gateway_connection_method_without_ip_or_inventory(devices):
    coord = SimpleNamespace(
        inventory_view=SimpleNamespace(type_bucket=lambda _: {"devices": devices}),
        system_dashboard_envoy_detail=None,
    )
    assert _gateway_connection_method(coord, None) is None
    coord.system_dashboard_envoy_detail = lambda: {
        "name": "IQ Gateway",
        "connection_details": {"cellular": True},
    }
    assert _gateway_connection_method(coord, None) == "Cellular"


@pytest.mark.parametrize("details", [{"ethernet": True, "wifi": False}, None, "bad"])
def test_gateway_connection_method_uses_cloud_dashboard_payload(
    coordinator_factory, details
):
    coord = coordinator_factory()
    coord.inventory_runtime._set_type_device_buckets(
        {
            "envoy": {
                "count": 1,
                "devices": [{"name": "IQ Gateway", "ip": "192.0.2.10"}],
            }
        },
        ["envoy"],
    )
    coord._system_dashboard_devices_details_raw = {
        "envoy": {
            "envoys": [
                {
                    "name": "IQ Gateway",
                    "serial_number": "GW-1",
                    "ip": "192.0.2.10",
                    "connection_details": details,
                }
            ]
        }
    }
    coord._system_dashboard_devices_details_raw["envoy"] = {
        "envoys": coord._system_dashboard_devices_details_raw["envoy"]
    }
    sensor = EnphaseGatewayConnectivityStatusSensor(coord)
    attrs = sensor.extra_state_attributes
    assert attrs["ip_address"] == "192.0.2.10"
    assert attrs["connection_method"] == (
        "Ethernet" if isinstance(details, dict) else None
    )
    coord._system_dashboard_devices_details_raw = {
        "envoy": {
            "envoys": [
                {
                    "name": "IQ Gateway",
                    "ip": "192.0.2.10",
                    "connection_details": {
                        "wifi": True,
                        "interface_ip": {"wifi": "192.0.2.10"},
                    },
                }
            ]
        }
    }
    coord._system_dashboard_devices_details_raw["envoy"] = {
        "envoys": coord._system_dashboard_devices_details_raw["envoy"]
    }
    assert sensor.extra_state_attributes["connection_method"] == "Wi-Fi"
    assert coord.system_dashboard_envoy_detail()["connection_details"] == {"wifi": True}


def test_gateway_connection_method_matches_nonfirst_dashboard_gateway(
    coordinator_factory,
):
    coord = coordinator_factory()
    coord.inventory_runtime._set_type_device_buckets(
        {
            "envoy": {
                "count": 1,
                "devices": [
                    {"name": "IQ Gateway", "serial_number": "GW-2", "ip": "192.0.2.20"}
                ],
            }
        },
        ["envoy"],
    )
    coord._system_dashboard_devices_details_raw = {
        "envoy": {
            "envoys": {
                "envoys": [
                    {
                        "name": "IQ Gateway",
                        "serial_number": "GW-1",
                        "ip": "192.0.2.10",
                        "connection_details": {"ethernet": True},
                    },
                    {
                        "name": "IQ Gateway",
                        "serial_number": "GW-2",
                        "ip": "192.0.2.20",
                        "connection_details": {"wifi": True},
                    },
                ]
            }
        }
    }
    attrs = EnphaseGatewayConnectivityStatusSensor(coord).extra_state_attributes
    assert attrs["ip_address"] == "192.0.2.20"
    assert attrs["connection_method"] == "Wi-Fi"


@pytest.mark.asyncio
async def test_gateway_transport_only_change_notifies_entities(coordinator_factory):
    coord = coordinator_factory()
    coord.update_interval = None
    coord.always_update = False
    coord.inventory_runtime._set_type_device_buckets(
        {
            "envoy": {
                "count": 1,
                "devices": [{"name": "IQ Gateway", "ip": "192.0.2.10"}],
            }
        },
        ["envoy"],
    )
    sensor = EnphaseGatewayConnectivityStatusSensor(coord)
    observed = []
    remove = coord.async_add_listener(
        lambda: observed.append(sensor.extra_state_attributes["connection_method"])
    )
    try:
        for method in ("ethernet", "wifi"):
            coord._system_dashboard_devices_details_raw = {
                "envoy": {
                    "envoys": {
                        "envoys": [
                            {
                                "name": "IQ Gateway",
                                "ip": "192.0.2.10",
                                "connection_details": {method: True},
                            }
                        ]
                    }
                }
            }
            coord.inventory_runtime._rebuild_inventory_summary_caches()
            coord.async_set_updated_data(dict(coord.data or {}))
        assert observed == ["Ethernet", "Wi-Fi"]
    finally:
        remove()


@pytest.mark.parametrize("other_ip", ["192.0.2.30", "192.0.2.10"])
def test_gateway_connection_method_tracks_identity_after_ip_change(
    coordinator_factory, other_ip
):
    coord = coordinator_factory()
    coord.inventory_runtime._set_type_device_buckets(
        {
            "envoy": {
                "count": 1,
                "devices": [
                    {"name": "IQ Gateway", "serial_number": "GW-1", "ip": "192.0.2.10"}
                ],
            }
        },
        ["envoy"],
    )
    # Inventory retains the Ethernet address while fresher dashboard data reports Wi-Fi.
    coord._system_dashboard_devices_details_raw = {
        "envoy": {
            "envoys": {
                "envoys": [
                    {
                        "name": "IQ Gateway",
                        "serial_number": "GW-2",
                        "ip": other_ip,
                        "connection_details": {"ethernet": True},
                    },
                    {
                        "name": "IQ Gateway",
                        "serial_number": "GW-1",
                        "ip": "192.0.2.20",
                        "connection_details": {"wifi": True},
                    },
                ]
            }
        }
    }
    attrs = EnphaseGatewayConnectivityStatusSensor(coord).extra_state_attributes
    assert attrs["ip_address"] == "192.0.2.10"
    assert attrs["connection_method"] == "Wi-Fi"


@pytest.mark.asyncio
async def test_today_connectivity_refresh_and_publication(coordinator_factory):
    from unittest.mock import AsyncMock

    coord = coordinator_factory()
    coord.update_interval = None
    coord.always_update = False
    runtime = coord.inventory_runtime
    runtime._set_type_device_buckets(
        {
            "envoy": {
                "count": 1,
                "devices": [
                    {"name": "IQ Gateway", "serial_number": "GW-1", "ip": "192.0.2.10"}
                ],
            }
        },
        ["envoy"],
    )
    coord.client.devices_tree = AsyncMock(return_value={})
    coord.client.devices_details = AsyncMock(return_value={})
    coord.client.pv_system_today = AsyncMock()
    sensor = EnphaseGatewayConnectivityStatusSensor(coord)
    observed = []
    remove = coord.async_add_listener(
        lambda: observed.append(sensor.extra_state_attributes["connection_method"])
    )
    try:
        for transport in ("ethernet", "wifi"):
            coord.client.pv_system_today.return_value = {
                "connectionDetails": [
                    {"serial_num": "GW-other", "cellular": True},
                    {
                        "serial_num": "GW-1",
                        transport: True,
                        "interface_ip": {transport: "192.0.2.99"},
                    },
                ],
                "system": {"connection_type": {"key": "cellular", "name": "Cellular"}},
            }
            await runtime._async_refresh_system_dashboard(force=True)
            coord.async_set_updated_data(dict(coord.data or {}))
        assert observed == ["Ethernet", "Wi-Fi"]
        assert coord.inventory_state._gateway_today_connections["GW-1"] == {
            "wifi": True
        }
        coord.client.pv_system_today.side_effect = RuntimeError("private payload")
        await runtime._async_refresh_system_dashboard(force=True)
        assert sensor.extra_state_attributes["connection_method"] == "Wi-Fi"
        assert (
            coord._system_dashboard_detail_failures["today_connectivity"]
            == "RuntimeError"
        )
        coord.client.pv_system_today.side_effect = None
        coord.client.pv_system_today.return_value = None
        await runtime._async_refresh_system_dashboard(force=True)
        assert sensor.extra_state_attributes["connection_method"] == "Wi-Fi"
        coord.client.pv_system_today.return_value = {"connectionDetails": []}
        await runtime._async_refresh_system_dashboard(force=True)
        assert sensor.extra_state_attributes["connection_method"] is None
    finally:
        remove()


@pytest.mark.parametrize(
    "records,expected",
    [
        (None, {}),
        ([None, {}, {"serial_num": 123}, {"serial_num": " "}], {}),
        (
            [
                {
                    "serial_num": " GW-1 ",
                    "wifi": "true",
                    "ethernet": True,
                    "cellular": False,
                }
            ],
            {"GW-1": {"ethernet": True, "cellular": False}},
        ),
    ],
)
def test_today_connections_normalize(coordinator_factory, records, expected):
    assert (
        coordinator_factory().inventory_runtime._normalize_today_connections(
            {"connectionDetails": records}
        )
        == expected
    )


@pytest.mark.parametrize(
    "flags,expected",
    [({"ethernet": False}, None), ({"wifi": True}, "Wi-Fi"), ({}, "Ethernet")],
)
def test_today_flags_override_dashboard(coordinator_factory, flags, expected):
    coord = coordinator_factory()
    coord.inventory_runtime._set_type_device_buckets(
        {
            "envoy": {
                "count": 1,
                "devices": [
                    {
                        "name": "IQ Gateway",
                        "serial_number": "GW-1",
                        "ip": "192.0.2.10",
                        "connection_details": {"ethernet": True},
                    }
                ],
            }
        },
        ["envoy"],
    )
    coord.inventory_state._gateway_today_connections = {"GW-1": flags}
    assert (
        EnphaseGatewayConnectivityStatusSensor(coord).extra_state_attributes[
            "connection_method"
        ]
        == expected
    )
