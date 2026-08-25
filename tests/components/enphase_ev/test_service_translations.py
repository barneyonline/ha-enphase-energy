from __future__ import annotations

import ast
import json
import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[3] / "custom_components" / "enphase_ev"
PLACEHOLDER_RE = re.compile(r"{([A-Za-z_][A-Za-z0-9_]*)}")


def _flatten_catalog(value: object, prefix: tuple[str, ...] = ()) -> dict[str, object]:
    """Return every catalog leaf keyed by its dotted path."""

    if isinstance(value, dict):
        flattened: dict[str, object] = {}
        for key, child in value.items():
            flattened.update(_flatten_catalog(child, (*prefix, str(key))))
        return flattened
    if isinstance(value, list):
        flattened = {}
        for index, child in enumerate(value):
            flattened.update(_flatten_catalog(child, (*prefix, str(index))))
        return flattened
    return {".".join(prefix): value}


def _catalog_has_path(catalog: dict[str, object], path: str) -> bool:
    value: object = catalog
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return False
        value = value[part]
    return True


def _intentional_identical_translation(
    locale: str,
    path: str,
    value: str,
) -> bool:
    """Return whether an English-identical value is valid in this locale."""

    if path == "config.step.user.title" and value == "Enphase Energy":
        return True
    if path == "entity.switch.power_match.name" and value == "PowerMatch":
        return True
    if (
        locale == "fr"
        and value == "Notifications"
        and path
        in {
            "options.step.init.menu_options.repair_notifications",
            "options.step.repair_notifications.title",
        }
    ):
        return True
    if (
        locale == "fr"
        and value == "Source"
        and path.endswith(".state_attributes.source.name")
    ):
        return True
    if locale == "de" and path == "entity.sensor.ac_battery_storage_status.name":
        return value == "{serial} Status"

    shared_state_locales = {
        "Online": {"cs", "da", "de", "hu", "it", "nl", "pl", "pt-BR", "ro", "sv-SE"},
        "Offline": {
            "cs",
            "da",
            "de",
            "fi",
            "hu",
            "it",
            "nl",
            "pl",
            "pt-BR",
            "ro",
            "sv-SE",
        },
        "Smart": {"da", "de", "nb-NO", "sv-SE"},
        "Manual": {"es", "pt-BR", "ro"},
    }
    if path in {
        "entity.sensor.shared_labels.state.online",
        "entity.sensor.shared_labels.state.offline",
        "entity.sensor.shared_labels.state.smart_charging",
        "entity.sensor.shared_labels.state.manual_charging",
    }:
        return locale in shared_state_locales.get(value, set())

    gateway_paths = {
        "config.step.devices.data.type_envoy",
        "options.step.devices.data.type_envoy",
        "options.step.devices.sections.devices.data.type_envoy",
        "options.step.init.data.type_envoy",
    }
    if path in gateway_paths and value == "Gateway":
        return locale in {"da", "de", "it", "nb-NO", "nl", "pt-BR", "ro", "sv-SE"}

    normal_paths = {
        "entity.sensor.ac_battery_overall_status.state.normal",
        "entity.sensor.ac_battery_storage_status.state.normal",
        "entity.sensor.battery_overall_status.state.normal",
        "entity.sensor.shared_labels.state.normal",
    }
    if path in normal_paths and value == "Normal":
        return locale in {"da", "de", "es", "fr", "nb-NO", "pt-BR", "ro", "sv-SE"}

    error_paths = {
        "entity.sensor.ac_battery_overall_status.state.error",
        "entity.sensor.ac_battery_storage_status.state.error",
        "entity.sensor.battery_overall_status.state.error",
        "entity.sensor.battery_storage_status.state.error",
        "entity.sensor.shared_labels.state.error",
    }
    if path in error_paths and value == "Error":
        return locale == "es"
    if (
        path == "options.step.grid_profile.data.grid_profile_region"
        and value == "Region"
    ):
        return locale in {"da", "de", "nb-NO", "pl", "sv-SE"}
    if path == "selector.grid_profile_status.options.no" and value == "No":
        return locale in {"es", "it"}
    return False


