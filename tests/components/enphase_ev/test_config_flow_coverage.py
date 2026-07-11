from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
import voluptuous as vol
from voluptuous.schema_builder import Optional as VolOptional
from voluptuous.schema_builder import Required as VolRequired
from homeassistant import config_entries
from homeassistant.const import UnitOfEnergy
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enphase_ev import config_flow
from custom_components.enphase_ev.api import (
    AuthTokens,
    ChargerInfo,
    EnlightenAuthInvalidCredentials,
    EnlightenAuthInvalidOTP,
    EnlightenAuthMFARequired,
    EnlightenAuthOTPBlocked,
    EnlightenAuthTooManySessions,
    EnlightenAuthUnavailable,
    SiteInfo,
)
from custom_components.enphase_ev.config_flow import (
    CONF_OTP,
    CONF_RESEND_CODE,
    CONF_TYPE_AC_BATTERY,
    CONF_TYPE_ENCHARGE,
    CONF_TYPE_ENVOY,
    CONF_TYPE_HEATPUMP,
    CONF_TYPE_IQEVSE,
    CONF_TYPE_MICROINVERTER,
    CONF_MIGRATION_BACKUP_CONFIRMED,
    CONF_MIGRATION_CONFIRM_REASSIGN,
    CONF_MIGRATION_DISABLE_ARCHIVED,
    CONF_GRID_PROFILE_COMMONLY_USED,
    CONF_GRID_PROFILE_CONFIRM_APPLY,
    CONF_GRID_PROFILE_ID,
    CONF_GRID_PROFILE_REGION,
    EnphaseEVConfigFlow,
    OptionsFlowHandler,
)
from custom_components.enphase_ev.const import (
    CONF_AUTH_BLOCK_REASON,
    CONF_AUTH_BLOCKED_UNTIL,
    CONF_AUTH_REFRESH_SUSPENDED_UNTIL,
    CONF_COOKIE,
    CONF_EAUTH,
    CONF_EMAIL,
    CONF_HEATPUMP_DISCOVERY_HANDLED,
    CONF_INCLUDE_INVERTERS,
    CONF_PASSWORD,
    CONF_REMEMBER_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_SELECTED_TYPE_KEYS,
    CONF_SESSION_ID,
    CONF_SITE_ID,
    CONF_SERIALS,
    CONF_SITE_ONLY,
    CONF_ACCESS_TOKEN,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_API_TIMEOUT,
    MAX_POLL_INTERVAL,
    MAX_SESSION_HISTORY_INTERVAL_MIN,
    MIN_API_TIMEOUT,
    MIN_FAST_POLL_INTERVAL,
    MIN_SESSION_HISTORY_INTERVAL_MIN,
    MIN_SLOW_POLL_INTERVAL,
    OPT_API_TIMEOUT,
    OPT_BATTERY_SCHEDULES_ENABLED,
    OPT_DEGRADED_SERVICE_REPAIR_ISSUES,
    OPT_FAST_POLL_INTERVAL,
    OPT_FAST_WHILE_STREAMING,
    OPT_NOMINAL_VOLTAGE,
    OPT_SESSION_HISTORY_INTERVAL,
    OPT_SLOW_POLL_INTERVAL,
    OPT_SCHEDULE_SYNC_ENABLED,
    OPT_WEATHER_ENABLED,
)
from custom_components.enphase_ev.envoy_history import migration_target_unique_id
from custom_components.enphase_ev.envoy_history import skip_option_value
from custom_components.enphase_ev.envoy_history import EnvoyHistoryExecutionError
from custom_components.enphase_ev.envoy_history import EnvoyHistoryMapping
from custom_components.enphase_ev.envoy_history import EnvoyHistoryValidation
from custom_components.enphase_ev.grid_profile_runtime import (
    ALL_PROFILES_OPTION,
    ActivationRegion,
    COMMONLY_USED_OPTION,
    GatewayGridProfileTarget,
    GridProfile,
    GridProfileRuntime,
    SUPPORT_CONFIRMED,
    SUPPORT_DENIED,
    SUPPORT_UNAVAILABLE,
)

TOKENS = AuthTokens(
    cookie="jar=1",
    session_id="sid-123",
    access_token="token-abc",
    token_expires_at=1_700_000_000,
)


def _make_flow(hass) -> EnphaseEVConfigFlow:
    flow = EnphaseEVConfigFlow()
    flow.hass = hass
    flow.context = {}
    return flow


def _add_registry_sensor(
    hass,
    *,
    entry: MockConfigEntry,
    platform: str,
    unique_id: str,
    object_id: str,
    state: str | None,
    attrs: dict[str, object] | None,
) -> str:
    ent_reg = er.async_get(hass)
    reg_entry = ent_reg.async_get_or_create(
        "sensor",
        platform,
        unique_id,
        config_entry=entry,
        suggested_object_id=object_id,
    )
    if state is not None:
        hass.states.async_set(reg_entry.entity_id, state, attrs or {})
    return reg_entry.entity_id


def _patch_entry_lookup(monkeypatch, hass, *entries: MockConfigEntry) -> None:
    original = hass.config_entries.async_get_entry
    lookup = {entry.entry_id: entry for entry in entries}
    monkeypatch.setattr(
        hass.config_entries,
        "async_get_entry",
        lambda entry_id: lookup.get(entry_id) or original(entry_id),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (EnlightenAuthInvalidCredentials(), "invalid_auth"),
        (EnlightenAuthTooManySessions(), "too_many_active_sessions"),
        (EnlightenAuthUnavailable(), "service_unavailable"),
        (ValueError("boom"), "unknown"),
    ],
)
async def test_user_step_handles_auth_errors(hass, exc, expected) -> None:
    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_authenticate",
            side_effect=exc,
        ),
    ):
        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            init["flow_id"],
            {
                CONF_EMAIL: " user@example.com ",
                CONF_PASSWORD: "secret",
                CONF_REMEMBER_PASSWORD: True,
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": expected}
    hass.config_entries.flow.async_abort(result["flow_id"])


@pytest.mark.asyncio
async def test_user_step_mfa_required_starts_mfa_step(hass) -> None:
    mfa_tokens = AuthTokens(cookie="jar=1", raw_cookies={"login_otp_nonce": "nonce"})

    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_authenticate",
            side_effect=EnlightenAuthMFARequired("mfa", tokens=mfa_tokens),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_resend_login_otp",
            AsyncMock(return_value=mfa_tokens),
        ),
    ):
        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            init["flow_id"],
            {
                CONF_EMAIL: "user@example.com",
                CONF_PASSWORD: "secret",
                CONF_REMEMBER_PASSWORD: False,
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "mfa"
    flow = hass.config_entries.flow._progress[result["flow_id"]]
    assert flow._mfa_tokens == mfa_tokens


@pytest.mark.asyncio
async def test_user_step_mfa_required_without_tokens(hass) -> None:
    with patch(
        "custom_components.enphase_ev.config_flow.async_authenticate",
        side_effect=EnlightenAuthMFARequired("mfa"),
    ):
        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            init["flow_id"],
            {
                CONF_EMAIL: "user@example.com",
                CONF_PASSWORD: "secret",
                CONF_REMEMBER_PASSWORD: True,
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "mfa_required"}
    hass.config_entries.flow.async_abort(result["flow_id"])


@pytest.mark.asyncio
async def test_mfa_step_validates_otp(hass) -> None:
    mfa_tokens = AuthTokens(cookie="jar=1", raw_cookies={"login_otp_nonce": "nonce"})
    sites = [
        SiteInfo(site_id="12345", name="Garage"),
        SiteInfo(site_id="67890", name="Backup"),
    ]

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
            AsyncMock(return_value=(TOKENS, sites)),
        ),
    ):
        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            init["flow_id"],
            {
                CONF_EMAIL: "user@example.com",
                CONF_PASSWORD: "secret",
                CONF_REMEMBER_PASSWORD: False,
            },
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "mfa"

        result = await hass.config_entries.flow.async_configure(
            init["flow_id"], {"otp": "123456"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "site"


@pytest.mark.asyncio
async def test_mfa_step_invalid_otp(hass) -> None:
    mfa_tokens = AuthTokens(cookie="jar=1", raw_cookies={"login_otp_nonce": "nonce"})

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
            AsyncMock(side_effect=EnlightenAuthInvalidOTP()),
        ),
    ):
        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            init["flow_id"],
            {
                CONF_EMAIL: "user@example.com",
                CONF_PASSWORD: "secret",
                CONF_REMEMBER_PASSWORD: False,
            },
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "mfa"

        result = await hass.config_entries.flow.async_configure(
            init["flow_id"], {"otp": "000000"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "mfa"
    assert result["errors"] == {"base": "otp_invalid"}


@pytest.mark.asyncio
async def test_mfa_step_resend_code(hass) -> None:
    mfa_tokens = AuthTokens(cookie="jar=1", raw_cookies={"login_otp_nonce": "nonce"})
    resent_tokens = AuthTokens(
        cookie="jar=2", raw_cookies={"login_otp_nonce": "nonce2"}
    )

    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_authenticate",
            side_effect=EnlightenAuthMFARequired("mfa", tokens=mfa_tokens),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_resend_login_otp",
            AsyncMock(side_effect=[mfa_tokens, resent_tokens]),
        ),
    ):
        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            init["flow_id"],
            {
                CONF_EMAIL: "user@example.com",
                CONF_PASSWORD: "secret",
                CONF_REMEMBER_PASSWORD: False,
            },
        )
        flow = hass.config_entries.flow._progress[result["flow_id"]]
        flow._mfa_resend_available_at = 0

        result = await hass.config_entries.flow.async_configure(
            init["flow_id"], {"resend_code": True}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "mfa"
    assert flow._mfa_tokens == resent_tokens


@pytest.mark.asyncio
async def test_mfa_step_resend_wait(hass) -> None:
    mfa_tokens = AuthTokens(cookie="jar=1", raw_cookies={"login_otp_nonce": "nonce"})

    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_authenticate",
            side_effect=EnlightenAuthMFARequired("mfa", tokens=mfa_tokens),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_resend_login_otp",
            AsyncMock(return_value=mfa_tokens),
        ) as resend_mock,
    ):
        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            init["flow_id"],
            {
                CONF_EMAIL: "user@example.com",
                CONF_PASSWORD: "secret",
                CONF_REMEMBER_PASSWORD: False,
            },
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "mfa"

        result = await hass.config_entries.flow.async_configure(
            init["flow_id"], {"resend_code": True}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "mfa"
    assert result["errors"] == {"base": "resend_wait"}
    assert resend_mock.await_count == 1


@pytest.mark.asyncio
async def test_mfa_step_blocked_otp(hass) -> None:
    mfa_tokens = AuthTokens(cookie="jar=1", raw_cookies={"login_otp_nonce": "nonce"})

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
            AsyncMock(side_effect=EnlightenAuthOTPBlocked()),
        ),
    ):
        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            init["flow_id"],
            {
                CONF_EMAIL: "user@example.com",
                CONF_PASSWORD: "secret",
                CONF_REMEMBER_PASSWORD: False,
            },
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "mfa"

        result = await hass.config_entries.flow.async_configure(
            init["flow_id"], {"otp": "123456"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "mfa"
    assert result["errors"] == {"base": "otp_blocked"}


@pytest.mark.asyncio
async def test_mfa_step_without_state_aborts(hass) -> None:
    flow = _make_flow(hass)
    result = await flow.async_step_mfa()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unknown"


@pytest.mark.asyncio
async def test_mfa_step_requires_otp(hass) -> None:
    flow = _make_flow(hass)
    flow._mfa_tokens = AuthTokens(
        cookie="jar=1", raw_cookies={"login_otp_nonce": "nonce"}
    )
    flow._email = "user@example.com"

    result = await flow.async_step_mfa({})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "otp_required"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (EnlightenAuthOTPBlocked(), "otp_blocked"),
        (EnlightenAuthUnavailable(), "service_unavailable"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_mfa_step_resend_errors(hass, exc, expected) -> None:
    flow = _make_flow(hass)
    flow._mfa_tokens = AuthTokens(
        cookie="jar=1", raw_cookies={"login_otp_nonce": "nonce"}
    )
    flow._email = "user@example.com"
    flow._mfa_resend_available_at = None

    with patch(
        "custom_components.enphase_ev.config_flow.async_resend_login_otp",
        AsyncMock(side_effect=exc),
    ):
        result = await flow.async_step_mfa({CONF_RESEND_CODE: True})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}


@pytest.mark.asyncio
async def test_mfa_step_resend_invalid_auth_restarts(hass) -> None:
    flow = _make_flow(hass)
    flow._mfa_tokens = AuthTokens(
        cookie="jar=1", raw_cookies={"login_otp_nonce": "nonce"}
    )
    flow._email = "user@example.com"
    flow._mfa_resend_available_at = None

    with patch(
        "custom_components.enphase_ev.config_flow.async_resend_login_otp",
        AsyncMock(side_effect=EnlightenAuthInvalidCredentials()),
    ):
        result = await flow.async_step_mfa({CONF_RESEND_CODE: True})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "invalid_auth"}


@pytest.mark.asyncio
async def test_mfa_step_resend_too_many_sessions_shows_error(hass) -> None:
    flow = _make_flow(hass)
    flow._mfa_tokens = AuthTokens(
        cookie="jar=1", raw_cookies={"login_otp_nonce": "nonce"}
    )
    flow._email = "user@example.com"
    flow._mfa_resend_available_at = None

    with patch(
        "custom_components.enphase_ev.config_flow.async_resend_login_otp",
        AsyncMock(side_effect=EnlightenAuthTooManySessions()),
    ):
        result = await flow.async_step_mfa({CONF_RESEND_CODE: True})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "mfa"
    assert result["errors"] == {"base": "too_many_active_sessions"}


@pytest.mark.asyncio
async def test_mfa_step_auto_send_invalid_auth_restarts(hass) -> None:
    flow = _make_flow(hass)
    flow._mfa_tokens = AuthTokens(
        cookie="jar=1", raw_cookies={"login_otp_nonce": "nonce"}
    )
    flow._email = "user@example.com"

    with patch(
        "custom_components.enphase_ev.config_flow.async_resend_login_otp",
        AsyncMock(side_effect=EnlightenAuthInvalidCredentials()),
    ):
        result = await flow.async_step_mfa()

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "invalid_auth"}


@pytest.mark.asyncio
async def test_send_mfa_code_missing_state_returns_unknown(hass) -> None:
    flow = _make_flow(hass)

    result = await flow._send_mfa_code()

    assert result == {"base": "unknown"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (EnlightenAuthUnavailable(), "service_unavailable"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_mfa_step_validate_errors(hass, exc, expected) -> None:
    flow = _make_flow(hass)
    flow._mfa_tokens = AuthTokens(
        cookie="jar=1", raw_cookies={"login_otp_nonce": "nonce"}
    )
    flow._email = "user@example.com"

    with patch(
        "custom_components.enphase_ev.config_flow.async_validate_login_otp",
        AsyncMock(side_effect=exc),
    ):
        result = await flow.async_step_mfa({CONF_OTP: "123456"})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}


@pytest.mark.asyncio
async def test_mfa_step_validate_invalid_auth_restarts(hass) -> None:
    flow = _make_flow(hass)
    flow._mfa_tokens = AuthTokens(
        cookie="jar=1", raw_cookies={"login_otp_nonce": "nonce"}
    )
    flow._email = "user@example.com"

    with patch(
        "custom_components.enphase_ev.config_flow.async_validate_login_otp",
        AsyncMock(side_effect=EnlightenAuthInvalidCredentials()),
    ):
        result = await flow.async_step_mfa({CONF_OTP: "123456"})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "invalid_auth"}


@pytest.mark.asyncio
async def test_mfa_step_validate_too_many_sessions_shows_error(hass) -> None:
    flow = _make_flow(hass)
    flow._mfa_tokens = AuthTokens(
        cookie="jar=1", raw_cookies={"login_otp_nonce": "nonce"}
    )
    flow._email = "user@example.com"

    with patch(
        "custom_components.enphase_ev.config_flow.async_validate_login_otp",
        AsyncMock(side_effect=EnlightenAuthTooManySessions()),
    ):
        result = await flow.async_step_mfa({CONF_OTP: "123456"})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "mfa"
    assert result["errors"] == {"base": "too_many_active_sessions"}


@pytest.mark.asyncio
async def test_user_step_single_site_shortcuts_to_devices(hass) -> None:
    site = SiteInfo(site_id="12345", name="Garage Site")
    chargers = [ChargerInfo(serial="EV123", name="Driveway")]

    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_authenticate",
            AsyncMock(return_value=(TOKENS, [site])),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_chargers",
            AsyncMock(return_value=chargers),
        ) as mock_chargers,
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_devices_inventory",
            AsyncMock(
                return_value={
                    "result": [
                        {"type": "iqevse", "devices": [{"serial_number": "EV123"}]}
                    ]
                }
            ),
        ),
    ):
        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            init["flow_id"],
            {
                CONF_EMAIL: "user@example.com",
                CONF_PASSWORD: "secret",
                CONF_REMEMBER_PASSWORD: False,
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "devices"
    flow = hass.config_entries.flow._progress[result["flow_id"]]
    assert flow._selected_site_id == "12345"
    assert flow._chargers_loaded is True
    assert flow._chargers == [("EV123", None)]
    mock_chargers.assert_not_awaited()
    hass.config_entries.flow.async_abort(result["flow_id"])


@pytest.mark.asyncio
async def test_site_step_prefills_first_site(hass) -> None:
    flow = _make_flow(hass)
    flow._sites = {"1001": "Existing", "2002": "Backup"}

    result = await flow.async_step_site()

    assert result["type"] is FlowResultType.FORM
    schema_keys = list(result["data_schema"].schema.keys())
    key = next(
        item
        for item in schema_keys
        if isinstance(item, VolRequired) and item.schema == CONF_SITE_ID
    )
    default = key.default() if callable(key.default) else key.default
    assert default == "1001"


@pytest.mark.asyncio
async def test_site_step_prefills_selected_site(hass) -> None:
    flow = _make_flow(hass)
    flow._sites = {"1001": "Existing", "2002": "Backup"}
    flow._selected_site_id = "2002"

    result = await flow.async_step_site()

    assert result["type"] is FlowResultType.FORM
    schema_keys = list(result["data_schema"].schema.keys())
    key = next(
        item
        for item in schema_keys
        if isinstance(item, VolRequired) and item.schema == CONF_SITE_ID
    )
    default = key.default() if callable(key.default) else key.default
    assert default == "2002"


@pytest.mark.asyncio
async def test_user_step_remember_password_defaults_disabled(hass) -> None:
    flow = _make_flow(hass)

    result = await flow.async_step_user()

    assert result["type"] is FlowResultType.FORM
    schema_keys = list(result["data_schema"].schema.keys())
    key = next(
        item
        for item in schema_keys
        if isinstance(item, VolOptional) and item.schema == CONF_REMEMBER_PASSWORD
    )
    default = key.default() if callable(key.default) else key.default
    assert default is False


@pytest.mark.asyncio
async def test_site_step_requires_selection(hass) -> None:
    flow = _make_flow(hass)
    flow._sites = {"1001": "Existing"}
    result = await flow.async_step_site({})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "site_required"}


