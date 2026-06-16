from __future__ import annotations

import ast
import json
import pathlib
import re


def test_clear_reauth_issue_device_field_translated() -> None:
    """Ensure device selector metadata is translated for all locales."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    for lang in ("en", "fr"):
        data = json.loads((translations_dir / f"{lang}.json").read_text())
        fields = data["services"]["clear_reauth_issue"]["fields"]
        assert "device_id" in fields, f"{lang} missing device_id translation"
        entry = fields["device_id"]
        assert entry.get("name"), f"{lang} device_id name empty"
        assert entry.get("description"), f"{lang} device_id description empty"


def test_try_reauth_now_strings_exist_for_all_locales() -> None:
    """Ensure manual reauth service and repair text are translated."""

    root = (
        pathlib.Path(__file__).resolve().parents[3] / "custom_components" / "enphase_ev"
    )
    translations_dir = root / "translations"
    paths = [
        "issues.auth_blocked.description",
        "issues.hems_auth_degraded.title",
        "issues.hems_auth_degraded.description",
        "issues.too_many_active_sessions.title",
        "issues.too_many_active_sessions.description",
        "config.error.too_many_active_sessions",
        "services.try_reauth_now.name",
        "services.try_reauth_now.description",
        "services.try_reauth_now.fields.device_id.name",
        "services.try_reauth_now.fields.device_id.description",
        "services.try_reauth_now.fields.site_id.name",
        "services.try_reauth_now.fields.site_id.description",
        "services.clear_hems_auth_backoff.name",
        "services.clear_hems_auth_backoff.description",
        "services.clear_hems_auth_backoff.fields.device_id.name",
        "services.clear_hems_auth_backoff.fields.device_id.description",
        "services.clear_hems_auth_backoff.fields.site_id.name",
        "services.clear_hems_auth_backoff.fields.site_id.description",
    ]
    strings_data = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    for path in paths:
        value = _at_path(strings_data, path)
        assert value.strip(), f"strings.json missing value for {path}"
    en_data = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    assert _at_path(strings_data, "services.trigger_message.name")
    try:
        _at_path(strings_data, "services.trigger_message.response.fields.success.name")
    except KeyError:
        pass
    else:
        raise AssertionError(
            "strings.json should not define manual reauth response fields under trigger_message"
        )
    try:
        _at_path(strings_data, "services.try_reauth_now.response.fields.success.name")
    except KeyError:
        pass
    else:
        raise AssertionError(
            "strings.json should not define unsupported response fields under try_reauth_now"
        )
    try:
        _at_path(
            strings_data,
            "services.clear_hems_auth_backoff.response.fields.success.name",
        )
    except KeyError:
        pass
    else:
        raise AssertionError(
            "strings.json should not define unsupported response fields under "
            "clear_hems_auth_backoff"
        )
    for locale in translations_dir.glob("*.json"):
        name = locale.name
        data = json.loads(locale.read_text(encoding="utf-8"))
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{name} missing value for {path}"
            if name != "en.json" and not name.startswith("en-"):
                assert value != _at_path(
                    en_data, path
                ), f"{name} should localize {path} (still matches English)"
        try:
            _at_path(
                data,
                "services.clear_hems_auth_backoff.response.fields.success.name",
            )
        except KeyError:
            pass
        else:
            raise AssertionError(
                f"{name} should not define unsupported response fields under "
                "clear_hems_auth_backoff"
            )
        issue = _at_path(data, "issues.auth_blocked.description")
        assert "{site_id}" in issue, f"{name} missing {{site_id}} placeholder"
        assert (
            "{blocked_until}" in issue
        ), f"{name} missing {{blocked_until}} placeholder"
        sessions_issue = _at_path(data, "issues.too_many_active_sessions.description")
        assert "{site_id}" in sessions_issue, f"{name} missing {{site_id}} placeholder"
        assert (
            "{blocked_until}" in sessions_issue
        ), f"{name} missing {{blocked_until}} placeholder"
        hems_issue = _at_path(data, "issues.hems_auth_degraded.description")
        for placeholder in ("site_id", "backoff_until", "failure_count", "reason"):
            assert (
                "{" + placeholder + "}" in hems_issue
            ), f"{name} missing {{{placeholder}}} placeholder"


def test_cloud_error_code_states_exist_for_all_locales() -> None:
    """Ensure cloud diagnostic error states can be translated in the UI."""

    from custom_components.enphase_ev.sensor import CLOUD_ERROR_CODE_STATES

    root = (
        pathlib.Path(__file__).resolve().parents[3] / "custom_components" / "enphase_ev"
    )
    translations_dir = root / "translations"
    paths = [
        "entity.sensor.cloud_error_code.state.none",
        "entity.sensor.cloud_error_code.state.rate_limited",
        "entity.sensor.cloud_error_code.state.auth_blocked",
        "entity.sensor.cloud_error_code.state.authentication_error",
        "entity.sensor.cloud_error_code.state.request_error",
        "entity.sensor.cloud_error_code.state.service_unavailable",
        "entity.sensor.cloud_error_code.state.invalid_payload",
        "entity.sensor.cloud_error_code.state.dns_error",
        "entity.sensor.cloud_error_code.state.network_error",
    ]
    strings_data = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    en_data = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    assert set(strings_data["entity"]["sensor"]["cloud_error_code"]["state"]) == set(
        CLOUD_ERROR_CODE_STATES
    )
    for path in paths:
        value = _at_path(strings_data, path)
        assert value.strip(), f"strings.json missing value for {path}"
    rate_limited_issue = _at_path(strings_data, "issues.rate_limited.description")
    assert "{backoff_ends}" in rate_limited_issue
    for locale in translations_dir.glob("*.json"):
        name = locale.name
        data = json.loads(locale.read_text(encoding="utf-8"))
        assert set(data["entity"]["sensor"]["cloud_error_code"]["state"]) == set(
            CLOUD_ERROR_CODE_STATES
        ), f"{name} cloud error states differ from sensor options"
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{name} missing value for {path}"
            if name != "en.json" and not name.startswith("en-"):
                assert value != _at_path(
                    en_data, path
                ), f"{name} should localize {path} (still matches English)"
        rate_limited_issue = _at_path(data, "issues.rate_limited.description")
        assert (
            "{backoff_ends}" in rate_limited_issue
        ), f"{name} missing {{backoff_ends}} placeholder"
        if name != "en.json" and not name.startswith("en-"):
            assert rate_limited_issue != _at_path(
                en_data, "issues.rate_limited.description"
            ), f"{name} should localize issues.rate_limited.description"
            assert _at_path(data, "issues.rate_limited.title") != _at_path(
                en_data, "issues.rate_limited.title"
            ), f"{name} should localize issues.rate_limited.title"


def test_site_service_status_states_exist_for_all_locales() -> None:
    """Ensure the site service-status diagnostic sensor can be translated."""

    from custom_components.enphase_ev.sensor import SITE_SERVICE_STATUS_STATES

    root = (
        pathlib.Path(__file__).resolve().parents[3] / "custom_components" / "enphase_ev"
    )
    translations_dir = root / "translations"
    paths = [
        "entity.sensor.site_service_status.name",
        "entity.sensor.site_service_status.state.ok",
        "entity.sensor.site_service_status.state.degraded",
        "entity.sensor.site_service_status.state.unknown",
        "entity.sensor.site_service_status.state_attributes.degraded_services.name",
        "entity.sensor.site_service_status.state_attributes.degraded_endpoint_families.name",
        "entity.sensor.site_service_status.state_attributes.degraded_service_count.name",
        "entity.sensor.site_service_status.state_attributes.degraded_endpoint_family_count.name",
        "entity.sensor.site_service_status.state_attributes.metrics_available.name",
    ]
    strings_data = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    en_data = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    assert set(strings_data["entity"]["sensor"]["site_service_status"]["state"]) == set(
        SITE_SERVICE_STATUS_STATES
    )
    for path in paths:
        value = _at_path(strings_data, path)
        assert value.strip(), f"strings.json missing value for {path}"
    for locale in translations_dir.glob("*.json"):
        name = locale.name
        data = json.loads(locale.read_text(encoding="utf-8"))
        assert set(data["entity"]["sensor"]["site_service_status"]["state"]) == set(
            SITE_SERVICE_STATUS_STATES
        ), f"{name} site service-status states differ from sensor options"
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{name} missing value for {path}"
            if name != "en.json" and not name.startswith("en-"):
                assert value != _at_path(
                    en_data, path
                ), f"{name} should localize {path} (still matches English)"


def _at_path(data: dict, path: str) -> str:
    cur = data
    for part in path.split("."):
        cur = cur[part]
    assert isinstance(cur, str)
    return cur


def _string_paths_under(data: dict, path: str) -> list[str]:
    """Return every string leaf path beneath the given translation subtree."""

    cur = data
    for part in path.split("."):
        cur = cur[part]

    def _walk(node: object, prefix: str) -> list[str]:
        if isinstance(node, dict):
            paths: list[str] = []
            for key, value in node.items():
                child = f"{prefix}.{key}" if prefix else key
                paths.extend(_walk(value, child))
            return paths
        if isinstance(node, str):
            return [prefix]
        return []

    return _walk(cur, path)


def _battery_schedule_string_paths(data: dict) -> list[str]:
    """Return the full battery-scheduler translation surface from the catalog."""

    paths = [
        "options.step.init.data.schedule_sync_enabled",
        "options.step.init.data.battery_schedules_enabled",
        "options.step.init.data_description.schedule_sync_enabled",
        "options.step.init.data_description.battery_schedules_enabled",
        "options.step.settings.data.battery_schedules_enabled",
        "options.step.settings.data_description.battery_schedules_enabled",
    ]

    scheduler_entity_prefixes = (
        "battery_new_schedule_",
        "battery_schedule_",
        "battery_cfg_schedules",
        "battery_dtg_schedules",
        "battery_rbd_schedules",
    )
    for platform, platform_entries in data["entity"].items():
        if not isinstance(platform_entries, dict):
            continue
        for entity_id in platform_entries:
            if entity_id.startswith(scheduler_entity_prefixes):
                paths.extend(
                    _string_paths_under(data, f"entity.{platform}.{entity_id}")
                )

    for exception_key in data["exceptions"]:
        if exception_key.startswith("battery_schedule_") or exception_key in {
            "scheduler_service_unavailable",
            "schedule_update_conflict_detail",
        }:
            paths.extend(_string_paths_under(data, f"exceptions.{exception_key}"))

    for service_key in (
        "force_refresh",
        "add_schedule",
        "update_schedule",
        "delete_schedule",
        "validate_schedule",
    ):
        paths.extend(_string_paths_under(data, f"services.{service_key}"))

    return sorted(set(paths))


def test_battery_profile_strings_localized_for_non_english_locales() -> None:
    """Guard against English fallback regressions for battery profile features."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    en_data = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    paths = [
        "entity.select.system_profile.name",
        "entity.number.battery_reserve.name",
        "entity.switch.savings_use_battery_after_peak.name",
        "entity.sensor.system_profile_status.name",
        "entity.sensor.system_profile_status.state.pending",
        "entity.button.cancel_pending_profile_change.name",
        "entity.button.storm_alert_opt_out.name",
        "issues.battery_profile_pending.title",
        "issues.battery_profile_pending.description",
    ]
    for locale in translations_dir.glob("*.json"):
        name = locale.name
        if name == "en.json" or name.startswith("en-"):
            continue
        data = json.loads(locale.read_text(encoding="utf-8"))
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{name} missing value for {path}"
            assert value != _at_path(
                en_data, path
            ), f"{name} should localize {path} (still matches English)"
        desc = _at_path(data, "issues.battery_profile_pending.description")
        assert "{site_id}" in desc, f"{name} missing {{site_id}} placeholder"
        assert (
            "{pending_timeout_minutes}" in desc
        ), f"{name} missing {{pending_timeout_minutes}} placeholder"