def test_translation_catalogs_cover_every_canonical_leaf() -> None:
    """Require complete, non-empty locale catalogs with matching placeholders."""

    canonical = _flatten_catalog(
        json.loads((ROOT / "strings.json").read_text(encoding="utf-8"))
    )
    assert all(isinstance(value, str) and value.strip() for value in canonical.values())

    for locale_path in sorted((ROOT / "translations").glob("*.json")):
        translated = _flatten_catalog(
            json.loads(locale_path.read_text(encoding="utf-8"))
        )
        missing = sorted(set(canonical) - set(translated))
        extra = sorted(set(translated) - set(canonical))
        assert not missing, f"{locale_path.name} missing translation paths: {missing}"
        assert not extra, f"{locale_path.name} has stale translation paths: {extra}"
        for path, value in translated.items():
            assert (
                isinstance(value, str) and value.strip()
            ), f"{locale_path.name} has an empty translation at {path}"
            expected_placeholders = set(PLACEHOLDER_RE.findall(str(canonical[path])))
            actual_placeholders = set(PLACEHOLDER_RE.findall(value))
            assert actual_placeholders == expected_placeholders, (
                f"{locale_path.name} placeholder mismatch at {path}: "
                f"expected {sorted(expected_placeholders)}, "
                f"got {sorted(actual_placeholders)}"
            )


def test_charger_authentication_disabled_translations_use_device_sense() -> None:
    """Keep disabled states in the device sense, not the disability sense."""

    expected = {
        "bg": "Деактивирано",
        "cs": "Deaktivováno",
        "da": "Deaktiveret",
        "de": "Deaktiviert",
        "el": "Απενεργοποιημένο",
        "es": "Desactivado",
        "et": "Deaktiveeritud",
        "fi": "Ei käytössä",
        "fr": "Désactivé",
        "hu": "Letiltva",
        "it": "Disattivato",
        "lt": "Išjungta",
        "lv": "Atspējots",
        "nb-NO": "Deaktivert",
        "nl": "Uitgeschakeld",
        "pl": "Wyłączony",
        "pt-BR": "Desativado",
        "ro": "Dezactivat",
        "sv-SE": "Inaktiverad",
    }
    path = "entity.sensor.charger_authentication.state.disabled"

    for locale, expected_value in expected.items():
        catalog = json.loads(
            (ROOT / "translations" / f"{locale}.json").read_text(encoding="utf-8")
        )
        assert _at_path(catalog, path) == expected_value


def test_non_english_catalogs_preserve_envoy_brand_name() -> None:
    """Keep Envoy as a product name instead of translating it as a person."""

    path = "exceptions.grid_envoy_serial_missing.message"
    for locale_path in sorted((ROOT / "translations").glob("*.json")):
        if locale_path.stem == "en" or locale_path.stem.startswith("en-"):
            continue
        catalog = json.loads(locale_path.read_text(encoding="utf-8"))
        assert re.search(
            r"\bEnvoy\b", _at_path(catalog, path)
        ), f"{locale_path.name} must preserve the Envoy product name"


def test_guided_tariff_fields_are_not_copied_from_danish() -> None:
    """Reject the copied Danish guided-tariff block in unrelated locales."""

    field_names = {
        "configure_import_tariff",
        "import_tariff_type",
        "import_variation",
        "import_flat_rate",
        "import_periods",
        "import_tiers",
        "import_off_peak_rate",
        "configure_export_tariff",
        "export_tariff_type",
        "export_variation",
        "export_plan",
        "export_flat_rate",
        "export_periods",
        "export_tiers",
        "export_off_peak_rate",
        "device_id",
    }
    catalogs = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((ROOT / "translations").glob("*.json"))
        if path.stem == "da" or (path.stem != "en" and not path.stem.startswith("en-"))
    }

    def guided_fields(catalog: dict[str, object]) -> dict[str, object]:
        flattened = _flatten_catalog(catalog)
        return {
            path: value
            for path, value in flattened.items()
            if path.startswith("services.update_tariff.fields.")
            and path.split(".")[3] in field_names
        }

    danish = guided_fields(catalogs.pop("da"))
    for locale, catalog in catalogs.items():
        assert (
            guided_fields(catalog) != danish
        ), f"{locale}.json copied the Danish guided-tariff translations"


def test_advanced_grid_profile_labels_are_not_copied_from_estonian() -> None:
    """Reject an Estonian advanced-options label copied into other locales."""

    services = ("browse_grid_profiles", "refresh_grid_profiles", "set_grid_profile")
    path_template = "services.{}.sections.advanced.name"
    for locale_path in sorted((ROOT / "translations").glob("*.json")):
        locale = locale_path.stem
        if locale == "en" or locale.startswith("en-"):
            continue
        catalog = json.loads(locale_path.read_text(encoding="utf-8"))
        values = {
            _at_path(catalog, path_template.format(service)) for service in services
        }
        assert len(values) == 1, f"{locale_path.name} has inconsistent labels: {values}"
        if locale != "et":
            assert values != {
                "Täiendavad valikud"
            }, f"{locale_path.name} copied the Estonian advanced-options label"