@pytest.mark.asyncio
async def test_site_step_rejects_non_numeric_site_id(hass) -> None:
    flow = _make_flow(hass)
    flow._sites = {}
    result = await flow.async_step_site({CONF_SITE_ID: "12A45"})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "site_invalid"}
    assert flow._selected_site_id is None
    assert flow._sites == {}


@pytest.mark.asyncio
async def test_site_step_handles_unknown_site_id(hass) -> None:
    flow = _make_flow(hass)
    flow._sites = {"1001": "Existing"}
    flow._selected_site_id = None
    with patch.object(
        flow,
        "async_step_devices",
        AsyncMock(return_value={"type": FlowResultType.FORM, "step_id": "devices"}),
    ) as mock_devices:
        result = await flow.async_step_site({CONF_SITE_ID: "98765"})

    assert result["type"] is FlowResultType.FORM
    mock_devices.assert_awaited_once()
    assert "98765" in flow._sites


@pytest.mark.asyncio
async def test_site_step_without_options_uses_text_schema(hass) -> None:
    flow = _make_flow(hass)
    flow._sites = {}
    result = await flow.async_step_site()
    assert result["type"] is FlowResultType.FORM
    # No options provided, schema should still exist
    assert result["step_id"] == "site"


@pytest.mark.asyncio
async def test_devices_step_submission_requires_state(hass) -> None:
    flow = _make_flow(hass)
    flow._chargers_loaded = True
    flow._chargers = [("EV1", "Garage")]
    result = await flow.async_step_devices({})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unknown"


@pytest.mark.asyncio
async def test_devices_step_defaults_to_available_type_keys(hass) -> None:
    flow = _make_flow(hass)
    flow._auth_tokens = TOKENS
    flow._selected_site_id = "12345"
    flow._sites = {"12345": "Garage"}
    flow._chargers_loaded = True
    flow._chargers = [("EV1", "Garage"), ("EV2", "Driveway")]
    flow._type_keys_loaded = True
    flow._available_type_keys = [
        "envoy",
        "ac_battery",
        "iqevse",
        "heatpump",
        "microinverter",
    ]

    result = await flow.async_step_devices()

    assert result["type"] is FlowResultType.FORM
    schema_keys = list(result["data_schema"].schema.keys())
    iqevse_key = next(
        item
        for item in schema_keys
        if isinstance(item, VolOptional) and item.schema == CONF_TYPE_IQEVSE
    )
    default = (
        iqevse_key.default() if callable(iqevse_key.default) else iqevse_key.default
    )
    assert default is True
    micro_key = next(
        item
        for item in schema_keys
        if isinstance(item, VolOptional) and item.schema == CONF_TYPE_MICROINVERTER
    )
    micro_default = (
        micro_key.default() if callable(micro_key.default) else micro_key.default
    )
    assert micro_default is True
    heatpump_key = next(
        item
        for item in schema_keys
        if isinstance(item, VolOptional) and item.schema == CONF_TYPE_HEATPUMP
    )
    heatpump_default = (
        heatpump_key.default()
        if callable(heatpump_key.default)
        else heatpump_key.default
    )
    assert heatpump_default is True
    ac_battery_key = next(
        item
        for item in schema_keys
        if isinstance(item, VolOptional) and item.schema == CONF_TYPE_AC_BATTERY
    )
    ac_battery_default = (
        ac_battery_key.default()
        if callable(ac_battery_key.default)
        else ac_battery_key.default
    )
    assert ac_battery_default is True


@pytest.mark.asyncio
async def test_devices_step_schema_bounds_scan_interval(hass) -> None:
    flow = _make_flow(hass)
    flow._auth_tokens = TOKENS
    flow._selected_site_id = "12345"
    flow._sites = {"12345": "Garage"}
    flow._chargers_loaded = True
    flow._chargers = [("EV1", "Garage")]
    flow._type_keys_loaded = True
    flow._available_type_keys = ["iqevse"]

    result = await flow.async_step_devices()
    schema = result["data_schema"]

    assert (
        schema({CONF_SCAN_INTERVAL: MIN_SLOW_POLL_INTERVAL})[CONF_SCAN_INTERVAL]
        == MIN_SLOW_POLL_INTERVAL
    )
    with pytest.raises(vol.Invalid):
        schema({CONF_SCAN_INTERVAL: MIN_SLOW_POLL_INTERVAL - 1})
    with pytest.raises(vol.Invalid):
        schema({CONF_SCAN_INTERVAL: MAX_POLL_INTERVAL + 1})


@pytest.mark.asyncio
async def test_devices_step_reconfigure_defaults_to_configured_type_keys(hass) -> None:
    flow = _make_flow(hass)
    flow._auth_tokens = TOKENS
    flow._selected_site_id = "12345"
    flow._sites = {"12345": "Garage"}
    flow._chargers_loaded = True
    flow._chargers = [("EV1", "Garage"), ("EV2", "Driveway"), ("EV3", "Shop")]
    flow._type_keys_loaded = True
    flow._available_type_keys = [
        "envoy",
        "encharge",
        "iqevse",
        "heatpump",
        "microinverter",
    ]
    flow._reconfigure_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: "12345",
            CONF_EMAIL: "user@example.com",
            CONF_SELECTED_TYPE_KEYS: ["envoy", "encharge"],
        },
    )

    result = await flow.async_step_devices()

    assert result["type"] is FlowResultType.FORM
    schema_keys = list(result["data_schema"].schema.keys())
    iqevse_key = next(
        item
        for item in schema_keys
        if isinstance(item, VolOptional) and item.schema == CONF_TYPE_IQEVSE
    )
    iqevse_default = (
        iqevse_key.default() if callable(iqevse_key.default) else iqevse_key.default
    )
    assert iqevse_default is False
    envoy_key = next(
        item
        for item in schema_keys
        if isinstance(item, VolOptional) and item.schema == CONF_TYPE_ENVOY
    )
    envoy_default = (
        envoy_key.default() if callable(envoy_key.default) else envoy_key.default
    )
    assert envoy_default is True
    heatpump_key = next(
        item
        for item in schema_keys
        if isinstance(item, VolOptional) and item.schema == CONF_TYPE_HEATPUMP
    )
    heatpump_default = (
        heatpump_key.default()
        if callable(heatpump_key.default)
        else heatpump_key.default
    )
    assert heatpump_default is True


@pytest.mark.asyncio
async def test_devices_step_allows_empty_selection(hass) -> None:
    flow = _make_flow(hass)
    flow._auth_tokens = TOKENS
    flow._selected_site_id = "12345"
    flow._sites = {"12345": "Garage"}
    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_chargers",
            AsyncMock(return_value=[]),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_devices_inventory",
            AsyncMock(return_value={"result": []}),
        ),
        patch(
            "custom_components.enphase_ev.coordinator.EnphaseEVClient.async_get_activation_reference_data",
            AsyncMock(return_value={}),
        ),
        patch(
            "custom_components.enphase_ev.coordinator.EnphaseEVClient.async_get_activation_record",
            AsyncMock(return_value={}),
        ),
    ):
        result = await flow.async_step_devices(
            {CONF_TYPE_MICROINVERTER: False, CONF_SCAN_INTERVAL: 60}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SITE_ONLY] is True
    assert result["data"][CONF_SERIALS] == []
    assert result["data"][CONF_SELECTED_TYPE_KEYS] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scan_interval",
    [0, -1, "invalid", MIN_SLOW_POLL_INTERVAL - 1, MAX_POLL_INTERVAL + 1],
)
async def test_devices_step_rejects_scan_interval_outside_range(
    hass, scan_interval: object
) -> None:
    flow = _make_flow(hass)
    flow._auth_tokens = TOKENS
    flow._selected_site_id = "12345"
    flow._sites = {"12345": "Garage"}
    flow._chargers_loaded = True
    flow._chargers = []
    flow._type_keys_loaded = True
    flow._available_type_keys = []

    result = await flow.async_step_devices(
        {CONF_TYPE_MICROINVERTER: False, CONF_SCAN_INTERVAL: scan_interval}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "devices"
    assert result["errors"] == {CONF_SCAN_INTERVAL: "unknown"}


@pytest.mark.asyncio
async def test_devices_step_marks_heatpump_discovery_handled_when_visible(hass) -> None:
    flow = _make_flow(hass)
    flow._auth_tokens = TOKENS
    flow._selected_site_id = "12345"
    flow._sites = {"12345": "Garage"}
    flow._chargers_loaded = True
    flow._chargers = []
    flow._type_keys_loaded = True
    flow._available_type_keys = ["envoy", "heatpump"]

    result = await flow.async_step_devices(
        {
            CONF_TYPE_ENVOY: True,
            CONF_TYPE_HEATPUMP: False,
            CONF_SCAN_INTERVAL: 60,
        }
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HEATPUMP_DISCOVERY_HANDLED] is True
    assert result["data"][CONF_SELECTED_TYPE_KEYS] == ["envoy"]


@pytest.mark.asyncio
async def test_devices_step_schema_has_type_fields_only(hass) -> None:
    flow = _make_flow(hass)
    flow._auth_tokens = TOKENS
    flow._selected_site_id = "12345"
    flow._sites = {"12345": "Garage"}
    flow._type_keys_loaded = True
    flow._available_type_keys = ["envoy", "encharge", "iqevse"]
    flow._chargers_loaded = True
    flow._chargers = [("EV1", "Garage")]

    with patch(
        "custom_components.enphase_ev.config_flow.async_fetch_chargers",
        AsyncMock(return_value=[]),
    ):
        result = await flow.async_step_devices()

    assert result["type"] is FlowResultType.FORM
    schema_keys = list(result["data_schema"].schema.keys())
    assert any(
        isinstance(key, VolOptional) and key.schema == CONF_TYPE_ENVOY
        for key in schema_keys
    )
    assert any(
        isinstance(key, VolOptional) and key.schema == CONF_TYPE_MICROINVERTER
        for key in schema_keys
    )
    assert not any(
        isinstance(key, VolOptional) and key.schema == CONF_SERIALS
        for key in schema_keys
    )


@pytest.mark.asyncio
@pytest.mark.filterwarnings(
    "ignore:It is recommended to use web.AppKey instances for keys\\.:aiohttp.web_exceptions.NotAppKeyWarning"
)
async def test_devices_step_allows_site_only_entry(hass, monkeypatch) -> None:
    site = SiteInfo(site_id="12345", name="Garage Site")

    async def async_noop_grid_profile_refresh(
        runtime: GridProfileRuntime,
        *,
        force: bool = False,
        load_profiles: bool = True,
    ):
        return runtime.browse()

    monkeypatch.setattr(
        GridProfileRuntime,
        "async_refresh",
        async_noop_grid_profile_refresh,
    )

    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_authenticate",
            AsyncMock(return_value=(TOKENS, [site])),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_chargers",
            AsyncMock(return_value=[]),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_devices_inventory",
            AsyncMock(return_value={"result": []}),
        ),
    ):
        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        devices = await hass.config_entries.flow.async_configure(
            init["flow_id"],
            {
                CONF_EMAIL: "user@example.com",
                CONF_PASSWORD: "secret",
                CONF_REMEMBER_PASSWORD: False,
            },
        )
        schema_keys = list(devices["data_schema"].schema.keys())
        assert any(
            isinstance(key, VolOptional) and key.schema == CONF_TYPE_MICROINVERTER
            for key in schema_keys
        )
        result = await hass.config_entries.flow.async_configure(
            devices["flow_id"],
            {CONF_TYPE_MICROINVERTER: False, CONF_SCAN_INTERVAL: 60},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SERIALS] == []
    assert result["data"][CONF_SITE_ONLY] is True
    assert result["data"][CONF_INCLUDE_INVERTERS] is False
    assert result["data"][CONF_SELECTED_TYPE_KEYS] == []
    assert result["data"][CONF_SCAN_INTERVAL] == 60
    assert result["title"] == "Site: 12345"


@pytest.mark.asyncio
async def test_devices_step_shows_warning_when_inventory_unknown(hass) -> None:
    flow = _make_flow(hass)
    flow._auth_tokens = TOKENS
    flow._selected_site_id = "12345"
    flow._sites = {"12345": "Garage"}
    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_chargers",
            AsyncMock(return_value=[]),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_devices_inventory",
            AsyncMock(return_value=None),
        ),
    ):
        result = await flow.async_step_devices()

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "service_unavailable"}


@pytest.mark.asyncio
async def test_devices_step_unknown_inventory_preserves_hidden_selected_keys(
    hass,
) -> None:
    flow = _make_flow(hass)
    flow._reconfigure_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: "12345",
            CONF_EMAIL: "user@example.com",
            CONF_SELECTED_TYPE_KEYS: ["envoy", "microinverter"],
        },
    )
    flow._inventory_unknown = True
    merged = flow._merged_selected_type_keys_for_unknown_inventory(
        ["envoy"],
        visible_type_keys=["envoy"],
    )
    assert merged == ["envoy", "microinverter"]


@pytest.mark.asyncio
async def test_devices_step_can_disable_inverters(hass) -> None:
    site = SiteInfo(site_id="12345", name="Garage Site")

    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_authenticate",
            AsyncMock(return_value=(TOKENS, [site])),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_chargers",
            AsyncMock(return_value=[ChargerInfo(serial="EV1", name="Garage")]),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_devices_inventory",
            AsyncMock(
                return_value={
                    "result": [
                        {"type": "envoy", "devices": [{"serial_number": "GW-1"}]},
                        {"type": "iqevse", "devices": [{"serial_number": "EV1"}]},
                        {
                            "type": "microinverter",
                            "devices": [{"serial_number": "INV-1"}],
                        },
                    ]
                }
            ),
        ),
    ):
        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        devices = await hass.config_entries.flow.async_configure(
            init["flow_id"],
            {
                CONF_EMAIL: "user@example.com",
                CONF_PASSWORD: "secret",
                CONF_REMEMBER_PASSWORD: False,
            },
        )
        result = await hass.config_entries.flow.async_configure(
            devices["flow_id"],
            {
                CONF_TYPE_IQEVSE: True,
                CONF_TYPE_MICROINVERTER: False,
                CONF_SCAN_INTERVAL: 60,
            },
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_INCLUDE_INVERTERS] is False


@pytest.mark.asyncio
async def test_devices_step_iqevse_selection_falls_back_to_inventory_serials(
    hass,
) -> None:
    flow = _make_flow(hass)
    flow._auth_tokens = TOKENS
    flow._selected_site_id = "12345"
    flow._sites = {"12345": "Garage"}
    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_chargers",
            AsyncMock(return_value=[]),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_devices_inventory",
            AsyncMock(
                return_value={
                    "result": [
                        {"type": "iqevse", "devices": [{"serial_number": "EV-INV-1"}]}
                    ]
                }
            ),
        ),
    ):
        result = await flow.async_step_devices(
            {CONF_TYPE_IQEVSE: True, CONF_SCAN_INTERVAL: 60}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SERIALS] == ["EV-INV-1"]
    assert result["data"][CONF_SITE_ONLY] is False


def test_selected_iqevse_serials_preserves_configured_serials_when_discovery_empty(
    hass,
) -> None:
    flow = _make_flow(hass)
    flow._reconfigure_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: "12345",
            CONF_EMAIL: "user@example.com",
            CONF_SERIALS: ["EV-OLD-1"],
            CONF_SELECTED_TYPE_KEYS: ["iqevse"],
        },
    )
    flow._inventory_iqevse_serials = []

    assert flow._selected_iqevse_serials([]) == ["EV-OLD-1"]


@pytest.mark.asyncio
async def test_finalize_login_entry_without_state_aborts(hass) -> None:
    flow = _make_flow(hass)
    result = await flow._finalize_login_entry(["EV123"], 60)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unknown"


@pytest.mark.asyncio
async def test_finalize_login_entry_reconfigure_uses_reload_helper_without_listener(
    hass,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: "12345",
            CONF_EMAIL: "user@example.com",
            CONF_REMEMBER_PASSWORD: False,
            CONF_HEATPUMP_DISCOVERY_HANDLED: True,
        },
    )
    entry.add_to_hass(hass)

    flow = _make_flow(hass)
    flow._reconfigure_entry = entry
    flow._auth_tokens = TOKENS
    flow._sites = {"12345": "Garage"}
    flow._selected_site_id = "12345"
    flow._remember_password = False
    flow._email = "user@example.com"
    flow.async_update_and_abort = Mock()
    flow.async_update_reload_and_abort = Mock(
        return_value={"type": FlowResultType.ABORT, "reason": "handled"}
    )

    result = await flow._finalize_login_entry(["EV123"], 45)

    assert result == {"type": FlowResultType.ABORT, "reason": "handled"}
    flow.async_update_and_abort.assert_not_called()
    flow.async_update_reload_and_abort.assert_called_once()
    kwargs = flow.async_update_reload_and_abort.call_args.kwargs
    assert kwargs["data"][CONF_HEATPUMP_DISCOVERY_HANDLED] is True


@pytest.mark.asyncio
async def test_finalize_login_entry_reconfigure_uses_update_helper_with_listener(
    hass,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: "12345",
            CONF_EMAIL: "user@example.com",
            CONF_REMEMBER_PASSWORD: False,
            CONF_HEATPUMP_DISCOVERY_HANDLED: True,
        },
    )
    entry.add_to_hass(hass)
    entry.add_update_listener(AsyncMock())

    flow = _make_flow(hass)
    flow._reconfigure_entry = entry
    flow._auth_tokens = TOKENS
    flow._sites = {"12345": "Garage"}
    flow._selected_site_id = "12345"
    flow._remember_password = False
    flow._email = "user@example.com"
    flow.async_update_and_abort = Mock(
        return_value={"type": FlowResultType.ABORT, "reason": "handled"}
    )
    flow.async_update_reload_and_abort = Mock()

    result = await flow._finalize_login_entry(["EV123"], 45)

    assert result == {"type": FlowResultType.ABORT, "reason": "handled"}
    flow.async_update_and_abort.assert_called_once()
    flow.async_update_reload_and_abort.assert_not_called()
    kwargs = flow.async_update_and_abort.call_args.kwargs
    assert kwargs["data"][CONF_HEATPUMP_DISCOVERY_HANDLED] is True