def test_shared_label_translations_exist_for_all_locales() -> None:
    """Ensure label catalogs backing translated runtime options exist everywhere."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    en_data = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    paths = [
        "entity.sensor.shared_labels.state.self_consumption",
        "entity.sensor.shared_labels.state.cost_savings",
        "entity.sensor.shared_labels.state.ai_optimisation",
        "entity.sensor.shared_labels.state.backup_only",
        "entity.sensor.shared_labels.state.importexport",
        "entity.sensor.shared_labels.state.importonly",
        "entity.sensor.shared_labels.state.exportonly",
        "entity.sensor.shared_labels.state.manual_charging",
        "entity.sensor.shared_labels.state.scheduled_charging",
        "entity.sensor.shared_labels.state.green_charging",
        "entity.sensor.shared_labels.state.smart_charging",
        "entity.sensor.shared_labels.state.online",
        "entity.sensor.shared_labels.state.offline",
        "entity.sensor.shared_labels.state.degraded",
        "entity.sensor.shared_labels.state.not_reporting",
        "entity.sensor.shared_labels.state.inactive",
    ]
    localized_paths = [
        "entity.sensor.shared_labels.state.self_consumption",
        "entity.sensor.shared_labels.state.cost_savings",
        "entity.sensor.shared_labels.state.ai_optimisation",
        "entity.sensor.shared_labels.state.backup_only",
        "entity.sensor.shared_labels.state.importexport",
        "entity.sensor.shared_labels.state.importonly",
        "entity.sensor.shared_labels.state.exportonly",
        "entity.sensor.shared_labels.state.green_charging",
        "entity.sensor.shared_labels.state.not_reporting",
    ]
    for locale in translations_dir.glob("*.json"):
        name = locale.name
        data = json.loads(locale.read_text(encoding="utf-8"))
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{name} missing value for {path}"
            if (
                name != "en.json"
                and not name.startswith("en-")
                and path in localized_paths
            ):
                assert value != _at_path(
                    en_data, path
                ), f"{name} should localize {path} (still matches English)"


def test_charge_mode_attribute_labels_exist_for_all_locales() -> None:
    """Ensure charge-mode helper attributes are labeled in every locale."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    paths = [
        "entity.sensor.charge_mode.state_attributes.amp_control_applicable.name",
        "entity.sensor.charge_mode.state_attributes.amp_control_managed_by_mode.name",
        "entity.sensor.charge_mode.state_attributes.amp_control_applies_in_modes.name",
    ]
    for locale in translations_dir.glob("*.json"):
        data = json.loads(locale.read_text(encoding="utf-8"))
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{locale.name} missing value for {path}"


