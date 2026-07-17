from __future__ import annotations

from types import SimpleNamespace
from datetime import datetime, timezone

from custom_components.enphase_ev.binary_sensor import SiteCloudReachableBinarySensor
from custom_components.enphase_ev.button import (
    CancelPendingProfileChangeButton,
)
from custom_components.enphase_ev.entity import EnphaseBaseEntity
from custom_components.enphase_ev.battery_schedule_editor import (
    BatteryScheduleEditorManager,
)
from custom_components.enphase_ev.number import (
    BatteryReserveNumber,
    BatteryShutdownLevelNumber,
)
from custom_components.enphase_ev.select import SystemProfileSelect
from custom_components.enphase_ev.sensor import (
    EnphaseCloudLatencySensor,
    EnphaseGridModeSensor,
    EnphaseSiteLastUpdateSensor,
    EnphaseSystemProfileStatusSensor,
    EnphaseTypeInventorySensor,
)
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from custom_components.enphase_ev.switch import (
    ChargeFromGridScheduleSwitch,
    ChargeFromGridSwitch,
    SavingsUseBatteryAfterPeakSwitch,
    StormGuardSwitch,
)
from custom_components.enphase_ev.time import BatteryScheduleEditStartTimeEntity


def test_site_binary_sensor_not_gated_by_envoy_type() -> None:
    coord = SimpleNamespace(
        site_id="12345",
        has_type=lambda _key: False,
        last_success_utc=None,
        last_update_success=True,
        last_failure_utc=None,
        last_failure_status=None,
        last_failure_description=None,
        last_failure_response=None,
        last_failure_source=None,
        backoff_ends_utc=None,
        update_interval=None,
    )
    entity = SiteCloudReachableBinarySensor(coord)
    assert entity.available is True


def test_site_binary_sensor_device_info_falls_back_without_type_info() -> None:
    coord = SimpleNamespace(
        site_id="12345",
        last_success_utc=None,
        last_update_success=True,
        last_failure_utc=None,
        last_failure_status=None,
        last_failure_description=None,
        last_failure_response=None,
        last_failure_source=None,
        backoff_ends_utc=None,
        update_interval=None,
    )
    info = SiteCloudReachableBinarySensor(coord).device_info
    assert info["identifiers"] == {("enphase_ev", "type:12345:cloud")}


def test_site_device_info_fallbacks_without_type_device_info_provider() -> None:
    coord = SimpleNamespace(
        site_id="site-1",
        last_update_success=True,
        battery_profile_pending=True,
        battery_reserve_editable=True,
        battery_reserve_min=10,
        battery_reserve_max=100,
        battery_selected_backup_percentage=25,
        battery_shutdown_level_available=True,
        battery_shutdown_level=15,
        battery_shutdown_level_min=10,
        battery_shutdown_level_max=20,
        battery_controls_available=True,
        battery_profile_option_labels={"self-consumption": "Self-Consumption"},
        battery_profile_option_keys=["self-consumption"],
        battery_selected_profile="self-consumption",
        savings_use_battery_switch_available=True,
        savings_use_battery_after_peak=True,
        charge_from_grid_control_available=True,
        battery_charge_from_grid_enabled=True,
        charge_from_grid_schedule_available=True,
        battery_charge_from_grid_schedule_enabled=True,
        storm_guard_state="enabled",
        storm_evse_enabled=True,
    )

    assert CancelPendingProfileChangeButton(coord).device_info["identifiers"] == {
        ("enphase_ev", "type:site-1:envoy")
    }
    assert BatteryReserveNumber(coord).device_info["identifiers"] == {
        ("enphase_ev", "type:site-1:encharge")
    }
    assert BatteryShutdownLevelNumber(coord).device_info["identifiers"] == {
        ("enphase_ev", "type:site-1:encharge")
    }
    assert SystemProfileSelect(coord).device_info["identifiers"] == {
        ("enphase_ev", "type:site-1:envoy")
    }
    assert SavingsUseBatteryAfterPeakSwitch(coord).device_info["identifiers"] == {
        ("enphase_ev", "type:site-1:encharge")
    }
    assert ChargeFromGridSwitch(coord).device_info["identifiers"] == {
        ("enphase_ev", "type:site-1:encharge")
    }
    assert ChargeFromGridScheduleSwitch(coord).device_info["identifiers"] == {
        ("enphase_ev", "type:site-1:encharge")
    }
    assert StormGuardSwitch(coord).device_info["identifiers"] == {
        ("enphase_ev", "type:site-1:envoy")
    }
    entry = SimpleNamespace(
        entry_id="entry-1",
        options={"battery_schedules_enabled": True},
        runtime_data=EnphaseRuntimeData(
            coordinator=coord,
            battery_schedule_editor=BatteryScheduleEditorManager(coord),
        ),
    )
    assert BatteryScheduleEditStartTimeEntity(coord, entry).device_info[
        "identifiers"
    ] == {("enphase_ev", "type:site-1:encharge")}
    assert EnphaseGridModeSensor(coord).device_info["identifiers"] == {
        ("enphase_ev", "type:site-1:envoy")
    }


