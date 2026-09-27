"""Inventory gating and device metadata contracts previously excluded from coverage."""

import pytest

from custom_components.enphase_ev.inventory_view import InventoryView


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        ("", None),
        ("aa:bb:cc:dd:ee:ff", "aa:bb:cc:dd:ee:ff"),
        ("A-B-C-D-E-F", "0a:0b:0c:0d:0e:0f"),
        ("aabb.ccdd.eeff", "aa:bb:cc:dd:ee:ff"),
        ("aabbccddeeff", "aa:bb:cc:dd:ee:ff"),
        ("aa:bb", None),
        ("xx:bb:cc:dd:ee:ff", None),
        ("aaa:bb:cc:dd:ee:ff", None),
        ("aabb.ccdd", None),
        ("xxxx.ccdd.eeff", None),
        ("abc.ccdd.eeff", None),
        ("other", None),
    ],
)
def test_inventory_mac_validation(value, expected):
    assert InventoryView._normalize_mac(value) == expected


@pytest.mark.parametrize(
    "kind",
    [
        "encharge",
        "microinverter",
        "iqevse",
        "generator",
        "heatpump",
        "envoy",
        "dry_contact",
        "other",
    ],
)
def test_inventory_device_metadata_tracks_real_members(coordinator_factory, kind):
    coord = coordinator_factory()
    coord._selected_type_keys = None
    coord._devices_inventory_ready = True
    coord._type_device_buckets = {
        kind: {
            "count": 2,
            "type_label": "Equipment",
            "devices": [
                {
                    "name": "Gateway Alpha" if kind == "envoy" else "Model A",
                    "serial_number": "A",
                    "model": "Model A",
                    "model_id": "SKU-A",
                    "sw_version": "1",
                    "hw_version": "H1",
                },
                {
                    "name": "Model B",
                    "serial_number": "B",
                    "model": "Model B",
                    "model_id": "SKU-B",
                    "sw_version": "2",
                    "hw_version": "H2",
                },
            ],
        }
    }
    view = InventoryView(coord)
    info = view.type_device_info(kind)
    assert info is not None
    assert info["identifiers"] == {("enphase_ev", f"type:{coord.site_id}:{kind}")}
    assert info["manufacturer"] == "Enphase"
    assert view.type_device_name(kind)
    assert view.type_device_model(kind)
    # Mixed members never invent a single physical serial or hardware version.
    if kind in {"encharge", "microinverter", "iqevse", "generator"}:
        assert view.type_device_serial_number(kind) is None
        assert view.type_device_hw_version(kind) is None
        assert view.type_device_model_id(kind) is None
        assert view.type_device_sw_version(kind) is None
    coord._selected_type_keys = {"unselected"}
    assert view.type_device_info(kind) is None


def test_inventory_startup_selection_and_confirmed_absence(coordinator_factory):
    coord = coordinator_factory(serials=[])
    view = InventoryView(coord)
    coord._selected_type_keys = {"encharge"}
    coord._devices_inventory_ready = False
    assert view.has_type_for_entities("encharge")
    assert not view.has_type_for_entities("envoy")
    coord._devices_inventory_ready = True
    coord._type_device_buckets = {}
    coord._battery_has_encharge = False
    assert not view.has_type_for_entities("encharge")
    coord._battery_has_encharge = True
    assert view.has_type_for_entities("encharge")
    assert view.type_identifier("encharge") == (
        "enphase_ev",
        f"type:{coord.site_id}:encharge",
    )
    coord._selected_type_keys = {"heatpump"}
    coord._heatpump_known_present = True
    assert view.has_type_for_entities("heatpump")


@pytest.mark.parametrize("key", [None, "", "  "])
def test_invalid_inventory_type_does_not_create_devices(coordinator_factory, key):
    view = InventoryView(coordinator_factory())
    for method in (
        view.type_bucket,
        view.type_label,
        view.type_identifier,
        view.type_device_name,
        view.type_device_model,
        view.type_device_serial_number,
        view.type_device_model_id,
        view.type_device_sw_version,
        view.type_device_sw_version_summary,
        view.type_device_hw_version,
        view.type_device_info,
    ):
        assert method(key) is None
    assert not view.has_type(key)
    assert not view.has_type_for_entities(key)


def test_inventory_malformed_bucket_retains_uncertainty(coordinator_factory):
    coord = coordinator_factory(serials=[])
    view = InventoryView(coord)
    coord._type_device_buckets = []
    assert not view.has_type("envoy")
    assert view.type_bucket("envoy") is None
    coord._type_device_buckets = {"envoy": {"count": "bad", "devices": "bad"}}
    assert not view.has_type("envoy")
    assert view.type_bucket("envoy")["devices"] == []
    assert view._type_bucket_members("missing") == []
    assert view.type_identifier("missing") is None