def test_battery_settings_entity_strings_exist_for_all_locales() -> None:
    """Ensure newly added battery settings entity labels exist in every locale."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    paths = [
        "entity.sensor.battery_mode.name",
        "entity.sensor.battery_cfg_schedule_status.name",
        "entity.sensor.battery_cfg_schedule_status.state.none",
        "entity.sensor.battery_cfg_schedule_status.state.pending",
        "entity.sensor.battery_cfg_schedule_status.state.active",
        "entity.sensor.grid_control_status.name",
        "entity.sensor.grid_control_status.state.ready",
        "entity.sensor.grid_control_status.state.blocked",
        "entity.sensor.grid_control_status.state.pending",
        "entity.sensor.battery_storage_charge.name",
        "entity.sensor.battery_storage_status.state.charging",
        "entity.sensor.battery_storage_status.state.discharging",
        "entity.sensor.battery_storage_status.state.idle",
        "entity.sensor.battery_storage_status.state.unknown",
        "entity.sensor.battery_overall_charge.name",
        "entity.sensor.battery_overall_status.name",
        "entity.sensor.battery_overall_status.state.normal",
        "entity.sensor.battery_overall_status.state.warning",
        "entity.sensor.battery_overall_status.state.error",
        "entity.sensor.battery_overall_status.state.unknown",
        "entity.number.battery_shutdown_level.name",
        "entity.switch.charge_from_grid.name",
        "entity.switch.charge_from_grid_schedule.name",
        "entity.time.charge_from_grid_start_time.name",
        "entity.time.charge_from_grid_end_time.name",
        "entity.calendar.backup_history.name",
    ]
    for locale in translations_dir.glob("*.json"):
        data = json.loads(locale.read_text(encoding="utf-8"))
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{locale.name} missing value for {path}"


def test_tariff_entity_strings_exist_for_all_locales() -> None:
    """Ensure tariff entity and attribute labels exist in every locale."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    rate_value_attrs = [
        "rate_structure",
        "variation_type",
        "source",
        "currency",
        "season_id",
        "start_month",
        "end_month",
        "day_group_id",
        "days",
        "period_id",
        "period_type",
        "start_time",
        "end_time",
        "rate",
        "formatted_rate",
        "tariff_locator",
        "tier_id",
        "start_value",
        "end_value",
        "unbounded",
        "last_refresh_utc",
    ]
    current_rate_attrs = [
        *rate_value_attrs,
        "active_rate_name",
        "configured_rates",
    ]
    paths = [
        "entity.sensor.tariff_billing_cycle.name",
        "entity.sensor.tariff_billing_cycle.state_attributes.start_date.name",
        "entity.sensor.tariff_billing_cycle.state_attributes.billing_frequency.name",
        "entity.sensor.tariff_billing_cycle.state_attributes.billing_interval_value.name",
        "entity.sensor.tariff_billing_cycle.state_attributes.billing_cycle.name",
        "entity.sensor.tariff_billing_cycle.state_attributes.last_refresh_utc.name",
        "entity.sensor.tariff_import_rate.name",
        "entity.sensor.tariff_import_rate.state_attributes.rate_structure.name",
        "entity.sensor.tariff_import_rate.state_attributes.variation_type.name",
        "entity.sensor.tariff_import_rate.state_attributes.source.name",
        "entity.sensor.tariff_import_rate.state_attributes.currency.name",
        "entity.sensor.tariff_import_rate.state_attributes.seasons.name",
        "entity.sensor.tariff_import_rate.state_attributes.last_refresh_utc.name",
        "entity.sensor.tariff_export_rate.name",
        "entity.sensor.tariff_export_rate.state_attributes.rate_structure.name",
        "entity.sensor.tariff_export_rate.state_attributes.variation_type.name",
        "entity.sensor.tariff_export_rate.state_attributes.source.name",
        "entity.sensor.tariff_export_rate.state_attributes.currency.name",
        "entity.sensor.tariff_export_rate.state_attributes.export_plan.name",
        "entity.sensor.tariff_export_rate.state_attributes.seasons.name",
        "entity.sensor.tariff_export_rate.state_attributes.last_refresh_utc.name",
    ]
    for family in ("import", "export"):
        key = f"tariff_{family}_rate_value"
        current_key = f"tariff_current_{family}_rate"
        paths.append(f"entity.sensor.{key}.name")
        paths.append(f"entity.sensor.{current_key}.name")
        paths.append(f"entity.number.{key}.name")
        attrs = list(rate_value_attrs)
        current_attrs = list(current_rate_attrs)
        if family == "export":
            attrs.append("export_plan")
            current_attrs.append("export_plan")
        for attr in attrs:
            paths.append(f"entity.sensor.{key}.state_attributes.{attr}.name")
            paths.append(f"entity.number.{key}.state_attributes.{attr}.name")
        for attr in current_attrs:
            paths.append(f"entity.sensor.{current_key}.state_attributes.{attr}.name")
    for locale in translations_dir.glob("*.json"):
        data = json.loads(locale.read_text(encoding="utf-8"))
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{locale.name} missing value for {path}"


