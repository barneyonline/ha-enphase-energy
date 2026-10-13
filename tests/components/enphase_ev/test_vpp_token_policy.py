"""VPP token policy keeps shared-jar and global credential boundaries intact."""

from datetime import UTC, datetime
from types import SimpleNamespace

import aiohttp
import pytest
from homeassistant.util import dt as dt_util
from yarl import URL

from custom_components.enphase_ev import api
from .test_api_helpers import _make_token

NOW = datetime(2026, 10, 14, 0, tzinfo=UTC)
EXPIRY = int(NOW.timestamp())


def token(account="account-a", expiry=EXPIRY + 3600, **extra):
    return _make_token({"user_id": account, "exp": expiry, **extra})


@pytest.mark.parametrize(
    "manager,access,live,expected",
    [
        (token(), token(), token(probe="live"), "live"),
        (token(), token(), token("account-b"), "manager"),
        (token(), token("account-b"), token("account-b", probe="live"), "manager"),
        (None, token(), token(probe="live"), "live"),
        (token(), None, token(probe="live"), "live"),
        ("OPAQUE-MANAGER", "OPAQUE-ACCESS", token(), "manager"),
        ("OPAQUE-MANAGER", token(), "OPAQUE-LIVE", "manager"),
        (token(), token(), token(account=""), "manager"),
        (token(account=" "), token(account=" "), token(account=" "), "manager"),
        (token(), token(), token(expiry=EXPIRY), "manager"),
        (token(), token(), token(expiry=EXPIRY - 1), "manager"),
        (token(), token(), None, "manager"),
        (token(), token(), "", "manager"),
        (token(expiry=EXPIRY), token(), None, "access"),
        (token(expiry=EXPIRY - 1), token(), None, "access"),
        (token(expiry=float(EXPIRY) - 0.5), token(), None, "access"),
        (token(expiry=float(EXPIRY) + 0.5), token(), None, "manager"),
        (token(expiry=EXPIRY - 1), token(expiry=EXPIRY - 1), None, None),
        (
            token(expiry=EXPIRY - 1),
            token(expiry=EXPIRY - 1),
            token(probe="live"),
            "live",
        ),
        (None, None, token(), None),
        (None, "OPAQUE-ACCESS", None, "access"),
        ("OPAQUE-MANAGER", "OPAQUE-ACCESS", None, "manager"),
        (token(expiry=None), token(), None, "manager"),
        (token(expiry=True), token(), None, "manager"),
        (token(expiry="expired"), token(), None, "manager"),
        (token(expiry=float("nan")), token(), None, "manager"),
        (token(expiry=float("inf")), token(), None, "manager"),
        (_make_token(["malformed-payload"]), token(), None, "manager"),
        (token(), token(), _make_token(["malformed-payload"]), "manager"),
        (
            token(account=True),
            "OPAQUE-ACCESS",
            token(account=True, probe="live"),
            "manager",
        ),
        (
            token(account={"id": "malformed"}),
            "OPAQUE-ACCESS",
            token(account={"id": "malformed"}, probe="live"),
            "manager",
        ),
        (
            _make_token({"exp": EXPIRY + 3600}),
            "OPAQUE-ACCESS",
            token(probe="live"),
            "manager",
        ),
        (
            _make_token({"data": {"userId": 123}, "exp": EXPIRY + 3600}),
            token(account="123"),
            _make_token({"userid": "123", "exp": EXPIRY + 3600}),
            "live",
        ),
    ],
)
async def test_vpp_headers_select_only_eligible_account_bound_live_tokens(
    monkeypatch, manager, access, live, expected
):
    monkeypatch.setattr(dt_util, "utcnow", lambda: NOW)
    jar = aiohttp.CookieJar()
    if live is not None:
        jar.update_cookies(
            {"enlighten_manager_token_production": live}, URL(api.BASE_URL)
        )
    client = api.EnphaseEVClient(
        SimpleNamespace(cookie_jar=jar),
        "1234567",
        access,
        f"enlighten_manager_token_production={manager}" if manager else "",
    )
    assert (
        client._vpp_headers()["Authorization"]
        == {"manager": manager, "access": access, "live": live, None: None}[expected]
    )
    assert client.scheduler_bearer() == manager
    assert client._control_headers() == (
        {"Authorization": f"Bearer {manager or access}"} if manager or access else {}
    )


@pytest.mark.parametrize("jar", [None, "broken", "invalid-value", "other-origin"])
async def test_vpp_jar_failure_or_scope_mismatch_preserves_stored_token(
    monkeypatch, jar
):
    monkeypatch.setattr(dt_util, "utcnow", lambda: NOW)
    manager = token()
    if jar == "broken":

        class BrokenJar:
            def filter_cookies(self, _url):
                raise RuntimeError("synthetic failure")

        jar = BrokenJar()
    elif jar == "invalid-value":
        jar = SimpleNamespace(
            filter_cookies=lambda _url: {
                "enlighten_manager_token_production": SimpleNamespace(value=123)
            }
        )
    elif jar == "other-origin":
        jar = aiohttp.CookieJar()
        jar.update_cookies(
            {"enlighten_manager_token_production": token(probe="other")},
            URL("https://unrelated.invalid"),
        )
    client = api.EnphaseEVClient(
        SimpleNamespace(cookie_jar=jar),
        "1234567",
        token(),
        f"enlighten_manager_token_production={manager}",
    )
    assert client._vpp_headers()["Authorization"] == manager
