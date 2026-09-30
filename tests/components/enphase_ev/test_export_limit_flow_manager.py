"""Exercise Export Limit options through Home Assistant's real flow manager."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.enphase_ev.const import OPT_EXPORT_LIMIT_CONTROLS_ENABLED
from custom_components.enphase_ev.export_limit_runtime import (
    ExportLimitRuntime,
    ExportLimitSnapshot,
)
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData


@pytest.mark.parametrize(
    ("action", "watts"),
    [
        ("export_limit_defaults", None),
        ("export_limit_set", 0),
        ("export_limit_set", 3000),
        ("export_limit_disable", None),
    ],
)
async def test_export_limit_action_menu_routes_through_flow_manager(
    hass, config_entry, action, watts
):
    hass.config_entries.async_update_entry(
        config_entry, options={OPT_EXPORT_LIMIT_CONTROLS_ENABLED: True}
    )
    coord = SimpleNamespace(hass=hass, config_entry=config_entry, site_id="test-site")
    runtime = ExportLimitRuntime(coord)
    runtime.async_prepare = AsyncMock(
        return_value=ExportLimitSnapshot(
            "gateway", False, False, False, 1.0, 100.0, 1000.0
        )
    )
    runtime.async_apply = AsyncMock()
    coord.export_limit_runtime = runtime
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "advanced"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "export_limit"}
    )
    assert result["type"] == "menu"
    assert result["step_id"] == "export_limit_action"
    assert result["menu_options"] == [
        "export_limit_defaults",
        "export_limit_set",
        "export_limit_disable",
    ]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": action}
    )
    assert result["type"] == "form"
    assert result["step_id"] == (
        "export_limit_confirm" if action == "export_limit_disable" else action
    )
    if action == "export_limit_set":
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"limit_watts": watts}
        )
        assert result["step_id"] == "export_limit_confirm"
        assert result["description_placeholders"]["requested"] == f"{watts} W"
    runtime.async_prepare.assert_awaited_once()
    runtime.async_apply.assert_not_awaited()