def test_electrical_grid_and_site_terms_avoid_website_false_friends() -> None:
    """Reject geometric-grid and website terms in physical-site contexts."""

    forbidden_grid_terms = {
        "bg": r"решет",
        "cs": r"mříž",
        "da": r"gitter",
        "de": r"Gitter|Raster",
        "el": r"πλέγμα",
        "es": r"cuadrícul",
        "et": r"ruudust",
        "fi": r"ruuduk",
        "fr": r"grille",
        "hu": r"rács",
        "it": r"grigli",
        "lt": r"tinklel",
        "lv": r"režģ",
        "nb-NO": r"rutenett",
        "nl": r"raster",
        "pl": r"siatk",
        "pt-BR": r"grade",
        "ro": r"gril",
        "sv-SE": r"rutnät",
    }
    forbidden_website_terms = {
        "bg": r"сайт",
        "cs": r"\bweb",
        "da": r"websted",
        "el": r"ιστότοπ",
        "et": r"\b(?:sait|saidi|saite|saidil|saidile|saidilt|saidid|saitide)",
        "fi": r"sivusto",
        "hu": r"webhely",
        "lt": r"svetain",
        "lv": r"vietn",
        "nb-NO": r"nettsted",
        "pl": r"witryn",
        "sv-SE": r"webbplats",
    }
    canonical = _flatten_catalog(
        json.loads((ROOT / "strings.json").read_text(encoding="utf-8"))
    )

    for locale_path in sorted((ROOT / "translations").glob("*.json")):
        locale = locale_path.stem
        if locale == "en" or locale.startswith("en-"):
            continue
        translated = _flatten_catalog(
            json.loads(locale_path.read_text(encoding="utf-8"))
        )
        for path, source in canonical.items():
            value = translated[path]
            assert isinstance(source, str) and isinstance(value, str)
            if re.search(r"\bgrid\b", source, re.IGNORECASE):
                assert not re.search(
                    forbidden_grid_terms[locale], value, re.IGNORECASE
                ), f"{locale_path.name}:{path} uses a geometric-grid translation"
            if locale in forbidden_website_terms and re.search(
                r"\bsite\b", source, re.IGNORECASE
            ):
                assert not re.search(
                    forbidden_website_terms[locale], value, re.IGNORECASE
                ), f"{locale_path.name}:{path} translates an installation as a website"


def test_tariff_labels_use_native_bulgarian_and_greek_scripts() -> None:
    """Reject Latin transliterations in Bulgarian and Greek tariff labels."""

    paths = (
        "entity.sensor.tariff_billing_cycle.name",
        "entity.sensor.tariff_import_rate.name",
        "entity.sensor.tariff_import_rate.state_attributes.source.name",
        "entity.sensor.tariff_export_rate.name",
        "entity.sensor.tariff_export_rate.state_attributes.export_plan.name",
    )
    for locale, native_script in {"bg": r"[А-Яа-я]", "el": r"[Α-Ωα-ω]"}.items():
        catalog = json.loads(
            (ROOT / "translations" / f"{locale}.json").read_text(encoding="utf-8")
        )
        for path in paths:
            assert re.search(
                native_script, _at_path(catalog, path)
            ), f"{locale}.json:{path} must use the locale's native script"


def test_non_english_catalogs_have_no_unreviewed_english_fallbacks() -> None:
    """Reject untranslated English copy while allowing valid cognates and brands."""

    canonical = _flatten_catalog(
        json.loads((ROOT / "strings.json").read_text(encoding="utf-8"))
    )
    unreviewed: list[str] = []
    for locale_path in sorted((ROOT / "translations").glob("*.json")):
        locale = locale_path.stem
        if locale == "en" or locale.startswith("en-"):
            continue
        translated = _flatten_catalog(
            json.loads(locale_path.read_text(encoding="utf-8"))
        )
        for path, value in translated.items():
            if (
                isinstance(value, str)
                and value == canonical[path]
                and not _intentional_identical_translation(locale, path, value)
            ):
                unreviewed.append(f"{locale_path.name}:{path}={value!r}")
    assert not unreviewed, "Unreviewed English translation fallbacks:\n" + "\n".join(
        unreviewed
    )