def test_tariff_entity_strings_localized_for_non_english_locales() -> None:
    """Guard tariff labels from silently falling back to English."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    en_data = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    paths = [
        "entity.sensor.tariff_billing_cycle.name",
        "entity.sensor.tariff_billing_cycle.state_attributes.start_date.name",
        "entity.sensor.tariff_billing_cycle.state_attributes.billing_cycle.name",
        "entity.sensor.tariff_import_rate.name",
        "entity.sensor.tariff_import_rate.state_attributes.rate_structure.name",
        "entity.sensor.tariff_current_import_rate.name",
        "entity.sensor.tariff_current_import_rate.state_attributes.active_rate_name.name",
        "entity.sensor.tariff_current_import_rate.state_attributes.configured_rates.name",
        "entity.sensor.tariff_import_rate_value.name",
        "entity.sensor.tariff_import_rate_value.state_attributes.period_type.name",
        "entity.sensor.tariff_import_rate_value.state_attributes.formatted_rate.name",
        "entity.sensor.tariff_import_rate_value.state_attributes.tariff_locator.name",
        "entity.number.tariff_import_rate_value.name",
        "entity.sensor.tariff_export_rate.name",
        "entity.sensor.tariff_export_rate.state_attributes.export_plan.name",
        "entity.sensor.tariff_current_export_rate.name",
        "entity.sensor.tariff_current_export_rate.state_attributes.active_rate_name.name",
        "entity.sensor.tariff_current_export_rate.state_attributes.configured_rates.name",
        "entity.sensor.tariff_export_rate_value.name",
        "entity.sensor.tariff_export_rate_value.state_attributes.rate.name",
        "entity.number.tariff_export_rate_value.name",
    ]
    for locale in translations_dir.glob("*.json"):
        name = locale.name
        if name == "en.json" or name.startswith("en-"):
            continue
        data = json.loads(locale.read_text(encoding="utf-8"))
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{name} missing value for {path}"
            assert value != _at_path(
                en_data, path
            ), f"{name} should localize {path} (still matches English)"


def test_battery_cfg_schedule_status_strings_localized_for_non_english_locales() -> (
    None
):
    """Guard CFG schedule status labels from silently falling back to English."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    en_data = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    paths = [
        "entity.sensor.battery_cfg_schedule_status.name",
        "entity.sensor.battery_cfg_schedule_status.state.none",
        "entity.sensor.battery_cfg_schedule_status.state.pending",
        "entity.sensor.battery_cfg_schedule_status.state.active",
    ]
    for locale in translations_dir.glob("*.json"):
        name = locale.name
        if name == "en.json" or name.startswith("en-"):
            continue
        data = json.loads(locale.read_text(encoding="utf-8"))
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{name} missing value for {path}"
            assert value != _at_path(
                en_data, path
            ), f"{name} should localize {path} (still matches English)"


