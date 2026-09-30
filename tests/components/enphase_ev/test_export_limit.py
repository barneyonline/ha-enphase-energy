"""Export Limit boundaries, durable verification and native options UI."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enphase_ev.api import EnphaseEVClient, Unauthorized
from custom_components.enphase_ev.api_client.export_limit_surface import (
    InvalidExportLimitForm,
    parse_form,
    read_form,
    read_settings,
    write_settings,
)
from custom_components.enphase_ev.const import DOMAIN, OPT_EXPORT_LIMIT_CONTROLS_ENABLED
from custom_components.enphase_ev.export_limit_runtime import (
    ExportLimitRuntime,
    parse_settings,
    validate_watts,
)
from custom_components.enphase_ev.options_flow import OptionsFlowHandler
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from custom_components.enphase_ev.sensor import EnphaseExportLimitSensor


def payload(**changes):
    settings = dict(
        enable=True,
        export_limit=True,
        enable_dynamic_limiting=False,
        reference_value=3,
        free_limit_value=5000,
        slew_rate=6000,
    )
    settings.update(changes)
    return {
        "data": {
            "gateway_settings": [
                {
                    "device_id": "gateway",
                    "site_id": "123",
                    "device_type": "ENVOY",
                    "pel_settings_infos": settings,
                }
            ]
        },
        "errors": None,
    }


def form(extra=""):
    return (
        '<form action="/site_pel_settings/123" method="post"><input name="authenticity_token" value="secret"><input name="info_pel_settings_info[settings_view]" value="true">'
        + "".join(
            f'<input name="info_pel_settings_info[{name}]" value="{value}">'
            for name, value in (
                ("enable_dynamic_limiting", "false"),
                ("export_limit", "true"),
                ("reference_value", "3"),
                ("free_limit_value", "5000"),
                ("slew_rate", "6000"),
            )
        )
        + extra
        + "</form>"
    )


@pytest.mark.parametrize(
    "value", [-1, 100001, 0.5, True, False, None, "5", float("nan"), float("inf")]
)
def test_invalid_watts(value):
    with pytest.raises(ServiceValidationError):
        validate_watts(value)


@pytest.mark.parametrize("value", [0, 1, 100000, 5000.0])
def test_valid_watts(value):
    assert validate_watts(value) == value


def test_settings_semantics_and_identity():
    snapshot = parse_settings(payload(), "123")
    assert snapshot.state == "limited"
    assert replace(snapshot, watts=0.0).state == "zero_export"
    assert replace(snapshot, enabled=False).state == "disabled"
    for changes in [
        dict(export=False),
        dict(dynamic=True),
        dict(reference=1),
        dict(watts=0.5),
        dict(watts=-1),
        dict(slew=0),
    ]:
        assert replace(snapshot, **changes).state == "unsupported"
    for data in [
        None,
        {},
        {"errors": ["denied"]},
        {"data": {}},
        {"data": {"gateway_settings": []}},
        {"data": {"gateway_settings": [None]}},
        {"data": {"gateway_settings": [{}, {}]}},
    ]:
        assert parse_settings(data, "123") is None
    assert parse_settings(payload(), "456") is None
    for key, value in [
        ("device_id", None),
        ("device_id", ""),
        ("device_type", "other"),
        ("pel_settings_infos", None),
    ]:
        data = payload()
        data["data"]["gateway_settings"][0][key] = value
        assert parse_settings(data, "123") is None
    for changes in [
        dict(enable="false"),
        dict(free_limit_value=None),
        dict(slew_rate=float("nan")),
        dict(reference_value=True),
    ]:
        assert parse_settings(payload(**changes), "123") is None


def test_form_successful_controls():
    html = """<input name="dup" value="a"><input name="dup" value="b">
      <input name="disabled" disabled value="x"><input type="radio" name="r" value="no">
      <input type="radio" name="r" value="yes" checked><input type="checkbox" name="c" checked>
      <input type="submit" name="submit" value="no"><input name="blank">
      <textarea name="notes">a&amp;b</textarea><select name="s"><option>first</option><option value="second" selected>Second</option></select>
      <select name="default"><option value="first">First</option></select>
      <select name="multi" multiple><option selected value="a">A</option><option selected value="b">B</option><option disabled value="c">C</option></select>
      <fieldset><input name="last" value="ok"></fieldset>"""
    fields = parse_form(form(html), "123")
    assert ("dup", "a") in fields and ("dup", "b") in fields
    assert ("r", "yes") in fields and ("r", "no") not in fields
    assert ("c", "on") in fields and ("blank", "") in fields
    assert ("notes", "a&b") in fields and ("s", "second") in fields
    assert ("default", "first") in fields
    assert ("multi", "a") in fields and ("multi", "b") in fields
    assert not any(k in ("submit", "disabled") for k, _ in fields)


@pytest.mark.parametrize(
    "html",
    [
        "",
        '<form action="/login"></form>',
        form().replace("secret", ""),
        form() + form(),
        form().replace("</form>", ""),
        form('<input type="password" name="password">'),
        form('<input type="file" name="file">'),
        form("<fieldset disabled></fieldset>"),
        form('<select name="x"><optgroup disabled></optgroup></select>'),
        form().replace(
            "/site_pel_settings/123", "https://evil.invalid/site_pel_settings/123"
        ),
        form().replace("info_pel_settings_info[slew_rate]", "other"),
    ],
)
def test_invalid_forms(html):
    with pytest.raises(InvalidExportLimitForm):
        parse_form(html, "123")


async def test_api_surface_and_facade():
    client = EnphaseEVClient(
        SimpleNamespace(cookie_jar=SimpleNamespace(filter_cookies=lambda _: {})),
        "123",
        "token",
        "cookie",
    )
    client._json = AsyncMock(return_value=payload())
    client._text_response = AsyncMock(
        return_value=SimpleNamespace(status=200, text=form(), location=None)
    )
    assert await client.async_get_export_limit_settings() == payload()
    fields = await client.async_get_export_limit_form()
    assert client._text_response.call_args.kwargs["headers"] == {
        "Accept": "text/html,application/xhtml+xml",
        "X-Requested-With": None,
    }
    assert client._text_response.call_args.kwargs["use_cookie_header_only"] is True
    await client.async_set_export_limit(
        fields + [("_method", "put"), ("dup", "a"), ("dup", "b")], 0, 6000
    )
    call = client._text_response.call_args
    assert call.args[0] == "PUT"
    assert call.kwargs["allow_replay"] is False
    assert call.kwargs["allow_reauth"] is False
    assert call.kwargs["allow_redirects"] is False
    assert call.kwargs["use_cookie_header_only"] is True
    assert call.kwargs["headers"] == {
        "Accept": "text/html,application/xhtml+xml",
        "X-Requested-With": None,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    assert "dup=a&dup=b" in call.kwargs["data"]
    assert "_method" not in call.kwargs["data"]
    assert "free_limit_value%5D=0" in call.kwargs["data"]
    await write_settings(client, fields, None, 6000)
    assert "disable_settings" in client._text_response.call_args.kwargs["data"]
    client._text_response.return_value.location = "/login"
    from custom_components.enphase_ev.api_client.errors import ActivationSessionExpired

    with pytest.raises(ActivationSessionExpired):
        await read_form(client)
    with pytest.raises(ActivationSessionExpired):
        await write_settings(client, fields, 5, 6000)
    client._text_response.return_value.location = None
    client._text_response.return_value.status = 302
    with pytest.raises(InvalidExportLimitForm):
        await read_form(client)
    assert await read_settings(client) == payload()


@pytest.fixture
def runtime(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": "123"},
        options={OPT_EXPORT_LIMIT_CONTROLS_ENABLED: True},
    )
    entry.add_to_hass(hass)
    coord = SimpleNamespace(
        hass=hass,
        config_entry=entry,
        site_id="123",
        async_update_listeners=MagicMock(),
        client=SimpleNamespace(
            async_get_export_limit_settings=AsyncMock(return_value=payload()),
            async_prepare_activation_auth=AsyncMock(return_value=True),
            async_get_activation_device_list=AsyncMock(return_value={"devices": []}),
            async_get_export_limit_form=AsyncMock(
                return_value=parse_form(form(), "123")
            ),
            async_set_export_limit=AsyncMock(),
        ),
    )
    runtime = ExportLimitRuntime(coord)
    coord.export_limit_runtime = runtime
    entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    runtime._store = SimpleNamespace(
        async_load=AsyncMock(return_value=None), async_save=AsyncMock()
    )
    with patch.object(runtime, "_schedule"):
        yield runtime
    runtime.stop()


async def test_default_off_and_opt_in(runtime, hass):
    entry = runtime.coordinator.config_entry
    hass.config_entries.async_update_entry(entry, options={})
    await runtime.async_start()
    runtime.coordinator.client.async_get_export_limit_settings.assert_not_called()
    with pytest.raises(ServiceValidationError) as err:
        await runtime.async_refresh()
    assert err.value.translation_key == "export_limit_disabled"
    flow = OptionsFlowHandler(entry)
    flow.hass = hass
    result = await flow.async_step_export_limit()
    assert result["data_schema"]({}) == {}
    assert result["errors"]["base"] == "export_limit_disabled"
    features = flow._build_features_schema()(
        {"device_features": {}, "advanced_features": {}}
    )
    assert features["advanced_features"][OPT_EXPORT_LIMIT_CONTROLS_ENABLED] is False
    with patch.object(flow, "_settings_type_keys", return_value=[]):
        result = await flow.async_step_features(
            {"device_features": {OPT_EXPORT_LIMIT_CONTROLS_ENABLED: True}}
        )
    assert result["data"][OPT_EXPORT_LIMIT_CONTROLS_ENABLED] is True
    runtime.coordinator.client.async_set_export_limit.assert_not_called()


async def test_apply_and_readback(runtime):
    client = runtime.coordinator.client
    snapshot = await runtime.async_prepare()
    result = await runtime.async_apply(0, confirm=True, expected=snapshot)
    assert result["confirmed_watts"] == 5000
    assert result["requested_watts"] == 0
    assert result["request_status"] == "pending"
    client.async_set_export_limit.assert_awaited_once_with(
        parse_form(form(), "123"), 0, 6000
    )
    with pytest.raises(ServiceValidationError) as err:
        await runtime.async_apply(3000, confirm=True)
    assert err.value.translation_key == "export_limit_pending"
    client.async_get_export_limit_settings.return_value = payload(free_limit_value=0)
    result = await runtime.async_refresh()
    assert result["confirmed_watts"] == 0 and result["request_status"] == "confirmed"
    assert runtime.pending is None
    client.async_get_export_limit_settings.return_value = payload(enable=False)
    await runtime.async_apply(None, confirm=True)
    assert client.async_set_export_limit.await_count == 1  # Already disabled.
    assert runtime.attributes()["confirmed_watts"] is None


async def test_disable_slew_and_gateway_verification(runtime):
    await runtime.async_apply(None, confirm=True)
    assert runtime.attributes()["requested_action"] == "disable"
    client = runtime.coordinator.client
    client.async_get_export_limit_settings.return_value = payload(
        enable=False, slew_rate=5
    )
    await runtime.async_refresh()
    assert runtime.pending is not None
    client.async_get_export_limit_settings.return_value = payload(enable=False)
    await runtime.async_refresh()
    assert runtime.request_status == "confirmed"


async def test_gates_and_noop(runtime):
    with pytest.raises(ServiceValidationError):
        await runtime.async_apply(5, confirm=False)
    snapshot = await runtime.async_prepare()
    await runtime.async_apply(5000, confirm=True)
    runtime.coordinator.client.async_set_export_limit.assert_not_called()
    with pytest.raises(ServiceValidationError) as err:
        await runtime.async_apply(
            5, confirm=True, expected=replace(snapshot, watts=4000.0)
        )
    assert err.value.translation_key == "export_limit_changed"
    runtime.coordinator.client.async_get_export_limit_settings.return_value = payload(
        enable_dynamic_limiting=True
    )
    with pytest.raises(ServiceValidationError):
        await runtime.async_apply(5, confirm=True)
    runtime.coordinator.client.async_get_export_limit_settings.return_value = {}
    with pytest.raises(ServiceValidationError):
        await runtime.async_prepare()


@pytest.mark.parametrize(
    "failure",
    [
        Unauthorized(),
        aiohttp.ClientResponseError(None, (), status=403),
        aiohttp.ClientResponseError(None, (), status=500),
        TimeoutError(),
    ],
)
async def test_write_failure_never_replays(runtime, failure):
    runtime.coordinator.client.async_set_export_limit.side_effect = failure
    if isinstance(failure, Unauthorized) or getattr(failure, "status", None) == 403:
        with pytest.raises(ServiceValidationError):
            await runtime.async_apply(0, confirm=True)
        assert runtime.pending is None and runtime.request_status == "rejected"
    else:
        await runtime.async_apply(0, confirm=True)
        assert runtime.pending is not None and runtime.request_status == "unconfirmed"
    assert runtime.coordinator.client.async_set_export_limit.await_count == 1


async def test_preflight_failure_and_read_failure(runtime):
    client = runtime.coordinator.client
    client.async_get_activation_device_list.side_effect = Unauthorized()
    with pytest.raises(ServiceValidationError):
        await runtime.async_prepare()
    with pytest.raises(ServiceValidationError):
        await runtime.async_apply(0, confirm=True)
    client.async_set_export_limit.assert_not_called()
    client.async_get_export_limit_settings.side_effect = TimeoutError()
    with pytest.raises(ServiceValidationError):
        await runtime.async_refresh()
    assert runtime.snapshot is None
    await runtime.async_start()
    runtime._schedule.assert_called()


async def test_timeout_resume_and_corrupt_storage(runtime):
    await runtime.async_apply(0, confirm=True)
    runtime.pending["started"] -= 601
    saved = {"pending": dict(runtime.pending)}
    runtime._loaded = False
    runtime._store.async_load.return_value = saved
    await runtime.async_refresh()
    assert runtime.request_status == "unconfirmed"
    assert runtime.coordinator.client.async_set_export_limit.await_count == 1
    runtime._loaded = False
    runtime.pending = None
    runtime._store.async_load.return_value = {"pending": {"gateway": "x"}}
    await runtime.async_refresh()
    assert runtime.pending is None


async def test_immediate_read_failure(runtime):
    runtime.coordinator.client.async_get_export_limit_settings.side_effect = [
        payload(),
        TimeoutError(),
    ]
    await runtime.async_apply(0, confirm=True)
    assert runtime.request_status == "unconfirmed" and runtime.snapshot is None


@pytest.mark.parametrize(
    "status", ["idle", "pending", "confirmed", "unconfirmed", "rejected"]
)
@pytest.mark.parametrize(
    "pending", [None, {"watts": 0}, {"watts": 3000}, {"watts": None}]
)
async def test_options_menu_shows_pending_setting(runtime, hass, status, pending):
    flow = OptionsFlowHandler(runtime.coordinator.config_entry)
    flow.hass = hass
    runtime.request_status = status
    runtime.pending = pending
    result = await flow.async_step_export_limit_action()
    expected = (
        "None"
        if pending is None
        else "Disabled" if pending["watts"] is None else f"{pending['watts']} W"
    )
    assert result["description_placeholders"]["pending"] == expected


async def test_options_menu_clears_pending_setting_after_readback(runtime, hass):
    flow = OptionsFlowHandler(runtime.coordinator.config_entry)
    flow.hass = hass
    await runtime.async_apply(0, confirm=True)
    result = await flow.async_step_export_limit_action()
    assert result["description_placeholders"]["pending"] == "0 W"
    runtime.coordinator.client.async_get_export_limit_settings.return_value = payload(
        free_limit_value=0
    )
    await runtime.async_refresh()
    assert runtime.request_status == "confirmed"
    assert runtime.pending is None
    result = await flow.async_step_export_limit_action()
    assert result["description_placeholders"]["pending"] == "None"


async def test_options_flow(runtime, hass):
    entry = runtime.coordinator.config_entry
    flow = OptionsFlowHandler(entry)
    flow.hass = hass
    menu = await flow.async_step_export_limit()
    assert menu["menu_options"] == [
        "export_limit_defaults",
        "export_limit_set",
        "export_limit_disable",
    ]
    result = await flow.async_step_export_limit_set()
    assert result["step_id"] == "export_limit_set"
    assert result["last_step"] is False
    result = await flow.async_step_export_limit_set({"limit_watts": 0.5})
    assert result["errors"]["base"] == "export_limit_invalid"
    result = await flow.async_step_export_limit_set({"limit_watts": 3000})
    assert result["description_placeholders"]["requested"] == "3000 W"
    result = await flow.async_step_export_limit_confirm({"confirm": False})
    assert result["errors"]["base"] == "export_limit_confirmation"
    result = await flow.async_step_export_limit_confirm({"confirm": True})
    assert result["step_id"] == "export_limit_submitted"
    assert (await flow.async_step_export_limit_submitted({}))["type"] == "create_entry"
    result = await flow.async_step_export_limit_set({"limit_watts": 0})
    assert result["step_id"] == "export_limit_confirm"
    assert result["description_placeholders"]["requested"] == "0 W"
    assert flow._export_watts == 0
    await flow.async_step_export_limit_disable()
    assert flow._export_watts is None
    flow._export_snapshot = None
    assert (await flow.async_step_export_limit_confirm())[
        "step_id"
    ] == "export_limit_action"


async def test_options_errors_and_disable(runtime, hass):
    flow = OptionsFlowHandler(runtime.coordinator.config_entry)
    flow.hass = hass
    await flow.async_step_export_limit()
    runtime.coordinator.client.async_get_export_limit_settings.return_value = payload(
        free_limit_value=4000
    )
    await flow.async_step_export_limit_set({"limit_watts": 1000})
    result = await flow.async_step_export_limit_confirm({"confirm": True})
    assert result["errors"]["base"] == "export_limit_changed"
    assert flow._export_snapshot.watts == 4000
    runtime.coordinator.client.async_get_activation_device_list.side_effect = (
        Unauthorized()
    )
    result = await flow.async_step_export_limit()
    assert result["errors"]["base"] == "export_limit_unavailable"
    with patch.object(flow, "_settings_type_keys", return_value=[]):
        result = await flow.async_step_features(
            {"device_features": {OPT_EXPORT_LIMIT_CONTROLS_ENABLED: False}}
        )
    assert result["data"][OPT_EXPORT_LIMIT_CONTROLS_ENABLED] is False
    runtime.coordinator.client.async_set_export_limit.assert_not_called()


async def test_poll_and_stop(runtime):
    with patch(
        "custom_components.enphase_ev.export_limit_runtime.ExportLimitRuntime._wait_for_poll",
        new_callable=AsyncMock,
    ) as sleep:

        async def stop_after_sleep(_):
            runtime._stopped = True

        sleep.side_effect = stop_after_sleep
        await runtime._poll()
        assert sleep.call_args.args == (60,)
    runtime._stopped = False
    task = asyncio.create_task(asyncio.sleep(10))
    runtime._task = task
    assert runtime.stop() is task
    await asyncio.gather(task, return_exceptions=True)


async def test_sensor_readback_and_availability(runtime):
    coord = runtime.coordinator
    coord.last_update_success = True
    coord.last_success_utc = None
    coord.config_entry = runtime.coordinator.config_entry
    with patch(
        "custom_components.enphase_ev.sensor_base.inventory_type_available",
        return_value=True,
    ):
        sensor = EnphaseExportLimitSensor(coord)
        assert sensor.native_value is None
        assert not sensor.available
        await runtime.async_refresh()
        assert sensor.native_value == "limited"
        assert sensor.available
        assert sensor.extra_state_attributes["confirmed_watts"] == 5000
        assert "gateway" not in str(sensor.extra_state_attributes)


async def test_options_readonly_labels_and_default_save(runtime, hass):
    flow = OptionsFlowHandler(runtime.coordinator.config_entry)
    flow.hass = hass
    runtime.coordinator.client.async_get_export_limit_settings.return_value = payload(
        enable_dynamic_limiting=True
    )
    result = await flow.async_step_export_limit()
    assert result["step_id"] == "export_limit_readonly"
    assert result["description_placeholders"]["current"] == "Unsupported"
    assert (await flow.async_step_export_limit_readonly({}))["type"] == "create_entry"
    runtime.coordinator.client.async_get_export_limit_settings.return_value = payload(
        enable=False
    )
    await flow.async_step_export_limit()
    result = await flow.async_step_export_limit_disable()
    assert result["description_placeholders"] == {
        "current": "Disabled",
        "requested": "Disabled",
        "current_slew": "6000.0",
        "requested_slew": "6000.0",
    }
    flow._export_snapshot = None
    assert await flow._export_current_label() == "Unsupported"
    hass.config_entries.async_update_entry(runtime.coordinator.config_entry, options={})
    assert (await flow.async_step_export_limit())["errors"][
        "base"
    ] == "export_limit_disabled"


async def test_background_verification_survives_dialog_and_stops(runtime):
    # Start the actual entry-owned background task rather than a flow-owned task.
    ExportLimitRuntime._schedule(runtime)
    task = runtime._task
    assert task is not None
    ExportLimitRuntime._schedule(runtime)
    assert runtime._task is task
    await asyncio.sleep(0)
    assert runtime.stop() is task
    await asyncio.gather(task, return_exceptions=True)
    assert runtime._task is None
    runtime.coordinator.client.async_set_export_limit.assert_not_called()


async def test_poll_pending_interval(runtime):
    await runtime.async_apply(0, confirm=True)
    with patch(
        "custom_components.enphase_ev.export_limit_runtime.ExportLimitRuntime._wait_for_poll",
        new_callable=AsyncMock,
    ) as sleep:

        async def stop_after_sleep(_):
            runtime._stopped = True

        sleep.side_effect = stop_after_sleep
        await runtime._poll()
        assert sleep.call_args.args == (30,)


async def test_refresh_requires_installer_and_clears_old_state(runtime):
    await runtime.async_refresh()
    client = runtime.coordinator.client
    client.async_get_export_limit_settings.reset_mock()
    client.async_get_activation_device_list.side_effect = Unauthorized()
    with pytest.raises(ServiceValidationError):
        await runtime.async_refresh()
    assert runtime.snapshot is None
    client.async_get_export_limit_settings.assert_not_awaited()


async def test_concurrent_requests_submit_only_once(runtime):
    results = await asyncio.gather(
        runtime.async_apply(0, confirm=True),
        runtime.async_apply(1000, confirm=True),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ServiceValidationError) for result in results) == 1
    runtime.coordinator.client.async_set_export_limit.assert_awaited_once()


async def test_actual_store_reload_does_not_replay(runtime):
    from homeassistant.helpers.storage import Store

    coord = runtime.coordinator
    runtime._store = Store(
        coord.hass, 1, f"{DOMAIN}.export_limit.{coord.config_entry.entry_id}"
    )
    await runtime.async_apply(0, confirm=True)
    restored = ExportLimitRuntime(coord)
    with patch.object(restored, "_schedule"):
        await restored.async_refresh()
        assert restored.pending["watts"] == 0
        assert restored.request_status == "unconfirmed"
        coord.client.async_get_export_limit_settings.return_value = payload(
            free_limit_value=0
        )
        await restored.async_refresh()
        assert restored.pending is None
    coord.client.async_set_export_limit.assert_awaited_once()


async def test_options_without_loaded_runtime(hass):
    flow = OptionsFlowHandler(
        MockConfigEntry(
            domain=DOMAIN, data={}, options={OPT_EXPORT_LIMIT_CONTROLS_ENABLED: True}
        )
    )
    flow.hass = hass
    result = await flow.async_step_export_limit()
    assert result["errors"]["base"] == "export_limit_unavailable"


def test_form_empty_values_and_disabled_selected_options():
    fields = parse_form(
        form(
            '<input type="checkbox" name="empty" value="" checked><select name="s"><option value="yes">Yes</option><option selected disabled value="no">No</option></select><select name="implicit"><option value="a">A<option value="b" selected>B</select>'
        ),
        "123",
    )
    assert ("empty", "") in fields
    assert not any(k == "s" for k, _ in fields)
    assert ("implicit", "b") in fields


async def test_storage_failure_prevents_submission_and_can_retry(runtime):
    runtime._store.async_save.side_effect = OSError("disk unavailable")
    with pytest.raises(ServiceValidationError):
        await runtime.async_apply(0, confirm=True)
    assert runtime.pending is None
    runtime.coordinator.client.async_set_export_limit.assert_not_awaited()
    runtime._store.async_save.side_effect = None
    await runtime.async_apply(0, confirm=True)
    runtime.coordinator.client.async_set_export_limit.assert_awaited_once()


async def test_verification_timeout_without_fresh_readback(runtime):
    await runtime.async_apply(0, confirm=True)
    runtime.pending["started"] -= 601
    runtime.coordinator.client.async_get_export_limit_settings.side_effect = (
        TimeoutError()
    )
    with pytest.raises(ServiceValidationError):
        await runtime.async_refresh()
    assert runtime.request_status == "unconfirmed"
    assert runtime.pending is not None


@pytest.mark.parametrize("value", [0, -1, True, "5", float("nan"), float("inf"), 0.001])
def test_invalid_slew_rate(value):
    from custom_components.enphase_ev.export_limit_runtime import validate_slew_rate

    with pytest.raises(ServiceValidationError) as err:
        validate_slew_rate(value)
    assert err.value.translation_key == "export_limit_invalid_slew"


async def test_slew_override_requires_matching_readback(runtime):
    # The watts already match: changing just the slew rate still sends one write.
    result = await runtime.async_apply(5000, confirm=True, slew_rate=12.25)
    assert result["slew_rate"] == 6000
    assert result["requested_slew_rate"] == 12.25
    assert runtime.pending["slew"] == 12.25
    runtime.coordinator.client.async_set_export_limit.assert_awaited_once_with(
        parse_form(form(), "123"), 5000, 12.25
    )
    assert runtime.pending is not None
    runtime.coordinator.client.async_get_export_limit_settings.return_value = payload(
        slew_rate=12.25
    )
    await runtime.async_refresh()
    assert runtime.pending is None
    assert runtime.attributes()["slew_rate"] == 12.25


async def test_default_settings_and_restore(runtime, hass):
    from custom_components.enphase_ev.const import (
        OPT_EXPORT_LIMIT_DEFAULT_WATTS,
        OPT_EXPORT_LIMIT_SLEW_RATE,
    )

    entry = runtime.coordinator.config_entry
    flow = OptionsFlowHandler(entry)
    flow.hass = hass
    assert (await flow.async_step_export_limit_defaults())[
        "step_id"
    ] == "export_limit_action"
    form_result = await flow.async_step_export_limit_defaults()
    defaults = form_result["data_schema"]({})
    assert defaults == {"limit_watts": 0, "slew_rate": 6000, "restore_slew_rate": False}
    result = await flow.async_step_export_limit_defaults(
        {"limit_watts": 1234, "slew_rate": 40.25}
    )
    assert result["data"][OPT_EXPORT_LIMIT_DEFAULT_WATTS] == 1234
    assert result["data"][OPT_EXPORT_LIMIT_SLEW_RATE] == 40.25
    hass.config_entries.async_update_entry(entry, options=result["data"])
    # Invalid input is rejected without saving or writing.
    for inputs in (
        {"limit_watts": 0.5, "slew_rate": 50},
        {"limit_watts": 0, "slew_rate": 0},
    ):
        assert (await flow.async_step_export_limit_defaults(inputs))["errors"]
    # Restore ignores the edited rate and reads fresh gateway state.
    runtime.coordinator.client.async_get_export_limit_settings.return_value = payload(
        slew_rate=7000
    )
    result = await flow.async_step_export_limit_defaults(
        {"limit_watts": 1234, "slew_rate": 0, "restore_slew_rate": True}
    )
    assert OPT_EXPORT_LIMIT_SLEW_RATE not in result["data"]
    assert flow._export_snapshot.slew == 7000
    result = await flow.async_step_export_limit_defaults(
        {"limit_watts": 0, "slew_rate": 7000}
    )
    assert OPT_EXPORT_LIMIT_SLEW_RATE not in result["data"]
    runtime.coordinator.client.async_get_export_limit_settings.side_effect = (
        TimeoutError
    )
    result = await flow.async_step_export_limit_defaults(
        {"limit_watts": 0, "restore_slew_rate": True}
    )
    assert result["errors"]["base"] == "export_limit_unavailable"
    runtime.coordinator.client.async_set_export_limit.assert_not_called()


async def test_export_limit_select(runtime, hass):
    from custom_components.enphase_ev.const import (
        OPT_EXPORT_LIMIT_DEFAULT_WATTS,
        OPT_EXPORT_LIMIT_SLEW_RATE,
    )
    from custom_components.enphase_ev.select import ExportLimitSelect

    coord = runtime.coordinator
    entity = ExportLimitSelect(coord)
    assert entity.entity_category is None
    assert entity.current_option is None and not entity.available
    assert entity.extra_state_attributes == {
        "default_limit_watts": 0,
        "default_slew_rate": None,
        "slew_rate_source": "gateway",
    }
    with patch(
        "custom_components.enphase_ev.select._type_device_info",
        return_value={"name": "IQ Gateway"},
    ):
        assert entity.device_info == {"name": "IQ Gateway"}
    await runtime.async_refresh()
    assert entity.available and entity.current_option == "enable_limit"
    assert entity.extra_state_attributes == {
        "default_limit_watts": 0,
        "default_slew_rate": 6000,
        "slew_rate_source": "gateway",
    }
    runtime.snapshot = replace(runtime.snapshot, enabled=False)
    assert entity.current_option == "disable_limit"
    runtime.snapshot = replace(runtime.snapshot, dynamic=True)
    assert entity.current_option is None and not entity.available
    with pytest.raises(ServiceValidationError):
        await entity.async_select_option("invalid")
    with patch.object(runtime, "async_apply", new_callable=AsyncMock) as apply:
        await entity.async_select_option("enable_limit")
        apply.assert_awaited_with(0, confirm=True, slew_rate=None)
        hass.config_entries.async_update_entry(
            coord.config_entry,
            options={
                OPT_EXPORT_LIMIT_CONTROLS_ENABLED: True,
                OPT_EXPORT_LIMIT_DEFAULT_WATTS: 2000,
                OPT_EXPORT_LIMIT_SLEW_RATE: 80,
            },
        )
        await entity.async_select_option("enable_limit")
        apply.assert_awaited_with(2000, confirm=True, slew_rate=80)
        assert entity.extra_state_attributes == {
            "default_limit_watts": 2000,
            "default_slew_rate": 80,
            "slew_rate_source": "saved_override",
        }
        await entity.async_select_option("disable_limit")
        apply.assert_awaited_with(None, confirm=True, slew_rate=None)


async def test_export_limit_select_setup(runtime, hass):
    from custom_components.enphase_ev import select

    coord = runtime.coordinator
    coord.iter_serials = lambda: []
    coord._devices_inventory_ready = True
    coord.async_add_listener = MagicMock(return_value=lambda: None)
    added = []
    with patch.object(
        select, "_retain_system_profile", return_value=False
    ), patch.object(select, "_retain_battery_schedule_editor", return_value=False):
        await select.async_setup_entry(
            hass, coord.config_entry, lambda entities, **kwargs: added.extend(entities)
        )
    assert any(isinstance(entity, select.ExportLimitSelect) for entity in added)


@pytest.mark.parametrize(
    "watts,final_state", [(0, "zero_export"), (3000, "limited"), (None, "disabled")]
)
async def test_sensor_pending_until_matching_readback(runtime, watts, final_state):
    from datetime import datetime, timezone

    coord = runtime.coordinator
    coord.last_update_success = True
    coord.last_success_utc = None
    with patch(
        "custom_components.enphase_ev.sensor_base.inventory_type_available",
        return_value=True,
    ):
        sensor = EnphaseExportLimitSensor(coord)
        await runtime.async_refresh()
        assert not sensor.extra_state_attributes["pending"]
        assert sensor.extra_state_attributes["pending_requested_at"] is None
        await runtime.async_apply(watts, confirm=True, slew_rate=250)
        assert sensor.native_value == "pending"
        assert sensor.available
        attrs = sensor.extra_state_attributes
        assert attrs["pending"] is True
        assert attrs["confirmed_watts"] == 5000
        assert attrs["requested_watts"] == watts
        assert attrs["slew_rate"] == 6000
        assert attrs["requested_slew_rate"] == 250
        assert (
            attrs["pending_requested_at"]
            == datetime.fromtimestamp(
                runtime.pending["started"], timezone.utc
            ).isoformat()
        )
        # Expiry and failed readback expose uncertainty without dropping the request.
        runtime.pending["started"] -= 601
        coord.client.async_get_export_limit_settings.side_effect = TimeoutError
        with pytest.raises(ServiceValidationError):
            await runtime.async_refresh()
        assert sensor.native_value == "unconfirmed"
        assert sensor.available
        assert sensor.extra_state_attributes["pending"] is True
        coord.client.async_get_export_limit_settings.side_effect = None
        coord.client.async_get_export_limit_settings.return_value = payload(
            enable=watts is not None, free_limit_value=watts or 0, slew_rate=250
        )
        await runtime.async_refresh()
        assert sensor.native_value == final_state
        assert sensor.available
        assert sensor.extra_state_attributes["pending"] is False
        assert sensor.extra_state_attributes["pending_requested_at"] is None
        coord.client.async_set_export_limit.assert_awaited_once()


async def test_readback_window_intervals_and_repairs(runtime, hass):
    from homeassistant.helpers import issue_registry as ir
    from custom_components.enphase_ev.const import (
        OPT_FAST_POLL_INTERVAL,
        OPT_SLOW_POLL_INTERVAL,
    )

    entry = runtime.coordinator.config_entry
    hass.config_entries.async_update_entry(
        entry,
        options={
            OPT_EXPORT_LIMIT_CONTROLS_ENABLED: True,
            OPT_FAST_POLL_INTERVAL: 45,
            OPT_SLOW_POLL_INTERVAL: 120,
        },
    )
    assert runtime._poll_delay() == 120
    await runtime.async_apply(0, confirm=True)
    started = runtime.pending["started"]
    issue_id = f"export_limit_pending_{entry.entry_id}"
    registry = ir.async_get(hass)
    with patch(
        "custom_components.enphase_ev.export_limit_runtime.time.time",
        return_value=started + 550,
    ):
        assert runtime._poll_delay() == 45
        runtime._publish()
        assert registry.async_get_issue(DOMAIN, issue_id) is None
    with patch(
        "custom_components.enphase_ev.export_limit_runtime.time.time",
        return_value=started + 599,
    ):
        assert runtime._poll_delay() == 1
    with patch(
        "custom_components.enphase_ev.export_limit_runtime.time.time",
        return_value=started + 600,
    ):
        await runtime.async_refresh()
        assert runtime._poll_delay() == 120
        assert runtime.request_status == "unconfirmed"
        issue = registry.async_get_issue(DOMAIN, issue_id)
        assert issue.severity == ir.IssueSeverity.WARNING
        assert issue.translation_placeholders == {"pending_timeout_minutes": "10"}
        # Even a failed read keeps the warning and slower cadence.
        runtime.coordinator.client.async_get_export_limit_settings.side_effect = (
            TimeoutError
        )
        with pytest.raises(ServiceValidationError):
            await runtime.async_refresh()
        assert registry.async_get_issue(DOMAIN, issue_id) is not None
        runtime.coordinator.client.async_get_export_limit_settings.side_effect = None
        runtime.coordinator.client.async_get_export_limit_settings.return_value = (
            payload(free_limit_value=0)
        )
        await runtime.async_refresh()
        assert registry.async_get_issue(DOMAIN, issue_id) is None
    assert runtime.coordinator.client.async_set_export_limit.await_count == 1
    # Turning the feature off clears its warning without issuing a gateway write.
    runtime.pending = {
        "started": started - 601,
        "watts": 0,
        "slew": 6000,
        "gateway": "gateway",
    }
    runtime._publish()
    assert registry.async_get_issue(DOMAIN, issue_id) is not None
    hass.config_entries.async_update_entry(entry, options={})
    runtime.stop()
    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_poll_wakeup_on_submission(runtime):
    await runtime._wait_for_poll(0.001)
    runtime._task = MagicMock()
    await runtime.async_apply(0, confirm=True)
    assert runtime._wake.is_set()
    await runtime._wait_for_poll(10)
    assert not runtime._wake.is_set()
    runtime._task = None


@pytest.mark.parametrize(
    "name",
    [
        "enable_dynamic_limiting",
        "export_limit",
        "reference_value",
        "free_limit_value",
        "slew_rate",
    ],
)
def test_form_requires_complete_unambiguous_configuration(name):
    html = form()
    field = f"info_pel_settings_info[{name}]"
    with pytest.raises(InvalidExportLimitForm):
        parse_form(html.replace(field, "unrelated"), "123")
    with pytest.raises(InvalidExportLimitForm):
        parse_form(form(f'<input name="{field}" value="other">'), "123")


@pytest.mark.parametrize(
    "extra",
    [
        '<form action="/other"></form>',
        '<textarea name="notes">unfinished',
        '<select name="mode"><option selected>unfinished',
        '<input name="foreign" form="other" value="ignore">',
    ],
)
def test_form_rejects_ambiguous_control_ownership(extra):
    with pytest.raises(InvalidExportLimitForm):
        parse_form(form(extra), "123")


def test_select_default_skips_disabled_options():
    fields = parse_form(
        form(
            '<select name="default"><option disabled value="no">No</option><option value="yes">Yes</option></select>'
        ),
        "123",
    )
    assert ("default", "yes") in fields


@pytest.mark.parametrize(
    "name,value",
    [
        ("enable_dynamic_limiting", "true"),
        ("export_limit", "false"),
        ("reference_value", "1"),
        ("reference_value", "unknown"),
        ("free_limit_value", "4000"),
        ("slew_rate", "7000"),
    ],
)
async def test_changed_live_form_blocks_write(runtime, name, value):
    client = runtime.coordinator.client
    fields = parse_form(form(), "123")
    client.async_get_export_limit_form.return_value = [
        (key, value if key == f"info_pel_settings_info[{name}]" else old)
        for key, old in fields
    ]
    with pytest.raises(ServiceValidationError) as err:
        await runtime.async_apply(0, confirm=True)
    assert err.value.translation_key == "export_limit_changed"
    client.async_set_export_limit.assert_not_awaited()
    runtime._store.async_save.assert_not_awaited()


async def test_disabled_live_form_can_enable(runtime):
    client = runtime.coordinator.client
    client.async_get_export_limit_settings.return_value = payload(enable=False)
    client.async_get_export_limit_form.return_value = parse_form(
        form().replace(
            'name="info_pel_settings_info[enable_dynamic_limiting]" value="false"',
            'name="info_pel_settings_info[enable_dynamic_limiting]" value="disable_settings"',
        ),
        "123",
    )
    await runtime.async_apply(0, confirm=True)
    client.async_set_export_limit.assert_awaited_once()


async def test_disable_while_saving_intent_clears_unsent_request(runtime, hass):
    async def disable_before_send():
        hass.config_entries.async_update_entry(
            runtime.coordinator.config_entry, options={}
        )

    runtime._store.async_save.side_effect = disable_before_send

    # Store receives the metadata payload.
    async def save(_):
        await disable_before_send()

    runtime._store.async_save.side_effect = save
    with pytest.raises(ServiceValidationError):
        await runtime.async_apply(0, confirm=True)
    assert runtime.pending is None
    assert runtime._store.async_save.call_args.args == ({"pending": None},)
    runtime.coordinator.client.async_set_export_limit.assert_not_awaited()


async def test_cancel_before_send_clears_durable_intent(runtime):
    runtime._store.async_save.side_effect = [asyncio.CancelledError(), None]
    with pytest.raises(asyncio.CancelledError):
        await runtime.async_apply(0, confirm=True)
    assert runtime.pending is None
    assert runtime._store.async_save.call_args.args == ({"pending": None},)
    runtime.coordinator.client.async_set_export_limit.assert_not_awaited()


async def test_cancel_during_send_retains_uncertain_intent(runtime):
    runtime.coordinator.client.async_set_export_limit.side_effect = (
        asyncio.CancelledError()
    )
    with pytest.raises(asyncio.CancelledError):
        await runtime.async_apply(0, confirm=True)
    assert runtime.pending is not None
    assert runtime.request_status == "unconfirmed"
    assert runtime._store.async_save.call_args.args[0]["pending"] is not None


async def test_unload_clears_pending_repair_and_resume_restores(runtime, hass):
    from homeassistant.helpers import issue_registry as ir

    await runtime.async_apply(0, confirm=True)
    runtime.pending["started"] -= 601
    runtime._publish()
    issue_id = f"export_limit_pending_{runtime.coordinator.config_entry.entry_id}"
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, issue_id) is not None
    runtime.stop()
    assert registry.async_get_issue(DOMAIN, issue_id) is None
    assert runtime.pending is not None
    await runtime.async_start()
    assert registry.async_get_issue(DOMAIN, issue_id) is not None


@pytest.mark.parametrize("protect", [False, True])
@pytest.mark.parametrize("override_middlewares", [False, True])
async def test_http_disconnect_cannot_replay_protected_put(
    protect, override_middlewares, loopback_sockets
):
    """The server sees the body before dropping its response: outcome is uncertain."""
    from aiohttp import web

    received = []
    middleware_calls = []

    async def endpoint(request):
        received.append(await request.text())
        request.transport.close()
        return web.Response()

    async def observer(request, handler):
        middleware_calls.append(request.method)
        return await handler(request)

    app = web.Application()
    app.router.add_put("/pel", endpoint)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with aiohttp.ClientSession(middlewares=(observer,)) as session:
            client = EnphaseEVClient(session, "123", "token", "cookie")
            kwargs = {"middlewares": (observer,)} if override_middlewares else {}
            with pytest.raises(aiohttp.ClientConnectionError):
                await client._text_response(
                    "PUT",
                    f"http://127.0.0.1:{port}/pel",
                    data="limit=0",
                    allow_replay=not protect,
                    allow_redirects=False,
                    **kwargs,
                )
        assert received == ["limit=0"] * (1 if protect else 2)
        assert middleware_calls  # Preserve session and per-request middleware.
    finally:
        await runner.cleanup()


async def test_protected_transport_success_and_auth_failure(loopback_sockets):
    from aiohttp import web

    calls = []

    async def endpoint(request):
        calls.append(request.method)
        return web.Response(status=401 if len(calls) > 1 else 200, text="ok")

    app = web.Application()
    app.router.add_put("/pel", endpoint)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with aiohttp.ClientSession() as session:
            client = EnphaseEVClient(session, "123", "token", "cookie")
            client._reauth_cb = AsyncMock(return_value=True)
            url = f"http://127.0.0.1:{port}/pel"
            result = await client._text_response("PUT", url, allow_replay=False)
            assert result.status == 200
            with pytest.raises(Unauthorized):
                await client._text_response("PUT", url, allow_replay=False)
            client._reauth_cb.assert_not_awaited()
            assert calls == ["PUT", "PUT"]
    finally:
        await runner.cleanup()


@pytest.fixture
def loopback_sockets():
    """Allow only loopback connections for real HTTP transport regression tests."""
    import pytest_socket

    pytest_socket.enable_socket()
    pytest_socket.socket_allow_hosts(["127.0.0.1"], allow_unix_socket=True)
    try:
        yield
    finally:
        pytest_socket.disable_socket(allow_unix_socket=True)


@pytest.mark.parametrize(
    "operation", ["async_prepare_activation_auth", "async_get_export_limit_form"]
)
async def test_export_session_expired_starts_reauth_and_preserves_error(
    runtime, operation
):
    from custom_components.enphase_ev.api_client.errors import ActivationSessionExpired

    getattr(runtime.coordinator.client, operation).side_effect = (
        ActivationSessionExpired("expired")
    )
    entry = runtime.coordinator.config_entry
    with patch.object(type(entry), "async_start_reauth") as reauth:
        with pytest.raises(ServiceValidationError) as raised:
            await runtime.async_prepare()
        assert raised.value.translation_key == "export_limit_session_expired"
        reauth.assert_called_once_with(runtime.coordinator.hass)


async def test_export_options_reports_expired_session(runtime):
    flow = OptionsFlowHandler(runtime.coordinator.config_entry)
    flow.hass = runtime.coordinator.hass
    with patch.object(
        runtime,
        "async_prepare",
        side_effect=ServiceValidationError(
            translation_domain=DOMAIN, translation_key="export_limit_session_expired"
        ),
    ):
        result = await flow.async_step_export_limit()
    assert result["errors"] == {"base": "grid_profile_session_expired"}


@pytest.mark.parametrize("step", ["confirm", "defaults"])
async def test_export_options_session_expiry_has_localized_recovery(runtime, step):
    from homeassistant.helpers.translation import async_get_translations
    from custom_components.enphase_ev.api_client.errors import ActivationSessionExpired

    flow = OptionsFlowHandler(runtime.coordinator.config_entry)
    flow.hass = runtime.coordinator.hass
    await flow.async_step_export_limit()
    result = await flow.async_step_export_limit_set({"limit_watts": 0})
    assert result["step_id"] == "export_limit_confirm"
    assert result["description_placeholders"]["requested"] == "0 W"
    runtime.coordinator.client.async_prepare_activation_auth.side_effect = (
        ActivationSessionExpired("expired")
    )
    entry = runtime.coordinator.config_entry
    with patch.object(type(entry), "async_start_reauth") as reauth:
        result = (
            await flow.async_step_export_limit_confirm({"confirm": True})
            if step == "confirm"
            else await flow.async_step_export_limit_defaults(
                {"limit_watts": 0, "restore_slew_rate": True}
            )
        )
    assert result["errors"] == {"base": "export_limit_session_expired"}
    reauth.assert_called_once_with(runtime.coordinator.hass)
    runtime.coordinator.client.async_set_export_limit.assert_not_awaited()
    translations = await async_get_translations(
        runtime.coordinator.hass, "en", "options", {DOMAIN}
    )
    message = translations[
        f"component.{DOMAIN}.options.error.{result['errors']['base']}"
    ]
    assert "Start reauthentication" in message


async def test_export_permission_denial_does_not_start_reauth(runtime):
    from custom_components.enphase_ev.api import ActivationAccessDenied

    runtime.coordinator.client.async_prepare_activation_auth.side_effect = (
        ActivationAccessDenied("denied")
    )
    entry = runtime.coordinator.config_entry
    with patch.object(type(entry), "async_start_reauth") as reauth:
        with pytest.raises(ServiceValidationError) as raised:
            await runtime.async_prepare()
        assert raised.value.translation_key == "export_limit_unavailable"
        reauth.assert_not_called()


@pytest.mark.parametrize(
    "operation",
    [
        "async_prepare_activation_auth",
        "async_get_export_limit_form",
        "async_set_export_limit",
    ],
)
@pytest.mark.parametrize("failure_kind", ["session", "login_wall"])
async def test_export_apply_session_failure_requests_reauth_without_replay(
    runtime, operation, failure_kind
):
    from custom_components.enphase_ev.api_client.errors import (
        ActivationSessionExpired,
        EnphaseLoginWallUnauthorized,
    )

    failure = (
        ActivationSessionExpired("expired")
        if failure_kind == "session"
        else EnphaseLoginWallUnauthorized(
            endpoint="/site_pel_settings/123", request_label="request"
        )
    )
    getattr(runtime.coordinator.client, operation).side_effect = failure
    entry = runtime.coordinator.config_entry
    with patch.object(type(entry), "async_start_reauth") as reauth:
        with pytest.raises(ServiceValidationError) as raised:
            await runtime.async_apply(0, confirm=True)
        assert raised.value.translation_key == "export_limit_session_expired"
        reauth.assert_called_once_with(runtime.coordinator.hass)
    assert runtime.coordinator.client.async_set_export_limit.await_count == int(
        operation == "async_set_export_limit"
    )
    assert runtime.pending is None


async def test_historical_gateway_settings_resolve_current_dashboard_identity(runtime):
    import copy

    current = payload(
        enable=False, export_limit=False, reference_value=1, slew_rate=6980
    )
    record = current["data"]["gateway_settings"][0]
    record["device_id"] = "300"
    historical = copy.deepcopy(record)
    historical["device_id"] = "200"
    historical["pel_settings_infos"]["slew_rate"] = 0
    current["data"]["gateway_settings"] = [record, historical, {"device_id": "100"}]
    runtime.coordinator.client.async_get_export_limit_settings.return_value = current
    runtime.coordinator.client.devices_details = AsyncMock(
        return_value={
            "envoys": [
                {"id": 300, "serial_number": "current-gateway", "status": "normal"}
            ]
        }
    )
    await runtime.async_refresh()
    assert runtime.snapshot.gateway == "300"
    assert runtime.snapshot.supported
    assert runtime.snapshot.state == "disabled"
    runtime.coordinator.client.devices_details.assert_awaited_once_with("envoy")
    runtime.coordinator.client.async_set_export_limit.assert_not_awaited()
    runtime.coordinator.client.async_get_export_limit_form.side_effect = ValueError(
        "form unavailable"
    )
    with pytest.raises(ServiceValidationError):
        await runtime.async_apply(0, confirm=True)
    runtime.coordinator.client.async_set_export_limit.assert_not_awaited()


@pytest.mark.parametrize(
    "envoys",
    [
        None,
        [],
        [{"id": 3, "serial_number": "a"}, {"id": 4, "serial_number": "b"}],
        [None],
        [{"id": 3, "serial_number": "a", "status": "retired"}],
        [{"id": 3}],
        [{"id": 3, "serial_number": " "}],
        [{"id": True, "serial_number": "a"}],
        [{"id": "invalid", "serial_number": "a"}],
        [{"id": 0, "serial_number": "a"}],
        [{"id": 999, "serial_number": "a"}],
    ],
)
async def test_historical_gateway_settings_never_guess_identity(runtime, envoys):
    settings = payload()
    settings["data"]["gateway_settings"].append({"device_id": "retired"})
    runtime.coordinator.client.async_get_export_limit_settings.return_value = settings
    runtime.coordinator.client.devices_details = AsyncMock(
        return_value={"envoys": envoys}
    )
    await runtime.async_refresh()
    assert runtime.snapshot is None
    runtime.coordinator.client.async_set_export_limit.assert_not_awaited()


@pytest.mark.parametrize("form_mode", ["false", "disable_settings"])
async def test_disabled_default_configuration_can_be_enabled_after_matching_form(
    runtime,
    form_mode,
):
    runtime.coordinator.client.async_get_export_limit_settings.return_value = payload(
        enable=False, export_limit=False, reference_value=1
    )
    fields = parse_form(form(), "123")
    replacements = {
        "enable_dynamic_limiting": form_mode,
        "export_limit": "false",
        "reference_value": "1",
    }
    fields = [
        (
            key,
            replacements.get(
                key.removeprefix("info_pel_settings_info[").removesuffix("]"), value
            ),
        )
        for key, value in fields
    ]
    runtime.coordinator.client.async_get_export_limit_form.return_value = fields
    await runtime.async_apply(1000, confirm=True)
    runtime.coordinator.client.async_set_export_limit.assert_awaited_once_with(
        fields, 1000, 6000.0
    )


def test_gateway_identity_selection_rejects_duplicate_or_foreign_records():
    settings = payload()
    record = settings["data"]["gateway_settings"][0]
    settings["data"]["gateway_settings"].append(dict(record))
    assert parse_settings(settings, "123", gateway_id="gateway") is None
    assert parse_settings(payload(), "other-site", gateway_id="gateway") is None


async def test_historical_gateway_lookup_failure_invalidates_prior_snapshot(runtime):
    await runtime.async_refresh()
    assert runtime.snapshot is not None
    settings = payload()
    settings["data"]["gateway_settings"].append({"device_id": "retired"})
    runtime.coordinator.client.async_get_export_limit_settings.return_value = settings
    runtime.coordinator.client.devices_details = AsyncMock(side_effect=TimeoutError())
    runtime.coordinator.async_update_listeners.reset_mock()
    with pytest.raises(ServiceValidationError):
        await runtime.async_apply(0, confirm=True)
    assert runtime.snapshot is None
    runtime.coordinator.async_update_listeners.assert_called_once()
    runtime.coordinator.client.async_set_export_limit.assert_not_awaited()


async def test_historical_gateway_lookup_missing_payload_blocks_identity(runtime):
    settings = payload()
    settings["data"]["gateway_settings"].append({"device_id": "retired"})
    runtime.coordinator.client.async_get_export_limit_settings.return_value = settings
    runtime.coordinator.client.devices_details = AsyncMock(return_value=None)
    await runtime.async_refresh()
    assert runtime.snapshot is None


async def test_disabled_default_form_stale_slew_still_blocks_write(runtime):
    runtime.coordinator.client.async_get_export_limit_settings.return_value = payload(
        enable=False,
        export_limit=False,
        reference_value=1,
        slew_rate=6980,
        free_limit_value=0,
    )
    replacements = {
        "enable_dynamic_limiting": "false",
        "export_limit": "false",
        "reference_value": "1",
        "slew_rate": "0.0",
        "free_limit_value": "0",
    }
    fields = [
        (
            key,
            replacements.get(
                key.removeprefix("info_pel_settings_info[").removesuffix("]"), value
            ),
        )
        for key, value in parse_form(form(), "123")
    ]
    runtime.coordinator.client.async_get_export_limit_form.return_value = fields
    with pytest.raises(ServiceValidationError) as raised:
        await runtime.async_apply(1000, confirm=True)
    assert raised.value.translation_key == "export_limit_changed"
    runtime.coordinator.client.async_set_export_limit.assert_not_awaited()


@pytest.mark.parametrize("operation", ["async_prepare", "async_apply"])
@pytest.mark.parametrize(
    "failure_stage", ["async_prepare_activation_auth", "async_get_export_limit_form"]
)
async def test_session_rejection_clears_previously_valid_export_snapshot(
    runtime, operation, failure_stage
):
    from custom_components.enphase_ev.api_client.errors import ActivationSessionExpired

    await runtime.async_refresh()
    assert runtime.snapshot is not None
    getattr(runtime.coordinator.client, failure_stage).side_effect = (
        ActivationSessionExpired("expired")
    )
    entry = runtime.coordinator.config_entry
    with patch.object(type(entry), "async_start_reauth"):
        with pytest.raises(ServiceValidationError):
            if operation == "async_apply":
                await runtime.async_apply(0, confirm=True)
            else:
                await runtime.async_prepare()
    assert runtime.snapshot is None
    runtime.coordinator.client.async_set_export_limit.assert_not_awaited()


@pytest.mark.parametrize("write", [True, False])
async def test_pel_http_unauthorized_is_session_expired(write):
    from custom_components.enphase_ev.api_client.errors import ActivationSessionExpired

    client = EnphaseEVClient(SimpleNamespace(), "123", "token", "cookie")
    client._text_response = AsyncMock(side_effect=Unauthorized())
    with pytest.raises(ActivationSessionExpired):
        if write:
            await write_settings(client, parse_form(form(), "123"), 0, 6000)
        else:
            await read_form(client)
    assert client._text_response.await_count == 1


def _zero_slew_form(runtime, *, enabled=False):
    """Model the captured zero-default form without real identifiers or tokens."""
    settings = payload(
        enable=enabled,
        export_limit=enabled,
        reference_value=3 if enabled else 1,
        free_limit_value=0,
        slew_rate=6980,
    )
    settings["data"]["gateway_settings"][0]["device_id"] = "300"
    runtime.coordinator.client.async_get_export_limit_settings.return_value = settings
    runtime.coordinator.client.devices_details = AsyncMock(
        return_value={
            "envoys": [
                {"id": 300, "serial_number": "current-gateway", "status": "normal"}
            ]
        }
    )
    changes = {
        "enable_dynamic_limiting": "false",
        "enable": str(enabled).lower(),
        "export_limit": str(enabled).lower(),
        "reference_value": "3" if enabled else "1",
        "free_limit_value": "0",
        "slew_rate": "0.0",
    }
    fields = [
        (
            key,
            changes.get(
                key.removeprefix("info_pel_settings_info[").removesuffix("]"), value
            ),
        )
        for key, value in parse_form(form(), "123")
    ]
    runtime.coordinator.client.async_get_export_limit_form.return_value = fields
    return settings, fields


async def test_guided_zero_slew_reconciliation_preserves_gateway_rate(runtime):
    settings, fields = _zero_slew_form(runtime)
    expected = await runtime.async_prepare()
    await runtime.async_apply(
        1000, confirm=True, expected=expected, reconcile_zero_slew=True
    )
    runtime.coordinator.client.async_set_export_limit.assert_awaited_once_with(
        fields, 1000, 6980.0
    )
    assert runtime.pending["slew"] == 6980
    assert runtime.request_status == "pending"
    assert runtime.snapshot.state == "disabled"
    settings["data"]["gateway_settings"][0]["pel_settings_infos"].update(
        enable=True, export_limit=True, reference_value=3, free_limit_value=1000
    )
    await runtime.async_refresh()
    assert runtime.pending is None
    assert runtime.request_status == "confirmed"
    assert runtime.coordinator.client.async_set_export_limit.await_count == 1


@pytest.mark.parametrize(
    "case",
    [
        "no_opt_in",
        "no_expected",
        "override",
        "unconfirmed",
        "enabled",
        "dynamic",
        "identity_changed",
        "missing_identity",
        "ambiguous",
        "slew_changed",
    ],
)
async def test_zero_slew_reconciliation_rejects_unsafe_context(runtime, case):
    settings, _ = _zero_slew_form(runtime)
    expected = await runtime.async_prepare()
    kwargs = {"confirm": True, "expected": expected, "reconcile_zero_slew": True}
    if case == "no_opt_in":
        kwargs["reconcile_zero_slew"] = False
    elif case == "no_expected":
        kwargs["expected"] = None
    elif case == "override":
        kwargs["slew_rate"] = 6000
    elif case == "unconfirmed":
        kwargs["confirm"] = False
    elif case in {"enabled", "dynamic", "slew_changed"}:
        settings["data"]["gateway_settings"][0]["pel_settings_infos"].update(
            {
                "enabled": {"enable": True},
                "dynamic": {"enable_dynamic_limiting": True},
                "slew_changed": {"slew_rate": 7000},
            }[case]
        )
    elif case == "identity_changed":
        settings["data"]["gateway_settings"][0]["device_id"] = "400"
        runtime.coordinator.client.devices_details.return_value["envoys"][0]["id"] = 400
    elif case == "missing_identity":
        runtime.coordinator.client.devices_details.return_value = None
    else:
        runtime.coordinator.client.devices_details.return_value["envoys"].append(
            {"id": 400, "serial_number": "other"}
        )
    with pytest.raises(ServiceValidationError):
        await runtime.async_apply(1000, **kwargs)
    runtime.coordinator.client.async_set_export_limit.assert_not_awaited()
    assert runtime.pending is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("slew_rate", "1"),
        ("slew_rate", "-1"),
        ("slew_rate", "nan"),
        ("slew_rate", "inf"),
        ("slew_rate", "invalid"),
        ("free_limit_value", "5"),
        ("export_limit", "true"),
        ("reference_value", "3"),
        ("enable_dynamic_limiting", "true"),
    ],
)
@pytest.mark.parametrize("enabled", [False, True])
async def test_zero_slew_reconciliation_rejects_other_form_disagreements(
    runtime, field, value, enabled
):
    _, fields = _zero_slew_form(runtime, enabled=enabled)
    if enabled and field == "export_limit":
        value = "false"
    elif enabled and field == "reference_value":
        value = "1"
    expected = await runtime.async_prepare()
    runtime.coordinator.client.async_get_export_limit_form.return_value = [
        (key, value if key == f"info_pel_settings_info[{field}]" else current)
        for key, current in fields
    ]
    with pytest.raises(ServiceValidationError) as raised:
        await runtime.async_apply(
            1000, confirm=True, expected=expected, reconcile_zero_slew=True
        )
    assert raised.value.translation_key == "export_limit_changed"
    runtime.coordinator.client.async_set_export_limit.assert_not_awaited()


async def test_guided_confirmation_requires_explicit_gateway_rate_restore(runtime):
    _zero_slew_form(runtime)
    flow = OptionsFlowHandler(runtime.coordinator.config_entry)
    flow.hass = runtime.coordinator.hass
    await flow.async_step_export_limit()
    result = await flow.async_step_export_limit_set({"limit_watts": 1000})
    assert result["description_placeholders"]["requested_slew"] == "6980.0"
    assert result["data_schema"]({"confirm": True})["restore_slew_rate"] is False
    result = await flow.async_step_export_limit_confirm({"confirm": True})
    assert result["errors"]["base"] == "export_limit_changed"
    runtime.coordinator.client.async_set_export_limit.assert_not_awaited()
    result = await flow.async_step_export_limit_confirm(
        {"confirm": True, "restore_slew_rate": True}
    )
    assert result["step_id"] == "export_limit_submitted"
    assert runtime.coordinator.client.async_set_export_limit.await_count == 1


async def test_guided_reconciliation_does_not_offer_to_replace_saved_override(runtime):
    from custom_components.enphase_ev.const import OPT_EXPORT_LIMIT_SLEW_RATE

    _zero_slew_form(runtime)
    entry = runtime.coordinator.config_entry
    runtime.coordinator.hass.config_entries.async_update_entry(
        entry, options={**entry.options, OPT_EXPORT_LIMIT_SLEW_RATE: 6000}
    )
    flow = OptionsFlowHandler(entry)
    flow.hass = runtime.coordinator.hass
    await flow.async_step_export_limit()
    result = await flow.async_step_export_limit_set({"limit_watts": 1000})
    assert result["description_placeholders"]["requested_slew"] == "6000"
    assert "restore_slew_rate" not in result["data_schema"]({"confirm": True})


@pytest.mark.parametrize(
    "name,first,last",
    [("export_limit", "false", "true"), ("reference_value", "1", "3")],
)
def test_live_pel_form_last_checked_radio_is_successful(name, first, last):
    field_name = f"info_pel_settings_info[{name}]"
    html = form().replace(
        f'<input name="{field_name}" value="{last}">',
        f'<input type="radio" name="{field_name}" value="{first}" checked>'
        f'<input name="unrelated" value="keep">'
        f'<input type="radio" name="{field_name}" value="{last}" checked>'
        f'<input type="radio" name="{field_name}" value="unchecked">',
    )
    fields = parse_form(html, "123")
    assert [value for key, value in fields if key == field_name] == [last]
    assert ("unrelated", "keep") in fields


@pytest.mark.parametrize(
    "extra",
    [
        '<input type="hidden" name="info_pel_settings_info[export_limit]" value="true">',
        '<input type="checkbox" name="info_pel_settings_info[export_limit]" value="true" checked>',
    ],
)
def test_radio_selection_does_not_hide_duplicate_nonradio_required_fields(extra):
    from custom_components.enphase_ev.api_client.export_limit_surface import (
        InvalidExportLimitForm,
    )

    html = form().replace(
        '<input name="info_pel_settings_info[export_limit]" value="true">',
        extra
        + '<input type="radio" name="info_pel_settings_info[export_limit]" value="true" checked>'
        '<input type="radio" name="info_pel_settings_info[export_limit]" value="false" checked>',
    )
    with pytest.raises(InvalidExportLimitForm):
        parse_form(html, "123")


def test_disabled_checked_radio_unchecks_previous_without_submitting():
    fields = parse_form(
        form(
            '<input type="radio" name="optional" value="first" checked>'
            '<input type="radio" name="optional" value="last" checked disabled>'
        ),
        "123",
    )
    assert all(key != "optional" for key, _ in fields)


@pytest.mark.parametrize("watts", [None, 1000])
async def test_enabled_zero_slew_reconciliation_preserves_gateway_rate(runtime, watts):
    settings, fields = _zero_slew_form(runtime, enabled=True)
    expected = await runtime.async_prepare()
    await runtime.async_apply(
        watts, confirm=True, expected=expected, reconcile_zero_slew=True
    )
    runtime.coordinator.client.async_set_export_limit.assert_awaited_once_with(
        fields, watts, 6980.0
    )
    assert runtime.pending["slew"] == 6980
    assert runtime.request_status == "pending"
    assert runtime.snapshot.enabled is True
    settings["data"]["gateway_settings"][0]["pel_settings_infos"].update(
        enable=watts is not None,
        export_limit=True,
        reference_value=3,
        free_limit_value=watts or 0,
    )
    await runtime.async_refresh()
    assert runtime.pending is None
    assert runtime.request_status == "confirmed"
    assert runtime.coordinator.client.async_set_export_limit.await_count == 1


@pytest.mark.parametrize("watts", [None, 1000])
async def test_guided_enabled_zero_slew_requires_explicit_restore(runtime, watts):
    _zero_slew_form(runtime, enabled=True)
    flow = OptionsFlowHandler(runtime.coordinator.config_entry)
    flow.hass = runtime.coordinator.hass
    await flow.async_step_export_limit()
    result = (
        await flow.async_step_export_limit_disable()
        if watts is None
        else await flow.async_step_export_limit_set({"limit_watts": watts})
    )
    assert result["data_schema"]({"confirm": True})["restore_slew_rate"] is False
    result = await flow.async_step_export_limit_confirm({"confirm": True})
    assert result["errors"]["base"] == "export_limit_changed"
    runtime.coordinator.client.async_set_export_limit.assert_not_awaited()
    result = await flow.async_step_export_limit_confirm(
        {"confirm": True, "restore_slew_rate": True}
    )
    assert result["step_id"] == "export_limit_submitted"
    assert runtime.coordinator.client.async_set_export_limit.await_args.args[1:] == (
        watts,
        6980.0,
    )


async def test_guided_disable_preserves_gateway_slew_despite_saved_override(runtime):
    from custom_components.enphase_ev.const import OPT_EXPORT_LIMIT_SLEW_RATE

    _zero_slew_form(runtime, enabled=True)
    entry = runtime.coordinator.config_entry
    runtime.coordinator.hass.config_entries.async_update_entry(
        entry, options={**entry.options, OPT_EXPORT_LIMIT_SLEW_RATE: 6000}
    )
    flow = OptionsFlowHandler(entry)
    flow.hass = runtime.coordinator.hass
    await flow.async_step_export_limit()
    result = await flow.async_step_export_limit_disable()
    assert result["description_placeholders"]["requested_slew"] == "6980.0"
    assert result["data_schema"]({"confirm": True})["restore_slew_rate"] is False
    await flow.async_step_export_limit_confirm(
        {"confirm": True, "restore_slew_rate": True}
    )
    assert runtime.coordinator.client.async_set_export_limit.await_args.args[1:] == (
        None,
        6980.0,
    )


async def test_saved_pending_intent_published_before_write(runtime):
    """Activity can capture the target before immediate readback clears it."""
    observations = []
    runtime.coordinator.async_update_listeners.side_effect = (
        lambda: observations.append(dict(runtime.attributes()))
    )

    async def write(*_args):
        assert runtime._store.async_save.await_count == 1
        assert observations[-1]["request_status"] == "pending"
        assert observations[-1]["requested_watts"] == 0
        runtime.coordinator.client.async_get_export_limit_settings.return_value = (
            payload(free_limit_value=0)
        )

    runtime.coordinator.client.async_set_export_limit.side_effect = write
    await runtime.async_apply(0, confirm=True)
    assert observations[-1]["request_status"] == "confirmed"
    assert observations[-1]["requested_watts"] is None
