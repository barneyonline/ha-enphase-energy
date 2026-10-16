"""Offline request-time VPP diagnostics contain categories, never credentials."""

import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace

import aiohttp
import pytest
from homeassistant.util import dt as dt_util
from yarl import URL

from custom_components.enphase_ev import api, diagnostics
from custom_components.enphase_ev.api_client.vpp_auth import (
    credential_diagnostics,
    select_vpp_token,
)
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from .test_api_helpers import _make_token
from .test_vpp_token_policy import EXPIRY, NOW, token
from .test_vpp_token_selection_probe import _fake_client


@pytest.mark.parametrize(
    "manager,access,live,ignored,source,account,expiry,eligible,excluded",
    [
        (
            token(),
            token(),
            token(probe="live"),
            None,
            "live_manager",
            "match",
            "not_expired",
            True,
            False,
        ),
        (
            token(),
            token(),
            token("other"),
            None,
            "stored_manager",
            "mismatch",
            "not_expired",
            False,
            False,
        ),
        (
            token(),
            token(),
            "opaque-live",
            None,
            "stored_manager",
            "unknown",
            "unknown",
            False,
            False,
        ),
        (
            token(),
            token(),
            token(expiry=EXPIRY),
            None,
            "stored_manager",
            "match",
            "expired",
            False,
            False,
        ),
        (
            token(),
            token(),
            None,
            None,
            "stored_manager",
            "unknown",
            "absent",
            False,
            False,
        ),
        (
            token(),
            token(),
            token(probe="live"),
            token(probe="live"),
            "stored_manager",
            "match",
            "not_expired",
            False,
            True,
        ),
        (
            token(),
            token("other"),
            token(),
            None,
            "stored_manager",
            "conflicting_context",
            "not_evaluated",
            False,
            False,
        ),
        (
            "opaque",
            "opaque-access",
            token(),
            None,
            "stored_manager",
            "unknown",
            "not_evaluated",
            False,
            False,
        ),
        (None, None, token(), None, "none", "unknown", "not_evaluated", False, False),
        (
            token(expiry=EXPIRY),
            token(),
            None,
            None,
            "stored_access",
            "unknown",
            "absent",
            False,
            False,
        ),
        (
            token(expiry=EXPIRY),
            token(expiry=EXPIRY),
            None,
            None,
            "none",
            "unknown",
            "absent",
            False,
            False,
        ),
    ],
)
async def test_request_selection_categories(
    monkeypatch,
    manager,
    access,
    live,
    ignored,
    source,
    account,
    expiry,
    eligible,
    excluded,
):
    monkeypatch.setattr(dt_util, "utcnow", lambda: NOW)
    jar = aiohttp.CookieJar()
    if live:
        jar.update_cookies(
            {"enlighten_manager_token_production": live}, URL(api.BASE_URL)
        )
    client = api.EnphaseEVClient(
        SimpleNamespace(cookie_jar=jar),
        "1234567",
        access,
        f"enlighten_manager_token_production={manager}" if manager else "",
    )
    client._vpp_ignored_live_token = ignored
    headers = client._vpp_headers()
    snapshot = credential_diagnostics(client._vpp_credential_diagnostics)
    assert snapshot["source"] == source
    assert snapshot["account_match"] == account
    assert snapshot["retained_candidate_excluded"] is excluded
    assert snapshot["candidates"]["live_manager"] == {
        "expiry": expiry,
        "eligible": eligible,
    }
    assert (
        headers["Authorization"]
        == {
            "live_manager": live,
            "stored_manager": manager,
            "stored_access": access,
            "none": None,
        }[source]
    )
    # Optional diagnostic capture must not change the policy result.
    assert (
        select_vpp_token(manager, access, jar, ignored_live_token=ignored)
        == headers["Authorization"]
    )


@pytest.mark.parametrize(
    "expiry,expected",
    [
        (EXPIRY, "expired"),
        (EXPIRY - 1.0, "expired"),
        (EXPIRY + 0.5, "not_expired"),
        (EXPIRY + 1, "not_expired"),
        (None, "unknown"),
        (True, "unknown"),
        ("secret-expiry", "unknown"),
        (float("nan"), "unknown"),
        (float("inf"), "unknown"),
    ],
)
def test_stored_expiry_categories_preserve_unknown_expiry_compatibility(
    monkeypatch, expiry, expected
):
    monkeypatch.setattr(dt_util, "utcnow", lambda: NOW)
    snapshot = {"unexpected": "must-be-cleared"}
    manager = token(expiry=expiry)
    selected = select_vpp_token(manager, None, None, diagnostics=snapshot)
    safe = credential_diagnostics(snapshot)
    assert "unexpected" not in snapshot
    assert safe["candidates"]["stored_manager"] == {
        "expiry": expected,
        "eligible": expected != "expired",
    }
    assert safe["candidates"]["stored_access"] == {
        "expiry": "absent",
        "eligible": False,
    }
    assert selected == (None if expected == "expired" else manager)


