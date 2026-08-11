from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import voluptuous as vol
import yaml
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.auth.const import GROUP_ID_ADMIN, GROUP_ID_USER
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.enphase_ev.api import (
    OCPP_TRIGGER_MESSAGES,
    OCPP_TRIGGER_MESSAGES_REQUIRING_CONFIRMATION,
)
from custom_components.enphase_ev.const import CONF_SITE_ID, CONF_SITE_ONLY, DOMAIN
from custom_components.enphase_ev.grid_profile_runtime import (
    SUPPORT_DENIED,
    SUPPORT_READ_ONLY,
    SUPPORT_UNAVAILABLE,
)
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from custom_components.enphase_ev.services import async_setup_services

SERVICES_YAML = Path(__file__).parents[3] / "custom_components/enphase_ev/services.yaml"
GRID_MODE_BLUEPRINT = (
    Path(__file__).parents[3]
    / "blueprints"
    / "script"
    / "enphase_ev"
    / "grid_mode_otp.yaml"
)
GRID_MODE_AUTOMATION_BLUEPRINT = (
    Path(__file__).parents[3]
    / "blueprints"
    / "automation"
    / "enphase_ev"
    / "grid_mode_otp_helper.yaml"
)


class _BlueprintLoader(yaml.SafeLoader):
    """Load blueprint input tags as their input names for structural tests."""


_BlueprintLoader.add_constructor(
    "!input", lambda loader, node: loader.construct_scalar(node)
)


@pytest.mark.asyncio
async def test_grid_mode_services_require_admin(hass: HomeAssistant) -> None:
    async_setup_services(hass)
    await hass.auth.async_create_user("Owner", group_ids=[GROUP_ID_ADMIN])
    user = await hass.auth.async_create_user(
        "Grid Mode user", group_ids=[GROUP_ID_USER]
    )
    assert not user.is_admin
    context = Context(user_id=user.id)

    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            "request_grid_toggle_otp",
            {"config_entry_id": "entry-id"},
            blocking=True,
            context=context,
        )
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            "set_grid_mode",
            {
                "config_entry_id": "entry-id",
                "mode": "off_grid",
                "otp": "1234",
            },
            blocking=True,
            context=context,
        )