def test_externalized_i18n_strings_localized_for_non_english_locales() -> None:
    """Guard newly externalized user-facing strings from English fallbacks."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    en_data = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    paths = [
        "exceptions.firmware_advisory_only.message",
        "entity.sensor.dry_contacts.name",
    ]
    for locale in translations_dir.glob("*.json"):
        name = locale.name
        if name == "en.json" or name.startswith("en-"):
            continue
        data = json.loads(locale.read_text(encoding="utf-8"))
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{name} missing value for {path}"
            assert value != _at_path(
                en_data, path
            ), f"{name} should localize {path} (still matches English)"


def test_battery_schedule_editor_strings_localized_for_non_english_locales() -> None:
    """Guard battery schedule strings from silently falling back to English."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    en_data = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    paths = _battery_schedule_string_paths(en_data)
    assert "services.force_refresh.fields.config_entry_id.name" in paths
    assert "exceptions.scheduler_service_unavailable.message" in paths
    assert "entity.button.battery_schedule_add.name" in paths
    for locale in translations_dir.glob("*.json"):
        name = locale.name
        data = json.loads(locale.read_text(encoding="utf-8"))
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{name} missing value for {path}"
            if name != "en.json" and not name.startswith("en-"):
                assert value != _at_path(
                    en_data, path
                ), f"{name} should localize {path} (still matches English)"


def test_translated_user_facing_errors_require_translation_keys() -> None:
    """Guard audited modules from reintroducing raw user-facing error strings."""

    root = (
        pathlib.Path(__file__).resolve().parents[3] / "custom_components" / "enphase_ev"
    )
    audited_files = [
        "ac_battery_runtime.py",
        "battery_runtime.py",
        "select.py",
        "services.py",
        "switch.py",
    ]
    for relative_path in audited_files:
        source = (root / relative_path).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
                continue
            func = node.exc.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute) else None
            )
            if name not in {"ServiceValidationError", "HomeAssistantError"}:
                continue
            has_translation_key = any(
                keyword.arg == "translation_key" for keyword in node.exc.keywords
            )
            assert has_translation_key, (
                f"{relative_path}:{node.lineno} raises {name} "
                "without translation_key"
            )


def test_exception_translation_keys_have_no_redundant_prefix() -> None:
    """Guard against the redundant ``exceptions.`` prefix on exception keys.

    Home Assistant resolves exception messages at
    ``component.{domain}.exceptions.{translation_key}.message`` and prepends the
    ``exceptions.`` segment itself. Passing a ``translation_key`` that already
    starts with ``exceptions.`` doubles that segment, so the lookup misses and
    the raw key is surfaced to the user instead of the translated message.
    """

    root = (
        pathlib.Path(__file__).resolve().parents[3] / "custom_components" / "enphase_ev"
    )
    pattern = re.compile(r'translation_key\s*=\s*\(?\s*f?"exceptions\.')
    offenders: list[str] = []
    for path in sorted(root.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            lineno = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.name}:{lineno}")
    assert not offenders, (
        "translation_key must not include the 'exceptions.' prefix; "
        "Home Assistant adds it automatically when resolving exception "
        f"messages. Offending sites: {offenders}"
    )


def test_evse_schedule_editor_strings_exist_for_all_locales() -> None:
    """Ensure EV schedule editor strings are present in every locale catalog."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    paths = [
        "entity.select.evse_schedule_selected.name",
        "entity.button.evse_schedule_refresh.name",
        "entity.button.evse_schedule_save.name",
        "entity.button.evse_schedule_delete.name",
        "entity.button.evse_schedule_add.name",
        "entity.time.evse_schedule_edit_start_time.name",
        "entity.time.evse_schedule_edit_end_time.name",
        "entity.switch.evse_schedule_edit_mon.name",
        "entity.switch.evse_schedule_edit_tue.name",
        "entity.switch.evse_schedule_edit_wed.name",
        "entity.switch.evse_schedule_edit_thu.name",
        "entity.switch.evse_schedule_edit_fri.name",
        "entity.switch.evse_schedule_edit_sat.name",
        "entity.switch.evse_schedule_edit_sun.name",
        "exceptions.evse_schedule_day_required.message",
        "exceptions.evse_schedule_times_different.message",
        "exceptions.evse_schedule_change_rejected.message",
    ]
    for locale in translations_dir.glob("*.json"):
        data = json.loads(locale.read_text(encoding="utf-8"))
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{locale.name} missing value for {path}"


def test_evse_schedule_editor_strings_localized_for_non_english_locales() -> None:
    """Guard EV schedule editor strings from silently falling back to English."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    en_data = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    paths = [
        "entity.select.evse_schedule_selected.name",
        "entity.button.evse_schedule_add.name",
        "entity.time.evse_schedule_edit_start_time.name",
        "entity.switch.evse_schedule_edit_mon.name",
        "exceptions.evse_schedule_change_rejected.message",
    ]
    for locale in translations_dir.glob("*.json"):
        name = locale.name
        if name == "en.json" or name.startswith("en-"):
            continue
        data = json.loads(locale.read_text(encoding="utf-8"))
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{name} missing value for {path}"
            assert value != _at_path(
                en_data, path
            ), f"{name} should localize {path} (still matches English)"