def test_update_entry_helper_uses_update_helper_without_title_when_listener_present(
    hass,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SITE_ID: "12345"})
    entry.add_to_hass(hass)
    entry.add_update_listener(AsyncMock())

    flow = _make_flow(hass)
    captured: dict[str, object] = {}

    def _sync_update(entry_obj, *, data, reason):
        captured["entry"] = entry_obj
        captured["data"] = dict(data)
        captured["reason"] = reason
        return {"type": FlowResultType.ABORT, "reason": "sync"}

    flow.async_update_and_abort = _sync_update  # type: ignore[assignment]
    flow.async_update_reload_and_abort = Mock()

    result = flow._async_update_entry_and_abort(
        entry,
        data={CONF_SITE_ID: "12345"},
        reason="reconfigure_successful",
    )

    assert result == {"type": FlowResultType.ABORT, "reason": "sync"}
    assert captured["entry"] is entry
    assert captured["data"] == {CONF_SITE_ID: "12345"}
    assert captured["reason"] == "reconfigure_successful"
    flow.async_update_reload_and_abort.assert_not_called()


def test_update_entry_helper_uses_reload_helper_without_title_when_no_listener(
    hass,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SITE_ID: "12345"})
    entry.add_to_hass(hass)

    flow = _make_flow(hass)
    captured: dict[str, object] = {}

    def _sync_update(entry_obj, *, data, reason):
        captured["entry"] = entry_obj
        captured["data"] = dict(data)
        captured["reason"] = reason
        return {"type": FlowResultType.ABORT, "reason": "sync"}

    flow.async_update_and_abort = Mock()
    flow.async_update_reload_and_abort = _sync_update  # type: ignore[assignment]

    result = flow._async_update_entry_and_abort(
        entry,
        data={CONF_SITE_ID: "12345"},
        reason="reconfigure_successful",
    )

    assert result == {"type": FlowResultType.ABORT, "reason": "sync"}
    assert captured["entry"] is entry
    assert captured["data"] == {CONF_SITE_ID: "12345"}
    assert captured["reason"] == "reconfigure_successful"
    flow.async_update_and_abort.assert_not_called()


@pytest.mark.asyncio
async def test_update_entry_helper_falls_back_for_legacy_core(hass) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SITE_ID: "12345"})
    entry.add_to_hass(hass)

    flow = _make_flow(hass)
    flow.async_update_and_abort = None  # type: ignore[method-assign]
    flow.async_update_reload_and_abort = None  # type: ignore[method-assign]
    reload_entry = AsyncMock(return_value=True)

    with patch.object(hass.config_entries, "async_reload", reload_entry):
        result = flow._async_update_entry_and_abort(
            entry,
            title="Site: 12345",
            data={CONF_SITE_ID: "12345"},
            reason="reconfigure_successful",
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.title == "Site: 12345"
    reload_entry.assert_awaited_once_with(entry.entry_id)


@pytest.mark.asyncio
async def test_finalize_login_entry_reconfigure_updates_entry(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="12345",
        data={
            CONF_SITE_ID: "12345",
            CONF_EMAIL: "user@example.com",
            CONF_REMEMBER_PASSWORD: True,
            CONF_PASSWORD: "old-secret",
            CONF_HEATPUMP_DISCOVERY_HANDLED: True,
        },
    )
    entry.add_to_hass(hass)

    flow = _make_flow(hass)
    flow._reconfigure_entry = entry
    flow._auth_tokens = TOKENS
    flow._sites = {"12345": "Garage"}
    flow._selected_site_id = "12345"
    flow._remember_password = True
    flow._password = "new-secret"
    flow._email = "user@example.com"

    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as mock_reload:
        result = await flow._finalize_login_entry(["EV123"], 30)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_PASSWORD] == "new-secret"
    assert entry.data[CONF_HEATPUMP_DISCOVERY_HANDLED] is True
    mock_reload.assert_awaited_once_with(entry.entry_id)


@pytest.mark.asyncio
async def test_finalize_login_entry_reauth_updates_entry(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="12345",
        data={
            CONF_SITE_ID: "12345",
            CONF_EMAIL: "user@example.com",
            CONF_REMEMBER_PASSWORD: True,
            CONF_PASSWORD: "old-secret",
            CONF_AUTH_REFRESH_SUSPENDED_UNTIL: "2026-04-10T13:00:00+00:00",
            CONF_AUTH_BLOCKED_UNTIL: "2026-04-10T13:00:00+00:00",
            CONF_AUTH_BLOCK_REASON: "login_wall_after_refresh_reject",
        },
    )
    entry.add_to_hass(hass)

    flow = _make_flow(hass)
    flow._reconfigure_entry = entry
    flow._reauth_entry = entry
    flow._auth_tokens = TOKENS
    flow._sites = {"12345": "Garage"}
    flow._selected_site_id = "12345"
    flow._remember_password = True
    flow._password = "new-secret"
    flow._email = "user@example.com"

    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as mock_reload:
        result = await flow._finalize_login_entry(["EV123"], 30)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new-secret"
    assert CONF_AUTH_REFRESH_SUSPENDED_UNTIL not in entry.data
    assert CONF_AUTH_BLOCKED_UNTIL not in entry.data
    assert CONF_AUTH_BLOCK_REASON not in entry.data
    mock_reload.assert_awaited_once_with(entry.entry_id)


@pytest.mark.asyncio
async def test_reconfigure_step_remember_password_preserves_opt_out(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: "12345",
            CONF_EMAIL: "user@example.com",
            CONF_REMEMBER_PASSWORD: False,
        },
    )
    entry.add_to_hass(hass)

    flow = _make_flow(hass)
    flow.context = {
        "source": config_entries.SOURCE_RECONFIGURE,
        "entry_id": entry.entry_id,
    }

    result = await flow.async_step_reconfigure()

    assert result["type"] is FlowResultType.FORM
    schema_keys = list(result["data_schema"].schema.keys())
    key = next(
        item
        for item in schema_keys
        if isinstance(item, VolOptional) and item.schema == CONF_REMEMBER_PASSWORD
    )
    default = key.default() if callable(key.default) else key.default
    assert default is False


@pytest.mark.asyncio
async def test_reauth_step_remember_password_preserves_opt_out(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: "12345",
            CONF_EMAIL: "user@example.com",
            CONF_REMEMBER_PASSWORD: False,
        },
    )
    entry.add_to_hass(hass)

    flow = _make_flow(hass)
    flow.context = {
        "source": config_entries.SOURCE_REAUTH,
        "entry_id": entry.entry_id,
    }

    result = await flow.async_step_reauth({})

    assert result["type"] is FlowResultType.FORM
    schema_keys = list(result["data_schema"].schema.keys())
    key = next(
        item
        for item in schema_keys
        if isinstance(item, VolOptional) and item.schema == CONF_REMEMBER_PASSWORD
    )
    default = key.default() if callable(key.default) else key.default
    assert default is False


@pytest.mark.asyncio
async def test_finalize_login_entry_reload_helper_removes_none(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="12345",
        data={
            CONF_SITE_ID: "12345",
            CONF_EMAIL: "user@example.com",
            CONF_REMEMBER_PASSWORD: True,
            CONF_PASSWORD: "legacy",
            CONF_SESSION_ID: "old-session",
            CONF_AUTH_REFRESH_SUSPENDED_UNTIL: "2026-04-10T13:00:00+00:00",
            CONF_AUTH_BLOCKED_UNTIL: "2026-04-10T13:00:00+00:00",
            CONF_AUTH_BLOCK_REASON: "login_wall_after_refresh_reject",
        },
    )
    entry.add_to_hass(hass)

    flow = _make_flow(hass)
    flow._reconfigure_entry = entry
    flow._auth_tokens = AuthTokens(
        cookie=None,
        session_id=None,
        access_token=None,
        token_expires_at=None,
    )
    flow._sites = {"12345": "Garage"}
    flow._selected_site_id = "12345"
    flow._remember_password = False
    flow._password = None
    flow._email = "user@example.com"

    captured: dict[str, object] = {}

    def _sync_update(entry_obj, *, data, reason, title=None):
        captured["entry"] = entry_obj
        captured["data"] = dict(data)
        captured["reason"] = reason
        captured["title"] = title
        return {"type": FlowResultType.ABORT, "reason": "sync"}

    flow.async_update_reload_and_abort = _sync_update  # type: ignore[assignment]

    result = await flow._finalize_login_entry(["EV1"], 30)

    assert result == {"type": FlowResultType.ABORT, "reason": "sync"}
    assert captured["entry"] is entry
    assert captured["reason"] == "reconfigure_successful"
    assert captured["title"] == "Site: 12345"
    assert CONF_PASSWORD not in captured["data"]
    assert CONF_SESSION_ID not in captured["data"]
    assert CONF_COOKIE not in captured["data"]
    assert CONF_EAUTH not in captured["data"]
    assert CONF_ACCESS_TOKEN not in captured["data"]
    assert CONF_AUTH_REFRESH_SUSPENDED_UNTIL not in captured["data"]
    assert CONF_AUTH_BLOCKED_UNTIL not in captured["data"]
    assert CONF_AUTH_BLOCK_REASON not in captured["data"]


@pytest.mark.asyncio
async def test_finalize_login_entry_reload_helper_passes_reauth_reason(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="12345",
        data={
            CONF_SITE_ID: "12345",
            CONF_EMAIL: "user@example.com",
            CONF_REMEMBER_PASSWORD: False,
        },
    )
    entry.add_to_hass(hass)

    flow = _make_flow(hass)
    flow._reconfigure_entry = entry
    flow._reauth_entry = entry
    flow._auth_tokens = TOKENS
    flow._sites = {"12345": "Garage"}
    flow._selected_site_id = "12345"
    flow._remember_password = False
    flow._password = None
    flow._email = "user@example.com"

    captured: dict[str, object] = {}

    def _sync_update(entry_obj, *, data, reason, title=None):
        captured["entry"] = entry_obj
        captured["data"] = dict(data)
        captured["reason"] = reason
        captured["title"] = title
        return {"type": FlowResultType.ABORT, "reason": "sync"}

    flow.async_update_reload_and_abort = _sync_update  # type: ignore[assignment]

    result = await flow._finalize_login_entry(["EV1"], 30)

    assert result == {"type": FlowResultType.ABORT, "reason": "sync"}
    assert captured["entry"] is entry
    assert captured["reason"] == "reauth_successful"
    assert captured["title"] == "Site: 12345"


@pytest.mark.asyncio
async def test_ensure_chargers_handles_missing_state(hass) -> None:
    flow = _make_flow(hass)
    await flow._ensure_chargers()
    assert flow._chargers_loaded is True
    assert flow._chargers == []


@pytest.mark.asyncio
async def test_ensure_chargers_fetches_from_api(hass) -> None:
    flow = _make_flow(hass)
    flow._auth_tokens = TOKENS
    flow._selected_site_id = "12345"
    chargers = [ChargerInfo(serial="EV1", name=None)]

    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_chargers",
            AsyncMock(return_value=chargers),
        ),
    ):
        await flow._ensure_chargers()

    assert flow._chargers_loaded is True
    assert flow._chargers == [("EV1", None)]


@pytest.mark.asyncio
async def test_ensure_chargers_skips_when_already_loaded(hass) -> None:
    flow = _make_flow(hass)
    flow._chargers_loaded = True

    with patch(
        "custom_components.enphase_ev.config_flow.async_fetch_chargers",
        AsyncMock(side_effect=AssertionError("should not call")),
    ):
        await flow._ensure_chargers()


@pytest.mark.asyncio
async def test_ensure_available_type_keys_skips_when_already_loaded(hass) -> None:
    flow = _make_flow(hass)
    flow._type_keys_loaded = True

    with patch(
        "custom_components.enphase_ev.config_flow.async_fetch_devices_inventory",
        AsyncMock(side_effect=AssertionError("should not call")),
    ):
        await flow._ensure_available_type_keys()


@pytest.mark.asyncio
async def test_ensure_device_selection_data_reuses_inventory_charger_serials(
    hass,
) -> None:
    flow = _make_flow(hass)
    flow._auth_tokens = TOKENS
    flow._selected_site_id = "12345"

    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_chargers",
            AsyncMock(side_effect=AssertionError("should not fetch chargers")),
        ) as mock_chargers,
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_devices_inventory",
            AsyncMock(
                return_value={
                    "result": [
                        {"type": "iqevse", "devices": [{"serial_number": "EV1"}]}
                    ]
                }
            ),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_hems_devices",
            AsyncMock(return_value=None),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_battery_site_settings",
            AsyncMock(return_value=None),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_inverters_inventory",
            AsyncMock(return_value=None),
        ),
    ):
        await flow._ensure_device_selection_data()

    assert flow._chargers_loaded is True
    assert flow._chargers == [("EV1", None)]
    assert flow._type_keys_loaded is True
    mock_chargers.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_device_selection_data_falls_back_to_charger_api(
    hass,
) -> None:
    flow = _make_flow(hass)
    flow._auth_tokens = TOKENS
    flow._selected_site_id = "12345"

    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_chargers",
            AsyncMock(return_value=[ChargerInfo(serial="EV2", name="Garage")]),
        ) as mock_chargers,
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_devices_inventory",
            AsyncMock(return_value={"result": []}),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_hems_devices",
            AsyncMock(return_value=None),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_battery_site_settings",
            AsyncMock(return_value=None),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_inverters_inventory",
            AsyncMock(return_value=None),
        ),
    ):
        await flow._ensure_device_selection_data()

    assert flow._chargers_loaded is True
    assert flow._chargers == [("EV2", "Garage")]
    mock_chargers.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_available_type_keys_treats_parallel_probe_exceptions_as_unknown(
    hass,
) -> None:
    flow = _make_flow(hass)
    flow._auth_tokens = TOKENS
    flow._selected_site_id = "12345"

    async def _raise_probe_error(*_args, **_kwargs):
        raise RuntimeError("probe failed")

    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_devices_inventory",
            _raise_probe_error,
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_hems_devices",
            _raise_probe_error,
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_battery_site_settings",
            _raise_probe_error,
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_inverters_inventory",
            _raise_probe_error,
        ),
    ):
        await flow._ensure_available_type_keys()

    assert flow._inventory_unknown is True
    assert flow._available_type_keys == []
    assert flow._inventory_iqevse_serials == []


@pytest.mark.asyncio
async def test_ensure_available_type_keys_discovers_hems_heatpump(hass) -> None:
    flow = _make_flow(hass)
    flow._auth_tokens = TOKENS
    flow._selected_site_id = "12345"

    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_devices_inventory",
            AsyncMock(return_value={"result": []}),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_hems_devices",
            AsyncMock(
                return_value={
                    "data": {
                        "hems-devices": {
                            "heat-pump": [
                                {"device-uid": "HP-1", "statusText": "Normal"}
                            ]
                        }
                    }
                }
            ),
        ),
    ):
        await flow._ensure_available_type_keys()

    assert flow._available_type_keys == ["heatpump"]


@pytest.mark.asyncio
async def test_ensure_available_type_keys_discovers_hems_heatpump_from_result_devices(
    hass,
) -> None:
    flow = _make_flow(hass)
    flow._auth_tokens = TOKENS
    flow._selected_site_id = "12345"

    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_devices_inventory",
            AsyncMock(return_value={"result": []}),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_hems_devices",
            AsyncMock(
                return_value={
                    "status": "success",
                    "result": {
                        "devices": [
                            {
                                "heat-pump": [
                                    {"device-uid": "HP-1", "statusText": "Normal"}
                                ]
                            }
                        ]
                    },
                }
            ),
        ),
    ):
        await flow._ensure_available_type_keys()

    assert flow._available_type_keys == ["heatpump"]


@pytest.mark.asyncio
async def test_ensure_available_type_keys_ignores_retired_hems_heatpump(hass) -> None:
    flow = _make_flow(hass)
    flow._auth_tokens = TOKENS
    flow._selected_site_id = "12345"

    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_devices_inventory",
            AsyncMock(return_value={"result": []}),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_hems_devices",
            AsyncMock(
                return_value={
                    "data": {
                        "hems-devices": {
                            "heat-pump": [
                                {"device-uid": "HP-1", "statusText": "Retired"}
                            ]
                        }
                    }
                }
            ),
        ),
    ):
        await flow._ensure_available_type_keys()

    assert flow._available_type_keys == []


@pytest.mark.asyncio
async def test_ensure_available_type_keys_falls_back_to_legacy_microinverters(
    hass,
) -> None:
    flow = _make_flow(hass)
    flow._auth_tokens = TOKENS
    flow._selected_site_id = "12345"

    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_devices_inventory",
            AsyncMock(
                return_value={
                    "result": [
                        {
                            "type": "envoy",
                            "devices": [{"serial_number": "GW-1", "status": "normal"}],
                        }
                    ]
                }
            ),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_inverters_inventory",
            AsyncMock(
                return_value={
                    "inverters": [{"serial_number": "INV-1", "status": "normal"}]
                }
            ),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_hems_devices",
            AsyncMock(return_value=None),
        ),
    ):
        await flow._ensure_available_type_keys()

    assert flow._available_type_keys == ["envoy", "microinverter"]


@pytest.mark.asyncio
async def test_ensure_available_type_keys_ignores_retired_legacy_microinverters(
    hass,
) -> None:
    flow = _make_flow(hass)
    flow._auth_tokens = TOKENS
    flow._selected_site_id = "12345"

    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_devices_inventory",
            AsyncMock(
                return_value={
                    "result": [
                        {
                            "type": "envoy",
                            "devices": [{"serial_number": "GW-1", "status": "normal"}],
                        }
                    ]
                }
            ),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_inverters_inventory",
            AsyncMock(
                return_value={
                    "inverters": [{"serial_number": "INV-1", "status": "retired"}]
                }
            ),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_hems_devices",
            AsyncMock(return_value=None),
        ),
    ):
        await flow._ensure_available_type_keys()

    assert flow._available_type_keys == ["envoy"]


@pytest.mark.asyncio
async def test_ensure_available_type_keys_clears_unknown_when_legacy_fallback_succeeds(
    hass,
) -> None:
    flow = _make_flow(hass)
    flow._auth_tokens = TOKENS
    flow._selected_site_id = "12345"

    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_devices_inventory",
            AsyncMock(return_value=None),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_inverters_inventory",
            AsyncMock(
                return_value={
                    "inverters": [{"serial_number": "INV-1", "status": "normal"}]
                }
            ),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_hems_devices",
            AsyncMock(return_value=None),
        ),
    ):
        await flow._ensure_available_type_keys()

    assert flow._inventory_unknown is False
    assert flow._available_type_keys == ["microinverter"]


@pytest.mark.asyncio
async def test_ensure_available_type_keys_discovers_ac_battery_from_site_settings(
    hass,
) -> None:
    flow = _make_flow(hass)
    flow._auth_tokens = TOKENS
    flow._selected_site_id = "12345"

    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_devices_inventory",
            AsyncMock(return_value={"result": []}),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_hems_devices",
            AsyncMock(return_value=None),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_battery_site_settings",
            AsyncMock(return_value={"data": {"hasAcb": True}}),
        ),
    ):
        await flow._ensure_available_type_keys()

    assert flow._available_type_keys == ["ac_battery"]


def test_legacy_microinverters_available_from_nested_result() -> None:
    assert config_flow._legacy_microinverters_available(
        {
            "result": {
                "inverters": [
                    {"serial_number": "INV-1", "status": "normal"},
                ]
            }
        }
    )