def _register_service_handlers(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> dict[tuple[str, str], object]:
    registered: dict[tuple[str, str], object] = {}

    def fake_register(self, domain, service, handler, schema=None, *args, **kwargs):
        registered[(domain, service)] = handler

    monkeypatch.setattr(hass.services.__class__, "async_register", fake_register)
    async_setup_services(hass)
    return registered


def _register_service_metadata(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> dict[tuple[str, str], dict[str, object]]:
    registered: dict[tuple[str, str], dict[str, object]] = {}

    def fake_register(self, domain, service, handler, schema=None, *args, **kwargs):
        registered[(domain, service)] = {
            "handler": handler,
            "schema": schema,
            "kwargs": kwargs,
        }

    monkeypatch.setattr(hass.services.__class__, "async_register", fake_register)
    async_setup_services(hass)
    return registered


def _fake_service_coordinator(*, site_id: str, serials: set[str]):
    return SimpleNamespace(
        grid_profile_controls_enabled=True,
        site_id=site_id,
        serials=serials,
        data={serial: {"sn": serial} for serial in serials},
        async_start_charging=AsyncMock(return_value={"status": "ok"}),
        async_stop_charging=AsyncMock(return_value=None),
        async_trigger_ocpp_message=AsyncMock(return_value={"status": "accepted"}),
        async_start_streaming=AsyncMock(return_value=None),
        async_stop_streaming=AsyncMock(return_value=None),
        async_request_refresh=AsyncMock(return_value=None),
        async_try_reauth_now=AsyncMock(
            return_value=SimpleNamespace(
                success=True, reason=None, retry_after_seconds=None
            )
        ),
        async_clear_hems_auth_backoff=AsyncMock(
            return_value={
                "site_id": site_id,
                "success": True,
                "reason": "cleared",
                "hems_backoff_until": "2026-05-08T00:00:00+00:00",
            }
        ),
        schedule_sync=SimpleNamespace(async_refresh=AsyncMock(return_value=None)),
        _email="user@example.com",
        _remember_password=True,
        _stored_password="secret",
    )


def test_trigger_message_schema_restricts_requested_message(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Trigger message service accepts only known OCPP message names."""

    registered = _register_service_metadata(hass, monkeypatch)
    schema = registered[(DOMAIN, "trigger_message")]["schema"]

    assert schema({"requested_message": "MeterValues"}) == {
        "requested_message": "MeterValues",
        "confirm_advanced": False,
    }
    assert schema(
        {"requested_message": "BootNotification", "confirm_advanced": True}
    ) == {
        "requested_message": "BootNotification",
        "confirm_advanced": True,
    }

    for requested_message in (
        "status",
        "Status",
        "MeterValues ",
        "DataTransfer",
        "MeterValues;rm",
        "M" * 65,
    ):
        with pytest.raises(vol.Invalid):
            schema({"requested_message": requested_message})


def test_trigger_message_service_options_match_allowlist() -> None:
    """Service selector options must stay aligned with backend validation."""

    services = yaml.safe_load(SERVICES_YAML.read_text())
    options = services["trigger_message"]["fields"]["requested_message"]["selector"][
        "select"
    ]["options"]

    assert set(options) == OCPP_TRIGGER_MESSAGES
    assert OCPP_TRIGGER_MESSAGES_REQUIRING_CONFIRMATION < OCPP_TRIGGER_MESSAGES


def test_grid_mode_blueprint_requests_otp_and_applies_mode() -> None:
    """The script blueprint must support both halves of the OTP workflow."""

    blueprint = yaml.load(GRID_MODE_BLUEPRINT.read_text(), Loader=_BlueprintLoader)
    operation = blueprint["fields"]["operation"]
    assert operation["default"] == "request_otp"
    assert operation["required"] is True
    assert blueprint["fields"]["otp"]["required"] is False

    branches = blueprint["sequence"][1]["choose"]
    assert [branch["sequence"][0]["service"] for branch in branches] == [
        "enphase_ev.request_grid_toggle_otp",
        "enphase_ev.set_grid_mode",
    ]
    assert branches[0]["sequence"][0]["data"] == {"config_entry_id": "config_entry_id"}
    assert branches[1]["sequence"][0]["data"] == {
        "config_entry_id": "config_entry_id",
        "mode": "{{ input_mode }}",
        "otp": "{{ runtime_otp }}",
    }


def test_grid_mode_automation_blueprint_uses_config_entry_routing() -> None:
    """The automation blueprint must not use retired site or device routing."""

    blueprint = yaml.load(
        GRID_MODE_AUTOMATION_BLUEPRINT.read_text(), Loader=_BlueprintLoader
    )
    inputs = blueprint["blueprint"]["input"]
    assert "config_entry_id" in inputs
    assert "site_id" not in inputs
    assert "target" not in inputs

    actions = blueprint["action"]
    request = actions[1]
    assert request == {
        "action": "enphase_ev.request_grid_toggle_otp",
        "data": {"config_entry_id": "config_entry_id"},
    }
    apply_action = actions[5]["choose"][3]["sequence"][0]
    assert apply_action == {
        "action": "enphase_ev.set_grid_mode",
        "data": {
            "config_entry_id": "config_entry_id",
            "mode": "{{ input_mode }}",
            "otp": "{{ runtime_otp }}",
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("support_state", [SUPPORT_DENIED, SUPPORT_READ_ONLY])
async def test_grid_profile_browse_services_require_installer_access(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, support_state: str
) -> None:
    handlers = _register_service_handlers(hass, monkeypatch)
    runtime = SimpleNamespace(
        installer_access_confirmed=False,
        support_state=support_state,
        async_refresh=AsyncMock(),
        async_load_profiles=AsyncMock(),
    )
    coord = _fake_service_coordinator(site_id="grid-site", serials=set())
    coord.grid_profile_runtime = runtime
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "grid-site", CONF_SITE_ONLY: True},
        title="Grid Site",
        unique_id="grid-site",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    for service in ("browse_grid_profiles", "refresh_grid_profiles"):
        with pytest.raises(ServiceValidationError) as err:
            await handlers[(DOMAIN, service)](
                SimpleNamespace(data={"site_id": "grid-site"})
            )
        assert err.value.translation_key == "grid_profile_installer_required"

    assert runtime.async_refresh.await_count == 2
    assert [awaited.kwargs for awaited in runtime.async_refresh.await_args_list] == [
        {"force": False, "load_profiles": False},
        {"force": True, "load_profiles": False},
    ]
    runtime.async_load_profiles.assert_not_awaited()


@pytest.mark.asyncio
async def test_grid_profile_services_require_enabled_option(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    handlers = _register_service_handlers(hass, monkeypatch)
    runtime = SimpleNamespace(
        installer_access_confirmed=True,
        async_refresh=AsyncMock(),
        async_load_profiles=AsyncMock(),
    )
    coord = _fake_service_coordinator(site_id="grid-site", serials=set())
    coord.grid_profile_controls_enabled = False
    coord.grid_profile_runtime = runtime
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "grid-site", CONF_SITE_ONLY: True},
        title="Grid Site",
        unique_id="grid-site",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    with pytest.raises(ServiceValidationError) as err:
        await handlers[(DOMAIN, "browse_grid_profiles")](
            SimpleNamespace(data={"site_id": "grid-site"})
        )

    assert err.value.translation_key == "grid_profile_controls_disabled"
    runtime.async_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_grid_profile_browse_services_recheck_access_after_catalog_load(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    handlers = _register_service_handlers(hass, monkeypatch)
    runtime = SimpleNamespace(
        installer_access_confirmed=True,
        support_state=SUPPORT_DENIED,
        async_refresh=AsyncMock(),
    )

    async def deny_catalog_access(**_kwargs: object) -> list[object]:
        runtime.installer_access_confirmed = False
        runtime.support_state = SUPPORT_DENIED
        return []

    runtime.async_load_profiles = AsyncMock(side_effect=deny_catalog_access)
    coord = _fake_service_coordinator(site_id="grid-site", serials=set())
    coord.grid_profile_runtime = runtime
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "grid-site", CONF_SITE_ONLY: True},
        title="Grid Site",
        unique_id="grid-site",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    for service in ("browse_grid_profiles", "refresh_grid_profiles"):
        runtime.installer_access_confirmed = True
        with pytest.raises(ServiceValidationError) as err:
            await handlers[(DOMAIN, service)](
                SimpleNamespace(data={"site_id": "grid-site"})
            )
        assert err.value.translation_key == "grid_profile_installer_required"

    assert runtime.async_load_profiles.await_count == 2


@pytest.mark.asyncio
async def test_grid_profile_services_report_activation_unavailable(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    handlers = _register_service_handlers(hass, monkeypatch)
    runtime = SimpleNamespace(
        installer_access_confirmed=False,
        support_state=SUPPORT_UNAVAILABLE,
        async_refresh=AsyncMock(),
        async_load_profiles=AsyncMock(),
    )
    coord = _fake_service_coordinator(site_id="grid-site", serials=set())
    coord.grid_profile_runtime = runtime
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "grid-site", CONF_SITE_ONLY: True},
        title="Grid Site",
        unique_id="grid-site",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    for service in (
        "browse_grid_profiles",
        "refresh_grid_profiles",
        "set_grid_profile",
    ):
        data = {"site_id": "grid-site"}
        if service == "set_grid_profile":
            data.update({"profile_id": "agf:test", "confirm": True})
        with pytest.raises(ServiceValidationError) as err:
            await handlers[(DOMAIN, service)](SimpleNamespace(data=data))
        assert err.value.translation_key == "grid_profile_unavailable"


@pytest.mark.asyncio
async def test_grid_profile_browse_services_return_requested_catalog(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    handlers = _register_service_handlers(hass, monkeypatch)
    runtime = SimpleNamespace(
        installer_access_confirmed=True,
        async_refresh=AsyncMock(),
        async_load_profiles=AsyncMock(),
        browse_dict=MagicMock(side_effect=[{"kind": "browse"}, {"kind": "refresh"}]),
    )
    coord = _fake_service_coordinator(site_id="grid-site", serials=set())
    coord.grid_profile_runtime = runtime
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "grid-site", CONF_SITE_ONLY: True},
        title="Grid Site",
        unique_id="grid-site",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    browse = await handlers[(DOMAIN, "browse_grid_profiles")](
        SimpleNamespace(
            data={
                "site_id": "grid-site",
                "region_code": "VIC",
                "query": "Australia",
                "commonly_used": False,
            }
        )
    )
    refresh = await handlers[(DOMAIN, "refresh_grid_profiles")](
        SimpleNamespace(
            data={
                "site_id": "grid-site",
                "region_code": "VIC",
                "commonly_used": True,
            }
        )
    )

    assert browse == {"kind": "browse"}
    assert refresh == {"kind": "refresh"}
    assert [awaited.kwargs for awaited in runtime.async_refresh.await_args_list] == [
        {"force": False, "load_profiles": False},
        {"force": True, "load_profiles": False},
    ]
    assert [
        awaited.kwargs for awaited in runtime.async_load_profiles.await_args_list
    ] == [
        {"region_code": "VIC", "commonly_used": False, "force": False},
        {"region_code": "VIC", "commonly_used": True, "force": True},
    ]


@pytest.mark.asyncio
async def test_set_grid_profile_service_uses_metadata_only_preflight(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    handlers = _register_service_handlers(hass, monkeypatch)
    runtime = SimpleNamespace(
        installer_access_confirmed=True,
        async_refresh=AsyncMock(),
        async_load_profiles=AsyncMock(),
        profile_for_id=MagicMock(return_value=None),
        async_apply_grid_profile=AsyncMock(return_value={"success": True}),
    )
    coord = _fake_service_coordinator(site_id="grid-site", serials=set())
    coord.grid_profile_runtime = runtime
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "grid-site", CONF_SITE_ONLY: True},
        title="Grid Site",
        unique_id="grid-site",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    with pytest.raises(ServiceValidationError) as err:
        await handlers[(DOMAIN, "set_grid_profile")](
            SimpleNamespace(
                data={
                    "site_id": "grid-site",
                    "profile_id": "agf:test",
                    "confirm": False,
                }
            )
        )
    assert err.value.translation_key == "grid_profile_confirmation_required"

    result = await handlers[(DOMAIN, "set_grid_profile")](
        SimpleNamespace(
            data={
                "site_id": "grid-site",
                "profile_id": "agf:test",
                "region_code": "VIC",
                "confirm": True,
            }
        )
    )

    assert result == {"success": True}
    runtime.async_refresh.assert_awaited_once_with(
        force=False,
        load_profiles=False,
    )
    assert [
        awaited.kwargs for awaited in runtime.async_load_profiles.await_args_list
    ] == [
        {"region_code": "VIC", "commonly_used": True, "force": False},
        {"region_code": "VIC", "commonly_used": False, "force": False},
    ]
    runtime.async_apply_grid_profile.assert_awaited_once_with(
        "agf:test",
        region_code="VIC",
        gateway_serial=None,
    )


def test_update_tariff_schema_validates_billing_and_rates(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    registered = _register_service_metadata(hass, monkeypatch)
    schema = registered[(DOMAIN, "update_tariff")]["schema"]

    assert (DOMAIN, "set_tariff_rate") not in registered
    assert schema(
        {
            "billing_start_date": "2026-04-01",
            "billing_frequency": "MONTH",
            "billing_interval_value": 24,
            "rates": [{"entity_id": "number.import_peak", "rate": 0.25}],
        }
    ) == {
        "billing_start_date": "2026-04-01",
        "billing_frequency": "MONTH",
        "billing_interval_value": 24,
        "rates": [{"entity_id": "number.import_peak", "rate": 0.25}],
    }
    assert schema({"entity_id": ["number.import_peak"], "rate": "0.27"}) == {
        "entity_id": ["number.import_peak"],
        "rate": 0.27,
    }
    assert schema({"rate_entity": "number.import_peak", "rate": "0.28"}) == {
        "rate_entity": "number.import_peak",
        "rate": 0.28,
    }
    assert schema(
        {
            "import_rate_entity": "number.import_peak",
            "import_rate": "0.29",
            "export_rate_entity": "number.export_peak",
            "export_rate": "0.08",
        }
    ) == {
        "import_rate_entity": "number.import_peak",
        "import_rate": 0.29,
        "export_rate_entity": "number.export_peak",
        "export_rate": 0.08,
    }
    assert schema(
        {
            "site_id": "tariff-site",
            "purchase_tariff": {
                "typeKind": "single",
                "typeId": "flat",
                "seasons": [],
            },
        }
    ) == {
        "site_id": "tariff-site",
        "purchase_tariff": {
            "typeKind": "single",
            "typeId": "flat",
            "seasons": [],
        },
    }
    assert schema(
        {
            "site_id": "tariff-site",
            "configure_import_tariff": True,
            "import_tariff_type": "flat",
            "import_flat_rate": "0.24",
            "configure_export_tariff": True,
            "export_tariff_type": "tou",
            "export_variation": "weekends",
            "export_plan": "grossFit",
            "export_periods": [
                {
                    "day_group_id": "weekend",
                    "period_type": "peak",
                    "start_time": "10:00",
                    "end_time": "15:00",
                    "rate": 0.1,
                }
            ],
        }
    ) == {
        "site_id": "tariff-site",
        "configure_import_tariff": True,
        "import_tariff_type": "flat",
        "import_flat_rate": 0.24,
        "configure_export_tariff": True,
        "export_tariff_type": "tou",
        "export_variation": "weekends",
        "export_plan": "grossFit",
        "export_periods": [
            {
                "day_group_id": "weekend",
                "period_type": "peak",
                "start_time": "10:00",
                "end_time": "15:00",
                "rate": 0.1,
            }
        ],
    }

    for data in (
        {},
        {"billing_start_date": "not-a-date"},
        {"billing_start_date": "2026-04-01"},
        {"rate_entity": "number.import_peak"},
        {"import_rate": 0.29},
        {"import_rate_entity": "number.import_peak"},
        {"export_rate": 0.08},
        {"export_rate_entity": "number.export_peak"},
        {
            "billing_start_date": "2026-04-01",
            "billing_frequency": "MONTH",
            "billing_interval_value": 25,
        },
        {
            "billing_start_date": "2026-04-01",
            "billing_frequency": "DAY",
            "billing_interval_value": 101,
        },
    ):
        with pytest.raises(vol.Invalid):
            schema(data)


@pytest.mark.asyncio
async def test_trigger_message_handler_restricts_requested_message_without_schema(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct handler calls still reject unsupported OCPP message names."""

    registered = _register_service_handlers(hass, monkeypatch)

    with pytest.raises(ServiceValidationError):
        await registered[(DOMAIN, "trigger_message")](
            SimpleNamespace(data={"requested_message": "DataTransfer"})
        )


@pytest.mark.asyncio
async def test_trigger_message_handler_requires_confirmation_for_advanced_messages(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct handler calls require confirmation for advanced OCPP triggers."""

    registered = _register_service_handlers(hass, monkeypatch)

    with pytest.raises(ServiceValidationError) as err:
        await registered[(DOMAIN, "trigger_message")](
            SimpleNamespace(data={"requested_message": "BootNotification"})
        )

    assert err.value.translation_key == "trigger_message_confirmation_required"

    with pytest.raises(ServiceValidationError) as err:
        await registered[(DOMAIN, "trigger_message")](
            SimpleNamespace(
                data={
                    "requested_message": "BootNotification",
                    "confirm_advanced": "true",
                }
            )
        )

    assert err.value.translation_key == "trigger_message_confirmation_required"


@pytest.mark.asyncio
async def test_services_route_evse_targets_to_owning_entry_with_site_only_entry(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Service calls must not route EVSE work to a site-only config entry."""

    handlers = _register_service_handlers(hass, monkeypatch)

    site_only_coord = _fake_service_coordinator(site_id="site-only", serials=set())
    evse_coord = _fake_service_coordinator(site_id="evse-site", serials={"EVSE123"})

    site_only_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "site-only", CONF_SITE_ONLY: True},
        title="Site Only",
        unique_id="site-only",
    )
    site_only_entry.add_to_hass(hass)
    site_only_entry.runtime_data = EnphaseRuntimeData(coordinator=site_only_coord)

    evse_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "evse-site", CONF_SITE_ONLY: False},
        title="EVSE Site",
        unique_id="evse-site",
    )
    evse_entry.add_to_hass(hass)
    evse_entry.runtime_data = EnphaseRuntimeData(coordinator=evse_coord)

    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=site_only_entry.entry_id,
        identifiers={(DOMAIN, "site:site-only")},
        manufacturer="Enphase",
        name="Site Only Device",
    )
    device_registry.async_get_or_create(
        config_entry_id=evse_entry.entry_id,
        identifiers={(DOMAIN, "site:evse-site")},
        manufacturer="Enphase",
        name="EVSE Site Device",
    )
    charger = device_registry.async_get_or_create(
        config_entry_id=evse_entry.entry_id,
        identifiers={(DOMAIN, "EVSE123")},
        manufacturer="Enphase",
        name="Garage Charger",
        via_device=(DOMAIN, "site:evse-site"),
    )

    await handlers[(DOMAIN, "start_charging")](
        SimpleNamespace(
            data={
                "device_id": [charger.id],
                "charging_level": 24,
                "connector_id": 2,
            }
        )
    )
    evse_coord.async_start_charging.assert_awaited_once_with(
        "EVSE123", requested_amps=24, connector_id=2
    )
    site_only_coord.async_start_charging.assert_not_awaited()

    await handlers[(DOMAIN, "stop_charging")](
        SimpleNamespace(data={"device_id": [charger.id]})
    )
    evse_coord.async_stop_charging.assert_awaited_once_with("EVSE123")
    site_only_coord.async_stop_charging.assert_not_awaited()

    trigger_result = await handlers[(DOMAIN, "trigger_message")](
        SimpleNamespace(
            data={"device_id": [charger.id], "requested_message": "MeterValues"}
        )
    )
    assert trigger_result == {
        "results": [
            {
                "device_id": charger.id,
                "serial": "EVSE123",
                "site_id": "evse-site",
                "response": {"status": "accepted"},
            }
        ]
    }
    evse_coord.async_trigger_ocpp_message.assert_awaited_once_with(
        "EVSE123", "MeterValues"
    )
    site_only_coord.async_trigger_ocpp_message.assert_not_awaited()

    evse_coord.async_trigger_ocpp_message.reset_mock()
    trigger_result = await handlers[(DOMAIN, "trigger_message")](
        SimpleNamespace(
            data={
                "device_id": [charger.id],
                "requested_message": "BootNotification",
                "confirm_advanced": True,
            }
        )
    )
    assert trigger_result["results"][0]["response"] == {"status": "accepted"}
    evse_coord.async_trigger_ocpp_message.assert_awaited_once_with(
        "EVSE123", "BootNotification"
    )

    await handlers[(DOMAIN, "start_live_stream")](
        SimpleNamespace(data={"device_id": [charger.id]})
    )
    evse_coord.async_start_streaming.assert_awaited_once_with(manual=True)
    site_only_coord.async_start_streaming.assert_not_awaited()

    await handlers[(DOMAIN, "stop_live_stream")](
        SimpleNamespace(data={"device_id": [charger.id]})
    )
    evse_coord.async_stop_streaming.assert_awaited_once_with(manual=True)
    site_only_coord.async_stop_streaming.assert_not_awaited()

    await handlers[(DOMAIN, "sync_schedules")](
        SimpleNamespace(data={"device_id": [charger.id]})
    )
    evse_coord.schedule_sync.async_refresh.assert_awaited_once_with(
        reason="service", serials=["EVSE123"]
    )
    site_only_coord.schedule_sync.async_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_services_route_duplicate_serial_to_device_config_entry(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A device owner disambiguates a serial exposed by multiple entries."""

    handlers = _register_service_handlers(hass, monkeypatch)
    serial = "SHARED-EVSE"
    first_coord = _fake_service_coordinator(site_id="shared-site", serials={serial})
    second_coord = _fake_service_coordinator(site_id="shared-site", serials={serial})
    first_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "shared-site"},
        title="First shared site",
        unique_id="first-shared-site",
    )
    second_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "shared-site"},
        title="Second shared site",
        unique_id="second-shared-site",
    )
    for entry, coord in (
        (first_entry, first_coord),
        (second_entry, second_coord),
    ):
        entry.add_to_hass(hass)
        entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    device = SimpleNamespace(
        id="second-device",
        identifiers={(DOMAIN, serial)},
        config_entry_id=second_entry.entry_id,
        via_device_id=None,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.services.dr.async_get",
        lambda _hass: SimpleNamespace(
            async_get=lambda device_id: device if device_id == device.id else None
        ),
    )

    await handlers[(DOMAIN, "start_charging")](
        SimpleNamespace(
            data={
                "device_id": [device.id],
                "charging_level": 24,
                "connector_id": 1,
            }
        )
    )

    second_coord.async_start_charging.assert_awaited_once_with(
        serial, requested_amps=24, connector_id=1
    )
    first_coord.async_start_charging.assert_not_awaited()


@pytest.mark.asyncio
async def test_services_route_composite_device_through_split_owners(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-migration composite target checks each real split owner."""

    handlers = _register_service_handlers(hass, monkeypatch)
    serial = "COMPOSITE-EVSE"
    first_coord = _fake_service_coordinator(
        site_id="first-site", serials={"OTHER-EVSE"}
    )
    second_coord = _fake_service_coordinator(site_id="second-site", serials={serial})
    first_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "first-site"},
        title="First composite owner",
        unique_id="first-composite-owner",
    )
    second_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "second-site"},
        title="Second composite owner",
        unique_id="second-composite-owner",
    )
    for entry, coord in (
        (first_entry, first_coord),
        (second_entry, second_coord),
    ):
        entry.add_to_hass(hass)
        entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    composite = SimpleNamespace(
        id="old-composite-device-id",
        identifiers={(DOMAIN, serial)},
        config_entry_id=first_entry.entry_id,
        config_entries={first_entry.entry_id, second_entry.entry_id},
        via_device_id=None,
    )
    split_devices = [
        SimpleNamespace(config_entry_id=first_entry.entry_id),
        SimpleNamespace(config_entry_id=second_entry.entry_id),
    ]
    registry = SimpleNamespace(
        async_get=lambda device_id: (composite if device_id == composite.id else None),
        async_get_devices_for_composite_device_id=lambda device_id: (
            split_devices if device_id == composite.id else []
        ),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.services.dr.async_get",
        lambda _hass: registry,
    )

    await handlers[(DOMAIN, "start_charging")](
        SimpleNamespace(
            data={
                "device_id": [composite.id],
                "charging_level": 18,
                "connector_id": 2,
            }
        )
    )

    second_coord.async_start_charging.assert_awaited_once_with(
        serial, requested_amps=18, connector_id=2
    )
    first_coord.async_start_charging.assert_not_awaited()