@pytest.mark.parametrize(
    "member,expected",
    [
        ({"channel_type": "systemcontroller"}, "controller"),
        ({"channel_type": "production"}, "production"),
        ({"channel_type": "consumption"}, "consumption"),
        ({"name": "Main Controller"}, "controller"),
        ({"name": "Production Meter"}, "production"),
        ({"name": "Consumption Meter"}, "consumption"),
        ({"name": "Unrelated"}, None),
    ],
)
def test_gateway_member_classification(member, expected):
    assert InventoryView._envoy_member_kind(member) == expected


def test_inventory_incomplete_heatpump_metadata(coordinator_factory, monkeypatch):
    from unittest.mock import MagicMock

    coord = coordinator_factory()
    view = InventoryView(coord)
    coord._devices_inventory_ready = True
    coord._type_device_buckets = {}
    monkeypatch.setattr(
        coord.heatpump_runtime,
        "heatpump_entities_established",
        MagicMock(side_effect=ValueError),
    )
    assert not view.has_type_for_entities("heatpump")
    assert view.type_device_name("unknown") is None
    monkeypatch.setattr(
        coord.heatpump_runtime, "_heatpump_primary_member", lambda: None
    )
    coord._type_device_buckets = {
        "heatpump": {
            "count": 1,
            "devices": [{"model": "Model A", "serial_number": "S", "hw_version": "H"}],
        }
    }
    assert view.type_device_model("heatpump") == "Model A x1"
    assert view.type_device_serial_number("heatpump") == "S"
    assert view.type_device_hw_version("heatpump") == "H"
    coord._type_device_buckets = {"heatpump": {"count": 1, "devices": [{}]}}
    assert view.type_device_model("heatpump") == "Heat Pump"
    assert view.parse_type_identifier(f"type:{coord.site_id}:heatpump") == (
        coord.site_id,
        "heatpump",
    )
    assert view.parse_type_identifier("invalid") is None


@pytest.mark.parametrize("name", ["IQ Battery 5P", None, ""])
def test_battery_friendly_model_preserves_sku(coordinator_factory, name):
    coord = coordinator_factory()
    sku = "B05-T02-ROW00-1-2"
    coord._type_device_buckets = {
        "encharge": {
            "type_label": "IQ Battery",
            "count": 2,
            "devices": [{"name": name, "sku_id": sku}] * 2,
        }
    }
    view = coord.inventory_view
    assert view.type_device_model("encharge") == (name or sku)
    assert view.type_device_model_id("encharge") == (sku if name else None)
    info = view.type_device_info("encharge")
    assert info["model"] == (name or sku)
    assert info.get("model_id") == (sku if name else None)


@pytest.mark.parametrize("kind", ["encharge", "microinverter"])
def test_grouped_cards_keep_models_firmware_and_serials_unambiguous(
    coordinator_factory, kind
):
    coord = coordinator_factory()
    coord._type_device_buckets = {
        kind: {
            "type_label": "Equipment",
            "count": 2,
            "devices": [
                {
                    "name": "Model A",
                    "sku_id": "SKU-A",
                    "serial_number": "A",
                    "sw_version": "520-long-release-1",
                },
                {
                    "name": "Model B",
                    "sku_id": "SKU-B",
                    "serial_number": "B",
                    "sw_version": "520-long-release-2",
                },
            ],
        }
    }
    info = coord.inventory_view.type_device_info(kind)
    assert info["model"] == "Model A x1, Model B x1"
    assert info["sw_version"] == "520-long-release-1 x1, 520-long-release-2 x1"
    assert "serial_number" not in info
    assert "model_id" not in info
    assert "hw_version" not in info
    assert (
        info["configuration_url"]
        == f"https://enlighten.enphaseenergy.com/app/system_dashboard/sites/{coord.site_id}/summary"
    )
    coord._type_device_buckets[kind]["devices"] = [
        {
            "name": "IQ7A",
            "sku_id": "IQ7A-72-E-ACM-INT",
            "serial_number": "A",
            "hw_version": "Rev A",
            "sw_version": "520-long-release-1",
        }
    ]
    info = coord.inventory_view.type_device_info(kind)
    assert info["model"] == "IQ7A"
    assert info["model_id"] == "IQ7A-72-E-ACM-INT"
    assert info["serial_number"] == "A"
    assert info["hw_version"] == "Rev A"
    assert info["sw_version"] == "520-long-release-1"


@pytest.mark.parametrize("kind", ["encharge", "microinverter"])
def test_partial_friendly_names_preserve_other_models(coordinator_factory, kind):
    coord = coordinator_factory()
    coord._type_device_buckets = {
        kind: {
            "type_label": "Equipment",
            "devices": [
                {"name": "Model A", "sku_id": "SKU-A"},
                {"name": " ", "sku_id": "SKU-B"},
                {"model": "Model C"},
            ],
        }
    }
    assert (
        coord.inventory_view.type_device_model(kind)
        == "Model A x1, Model C x1, SKU-B x1"
    )
    assert coord.inventory_view.type_device_model_id(kind) is None