def test_legacy_microinverters_available_rejects_invalid_nested_shape() -> None:
    assert not config_flow._legacy_microinverters_available(
        {"result": {"inverters": {"serial_number": "INV-1"}}}
    )


def test_hems_devices_groups_handles_invalid_shapes() -> None:
    assert config_flow._hems_devices_groups({"data": []}) == []
    assert config_flow._hems_devices_groups({"data": {"hems-devices": []}}) == []
    assert config_flow._hems_devices_groups(
        {"result": {"devices": [{"gateway": []}, "bad"]}}
    ) == [{"gateway": []}]
    assert config_flow._hems_devices_groups(
        {"result": {"devices": {"heat-pump": []}}}
    ) == [{"heat-pump": []}]


def test_normalize_serials_variants(hass) -> None:
    flow = _make_flow(hass)
    assert flow._normalize_serials(["A", "A", " "]) == ["A"]
    assert flow._normalize_serials("A, B\nC") == ["A", "B", "C"]
    assert flow._normalize_serials(123) == []


def test_normalize_type_keys_handles_non_iterable_input(hass) -> None:
    flow = _make_flow(hass)
    assert flow._normalize_type_keys(123) == []
    assert flow._normalize_type_keys("envoy,iqevse") == ["envoy", "iqevse"]


def test_default_scan_interval_uses_reconfigure_value(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SCAN_INTERVAL: 15},
    )
    flow = _make_flow(hass)
    flow._reconfigure_entry = entry
    assert flow._default_scan_interval() == MIN_SLOW_POLL_INTERVAL


def test_default_scan_interval_falls_back_for_invalid_reconfigure_value(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SCAN_INTERVAL: "invalid"},
    )
    flow = _make_flow(hass)
    flow._reconfigure_entry = entry
    assert flow._default_scan_interval() == DEFAULT_SCAN_INTERVAL


def test_default_scan_interval_handles_legacy_coercion_edges(hass) -> None:
    flow = _make_flow(hass)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SCAN_INTERVAL: True},
    )
    flow._reconfigure_entry = entry
    assert flow._default_scan_interval() == MIN_SLOW_POLL_INTERVAL

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SCAN_INTERVAL: object()},
    )
    flow._reconfigure_entry = entry
    assert flow._default_scan_interval() == DEFAULT_SCAN_INTERVAL

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SCAN_INTERVAL: MAX_POLL_INTERVAL + 1},
    )
    flow._reconfigure_entry = entry
    assert flow._default_scan_interval() == MAX_POLL_INTERVAL


def test_default_include_inverters_uses_reconfigure_value(hass) -> None:
    flow = _make_flow(hass)
    flow._reconfigure_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_INCLUDE_INVERTERS: False},
    )
    assert flow._default_include_inverters() is False


def test_default_selected_type_keys_legacy_uses_reconfigure_value(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_INCLUDE_INVERTERS: False,
            CONF_SERIALS: [],
        },
    )
    flow = _make_flow(hass)
    flow._reconfigure_entry = entry
    assert flow._default_selected_type_keys(["envoy", "iqevse", "microinverter"]) == [
        "envoy"
    ]


def test_default_selected_type_keys_uses_flow_state(hass) -> None:
    flow = _make_flow(hass)
    flow._site_only = True
    flow._include_inverters = False
    assert flow._default_selected_type_keys(["envoy", "iqevse", "microinverter"]) == [
        "envoy"
    ]


def test_battery_site_settings_has_acb_helper_branches() -> None:
    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert config_flow._battery_site_settings_has_acb(None) is False
    assert config_flow._battery_site_settings_has_acb({"data": "bad"}) is False
    assert config_flow._battery_site_settings_has_acb({"hasAcb": True}) is True
    assert config_flow._battery_site_settings_has_acb({"hasAcb": None}) is False
    assert config_flow._battery_site_settings_has_acb({"hasAcb": "YES"}) is True
    assert config_flow._battery_site_settings_has_acb({"hasAcb": BadStr()}) is False


def test_default_selected_type_keys_reconfigure_auto_selects_discovered_heatpump(
    hass,
) -> None:
    flow = _make_flow(hass)
    flow._reconfigure_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SELECTED_TYPE_KEYS: ["envoy"]},
    )

    assert flow._default_selected_type_keys(["envoy", "heatpump", "iqevse"]) == [
        "envoy",
        "heatpump",
    ]


def test_default_selected_type_keys_reconfigure_respects_handled_heatpump_discovery(
    hass,
) -> None:
    flow = _make_flow(hass)
    flow._reconfigure_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SELECTED_TYPE_KEYS: ["envoy"],
            CONF_HEATPUMP_DISCOVERY_HANDLED: True,
        },
    )

    assert flow._default_selected_type_keys(["envoy", "heatpump", "iqevse"]) == [
        "envoy",
    ]


def test_selected_type_keys_from_user_input_skips_unknown_field_keys(hass) -> None:
    flow = _make_flow(hass)
    selected = flow._selected_type_keys_from_user_input(
        {CONF_TYPE_ENVOY: True},
        ["envoy", "unknown_type"],
        default_selected_type_keys=["envoy", "unknown_type"],
    )
    assert selected == ["envoy"]


def test_legacy_selected_type_keys_uses_available_inventory_keys(hass) -> None:
    flow = _make_flow(hass)
    flow._available_type_keys = ["envoy", "iqevse", "microinverter"]
    flow._chargers = [("EV1", "Garage")]
    selected = flow._legacy_selected_type_keys([], include_inverters=False)
    assert selected == ["envoy"]


def test_stored_selected_type_keys_legacy_path(hass) -> None:
    flow = _make_flow(hass)
    flow._reconfigure_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SERIALS: ["EV1"], CONF_INCLUDE_INVERTERS: True},
    )
    assert flow._stored_selected_type_keys() == [
        "envoy",
        "encharge",
        "iqevse",
        "microinverter",
    ]


def test_stored_selected_type_keys_legacy_path_respects_site_only(hass) -> None:
    flow = _make_flow(hass)
    flow._reconfigure_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SERIALS: ["EV1"],
            CONF_INCLUDE_INVERTERS: True,
            CONF_SITE_ONLY: True,
        },
    )
    assert flow._stored_selected_type_keys() == ["envoy", "encharge", "microinverter"]


def test_fallback_type_keys_for_unknown_inventory_prefers_stored_selection(
    hass,
) -> None:
    flow = _make_flow(hass)
    flow._reconfigure_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SELECTED_TYPE_KEYS: ["envoy", "microinverter"]},
    )
    assert flow._fallback_type_keys_for_unknown_inventory([]) == [
        "envoy",
        "microinverter",
    ]


def test_fallback_type_keys_for_unknown_inventory_adds_iqevse_when_discovered(
    hass,
) -> None:
    flow = _make_flow(hass)
    flow._include_inverters = False
    assert flow._fallback_type_keys_for_unknown_inventory(["EV1"]) == [
        "envoy",
        "encharge",
        "iqevse",
    ]


def test_default_selected_type_keys_and_fallback_include_ac_battery(hass) -> None:
    flow = _make_flow(hass)
    assert flow._default_selected_type_keys(["envoy", "ac_battery"]) == [
        "envoy",
        "ac_battery",
    ]

    flow._available_type_keys = ["envoy", "ac_battery"]
    flow._include_inverters = False
    assert flow._fallback_type_keys_for_unknown_inventory([]) == [
        "envoy",
        "encharge",
        "ac_battery",
    ]


@pytest.mark.asyncio
async def test_devices_step_schema_ignores_unknown_type_key(hass) -> None:
    flow = _make_flow(hass)
    flow._auth_tokens = TOKENS
    flow._selected_site_id = "12345"
    flow._sites = {"12345": "Garage"}
    flow._chargers_loaded = True
    flow._chargers = [("EV1", "Garage")]
    flow._type_keys_loaded = True
    flow._available_type_keys = ["unknown_type", "iqevse"]

    result = await flow.async_step_devices()

    assert result["type"] is FlowResultType.FORM
    schema_keys = list(result["data_schema"].schema.keys())
    assert any(
        isinstance(key, VolOptional) and key.schema == CONF_TYPE_IQEVSE
        for key in schema_keys
    )


@pytest.mark.asyncio
async def test_devices_step_schema_handles_missing_type_field_mapping(hass) -> None:
    flow = _make_flow(hass)
    flow._auth_tokens = TOKENS
    flow._selected_site_id = "12345"
    flow._sites = {"12345": "Garage"}
    flow._chargers_loaded = True
    flow._chargers = [("EV1", "Garage")]
    flow._type_keys_loaded = True
    flow._available_type_keys = ["envoy", "iqevse"]

    with patch.dict(
        "custom_components.enphase_ev.config_flow._TYPE_FIELD_BY_KEY",
        {"iqevse": CONF_TYPE_IQEVSE},
        clear=True,
    ):
        result = await flow.async_step_devices()

    assert result["type"] is FlowResultType.FORM


def test_get_reconfigure_entry_uses_core_helper(hass) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)

    flow = _make_flow(hass)
    with patch.object(
        config_entries.ConfigFlow, "_get_reconfigure_entry", return_value=entry
    ):
        assert flow._get_reconfigure_entry() == entry


def test_get_reauth_entry_uses_core_helper(hass) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)

    flow = _make_flow(hass)
    with patch.object(
        config_entries.ConfigFlow, "_get_reauth_entry", return_value=entry
    ):
        assert flow._get_reauth_entry() == entry


@pytest.mark.asyncio
async def test_async_step_reconfigure_missing_entry_aborts(hass) -> None:
    flow = _make_flow(hass)
    with patch.object(
        config_entries.ConfigFlow, "_get_reconfigure_entry", return_value=None
    ):
        result = await flow.async_step_reconfigure()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unknown"


@pytest.mark.asyncio
async def test_async_step_reauth_missing_entry_aborts(hass) -> None:
    flow = _make_flow(hass)
    with patch.object(
        config_entries.ConfigFlow, "_get_reauth_entry", return_value=None
    ):
        result = await flow.async_step_reauth()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unknown"


def test_async_get_options_flow_returns_handler(hass) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    handler = EnphaseEVConfigFlow.async_get_options_flow(entry)
    assert isinstance(handler, OptionsFlowHandler)


def test_options_flow_init_uses_parameterless_super(monkeypatch, hass) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={})

    original_init = config_entries.OptionsFlow.__init__
    init_args: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def maybe_raise(self, *args, **kwargs):
        init_args.append((args, kwargs))
        return original_init(self)

    monkeypatch.setattr(config_entries.OptionsFlow, "__init__", maybe_raise)

    handler = OptionsFlowHandler(entry)
    assert handler._entry is entry
    assert init_args == [((), {})]


def test_options_flow_normalize_helpers_cover_string_and_fallback(hass) -> None:
    handler = OptionsFlowHandler(MockConfigEntry(domain=DOMAIN, data={}))
    handler.hass = hass

    assert handler._normalize_serials("EV1, EV2\nEV3") == ["EV1", "EV2", "EV3"]
    assert handler._normalize_serials(123) == []
    assert handler._normalize_type_keys(["envoy", "iqevse"]) == ["envoy", "iqevse"]
    assert handler._normalize_type_keys("envoy,iqevse") == ["envoy", "iqevse"]
    assert handler._normalize_type_keys(123) == []
    assert handler._normalize_any_type_keys("envoy,generator") == [
        "envoy",
        "generator",
    ]
    assert handler._normalize_any_type_keys(123) == []


def test_options_flow_legacy_selected_type_keys_adds_iqevse(hass) -> None:
    handler = OptionsFlowHandler(MockConfigEntry(domain=DOMAIN, data={}))
    handler.hass = hass

    assert handler._legacy_selected_type_keys(["EV1"], include_inverters=False) == [
        "envoy",
        "encharge",
        "iqevse",
    ]


def test_options_flow_stored_selected_type_keys_legacy_respects_site_only(hass) -> None:
    handler = OptionsFlowHandler(
        MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_SERIALS: ["EV1"],
                CONF_INCLUDE_INVERTERS: True,
                CONF_SITE_ONLY: True,
            },
        )
    )
    handler.hass = hass

    assert handler._stored_selected_type_keys() == [
        "envoy",
        "encharge",
        "microinverter",
    ]


def test_options_flow_build_schema_skips_missing_type_mapping(hass) -> None:
    handler = OptionsFlowHandler(MockConfigEntry(domain=DOMAIN, data={}, options={}))
    handler.hass = hass

    with patch.dict(
        "custom_components.enphase_ev.config_flow._TYPE_FIELD_BY_KEY",
        {"envoy": CONF_TYPE_ENVOY},
        clear=True,
    ):
        schema = handler._build_schema()

    schema_keys = list(schema.schema.keys())
    assert any(
        isinstance(key, VolOptional) and key.schema == CONF_TYPE_ENVOY
        for key in schema_keys
    )
    assert not any(
        isinstance(key, VolOptional) and key.schema == CONF_TYPE_IQEVSE
        for key in schema_keys
    )


def test_options_flow_schema_bounds_runtime_options(hass) -> None:
    handler = OptionsFlowHandler(MockConfigEntry(domain=DOMAIN, data={}, options={}))
    handler.hass = hass

    schema = handler._build_schema()
    api_timeout_selector = next(
        value
        for marker, value in schema.schema.items()
        if isinstance(marker, VolOptional) and marker.schema == OPT_API_TIMEOUT
    )

    assert api_timeout_selector.selector_type == "number"
    assert api_timeout_selector.config["mode"] == "box"

    assert (
        schema(
            {
                OPT_FAST_POLL_INTERVAL: MIN_FAST_POLL_INTERVAL,
                OPT_SLOW_POLL_INTERVAL: MIN_SLOW_POLL_INTERVAL,
                OPT_API_TIMEOUT: MIN_API_TIMEOUT,
                OPT_SESSION_HISTORY_INTERVAL: MIN_SESSION_HISTORY_INTERVAL_MIN,
            }
        )[OPT_API_TIMEOUT]
        == MIN_API_TIMEOUT
    )
    with pytest.raises(vol.Invalid):
        schema({OPT_FAST_POLL_INTERVAL: MAX_POLL_INTERVAL + 1})
    with pytest.raises(vol.Invalid):
        schema({OPT_SLOW_POLL_INTERVAL: MAX_POLL_INTERVAL + 1})
    with pytest.raises(vol.Invalid):
        schema({OPT_API_TIMEOUT: MIN_API_TIMEOUT - 1})
    with pytest.raises(vol.Invalid):
        schema({OPT_API_TIMEOUT: MAX_API_TIMEOUT + 1})
    with pytest.raises(vol.Invalid):
        schema({OPT_SESSION_HISTORY_INTERVAL: MIN_SESSION_HISTORY_INTERVAL_MIN - 1})
    with pytest.raises(vol.Invalid):
        schema({OPT_SESSION_HISTORY_INTERVAL: MAX_SESSION_HISTORY_INTERVAL_MIN + 1})


@pytest.mark.asyncio
async def test_options_flow_settings_hides_ac_battery_when_site_not_supported(
    hass, monkeypatch
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: "12345",
            CONF_COOKIE: "cookie=1",
            CONF_EAUTH: "token",
            CONF_SELECTED_TYPE_KEYS: ["envoy"],
        },
        options={},
    )
    entry.add_to_hass(hass)
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    monkeypatch.setattr(
        "custom_components.enphase_ev.config_flow.async_fetch_battery_site_settings",
        AsyncMock(return_value={"data": {"hasAcb": False}}),
    )

    result = await handler.async_step_settings()

    schema_keys = list(result["data_schema"].schema.keys())
    assert not any(
        isinstance(key, VolOptional) and key.schema == CONF_TYPE_AC_BATTERY
        for key in schema_keys
    )


@pytest.mark.asyncio
async def test_options_flow_settings_shows_ac_battery_when_site_supported(
    hass, monkeypatch
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: "12345",
            CONF_COOKIE: "cookie=1",
            CONF_EAUTH: "token",
            CONF_SELECTED_TYPE_KEYS: ["envoy"],
        },
        options={},
    )
    entry.add_to_hass(hass)
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    monkeypatch.setattr(
        "custom_components.enphase_ev.config_flow.async_fetch_battery_site_settings",
        AsyncMock(return_value={"data": {"hasAcb": True}}),
    )

    result = await handler.async_step_settings()

    schema_keys = list(result["data_schema"].schema.keys())
    assert any(
        isinstance(key, VolOptional) and key.schema == CONF_TYPE_AC_BATTERY
        for key in schema_keys
    )


@pytest.mark.asyncio
async def test_options_flow_ac_battery_supported_for_options_short_circuits_when_selected(
    hass,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: "12345",
            CONF_COOKIE: "cookie=1",
            CONF_EAUTH: "token",
            CONF_SELECTED_TYPE_KEYS: ["envoy", "ac_battery"],
        },
        options={},
    )
    entry.add_to_hass(hass)
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    assert await handler._ac_battery_supported_for_options() is True


@pytest.mark.asyncio
async def test_options_flow_ac_battery_supported_for_options_requires_tokens_and_site(
    hass,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "", CONF_SELECTED_TYPE_KEYS: ["envoy"]},
        options={},
    )
    entry.add_to_hass(hass)
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    assert await handler._ac_battery_supported_for_options() is False


@pytest.mark.asyncio
async def test_options_flow_settings_ignores_unknown_visible_type_keys(
    hass, monkeypatch
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: "12345",
            CONF_COOKIE: "cookie=1",
            CONF_EAUTH: "token",
            CONF_SELECTED_TYPE_KEYS: ["envoy"],
            CONF_SERIALS: [],
        },
        options={},
    )
    entry.add_to_hass(hass)
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    monkeypatch.setattr(
        handler,
        "_settings_type_keys",
        AsyncMock(return_value=["envoy", "unknown_type"]),
    )

    result = await handler.async_step_settings({CONF_TYPE_ENVOY: True})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert handler._entry.data[CONF_SELECTED_TYPE_KEYS] == ["envoy"]


def test_options_flow_default_nominal_voltage_uses_country(hass) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    handler = OptionsFlowHandler(entry)
    handler.hass = hass
    hass.config.country = "US"
    hass.config.language = "fr"

    assert handler._default_nominal_voltage() == 120


def test_options_flow_default_nominal_voltage_prefers_runtime_coordinator(hass) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(
            preferred_nominal_voltage=lambda: 220,
            nominal_voltage=230,
        )
    )
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    assert handler._default_nominal_voltage() == 220


def test_options_flow_default_nominal_voltage_uses_runtime_nominal_fallback(
    hass,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(
            preferred_nominal_voltage=lambda: None,
            nominal_voltage=220,
        )
    )
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    assert handler._default_nominal_voltage() == 220