def test_literal_translation_references_exist_in_canonical_catalog() -> None:
    """Verify literal entity, exception, repair, flow, and selector keys exist."""

    catalog = json.loads((ROOT / "strings.json").read_text(encoding="utf-8"))
    entity_modules = {
        "binary_sensor.py": "binary_sensor",
        "button.py": "button",
        "calendar.py": "calendar",
        "number.py": "number",
        "select.py": "select",
        "sensor.py": "sensor",
        "sensor_vpp.py": "sensor",
        "switch.py": "switch",
        "time.py": "time",
        "update.py": "update",
        "weather.py": "weather",
    }
    missing: list[str] = []
    for filename, domain in entity_modules.items():
        tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                if isinstance(node.value, ast.Constant) and isinstance(
                    node.value.value, str
                ):
                    for target in targets:
                        name = (
                            target.id
                            if isinstance(target, ast.Name)
                            else (
                                target.attr
                                if isinstance(target, ast.Attribute)
                                else None
                            )
                        )
                        if name == "_attr_translation_key":
                            key = node.value.value
                            if not _catalog_has_path(catalog, f"entity.{domain}.{key}"):
                                missing.append(
                                    f"{filename}:{node.lineno} "
                                    f"entity.{domain}.{key}"
                                )
            if not isinstance(node, ast.Call):
                continue
            call_name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr if isinstance(node.func, ast.Attribute) else None
            )
            if call_name in {
                "ServiceValidationError",
                "HomeAssistantError",
                "async_create_issue",
                "raise_translated_service_validation",
            }:
                continue
            keyword = next(
                (item for item in node.keywords if item.arg == "translation_key"),
                None,
            )
            if (
                keyword is not None
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            ):
                key = keyword.value.value
                if not _catalog_has_path(catalog, f"entity.{domain}.{key}"):
                    missing.append(f"{filename}:{node.lineno} entity.{domain}.{key}")

    for source_path in sorted(ROOT.glob("*.py")):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            call_name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr if isinstance(node.func, ast.Attribute) else None
            )
            if call_name not in {
                "ServiceValidationError",
                "HomeAssistantError",
                "async_create_issue",
                "raise_translated_service_validation",
            }:
                continue
            keyword = next(
                (item for item in node.keywords if item.arg == "translation_key"),
                None,
            )
            if (
                keyword is None
                or not isinstance(keyword.value, ast.Constant)
                or not isinstance(keyword.value.value, str)
            ):
                continue
            surface = "issues" if call_name == "async_create_issue" else "exceptions"
            key = keyword.value.value
            if not _catalog_has_path(catalog, f"{surface}.{key}"):
                missing.append(f"{source_path.name}:{node.lineno} {surface}.{key}")

    config_tree = ast.parse((ROOT / "config_flow.py").read_text(encoding="utf-8"))
    for class_node in (
        node for node in config_tree.body if isinstance(node, ast.ClassDef)
    ):
        surface = "options" if class_node.name == "OptionsFlowHandler" else "config"
        for node in ast.walk(class_node):
            if not isinstance(node, ast.Call):
                continue
            call_name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else node.func.id if isinstance(node.func, ast.Name) else None
            )
            if call_name in {"async_show_form", "async_show_menu"}:
                keyword = next(
                    (item for item in node.keywords if item.arg == "step_id"),
                    None,
                )
                if (
                    keyword is not None
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    key = keyword.value.value
                    if not _catalog_has_path(catalog, f"{surface}.step.{key}"):
                        missing.append(
                            f"config_flow.py:{node.lineno} {surface}.step.{key}"
                        )
            if call_name == "async_abort":
                keyword = next(
                    (item for item in node.keywords if item.arg == "reason"),
                    None,
                )
                if (
                    keyword is not None
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                    and keyword.value.value != "unknown"
                    and not _catalog_has_path(
                        catalog, f"{surface}.abort.{keyword.value.value}"
                    )
                ):
                    missing.append(
                        f"config_flow.py:{node.lineno} "
                        f"{surface}.abort.{keyword.value.value}"
                    )

    for node in ast.walk(config_tree):
        if not isinstance(node, ast.Dict):
            continue
        for key_node, value_node in zip(node.keys, node.values):
            if (
                isinstance(key_node, ast.Constant)
                and key_node.value == "translation_key"
                and isinstance(value_node, ast.Constant)
                and isinstance(value_node.value, str)
                and not _catalog_has_path(catalog, f"selector.{value_node.value}")
            ):
                missing.append(
                    f"config_flow.py:{node.lineno} selector.{value_node.value}"
                )

    assert not missing, "Missing canonical translation references:\n" + "\n".join(
        missing
    )


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


def test_service_display_text_lives_in_translations() -> None:
    """Ensure service action display text follows Home Assistant translation docs."""

    services = yaml.safe_load((ROOT / "services.yaml").read_text(encoding="utf-8"))
    strings = json.loads((ROOT / "strings.json").read_text(encoding="utf-8"))
    assert set(strings["services"]) == set(services)

    for service_key, service_data in services.items():
        assert "name" not in service_data, service_key
        assert "description" not in service_data, service_key
        service_strings = strings["services"][service_key]
        assert service_strings["name"].strip(), service_key
        assert service_strings["description"].strip(), service_key

        expected_fields: set[str] = set()
        expected_sections: set[str] = set()
        for field_key, field_data in service_data.get("fields", {}).items():
            if "fields" in field_data:
                expected_sections.add(field_key)
                expected_fields.update(field_data["fields"])
            else:
                expected_fields.add(field_key)
        assert set(service_strings.get("fields", {})) == expected_fields, service_key
        assert (
            set(service_strings.get("sections", {})) == expected_sections
        ), service_key

        for field_key, field_data in service_data.get("fields", {}).items():
            if "fields" in field_data:
                section_strings = service_strings["sections"][field_key]
                assert section_strings["name"].strip(), f"{service_key}.{field_key}"
                nested_fields = field_data["fields"]
            else:
                nested_fields = {field_key: field_data}
            for nested_key, nested_data in nested_fields.items():
                assert "name" not in nested_data, f"{service_key}.{nested_key}"
                assert "description" not in nested_data, f"{service_key}.{nested_key}"
                field_strings = service_strings["fields"][nested_key]
                assert field_strings["name"].strip(), f"{service_key}.{nested_key}"
                assert field_strings[
                    "description"
                ].strip(), f"{service_key}.{nested_key}"


def test_inverter_lifetime_energy_name_is_localized_for_all_locales() -> None:
    """Ensure per-inverter names retain their placeholder in every locale."""
    strings = json.loads((ROOT / "strings.json").read_text(encoding="utf-8"))
    path = "entity.sensor.inverter_lifetime_energy.name"
    catalog_name = _at_path(strings, path)
    assert catalog_name == "{serial} Lifetime Energy"

    translations_dir = ROOT / "translations"
    english = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    english_name = _at_path(english, path)
    for locale in translations_dir.glob("*.json"):
        name = _at_path(json.loads(locale.read_text(encoding="utf-8")), path)
        assert "{serial}" in name, f"{locale.name} missing {{serial}} placeholder"
        if locale.name != "en.json" and not locale.name.startswith("en-"):
            assert name != english_name, f"{locale.name} should localize {path}"


def test_inverter_power_name_is_localized_for_all_locales() -> None:
    """Ensure per-inverter Power names retain their localized placeholder."""
    strings = json.loads((ROOT / "strings.json").read_text(encoding="utf-8"))
    path = "entity.sensor.inverter_telemetry.name"
    catalog_name = _at_path(strings, path)
    assert catalog_name == "{serial_number} Power"

    translations_dir = ROOT / "translations"
    english = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    english_name = _at_path(english, path)
    for locale in translations_dir.glob("*.json"):
        name = _at_path(json.loads(locale.read_text(encoding="utf-8")), path)
        assert (
            "{serial_number}" in name
        ), f"{locale.name} missing {{serial_number}} placeholder"
        if locale.name != "en.json" and not locale.name.startswith("en-"):
            assert name != english_name, f"{locale.name} should localize {path}"


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
        "options.step.devices.sections.device_features.data.battery_schedules_enabled",
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
        "entity.sensor.battery_storage_charge.name",
        "entity.sensor.battery_storage_status.state.charging",
        "entity.sensor.battery_storage_status.state.discharging",
        "entity.sensor.battery_storage_status.state.idle",
        "entity.sensor.battery_storage_status.state.error",
        "entity.sensor.battery_storage_status.state.unknown",
        "entity.sensor.battery_overall_charge.name",
        "entity.sensor.battery_overall_status.name",
        "entity.sensor.battery_overall_status.state.normal",
        "entity.sensor.battery_overall_status.state.warning",
        "entity.sensor.battery_overall_status.state.error",
        "entity.sensor.battery_overall_status.state.unknown",
        "entity.number.battery_shutdown_level.name",
        "entity.switch.charge_from_grid.name",
        "entity.switch.power_match.name",
        "entity.switch.charge_from_grid_schedule.name",
        "exceptions.power_match_unavailable.message",
        "exceptions.power_match_toggle_not_applied.message",
        "entity.time.charge_from_grid_start_time.name",
        "entity.time.charge_from_grid_end_time.name",
        "entity.calendar.backup_history.name",
        "entity.calendar.system_event_history.name",
    ]
    for locale in translations_dir.glob("*.json"):
        data = json.loads(locale.read_text(encoding="utf-8"))
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{locale.name} missing value for {path}"

    en_data = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    exception_paths = [
        "exceptions.power_match_unavailable.message",
        "exceptions.power_match_toggle_not_applied.message",
    ]
    for locale in translations_dir.glob("*.json"):
        if locale.name == "en.json" or locale.name.startswith("en-"):
            continue
        data = json.loads(locale.read_text(encoding="utf-8"))
        for path in exception_paths:
            assert _at_path(data, path) != _at_path(en_data, path)


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
        "exceptions.start_charging_rejected.message",
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
        "evse_runtime.py",
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