def test_update_cfg_schedule_service_strings_localized_for_non_english_locales() -> (
    None
):
    """Guard the atomic CFG update service from silently falling back to English."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    en_data = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    paths = [
        "services.update_cfg_schedule.name",
        "services.update_cfg_schedule.description",
        "services.update_cfg_schedule.fields.start_time.name",
        "services.update_cfg_schedule.fields.start_time.description",
        "services.update_cfg_schedule.fields.end_time.name",
        "services.update_cfg_schedule.fields.end_time.description",
        "services.update_cfg_schedule.fields.limit.name",
        "services.update_cfg_schedule.fields.limit.description",
        "services.update_cfg_schedule.fields.site_id.name",
        "services.update_cfg_schedule.fields.site_id.description",
    ]
    for locale in translations_dir.glob("*.json"):
        name = locale.name
        data = json.loads(locale.read_text(encoding="utf-8"))
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{name} missing value for {path}"
            if name != "en.json" and not name.startswith("en-"):
                assert value != _at_path(
                    en_data, path
                ), f"{name} should localize {path} (still matches English)"


def test_battery_entity_strings_localized_for_non_english_locales() -> None:
    """Guard battery entity labels from silently falling back to English."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    en_data = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    paths = [
        "entity.sensor.battery_available_energy.name",
        "entity.sensor.battery_available_power.name",
        "entity.sensor.site_battery_power.name",
        "entity.sensor.battery_storage_status.name",
        "entity.sensor.battery_storage_status.state.charging",
        "entity.sensor.battery_storage_status.state.discharging",
        "entity.sensor.battery_storage_status.state.idle",
        "entity.sensor.battery_storage_status.state.unknown",
        "entity.sensor.battery_storage_health.name",
        "entity.sensor.battery_storage_cycle_count.name",
        "entity.sensor.battery_storage_last_reported.name",
        "entity.sensor.battery_last_reported.name",
    ]
    for locale in translations_dir.glob("*.json"):
        name = locale.name
        data = json.loads(locale.read_text(encoding="utf-8"))
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{name} missing value for {path}"
            if name != "en.json" and not name.startswith("en-"):
                assert value != _at_path(
                    en_data, path
                ), f"{name} should localize {path} (still matches English)"

        if name != "en.json":
            for path in (
                "entity.sensor.battery_storage_status.name",
                "entity.sensor.battery_storage_health.name",
                "entity.sensor.battery_storage_cycle_count.name",
                "entity.sensor.battery_storage_last_reported.name",
            ):
                assert "{serial}" in _at_path(
                    data, path
                ), f"{name} missing {{serial}} placeholder in {path}"


def test_battery_options_description_mentions_status_not_inventory() -> None:
    """Ensure battery options copy reflects the removed inventory sensor."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    expected = "Includes battery status and control entities when available."
    for locale_name in (
        "en.json",
        "en-AU.json",
        "en-CA.json",
        "en-IE.json",
        "en-NZ.json",
        "en-US.json",
    ):
        data = json.loads((translations_dir / locale_name).read_text(encoding="utf-8"))
        for path in (
            "config.step.devices.data_description.type_encharge",
            "options.step.init.data_description.type_encharge",
        ):
            assert _at_path(data, path) == expected


def test_microinverter_inventory_strings_localized_for_non_english_locales() -> None:
    """Guard microinverter inventory labels from silently falling back to English."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    en_data = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    assert (
        _at_path(en_data, "entity.sensor.microinverter_reporting_count.name")
        == "Active Microinverters"
    )
    paths = [
        "entity.sensor.microinverter_connectivity_status.name",
        "entity.sensor.microinverter_reporting_count.name",
        "entity.sensor.microinverter_last_reported.name",
    ]
    for locale in translations_dir.glob("*.json"):
        name = locale.name
        data = json.loads(locale.read_text(encoding="utf-8"))
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{name} missing value for {path}"
            if name != "en.json" and not name.startswith("en-"):
                assert value != _at_path(
                    en_data, path
                ), f"{name} should localize {path} (still matches English)"


def test_heatpump_inventory_strings_localized_for_non_english_locales() -> None:
    """Guard heat pump labels from silently falling back to English."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    en_data = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    assert (
        _at_path(en_data, "entity.sensor.heat_pump_status.name")
        == "Heat Pump Runtime Status"
    )
    paths = [
        "entity.sensor.heat_pump_status.name",
        "entity.sensor.heat_pump_connectivity_status.name",
        "entity.sensor.heat_pump_sg_ready_mode.name",
        "entity.sensor.heat_pump_energy_meter.name",
        "entity.sensor.heat_pump_last_reported.name",
        "entity.sensor.heat_pump_power.name",
    ]
    for locale in translations_dir.glob("*.json"):
        name = locale.name
        data = json.loads(locale.read_text(encoding="utf-8"))
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{name} missing value for {path}"
            if name != "en.json" and not name.startswith("en-"):
                assert value != _at_path(
                    en_data, path
                ), f"{name} should localize {path} (still matches English)"


def test_heatpump_binary_sensor_strings_localized_for_non_english_locales() -> None:
    """Guard heat pump binary-sensor labels from silently falling back to English."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    en_data = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    path = "entity.binary_sensor.heat_pump_sg_ready_active.name"
    assert _at_path(en_data, path) == "Heat Pump SG-Ready Active"
    for locale in translations_dir.glob("*.json"):
        name = locale.name
        data = json.loads(locale.read_text(encoding="utf-8"))
        value = _at_path(data, path)
        assert value.strip(), f"{name} missing value for {path}"
        if name != "en.json" and not name.startswith("en-"):
            assert value != _at_path(
                en_data, path
            ), f"{name} should localize {path} (still matches English)"