@pytest.mark.asyncio
async def test_options_flow_discover_iqevse_serials_without_site_returns_empty(
    hass,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    assert await handler._discover_iqevse_serials() == []


@pytest.mark.asyncio
async def test_options_flow_discover_iqevse_serials_returns_empty_when_inventory_unknown(
    hass,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: "12345",
            CONF_EAUTH: "token-abc",
            CONF_COOKIE: "jar=1",
        },
    )
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_chargers",
            AsyncMock(return_value=[]),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_devices_inventory",
            AsyncMock(return_value=None),
        ),
    ):
        assert await handler._discover_iqevse_serials() == []


@pytest.mark.asyncio
async def test_options_flow_discover_iqevse_serials_prefers_inventory(
    hass,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: "12345",
            CONF_EAUTH: "token-abc",
            CONF_COOKIE: "jar=1",
        },
    )
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_chargers",
            AsyncMock(side_effect=AssertionError("should not fetch chargers")),
        ) as mock_chargers,
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_devices_inventory",
            AsyncMock(
                return_value={
                    "result": [
                        {"type": "iqevse", "devices": [{"serial_number": "EV-INV"}]}
                    ]
                }
            ),
        ),
    ):
        assert await handler._discover_iqevse_serials() == ["EV-INV"]

    mock_chargers.assert_not_awaited()


@pytest.mark.asyncio
async def test_options_flow_discover_iqevse_serials_falls_back_when_inventory_fails(
    hass,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: "12345",
            CONF_EAUTH: "token-abc",
            CONF_COOKIE: "jar=1",
        },
    )
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_chargers",
            AsyncMock(return_value=[ChargerInfo(serial="EV-FALLBACK", name="Garage")]),
        ) as mock_chargers,
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_devices_inventory",
            AsyncMock(side_effect=RuntimeError("inventory failed")),
        ) as mock_inventory,
    ):
        assert await handler._discover_iqevse_serials() == ["EV-FALLBACK"]

    mock_inventory.assert_awaited_once()
    mock_chargers.assert_awaited_once()


@pytest.mark.asyncio
async def test_options_flow_forget_password(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: "12345",
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_REMEMBER_PASSWORD: True,
        },
    )
    entry.add_to_hass(hass)

    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    result = await handler.async_step_init({"forget_password": True})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_REMEMBER_PASSWORD] is False
    assert CONF_PASSWORD not in entry.data


@pytest.mark.asyncio
async def test_options_flow_reauth_invokes_callback(hass) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    entry.async_start_reauth = Mock()

    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    result = await handler.async_step_init({"reauth": True})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry.async_start_reauth.assert_called_once_with(hass, data=entry.data)


@pytest.mark.asyncio
async def test_options_flow_init_shows_menu(hass) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SITE_ID: "12345"})
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    result = await handler.async_step_init()

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "init"
    assert result["menu_options"] == ["settings", "migrate_envoy"]


@pytest.mark.asyncio
async def test_options_flow_advanced_shows_grid_profile_menu(hass) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SITE_ID: "12345"})
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(
            grid_profile_runtime=_options_flow_grid_profile_runtime()
        )
    )
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    init_result = await handler.async_step_init()
    result = await handler.async_step_advanced()

    assert init_result["menu_options"] == ["settings", "advanced", "migrate_envoy"]
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "advanced"
    assert result["menu_options"] == ["grid_profile"]


@pytest.mark.asyncio
async def test_options_flow_hides_advanced_without_installer_access(hass) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SITE_ID: "12345"})
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(
            grid_profile_runtime=_options_flow_grid_profile_runtime(
                support_state=SUPPORT_DENIED
            )
        )
    )
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    result = await handler.async_step_init()

    assert result["menu_options"] == ["settings", "migrate_envoy"]
    result = await handler.async_step_advanced()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "grid_profile_installer_required"


@pytest.mark.asyncio
async def test_options_flow_hides_advanced_without_country_regions(hass) -> None:
    runtime = _options_flow_grid_profile_runtime()
    runtime.regions_by_country["AU"] = []
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SITE_ID: "12345"})
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(grid_profile_runtime=runtime)
    )
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    result = await handler.async_step_init()

    assert result["menu_options"] == ["settings", "migrate_envoy"]


def _options_flow_grid_profile_runtime(
    *,
    support_state: str = SUPPORT_CONFIRMED,
    with_profiles: bool = True,
    with_gateway: bool = False,
) -> GridProfileRuntime:
    coordinator = SimpleNamespace(
        client=SimpleNamespace(),
        async_update_listeners=Mock(),
    )
    runtime = GridProfileRuntime(coordinator)
    runtime.support_state = support_state
    runtime.country_code = "AU"
    runtime.regions_by_country = {
        "AU": [ActivationRegion("AU", "ACT", "Australian Capital Territory")]
    }
    runtime.staged_region_code = "ACT"
    if with_profiles:
        runtime.catalog_cache[("AU", "ACT", True)] = (
            9_999_999_999.0,
            [
                GridProfile(
                    profile_id="agf:test",
                    name="Test Grid Profile",
                    group="ACT, AU",
                    country="AU",
                    state="ACT",
                    pel_enabled=True,
                    is_277v_compatible=False,
                )
            ],
        )
    runtime.async_refresh = AsyncMock(return_value=runtime.browse())  # type: ignore[method-assign]
    runtime.async_load_profiles = AsyncMock(  # type: ignore[method-assign]
        return_value=runtime.filtered_profiles()
    )
    if with_gateway:
        runtime.gateway_targets = {
            "122532006376": GatewayGridProfileTarget(
                serial_num="122532006376",
                part_num="800-00555-r01",
                ensemble_envoy=True,
            )
        }
    return runtime


@pytest.mark.asyncio
async def test_options_flow_grid_profile_aborts_without_runtime(hass) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SITE_ID: "12345"})
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    result = await handler.async_step_advanced()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "grid_profile_unavailable"

    result = await handler.async_step_grid_profile()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "grid_profile_unavailable"

    result = await handler.async_step_grid_profile_select()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "grid_profile_unavailable"

    result = await handler.async_step_grid_profile_confirm()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "grid_profile_unavailable"


@pytest.mark.asyncio
async def test_options_flow_grid_profile_aborts_without_installer_access(hass) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SITE_ID: "12345"})
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(
            grid_profile_runtime=_options_flow_grid_profile_runtime(
                support_state=SUPPORT_DENIED
            )
        )
    )
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    result = await handler.async_step_grid_profile()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "grid_profile_installer_required"
    entry.runtime_data.coordinator.grid_profile_runtime.async_refresh.assert_awaited_once_with(
        force=False,
        load_profiles=False,
    )

    result = await handler.async_step_grid_profile_select()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "grid_profile_installer_required"

    result = await handler.async_step_grid_profile_confirm()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "grid_profile_installer_required"


@pytest.mark.asyncio
async def test_options_flow_grid_profile_reports_activation_unavailable(hass) -> None:
    runtime = _options_flow_grid_profile_runtime(
        support_state=SUPPORT_UNAVAILABLE,
        with_profiles=False,
    )
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SITE_ID: "12345"})
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(grid_profile_runtime=runtime)
    )
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    advanced = await handler.async_step_advanced()
    grid_profile = await handler.async_step_grid_profile()
    select = await handler.async_step_grid_profile_select()
    confirm = await handler.async_step_grid_profile_confirm()

    for result in (advanced, grid_profile, select, confirm):
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "grid_profile_unavailable"


@pytest.mark.asyncio
async def test_options_flow_grid_profile_filter_guard_paths(hass) -> None:
    runtime = _options_flow_grid_profile_runtime()
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SITE_ID: "12345"})
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(grid_profile_runtime=runtime)
    )
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    assert handler._grid_profile_commonly_used_from_input(True) is True  # noqa: SLF001
    assert handler._grid_profile_flag_label(None) == "Unknown"  # noqa: SLF001
    runtime.staged_region_code = None
    schema = handler._grid_profile_filter_schema(runtime)  # noqa: SLF001
    region_marker = next(
        marker for marker in schema.schema if marker.schema == CONF_GRID_PROFILE_REGION
    )
    assert region_marker.default() == "ACT"

    runtime.staged_region_code = "ACT"
    result = await handler.async_step_grid_profile(
        {CONF_GRID_PROFILE_REGION: "INVALID"}
    )
    assert result["errors"] == {CONF_GRID_PROFILE_REGION: "grid_profile_region_invalid"}

    runtime.regions_by_country["AU"] = []
    result = await handler.async_step_grid_profile()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "grid_profile_no_regions"


@pytest.mark.asyncio
async def test_options_flow_grid_profile_localizes_status_labels(
    hass, monkeypatch
) -> None:
    runtime = _options_flow_grid_profile_runtime()
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SITE_ID: "12345"})
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(grid_profile_runtime=runtime)
    )
    handler = OptionsFlowHandler(entry)
    handler.hass = hass
    monkeypatch.setattr(hass.config, "language", "de")

    await handler._async_prime_grid_profile_labels()  # noqa: SLF001

    assert handler._grid_profile_flag_label(True) == "Ja"  # noqa: SLF001
    assert handler._grid_profile_flag_label(False) == "Nein"  # noqa: SLF001
    assert handler._grid_profile_flag_label(None) == "Unbekannt"  # noqa: SLF001
    assert (  # noqa: SLF001
        handler._grid_profile_status_label("accepted") == "Akzeptiert"
    )
    assert (  # noqa: SLF001
        handler._grid_profile_status_label("waiting_for_cloud") == "Waiting for cloud"
    )


@pytest.mark.asyncio
async def test_options_flow_grid_profile_empty_and_stale_selection_paths(hass) -> None:
    runtime = _options_flow_grid_profile_runtime(with_profiles=False)
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SITE_ID: "12345"})
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(grid_profile_runtime=runtime)
    )
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    result = await handler.async_step_grid_profile_select()
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "grid_profile"

    runtime = _options_flow_grid_profile_runtime()
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(grid_profile_runtime=runtime)
    )
    result = await handler.async_step_grid_profile_confirm()
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "grid_profile_select"


@pytest.mark.asyncio
async def test_options_flow_grid_profile_reports_translated_apply_failure(hass) -> None:
    runtime = _options_flow_grid_profile_runtime(with_gateway=True)
    runtime.set_staged_profile("agf:test")
    runtime.async_apply_staged = AsyncMock(  # type: ignore[method-assign]
        side_effect=ServiceValidationError(
            "apply failed",
            translation_key="grid_profile_apply_failed",
        )
    )
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SITE_ID: "12345"})
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(grid_profile_runtime=runtime)
    )
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    result = await handler.async_step_grid_profile_confirm(
        {CONF_GRID_PROFILE_CONFIRM_APPLY: True}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "grid_profile_apply_failed"}


@pytest.mark.asyncio
async def test_options_flow_grid_profile_form_does_not_fetch_profiles(hass) -> None:
    runtime = _options_flow_grid_profile_runtime()
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SITE_ID: "12345"})
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(grid_profile_runtime=runtime)
    )
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    result = await handler.async_step_grid_profile()

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "grid_profile"
    runtime.async_refresh.assert_not_awaited()
    runtime.async_load_profiles.assert_not_awaited()


@pytest.mark.asyncio
async def test_options_flow_grid_profile_stages_profile_before_confirm(hass) -> None:
    runtime = _options_flow_grid_profile_runtime()
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SITE_ID: "12345"})
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(grid_profile_runtime=runtime)
    )
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    result = await handler.async_step_grid_profile(
        {
            CONF_GRID_PROFILE_REGION: "ACT",
            CONF_GRID_PROFILE_COMMONLY_USED: COMMONLY_USED_OPTION,
        }
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "grid_profile_select"
    runtime.async_refresh.assert_not_awaited()
    runtime.async_load_profiles.assert_awaited_once_with(
        region_code="ACT",
        commonly_used=True,
        force=True,
    )
    assert runtime.staged_query == ""

    result = await handler.async_step_grid_profile_select(
        {CONF_GRID_PROFILE_ID: "agf:test"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "grid_profile_confirm"
    assert runtime.staged_profile_id == "agf:test"
    assert result["description_placeholders"]["selected_profile"] == (
        "ACT, AU: Test Grid Profile"
    )
    assert result["description_placeholders"]["selected_profile_id"] == "agf:test"
    assert result["description_placeholders"]["selected_profile_pel"] == "Yes"
    assert result["description_placeholders"]["selected_profile_277v"] == "No"
    assert result["data_schema"].schema == {}

    result = await handler.async_step_grid_profile_confirm(
        {CONF_GRID_PROFILE_CONFIRM_APPLY: True}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "grid_profile_confirm"
    assert result["errors"] == {"base": "grid_profile_gateway_required"}


@pytest.mark.asyncio
async def test_options_flow_grid_profile_rejects_profile_from_cached_other_region(
    hass,
) -> None:
    runtime = _options_flow_grid_profile_runtime()
    runtime.regions_by_country["AU"].append(ActivationRegion("AU", "VIC", "Victoria"))
    runtime.catalog_cache[("AU", "VIC", True)] = (
        9_999_999_999.0,
        [
            GridProfile(
                profile_id="agf:vic-only",
                name="Victoria only",
                group="VIC, AU",
                country="AU",
                state="VIC",
            )
        ],
    )
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SITE_ID: "12345"})
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(grid_profile_runtime=runtime)
    )
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    result = await handler.async_step_grid_profile_select(
        {CONF_GRID_PROFILE_ID: "agf:vic-only"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "grid_profile_select"
    assert result["errors"] == {CONF_GRID_PROFILE_ID: "grid_profile_profile_invalid"}


@pytest.mark.asyncio
async def test_options_flow_grid_profile_available_apply_requires_confirmation(
    hass,
) -> None:
    runtime = _options_flow_grid_profile_runtime(with_gateway=True)
    runtime.async_apply_grid_profile = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "success": True,
            "profile_id": "agf:test",
            "requested_profile_id": "agf:requested",
            "cloud_apply_status": "accepted",
        }
    )
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SITE_ID: "12345"})
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(grid_profile_runtime=runtime)
    )
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    await handler.async_step_grid_profile(
        {
            CONF_GRID_PROFILE_REGION: "ACT",
            CONF_GRID_PROFILE_COMMONLY_USED: COMMONLY_USED_OPTION,
        }
    )
    result = await handler.async_step_grid_profile_select(
        {CONF_GRID_PROFILE_ID: "agf:test"}
    )

    assert CONF_GRID_PROFILE_CONFIRM_APPLY in {
        key.schema for key in result["data_schema"].schema
    }

    result = await handler.async_step_grid_profile_confirm({})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "confirm_required"}

    result = await handler.async_step_grid_profile_confirm(
        {CONF_GRID_PROFILE_CONFIRM_APPLY: True}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "grid_profile_applied"
    assert result["description_placeholders"]["selected_profile"] == (
        "ACT, AU: Test Grid Profile"
    )
    assert result["description_placeholders"]["requested_profile_id"] == "agf:requested"
    assert result["description_placeholders"]["cloud_apply_status"] == "Accepted"
    runtime.async_apply_grid_profile.assert_awaited_once_with(
        "agf:test",
        region_code="ACT",
    )

    result = await handler.async_step_grid_profile_applied({})

    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.asyncio
async def test_options_flow_grid_profile_reports_empty_filter(hass) -> None:
    runtime = _options_flow_grid_profile_runtime(with_profiles=False)
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SITE_ID: "12345"})
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(grid_profile_runtime=runtime)
    )
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    result = await handler.async_step_grid_profile(
        {
            CONF_GRID_PROFILE_REGION: "ACT",
            CONF_GRID_PROFILE_COMMONLY_USED: ALL_PROFILES_OPTION,
        }
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "grid_profile"
    assert result["errors"] == {"base": "grid_profile_no_profiles"}


@pytest.mark.asyncio
async def test_options_flow_grid_profile_aborts_when_catalog_denies_access(
    hass,
) -> None:
    runtime = _options_flow_grid_profile_runtime(with_profiles=False)

    async def deny_catalog_access(**_kwargs: object) -> list[object]:
        runtime.support_state = SUPPORT_DENIED
        return []

    runtime.async_load_profiles = AsyncMock(side_effect=deny_catalog_access)  # type: ignore[method-assign]
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SITE_ID: "12345"})
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(grid_profile_runtime=runtime)
    )
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    result = await handler.async_step_grid_profile(
        {
            CONF_GRID_PROFILE_REGION: "ACT",
            CONF_GRID_PROFILE_COMMONLY_USED: COMMONLY_USED_OPTION,
        }
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "grid_profile_installer_required"


@pytest.mark.asyncio
async def test_options_flow_grid_profile_reports_catalog_outage(hass) -> None:
    runtime = _options_flow_grid_profile_runtime(with_profiles=False)

    async def fail_catalog_access(**_kwargs: object) -> list[object]:
        runtime.support_state = SUPPORT_UNAVAILABLE
        return []

    runtime.async_load_profiles = AsyncMock(side_effect=fail_catalog_access)  # type: ignore[method-assign]
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SITE_ID: "12345"})
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(grid_profile_runtime=runtime)
    )
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    result = await handler.async_step_grid_profile(
        {
            CONF_GRID_PROFILE_REGION: "ACT",
            CONF_GRID_PROFILE_COMMONLY_USED: COMMONLY_USED_OPTION,
        }
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "grid_profile_unavailable"


@pytest.mark.asyncio
async def test_options_flow_show_form_with_defaults(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: "12345",
            CONF_SELECTED_TYPE_KEYS: ["envoy", "iqevse"],
            CONF_SERIALS: ["EV-1"],
        },
        options={},
    )
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    with patch.object(
        handler,
        "add_suggested_values_to_schema",
        wraps=handler.add_suggested_values_to_schema,
    ) as mock_add:
        result = await handler.async_step_settings()

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "settings"
    mock_add.assert_called_once()
    schema_keys = list(result["data_schema"].schema.keys())
    assert any(
        isinstance(key, VolOptional) and key.schema == CONF_TYPE_ENVOY
        for key in schema_keys
    )
    assert any(
        isinstance(key, VolOptional) and key.schema == CONF_TYPE_ENCHARGE
        for key in schema_keys
    )
    assert any(
        isinstance(key, VolOptional) and key.schema == CONF_TYPE_IQEVSE
        for key in schema_keys
    )
    assert any(
        isinstance(key, VolOptional) and key.schema == CONF_TYPE_HEATPUMP
        for key in schema_keys
    )
    assert any(
        isinstance(key, VolOptional) and key.schema == CONF_TYPE_MICROINVERTER
        for key in schema_keys
    )
    assert not any(
        isinstance(key, VolOptional) and key.schema == CONF_SCAN_INTERVAL
        for key in schema_keys
    )
    assert not any(
        isinstance(key, VolOptional) and key.schema == CONF_SITE_ONLY
        for key in schema_keys
    )
    validated = result["data_schema"]({})
    assert validated[OPT_SCHEDULE_SYNC_ENABLED] is True
    assert validated[OPT_BATTERY_SCHEDULES_ENABLED] is True
    assert validated[OPT_DEGRADED_SERVICE_REPAIR_ISSUES] is True
    assert validated[OPT_WEATHER_ENABLED] is False


