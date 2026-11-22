"""Exercise charger-status failures through the request and refresh paths."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import update_coordinator
from homeassistant.helpers.update_coordinator import CoordinatorEntity, UpdateFailed

from custom_components.enphase_ev import coordinator as coordinator_module
from custom_components.enphase_ev.api_client import request_surface
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL


@pytest.fixture
def unreachable_type(monkeypatch):
    """Use Core's exception where installed and simulate the capability otherwise."""

    if exception_type := getattr(update_coordinator, "UpdateFailedUnreachable", None):
        return exception_type

    class UpdateFailedUnreachable(UpdateFailed):
        pass

    monkeypatch.setattr(
        update_coordinator,
        "UpdateFailedUnreachable",
        UpdateFailedUnreachable,
        raising=False,
    )
    return UpdateFailedUnreachable


@pytest.fixture
def install_status_response(coordinator_factory, monkeypatch):
    """Run the real Enphase client with a controlled HTTP request context."""

    coord = coordinator_factory()
    coord.client.set_reauth_callback(None)
    monkeypatch.setattr(coordinator_module.random, "uniform", lambda *_args: 1.0)

    def install(*, before_headers=None, body=None, status=200, headers=None):
        @asynccontextmanager
        async def request(_method, url, **_kwargs):
            if before_headers is not None:
                await before_headers()
            yield SimpleNamespace(
                status=status,
                headers=headers or {},
                reason="response received",
                history=(),
                request_info=SimpleNamespace(real_url=url),
                json=body,
                text=AsyncMock(return_value="response received"),
            )

        @asynccontextmanager
        async def session(**_kwargs):
            yield SimpleNamespace(request=request)

        monkeypatch.setattr(coord.client, "_request_session", session)

    return coord, install


@pytest.mark.parametrize("has_new_type", [True, False], ids=["new-core", "older-core"])
@pytest.mark.parametrize("failure", ["connection", "timeout"])
async def test_no_status_response_uses_available_failure_type(
    install_status_response, monkeypatch, request, has_new_type, failure
):
    coord, install = install_status_response
    if has_new_type:
        expected = request.getfixturevalue("unreachable_type")
    else:
        monkeypatch.delattr(
            update_coordinator, "UpdateFailedUnreachable", raising=False
        )
        expected = UpdateFailed

    async def wait_for_headers():
        if failure == "connection":
            raise aiohttp.ClientConnectionError("Connection failed")
        await asyncio.sleep(0)
        pytest.fail("Request deadline did not fire")

    if failure == "timeout":
        coord.client._timeout = 0
    install(before_headers=wait_for_headers)

    with pytest.raises(UpdateFailed, match="Error communicating with API") as caught:
        await coord._async_update_data()

    assert type(caught.value) is expected
    assert caught.value.retry_after == coord._slow_interval_floor()
    assert coord.last_failure_source == "network"
    assert coord.last_failure_status is None
    assert coord.last_failure_response is None


@pytest.mark.parametrize(
    "failure",
    [
        "queue",
        "body-timeout",
        "body-disconnect",
        "body-payload",
        "tls",
        "generic-client",
    ],
)
async def test_other_client_failures_keep_regular_update_failed(
    install_status_response, monkeypatch, unreachable_type, failure
):
    coord, install = install_status_response

    async def fail_in_body():
        if failure == "body-disconnect":
            raise aiohttp.ServerDisconnectedError()
        if failure == "body-payload":
            raise aiohttp.ClientPayloadError("Invalid response body")
        await asyncio.sleep(0)
        pytest.fail("Body deadline did not fire")

    async def fail_before_headers():
        if failure == "tls":
            raise aiohttp.ServerFingerprintMismatch(
                b"expected", b"actual", "example.invalid", 443
            )
        raise aiohttp.ClientError("Unexpected client failure")

    @asynccontextmanager
    async def wait_in_queue(*_args):
        await asyncio.sleep(0)
        pytest.fail("Queue deadline did not fire")
        yield

    if failure in {"queue", "body-timeout"}:
        coord.client._timeout = 0
    if failure == "queue":
        monkeypatch.setattr(
            request_surface, "_timed_enlighten_read_request_guard", wait_in_queue
        )
    if failure in {"tls", "generic-client"}:
        install(before_headers=fail_before_headers)
    else:
        install(body=fail_in_body)

    with pytest.raises(UpdateFailed, match="Error communicating with API") as caught:
        await coord._async_update_data()

    assert type(caught.value) is UpdateFailed
    assert caught.value.retry_after == coord._slow_interval_floor()