def test_french_heatpump_inventory_strings_are_specific() -> None:
    """Ensure French heat pump labels are not mixed with battery/site-consumption labels."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    fr_data = json.loads((translations_dir / "fr.json").read_text(encoding="utf-8"))
    expected = {
        "entity.sensor.heat_pump_status.name": (
            "État de fonctionnement de la pompe à chaleur"
        ),
        "entity.sensor.heat_pump_connectivity_status.name": (
            "État de connectivité de la pompe à chaleur"
        ),
        "entity.sensor.heat_pump_sg_ready_mode.name": (
            "Mode SG-Ready de la pompe à chaleur"
        ),
        "entity.sensor.heat_pump_energy_meter.name": (
            "État du compteur d'énergie de la pompe à chaleur"
        ),
        "entity.sensor.heat_pump_last_reported.name": (
            "Dernier rapport de fonctionnement de la pompe à chaleur"
        ),
        "entity.sensor.heat_pump_power.name": "Puissance de la pompe à chaleur",
        "entity.binary_sensor.heat_pump_sg_ready_active.name": (
            "SG-Ready actif de la pompe à chaleur"
        ),
    }
    for path, value in expected.items():
        assert _at_path(fr_data, path) == value


def test_heatpump_inventory_strings_are_not_site_consumption_concatenations() -> None:
    """Guard against reusing site-consumption labels for heat-pump inventory sensors."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    for locale in translations_dir.glob("*.json"):
        data = json.loads(locale.read_text(encoding="utf-8"))
        prefix = _at_path(data, "entity.sensor.site_heat_pump_consumption.name")
        assert _at_path(data, "entity.sensor.heat_pump_connectivity_status.name") != (
            f"{prefix} {_at_path(data, 'entity.sensor.gateway_connectivity_status.name')}"
        ), f"{locale.name} reintroduced concatenated heat pump connectivity label"
        assert _at_path(data, "entity.sensor.heat_pump_status.name") != (
            f"{prefix} {_at_path(data, 'entity.sensor.battery_overall_status.name')}"
        ), f"{locale.name} reintroduced concatenated heat pump status label"
        assert _at_path(data, "entity.sensor.heat_pump_sg_ready_mode.name") != (
            f"{prefix} SG-Ready Gateway"
        ), f"{locale.name} reintroduced concatenated SG-Ready label"
        assert _at_path(data, "entity.sensor.heat_pump_energy_meter.name") != (
            f"{prefix} {_at_path(data, 'entity.sensor.gateway_consumption_meter.name')}"
        ), f"{locale.name} reintroduced concatenated energy meter label"
        assert _at_path(data, "entity.sensor.heat_pump_last_reported.name") != (
            f"{prefix} {_at_path(data, 'entity.sensor.microinverter_last_reported.name')}"
        ), f"{locale.name} reintroduced concatenated last reported label"
        assert _at_path(data, "entity.sensor.heat_pump_power.name") != (
            f"{prefix} {_at_path(data, 'entity.sensor.battery_available_power.name')}"
        ), f"{locale.name} reintroduced concatenated power label"


def test_site_device_lifetime_strings_localized_for_non_english_locales() -> None:
    """Ensure new site device-lifetime sensor labels are translated."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    en_data = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    paths = [
        "entity.sensor.site_evse_charging.name",
        "entity.sensor.site_heat_pump_consumption.name",
        "entity.sensor.site_water_heater_consumption.name",
    ]
    for locale in translations_dir.glob("*.json"):
        name = locale.name
        data = json.loads(locale.read_text(encoding="utf-8"))
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{name} missing value for {path}"
            if name != "en.json" and not name.startswith("en-"):
                assert value != _at_path(
                    en_data, path
                ), f"{name} should localize {path} (still matches English)"


def test_gateway_status_string_localized_for_non_english_locales() -> None:
    """Ensure gateway status label remains localized for non-English locales."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    path = "entity.sensor.gateway_connectivity_status.name"
    en_data = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    for locale in translations_dir.glob("*.json"):
        name = locale.name
        data = json.loads(locale.read_text(encoding="utf-8"))
        value = _at_path(data, path)
        assert value.strip(), f"{name} missing value for {path}"
        if name != "en.json" and not name.startswith("en-"):
            assert value != _at_path(
                en_data, path
            ), f"{name} should localize {path} (still matches English)"


def test_cloud_current_power_string_localized_for_non_english_locales() -> None:
    """Ensure current cloud power label is translated for non-English locales."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    path = "entity.sensor.current_production_power.name"
    en_data = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    for locale in translations_dir.glob("*.json"):
        name = locale.name
        data = json.loads(locale.read_text(encoding="utf-8"))
        value = _at_path(data, path)
        assert value.strip(), f"{name} missing value for {path}"
        if name != "en.json" and not name.startswith("en-"):
            assert value != _at_path(
                en_data, path
            ), f"{name} should localize {path} (still matches English)"


def test_site_grid_power_string_localized_for_non_english_locales() -> None:
    """Ensure grid-power label is translated for non-English locales."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    path = "entity.sensor.site_grid_power.name"
    en_data = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    for locale in translations_dir.glob("*.json"):
        name = locale.name
        data = json.loads(locale.read_text(encoding="utf-8"))
        value = _at_path(data, path)
        assert value.strip(), f"{name} missing value for {path}"
        if name != "en.json" and not name.startswith("en-"):
            assert value != _at_path(
                en_data, path
            ), f"{name} should localize {path} (still matches English)"


def test_site_power_state_attribute_strings_exist_for_all_locales() -> None:
    """Ensure derived site power attributes are translated for every locale."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    en_data = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    paths = [
        "entity.sensor.site_grid_power.state_attributes.last_flow_kwh.name",
        "entity.sensor.site_grid_power.state_attributes.source_flows.name",
        "entity.sensor.site_grid_power.state_attributes.sampled_at_utc.name",
        "entity.sensor.site_battery_power.state_attributes.last_flow_kwh.name",
        "entity.sensor.site_battery_power.state_attributes.source_flows.name",
        "entity.sensor.site_battery_power.state_attributes.sampled_at_utc.name",
    ]
    for locale in translations_dir.glob("*.json"):
        name = locale.name
        data = json.loads(locale.read_text(encoding="utf-8"))
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{name} missing value for {path}"
            if name != "en.json" and not name.startswith("en-"):
                assert value != _at_path(
                    en_data, path
                ), f"{name} should localize {path} (still matches English)"


def test_update_entity_strings_localized_for_non_english_locales() -> None:
    """Ensure firmware update entity labels are translated for non-English locales."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    en_data = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    paths = [
        "entity.update.gateway_firmware.name",
        "entity.update.charger_firmware.name",
    ]
    for locale in translations_dir.glob("*.json"):
        name = locale.name
        data = json.loads(locale.read_text(encoding="utf-8"))
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{name} missing value for {path}"
            if name != "en.json" and not name.startswith("en-"):
                assert value != _at_path(
                    en_data, path
                ), f"{name} should localize {path} (still matches English)"