@pytest.mark.asyncio
async def test_options_flow_show_form_uses_existing_options(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: "12345",
            CONF_SELECTED_TYPE_KEYS: ["envoy", "microinverter"],
        },
        options={
            OPT_FAST_POLL_INTERVAL: 5,
            OPT_SLOW_POLL_INTERVAL: 120,
            OPT_FAST_WHILE_STREAMING: False,
            OPT_API_TIMEOUT: 25,
            OPT_NOMINAL_VOLTAGE: 230,
            OPT_SESSION_HISTORY_INTERVAL: 30,
            OPT_SCHEDULE_SYNC_ENABLED: False,
            OPT_BATTERY_SCHEDULES_ENABLED: True,
            OPT_DEGRADED_SERVICE_REPAIR_ISSUES: False,
            OPT_WEATHER_ENABLED: True,
            CONF_SITE_ONLY: True,
        },
    )
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    result = await handler.async_step_settings()

    assert result["type"] is FlowResultType.FORM
    schema = result["data_schema"]
    validated = schema({})
    assert validated[CONF_TYPE_ENVOY] is True
    assert validated[CONF_TYPE_ENCHARGE] is False
    assert validated[CONF_TYPE_IQEVSE] is False
    assert validated[CONF_TYPE_HEATPUMP] is False
    assert validated[CONF_TYPE_MICROINVERTER] is True
    assert validated[OPT_FAST_POLL_INTERVAL] == MIN_FAST_POLL_INTERVAL
    assert validated[OPT_SLOW_POLL_INTERVAL] == 120
    assert validated[OPT_FAST_WHILE_STREAMING] is False
    assert validated[OPT_API_TIMEOUT] == 25
    assert validated[OPT_NOMINAL_VOLTAGE] == 230
    assert validated[OPT_SESSION_HISTORY_INTERVAL] == 30
    assert validated[OPT_SCHEDULE_SYNC_ENABLED] is False
    assert validated[OPT_BATTERY_SCHEDULES_ENABLED] is True
    assert validated[OPT_DEGRADED_SERVICE_REPAIR_ISSUES] is False
    assert validated[OPT_WEATHER_ENABLED] is True
    assert CONF_SCAN_INTERVAL not in validated
    assert CONF_SITE_ONLY not in validated


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        (OPT_API_TIMEOUT, MIN_API_TIMEOUT - 1),
        (OPT_API_TIMEOUT, MAX_API_TIMEOUT + 1),
        (OPT_SESSION_HISTORY_INTERVAL, MIN_SESSION_HISTORY_INTERVAL_MIN - 1),
        (OPT_SESSION_HISTORY_INTERVAL, MAX_SESSION_HISTORY_INTERVAL_MIN + 1),
    ],
)
async def test_options_flow_rejects_timeout_options_outside_range(
    hass, field: str, value: int
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: "12345",
            CONF_SELECTED_TYPE_KEYS: ["envoy", "microinverter"],
        },
        options={},
    )
    entry.add_to_hass(hass)
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    result = await handler.async_step_settings(
        {
            CONF_TYPE_ENVOY: True,
            CONF_TYPE_ENCHARGE: False,
            CONF_TYPE_AC_BATTERY: False,
            CONF_TYPE_IQEVSE: False,
            CONF_TYPE_HEATPUMP: False,
            CONF_TYPE_MICROINVERTER: True,
            OPT_FAST_POLL_INTERVAL: 45,
            OPT_SLOW_POLL_INTERVAL: 60,
            OPT_API_TIMEOUT: 15,
            OPT_SESSION_HISTORY_INTERVAL: 10,
            field: value,
        }
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "settings"
    assert result["errors"] == {field: "unknown"}


@pytest.mark.asyncio
async def test_options_flow_legacy_site_only_not_flipped_by_unrelated_save(
    hass,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: "12345",
            CONF_SERIALS: ["EV-OLD"],
            CONF_SITE_ONLY: True,
            CONF_INCLUDE_INVERTERS: True,
        },
        options={OPT_FAST_POLL_INTERVAL: 30},
    )
    entry.add_to_hass(hass)
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    result = await handler.async_step_init({OPT_FAST_POLL_INTERVAL: 45})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_SELECTED_TYPE_KEYS] == ["envoy", "encharge", "microinverter"]
    assert entry.data[CONF_SITE_ONLY] is True
    assert entry.data[CONF_SERIALS] == []
    assert result["data"][OPT_FAST_POLL_INTERVAL] == 45


@pytest.mark.asyncio
async def test_options_flow_normalizes_poll_intervals_on_save(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: "12345",
            CONF_SELECTED_TYPE_KEYS: ["envoy", "microinverter"],
        },
        options={},
    )
    entry.add_to_hass(hass)
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    result = await handler.async_step_settings(
        {
            CONF_TYPE_ENVOY: True,
            CONF_TYPE_ENCHARGE: False,
            CONF_TYPE_AC_BATTERY: False,
            CONF_TYPE_IQEVSE: False,
            CONF_TYPE_HEATPUMP: False,
            CONF_TYPE_MICROINVERTER: True,
            OPT_FAST_POLL_INTERVAL: 45,
            OPT_SLOW_POLL_INTERVAL: MIN_SLOW_POLL_INTERVAL,
            OPT_FAST_WHILE_STREAMING: True,
            OPT_API_TIMEOUT: 15.0,
            OPT_NOMINAL_VOLTAGE: 230,
            OPT_SESSION_HISTORY_INTERVAL: 10,
            OPT_SCHEDULE_SYNC_ENABLED: False,
            OPT_BATTERY_SCHEDULES_ENABLED: False,
            OPT_DEGRADED_SERVICE_REPAIR_ISSUES: False,
            "reauth": False,
            "forget_password": False,
        }
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][OPT_API_TIMEOUT] == 15
    assert result["data"][OPT_FAST_POLL_INTERVAL] == 45
    assert result["data"][OPT_SLOW_POLL_INTERVAL] == 60
    assert result["data"][OPT_DEGRADED_SERVICE_REPAIR_ISSUES] is False


@pytest.mark.asyncio
async def test_options_flow_rejects_non_integral_api_timeout_float(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: "12345",
            CONF_SELECTED_TYPE_KEYS: ["envoy", "microinverter"],
        },
        options={},
    )
    entry.add_to_hass(hass)
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    result = await handler.async_step_settings(
        {
            CONF_TYPE_ENVOY: True,
            CONF_TYPE_ENCHARGE: False,
            CONF_TYPE_AC_BATTERY: False,
            CONF_TYPE_IQEVSE: False,
            CONF_TYPE_HEATPUMP: False,
            CONF_TYPE_MICROINVERTER: True,
            OPT_FAST_POLL_INTERVAL: 45,
            OPT_SLOW_POLL_INTERVAL: MIN_SLOW_POLL_INTERVAL,
            OPT_FAST_WHILE_STREAMING: True,
            OPT_API_TIMEOUT: 15.5,
            OPT_NOMINAL_VOLTAGE: 230,
            OPT_SESSION_HISTORY_INTERVAL: 10,
            OPT_SCHEDULE_SYNC_ENABLED: False,
            OPT_BATTERY_SCHEDULES_ENABLED: False,
        }
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {OPT_API_TIMEOUT: "unknown"}


@pytest.mark.asyncio
async def test_options_flow_updates_selected_device_categories_in_data(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: "12345",
            CONF_SELECTED_TYPE_KEYS: ["envoy", "encharge", "iqevse", "microinverter"],
            CONF_SERIALS: ["EV-1"],
            CONF_SITE_ONLY: False,
            CONF_INCLUDE_INVERTERS: True,
        },
        options={},
    )
    entry.add_to_hass(hass)

    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    result = await handler.async_step_init(
        {
            CONF_TYPE_ENVOY: True,
            CONF_TYPE_ENCHARGE: True,
            CONF_TYPE_IQEVSE: False,
            CONF_TYPE_MICROINVERTER: False,
        }
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_SELECTED_TYPE_KEYS] == ["envoy", "encharge"]
    assert entry.data[CONF_SITE_ONLY] is True
    assert entry.data[CONF_INCLUDE_INVERTERS] is False
    assert entry.data[CONF_SERIALS] == []
    assert CONF_TYPE_ENVOY not in result["data"]
    assert CONF_TYPE_ENCHARGE not in result["data"]
    assert CONF_TYPE_IQEVSE not in result["data"]
    assert CONF_TYPE_MICROINVERTER not in result["data"]


@pytest.mark.asyncio
async def test_options_flow_enabling_iqevse_discovers_serials(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: "12345",
            CONF_EAUTH: "token-abc",
            CONF_COOKIE: "jar=1",
            CONF_SELECTED_TYPE_KEYS: ["envoy", "encharge"],
            CONF_SERIALS: [],
            CONF_INCLUDE_INVERTERS: False,
        },
        options={},
    )
    entry.add_to_hass(hass)
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_chargers",
            AsyncMock(
                return_value=[ChargerInfo(serial="EV-DISCOVERED", name="Garage")]
            ),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_devices_inventory",
            AsyncMock(return_value={"result": []}),
        ) as mock_inventory,
    ):
        result = await handler.async_step_init(
            {
                CONF_TYPE_ENVOY: True,
                CONF_TYPE_ENCHARGE: True,
                CONF_TYPE_IQEVSE: True,
                CONF_TYPE_MICROINVERTER: False,
            }
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_SERIALS] == ["EV-DISCOVERED"]
    assert entry.data[CONF_SITE_ONLY] is False
    assert entry.data[CONF_SELECTED_TYPE_KEYS] == ["envoy", "encharge", "iqevse"]
    mock_inventory.assert_awaited_once()


@pytest.mark.asyncio
async def test_options_flow_iqevse_without_serials_shows_error(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: "12345",
            CONF_EAUTH: "token-abc",
            CONF_COOKIE: "jar=1",
            CONF_SELECTED_TYPE_KEYS: ["envoy", "encharge"],
            CONF_SERIALS: [],
            CONF_INCLUDE_INVERTERS: False,
        },
        options={},
    )
    entry.add_to_hass(hass)
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_chargers",
            AsyncMock(return_value=[]),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_devices_inventory",
            AsyncMock(return_value={"result": []}),
        ),
    ):
        result = await handler.async_step_init(
            {
                CONF_TYPE_ENVOY: True,
                CONF_TYPE_ENCHARGE: True,
                CONF_TYPE_IQEVSE: True,
                CONF_TYPE_MICROINVERTER: False,
            }
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "serials_required"}
    assert entry.data[CONF_SELECTED_TYPE_KEYS] == ["envoy", "encharge"]
    assert entry.data[CONF_SERIALS] == []


@pytest.mark.asyncio
async def test_options_flow_submit_skips_unmapped_type_fields(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: "12345",
            CONF_EAUTH: "token-abc",
            CONF_COOKIE: "jar=1",
            CONF_SELECTED_TYPE_KEYS: ["iqevse"],
            CONF_SERIALS: ["EV-1"],
        },
        options={},
    )
    entry.add_to_hass(hass)
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    with patch.dict(
        "custom_components.enphase_ev.config_flow._TYPE_FIELD_BY_KEY",
        {"iqevse": CONF_TYPE_IQEVSE},
        clear=True,
    ):
        result = await handler.async_step_init({CONF_TYPE_IQEVSE: True})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_SELECTED_TYPE_KEYS] == ["iqevse"]


@pytest.mark.asyncio
async def test_options_flow_preserves_unknown_selected_type_keys(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: "12345",
            CONF_SELECTED_TYPE_KEYS: ["envoy", "generator"],
            CONF_SERIALS: [],
            CONF_INCLUDE_INVERTERS: False,
        },
        options={},
    )
    entry.add_to_hass(hass)
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    result = await handler.async_step_init(
        {
            CONF_TYPE_ENVOY: True,
            CONF_TYPE_ENCHARGE: False,
            CONF_TYPE_IQEVSE: False,
            CONF_TYPE_MICROINVERTER: False,
        }
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_SELECTED_TYPE_KEYS] == ["envoy", "generator"]


@pytest.mark.asyncio
async def test_options_flow_reauth_not_blocked_by_missing_iqevse_serials(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: "12345",
            CONF_EAUTH: "token-abc",
            CONF_COOKIE: "jar=1",
            CONF_SELECTED_TYPE_KEYS: ["envoy", "iqevse"],
            CONF_SERIALS: [],
            CONF_INCLUDE_INVERTERS: False,
        },
        options={},
    )
    entry.add_to_hass(hass)
    entry.async_start_reauth = Mock()
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_chargers",
            AsyncMock(return_value=[]),
        ) as mock_chargers,
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_devices_inventory",
            AsyncMock(return_value={"result": []}),
        ) as mock_inventory,
    ):
        result = await handler.async_step_init(
            {
                CONF_TYPE_ENVOY: True,
                CONF_TYPE_ENCHARGE: False,
                CONF_TYPE_IQEVSE: True,
                CONF_TYPE_MICROINVERTER: False,
                "reauth": True,
            }
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry.async_start_reauth.assert_called_once_with(hass, data=entry.data)
    mock_chargers.assert_not_awaited()
    mock_inventory.assert_not_awaited()


@pytest.mark.asyncio
async def test_options_flow_reauth_updates_entry_before_starting_flow(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: "12345",
            CONF_SELECTED_TYPE_KEYS: ["envoy"],
            CONF_SERIALS: [],
            CONF_INCLUDE_INVERTERS: False,
            CONF_SITE_ONLY: True,
        },
    )
    entry.add_to_hass(hass)
    captured: dict[str, object] = {}

    def _start_reauth(*args, **kwargs) -> None:
        captured["entry_data"] = dict(entry.data)
        captured["args"] = args
        captured["kwargs"] = kwargs

    entry.async_start_reauth = Mock(side_effect=_start_reauth)
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    result = await handler.async_step_init(
        {
            CONF_TYPE_ENVOY: True,
            CONF_TYPE_ENCHARGE: False,
            CONF_TYPE_IQEVSE: False,
            CONF_TYPE_MICROINVERTER: True,
            CONF_TYPE_HEATPUMP: True,
            "reauth": True,
        }
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert captured["args"] == (hass,)
    assert captured["entry_data"] == entry.data
    assert captured["kwargs"]["data"] == entry.data
    assert entry.data[CONF_SELECTED_TYPE_KEYS] == [
        "envoy",
        "heatpump",
        "microinverter",
    ]
    assert entry.data[CONF_INCLUDE_INVERTERS] is True


@pytest.mark.asyncio
async def test_options_flow_migrate_envoy_shows_source_step_for_multiple_entries(
    hass,
    monkeypatch,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "12345"}
    )
    entry.add_to_hass(hass)
    envoy_a = MockConfigEntry(
        domain="enphase_envoy", entry_id="envoy-a", title="Envoy A"
    )
    envoy_b = MockConfigEntry(
        domain="enphase_envoy", entry_id="envoy-b", title="Envoy B"
    )
    _patch_entry_lookup(monkeypatch, hass, envoy_a, envoy_b)

    attrs = {
        "device_class": "energy",
        "state_class": "total_increasing",
        "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
    }
    _add_registry_sensor(
        hass,
        entry=entry,
        platform=DOMAIN,
        unique_id=migration_target_unique_id("12345", "solar_production"),
        object_id="site_solar_production",
        state="3.0",
        attrs=attrs,
    )
    _add_registry_sensor(
        hass,
        entry=envoy_a,
        platform="enphase_envoy",
        unique_id="envoy-a-prod",
        object_id="envoy_a_lifetime_production",
        state="3.0",
        attrs=attrs,
    )
    _add_registry_sensor(
        hass,
        entry=envoy_b,
        platform="enphase_envoy",
        unique_id="envoy-b-prod",
        object_id="envoy_b_lifetime_production",
        state="3.0",
        attrs=attrs,
    )

    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    result = await handler.async_step_migrate_envoy()

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "migrate_envoy_source"


@pytest.mark.asyncio
async def test_options_flow_migrate_envoy_aborts_without_sources_or_targets(
    hass,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "12345"}
    )
    entry.add_to_hass(hass)
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    result = await handler.async_step_migrate_envoy()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "migration_no_envoy_sources"

    handler._migration_sources = [
        config_flow.EnvoyHistorySource("envoy-a", "Envoy", [])
    ]
    result = await handler.async_step_migrate_envoy()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "migration_no_targets"


@pytest.mark.asyncio
async def test_options_flow_migrate_envoy_uses_statistics_for_unloaded_sources(
    hass,
    monkeypatch,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "12345"}
    )
    entry.add_to_hass(hass)
    envoy = MockConfigEntry(domain="enphase_envoy", entry_id="envoy-a", title="Envoy A")
    _patch_entry_lookup(monkeypatch, hass, envoy)

    attrs = {
        "device_class": "energy",
        "state_class": "total_increasing",
        "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
    }
    _add_registry_sensor(
        hass,
        entry=entry,
        platform=DOMAIN,
        unique_id=migration_target_unique_id("12345", "solar_production"),
        object_id="site_solar_production",
        state="3.0",
        attrs=attrs,
    )
    old_entity = _add_registry_sensor(
        hass,
        entry=envoy,
        platform="enphase_envoy",
        unique_id="envoy-a-prod",
        object_id="envoy_lifetime_production",
        state=None,
        attrs=None,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.envoy_history.recorder_statistics.async_list_statistic_ids",
        AsyncMock(
            return_value=[
                {
                    "statistic_id": old_entity,
                    "has_sum": True,
                    "name": "Lifetime PV",
                    "statistics_unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
                }
            ]
        ),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.envoy_history.recorder_statistics.get_last_statistics",
        lambda *_args, **_kwargs: {old_entity: [{"sum": 3.0}]},
    )

    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    result = await handler.async_step_migrate_envoy()

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "migrate_envoy_intro"