def test_site_power_strings_localized_for_non_english_locales() -> None:
    """Ensure site-power labels are translated for non-English locales."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    paths = (
        "entity.sensor.site_consumption_power.name",
        "entity.sensor.site_grid_power.name",
    )
    en_data = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
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
        "entity.sensor.site_consumption_power.state_attributes.last_window_seconds.name",
        "entity.sensor.site_consumption_power.state_attributes.sampled_at_utc.name",
        "entity.sensor.site_consumption_power.state_attributes.method.name",
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
        "options.step.init.menu_option_descriptions.advanced",
        "options.step.advanced.menu_options.grid_toggle",
        "options.step.advanced.menu_option_descriptions.grid_toggle",
        "options.step.grid_toggle.title",
        "options.step.grid_toggle.description",
        "options.step.grid_toggle.data.mode",
        "options.step.grid_toggle_otp.title",
        "options.step.grid_toggle_otp.description",
        "options.step.grid_toggle_otp.data.otp",
        "options.step.grid_toggle_otp.data.confirm",
        "options.step.grid_toggle_applied.title",
        "options.step.grid_toggle_applied.description",
        "options.error.grid_mode_already_active",
        "options.error.grid_mode_confirm_required",
        "options.abort.grid_mode_unavailable",
        "options.abort.grid_mode_blocked",
        "selector.grid_mode.options.on_grid",
        "selector.grid_mode.options.off_grid",
        "selector.grid_mode.options.unknown",
        "selector.grid_control_block_reason.options.disable_grid_control",
        "selector.grid_control_block_reason.options.active_download",
        "selector.grid_control_block_reason.options.sunlight_backup_system_check",
        "selector.grid_control_block_reason.options.grid_outage_check",
        "selector.grid_control_block_reason.options.pending",
        "selector.grid_control_block_reason.options.unknown",
        "entity.sensor.grid_mode.name",
        "entity.sensor.grid_mode.state.on_grid",
        "entity.sensor.grid_mode.state.off_grid",
        "entity.sensor.grid_mode.state.unknown",
        "services.request_grid_toggle_otp.name",
        "services.request_grid_toggle_otp.description",
        "services.request_grid_toggle_otp.fields.config_entry_id.name",
        "services.request_grid_toggle_otp.fields.config_entry_id.description",
        "services.set_grid_mode.name",
        "services.set_grid_mode.description",
        "services.set_grid_mode.fields.config_entry_id.name",
        "services.set_grid_mode.fields.config_entry_id.description",
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
    english = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    option_paths = paths[:25]
    for locale in translations_dir.glob("*.json"):
        data = json.loads(locale.read_text(encoding="utf-8"))
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{locale.name} missing value for {path}"
        if locale.name != "en.json" and not locale.name.startswith("en-"):
            for path in option_paths:
                assert _at_path(data, path) != _at_path(
                    english, path
                ), f"{locale.name} should localize {path}"

        blocked = _at_path(data, "exceptions.grid_control_blocked.message")
        ambiguous = _at_path(data, "exceptions.grid_site_ambiguous.message")
        assert (
            "{reasons}" not in blocked
        ), f"{locale.name} should not expose raw grid-control reasons"
        assert (
            "{count}" in ambiguous
        ), f"{locale.name} missing {{count}} in grid_site_ambiguous message"


def test_pricing_edit_strings_exist_for_all_locales() -> None:
    """Ensure pricing-edit options and validation errors stay localized."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    paths = [
        "options.step.devices.sections.device_features.data.pricing_edits_enabled",
        "options.step.devices.sections.device_features.data_description.pricing_edits_enabled",
        "exceptions.pricing_edits_disabled.message",
    ]
    english = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    assert (
        _at_path(
            english,
            "options.step.devices.sections.device_features.data.pricing_edits_enabled",
        )
        == "Enable Pricing Edits"
    )
    assert (
        _at_path(
            english,
            "options.step.devices.sections.device_features.data_description.pricing_edits_enabled",
        )
        == "Manage IQ Gateway Electricity Rates"
    )
    for locale in translations_dir.glob("*.json"):
        data = json.loads(locale.read_text(encoding="utf-8"))
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{locale.name} missing value for {path}"
            if locale.name != "en.json" and not locale.name.startswith("en-"):
                assert value != _at_path(
                    english, path
                ), f"{locale.name} should localize {path}"


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
        "options.step.init.menu_options.devices",
        "options.step.init.menu_option_descriptions.devices",
        "options.step.init.menu_options.repair_notifications",
        "options.step.init.menu_option_descriptions.repair_notifications",
        "options.step.init.menu_options.authentication_settings",
        "options.step.init.menu_option_descriptions.authentication_settings",
        "options.step.devices.title",
        "options.step.devices.description",
        "options.step.devices.sections.devices.name",
        "options.step.devices.sections.device_features.name",
        "options.step.devices.data.devices",
        "options.step.devices.data.device_features",
        "options.step.devices.data.type_envoy",
        "options.step.devices.data.schedule_sync_enabled",
        "options.step.devices.data_description.type_envoy",
        "options.step.devices.sections.devices.data.type_envoy",
        "options.step.devices.sections.devices.data.type_encharge",
        "options.step.devices.sections.devices.data.type_ac_battery",
        "options.step.devices.sections.devices.data.type_iqevse",
        "options.step.devices.sections.devices.data.type_heatpump",
        "options.step.devices.sections.devices.data.type_microinverter",
        "options.step.devices.sections.device_features.data.schedule_sync_enabled",
        "options.step.devices.sections.device_features.data.battery_schedules_enabled",
        "options.step.devices.sections.device_features.data.system_event_repair_issues",
        "options.step.devices.sections.device_features.data.pricing_edits_enabled",
        "options.step.devices.sections.device_features.data.microinverter_lifetime_energy_enabled",
        "options.step.devices.sections.device_features.data.microinverter_power_enabled",
        "options.step.devices.sections.device_features.data.weather_enabled",
        "options.step.devices.sections.device_features.data.grid_profile_controls_enabled",
        "options.step.init.data.api_timeout",
        "options.step.init.data.nominal_voltage",
        "options.step.devices.sections.devices.data_description.type_envoy",
        "options.step.devices.sections.devices.data_description.type_encharge",
        "options.step.devices.sections.devices.data_description.type_ac_battery",
        "options.step.devices.sections.devices.data_description.type_iqevse",
        "options.step.devices.sections.devices.data_description.type_heatpump",
        "options.step.devices.sections.devices.data_description.type_microinverter",
        "options.step.devices.sections.device_features.data_description.system_event_repair_issues",
        "options.step.devices.sections.device_features.data_description.schedule_sync_enabled",
        "options.step.devices.sections.device_features.data_description.battery_schedules_enabled",
        "options.step.devices.sections.device_features.data_description.pricing_edits_enabled",
        "options.step.devices.sections.device_features.data_description.microinverter_lifetime_energy_enabled",
        "options.step.devices.sections.device_features.data_description.microinverter_power_enabled",
        "options.step.devices.sections.device_features.data_description.weather_enabled",
        "options.step.devices.sections.device_features.data_description.grid_profile_controls_enabled",
        "options.step.authentication_settings.title",
        "options.step.authentication_settings.description",
        "options.step.authentication_settings.data.reauth",
        "options.step.authentication_settings.data.forget_password",
        "options.step.authentication_settings.data_description.reauth",
        "options.step.authentication_settings.data_description.forget_password",
        "options.step.repair_notifications.title",
        "options.step.repair_notifications.description",
        "options.step.repair_notifications.data.degraded_service_repair_issues",
        "options.step.repair_notifications.data.system_event_repair_issues",
        "options.step.repair_notifications.data_description.degraded_service_repair_issues",
        "options.step.repair_notifications.data_description.system_event_repair_issues",
        "options.step.init.data_description.api_timeout",
        "options.step.init.data_description.nominal_voltage",
        "exceptions.grid_profile_controls_disabled.message",
        "options.error.serials_required",
    ]
    non_english_must_differ = [
        "config.step.devices.data.type_heatpump",
        "config.step.devices.data_description.type_heatpump",
        "options.step.init.menu_options.devices",
        "options.step.init.menu_option_descriptions.devices",
        "options.step.init.menu_option_descriptions.repair_notifications",
        "options.step.init.menu_options.authentication_settings",
        "options.step.init.menu_option_descriptions.authentication_settings",
        "options.step.devices.title",
        "options.step.devices.description",
        "options.step.devices.sections.devices.name",
        "options.step.devices.sections.device_features.name",
        "options.step.devices.data.devices",
        "options.step.devices.data.device_features",
        "options.step.devices.data.schedule_sync_enabled",
        "options.step.devices.sections.devices.data.type_heatpump",
        "options.step.devices.sections.device_features.data.schedule_sync_enabled",
        "options.step.devices.sections.device_features.data.battery_schedules_enabled",
        "options.step.devices.sections.device_features.data.system_event_repair_issues",
        "options.step.devices.sections.device_features.data.pricing_edits_enabled",
        "options.step.devices.sections.device_features.data.microinverter_lifetime_energy_enabled",
        "options.step.devices.sections.device_features.data.microinverter_power_enabled",
        "options.step.devices.sections.device_features.data.weather_enabled",
        "options.step.devices.sections.device_features.data.grid_profile_controls_enabled",
        "options.step.devices.sections.devices.data_description.type_heatpump",
        "options.step.devices.sections.device_features.data_description.system_event_repair_issues",
        "options.step.devices.sections.device_features.data_description.schedule_sync_enabled",
        "options.step.devices.sections.device_features.data_description.battery_schedules_enabled",
        "options.step.devices.sections.device_features.data_description.pricing_edits_enabled",
        "options.step.devices.sections.device_features.data_description.microinverter_lifetime_energy_enabled",
        "options.step.devices.sections.device_features.data_description.microinverter_power_enabled",
        "options.step.devices.sections.device_features.data_description.weather_enabled",
        "options.step.devices.sections.device_features.data_description.grid_profile_controls_enabled",
        "options.step.authentication_settings.title",
        "options.step.authentication_settings.description",
        "options.step.authentication_settings.data.reauth",
        "options.step.authentication_settings.data.forget_password",
        "options.step.authentication_settings.data_description.reauth",
        "options.step.authentication_settings.data_description.forget_password",
        "options.step.repair_notifications.description",
        "options.step.repair_notifications.data.degraded_service_repair_issues",
        "options.step.repair_notifications.data.system_event_repair_issues",
        "options.step.repair_notifications.data_description.degraded_service_repair_issues",
        "options.step.repair_notifications.data_description.system_event_repair_issues",
        "options.step.init.data.api_timeout",
        "options.step.init.data.nominal_voltage",
        "options.step.init.data_description.api_timeout",
        "options.step.init.data_description.nominal_voltage",
        "exceptions.grid_profile_controls_disabled.message",
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


def test_grid_profile_description_warns_about_malfunction_for_all_locales() -> None:
    """Ensure every Grid Profile Control form includes a localized warning."""

    path = "options.step.grid_profile.description"
    expected_english = (
        "Filter the Activation grid profile catalog for this site's country.\n\n"
        "WARNING: APPLYING AN INCORRECT GRID PROFILE MAY CAUSE THE SYSTEM TO "
        "MALFUNCTION."
    )
    strings = json.loads((ROOT / "strings.json").read_text(encoding="utf-8"))
    assert _at_path(strings, path) == expected_english

    translations_dir = ROOT / "translations"
    english = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    assert _at_path(english, path) == expected_english
    english_warning = expected_english.split("\n\n", 1)[1]

    for locale in translations_dir.glob("*.json"):
        value = _at_path(
            json.loads(locale.read_text(encoding="utf-8")),
            path,
        )
        introduction, warning = value.split("\n\n", 1)
        assert introduction.strip(), f"{locale.name} missing Grid Profile introduction"
        assert warning.strip(), f"{locale.name} missing Grid Profile warning"
        assert warning.isupper(), f"{locale.name} Grid Profile warning is not uppercase"
        if locale.name != "en.json" and not locale.name.startswith("en-"):
            assert (
                warning != english_warning
            ), f"{locale.name} should localize the Grid Profile warning"