def test_gateway_iq_energy_router_string_localized_for_non_english_locales() -> None:
    """Ensure IQ Energy Router label remains localized for non-English locales."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    path = "entity.sensor.gateway_iq_energy_router.name"
    en_data = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    for locale in translations_dir.glob("*.json"):
        name = locale.name
        data = json.loads(locale.read_text(encoding="utf-8"))
        value = _at_path(data, path)
        assert value.strip(), f"{name} missing value for {path}"
        assert "{index}" in value, f"{name} missing {{index}} placeholder for {path}"
        if name != "en.json" and not name.startswith("en-"):
            assert value != _at_path(
                en_data, path
            ), f"{name} should localize {path} (still matches English)"


def test_ev_charger_status_and_storm_guard_labels_localized() -> None:
    """Ensure EV charger status and storm guard labels stay localized."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    en_data = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    paths = [
        "entity.sensor.status.name",
        "entity.sensor.storm_guard_state.name",
    ]
    non_english_must_differ = {"entity.sensor.status.name"}

    for locale in translations_dir.glob("*.json"):
        name = locale.name
        data = json.loads(locale.read_text(encoding="utf-8"))
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{name} missing value for {path}"
            if (
                path in non_english_must_differ
                and name != "en.json"
                and not name.startswith("en-")
            ):
                assert value != _at_path(
                    en_data, path
                ), f"{name} should localize {path} (still matches English)"


def test_grid_control_strings_exist_for_all_locales() -> None:
    """Ensure OTP/grid-control strings exist across services, entities, and errors."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    paths = [
        "entity.button.request_grid_toggle_otp.name",
        "entity.sensor.grid_mode.name",
        "entity.sensor.grid_mode.state.on_grid",
        "entity.sensor.grid_mode.state.off_grid",
        "entity.sensor.grid_mode.state.unknown",
        "services.request_grid_toggle_otp.name",
        "services.request_grid_toggle_otp.description",
        "services.set_grid_mode.name",
        "services.set_grid_mode.description",
        "services.set_grid_mode.fields.mode.name",
        "services.set_grid_mode.fields.mode.description",
        "services.set_grid_mode.fields.otp.name",
        "services.set_grid_mode.fields.otp.description",
        "exceptions.grid_control_unavailable.message",
        "exceptions.grid_control_blocked.message",
        "exceptions.grid_mode_invalid.message",
        "exceptions.grid_otp_required.message",
        "exceptions.grid_otp_invalid_format.message",
        "exceptions.grid_otp_invalid.message",
        "exceptions.grid_envoy_serial_missing.message",
        "exceptions.grid_site_required.message",
        "exceptions.grid_site_ambiguous.message",
    ]
    for locale in translations_dir.glob("*.json"):
        data = json.loads(locale.read_text(encoding="utf-8"))
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{locale.name} missing value for {path}"

        blocked = _at_path(data, "exceptions.grid_control_blocked.message")
        ambiguous = _at_path(data, "exceptions.grid_site_ambiguous.message")
        assert (
            "{reasons}" in blocked
        ), f"{locale.name} missing {{reasons}} in grid_control_blocked message"
        assert (
            "{count}" in ambiguous
        ), f"{locale.name} missing {{count}} in grid_site_ambiguous message"


def test_options_device_category_strings_exist_for_all_locales() -> None:
    """Ensure options flow strings stay in sync for category-based controls."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    paths = [
        "config.step.devices.data.type_heatpump",
        "config.step.devices.data_description.type_heatpump",
        "options.step.init.data.type_envoy",
        "options.step.init.data.type_encharge",
        "options.step.init.data.type_iqevse",
        "options.step.init.data.type_heatpump",
        "options.step.init.data.type_microinverter",
        "options.step.init.data.api_timeout",
        "options.step.init.data.nominal_voltage",
        "options.step.init.data_description.type_envoy",
        "options.step.init.data_description.type_encharge",
        "options.step.init.data_description.type_iqevse",
        "options.step.init.data_description.type_heatpump",
        "options.step.init.data_description.type_microinverter",
        "options.step.init.data_description.api_timeout",
        "options.step.init.data_description.nominal_voltage",
        "options.error.serials_required",
    ]
    non_english_must_differ = [
        "config.step.devices.data.type_heatpump",
        "config.step.devices.data_description.type_heatpump",
        "options.step.init.data.type_heatpump",
        "options.step.init.data_description.type_heatpump",
        "options.step.init.data.api_timeout",
        "options.step.init.data.nominal_voltage",
        "options.step.init.data_description.api_timeout",
        "options.step.init.data_description.nominal_voltage",
    ]
    en_data = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    for locale in translations_dir.glob("*.json"):
        name = locale.name
        data = json.loads(locale.read_text(encoding="utf-8"))
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{name} missing value for {path}"
        if name != "en.json" and not name.startswith("en-"):
            for path in non_english_must_differ:
                assert _at_path(data, path) != _at_path(
                    en_data, path
                ), f"{name} should localize {path} (still matches English)"
