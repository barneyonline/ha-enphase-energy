from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enphase_ev.api import (
    AuthTokens,
    ChargerInfo,
    EnlightenAuthMFARequired,
    SiteInfo,
)
from custom_components.enphase_ev.config_flow import (
    CONF_TYPE_IQEVSE,
    EnphaseEVConfigFlow,
)
from custom_components.enphase_ev.const import (
    CONF_EMAIL,
    CONF_INCLUDE_INVERTERS,
    CONF_PASSWORD,
    CONF_REMEMBER_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_SERIALS,
    CONF_SITE_ID,
    CONF_SITE_NAME,
    CONF_SITE_ONLY,
    DOMAIN,
)


@pytest.mark.asyncio
async def test_reconfigure_login_entry_shows_user_form(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: "123456",
            CONF_EMAIL: "user@example.com",
            CONF_REMEMBER_PASSWORD: True,
            CONF_PASSWORD: "secret",
        },
    )
    entry.add_to_hass(hass)

    flow = EnphaseEVConfigFlow()
    flow.hass = hass
    flow.context = {
        "source": config_entries.SOURCE_RECONFIGURE,
        "entry_id": entry.entry_id,
    }

    result = await flow.async_step_reconfigure()

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


@pytest.mark.asyncio
async def test_reconfigure_manual_entry_aborts(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "123456"},
    )
    entry.add_to_hass(hass)

    flow = EnphaseEVConfigFlow()
    flow.hass = hass
    flow.context = {
        "source": config_entries.SOURCE_RECONFIGURE,
        "entry_id": entry.entry_id,
    }

    result = await flow.async_step_reconfigure()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "manual_mode_removed"


@pytest.mark.asyncio
async def test_reauth_manual_entry_aborts(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "123456"},
    )
    entry.add_to_hass(hass)

    flow = EnphaseEVConfigFlow()
    flow.hass = hass
    flow.context = {
        "source": config_entries.SOURCE_REAUTH,
        "entry_id": entry.entry_id,
    }

    result = await flow.async_step_reauth({})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "manual_mode_removed"