def test_system_profile_select_and_storm_guard_availability_type_checks() -> None:
    coord = SimpleNamespace(
        site_id="site-2",
        last_update_success=True,
        has_type=lambda _key: False,
        battery_user_is_owner=True,
        battery_user_is_installer=False,
        battery_controls_available=True,
        battery_profile_option_labels={"self-consumption": "Self-Consumption"},
        battery_profile_option_keys=["self-consumption"],
        battery_selected_profile="self-consumption",
        storm_guard_state="enabled",
        storm_evse_enabled=True,
    )
    assert SystemProfileSelect(coord).available is False
    assert StormGuardSwitch(coord).available is False


def test_site_entities_fall_back_to_role_checks_without_property_or_type_helpers() -> (
    None
):
    coord = SimpleNamespace(
        site_id="site-3",
        last_update_success=True,
        battery_user_is_owner=True,
        battery_user_is_installer=False,
        battery_reserve_editable=True,
        battery_reserve_min=10,
        battery_reserve_max=100,
        battery_selected_backup_percentage=25,
        battery_controls_available=True,
        battery_profile_option_labels={"self-consumption": "Self-Consumption"},
        battery_profile_option_keys=["self-consumption"],
        battery_selected_profile="self-consumption",
        storm_guard_state="enabled",
        storm_evse_enabled=True,
    )

    assert BatteryReserveNumber(coord).available is True
    assert SystemProfileSelect(coord).available is True
    assert StormGuardSwitch(coord).available is True


def test_system_profile_select_fallback_unavailable_without_battery_controls() -> None:
    coord = SimpleNamespace(
        site_id="site-4",
        last_update_success=True,
        battery_user_is_owner=True,
        battery_user_is_installer=False,
        battery_controls_available=False,
        battery_profile_option_labels={"self-consumption": "Self-Consumption"},
        battery_profile_option_keys=["self-consumption"],
        battery_selected_profile="self-consumption",
    )

    assert SystemProfileSelect(coord).available is False


def test_system_profile_select_fallback_unavailable_for_read_only_user() -> None:
    coord = SimpleNamespace(
        site_id="site-5",
        last_update_success=True,
        battery_user_is_owner=False,
        battery_user_is_installer=False,
        battery_controls_available=True,
        battery_profile_option_labels={"self-consumption": "Self-Consumption"},
        battery_profile_option_keys=["self-consumption"],
        battery_selected_profile="self-consumption",
    )

    assert SystemProfileSelect(coord).available is False


def test_base_entity_device_info_does_not_set_via_device_for_chargers() -> None:
    class DummyEntity(EnphaseBaseEntity):
        pass

    coord = SimpleNamespace(
        data={
            "SN1": {
                "display_name": "Garage Charger",
                "model_name": "IQ EVSE",
            }
        },
        site_id="site-1",
        type_identifier=lambda _key: ("enphase_ev", "type:site-1:iqevse"),
    )
    entity = DummyEntity(coord, "SN1")
    info = entity.device_info
    assert "via_device" not in info