@pytest.mark.parametrize(
    "snapshot",
    [
        None,
        "secret",
        [],
        {},
        {
            "source": "secret-source",
            "account_match": ["secret-account"],
            "retained_candidate_excluded": "secret-truthy",
            "candidates": {
                "stored_manager": {
                    "expiry": {"secret": True},
                    "eligible": 1,
                    "token": "secret-token",
                },
                "stored_access": "secret-access",
                "live_manager": {"expiry": "secret-expiry"},
                "secret-category": "secret-value",
            },
            "authorization": "secret-authorization",
        },
        {"candidates": "secret-candidates"},
    ],
)
def test_export_rejects_unbounded_values(snapshot):
    safe = credential_diagnostics(snapshot)
    assert safe == credential_diagnostics(None)
    assert "secret" not in json.dumps(safe)
    assert safe["source"] == "not_attempted"


@pytest.mark.parametrize(
    "method,args",
    [
        ("vpp_enrollment_id", ()),
        ("vpp_enrollment_details", ("a" * 24,)),
        ("vpp_events", ("b" * 24,)),
    ],
)
async def test_failed_request_snapshot_survives_updates_and_redacted_export(
    hass, config_entry, coordinator_factory, monkeypatch, method, args, caplog
):
    monkeypatch.setattr(dt_util, "utcnow", lambda: NOW)
    client, shared, stateless, reauth, old, new, access = _fake_client(
        "expired", NOW, reject_all=True
    )
    coord = coordinator_factory(client=client)
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    before = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)
    assert (
        before["coordinator"]["vpp_credential_selection"]["source"] == "not_attempted"
    )
    stateless.request.assert_not_called()
    with pytest.raises(api.Unauthorized):
        await getattr(client, method)(*args)
    expected = credential_diagnostics(client._vpp_credential_diagnostics)
    assert expected["source"] == "stored_access"
    assert expected["candidates"]["stored_manager"]["expiry"] == "expired"
    # A download reports the credential actually selected, not current credentials.
    client.update_credentials(
        eauth="synthetic-replacement", cookie="session=synthetic-new-session"
    )
    monkeypatch.setattr(dt_util, "utcnow", lambda: NOW + timedelta(hours=3))
    monkeypatch.setattr(
        shared.cookie_jar,
        "filter_cookies",
        lambda _url: pytest.fail("diagnostics must not read the cookie jar"),
    )
    exported = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)
    safe = exported["coordinator"]["vpp_credential_selection"]
    assert safe == expected
    safe["candidates"]["stored_manager"]["expiry"] = "tampered"
    assert credential_diagnostics(client._vpp_credential_diagnostics) == expected
    serialized = (
        json.dumps(exported)
        + caplog.text
        + json.dumps(client._vpp_credential_diagnostics)
    )
    for secret in (
        old,
        new,
        access,
        "synthetic-account-a",
        "synthetic-session",
        "synthetic-private",
        "synthetic-replacement",
        "synthetic-new-session",
    ):
        assert secret not in serialized
        assert hashlib.sha256(secret.encode()).hexdigest() not in serialized
    assert stateless.request.call_count == 1
    shared.request.assert_not_called()
    reauth.assert_not_awaited()


async def test_actual_retained_candidate_exclusion_and_next_request_rotation(
    monkeypatch,
):
    monkeypatch.setattr(dt_util, "utcnow", lambda: NOW)
    client, shared, stateless, reauth, old, new, access = _fake_client("jar", NOW)
    client.update_credentials(
        eauth=access, cookie=f"enlighten_manager_token_production={old}"
    )
    with pytest.raises(api.Unauthorized):
        await client.vpp_enrollment_id()
    previous = credential_diagnostics(client._vpp_credential_diagnostics)
    assert previous["source"] == "stored_manager"
    assert previous["retained_candidate_excluded"] is True
    assert previous["account_match"] == "match"
    shared.cookie_jar.update_cookies(
        {"enlighten_manager_token_production": access}, URL(api.BASE_URL)
    )
    with pytest.raises(api.Unauthorized):
        await client.vpp_enrollment_id()
    current = credential_diagnostics(client._vpp_credential_diagnostics)
    assert current["source"] == "live_manager"
    assert current["retained_candidate_excluded"] is False
    assert current["candidates"]["live_manager"]["eligible"] is True
    assert previous["retained_candidate_excluded"] is True
    assert stateless.request.call_count == 2
    reauth.assert_not_awaited()


async def test_unreadable_jar_reports_absent_without_exception_text(monkeypatch):
    monkeypatch.setattr(dt_util, "utcnow", lambda: NOW)

    def broken_jar(_url):
        raise RuntimeError("synthetic-secret-jar-failure")

    state = {}
    manager = token()
    assert (
        select_vpp_token(
            manager, None, SimpleNamespace(filter_cookies=broken_jar), diagnostics=state
        )
        == manager
    )
    assert state["candidates"]["live_manager"] == {
        "expiry": "absent",
        "eligible": False,
    }
    assert state["account_match"] == "unknown"
    assert "synthetic-secret" not in json.dumps(state)


def test_malformed_jwt_claims_remain_categories(monkeypatch):
    monkeypatch.setattr(dt_util, "utcnow", lambda: NOW)
    state = {}
    manager = _make_token(["secret-claim"])
    assert (
        select_vpp_token(manager, "opaque-access", None, diagnostics=state) == manager
    )
    assert credential_diagnostics(state)["candidates"]["stored_manager"] == {
        "expiry": "unknown",
        "eligible": True,
    }
    assert "secret" not in json.dumps(state)