@pytest.mark.asyncio
async def test_reconfigure_skips_site_selection(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="12345",
        data={
            CONF_SITE_ID: "12345",
            CONF_SITE_NAME: "Garage Site",
            CONF_EMAIL: "user@example.com",
            CONF_REMEMBER_PASSWORD: True,
            CONF_PASSWORD: "secret",
        },
    )
    entry.add_to_hass(hass)

    flow = EnphaseEVConfigFlow()
    flow.hass = hass
    flow.context = {
        "source": config_entries.SOURCE_RECONFIGURE,
        "entry_id": entry.entry_id,
    }

    tokens = AuthTokens(
        cookie="jar=1",
        session_id="sid123",
        access_token="token123",
        token_expires_at=1_700_000_000,
    )
    sites = [
        SiteInfo(site_id="12345", name="Garage Site"),
        SiteInfo(site_id="67890", name="Backup Site"),
    ]
    chargers = [ChargerInfo(serial="EV123", name="Driveway Charger")]

    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_authenticate",
            AsyncMock(return_value=(tokens, sites)),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_chargers",
            AsyncMock(return_value=chargers),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_devices_inventory",
            AsyncMock(return_value={"result": []}),
        ),
    ):
        result = await flow.async_step_reconfigure()
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"

        result = await flow.async_step_user(
            {
                CONF_EMAIL: "user@example.com",
                CONF_PASSWORD: "secret",
                CONF_REMEMBER_PASSWORD: True,
            }
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "devices"


@pytest.mark.asyncio
async def test_reconfigure_allows_disabling_all_devices(hass, monkeypatch) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="12345",
        data={
            CONF_SITE_ID: "12345",
            CONF_SITE_NAME: "Garage Site",
            CONF_EMAIL: "user@example.com",
            CONF_REMEMBER_PASSWORD: True,
            CONF_PASSWORD: "secret",
            CONF_SERIALS: ["EV123"],
        },
    )
    entry.add_to_hass(hass)

    flow = EnphaseEVConfigFlow()
    flow.hass = hass
    flow.context = {
        "source": config_entries.SOURCE_RECONFIGURE,
        "entry_id": entry.entry_id,
    }

    tokens = AuthTokens(
        cookie="jar=1",
        session_id="sid123",
        access_token="token123",
        token_expires_at=1_700_000_000,
    )
    sites = [SiteInfo(site_id="12345", name="Garage Site")]
    chargers = [ChargerInfo(serial="EV123", name="Driveway Charger")]

    updated: dict[str, object] = {}
    monkeypatch.setattr(
        hass.config_entries,
        "async_update_entry",
        lambda entry=None, **kwargs: updated.update(kwargs),
    )
    monkeypatch.setattr(
        hass.config_entries, "async_reload", AsyncMock(return_value=True)
    )

    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_authenticate",
            AsyncMock(return_value=(tokens, sites)),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_chargers",
            AsyncMock(return_value=chargers),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_devices_inventory",
            AsyncMock(return_value={"result": []}),
        ),
    ):
        result = await flow.async_step_reconfigure()
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"

        result = await flow.async_step_user(
            {
                CONF_EMAIL: "user@example.com",
                CONF_PASSWORD: "secret",
                CONF_REMEMBER_PASSWORD: True,
            }
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "devices"

        result = await flow.async_step_devices(
            {CONF_TYPE_IQEVSE: False, CONF_SCAN_INTERVAL: 60}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert updated["data"][CONF_SERIALS] == []
    assert updated["data"][CONF_SITE_ONLY] is True
    assert updated["data"][CONF_INCLUDE_INVERTERS] is False


@pytest.mark.asyncio
async def test_reconfigure_wrong_account_abort_has_placeholders(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: "12345",
            CONF_SITE_NAME: "Garage Site",
            CONF_EMAIL: "user@example.com",
            CONF_REMEMBER_PASSWORD: True,
        },
    )
    entry.add_to_hass(hass)

    flow = EnphaseEVConfigFlow()
    flow.hass = hass
    flow.context = {
        "source": config_entries.SOURCE_RECONFIGURE,
        "entry_id": entry.entry_id,
    }

    tokens = AuthTokens(
        cookie="jar=1",
        session_id="sid123",
        access_token="token123",
        token_expires_at=1_700_000_000,
    )
    sites = [
        SiteInfo(site_id="12345", name="Garage Site"),
        SiteInfo(site_id="67890", name="Backup Site"),
    ]
    chargers = [ChargerInfo(serial="EV999", name="Workshop Charger")]

    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_authenticate",
            AsyncMock(return_value=(tokens, sites)),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_chargers",
            AsyncMock(return_value=chargers),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_devices_inventory",
            AsyncMock(return_value={"result": []}),
        ),
    ):
        # Kick off the reconfigure flow and authenticate
        result = await flow.async_step_reconfigure()
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"

        result = await flow.async_step_user(
            {
                CONF_EMAIL: "user@example.com",
                CONF_PASSWORD: "secret",
                CONF_REMEMBER_PASSWORD: True,
            }
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "devices"

        # Simulate selecting a different site before finalizing
        flow._selected_site_id = "67890"

        result = await flow.async_step_devices(
            {CONF_TYPE_IQEVSE: True, CONF_SCAN_INTERVAL: 60}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    placeholders = result.get("description_placeholders")
    assert placeholders == {
        "configured_label": "Garage Site (12345)",
        "requested_label": "Backup Site (67890)",
    }


@pytest.mark.asyncio
async def test_reauth_skips_site_selection(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: "12345",
            CONF_SITE_NAME: "Garage Site",
            CONF_EMAIL: "user@example.com",
            CONF_REMEMBER_PASSWORD: True,
            CONF_PASSWORD: "secret",
        },
    )
    entry.add_to_hass(hass)

    flow = EnphaseEVConfigFlow()
    flow.hass = hass
    flow.context = {
        "source": config_entries.SOURCE_REAUTH,
        "entry_id": entry.entry_id,
    }

    tokens = AuthTokens(
        cookie="jar=1",
        session_id="sid123",
        access_token="token123",
        token_expires_at=1_700_000_000,
    )
    sites = [
        SiteInfo(site_id="12345", name="Garage Site"),
        SiteInfo(site_id="67890", name="Backup Site"),
    ]
    chargers = [ChargerInfo(serial="EV123", name="Driveway Charger")]

    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_authenticate",
            AsyncMock(return_value=(tokens, sites)),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_chargers",
            AsyncMock(return_value=chargers),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_devices_inventory",
            AsyncMock(return_value={"result": []}),
        ),
    ):
        result = await flow.async_step_reauth({})
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"

        result = await flow.async_step_user(
            {
                CONF_EMAIL: "user@example.com",
                CONF_PASSWORD: "secret",
                CONF_REMEMBER_PASSWORD: True,
            }
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "devices"


@pytest.mark.asyncio
async def test_reauth_flow_manager_submit_routes_to_user_step(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: "12345",
            CONF_SITE_NAME: "Garage Site",
            CONF_EMAIL: "user@example.com",
            CONF_REMEMBER_PASSWORD: True,
            CONF_PASSWORD: "secret",
        },
    )
    entry.add_to_hass(hass)

    tokens = AuthTokens(
        cookie="jar=1",
        session_id="sid123",
        access_token="token123",
        token_expires_at=1_700_000_000,
    )
    sites = [
        SiteInfo(site_id="12345", name="Garage Site"),
        SiteInfo(site_id="67890", name="Backup Site"),
    ]
    chargers = [ChargerInfo(serial="EV123", name="Driveway Charger")]

    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_authenticate",
            AsyncMock(return_value=(tokens, sites)),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_chargers",
            AsyncMock(return_value=chargers),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_devices_inventory",
            AsyncMock(return_value={"result": []}),
        ),
    ):
        init = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_REAUTH,
                "entry_id": entry.entry_id,
            },
        )
        assert init["type"] is FlowResultType.FORM
        assert init["step_id"] == "user"

        result = await hass.config_entries.flow.async_configure(
            init["flow_id"],
            {
                CONF_EMAIL: "user@example.com",
                CONF_PASSWORD: "secret",
                CONF_REMEMBER_PASSWORD: True,
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "devices"


@pytest.mark.asyncio
@pytest.mark.filterwarnings(
    "ignore:It is recommended to use web.AppKey instances for keys\\.:aiohttp.web_exceptions.NotAppKeyWarning"
)
async def test_reauth_allows_empty_device_selection(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="12345",
        data={
            CONF_SITE_ID: "12345",
            CONF_SITE_NAME: "Garage Site",
            CONF_EMAIL: "user@example.com",
            CONF_REMEMBER_PASSWORD: True,
            CONF_PASSWORD: "secret",
            CONF_SERIALS: ["EV123"],
        },
    )
    entry.add_to_hass(hass)

    flow = EnphaseEVConfigFlow()
    flow.hass = hass
    flow.context = {
        "source": config_entries.SOURCE_REAUTH,
        "entry_id": entry.entry_id,
    }

    tokens = AuthTokens(
        cookie="jar=1",
        session_id="sid123",
        access_token="token123",
        token_expires_at=1_700_000_000,
    )
    sites = [SiteInfo(site_id="12345", name="Garage Site")]
    chargers = [ChargerInfo(serial="EV123", name="Driveway Charger")]

    with (
        patch.object(hass.config_entries, "async_reload", AsyncMock()) as mock_reload,
        patch(
            "custom_components.enphase_ev.config_flow.async_authenticate",
            AsyncMock(return_value=(tokens, sites)),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_chargers",
            AsyncMock(return_value=chargers),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_devices_inventory",
            AsyncMock(return_value={"result": []}),
        ),
    ):
        result = await flow.async_step_reauth({})
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"

        result = await flow.async_step_user(
            {
                CONF_EMAIL: "user@example.com",
                CONF_PASSWORD: "secret",
                CONF_REMEMBER_PASSWORD: True,
            }
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "devices"

        result = await flow.async_step_devices(
            {CONF_TYPE_IQEVSE: False, CONF_SCAN_INTERVAL: 60}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    mock_reload.assert_awaited_once_with(entry.entry_id)


@pytest.mark.asyncio
async def test_reauth_with_mfa(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: "12345",
            CONF_SITE_NAME: "Garage Site",
            CONF_EMAIL: "user@example.com",
            CONF_REMEMBER_PASSWORD: True,
            CONF_PASSWORD: "secret",
        },
    )
    entry.add_to_hass(hass)

    flow = EnphaseEVConfigFlow()
    flow.hass = hass
    flow.context = {
        "source": config_entries.SOURCE_REAUTH,
        "entry_id": entry.entry_id,
    }

    mfa_tokens = AuthTokens(cookie="jar=1", raw_cookies={"login_otp_nonce": "nonce"})
    tokens = AuthTokens(
        cookie="jar=2",
        session_id="sid123",
        access_token="token123",
        token_expires_at=1_700_000_000,
    )
    sites = [
        SiteInfo(site_id="12345", name="Garage Site"),
        SiteInfo(site_id="67890", name="Backup Site"),
    ]
    chargers = [ChargerInfo(serial="EV123", name="Driveway Charger")]

    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_authenticate",
            side_effect=EnlightenAuthMFARequired("mfa", tokens=mfa_tokens),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_resend_login_otp",
            AsyncMock(return_value=mfa_tokens),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_validate_login_otp",
            AsyncMock(return_value=(tokens, sites)),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_chargers",
            AsyncMock(return_value=chargers),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_devices_inventory",
            AsyncMock(return_value={"result": []}),
        ),
    ):
        result = await flow.async_step_reauth({})
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"

        result = await flow.async_step_user(
            {
                CONF_EMAIL: "user@example.com",
                CONF_PASSWORD: "secret",
                CONF_REMEMBER_PASSWORD: True,
            }
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "mfa"

        result = await flow.async_step_mfa({"otp": "123456"})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "devices"