@pytest.mark.asyncio
async def test_targeted_services_raise_without_target_or_owner(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Targeted services should fail instead of silently doing nothing."""

    handlers = _register_service_handlers(hass, monkeypatch)
    coord = _fake_service_coordinator(site_id="evse-site", serials={"EVSE123"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "evse-site", CONF_SITE_ONLY: False},
        title="EVSE Site",
        unique_id="evse-site",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    device_registry = dr.async_get(hass)
    site_device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "site:evse-site")},
        manufacturer="Enphase",
        name="EVSE Site Device",
    )
    orphan_charger = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "ORPHAN123")},
        manufacturer="Enphase",
        name="Orphan Charger",
    )

    no_target_calls = (
        ("start_charging", {}),
        ("stop_charging", {}),
        ("trigger_message", {"requested_message": "MeterValues"}),
        ("start_live_stream", {}),
        ("stop_live_stream", {}),
        ("sync_schedules", {}),
    )
    for service, data in no_target_calls:
        with pytest.raises(ServiceValidationError):
            await handlers[(DOMAIN, service)](SimpleNamespace(data=data))

    charger_target_calls = (
        ("start_charging", {"device_id": [site_device.id]}),
        ("stop_charging", {"device_id": [site_device.id]}),
        (
            "trigger_message",
            {"device_id": [site_device.id], "requested_message": "MeterValues"},
        ),
        ("sync_schedules", {"device_id": [site_device.id]}),
    )
    for service, data in charger_target_calls:
        with pytest.raises(ServiceValidationError):
            await handlers[(DOMAIN, service)](SimpleNamespace(data=data))

    owner_required_calls = (
        ("start_charging", {"device_id": [orphan_charger.id]}),
        ("stop_charging", {"device_id": [orphan_charger.id]}),
        (
            "trigger_message",
            {"device_id": [orphan_charger.id], "requested_message": "MeterValues"},
        ),
        ("sync_schedules", {"device_id": [orphan_charger.id]}),
    )
    for service, data in owner_required_calls:
        with pytest.raises(ServiceValidationError):
            await handlers[(DOMAIN, service)](SimpleNamespace(data=data))


@pytest.mark.asyncio
async def test_targeted_services_reject_mixed_valid_and_unknown_devices(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Targeted services should fail when any requested device cannot be resolved."""

    handlers = _register_service_handlers(hass, monkeypatch)
    coord = _fake_service_coordinator(site_id="evse-site", serials={"EVSE123"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "evse-site", CONF_SITE_ONLY: False},
        title="EVSE Site",
        unique_id="evse-site",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "site:evse-site")},
        manufacturer="Enphase",
        name="EVSE Site Device",
    )
    charger = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "EVSE123")},
        manufacturer="Enphase",
        name="Garage Charger",
        via_device=(DOMAIN, "site:evse-site"),
    )
    unknown_device_id = "missing-device-id"

    for service, data in (
        (
            "start_charging",
            {"device_id": [charger.id, unknown_device_id], "charging_level": 24},
        ),
        ("stop_charging", {"device_id": [charger.id, unknown_device_id]}),
        (
            "trigger_message",
            {
                "device_id": [charger.id, unknown_device_id],
                "requested_message": "MeterValues",
            },
        ),
        ("sync_schedules", {"device_id": [charger.id, unknown_device_id]}),
    ):
        with pytest.raises(ServiceValidationError):
            await handlers[(DOMAIN, service)](SimpleNamespace(data=data))

    coord.async_start_charging.assert_not_awaited()
    coord.async_stop_charging.assert_not_awaited()
    coord.async_trigger_ocpp_message.assert_not_awaited()
    coord.schedule_sync.async_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_try_reauth_now_uses_stored_credentials_for_selected_site(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manual reauth should run once for the targeted stored-credential site."""

    handlers = _register_service_handlers(hass, monkeypatch)
    coord = _fake_service_coordinator(site_id="evse-site", serials={"EVSE123"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "evse-site", CONF_SITE_ONLY: False},
        title="EVSE Site",
        unique_id="evse-site",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    result = await handlers[(DOMAIN, "try_reauth_now")](
        SimpleNamespace(data={"site_id": "evse-site"})
    )

    assert result == {"site_id": "evse-site", "success": True, "reason": None}
    coord.async_try_reauth_now.assert_awaited_once_with()
    coord.async_request_refresh.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_try_reauth_now_reused_success_does_not_force_refresh(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manual reauth should not refresh all data when auth was only reused."""

    handlers = _register_service_handlers(hass, monkeypatch)
    coord = _fake_service_coordinator(site_id="evse-site", serials={"EVSE123"})
    coord.async_try_reauth_now.return_value = SimpleNamespace(
        success=True,
        reason=None,
        retry_after_seconds=None,
        performed_refresh=False,
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "evse-site", CONF_SITE_ONLY: False},
        title="EVSE Site",
        unique_id="evse-site",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    result = await handlers[(DOMAIN, "try_reauth_now")](
        SimpleNamespace(data={"site_id": "evse-site"})
    )

    assert result == {"site_id": "evse-site", "success": True, "reason": None}
    coord.async_try_reauth_now.assert_awaited_once_with()
    coord.async_request_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_try_reauth_now_reports_missing_stored_credentials(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manual reauth should not prompt or retry when no stored password exists."""

    handlers = _register_service_handlers(hass, monkeypatch)
    coord = _fake_service_coordinator(site_id="evse-site", serials={"EVSE123"})
    coord._stored_password = None
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "evse-site", CONF_SITE_ONLY: False},
        title="EVSE Site",
        unique_id="evse-site",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    result = await handlers[(DOMAIN, "try_reauth_now")](
        SimpleNamespace(data={"site_id": "evse-site"})
    )

    assert result == {
        "site_id": "evse-site",
        "success": False,
        "reason": "stored_credentials_unavailable",
    }
    coord.async_try_reauth_now.assert_not_awaited()
    coord.async_request_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_try_reauth_now_reports_manual_retry_cooldown(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manual reauth should report when the retry cooldown prevents a new login."""

    handlers = _register_service_handlers(hass, monkeypatch)
    coord = _fake_service_coordinator(site_id="evse-site", serials={"EVSE123"})
    coord.async_try_reauth_now.return_value = SimpleNamespace(
        success=False,
        reason="manual_retry_cooldown_active",
        retry_after_seconds=42,
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "evse-site", CONF_SITE_ONLY: False},
        title="EVSE Site",
        unique_id="evse-site",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    result = await handlers[(DOMAIN, "try_reauth_now")](
        SimpleNamespace(data={"site_id": "evse-site"})
    )

    assert result == {
        "site_id": "evse-site",
        "success": False,
        "reason": "manual_retry_cooldown_active",
        "retry_after_seconds": 42,
    }
    coord.async_try_reauth_now.assert_awaited_once_with()
    coord.async_request_refresh.assert_not_awaited()

    coord.async_start_charging.assert_not_awaited()
    coord.async_stop_charging.assert_not_awaited()
    coord.async_trigger_ocpp_message.assert_not_awaited()
    coord.async_start_streaming.assert_not_awaited()
    coord.async_stop_streaming.assert_not_awaited()
    coord.schedule_sync.async_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_clear_hems_auth_backoff_targets_selected_site(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HEMS backoff clear should call the coordinator safe retry control."""

    handlers = _register_service_handlers(hass, monkeypatch)
    coord = _fake_service_coordinator(site_id="evse-site", serials={"EVSE123"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "evse-site", CONF_SITE_ONLY: False},
        title="EVSE Site",
        unique_id="evse-site",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    result = await handlers[(DOMAIN, "clear_hems_auth_backoff")](
        SimpleNamespace(data={"site_id": "evse-site"})
    )

    assert result == {
        "site_id": "evse-site",
        "success": True,
        "reason": "cleared",
        "hems_backoff_until": "2026-05-08T00:00:00+00:00",
    }
    coord.async_clear_hems_auth_backoff.assert_awaited_once_with()
    coord.async_try_reauth_now.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_tariff_accepts_rate_entities(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    handlers = _register_service_handlers(hass, monkeypatch)
    coord = _fake_service_coordinator(site_id="tariff-site", serials=set())
    coord.tariff_runtime = SimpleNamespace(async_update_tariff=AsyncMock())
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "tariff-site", CONF_SITE_ONLY: True},
        title="Tariff Site",
        unique_id="tariff-site",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    locator = {
        "branch": "purchase",
        "kind": "period",
        "season_index": 1,
        "day_index": 1,
        "period_index": 1,
    }
    reg_entry = er.async_get(hass).async_get_or_create(
        "number",
        DOMAIN,
        f"{DOMAIN}_site_tariff-site_tariff_import_rate_default_week_peak_number",
        config_entry=entry,
    )
    hass.states.async_set(reg_entry.entity_id, 0.18, {"tariff_locator": locator})

    await handlers[(DOMAIN, "update_tariff")](
        SimpleNamespace(
            data={"rates": [{"entity_id": reg_entry.entity_id, "rate": 0.25}]}
        )
    )

    coord.tariff_runtime.async_update_tariff.assert_awaited_once_with(
        billing=None,
        rate_updates=[{"locator": locator, "rate": 0.25}],
    )


@pytest.mark.asyncio
async def test_update_tariff_accepts_friendly_rate_fields(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    handlers = _register_service_handlers(hass, monkeypatch)
    coord = _fake_service_coordinator(site_id="tariff-site", serials=set())
    coord.tariff_runtime = SimpleNamespace(async_update_tariff=AsyncMock())
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "tariff-site", CONF_SITE_ONLY: True},
        title="Tariff Site",
        unique_id="tariff-site",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    import_locator = {
        "branch": "purchase",
        "kind": "period",
        "season_index": 1,
        "day_index": 1,
        "period_index": 1,
    }
    export_locator = {
        "branch": "buyback",
        "kind": "period",
        "season_index": 1,
        "day_index": 1,
        "period_index": 1,
    }
    ent_reg = er.async_get(hass)
    import_entity = ent_reg.async_get_or_create(
        "number",
        DOMAIN,
        f"{DOMAIN}_site_tariff-site_tariff_import_rate_default_week_peak_number",
        config_entry=entry,
    )
    export_entity = ent_reg.async_get_or_create(
        "number",
        DOMAIN,
        f"{DOMAIN}_site_tariff-site_tariff_export_rate_default_week_peak_number",
        config_entry=entry,
    )
    hass.states.async_set(
        import_entity.entity_id, 0.18, {"tariff_locator": import_locator}
    )
    hass.states.async_set(
        export_entity.entity_id, 0.04, {"tariff_locator": export_locator}
    )

    await handlers[(DOMAIN, "update_tariff")](
        SimpleNamespace(data={"entity_id": [import_entity.entity_id], "rate": 0.25})
    )

    coord.tariff_runtime.async_update_tariff.assert_awaited_once_with(
        billing=None,
        rate_updates=[{"locator": import_locator, "rate": 0.25}],
    )
    coord.tariff_runtime.async_update_tariff.reset_mock()

    await handlers[(DOMAIN, "update_tariff")](
        SimpleNamespace(data={"rate_entity": import_entity.entity_id, "rate": 0.26})
    )

    coord.tariff_runtime.async_update_tariff.assert_awaited_once_with(
        billing=None,
        rate_updates=[{"locator": import_locator, "rate": 0.26}],
    )
    coord.tariff_runtime.async_update_tariff.reset_mock()

    await handlers[(DOMAIN, "update_tariff")](
        SimpleNamespace(
            data={
                "import_rate_entity": import_entity.entity_id,
                "import_rate": 0.27,
                "export_rate_entity": export_entity.entity_id,
                "export_rate": 0.08,
            }
        )
    )

    coord.tariff_runtime.async_update_tariff.assert_awaited_once_with(
        billing=None,
        rate_updates=[
            {"locator": import_locator, "rate": 0.27},
            {"locator": export_locator, "rate": 0.08},
        ],
    )
    coord.tariff_runtime.async_update_tariff.reset_mock()

    await handlers[(DOMAIN, "update_tariff")](
        SimpleNamespace(data={"entity_id": import_entity.entity_id, "rate": 0.28})
    )

    coord.tariff_runtime.async_update_tariff.assert_awaited_once_with(
        billing=None,
        rate_updates=[{"locator": import_locator, "rate": 0.28}],
    )


@pytest.mark.asyncio
async def test_update_tariff_rate_entity_extractor_fallback(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    handlers = _register_service_handlers(hass, monkeypatch)
    coord = _fake_service_coordinator(site_id="tariff-site", serials=set())
    coord.tariff_runtime = SimpleNamespace(async_update_tariff=AsyncMock())
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "tariff-site", CONF_SITE_ONLY: True},
        title="Tariff Site",
        unique_id="tariff-site",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    locator = {
        "branch": "purchase",
        "kind": "period",
        "season_index": 1,
        "day_index": 1,
        "period_index": 1,
    }
    reg_entry = er.async_get(hass).async_get_or_create(
        "number",
        DOMAIN,
        f"{DOMAIN}_site_tariff-site_tariff_import_rate_default_week_fallback_number",
        config_entry=entry,
    )
    hass.states.async_set(reg_entry.entity_id, 0.18, {"tariff_locator": locator})
    monkeypatch.setattr(
        "homeassistant.helpers.target.async_extract_referenced_entity_ids",
        None,
        raising=False,
    )
    monkeypatch.setattr(
        "homeassistant.helpers.service.async_extract_referenced_entity_ids",
        lambda _hass, _call: [reg_entry.entity_id],
        raising=False,
    )

    await handlers[(DOMAIN, "update_tariff")](SimpleNamespace(data={"rate": 0.25}))

    coord.tariff_runtime.async_update_tariff.assert_awaited_once_with(
        billing=None,
        rate_updates=[{"locator": locator, "rate": 0.25}],
    )
    coord.tariff_runtime.async_update_tariff.reset_mock()

    def raise_extract_error(_hass, _call):
        raise RuntimeError("extract failed")

    monkeypatch.setattr(
        "homeassistant.helpers.service.async_extract_referenced_entity_ids",
        raise_extract_error,
        raising=False,
    )

    await handlers[(DOMAIN, "update_tariff")](
        SimpleNamespace(data={"entity_id": reg_entry.entity_id, "rate": 0.26})
    )

    coord.tariff_runtime.async_update_tariff.assert_awaited_once_with(
        billing=None,
        rate_updates=[{"locator": locator, "rate": 0.26}],
    )


@pytest.mark.asyncio
async def test_update_tariff_rejects_invalid_rate_entity_targets(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    handlers = _register_service_handlers(hass, monkeypatch)
    coord = _fake_service_coordinator(site_id="tariff-site", serials=set())
    coord.tariff_runtime = SimpleNamespace(async_update_tariff=AsyncMock())
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "tariff-site", CONF_SITE_ONLY: True},
        title="Tariff Site",
        unique_id="tariff-site",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    ent_reg = er.async_get(hass)
    other_platform = ent_reg.async_get_or_create(
        "number",
        "other_platform",
        "tariff_import_rate_other_platform",
    )
    wrong_unique_id = ent_reg.async_get_or_create(
        "number",
        DOMAIN,
        f"{DOMAIN}_site_tariff-site_other_number",
        config_entry=entry,
    )
    missing_locator = ent_reg.async_get_or_create(
        "number",
        DOMAIN,
        f"{DOMAIN}_site_tariff-site_tariff_import_rate_missing_locator_number",
        config_entry=entry,
    )
    fallback_entity = ent_reg.async_get_or_create(
        "number",
        DOMAIN,
        f"{DOMAIN}_site_tariff-site_tariff_import_rate_no_config_entry_number",
    )
    unknown_site_entity = ent_reg.async_get_or_create(
        "number",
        DOMAIN,
        f"{DOMAIN}_site_unknown-site_tariff_import_rate_no_config_entry_number",
    )
    locator = {
        "branch": "purchase",
        "kind": "period",
        "season_index": 1,
        "day_index": 1,
        "period_index": 1,
    }
    hass.states.async_set(fallback_entity.entity_id, 0.18, {"tariff_locator": locator})
    hass.states.async_set(
        unknown_site_entity.entity_id, 0.18, {"tariff_locator": locator}
    )

    await handlers[(DOMAIN, "update_tariff")](
        SimpleNamespace(data={"rate_entity": fallback_entity.entity_id, "rate": 0.25})
    )
    coord.tariff_runtime.async_update_tariff.assert_awaited_once_with(
        billing=None,
        rate_updates=[{"locator": locator, "rate": 0.25}],
    )

    for entity_id, translation_key in (
        ("number.missing", "tariff_rate_entity_invalid"),
        (other_platform.entity_id, "tariff_rate_entity_invalid"),
        (wrong_unique_id.entity_id, "tariff_rate_entity_invalid"),
        (unknown_site_entity.entity_id, "tariff_rate_entity_invalid"),
        (missing_locator.entity_id, "tariff_rate_target_invalid"),
    ):
        with pytest.raises(ServiceValidationError) as err:
            await handlers[(DOMAIN, "update_tariff")](
                SimpleNamespace(data={"rate_entity": entity_id, "rate": 0.2})
            )
        assert err.value.translation_key == translation_key


@pytest.mark.asyncio
async def test_update_tariff_rejects_missing_and_incomplete_updates(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    handlers = _register_service_handlers(hass, monkeypatch)

    with pytest.raises(ServiceValidationError) as err:
        await handlers[(DOMAIN, "update_tariff")](SimpleNamespace(data={}))
    assert err.value.translation_key == "tariff_update_required"

    with pytest.raises(ServiceValidationError) as err:
        await handlers[(DOMAIN, "update_tariff")](
            SimpleNamespace(data={"billing_start_date": "2026-04-01"})
        )
    assert err.value.translation_key == "tariff_billing_incomplete"

    with pytest.raises(ServiceValidationError) as err:
        await handlers[(DOMAIN, "update_tariff")](SimpleNamespace(data={"rate": 0.2}))
    assert err.value.translation_key == "tariff_rate_entity_required"


@pytest.mark.asyncio
async def test_update_tariff_billing_only_resolves_site_targets(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    handlers = _register_service_handlers(hass, monkeypatch)
    coord = _fake_service_coordinator(site_id="tariff-site", serials=set())
    coord.tariff_runtime = SimpleNamespace(async_update_tariff=AsyncMock())
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "tariff-site", CONF_SITE_ONLY: True},
        title="Tariff Site",
        unique_id="tariff-site",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    await handlers[(DOMAIN, "update_tariff")](
        SimpleNamespace(
            data={
                "site_id": "tariff-site",
                "billing_start_date": "2026-04-01",
                "billing_frequency": "MONTH",
                "billing_interval_value": 1,
            }
        )
    )

    coord.tariff_runtime.async_update_tariff.assert_awaited_once_with(
        billing={
            "billing_start_date": "2026-04-01",
            "billing_frequency": "MONTH",
            "billing_interval_value": 1,
        },
        rate_updates=[],
    )
    coord.tariff_runtime.async_update_tariff.reset_mock()

    await handlers[(DOMAIN, "update_tariff")](
        SimpleNamespace(
            data={
                "config_entry_id": entry.entry_id,
                "billing_start_date": "2026-04-02",
                "billing_frequency": "DAY",
                "billing_interval_value": 30,
            }
        )
    )

    coord.tariff_runtime.async_update_tariff.assert_awaited_once()
    coord.tariff_runtime.async_update_tariff.reset_mock()

    site_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "site:tariff-site")},
        manufacturer="Enphase",
        name="Tariff Site",
    )
    await handlers[(DOMAIN, "update_tariff")](
        SimpleNamespace(
            data={
                "device_id": [site_device.id],
                "billing_start_date": "2026-04-03",
                "billing_frequency": "MONTH",
                "billing_interval_value": 2,
            }
        )
    )

    coord.tariff_runtime.async_update_tariff.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_tariff_structural_updates_resolve_site_targets(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    handlers = _register_service_handlers(hass, monkeypatch)
    coord = _fake_service_coordinator(site_id="tariff-site", serials=set())
    coord.tariff_runtime = SimpleNamespace(async_update_tariff=AsyncMock())
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "tariff-site", CONF_SITE_ONLY: True},
        title="Tariff Site",
        unique_id="tariff-site",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    purchase = {
        "typeKind": "single",
        "typeId": "flat",
        "source": "manual",
        "seasons": [
            {
                "id": "default",
                "days": [
                    {
                        "id": "week",
                        "periods": [
                            {
                                "id": "off-peak",
                                "type": "off-peak",
                                "rate": "0.25",
                                "startTime": "",
                                "endTime": "",
                            }
                        ],
                    }
                ],
            }
        ],
    }
    buyback = {
        "typeKind": "single",
        "typeId": "flat",
        "source": "manual",
        "exportPlan": "netFit",
        "seasons": [
            {
                "id": "default",
                "days": [
                    {
                        "id": "week",
                        "periods": [
                            {
                                "id": "off-peak",
                                "type": "off-peak",
                                "rate": "0.08",
                                "startTime": "",
                                "endTime": "",
                            }
                        ],
                    }
                ],
            }
        ],
    }

    await handlers[(DOMAIN, "update_tariff")](
        SimpleNamespace(
            data={
                "site_id": "tariff-site",
                "purchase_tariff": purchase,
                "buyback_tariff": buyback,
            }
        )
    )

    coord.tariff_runtime.async_update_tariff.assert_awaited_once_with(
        billing=None,
        rate_updates=[],
        purchase_tariff=purchase,
        buyback_tariff=buyback,
    )
    coord.tariff_runtime.async_update_tariff.reset_mock()

    await handlers[(DOMAIN, "update_tariff")](
        SimpleNamespace(
            data={
                "config_entry_id": entry.entry_id,
                "tariff_payload": {"purchase": purchase, "buyback": buyback},
            }
        )
    )

    coord.tariff_runtime.async_update_tariff.assert_awaited_once_with(
        billing=None,
        rate_updates=[],
        tariff_payload={"purchase": purchase, "buyback": buyback},
    )


@pytest.mark.asyncio
async def test_update_tariff_guided_structural_fields_build_tariffs(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    handlers = _register_service_handlers(hass, monkeypatch)
    coord = _fake_service_coordinator(site_id="tariff-site", serials=set())
    coord.tariff_runtime = SimpleNamespace(async_update_tariff=AsyncMock())
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "tariff-site", CONF_SITE_ONLY: True},
        title="Tariff Site",
        unique_id="tariff-site",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    await handlers[(DOMAIN, "update_tariff")](
        SimpleNamespace(
            data={
                "site_id": "tariff-site",
                "configure_import_tariff": True,
                "import_tariff_type": "flat",
                "import_flat_rate": 0.24,
                "configure_export_tariff": True,
                "export_tariff_type": "tou",
                "export_variation": "weekends",
                "export_plan": "grossFit",
                "export_periods": [
                    {
                        "season_id": "summer",
                        "start_month": 1,
                        "end_month": 3,
                        "day_group_id": "weekday",
                        "period_id": "solar-peak",
                        "period_type": "peak",
                        "start_time": "10:00",
                        "end_time": "15:00",
                        "rate": 0.11,
                    },
                    {
                        "season_id": "summer",
                        "start_month": 1,
                        "end_month": 3,
                        "day_group_id": "weekend",
                        "id": "off-peak",
                        "type": "off-peak",
                        "rate": 0.04,
                    },
                ],
            }
        )
    )

    coord.tariff_runtime.async_update_tariff.assert_awaited_once()
    kwargs = coord.tariff_runtime.async_update_tariff.await_args.kwargs
    assert kwargs["billing"] is None
    assert kwargs["rate_updates"] == []
    assert kwargs["purchase_tariff"] == {
        "typeKind": "single",
        "typeId": "flat",
        "source": "manual",
        "seasons": [
            {
                "id": "default",
                "startMonth": "1",
                "endMonth": "12",
                "days": [
                    {
                        "id": "week",
                        "days": [1, 2, 3, 4, 5, 6, 7],
                        "periods": [
                            {
                                "id": "off-peak",
                                "type": "off-peak",
                                "rate": "0.24",
                                "startTime": "",
                                "endTime": "",
                                "rateComponents": [],
                            }
                        ],
                        "updatedValue": "",
                    }
                ],
            }
        ],
    }
    assert kwargs["buyback_tariff"]["typeKind"] == "weekends"
    assert kwargs["buyback_tariff"]["typeId"] == "tou"
    assert kwargs["buyback_tariff"]["exportPlan"] == "grossFit"
    day_groups = kwargs["buyback_tariff"]["seasons"][0]["days"]
    assert day_groups[0]["days"] == [1, 2, 3, 4, 5]
    assert day_groups[0]["periods"][0]["startTime"] == 600
    assert day_groups[0]["periods"][0]["endTime"] == 900
    assert day_groups[1]["days"] == [6, 7]

    coord.tariff_runtime.async_update_tariff.reset_mock()
    await handlers[(DOMAIN, "update_tariff")](
        SimpleNamespace(
            data={
                "site_id": "tariff-site",
                "configure_import_tariff": True,
                "import_tariff_type": "tiered",
                "import_variation": "seasonal",
                "import_off_peak_rate": 0.04,
                "import_tiers": [
                    {
                        "season": "winter",
                        "start_month": 4,
                        "end_month": 9,
                        "tier_id": "tier-1",
                        "start_value": 0,
                        "end_value": 10,
                        "rate": 0.18,
                    },
                    {
                        "season": "winter",
                        "start_month": 4,
                        "end_month": 9,
                        "id": "tier-2",
                        "startValue": 10,
                        "endValue": "",
                        "rate": 0.27,
                    },
                ],
            }
        )
    )

    tiered = coord.tariff_runtime.async_update_tariff.await_args.kwargs[
        "purchase_tariff"
    ]
    assert tiered["typeKind"] == "seasonal"
    assert tiered["typeId"] == "tiered"
    assert tiered["seasons"][0]["offPeak"] == "0.04"
    assert tiered["seasons"][0]["tiers"][1]["endValue"] == -1


@pytest.mark.asyncio
async def test_update_tariff_guided_structural_fields_validate_inputs(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    handlers = _register_service_handlers(hass, monkeypatch)
    coord = _fake_service_coordinator(site_id="tariff-site", serials=set())
    coord.tariff_runtime = SimpleNamespace(async_update_tariff=AsyncMock())
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "tariff-site", CONF_SITE_ONLY: True},
        title="Tariff Site",
        unique_id="tariff-site",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    invalid_inputs = (
        {"configure_import_tariff": True},
        {"configure_import_tariff": True, "import_tariff_type": "flat"},
        {"configure_import_tariff": True, "import_tariff_type": "tou"},
        {"configure_import_tariff": True, "import_tariff_type": "tiered"},
        {
            "configure_import_tariff": True,
            "import_tariff_type": "tou",
            "import_periods": ["bad"],
        },
        {
            "configure_import_tariff": True,
            "import_tariff_type": "tou",
            "import_periods": [{"rate": "bad"}],
        },
        {
            "configure_import_tariff": True,
            "import_tariff_type": "tou",
            "import_periods": [{"rate": -0.2}],
        },
        {
            "configure_import_tariff": True,
            "import_tariff_type": "tou",
            "import_periods": [{"rate": 0.2, "start_month": "bad"}],
        },
        {
            "configure_import_tariff": True,
            "import_tariff_type": "tou",
            "import_periods": [{"rate": 0.2, "start_month": 13}],
        },
        {
            "configure_import_tariff": True,
            "import_tariff_type": "tou",
            "import_periods": [{"rate": 0.2, "start_time": "bad"}],
        },
        {
            "configure_import_tariff": True,
            "import_tariff_type": "tou",
            "import_periods": [{"rate": 0.2, "start_time": "bad:time"}],
        },
        {
            "configure_import_tariff": True,
            "import_tariff_type": "tou",
            "import_periods": [{"rate": 0.2, "start_time": 1500}],
        },
        {
            "configure_import_tariff": True,
            "import_tariff_type": "tou",
            "import_periods": [{"rate": 0.2, "days": ["bad"]}],
        },
        {
            "configure_import_tariff": True,
            "import_tariff_type": "tou",
            "import_periods": [{"rate": 0.2, "days": [0]}],
        },
        {
            "configure_import_tariff": True,
            "import_tariff_type": "tiered",
            "import_tiers": ["bad"],
        },
    )
    for data in invalid_inputs:
        with pytest.raises(ServiceValidationError) as err:
            await handlers[(DOMAIN, "update_tariff")](
                SimpleNamespace(data={"site_id": "tariff-site", **data})
            )
        assert err.value.translation_key in {
            "tariff_structure_invalid",
            "tariff_rate_invalid",
        }
    coord.tariff_runtime.async_update_tariff.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_tariff_rejects_duplicate_and_cross_site_rates(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    handlers = _register_service_handlers(hass, monkeypatch)
    first = _fake_service_coordinator(site_id="first-site", serials=set())
    first.tariff_runtime = SimpleNamespace(async_update_tariff=AsyncMock())
    second = _fake_service_coordinator(site_id="second-site", serials=set())
    second.tariff_runtime = SimpleNamespace(async_update_tariff=AsyncMock())
    first_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "first-site", CONF_SITE_ONLY: True},
        title="First Site",
        unique_id="first-site",
    )
    second_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "second-site", CONF_SITE_ONLY: True},
        title="Second Site",
        unique_id="second-site",
    )
    first_entry.add_to_hass(hass)
    second_entry.add_to_hass(hass)
    first_entry.runtime_data = EnphaseRuntimeData(coordinator=first)
    second_entry.runtime_data = EnphaseRuntimeData(coordinator=second)
    ent_reg = er.async_get(hass)
    first_entity = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_site_first-site_tariff_import_rate_default_week_peak",
        config_entry=first_entry,
    )
    second_entity = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_site_second-site_tariff_import_rate_default_week_peak",
        config_entry=second_entry,
    )
    locator = {
        "branch": "purchase",
        "kind": "period",
        "season_index": 1,
        "day_index": 1,
        "period_index": 1,
    }
    hass.states.async_set(first_entity.entity_id, 0.18, {"tariff_locator": locator})
    hass.states.async_set(second_entity.entity_id, 0.19, {"tariff_locator": locator})

    with pytest.raises(ServiceValidationError) as err:
        await handlers[(DOMAIN, "update_tariff")](
            SimpleNamespace(
                data={
                    "rates": [
                        {"entity_id": first_entity.entity_id, "rate": 0.25},
                        {"entity_id": first_entity.entity_id, "rate": 0.26},
                    ]
                }
            )
        )
    assert err.value.translation_key == "tariff_rate_entity_duplicate"

    with pytest.raises(ServiceValidationError) as err:
        await handlers[(DOMAIN, "update_tariff")](
            SimpleNamespace(
                data={
                    "rates": [
                        {"entity_id": first_entity.entity_id, "rate": 0.25},
                        {"entity_id": second_entity.entity_id, "rate": 0.26},
                    ]
                }
            )
        )
    assert err.value.translation_key == "tariff_site_mismatch"

    with pytest.raises(ServiceValidationError) as err:
        await handlers[(DOMAIN, "update_tariff")](
            SimpleNamespace(
                data={
                    "site_id": "second-site",
                    "billing_start_date": "2026-04-01",
                    "billing_frequency": "MONTH",
                    "billing_interval_value": 1,
                    "rates": [{"entity_id": first_entity.entity_id, "rate": 0.25}],
                }
            )
        )
    assert err.value.translation_key == "tariff_site_mismatch"

    with pytest.raises(ServiceValidationError) as err:
        await handlers[(DOMAIN, "update_tariff")](
            SimpleNamespace(
                data={
                    "export_rate_entity": first_entity.entity_id,
                    "export_rate": 0.08,
                }
            )
        )
    assert err.value.translation_key == "tariff_rate_entity_invalid"
