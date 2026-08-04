"""Tests for the Grid Services VPP API surface."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from yarl import URL

from custom_components.enphase_ev import api
from custom_components.enphase_ev.api_client.vpp_surface import valid_object_id

ENROLLMENT_ID = "a" * 24
PROGRAM_ID = "b" * 24


class _Response:
    status = 200
    reason = "OK"
    headers: dict[str, str] = {}
    cookies: dict[str, object] = {}
    history: tuple[object, ...] = ()
    request_info = SimpleNamespace(real_url=URL(api.GS_BASE_URL))
    url = URL(api.GS_BASE_URL)

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        return False

    async def json(self) -> object:
        return {"data": None}

    async def text(self) -> str:
        return ""


def _client() -> api.EnphaseEVClient:
    client = api.EnphaseEVClient(
        object(),  # type: ignore[arg-type]
        "1234567",
        "EAUTH",
        "session=private; enlighten_manager_token_production=MANAGER",
    )
    client._json = AsyncMock(return_value={"data": []})  # type: ignore[method-assign]
    return client


def test_vpp_header_profile_isolates_grid_services_request() -> None:
    client = _client()

    headers = client._vpp_headers()  # noqa: SLF001

    assert headers == {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Origin": api.BASE_URL,
        "Referer": f"{api.BASE_URL}/",
        "Cookie": None,
        "X-CSRF-Token": None,
        "X-Requested-With": None,
        "e-auth-token": None,
        "Content-Type": None,
        "Authorization": "Bearer MANAGER",
    }


@pytest.mark.asyncio
async def test_vpp_api_uses_observed_paths_queries_and_callable_headers() -> None:
    client = _client()

    await client.vpp_enrollment_id()
    await client.vpp_enrollment_details(ENROLLMENT_ID)
    await client.vpp_events(PROGRAM_ID)

    calls = client._json.await_args_list  # type: ignore[attr-defined]
    assert calls[0].args == (
        "GET",
        f"{api.GS_BASE_URL}/enrollment-mgr/api/v1/enrollment/enrolled/1234567",
    )
    assert calls[0].kwargs["headers"] == client._vpp_headers  # noqa: SLF001
    assert calls[1].args[1].endswith(f"/enrollment/{ENROLLMENT_ID}")
    assert calls[1].kwargs["redaction_identifiers"] == (ENROLLMENT_ID,)
    assert calls[2].args[1] == (
        f"{api.GS_BASE_URL}/vpp-mgr/api/v1/events/get?"
        f"site_id=1234567&programId={PROGRAM_ID}&start_date=&end_date=&"
        "sort_by=&ascending=&time="
    )
    assert calls[2].kwargs["redaction_identifiers"] == (PROGRAM_ID,)
    assert all(call.kwargs["log_invalid_payload"] is False for call in calls)
    assert all(call.kwargs["use_cookie_header_only"] is True for call in calls)


@pytest.mark.asyncio
async def test_vpp_api_uses_stateless_session_even_when_shared_jar_has_cookies() -> (
    None
):
    shared = SimpleNamespace(
        cookie_jar=SimpleNamespace(
            filter_cookies=lambda _url: {"session": "shared-secret"}
        ),
        request=MagicMock(side_effect=AssertionError("shared session used")),
    )
    stateless = SimpleNamespace(
        cookie_jar=aiohttp.DummyCookieJar(),
        request=MagicMock(return_value=_Response()),
    )
    client = api.EnphaseEVClient(
        shared,  # type: ignore[arg-type]
        "1234567",
        "EAUTH",
        "session=private; enlighten_manager_token_production=MANAGER",
        cookie_header_session=stateless,  # type: ignore[arg-type]
    )

    assert await client.vpp_enrollment_id() == {"data": None}

    shared.request.assert_not_called()
    stateless.request.assert_called_once()
    request_headers = stateless.request.call_args.kwargs["headers"]
    assert "Cookie" not in request_headers
    assert request_headers["Authorization"] == "Bearer MANAGER"


@pytest.mark.asyncio
async def test_vpp_api_rejects_untrusted_object_ids_before_request() -> None:
    client = _client()

    with pytest.raises(ValueError, match="enrollment"):
        await client.vpp_enrollment_details("../private")
    with pytest.raises(ValueError, match="program"):
        await client.vpp_events("not-hex")

    client._json.assert_not_awaited()  # type: ignore[attr-defined]
    assert valid_object_id(PROGRAM_ID.upper()) == PROGRAM_ID.upper()
    assert valid_object_id(None) is None


def test_grid_services_reads_share_the_enlighten_limiter() -> None:
    assert api._should_limit_enlighten_read_request(  # noqa: SLF001
        "GET", f"{api.GS_BASE_URL}/vpp-mgr/api/v1/events/get"
    )
    assert not api._should_limit_enlighten_read_request(  # noqa: SLF001
        "POST", f"{api.GS_BASE_URL}/vpp-mgr/api/v1/events/get"
    )