@pytest.mark.asyncio
async def test_options_flow_migrate_envoy_source_accepts_selected_entry(
    hass, monkeypatch
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "12345"}
    )
    entry.add_to_hass(hass)
    envoy_a = MockConfigEntry(
        domain="enphase_envoy", entry_id="envoy-a", title="Envoy A"
    )
    _patch_entry_lookup(monkeypatch, hass, envoy_a)

    attrs = {
        "device_class": "energy",
        "state_class": "total_increasing",
        "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
    }
    _add_registry_sensor(
        hass,
        entry=entry,
        platform=DOMAIN,
        unique_id=migration_target_unique_id("12345", "solar_production"),
        object_id="site_solar_production",
        state="3.0",
        attrs=attrs,
    )
    _add_registry_sensor(
        hass,
        entry=envoy_a,
        platform="enphase_envoy",
        unique_id="envoy-a-prod",
        object_id="envoy_lifetime_production",
        state="3.0",
        attrs=attrs,
    )

    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    result = await handler.async_step_migrate_envoy_source(
        {config_flow.CONF_MIGRATION_SOURCE_ENTRY: "envoy-a"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "migrate_envoy_intro"
    assert handler._selected_migration_source_id == "envoy-a"
    assert handler._migration_selection == {}


@pytest.mark.asyncio
async def test_options_flow_migrate_envoy_intro_requires_backup(
    hass, monkeypatch
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "12345"}
    )
    entry.add_to_hass(hass)
    envoy = MockConfigEntry(domain="enphase_envoy", entry_id="envoy-a", title="Envoy A")
    _patch_entry_lookup(monkeypatch, hass, envoy)

    attrs = {
        "device_class": "energy",
        "state_class": "total_increasing",
        "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
    }
    _add_registry_sensor(
        hass,
        entry=entry,
        platform=DOMAIN,
        unique_id=migration_target_unique_id("12345", "solar_production"),
        object_id="site_solar_production",
        state="3.0",
        attrs=attrs,
    )
    _add_registry_sensor(
        hass,
        entry=envoy,
        platform="enphase_envoy",
        unique_id="envoy-a-prod",
        object_id="envoy_lifetime_production",
        state="3.0",
        attrs=attrs,
    )

    handler = OptionsFlowHandler(entry)
    handler.hass = hass
    handler._selected_migration_source_id = "envoy-a"

    result = await handler.async_step_migrate_envoy_intro(
        {CONF_MIGRATION_BACKUP_CONFIRMED: False}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "migrate_envoy_intro"
    assert result["errors"] == {"base": "backup_required"}


@pytest.mark.asyncio
async def test_options_flow_migrate_intro_redirects_when_source_missing(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "12345"}
    )
    entry.add_to_hass(hass)
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    result = await handler.async_step_migrate_envoy_intro()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "migration_no_envoy_sources"


@pytest.mark.asyncio
async def test_options_flow_migrate_intro_advances_when_backup_confirmed(
    hass, monkeypatch
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "12345"}
    )
    entry.add_to_hass(hass)
    envoy = MockConfigEntry(domain="enphase_envoy", entry_id="envoy-a", title="Envoy A")
    _patch_entry_lookup(monkeypatch, hass, envoy)
    attrs = {
        "device_class": "energy",
        "state_class": "total_increasing",
        "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
    }
    _add_registry_sensor(
        hass,
        entry=entry,
        platform=DOMAIN,
        unique_id=migration_target_unique_id("12345", "solar_production"),
        object_id="site_solar_production",
        state="3.0",
        attrs=attrs,
    )
    _add_registry_sensor(
        hass,
        entry=envoy,
        platform="enphase_envoy",
        unique_id="envoy-a-prod",
        object_id="envoy_lifetime_production",
        state="3.0",
        attrs=attrs,
    )
    handler = OptionsFlowHandler(entry)
    handler.hass = hass
    handler._selected_migration_source_id = "envoy-a"

    result = await handler.async_step_migrate_envoy_intro(
        {CONF_MIGRATION_BACKUP_CONFIRMED: True}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "migrate_envoy_mapping"


@pytest.mark.asyncio
async def test_options_flow_migrate_mapping_preselects_candidates(
    hass, monkeypatch
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "12345"}
    )
    entry.add_to_hass(hass)
    envoy = MockConfigEntry(domain="enphase_envoy", entry_id="envoy-a", title="Envoy A")
    _patch_entry_lookup(monkeypatch, hass, envoy)

    attrs = {
        "device_class": "energy",
        "state_class": "total_increasing",
        "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
    }
    prod_entity = _add_registry_sensor(
        hass,
        entry=envoy,
        platform="enphase_envoy",
        unique_id="envoy-a-prod",
        object_id="envoy_lifetime_production",
        state="5.0",
        attrs=attrs,
    )
    cons_entity = _add_registry_sensor(
        hass,
        entry=envoy,
        platform="enphase_envoy",
        unique_id="envoy-a-cons",
        object_id="envoy_lifetime_consumption",
        state="4.0",
        attrs=attrs,
    )
    _add_registry_sensor(
        hass,
        entry=entry,
        platform=DOMAIN,
        unique_id=migration_target_unique_id("12345", "solar_production"),
        object_id="site_solar_production",
        state="5.1",
        attrs=attrs,
    )
    _add_registry_sensor(
        hass,
        entry=entry,
        platform=DOMAIN,
        unique_id=migration_target_unique_id("12345", "consumption"),
        object_id="site_consumption",
        state="4.2",
        attrs=attrs,
    )

    handler = OptionsFlowHandler(entry)
    handler.hass = hass
    handler._selected_migration_source_id = "envoy-a"

    result = await handler.async_step_migrate_envoy_mapping()

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "migrate_envoy_mapping"
    validated = result["data_schema"]({})
    assert validated["solar_production"] == prod_entity
    assert validated["consumption"] == cons_entity


@pytest.mark.asyncio
async def test_options_flow_migrate_mapping_has_no_skip_default_without_suggestion(
    hass,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "12345"}
    )
    handler = OptionsFlowHandler(entry)
    handler.hass = hass
    handler._migration_targets = {
        "solar_production": config_flow.EnvoyHistoryTarget(
            flow_key="solar_production",
            label="Site Solar Production",
            unique_id="uid",
            entity_id="sensor.site_solar_production",
            current_value_kwh=1.0,
        )
    }
    schema = handler._build_migration_mapping_schema(
        config_flow.EnvoyHistorySource("envoy-a", "Envoy", []),
        [],
        {},
    )

    marker = next(iter(schema.schema))
    assert marker.schema == "solar_production"
    assert marker.default is vol.UNDEFINED


@pytest.mark.asyncio
async def test_options_flow_migrate_mapping_includes_external_compatible_sensors(
    hass, monkeypatch
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "12345"}
    )
    entry.add_to_hass(hass)
    envoy = MockConfigEntry(domain="enphase_envoy", entry_id="envoy-a", title="Envoy A")
    template_entry = MockConfigEntry(domain="template", entry_id="template-entry")
    _patch_entry_lookup(monkeypatch, hass, envoy, template_entry)

    attrs = {
        "device_class": "energy",
        "state_class": "total_increasing",
        "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
    }
    envoy_entity = _add_registry_sensor(
        hass,
        entry=envoy,
        platform="enphase_envoy",
        unique_id="envoy-a-prod",
        object_id="envoy_lifetime_production",
        state="5.0",
        attrs=attrs,
    )
    template_entity = _add_registry_sensor(
        hass,
        entry=template_entry,
        platform="template",
        unique_id="template-total",
        object_id="template_lifetime_production",
        state="5.0",
        attrs=attrs,
    )
    _add_registry_sensor(
        hass,
        entry=entry,
        platform=DOMAIN,
        unique_id=migration_target_unique_id("12345", "solar_production"),
        object_id="site_solar_production",
        state="5.1",
        attrs=attrs,
    )

    handler = OptionsFlowHandler(entry)
    handler.hass = hass
    handler._selected_migration_source_id = "envoy-a"

    result = await handler.async_step_migrate_envoy_mapping()

    assert result["type"] is FlowResultType.FORM
    schema = result["data_schema"]
    selector_config = next(iter(schema.schema.values())).config
    options = selector_config["options"]
    assert [option["value"] for option in options] == [
        skip_option_value(),
        envoy_entity,
        template_entity,
    ]


@pytest.mark.asyncio
async def test_options_flow_migrate_mapping_handles_user_input_paths(
    hass, monkeypatch
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "12345"}
    )
    entry.add_to_hass(hass)
    envoy = MockConfigEntry(domain="enphase_envoy", entry_id="envoy-a", title="Envoy A")
    _patch_entry_lookup(monkeypatch, hass, envoy)
    attrs = {
        "device_class": "energy",
        "state_class": "total_increasing",
        "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
    }
    old_entity = _add_registry_sensor(
        hass,
        entry=envoy,
        platform="enphase_envoy",
        unique_id="envoy-a-prod",
        object_id="envoy_lifetime_production",
        state="5.0",
        attrs=attrs,
    )
    _add_registry_sensor(
        hass,
        entry=entry,
        platform=DOMAIN,
        unique_id=migration_target_unique_id("12345", "solar_production"),
        object_id="site_solar_production",
        state="5.1",
        attrs=attrs,
    )
    handler = OptionsFlowHandler(entry)
    handler.hass = hass
    handler._selected_migration_source_id = "envoy-a"

    result = await handler.async_step_migrate_envoy_mapping(
        {"solar_production": skip_option_value()}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "migration_no_selection"}

    result = await handler.async_step_migrate_envoy_mapping(
        {"solar_production": old_entity}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "migrate_envoy_confirm"


@pytest.mark.asyncio
async def test_options_flow_migrate_confirm_preview_excludes_skipped_suggestions(
    hass, monkeypatch
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "12345"}
    )
    entry.add_to_hass(hass)
    envoy = MockConfigEntry(domain="enphase_envoy", entry_id="envoy-a", title="Envoy A")
    _patch_entry_lookup(monkeypatch, hass, envoy)
    attrs = {
        "device_class": "energy",
        "state_class": "total_increasing",
        "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
    }
    prod_entity = _add_registry_sensor(
        hass,
        entry=envoy,
        platform="enphase_envoy",
        unique_id="envoy-a-prod",
        object_id="envoy_lifetime_production",
        state="5.0",
        attrs=attrs,
    )
    cons_entity = _add_registry_sensor(
        hass,
        entry=envoy,
        platform="enphase_envoy",
        unique_id="envoy-a-cons",
        object_id="envoy_lifetime_consumption",
        state="4.0",
        attrs=attrs,
    )
    _add_registry_sensor(
        hass,
        entry=entry,
        platform=DOMAIN,
        unique_id=migration_target_unique_id("12345", "solar_production"),
        object_id="site_solar_production",
        state="5.1",
        attrs=attrs,
    )
    _add_registry_sensor(
        hass,
        entry=entry,
        platform=DOMAIN,
        unique_id=migration_target_unique_id("12345", "consumption"),
        object_id="site_consumption",
        state="4.2",
        attrs=attrs,
    )
    handler = OptionsFlowHandler(entry)
    handler.hass = hass
    handler._selected_migration_source_id = "envoy-a"

    result = await handler.async_step_migrate_envoy_mapping(
        {
            "solar_production": prod_entity,
            "consumption": skip_option_value(),
        }
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "migrate_envoy_confirm"
    assert handler._migration_selection == {"solar_production": prod_entity}
    mapping_preview = result["description_placeholders"]["mapping_preview"]
    assert prod_entity in mapping_preview
    assert cons_entity not in mapping_preview


@pytest.mark.asyncio
async def test_options_flow_migrate_mapping_allows_lower_target_value(
    hass, monkeypatch
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "12345"}
    )
    entry.add_to_hass(hass)
    envoy = MockConfigEntry(domain="enphase_envoy", entry_id="envoy-a", title="Envoy A")
    _patch_entry_lookup(monkeypatch, hass, envoy)
    attrs = {
        "device_class": "energy",
        "state_class": "total_increasing",
        "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
    }
    old_entity = _add_registry_sensor(
        hass,
        entry=envoy,
        platform="enphase_envoy",
        unique_id="envoy-a-prod",
        object_id="envoy_lifetime_production",
        state="5.0",
        attrs=attrs,
    )
    _add_registry_sensor(
        hass,
        entry=entry,
        platform=DOMAIN,
        unique_id=migration_target_unique_id("12345", "solar_production"),
        object_id="site_solar_production",
        state="4.0",
        attrs=attrs,
    )
    handler = OptionsFlowHandler(entry)
    handler.hass = hass
    handler._selected_migration_source_id = "envoy-a"

    result = await handler.async_step_migrate_envoy_mapping(
        {"solar_production": old_entity}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "migrate_envoy_confirm"
    assert result["errors"] == {}


@pytest.mark.asyncio
async def test_options_flow_migrate_mapping_redirects_when_source_missing(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "12345"}
    )
    entry.add_to_hass(hass)
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    result = await handler.async_step_migrate_envoy_mapping()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "migration_no_envoy_sources"


@pytest.mark.asyncio
async def test_options_flow_migrate_confirm_temporarily_unloads_loaded_envoy(
    hass, monkeypatch
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "12345"}
    )
    entry.add_to_hass(hass)
    envoy = MockConfigEntry(domain="enphase_envoy", entry_id="envoy-a", title="Envoy A")
    _patch_entry_lookup(monkeypatch, hass, envoy)
    object.__setattr__(envoy, "state", config_entries.ConfigEntryState.LOADED)

    attrs = {
        "device_class": "energy",
        "state_class": "total_increasing",
        "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
    }
    old_entity = _add_registry_sensor(
        hass,
        entry=envoy,
        platform="enphase_envoy",
        unique_id="envoy-a-prod",
        object_id="envoy_lifetime_production",
        state="5.0",
        attrs=attrs,
    )
    _add_registry_sensor(
        hass,
        entry=entry,
        platform=DOMAIN,
        unique_id=migration_target_unique_id("12345", "solar_production"),
        object_id="site_solar_production",
        state="5.1",
        attrs=attrs,
    )

    handler = OptionsFlowHandler(entry)
    handler.hass = hass
    handler._selected_migration_source_id = "envoy-a"
    handler._migration_selection = {"solar_production": old_entity}

    async def _unload(entry_id):
        assert entry_id == "envoy-a"
        object.__setattr__(envoy, "state", config_entries.ConfigEntryState.NOT_LOADED)
        return True

    unload_spy = AsyncMock(side_effect=_unload)
    monkeypatch.setattr(hass.config_entries, "async_unload", unload_spy)
    reload_spy = AsyncMock(side_effect=[True, True])
    monkeypatch.setattr(hass.config_entries, "async_reload", reload_spy)

    result = await handler.async_step_migrate_envoy_confirm(
        {CONF_MIGRATION_CONFIRM_REASSIGN: True}
    )

    ent_reg = er.async_get(hass)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    unload_spy.assert_awaited_once_with("envoy-a")
    assert reload_spy.await_args_list[0].args == (entry.entry_id,)
    assert reload_spy.await_args_list[1].args == ("envoy-a",)
    assert ent_reg.async_get(old_entity).platform == DOMAIN
    archived_entity = ent_reg.async_get("sensor.envoy_lifetime_production_envoy_legacy")
    assert archived_entity is not None
    assert archived_entity.platform == "enphase_envoy"
    assert archived_entity.disabled_by == er.RegistryEntryDisabler.USER


@pytest.mark.asyncio
async def test_options_flow_migrate_confirm_redirects_when_source_or_selection_missing(
    hass,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "12345"}
    )
    entry.add_to_hass(hass)
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    result = await handler.async_step_migrate_envoy_confirm()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "migration_no_envoy_sources"

    handler._migration_sources = [
        config_flow.EnvoyHistorySource("envoy-a", "Envoy", [])
    ]
    handler._selected_migration_source_id = "envoy-a"
    handler._migration_targets = {
        "solar_production": config_flow.EnvoyHistoryTarget(
            flow_key="solar_production",
            label="Solar",
            unique_id="uid",
            entity_id="sensor.site_solar_production",
            current_value_kwh=1.0,
        )
    }
    result = await handler.async_step_migrate_envoy_confirm()
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "migrate_envoy_mapping"


@pytest.mark.asyncio
async def test_options_flow_migrate_mapping_suggests_external_candidate(
    hass, monkeypatch
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "12345"}
    )
    entry.add_to_hass(hass)
    envoy = MockConfigEntry(domain="enphase_envoy", entry_id="envoy-a", title="Envoy A")
    template_entry = MockConfigEntry(domain="template", entry_id="template-entry")
    _patch_entry_lookup(monkeypatch, hass, envoy, template_entry)

    attrs = {
        "device_class": "energy",
        "state_class": "total_increasing",
        "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
    }
    _add_registry_sensor(
        hass,
        entry=envoy,
        platform="enphase_envoy",
        unique_id="envoy-a-other",
        object_id="envoy_other_total",
        state="5.0",
        attrs=attrs,
    )
    template_entity = _add_registry_sensor(
        hass,
        entry=template_entry,
        platform="template",
        unique_id="template-total",
        object_id="template_total_energy",
        state="5.0",
        attrs={**attrs, "friendly_name": "Lifetime PV"},
    )
    _add_registry_sensor(
        hass,
        entry=entry,
        platform=DOMAIN,
        unique_id=migration_target_unique_id("12345", "solar_production"),
        object_id="site_solar_production",
        state="5.1",
        attrs=attrs,
    )

    handler = OptionsFlowHandler(entry)
    handler.hass = hass
    handler._selected_migration_source_id = "envoy-a"

    result = await handler.async_step_migrate_envoy_mapping()

    assert result["type"] is FlowResultType.FORM
    validated = result["data_schema"]({})
    assert validated["solar_production"] == template_entity


@pytest.mark.asyncio
async def test_options_flow_migrate_confirm_skips_envoy_unload_for_external_source_only(
    hass, monkeypatch
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "12345"}
    )
    entry.add_to_hass(hass)
    envoy = MockConfigEntry(domain="enphase_envoy", entry_id="envoy-a", title="Envoy A")
    template_entry = MockConfigEntry(domain="template", entry_id="template-entry")
    _patch_entry_lookup(monkeypatch, hass, envoy, template_entry)
    object.__setattr__(envoy, "state", config_entries.ConfigEntryState.LOADED)

    attrs = {
        "device_class": "energy",
        "state_class": "total_increasing",
        "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
    }
    _add_registry_sensor(
        hass,
        entry=envoy,
        platform="enphase_envoy",
        unique_id="envoy-a-other",
        object_id="envoy_other_total",
        state="5.0",
        attrs=attrs,
    )
    template_entity = _add_registry_sensor(
        hass,
        entry=template_entry,
        platform="template",
        unique_id="template-total",
        object_id="template_lifetime_production",
        state="5.0",
        attrs=attrs,
    )
    _add_registry_sensor(
        hass,
        entry=entry,
        platform=DOMAIN,
        unique_id=migration_target_unique_id("12345", "solar_production"),
        object_id="site_solar_production",
        state="5.1",
        attrs=attrs,
    )

    handler = OptionsFlowHandler(entry)
    handler.hass = hass
    handler._selected_migration_source_id = "envoy-a"
    handler._migration_selection = {"solar_production": template_entity}

    form = await handler.async_step_migrate_envoy_confirm()
    validated = form["data_schema"]({CONF_MIGRATION_CONFIRM_REASSIGN: False})
    assert validated[CONF_MIGRATION_DISABLE_ARCHIVED] is False

    unload_spy = AsyncMock(return_value=True)
    reload_spy = AsyncMock(return_value=True)
    monkeypatch.setattr(hass.config_entries, "async_unload", unload_spy)
    monkeypatch.setattr(hass.config_entries, "async_reload", reload_spy)

    result = await handler.async_step_migrate_envoy_confirm(
        {CONF_MIGRATION_CONFIRM_REASSIGN: True}
    )

    ent_reg = er.async_get(hass)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    unload_spy.assert_not_awaited()
    reload_spy.assert_awaited_once_with(entry.entry_id)
    assert ent_reg.async_get(template_entity).platform == DOMAIN
    archived_entity = ent_reg.async_get("sensor.template_lifetime_production_legacy")
    assert archived_entity is not None
    assert archived_entity.platform == "template"
    assert archived_entity.disabled_by is None


