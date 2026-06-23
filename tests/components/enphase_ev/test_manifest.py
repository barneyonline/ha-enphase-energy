import json
import pathlib

MIN_HOME_ASSISTANT_VERSION = "2026.6.0"


def test_manifest_keys_present():
    manifest_path = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "manifest.json"
    )
    raw = manifest_path.read_text()
    data = json.loads(raw)

    assert data.get("version"), "manifest must include version"
    assert data.get("config_flow") is True, "config_flow must be true"
    assert data.get("integration_type") == "hub", "integration_type should be 'hub'"
    assert (
        data.get("iot_class") == "cloud_polling"
    ), "iot_class should be 'cloud_polling'"
    assert data.get("quality_scale") == "platinum", "quality_scale should be 'platinum'"


def test_branding_name_is_aligned_across_manifest_hacs_and_strings():
    root = pathlib.Path(__file__).resolve().parents[3]
    expected_name = "Enphase Energy"

    manifest = json.loads(
        (root / "custom_components" / "enphase_ev" / "manifest.json").read_text()
    )
    hacs = json.loads((root / "hacs.json").read_text())
    strings = json.loads(
        (root / "custom_components" / "enphase_ev" / "strings.json").read_text()
    )

    assert manifest.get("name") == expected_name
    assert hacs.get("name") == expected_name
    assert strings["config"]["step"]["user"]["title"] == expected_name


def test_minimum_homeassistant_version_is_declared_in_hacs_only():
    root = pathlib.Path(__file__).resolve().parents[3]

    manifest = json.loads(
        (root / "custom_components" / "enphase_ev" / "manifest.json").read_text()
    )
    hacs = json.loads((root / "hacs.json").read_text())

    assert "homeassistant" not in manifest
    assert hacs.get("homeassistant") == MIN_HOME_ASSISTANT_VERSION


def test_development_requirements_cover_minimum_homeassistant_version():
    root = pathlib.Path(__file__).resolve().parents[3]

    hacs = json.loads((root / "hacs.json").read_text())
    requirements_dev = (
        root / "devtools" / "docker" / "requirements-dev.txt"
    ).read_text()
    requirements_min_ha = (
        root / "devtools" / "docker" / "requirements-min-ha.txt"
    ).read_text()

    assert hacs.get("homeassistant") == MIN_HOME_ASSISTANT_VERSION
    assert f"homeassistant>={MIN_HOME_ASSISTANT_VERSION}" in requirements_dev
    assert f"homeassistant=={MIN_HOME_ASSISTANT_VERSION}" in requirements_min_ha