@pytest.mark.parametrize("status", [401, 403, 429, 503])
async def test_status_http_responses_keep_existing_failure_paths(
    install_status_response, monkeypatch, unreachable_type, status
):
    coord, install = install_status_response
    install(status=status, headers={"Retry-After": "600"})
    if status == 401:
        with pytest.raises(ConfigEntryAuthFailed):
            await coord._async_update_data()
    elif status == 503:
        degrade = AsyncMock(return_value={})
        monkeypatch.setattr(coord, "_async_run_site_only_refresh_pipeline", degrade)
        assert await coord._async_update_data() == {}
        assert degrade.await_args.kwargs == {"status_degraded": True}
        assert coord.last_failure_status == status
    else:
        with pytest.raises(UpdateFailed, match=f"Cloud error {status}") as caught:
            await coord._async_update_data()
        assert type(caught.value) is UpdateFailed
        assert caught.value.retry_after == 600
        assert coord.last_failure_source == "http"
        assert coord.last_failure_status == status


@pytest.mark.parametrize("unexpected", [False, True], ids=["malformed", "unexpected"])
async def test_status_payload_and_programming_errors_keep_existing_failure_paths(
    install_status_response, unreachable_type, unexpected
):
    coord, install = install_status_response
    if unexpected:
        error = RuntimeError("Unexpected parsing failure")
        install(body=AsyncMock(side_effect=error))
        with pytest.raises(RuntimeError) as caught:
            await coord._async_update_data()
        assert caught.value is error
    else:
        install(body=AsyncMock(return_value=["not a status object"]))
        with pytest.raises(UpdateFailed, match="Invalid API payload") as caught:
            await coord._async_update_data()
        assert type(caught.value) is UpdateFailed
        assert caught.value.retry_after == coord._slow_interval_floor()
        assert coord.last_failure_source == "payload"


async def test_status_unreachability_keeps_availability_backoff_and_recovery(
    install_status_response, unreachable_type, caplog, monkeypatch
):
    coord, install = install_status_response
    entity = CoordinatorEntity(coord)
    assert entity.available
    caplog.set_level(logging.INFO, logger=coord.logger.name)
    schedule = AsyncMock()
    monkeypatch.setattr(coord, "_async_run_post_status_refresh_pipeline", schedule)

    request = AsyncMock(side_effect=aiohttp.ClientConnectionError("Connection failed"))
    install(before_headers=request)
    await coord.async_refresh()
    assert not entity.available
    assert type(coord.last_exception) is unreachable_type
    assert coord.last_exception.retry_after == coord._slow_interval_floor()
    assert coord._backoff_until
    if unreachable_type.__module__ == update_coordinator.__name__:
        assert any(
            record.levelno == logging.INFO and "is unavailable" in record.message
            for record in caplog.records
        )

    await coord.async_refresh()
    request.assert_awaited_once()
    assert not entity.available
    assert type(coord.last_exception) is UpdateFailed

    coord._backoff_until = 0
    install(body=AsyncMock(return_value={"evChargerData": [{"sn": RANDOM_SERIAL}]}))
    await coord.async_refresh()
    assert entity.available
    assert coord._backoff_until is None
    assert coord._network_errors == 0
    assert any(
        record.levelno == logging.INFO and "recovered" in record.message
        for record in caplog.records
    )