@pytest.mark.asyncio
async def test_options_flow_migrate_confirm_requires_confirmation(
    hass, monkeypatch
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "12345"}
    )
    entry.add_to_hass(hass)
    envoy = MockConfigEntry(domain="enphase_envoy", entry_id="envoy-a", title="Envoy A")
    _patch_entry_lookup(monkeypatch, hass, envoy)
    attrs = {
        "device_class": "energy",
        "state_class": "total_increasing",
        "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
    }
    old_entity = _add_registry_sensor(
        hass,
        entry=envoy,
        platform="enphase_envoy",
        unique_id="envoy-a-prod",
        object_id="envoy_lifetime_production",
        state="5.0",
        attrs=attrs,
    )
    _add_registry_sensor(
        hass,
        entry=entry,
        platform=DOMAIN,
        unique_id=migration_target_unique_id("12345", "solar_production"),
        object_id="site_solar_production",
        state="5.1",
        attrs=attrs,
    )
    handler = OptionsFlowHandler(entry)
    handler.hass = hass
    handler._selected_migration_source_id = "envoy-a"
    handler._migration_selection = {"solar_production": old_entity}

    result = await handler.async_step_migrate_envoy_confirm(
        {CONF_MIGRATION_CONFIRM_REASSIGN: False}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "confirm_required"}


@pytest.mark.asyncio
async def test_options_flow_migrate_confirm_errors_when_auto_unload_fails(
    hass, monkeypatch
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "12345"}
    )
    entry.add_to_hass(hass)
    envoy = MockConfigEntry(domain="enphase_envoy", entry_id="envoy-a", title="Envoy A")
    _patch_entry_lookup(monkeypatch, hass, envoy)
    object.__setattr__(envoy, "state", config_entries.ConfigEntryState.LOADED)

    attrs = {
        "device_class": "energy",
        "state_class": "total_increasing",
        "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
    }
    old_entity = _add_registry_sensor(
        hass,
        entry=envoy,
        platform="enphase_envoy",
        unique_id="envoy-a-prod",
        object_id="envoy_lifetime_production",
        state="5.0",
        attrs=attrs,
    )
    _add_registry_sensor(
        hass,
        entry=entry,
        platform=DOMAIN,
        unique_id=migration_target_unique_id("12345", "solar_production"),
        object_id="site_solar_production",
        state="5.1",
        attrs=attrs,
    )

    handler = OptionsFlowHandler(entry)
    handler.hass = hass
    handler._selected_migration_source_id = "envoy-a"
    handler._migration_selection = {"solar_production": old_entity}

    monkeypatch.setattr(
        hass.config_entries,
        "async_unload",
        AsyncMock(return_value=False),
    )

    result = await handler.async_step_migrate_envoy_confirm(
        {CONF_MIGRATION_CONFIRM_REASSIGN: True}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "migrate_envoy_confirm"
    assert result["errors"] == {"base": "envoy_entry_loaded"}


@pytest.mark.asyncio
async def test_options_flow_migrate_confirm_warns_for_lower_target_value_and_continues(
    hass, monkeypatch
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "12345"}
    )
    entry.add_to_hass(hass)
    envoy = MockConfigEntry(domain="enphase_envoy", entry_id="envoy-a", title="Envoy A")
    _patch_entry_lookup(monkeypatch, hass, envoy)
    object.__setattr__(envoy, "state", config_entries.ConfigEntryState.LOADED)
    attrs = {
        "device_class": "energy",
        "state_class": "total_increasing",
        "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
    }
    old_entity = _add_registry_sensor(
        hass,
        entry=envoy,
        platform="enphase_envoy",
        unique_id="envoy-a-prod",
        object_id="envoy_lifetime_production",
        state="5.0",
        attrs=attrs,
    )
    _add_registry_sensor(
        hass,
        entry=entry,
        platform=DOMAIN,
        unique_id=migration_target_unique_id("12345", "solar_production"),
        object_id="site_solar_production",
        state="4.0",
        attrs=attrs,
    )
    handler = OptionsFlowHandler(entry)
    handler.hass = hass
    handler._selected_migration_source_id = "envoy-a"
    handler._migration_selection = {"solar_production": old_entity}

    monkeypatch.setattr(
        hass.config_entries, "async_unload", AsyncMock(return_value=True)
    )
    reload_spy = AsyncMock(return_value=True)
    monkeypatch.setattr(hass.config_entries, "async_reload", reload_spy)

    form = await handler.async_step_migrate_envoy_confirm()

    assert form["type"] is FlowResultType.FORM
    assert form["errors"] == {}
    assert (
        form["description_placeholders"]["warning_preview"]
        == "\n\nWarning: selected Enphase Energy totals are currently lower than "
        "the existing source totals. Migration can still continue.\n"
        "- `Site Solar Production`: Enphase Energy `sensor.site_solar_production` "
        "= 4.00 kWh; existing `sensor.envoy_lifetime_production` = 5.00 kWh"
    )

    result = await handler.async_step_migrate_envoy_confirm(
        {CONF_MIGRATION_CONFIRM_REASSIGN: True}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert reload_spy.awaited


@pytest.mark.asyncio
async def test_options_flow_migrate_confirm_reloads_source_after_post_unload_validation_error(
    hass, monkeypatch
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "12345"}
    )
    entry.add_to_hass(hass)
    envoy = MockConfigEntry(domain="enphase_envoy", entry_id="envoy-a", title="Envoy A")
    _patch_entry_lookup(monkeypatch, hass, envoy)
    object.__setattr__(envoy, "state", config_entries.ConfigEntryState.LOADED)
    attrs = {
        "device_class": "energy",
        "state_class": "total_increasing",
        "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
    }
    old_entity = _add_registry_sensor(
        hass,
        entry=envoy,
        platform="enphase_envoy",
        unique_id="envoy-a-prod",
        object_id="envoy_lifetime_production",
        state="5.0",
        attrs=attrs,
    )
    new_entity = _add_registry_sensor(
        hass,
        entry=entry,
        platform=DOMAIN,
        unique_id=migration_target_unique_id("12345", "solar_production"),
        object_id="site_solar_production",
        state="5.1",
        attrs=attrs,
    )
    handler = OptionsFlowHandler(entry)
    handler.hass = hass
    handler._selected_migration_source_id = "envoy-a"
    handler._migration_selection = {"solar_production": old_entity}

    monkeypatch.setattr(
        hass.config_entries, "async_unload", AsyncMock(return_value=True)
    )
    reload_source_spy = AsyncMock(return_value=True)
    monkeypatch.setattr(
        handler, "_async_reload_migration_source_entry", reload_source_spy
    )

    success_validation = EnvoyHistoryValidation(
        None,
        [
            EnvoyHistoryMapping(
                flow_key="solar_production",
                label="Solar",
                old_entity_id=old_entity,
                archived_entity_id="sensor.envoy_lifetime_production_envoy_legacy",
                old_value_kwh=5.0,
                new_entity_id=new_entity,
                new_value_kwh=5.1,
                target_unique_id=migration_target_unique_id(
                    "12345", "solar_production"
                ),
            )
        ],
    )
    error_validation = EnvoyHistoryValidation("incompatible_energy_total", [])

    def _validate(*_args, require_source_unloaded=True, **_kwargs):
        return error_validation if require_source_unloaded else success_validation

    monkeypatch.setattr(
        "custom_components.enphase_ev.config_flow.validate_selected_mappings",
        _validate,
    )

    result = await handler.async_step_migrate_envoy_confirm(
        {CONF_MIGRATION_CONFIRM_REASSIGN: True}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "incompatible_energy_total"}
    reload_source_spy.assert_awaited_once()


@pytest.mark.asyncio
async def test_options_flow_migrate_confirm_omits_warning_preview_when_not_needed(
    hass, monkeypatch
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "12345"}
    )
    entry.add_to_hass(hass)
    envoy = MockConfigEntry(domain="enphase_envoy", entry_id="envoy-a", title="Envoy A")
    _patch_entry_lookup(monkeypatch, hass, envoy)
    attrs = {
        "device_class": "energy",
        "state_class": "total_increasing",
        "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
    }
    old_entity = _add_registry_sensor(
        hass,
        entry=envoy,
        platform="enphase_envoy",
        unique_id="envoy-a-prod",
        object_id="envoy_lifetime_production",
        state="5.0",
        attrs=attrs,
    )
    _add_registry_sensor(
        hass,
        entry=entry,
        platform=DOMAIN,
        unique_id=migration_target_unique_id("12345", "solar_production"),
        object_id="site_solar_production",
        state="5.1",
        attrs=attrs,
    )
    handler = OptionsFlowHandler(entry)
    handler.hass = hass
    handler._selected_migration_source_id = "envoy-a"
    handler._migration_selection = {"solar_production": old_entity}

    form = await handler.async_step_migrate_envoy_confirm()

    assert form["type"] is FlowResultType.FORM
    assert form["description_placeholders"]["warning_preview"] == ""


@pytest.mark.asyncio
async def test_options_flow_migrate_confirm_successfully_reassigns_entity_ids(
    hass, monkeypatch
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "12345"}
    )
    entry.add_to_hass(hass)
    envoy = MockConfigEntry(domain="enphase_envoy", entry_id="envoy-a", title="Envoy A")
    _patch_entry_lookup(monkeypatch, hass, envoy)
    object.__setattr__(envoy, "state", config_entries.ConfigEntryState.NOT_LOADED)

    attrs = {
        "device_class": "energy",
        "state_class": "total_increasing",
        "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
    }
    old_entity = _add_registry_sensor(
        hass,
        entry=envoy,
        platform="enphase_envoy",
        unique_id="envoy-a-prod",
        object_id="envoy_lifetime_production",
        state="5.0",
        attrs=attrs,
    )
    new_entity = _add_registry_sensor(
        hass,
        entry=entry,
        platform=DOMAIN,
        unique_id=migration_target_unique_id("12345", "solar_production"),
        object_id="site_solar_production",
        state="5.1",
        attrs=attrs,
    )

    handler = OptionsFlowHandler(entry)
    handler.hass = hass
    handler._selected_migration_source_id = "envoy-a"
    handler._migration_selection = {"solar_production": old_entity}

    reload_spy = AsyncMock(return_value=True)
    monkeypatch.setattr(hass.config_entries, "async_reload", reload_spy)

    result = await handler.async_step_migrate_envoy_confirm(
        {
            CONF_MIGRATION_CONFIRM_REASSIGN: True,
            CONF_MIGRATION_DISABLE_ARCHIVED: False,
        }
    )

    ent_reg = er.async_get(hass)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["description"] == "migration_success"
    reload_spy.assert_awaited_once_with(entry.entry_id)
    assert ent_reg.async_get(old_entity).platform == DOMAIN
    assert ent_reg.async_get(new_entity) is None
    archived_entity = ent_reg.async_get("sensor.envoy_lifetime_production_envoy_legacy")
    assert archived_entity is not None
    assert archived_entity.disabled_by is None


@pytest.mark.asyncio
async def test_options_flow_migrate_confirm_reports_current_reload_needed(
    hass, monkeypatch
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "12345"}
    )
    entry.add_to_hass(hass)
    envoy = MockConfigEntry(domain="enphase_envoy", entry_id="envoy-a", title="Envoy A")
    _patch_entry_lookup(monkeypatch, hass, envoy)
    object.__setattr__(envoy, "state", config_entries.ConfigEntryState.NOT_LOADED)

    attrs = {
        "device_class": "energy",
        "state_class": "total_increasing",
        "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
    }
    old_entity = _add_registry_sensor(
        hass,
        entry=envoy,
        platform="enphase_envoy",
        unique_id="envoy-a-prod",
        object_id="envoy_lifetime_production",
        state="5.0",
        attrs=attrs,
    )
    _add_registry_sensor(
        hass,
        entry=entry,
        platform=DOMAIN,
        unique_id=migration_target_unique_id("12345", "solar_production"),
        object_id="site_solar_production",
        state="5.1",
        attrs=attrs,
    )

    handler = OptionsFlowHandler(entry)
    handler.hass = hass
    handler._selected_migration_source_id = "envoy-a"
    handler._migration_selection = {"solar_production": old_entity}

    monkeypatch.setattr(
        hass.config_entries,
        "async_reload",
        AsyncMock(return_value=False),
    )

    result = await handler.async_step_migrate_envoy_confirm(
        {CONF_MIGRATION_CONFIRM_REASSIGN: True}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["description"] == "migration_success_reload_needed"


@pytest.mark.asyncio
async def test_options_flow_migrate_confirm_reports_current_reload_exception(
    hass, monkeypatch
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "12345"}
    )
    entry.add_to_hass(hass)
    envoy = MockConfigEntry(domain="enphase_envoy", entry_id="envoy-a", title="Envoy A")
    _patch_entry_lookup(monkeypatch, hass, envoy)
    object.__setattr__(envoy, "state", config_entries.ConfigEntryState.NOT_LOADED)

    attrs = {
        "device_class": "energy",
        "state_class": "total_increasing",
        "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
    }
    old_entity = _add_registry_sensor(
        hass,
        entry=envoy,
        platform="enphase_envoy",
        unique_id="envoy-a-prod",
        object_id="envoy_lifetime_production",
        state="5.0",
        attrs=attrs,
    )
    _add_registry_sensor(
        hass,
        entry=entry,
        platform=DOMAIN,
        unique_id=migration_target_unique_id("12345", "solar_production"),
        object_id="site_solar_production",
        state="5.1",
        attrs=attrs,
    )

    handler = OptionsFlowHandler(entry)
    handler.hass = hass
    handler._selected_migration_source_id = "envoy-a"
    handler._migration_selection = {"solar_production": old_entity}

    monkeypatch.setattr(
        hass.config_entries,
        "async_reload",
        AsyncMock(side_effect=RuntimeError("boom")),
    )

    result = await handler.async_step_migrate_envoy_confirm(
        {CONF_MIGRATION_CONFIRM_REASSIGN: True}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["description"] == "migration_success_reload_needed"


@pytest.mark.asyncio
async def test_options_flow_reload_migration_source_helper_handles_none_and_errors(
    hass, monkeypatch
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "12345"}
    )
    handler = OptionsFlowHandler(entry)
    handler.hass = hass
    source = config_flow.EnvoyHistorySource("envoy-a", "Envoy A", [])

    assert await handler._async_reload_migration_source_entry(source, None) is True

    monkeypatch.setattr(
        hass.config_entries,
        "async_reload",
        AsyncMock(side_effect=RuntimeError("boom")),
    )
    source_entry = MockConfigEntry(domain="enphase_envoy", entry_id="envoy-a")
    assert (
        await handler._async_reload_migration_source_entry(source, source_entry)
        is False
    )


@pytest.mark.asyncio
async def test_options_flow_migrate_confirm_reports_partial_failure_after_source_reload(
    hass, monkeypatch
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "12345"}
    )
    entry.add_to_hass(hass)
    envoy = MockConfigEntry(domain="enphase_envoy", entry_id="envoy-a", title="Envoy A")
    _patch_entry_lookup(monkeypatch, hass, envoy)
    object.__setattr__(envoy, "state", config_entries.ConfigEntryState.LOADED)
    attrs = {
        "device_class": "energy",
        "state_class": "total_increasing",
        "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
    }
    old_entity = _add_registry_sensor(
        hass,
        entry=envoy,
        platform="enphase_envoy",
        unique_id="envoy-a-prod",
        object_id="envoy_lifetime_production",
        state="5.0",
        attrs=attrs,
    )
    _add_registry_sensor(
        hass,
        entry=entry,
        platform=DOMAIN,
        unique_id=migration_target_unique_id("12345", "solar_production"),
        object_id="site_solar_production",
        state="5.1",
        attrs=attrs,
    )
    handler = OptionsFlowHandler(entry)
    handler.hass = hass
    handler._selected_migration_source_id = "envoy-a"
    handler._migration_selection = {"solar_production": old_entity}

    monkeypatch.setattr(
        hass.config_entries, "async_unload", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.config_flow.execute_takeover",
        lambda *_args, **_kwargs: EnvoyHistoryExecutionError(
            completed=[],
            failed=EnvoyHistoryMapping(
                flow_key="solar_production",
                label="Solar",
                old_entity_id=old_entity,
                archived_entity_id="sensor.archived",
                old_value_kwh=5.0,
                new_entity_id="sensor.site_solar_production",
                new_value_kwh=5.1,
                target_unique_id=migration_target_unique_id(
                    "12345", "solar_production"
                ),
            ),
            reason="boom",
        ),
    )
    reload_spy = AsyncMock(return_value=True)
    monkeypatch.setattr(hass.config_entries, "async_reload", reload_spy)

    result = await handler.async_step_migrate_envoy_confirm(
        {CONF_MIGRATION_CONFIRM_REASSIGN: True}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "migration_partial_failure"
    assert reload_spy.awaited


@pytest.mark.asyncio
async def test_options_flow_migrate_confirm_reports_envoy_reload_needed(
    hass, monkeypatch
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "12345"}
    )
    entry.add_to_hass(hass)
    envoy = MockConfigEntry(domain="enphase_envoy", entry_id="envoy-a", title="Envoy A")
    _patch_entry_lookup(monkeypatch, hass, envoy)
    object.__setattr__(envoy, "state", config_entries.ConfigEntryState.LOADED)

    attrs = {
        "device_class": "energy",
        "state_class": "total_increasing",
        "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
    }
    old_entity = _add_registry_sensor(
        hass,
        entry=envoy,
        platform="enphase_envoy",
        unique_id="envoy-a-prod",
        object_id="envoy_lifetime_production",
        state="5.0",
        attrs=attrs,
    )
    _add_registry_sensor(
        hass,
        entry=entry,
        platform=DOMAIN,
        unique_id=migration_target_unique_id("12345", "solar_production"),
        object_id="site_solar_production",
        state="5.1",
        attrs=attrs,
    )

    handler = OptionsFlowHandler(entry)
    handler.hass = hass
    handler._selected_migration_source_id = "envoy-a"
    handler._migration_selection = {"solar_production": old_entity}

    async def _unload(entry_id):
        assert entry_id == "envoy-a"
        object.__setattr__(envoy, "state", config_entries.ConfigEntryState.NOT_LOADED)
        return True

    monkeypatch.setattr(
        hass.config_entries, "async_unload", AsyncMock(side_effect=_unload)
    )
    monkeypatch.setattr(
        hass.config_entries,
        "async_reload",
        AsyncMock(side_effect=[True, False]),
    )

    result = await handler.async_step_migrate_envoy_confirm(
        {CONF_MIGRATION_CONFIRM_REASSIGN: True}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["description"] == "migration_success_reload_needed"