def test_type_device_entities_use_provided_type_device_info() -> None:
    provided = {"identifiers": {("enphase_ev", "type:site-x:envoy")}}
    coord = SimpleNamespace(
        site_id="site-x",
        last_update_success=True,
        has_type=lambda _key: True,
        inventory_view=SimpleNamespace(
            has_type=lambda _key: True,
            has_type_for_entities=lambda _key: True,
            type_label=lambda _key: "Gateway",
            type_bucket=lambda _key: {"count": "bad", "devices": "bad"},
            type_device_info=lambda _key: provided,
        ),
        last_success_utc=None,
        last_failure_utc=None,
        last_failure_status=None,
        last_failure_description=None,
        last_failure_response=None,
        last_failure_source=None,
        backoff_ends_utc=None,
        latency_ms=None,
        battery_controls_available=False,
        battery_profile=None,
    )

    assert SiteCloudReachableBinarySensor(coord).device_info is provided
    assert CancelPendingProfileChangeButton(coord).device_info is provided
    assert BatteryReserveNumber(coord).device_info is provided
    assert BatteryShutdownLevelNumber(coord).device_info is provided
    assert SystemProfileSelect(coord).device_info is provided
    assert SavingsUseBatteryAfterPeakSwitch(coord).device_info is provided
    assert ChargeFromGridSwitch(coord).device_info is provided
    assert ChargeFromGridScheduleSwitch(coord).device_info is provided
    assert StormGuardSwitch(coord).device_info is provided
    entry = SimpleNamespace(
        entry_id="entry-2",
        options={"battery_schedules_enabled": True},
        runtime_data=EnphaseRuntimeData(
            coordinator=coord,
            battery_schedule_editor=BatteryScheduleEditorManager(coord),
        ),
    )
    assert BatteryScheduleEditStartTimeEntity(coord, entry).device_info is provided
    assert EnphaseGridModeSensor(coord).device_info is provided
    assert EnphaseTypeInventorySensor(coord, "envoy").device_info is provided
    assert EnphaseSiteLastUpdateSensor(coord).device_info is provided
    assert EnphaseCloudLatencySensor(coord).device_info is provided

    # Cover error/fallback handling in inventory/status sensors.
    inventory = EnphaseTypeInventorySensor(coord, "envoy")
    assert inventory.native_value == 0
    assert inventory.extra_state_attributes["devices"] == []
    assert inventory.available is True

    system_profile = EnphaseSystemProfileStatusSensor(coord)
    assert system_profile.available is False
    assert EnphaseSiteLastUpdateSensor(coord).extra_state_attributes == {}


def test_site_and_type_inventory_device_info_fallback_identifiers() -> None:
    coord = SimpleNamespace(
        site_id="site-fallback",
        last_update_success=True,
        has_type=lambda _key: True,
        inventory_view=SimpleNamespace(
            has_type=lambda _key: True,
            has_type_for_entities=lambda _key: True,
            type_device_info=lambda _key: None,
            type_label=lambda _key: "Gateway",
            type_bucket=lambda _key: {"count": 1, "devices": []},
        ),
        last_success_utc=None,
        last_failure_utc=None,
        last_failure_status=None,
        last_failure_description=None,
        last_failure_response=None,
        last_failure_source=None,
        backoff_ends_utc=None,
        latency_ms=None,
    )
    assert EnphaseTypeInventorySensor(coord, "envoy").device_info["identifiers"] == {
        ("enphase_ev", "type:site-fallback:envoy")
    }
    assert EnphaseSiteLastUpdateSensor(coord).device_info["identifiers"] == {
        ("enphase_ev", "type:site-fallback:cloud")
    }
    assert EnphaseCloudLatencySensor(coord).device_info["identifiers"] == {
        ("enphase_ev", "type:site-fallback:cloud")
    }


def test_site_sensor_attributes_and_latency_attrs_paths() -> None:
    coord = SimpleNamespace(
        site_id="site-attrs",
        last_update_success=True,
        has_type=lambda _key: True,
        inventory_view=SimpleNamespace(
            has_type=lambda _key: True,
            has_type_for_entities=lambda _key: True,
            type_label=lambda _key: "Gateway",
            type_bucket=lambda _key: {"count": 1, "devices": []},
        ),
        last_success_utc=None,
        last_failure_utc=None,
        last_failure_status=None,
        last_failure_description=None,
        last_failure_response=None,
        last_failure_source=None,
        backoff_ends_utc=None,
        latency_ms=10,
    )
    assert EnphaseSiteLastUpdateSensor(coord).extra_state_attributes == {}
    assert EnphaseCloudLatencySensor(coord).extra_state_attributes == {}


def test_cloud_site_sensor_last_success_attrs_not_type_gated() -> None:
    coord = SimpleNamespace(
        site_id="site-gate",
        last_update_success=True,
        has_type=lambda _key: False,
        inventory_view=SimpleNamespace(
            has_type=lambda _key: False,
            has_type_for_entities=lambda _key: False,
            type_label=lambda _key: "Gateway",
            type_bucket=lambda _key: {"count": 1, "devices": []},
            type_device_info=lambda _key: None,
        ),
        last_success_utc=datetime.now(timezone.utc),
        last_failure_utc=None,
        last_failure_status=None,
        last_failure_description=None,
        last_failure_response=None,
        last_failure_source=None,
        backoff_ends_utc=None,
        latency_ms=None,
    )
    sensor = EnphaseSiteLastUpdateSensor(coord)
    assert sensor.available is True
    attrs = sensor.extra_state_attributes
    assert "last_success_utc" not in attrs
