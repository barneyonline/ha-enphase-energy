"""Tests for cloud Activation grid profile runtime and API helpers."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.enphase_ev import api
from custom_components.enphase_ev.api import (
    ActivationAccessDenied,
    EnphaseLoginWallUnauthorized,
    InvalidPayloadError,
    OptionalEndpointUnavailable,
    Unauthorized,
)
from custom_components.enphase_ev.coordinator import EnphaseCoordinator
from custom_components.enphase_ev.grid_profile_runtime import (
    ALL_PROFILES_OPTION,
    COMMONLY_USED_OPTION,
    SUPPORT_CONFIRMED,
    SUPPORT_DENIED,
    SUPPORT_UNKNOWN,
    SUPPORT_UNAVAILABLE,
    ActivationRegion,
    GridProfile,
    GridProfileRuntime,
    _clean_text,
    _coerce_bool,
    _group_matches_region,
)
from custom_components.enphase_ev.sensor import _retain_grid_profile_sensors


class _DefaultSession:
    cookie_jar = SimpleNamespace(filter_cookies=lambda _url: {})


def _make_api_client() -> api.EnphaseEVClient:
    return api.EnphaseEVClient(_DefaultSession(), "3381244", "TOKEN", "COOKIE")


async def test_activation_auth_bootstraps_embedded_settings_token() -> None:
    client = _make_api_client()
    client.update_credentials(
        cookie="session=current; enlighten_manager_token_production=stale"
    )
    token = "header.payload.signature"
    client._text = AsyncMock(  # noqa: SLF001
        return_value=(
            '<iframe src="/app/activation_ui/?locale=en-AU&amp;token='
            f'{token}&amp;siteid=3381244&amp;gridprofile=gridprofile"></iframe>'
        )
    )

    assert await client.async_prepare_activation_auth() is True
    assert await client.async_prepare_activation_auth() is True

    headers = client._activation_headers()  # noqa: SLF001
    assert headers["Authorization"] == f"Bearer {token}"
    assert headers["Referer"] == (
        "https://enlighten.enphaseenergy.com/app/activation_ui/"
        f"?locale=en-AU&token={token}&siteid=3381244&gridprofile=gridprofile"
    )
    assert headers["Cookie"] == (
        f"session=current; enlighten_manager_token_production={token}"
    )
    client._text.assert_awaited_once()  # type: ignore[attr-defined]


def test_activation_auth_parser_handles_protocol_relative_and_malformed_urls() -> None:
    token = "header.payload.signature"
    assert api._activation_context_from_settings_html(  # noqa: SLF001
        "//enlighten.enphaseenergy.com/app/activation_ui/"
        f"?locale=en-AU&token={token}&siteid=3381244"
    ) == (
        token,
        "https://enlighten.enphaseenergy.com/app/activation_ui/"
        f"?locale=en-AU&token={token}&siteid=3381244",
    )
    assert (
        api._activation_context_from_settings_html(  # noqa: SLF001
            f"https://example.com/app/activation_ui/?token={token}"
        )
        is None
    )
    with patch.object(api, "URL", side_effect=ValueError):
        assert (
            api._activation_context_from_settings_html(  # noqa: SLF001
                f"/app/activation_ui/?token={token}"
            )
            is None
        )


def test_activation_auth_expired_cached_token_uses_stored_fallback() -> None:
    client = _make_api_client()
    client._activation_token = "header.eyJleHAiOjF9.signature"  # noqa: SLF001

    assert client._activation_auth_token() == "TOKEN"  # noqa: SLF001
    assert client._activation_token is None  # noqa: SLF001
    assert client._activation_referer is None  # noqa: SLF001


@pytest.mark.parametrize(
    "payload",
    [
        "<html>No Activation UI</html>",
        '<iframe src="/app/activation_ui/?token=not-a-jwt"></iframe>',
    ],
)
async def test_activation_auth_bootstrap_preserves_fallback_without_valid_token(
    payload: str,
) -> None:
    client = _make_api_client()
    client.update_credentials(eauth="access-token", cookie="session=current")
    client._text = AsyncMock(return_value=payload)  # noqa: SLF001

    assert await client.async_prepare_activation_auth() is False
    headers = client._activation_headers()  # noqa: SLF001
    assert headers["Authorization"] == "Bearer access-token"
    assert headers["Cookie"] == "session=current"


@pytest.mark.parametrize(
    "rejection",
    [
        Unauthorized("expired"),
        aiohttp.ClientResponseError(None, (), status=403),
    ],
)
async def test_activation_request_rebootstraps_rejected_cached_token(
    rejection: Exception,
) -> None:
    client = _make_api_client()
    client.update_credentials(eauth="fallback", cookie="session=current")
    client._activation_token = "old.header.signature"  # noqa: SLF001
    client._activation_referer = (
        "https://enlighten.enphaseenergy.com/old"  # noqa: SLF001
    )
    new_token = "new.header.signature"
    client._text = AsyncMock(  # noqa: SLF001
        return_value=(
            '<iframe src="/app/activation_ui/?token='
            f'{new_token}&amp;siteid=3381244"></iframe>'
        )
    )
    client._json = AsyncMock(  # noqa: SLF001
        side_effect=[rejection, {"activation": "available"}]
    )

    assert await client.async_get_activation_record() == {"activation": "available"}

    assert client._json.await_count == 2  # type: ignore[attr-defined]
    first_headers = client._json.await_args_list[0].kwargs[  # type: ignore[attr-defined]
        "headers"
    ]
    second_headers = client._json.await_args_list[1].kwargs[  # type: ignore[attr-defined]
        "headers"
    ]
    assert first_headers["Authorization"] == "Bearer old.header.signature"
    assert second_headers["Authorization"] == f"Bearer {new_token}"
    assert second_headers["Cookie"] == (
        f"session=current; enlighten_manager_token_production={new_token}"
    )
    client._text.assert_awaited_once()  # type: ignore[attr-defined]


async def test_activation_auth_bootstrap_soft_fails_when_settings_unavailable() -> None:
    client = _make_api_client()
    client._text = AsyncMock(side_effect=Unauthorized())  # noqa: SLF001

    assert await client.async_prepare_activation_auth() is False
    assert (
        client._activation_headers()["Authorization"] == "Bearer TOKEN"
    )  # noqa: SLF001


async def test_activation_api_helpers_use_cloud_payloads() -> None:
    client = _make_api_client()
    calls: list[tuple[str, str, dict[str, Any]]] = []

    async def fake_json(method: str, url: str, **kwargs: Any) -> object:
        calls.append((method, url, kwargs))
        if method == "GET" and url.endswith("/systems/3381244/devices/list"):
            return [
                {
                    "envoyCombiner": {"IQ Gateway": ["122532006376"]},
                    "envoyGridProfile": {
                        "selected_profile_id": "agf:6643fae616246153786f318b",
                        "requested_profile_id": None,
                    },
                    "ensembleEnvoy": True,
                }
            ]
        if method == "PUT":
            return [
                {
                    "serial_num": "122532006376",
                    "part_num": "800-00649-r01",
                    "grid_profile": {
                        "selected_profile_id": "agf:6643fae616246153786f318b",
                        "requested_profile_id": "agf:6643fae616246153786f318b",
                    },
                }
            ]
        return {"ok": True}

    client._json = fake_json  # type: ignore[method-assign]

    await client.async_get_activation_reference_data()
    await client.async_get_activation_record()
    devices_response = await client.async_get_activation_device_list()
    await client.async_get_grid_profiles_filtered(
        country="AU",
        state="VIC",
        commonly_used=False,
    )
    apply_response = await client.async_apply_grid_profile(
        gateway_serial="122532006376",
        part_num="800-00555-r01",
        ensemble_envoy=True,
        profile_id="agf:6643fae616246153786f318b",
    )
    assert apply_response == {
        "envoys": [
            {
                "serial_num": "122532006376",
                "part_num": "800-00649-r01",
                "grid_profile": {
                    "selected_profile_id": "agf:6643fae616246153786f318b",
                    "requested_profile_id": "agf:6643fae616246153786f318b",
                },
            }
        ]
    }
    assert devices_response == {
        "devices": [
            {
                "envoyCombiner": {"IQ Gateway": ["122532006376"]},
                "envoyGridProfile": {
                    "selected_profile_id": "agf:6643fae616246153786f318b",
                    "requested_profile_id": None,
                },
                "ensembleEnvoy": True,
            }
        ]
    }

    reference = calls[0]
    assert reference[0] == "GET"
    assert reference[1].endswith(
        "/service/activation_service/api/details/reference_data"
    )
    assert reference[2]["headers"]["enlm-token"] == "TOKEN"

    activation_record = calls[1]
    assert activation_record[0] == "GET"
    assert activation_record[1].endswith(
        "/service/activation_backend/api/gateway/v4/activations/3381244"
    )
    assert activation_record[2]["params"] == {"expand": "owner,host"}
    assert activation_record[2]["headers"]["Authorization"] == "Bearer TOKEN"

    envoys = calls[2]
    assert envoys[0] == "GET"
    assert envoys[1].endswith(
        "/service/activation_backend/api/gateway/v4/systems/3381244/devices/list"
    )
    assert envoys[2]["headers"]["Authorization"] == "Bearer TOKEN"

    profiles = calls[3]
    assert profiles[0] == "POST"
    assert profiles[1].endswith(
        "/service/activation_backend/api/gateway/v4/systems/"
        "3381244/grid_profiles_filtered"
    )
    assert profiles[2]["json"] == {
        "commonly_used": False,
        "country": "AU",
        "state": "VIC",
    }
    assert profiles[2]["headers"]["Authorization"] == "Bearer TOKEN"

    apply = calls[4]
    assert apply[0] == "PUT"
    assert apply[1].endswith(
        "/service/activation_backend/api/gateway/v4/systems/3381244/envoys"
    )
    assert apply[2]["json"] == [
        {
            "grid_profile_id": "agf:6643fae616246153786f318b",
            "serial_num": "122532006376",
            "part_num": "800-00555-r01",
            "ensemble_envoy": True,
        }
    ]
    assert apply[2]["allow_empty_success"] is True
    await client.async_apply_grid_profile(
        gateway_serial="122532006376",
        part_num=None,
        ensemble_envoy=True,
        profile_id="agf:6643fae616246153786f318b",
    )
    assert calls[5][2]["json"] == [
        {
            "grid_profile_id": "agf:6643fae616246153786f318b",
            "serial_num": "122532006376",
            "ensemble_envoy": True,
        }
    ]


@pytest.mark.parametrize(
    ("error", "expected_exception"),
    [
        (Unauthorized("denied"), ActivationAccessDenied),
        (
            InvalidPayloadError(
                "invalid Activation payload",
                endpoint="/activation",
                status=200,
                content_type="application/json",
                failure_kind="json_decode",
            ),
            OptionalEndpointUnavailable,
        ),
    ],
)
async def test_activation_api_distinguishes_access_denial_from_payload_failure(
    error: Exception,
    expected_exception: type[Exception],
) -> None:
    client = _make_api_client()
    client._json = AsyncMock(side_effect=error)

    with pytest.raises(expected_exception) as raised:
        await client.async_get_activation_record()

    if expected_exception is OptionalEndpointUnavailable:
        assert not isinstance(raised.value, ActivationAccessDenied)


@pytest.mark.parametrize(
    ("error", "expected_exception"),
    [
        (
            EnphaseLoginWallUnauthorized(
                endpoint="/activation",
                request_label="GET /activation",
            ),
            ActivationAccessDenied,
        ),
        (
            aiohttp.ClientResponseError(None, (), status=403),
            ActivationAccessDenied,
        ),
        (
            aiohttp.ClientResponseError(None, (), status=500),
            aiohttp.ClientResponseError,
        ),
    ],
)
async def test_activation_api_classifies_http_access_failures(
    error: Exception,
    expected_exception: type[Exception],
) -> None:
    client = _make_api_client()
    client._json = AsyncMock(side_effect=error)

    with pytest.raises(expected_exception):
        await client.async_get_activation_record()


async def test_activation_api_validates_response_shapes() -> None:
    client = _make_api_client()
    client._json = AsyncMock(return_value=[])

    with pytest.raises(OptionalEndpointUnavailable):
        await client.async_get_activation_record()

    client._json = AsyncMock(return_value={"devices": []})
    assert await client.async_get_activation_device_list() == {"devices": []}
    client._json = AsyncMock(return_value={"envoys": []})
    assert await client.async_apply_grid_profile(
        gateway_serial="122532006376",
        part_num=None,
        ensemble_envoy=True,
        profile_id="agf:test",
    ) == {"envoys": []}

    client._json = AsyncMock(return_value="invalid")
    with pytest.raises(OptionalEndpointUnavailable):
        await client.async_get_activation_device_list()
    with pytest.raises(OptionalEndpointUnavailable):
        await client.async_apply_grid_profile(
            gateway_serial="122532006376",
            part_num=None,
            ensemble_envoy=True,
            profile_id="agf:test",
        )


class _FakeGridProfileClient:
    def __init__(self) -> None:
        self.activation_auth_prepare_requests = 0
        self.profile_requests: list[tuple[str, str, bool]] = []
        self.apply_requests: list[dict[str, object]] = []
        self.reference_payload: dict[str, object] = {
            "country_regions": {
                "AU": [
                    {
                        "id": 14,
                        "countryCode": "AU",
                        "regionCode": "VIC",
                        "regionName": "Victoria",
                    }
                ],
                "NZ": [
                    {
                        "id": 1,
                        "countryCode": "NZ",
                        "regionCode": "WGN",
                        "regionName": "Wellington",
                    }
                ],
            }
        }
        self.activation_record: dict[str, object] = {
            "system": {"countryCode": "AU"},
            "address": {"state": "VIC", "country": "AU"},
            "envoys": [
                {
                    "serial_num": "122532006376",
                    "part_num": "800-00555-r01",
                    "ensemble_envoy": True,
                    "grid_profile_id": "agf:common",
                    "grid_profile_name": (
                        "AS/NZS 4777.2: 2020 Australia A Region (1.3.12)"
                    ),
                }
            ],
        }
        self.profile_payload: dict[str, object] = {
            "grid_profiles": {
                "ACT, AU": [
                    {
                        "name": "Australian Capital Territory Profile",
                        "profile_id": "agf:act",
                        "pel_enabled": False,
                    }
                ],
                "VIC, AU": [
                    {
                        "name": "AS/NZS 4777.2: 2020 Australia A Region (1.3.12)",
                        "profile_id": "agf:common",
                        "pel_enabled": False,
                    },
                    {
                        "name": (
                            "AS/NZS 4777.2: 2020 Australia A Region "
                            "0 kW Export (1.3.9)"
                        ),
                        "profile_id": "agf:export",
                        "pel_enabled": True,
                    },
                ],
            },
            "recommended_profile": {"profile_id": "agf:common"},
        }
        self.common_profile_payload: dict[str, object] = {
            "title": {
                "country": "AU",
                "state": "VIC",
                "commonly_used": True,
            },
            "grid_profiles": {
                "VIC, AU": [
                    {
                        "name": (
                            "AS/NZS 4777.2: 2020 Australia A Region "
                            "0 kW Export (1.3.9)"
                        ),
                        "profile_id": "agf:export",
                        "pel_enabled": True,
                    }
                ]
            },
            "recommended_profile": {
                "name": "AS/NZS 4777.2: 2020 Australia A Region (1.3.12)",
                "profile_id": "agf:common",
                "pel_enabled": False,
                "is_277v_compatible": False,
            },
        }
        self.activation_devices_payload: dict[str, object] | None = None
        self.dashboard_summary_payload: dict[str, object] | None = None
        self.dashboard_summary_requests = 0
        self.reference_requests = 0
        self.activation_record_requests = 0
        self.activation_device_requests = 0

    async def async_prepare_activation_auth(self) -> bool:
        self.activation_auth_prepare_requests += 1
        return True

    async def async_get_activation_reference_data(self) -> dict[str, object]:
        self.reference_requests += 1
        return self.reference_payload

    async def async_get_activation_record(self) -> dict[str, object]:
        self.activation_record_requests += 1
        return self.activation_record

    async def async_get_activation_device_list(self) -> dict[str, object]:
        self.activation_device_requests += 1
        if self.activation_devices_payload is not None:
            return self.activation_devices_payload
        envoys = self.activation_record.get("envoys")
        if not isinstance(envoys, list):
            return {"devices": []}
        records: list[dict[str, object]] = []
        for envoy in envoys:
            if not isinstance(envoy, dict):
                continue
            record = dict(envoy)
            serial = record.pop("serial_num", None)
            ensemble = record.pop("ensemble_envoy", None)
            profile_id = record.pop("grid_profile_id", None)
            profile_name = record.pop("grid_profile_name", None)
            device_record: dict[str, object] = {
                "envoyCombiner": {"IQ Gateway": [serial]} if serial else {},
                "ensembleEnvoy": ensemble,
            }
            if profile_id or profile_name:
                device_record["envoyGridProfile"] = {
                    "selected_profile_id": profile_id,
                    "selected_grid_profile_name": profile_name,
                    "requested_profile_id": record.pop("requested_profile_id", None),
                }
            records.append(device_record)
        return {"devices": records}

    async def system_dashboard_summary(
        self, *, allow_reauth: bool = True
    ) -> dict[str, object] | None:
        self.dashboard_summary_requests += 1
        return self.dashboard_summary_payload

    async def async_get_grid_profiles_filtered(
        self,
        *,
        country: str,
        state: str,
        commonly_used: bool,
    ) -> dict[str, object]:
        self.profile_requests.append((country, state, commonly_used))
        if commonly_used:
            return self.common_profile_payload
        return self.profile_payload

    async def async_apply_grid_profile(self, **kwargs: object) -> dict[str, object]:
        self.apply_requests.append(kwargs)
        return {"accepted": True}


class _FakeCoordinator:
    def __init__(self, client: _FakeGridProfileClient) -> None:
        self.client = client
        self.battery_country_code = None
        self.battery_region = None
        self.successes: list[str] = []
        self.failures: list[tuple[str, Exception]] = []
        self.listener_updates = 0

    def _endpoint_family_should_run(self, _family: str, *, force: bool = False) -> bool:
        return True

    def _note_endpoint_family_success(
        self,
        family: str,
        *,
        success_ttl_s: float | None = None,
    ) -> None:
        self.successes.append(family)

    def _note_endpoint_family_failure(self, family: str, err: Exception) -> bool:
        self.failures.append((family, err))
        return True

    def async_update_listeners(self) -> None:
        self.listener_updates += 1


async def test_runtime_scopes_regions_profiles_and_applies_exact_payload() -> None:
    client = _FakeGridProfileClient()
    coord = _FakeCoordinator(client)
    runtime = GridProfileRuntime(coord)

    result = await runtime.async_refresh(force=True)

    assert result.support_state == SUPPORT_CONFIRMED
    assert runtime.installer_access_ever_confirmed
    assert runtime.country_code == "AU"
    assert runtime.site_region_code == "VIC"
    assert runtime.region_options == ["VIC, AU - Victoria"]
    assert client.activation_auth_prepare_requests >= 1
    assert runtime.filtered_regions("Wellington") == []
    assert [region.region_code for region in runtime.filtered_regions("VIC")] == ["VIC"]
    assert client.profile_requests == [("AU", "VIC", True)]

    assert [profile.profile_id for profile in runtime.filtered_profiles()] == [
        "agf:common"
    ]
    assert runtime.filtered_profiles("export") == []
    assert runtime.filtered_profiles("capital") == []

    runtime.set_list_mode(ALL_PROFILES_OPTION)
    await runtime.async_load_profiles(
        region_code="VIC",
        commonly_used=False,
        force=True,
    )
    assert [profile.profile_id for profile in runtime.filtered_profiles("export")] == [
        "agf:export"
    ]
    runtime.set_staged_profile("agf:export")
    response = await runtime.async_apply_staged()

    assert response == {
        "success": True,
        "profile_id": "agf:export",
        "profile_name": "AS/NZS 4777.2: 2020 Australia A Region 0 kW Export (1.3.9)",
        "selected_profile_id": "agf:common",
        "requested_profile_id": "agf:export",
        "cloud_apply_status": "accepted",
    }
    assert client.apply_requests[-1] == {
        "gateway_serial": "122532006376",
        "part_num": "800-00555-r01",
        "ensemble_envoy": True,
        "profile_id": "agf:export",
    }
    assert runtime.pending_profile_id == "agf:export"


async def test_runtime_metadata_refresh_does_not_fetch_profile_catalog() -> None:
    client = _FakeGridProfileClient()
    runtime = GridProfileRuntime(_FakeCoordinator(client))

    result = await runtime.async_refresh(force=True, load_profiles=False)

    assert result.support_state == SUPPORT_CONFIRMED
    assert runtime.country_code == "AU"
    assert runtime.region_options == ["VIC, AU - Victoria"]
    assert client.profile_requests == []
    assert runtime.filtered_profiles() == []


async def test_runtime_profile_catalog_reuses_unexpired_cache() -> None:
    client = _FakeGridProfileClient()
    runtime = GridProfileRuntime(_FakeCoordinator(client))
    await runtime.async_refresh(force=True)
    client.profile_requests.clear()

    profiles = await runtime.async_load_profiles(force=False)

    assert [profile.profile_id for profile in profiles] == ["agf:common"]
    assert client.profile_requests == []


async def test_runtime_metadata_refresh_does_not_resolve_uncached_profile() -> None:
    client = _FakeGridProfileClient()
    runtime = GridProfileRuntime(_FakeCoordinator(client))
    await runtime.async_refresh(force=True)
    client.profile_requests.clear()
    client.activation_devices_payload = {
        "devices": [
            {
                "envoyCombiner": {"IQ Gateway": ["122532006376"]},
                "ensembleEnvoy": True,
                "envoyGridProfile": {
                    "selected_profile_id": "agf:export",
                    "selected_grid_profile_name": (
                        "AS/NZS 4777.2: 2020 Australia A Region " "0 kW Export (1.3.9)"
                    ),
                },
            }
        ]
    }

    await runtime.async_refresh(force=False, load_profiles=False)

    assert client.profile_requests == []
    assert runtime.current_profile_attributes()["pel_enabled"] is None


async def test_runtime_rejects_catalog_region_outside_site_country() -> None:
    runtime = GridProfileRuntime(_FakeCoordinator(_FakeGridProfileClient()))
    await runtime.async_refresh(force=True)

    with pytest.raises(ServiceValidationError) as err:
        await runtime.async_load_profiles(region_code="WGN", force=True)

    assert err.value.translation_key == "grid_profile_region_invalid"


async def test_coordinator_refreshes_grid_profile_metadata_after_first_poll() -> None:
    refresh = AsyncMock()
    created_tasks: list[asyncio.Task[None]] = []

    class _Coordinator:
        async_refresh_grid_profile_metadata = (
            EnphaseCoordinator.async_refresh_grid_profile_metadata
        )
        _clear_grid_profile_metadata_task = (
            EnphaseCoordinator._clear_grid_profile_metadata_task
        )
        _schedule_grid_profile_metadata_refresh = (
            EnphaseCoordinator._schedule_grid_profile_metadata_refresh
        )

        def __init__(self) -> None:
            self.site_id = "site"
            self.grid_profile_runtime = SimpleNamespace(
                async_refresh=refresh,
                support_state=SUPPORT_CONFIRMED,
            )
            self._grid_profile_metadata_task = None
            self._grid_profile_metadata_refresh_lock = asyncio.Lock()
            self.hass = SimpleNamespace(async_create_task=self._create_task)

        def _create_task(self, coro, *, name=None):
            task = asyncio.create_task(coro, name=name)
            created_tasks.append(task)
            return task

    coordinator = _Coordinator()
    first_refresh = SimpleNamespace(first_refresh=True)
    steady_refresh = SimpleNamespace(first_refresh=False)

    coordinator._schedule_grid_profile_metadata_refresh(first_refresh)
    refresh.assert_not_awaited()

    coordinator._schedule_grid_profile_metadata_refresh(steady_refresh)
    task = coordinator._grid_profile_metadata_task
    assert task is not None
    coordinator._schedule_grid_profile_metadata_refresh(steady_refresh)
    assert len(created_tasks) == 1
    await task

    refresh.assert_awaited_once_with(force=False, load_profiles=True)
    assert coordinator._grid_profile_metadata_task is None

    refresh.reset_mock()
    coordinator.grid_profile_runtime.support_state = SUPPORT_UNKNOWN
    coordinator._schedule_grid_profile_metadata_refresh(steady_refresh)
    coordinator.grid_profile_runtime.support_state = SUPPORT_DENIED
    coordinator._schedule_grid_profile_metadata_refresh(steady_refresh)
    refresh.assert_not_awaited()

    coordinator.grid_profile_runtime.support_state = SUPPORT_CONFIRMED
    coordinator.grid_profile_runtime.async_refresh = None
    await coordinator.async_refresh_grid_profile_metadata()


async def test_coordinator_grid_profile_metadata_refresh_has_deadline() -> None:
    async def _never_finishes(**_kwargs) -> None:
        await asyncio.sleep(60)

    coordinator = SimpleNamespace(
        site_id="site",
        grid_profile_runtime=SimpleNamespace(async_refresh=_never_finishes),
        _grid_profile_metadata_refresh_lock=asyncio.Lock(),
    )
    with patch(
        "custom_components.enphase_ev.coordinator.GRID_PROFILE_METADATA_REFRESH_DEADLINE_S",
        0,
    ):
        await EnphaseCoordinator.async_refresh_grid_profile_metadata(
            coordinator, force=True
        )


async def test_coordinator_grid_profile_deadline_includes_lock_wait() -> None:
    refresh = AsyncMock()
    lock = asyncio.Lock()
    await lock.acquire()
    coordinator = SimpleNamespace(
        site_id="site",
        grid_profile_runtime=SimpleNamespace(async_refresh=refresh),
        _grid_profile_metadata_refresh_lock=lock,
    )
    with patch(
        "custom_components.enphase_ev.coordinator.GRID_PROFILE_METADATA_REFRESH_DEADLINE_S",
        0,
    ):
        await EnphaseCoordinator.async_refresh_grid_profile_metadata(coordinator)
    lock.release()

    refresh.assert_not_awaited()


async def test_coordinator_serializes_startup_and_steady_grid_profile_refreshes() -> (
    None
):
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls: list[bool] = []

    async def _refresh(*, force: bool, load_profiles: bool) -> None:
        assert load_profiles is True
        calls.append(force)
        if force:
            first_started.set()
            await release_first.wait()

    coordinator = SimpleNamespace(
        site_id="site",
        grid_profile_runtime=SimpleNamespace(async_refresh=_refresh),
        _grid_profile_metadata_refresh_lock=asyncio.Lock(),
    )
    startup = asyncio.create_task(
        EnphaseCoordinator.async_refresh_grid_profile_metadata(coordinator, force=True)
    )
    await first_started.wait()
    steady = asyncio.create_task(
        EnphaseCoordinator.async_refresh_grid_profile_metadata(coordinator)
    )
    await asyncio.sleep(0)
    assert calls == [True]

    release_first.set()
    await asyncio.gather(startup, steady)
    assert calls == [True, False]


async def test_runtime_apply_rejects_profile_cached_for_another_region() -> None:
    client = _FakeGridProfileClient()
    runtime = GridProfileRuntime(_FakeCoordinator(client))
    await runtime.async_refresh(force=True)
    runtime.catalog_cache[("AU", "ACT", True)] = (
        9_999_999_999.0,
        [
            GridProfile(
                profile_id="agf:act-only",
                name="ACT only profile",
                group="ACT, AU",
                country="AU",
                state="ACT",
            )
        ],
    )

    with pytest.raises(ServiceValidationError) as err:
        await runtime.async_apply_grid_profile(
            "agf:act-only",
            region_code="VIC",
        )

    assert err.value.translation_key == "grid_profile_profile_invalid"
    assert client.apply_requests == []

    with pytest.raises(ServiceValidationError) as err:
        await runtime.async_apply_grid_profile(
            "agf:act-only",
            region_code="INVALID",
        )

    assert err.value.translation_key == "grid_profile_region_invalid"


async def test_runtime_defaults_region_from_activation_site_state() -> None:
    client = _FakeGridProfileClient()
    client.reference_payload = {
        "country_regions": {
            "AU": [
                {
                    "id": 1,
                    "countryCode": "AU",
                    "regionCode": "ACT",
                    "regionName": "Australian Capital Territory",
                },
                {
                    "id": 14,
                    "countryCode": "AU",
                    "regionCode": "VIC",
                    "regionName": "Victoria",
                },
            ]
        }
    }
    client.activation_record = {
        "address": {"state": "VIC", "country": "AU"},
        "grid_profile": "agf:common",
    }
    runtime = GridProfileRuntime(_FakeCoordinator(client))

    await runtime.async_refresh(force=True)

    assert runtime.staged_region_code == "VIC"
    assert client.profile_requests == [("AU", "VIC", True)]


async def test_runtime_denies_feature_when_activation_access_unavailable() -> None:
    client = _FakeGridProfileClient()

    async def denied_reference() -> dict[str, object]:
        raise ActivationAccessDenied("denied")

    client.async_get_activation_reference_data = denied_reference  # type: ignore[method-assign]
    runtime = GridProfileRuntime(_FakeCoordinator(client))

    result = await runtime.async_refresh(force=True)

    assert result.support_state == SUPPORT_DENIED
    assert not runtime.installer_access_confirmed
    assert runtime.region_options == []


async def test_runtime_marks_optional_activation_failure_unavailable() -> None:
    client = _FakeGridProfileClient()

    async def unavailable_reference() -> dict[str, object]:
        raise OptionalEndpointUnavailable("invalid payload")

    client.async_get_activation_reference_data = unavailable_reference  # type: ignore[method-assign]
    runtime = GridProfileRuntime(_FakeCoordinator(client))

    result = await runtime.async_refresh(force=True)

    assert result.support_state == SUPPORT_UNAVAILABLE
    assert not runtime.installer_access_confirmed


async def test_runtime_refreshes_device_status_during_partial_metadata_outage() -> None:
    client = _FakeGridProfileClient()
    runtime = GridProfileRuntime(_FakeCoordinator(client))
    await runtime.async_refresh(force=True, load_profiles=False)
    client.activation_devices_payload = {
        "devices": [
            {
                "envoyCombiner": {"IQ Gateway": ["122532006376"]},
                "ensembleEnvoy": True,
                "envoyGridProfile": {
                    "selected_profile_id": "agf:common",
                    "selected_grid_profile_name": "Updated profile",
                    "requested_profile_id": None,
                },
            }
        ]
    }

    async def unavailable_metadata() -> dict[str, object]:
        raise OptionalEndpointUnavailable("metadata unavailable")

    client.async_get_activation_reference_data = unavailable_metadata  # type: ignore[method-assign]
    client.async_get_activation_record = unavailable_metadata  # type: ignore[method-assign]

    result = await runtime.async_refresh(force=True, load_profiles=False)

    assert result.support_state == SUPPORT_CONFIRMED
    assert runtime.current_profile_display() == "Updated profile"
    assert client.activation_device_requests == 2


async def test_runtime_reuses_reference_cache_and_refreshes_status_only() -> None:
    client = _FakeGridProfileClient()
    runtime = GridProfileRuntime(_FakeCoordinator(client))
    await runtime.async_refresh(force=True, load_profiles=False)

    await runtime.async_refresh(force=False, load_profiles=False)

    assert client.reference_requests == 1
    assert client.activation_record_requests == 2
    assert client.activation_device_requests == 2

    await runtime.async_refresh_device_status(force=True)

    assert client.reference_requests == 1
    assert client.activation_record_requests == 2
    assert client.activation_device_requests == 3


async def test_runtime_status_only_refresh_failure_paths() -> None:
    client = _FakeGridProfileClient()
    coordinator = _FakeCoordinator(client)
    runtime = GridProfileRuntime(coordinator)
    coordinator._endpoint_family_should_run = lambda _family, force=False: False  # type: ignore[method-assign]

    assert (
        await runtime.async_refresh_device_status()
    ).support_state == SUPPORT_UNKNOWN
    assert client.activation_device_requests == 0

    coordinator._endpoint_family_should_run = lambda _family, force=False: True  # type: ignore[method-assign]

    async def unavailable_devices() -> dict[str, object]:
        raise OptionalEndpointUnavailable("device status unavailable")

    available_devices = client.async_get_activation_device_list
    client.async_get_activation_device_list = unavailable_devices  # type: ignore[method-assign]
    assert (
        await runtime.async_refresh_device_status(force=True)
    ).support_state == SUPPORT_UNAVAILABLE

    client.async_get_activation_device_list = available_devices  # type: ignore[method-assign]
    await runtime.async_refresh(force=True, load_profiles=False)
    client.async_get_activation_device_list = unavailable_devices  # type: ignore[method-assign]

    assert (
        await runtime.async_refresh_device_status(force=True)
    ).support_state == SUPPORT_CONFIRMED
    assert coordinator.failures


async def test_runtime_full_refresh_contains_device_status_failure() -> None:
    client = _FakeGridProfileClient()

    async def unavailable_devices() -> dict[str, object]:
        raise OptionalEndpointUnavailable("device status unavailable")

    client.async_get_activation_device_list = unavailable_devices  # type: ignore[method-assign]
    runtime = GridProfileRuntime(_FakeCoordinator(client))

    result = await runtime.async_refresh(force=True, load_profiles=False)

    assert result.support_state == SUPPORT_UNAVAILABLE
    assert runtime.activation_record is not None
    assert runtime.reference_payload is not None


async def test_runtime_contains_malformed_profile_catalog() -> None:
    client = _FakeGridProfileClient()
    client.common_profile_payload = {"unexpected": []}
    runtime = GridProfileRuntime(_FakeCoordinator(client))

    result = await runtime.async_refresh(force=True)

    assert result.support_state == SUPPORT_UNAVAILABLE
    assert runtime.filtered_profiles() == []


async def test_runtime_common_mode_matches_unprefixed_recommended_id() -> None:
    client = _FakeGridProfileClient()
    client.common_profile_payload["grid_profiles"] = {
        "VIC, AU": [
            {
                "name": "AS/NZS 4777.2: 2020 Australia A Region (1.3.12)",
                "profile_id": "agf:common",
            }
        ]
    }
    client.common_profile_payload["recommended_profile"] = {"profile_id": "common"}
    runtime = GridProfileRuntime(_FakeCoordinator(client))

    await runtime.async_refresh(force=True)

    assert [profile.profile_id for profile in runtime.filtered_profiles()] == [
        "agf:common"
    ]


async def test_runtime_common_mode_falls_back_to_grouped_response() -> None:
    client = _FakeGridProfileClient()
    client.common_profile_payload["recommended_profile"] = {"profile_id": "agf:missing"}
    runtime = GridProfileRuntime(_FakeCoordinator(client))

    await runtime.async_refresh(force=True)

    assert [profile.profile_id for profile in runtime.filtered_profiles()] == [
        "agf:export"
    ]


async def test_runtime_common_mode_prefers_complete_recommended_profile() -> None:
    client = _FakeGridProfileClient()
    runtime = GridProfileRuntime(_FakeCoordinator(client))

    await runtime.async_refresh(force=True)

    profiles = runtime.filtered_profiles()

    assert [
        (profile.profile_id, profile.name, profile.recommended) for profile in profiles
    ] == [
        (
            "agf:common",
            "AS/NZS 4777.2: 2020 Australia A Region (1.3.12)",
            True,
        )
    ]


async def test_profile_lookup_prefers_staged_region_cache() -> None:
    client = _FakeGridProfileClient()
    runtime = GridProfileRuntime(_FakeCoordinator(client))

    await runtime.async_refresh(force=True)
    runtime.catalog_cache[("AU", "ACT", True)] = (
        9999999999.0,
        [
            GridProfile(
                profile_id="agf:common",
                name="ACT Common",
                group="ACT, AU",
                country="AU",
                state="ACT",
            )
        ],
    )
    runtime.catalog_cache[("AU", "VIC", True)] = (
        9999999999.0,
        [
            GridProfile(
                profile_id="agf:common",
                name="VIC Common",
                group="VIC, AU",
                country="AU",
                state="VIC",
            )
        ],
    )
    runtime.set_region("VIC")

    profile = runtime.profile_for_id("agf:common")

    assert profile is not None
    assert profile.option_label == "VIC, AU: VIC Common"


async def test_current_profile_attributes_ignore_staged_region_filter() -> None:
    client = _FakeGridProfileClient()
    client.activation_record["address"] = {"state": "VIC", "country": "AU"}
    runtime = GridProfileRuntime(_FakeCoordinator(client))
    await runtime.async_refresh(force=True)
    runtime.regions_by_country["AU"].append(
        ActivationRegion("AU", "ACT", "Australian Capital Territory")
    )
    runtime.catalog_cache[("AU", "ACT", True)] = (
        9_999_999_999.0,
        [
            GridProfile(
                profile_id="agf:common",
                name="ACT Common",
                group="ACT, AU",
                country="AU",
                state="ACT",
                pel_enabled=True,
                is_277v_compatible=True,
            )
        ],
    )

    runtime.set_region("ACT")

    attributes = runtime.current_profile_attributes()
    assert runtime.staged_region_code == "ACT"
    assert attributes["region_code"] == "VIC"
    assert attributes["profile_group"] == "VIC, AU"
    assert attributes["pel_enabled"] is False
    assert attributes["is_277v_compatible"] is False


def test_grid_profile_sensor_survives_only_transient_unavailability() -> None:
    runtime = SimpleNamespace(
        installer_access_confirmed=False,
        installer_access_ever_confirmed=True,
        support_state=SUPPORT_UNAVAILABLE,
    )
    coordinator = SimpleNamespace(grid_profile_runtime=runtime)

    assert _retain_grid_profile_sensors(coordinator)

    runtime.support_state = SUPPORT_DENIED
    assert not _retain_grid_profile_sensors(coordinator)

    runtime.support_state = SUPPORT_UNAVAILABLE
    runtime.installer_access_ever_confirmed = False
    assert not _retain_grid_profile_sensors(coordinator)


async def test_runtime_derives_country_from_explicit_site_fields_only() -> None:
    client = _FakeGridProfileClient()
    client.activation_record = {
        "host": {"enlighten_view": "ON"},
        "address": {"state": "VIC", "country": "AU"},
        "grid_profile": "agf:common",
    }
    runtime = GridProfileRuntime(_FakeCoordinator(client))

    await runtime.async_refresh(force=True)

    assert runtime.country_code == "AU"
    assert runtime.region_options == ["VIC, AU - Victoria"]
    assert client.profile_requests == [("AU", "VIC", True)]
    assert (
        runtime.current_profile_display()
        == "AS/NZS 4777.2: 2020 Australia A Region (1.3.12)"
    )


async def test_runtime_uses_dashboard_country_without_guessing_region() -> None:
    client = _FakeGridProfileClient()
    client.reference_payload = {
        "country_regions": {
            "AU": [
                {
                    "countryCode": "AU",
                    "regionCode": "ACT",
                    "regionName": "Australian Capital Territory",
                },
                {
                    "countryCode": "AU",
                    "regionCode": "VIC",
                    "regionName": "Victoria",
                },
            ]
        }
    }
    client.activation_record = {
        "host": {"enlighten_view": "ON"},
        "grid_profile": "agf:common",
    }
    client.dashboard_summary_payload = {"country_code": "AU"}
    runtime = GridProfileRuntime(_FakeCoordinator(client))
    runtime.site_region_code = "WGN"
    runtime.staged_region_code = "WGN"

    await runtime.async_refresh(force=True)

    assert runtime.country_code == "AU"
    assert runtime.site_region_code is None
    assert runtime.staged_region_code is None
    assert client.dashboard_summary_requests == 1
    assert client.profile_requests == []

    runtime.set_region("VIC")
    await runtime.async_load_profiles(
        region_code="VIC",
        commonly_used=True,
        force=True,
    )

    assert runtime.site_region_code == "VIC"
    assert runtime.staged_region_code == "VIC"
    assert client.profile_requests == [("AU", "VIC", True)]


async def test_runtime_dashboard_country_fallback_is_optional() -> None:
    coordinator = _FakeCoordinator(_FakeGridProfileClient())
    coordinator.client = SimpleNamespace()
    runtime = GridProfileRuntime(coordinator)

    assert await runtime._async_derive_country() is None  # noqa: SLF001

    coordinator.client.system_dashboard_summary = AsyncMock(
        side_effect=RuntimeError("dashboard unavailable")
    )
    assert await runtime._async_derive_country() is None  # noqa: SLF001


async def test_runtime_resolves_current_profile_from_all_profiles() -> None:
    client = _FakeGridProfileClient()
    client.activation_record = {
        "address": {"state": "VIC", "country": "AU"},
        "grid_profile": "agf:export",
    }

    async def profile_payload(
        *,
        country: str,
        state: str,
        commonly_used: bool,
    ) -> dict[str, object]:
        client.profile_requests.append((country, state, commonly_used))
        if commonly_used:
            return {
                "grid_profiles": {
                    "VIC, AU": [
                        {
                            "name": "Australia Region A",
                            "profile_id": "agf:common",
                        }
                    ]
                }
            }
        return client.profile_payload

    client.async_get_grid_profiles_filtered = profile_payload  # type: ignore[method-assign]
    runtime = GridProfileRuntime(_FakeCoordinator(client))

    await runtime.async_refresh(force=True)

    assert client.profile_requests == [("AU", "VIC", True), ("AU", "VIC", False)]
    assert (
        runtime.current_profile_display()
        == "AS/NZS 4777.2: 2020 Australia A Region 0 kW Export (1.3.9)"
    )
    assert runtime.current_profile_attributes() == {
        "profile_id": "agf:export",
        "profile_group": "VIC, AU",
        "pel_enabled": True,
        "is_277v_compatible": None,
        "recommended": False,
        "source": "activation_record",
        "country_code": "AU",
        "region_code": "VIC",
        "support_state": SUPPORT_CONFIRMED,
        "gateway_target_count": 0,
    }


async def test_runtime_prefers_activation_envoys_selected_profile() -> None:
    client = _FakeGridProfileClient()
    client.activation_record = {
        "system": {"countryCode": "AU"},
        "address": {"state": "VIC", "country": "AU"},
        "envoys": [
            {
                "serial_num": "122532006376",
                "part_num": "800-00555-r01",
                "ensemble_envoy": True,
                "grid_profile_id": "agf:export",
                "grid_profile_name": (
                    "AS/NZS 4777.2: 2020 Australia A Region 0 kW Export (1.3.9)"
                ),
                "requested_profile_id": "agf:common",
            }
        ],
    }
    client.activation_devices_payload = {
        "devices": [
            {
                "envoyCombiner": {"IQ Gateway": ["122532006376"]},
                "ensembleEnvoy": True,
                "envoyGridProfile": {
                    "selected_profile_id": "agf:common",
                    "selected_grid_profile_name": (
                        "AS/NZS 4777.2: 2020 Australia A Region (1.3.12)"
                    ),
                    "requested_profile_id": None,
                },
            }
        ]
    }
    runtime = GridProfileRuntime(_FakeCoordinator(client))
    runtime.pending_profile_id = "agf:common"

    await runtime.async_refresh(force=True)

    assert (
        runtime.current_profile_display()
        == "AS/NZS 4777.2: 2020 Australia A Region (1.3.12)"
    )
    assert runtime.pending_profile_id is None
    assert runtime.requested_profile_id is None
    target = runtime.gateway_targets["122532006376"]
    assert target.current_profile_id == "agf:common"
    assert target.current_profile_name == (
        "AS/NZS 4777.2: 2020 Australia A Region (1.3.12)"
    )
    assert target.part_num == "800-00555-r01"
    assert runtime.current_profile_attributes() == {
        "profile_id": "agf:common",
        "profile_group": "VIC, AU",
        "pel_enabled": False,
        "is_277v_compatible": False,
        "recommended": True,
        "source": "gateway",
        "country_code": "AU",
        "region_code": "VIC",
        "support_state": SUPPORT_CONFIRMED,
        "gateway_target_count": 1,
    }


async def test_runtime_requires_gateway_metadata_before_apply() -> None:
    client = _FakeGridProfileClient()
    client.activation_record = {
        "system": {"countryCode": "AU"},
        "address": {"state": "VIC", "country": "AU"},
        "envoys": [
            {
                "serial_num": "122532006376",
                "part_num": "800-00555-r01",
            }
        ],
    }
    runtime = GridProfileRuntime(_FakeCoordinator(client))

    await runtime.async_refresh(force=True)
    runtime.set_staged_profile("agf:common")

    with pytest.raises(ServiceValidationError):
        await runtime.async_apply_staged()


async def test_runtime_applies_with_activation_root_gateway_without_part_num() -> None:
    client = _FakeGridProfileClient()
    client.activation_record = {
        "system": {"countryCode": "AU"},
        "address": {"state": "VIC", "country": "AU"},
        "ensemble_envoy": "122532006376",
        "grid_profile": "agf:common",
        "requested_profile": "agf:common",
    }
    runtime = GridProfileRuntime(_FakeCoordinator(client))

    await runtime.async_refresh(force=True)
    runtime.set_staged_profile("agf:common")
    await runtime.async_apply_staged()

    assert client.apply_requests[-1] == {
        "gateway_serial": "122532006376",
        "part_num": None,
        "ensemble_envoy": True,
        "profile_id": "agf:common",
    }


async def test_runtime_clears_pending_when_target_device_status_matches() -> None:
    client = _FakeGridProfileClient()
    client.activation_record = {
        "system": {"countryCode": "AU"},
        "address": {"state": "VIC", "country": "AU"},
        "ensemble_envoy": "122532006376",
        "grid_profile": "agf:old",
        "requested_profile": "agf:old",
    }
    runtime = GridProfileRuntime(_FakeCoordinator(client))

    await runtime.async_refresh(force=True)
    runtime.set_staged_profile("agf:common")
    await runtime.async_apply_staged()

    assert runtime.pending_profile_id == "agf:common"

    client.activation_record = {
        "system": {"countryCode": "AU"},
        "address": {"state": "VIC", "country": "AU"},
        "ensemble_envoy": "122532006376",
        "grid_profile": "agf:common",
        "requested_profile": "agf:common",
    }
    client.activation_devices_payload = {
        "devices": [
            {
                "envoyCombiner": {"IQ Gateway": ["122532006376"]},
                "ensembleEnvoy": True,
                "envoyGridProfile": {
                    "selected_profile_id": "agf:old",
                    "requested_profile_id": "agf:common",
                },
            }
        ]
    }
    await runtime.async_refresh(force=True)

    assert runtime.pending_profile_id == "agf:common"
    assert runtime.pending_gateway_serial == "122532006376"

    client.activation_devices_payload = {
        "devices": [
            {
                "envoyCombiner": {"IQ Gateway": ["122532006376"]},
                "ensembleEnvoy": True,
                "envoyGridProfile": {
                    "selected_profile_id": "agf:common",
                    "requested_profile_id": None,
                },
            }
        ]
    }
    await runtime.async_refresh(force=True)

    assert runtime.pending_profile_id is None
    assert runtime.pending_gateway_serial is None


async def test_runtime_starts_and_cancels_pending_refresh_task() -> None:
    client = _FakeGridProfileClient()
    client.activation_record = {
        "system": {"countryCode": "AU"},
        "address": {"state": "VIC", "country": "AU"},
        "ensemble_envoy": "122532006376",
        "grid_profile": "agf:old",
        "requested_profile": "agf:old",
    }
    coord = _FakeCoordinator(client)
    coord.hass = SimpleNamespace(
        async_create_task=lambda coro, name=None: asyncio.create_task(coro, name=name)
    )
    runtime = GridProfileRuntime(coord)

    await runtime.async_refresh(force=True)
    runtime.set_staged_profile("agf:common")
    await runtime.async_apply_staged()

    task = runtime._pending_refresh_task  # noqa: SLF001
    assert task is not None
    assert not task.done()

    runtime.cancel_pending_refresh()
    await asyncio.sleep(0)

    assert runtime._pending_refresh_task is None  # noqa: SLF001
    assert task.cancelled()


async def test_runtime_serializes_grid_profile_writes() -> None:
    client = _FakeGridProfileClient()
    runtime = GridProfileRuntime(_FakeCoordinator(client))
    await runtime.async_refresh(force=True)

    first_started = asyncio.Event()
    release_first = asyncio.Event()
    active_writes = 0
    max_active_writes = 0
    write_count = 0

    async def _apply(**_kwargs) -> dict[str, object]:
        nonlocal active_writes, max_active_writes, write_count
        write_count += 1
        active_writes += 1
        max_active_writes = max(max_active_writes, active_writes)
        if write_count == 1:
            first_started.set()
            await release_first.wait()
        active_writes -= 1
        return {}

    client.async_apply_grid_profile = _apply  # type: ignore[method-assign]
    first = asyncio.create_task(runtime.async_apply_grid_profile("agf:common"))
    await first_started.wait()
    second = asyncio.create_task(runtime.async_apply_grid_profile("agf:common"))
    await asyncio.sleep(0)

    assert write_count == 1
    release_first.set()
    await asyncio.gather(first, second)

    assert write_count == 2
    assert max_active_writes == 1


async def test_runtime_expires_unconfirmed_pending_profile() -> None:
    coordinator = _FakeCoordinator(_FakeGridProfileClient())
    runtime = GridProfileRuntime(coordinator)
    runtime.support_state = SUPPORT_CONFIRMED
    runtime.pending_profile_id = "agf:pending"
    runtime.pending_gateway_serial = "gateway"
    runtime.pending_started_mono = 1.0
    runtime._pending_poll_window_s = 0  # noqa: SLF001
    runtime._pending_refresh_task = asyncio.current_task()  # noqa: SLF001

    await runtime._async_poll_pending_profile("agf:pending")  # noqa: SLF001

    assert runtime.pending_profile_id is None
    assert runtime.pending_gateway_serial is None
    assert runtime.pending_started_mono is None
    assert runtime._pending_refresh_task is None  # noqa: SLF001
    assert coordinator.listener_updates == 1


async def test_runtime_accepts_false_ensemble_envoy_metadata() -> None:
    client = _FakeGridProfileClient()
    client.activation_record = {
        "system": {"countryCode": "AU"},
        "address": {"state": "VIC", "country": "AU"},
        "envoys": [
            {
                "serial_num": "122532006376",
                "part_num": "800-00555-r01",
                "ensemble_envoy": False,
            }
        ],
    }
    runtime = GridProfileRuntime(_FakeCoordinator(client))

    await runtime.async_refresh(force=True)
    runtime.set_staged_profile("agf:common")
    await runtime.async_apply_staged()

    assert client.apply_requests[-1]["ensemble_envoy"] is False


async def test_runtime_apply_button_unavailable_for_ambiguous_gateways() -> None:
    client = _FakeGridProfileClient()
    client.activation_record = {
        "system": {"countryCode": "AU"},
        "address": {"state": "VIC", "country": "AU"},
        "envoys": [
            {
                "serial_num": "122532006376",
                "part_num": "800-00555-r01",
                "ensemble_envoy": True,
                "grid_profile_id": "agf:first",
            },
            {
                "serial_num": "122532006377",
                "part_num": "800-00555-r01",
                "ensemble_envoy": False,
                "grid_profile_id": "agf:second",
            },
        ],
    }
    runtime = GridProfileRuntime(_FakeCoordinator(client))

    await runtime.async_refresh(force=True)
    runtime.set_staged_profile("agf:common")

    assert not runtime.apply_available
    assert runtime.current_profile_display() is None
    assert runtime.current_profile_attributes() == {
        "profile_id": None,
        "profile_group": None,
        "pel_enabled": None,
        "is_277v_compatible": None,
        "recommended": None,
        "source": "ambiguous_gateway",
        "country_code": "AU",
        "region_code": "VIC",
        "support_state": SUPPORT_CONFIRMED,
        "gateway_target_count": 2,
    }
    browse = runtime.browse()
    assert browse.current_profile is None
    assert browse.requested_profile is None


async def test_runtime_apply_response_uses_target_gateway_profile() -> None:
    client = _FakeGridProfileClient()
    client.activation_record = {
        "system": {"countryCode": "AU"},
        "address": {"state": "VIC", "country": "AU"},
        "envoys": [
            {
                "serial_num": "122532006376",
                "part_num": "800-00555-r01",
                "ensemble_envoy": True,
                "grid_profile_id": "agf:first",
            },
            {
                "serial_num": "122532006377",
                "part_num": "800-00555-r01",
                "ensemble_envoy": False,
                "grid_profile_id": "agf:second",
            },
        ],
    }
    runtime = GridProfileRuntime(_FakeCoordinator(client))
    await runtime.async_refresh(force=True)

    response = await runtime.async_apply_grid_profile(
        "agf:common",
        region_code="VIC",
        gateway_serial="122532006376",
    )

    assert response["selected_profile_id"] == "agf:first"
    assert runtime.pending_gateway_serial == "122532006376"
    assert client.profile_requests == [
        ("AU", "VIC", True),
        ("AU", "VIC", False),
    ]


def test_grid_profile_runtime_helper_and_lookup_edge_paths() -> None:
    class _Unprintable:
        def __str__(self) -> str:
            raise ValueError

    assert _clean_text(_Unprintable()) is None
    assert _coerce_bool(1) is True
    assert _coerce_bool("yes") is True
    assert _coerce_bool("no") is False
    assert _coerce_bool("maybe") is None
    assert ActivationRegion("AU", "VIC", "").label == "VIC, AU"
    assert _group_matches_region("", country="AU", state="VIC")
    assert not _group_matches_region("VIC, NZ", country="AU", state="VIC")

    runtime = GridProfileRuntime(_FakeCoordinator(_FakeGridProfileClient()))
    runtime.country_code = "AU"
    runtime.regions_by_country = {"AU": [ActivationRegion("AU", "VIC", "Victoria")]}
    runtime.staged_region_code = "VIC"
    profile = GridProfile(
        profile_id="agf:fallback",
        name="Fallback profile",
        group="ACT, AU",
        country="AU",
        state="ACT",
    )
    runtime.catalog_cache[("NZ", "WGN", True)] = (
        9_999_999_999.0,
        [
            GridProfile(
                profile_id="agf:nz",
                name="New Zealand profile",
                group="WGN, NZ",
                country="NZ",
                state="WGN",
            )
        ],
    )
    runtime.catalog_cache[("AU", "ACT", True)] = (
        9_999_999_999.0,
        [profile],
    )
    runtime.catalog_cache[("AU", "VIC", True)] = (
        9_999_999_999.0,
        [
            GridProfile(
                profile_id="agf:vic",
                name="Victoria profile",
                group="VIC, AU",
                country="AU",
                state="VIC",
            )
        ],
    )

    assert runtime.list_mode_option == COMMONLY_USED_OPTION
    assert runtime.staged_region_label == "VIC, AU - Victoria"
    assert runtime.staged_profile_options == ["VIC, AU: Victoria profile"]
    assert runtime.status == "unknown"
    runtime.pending_profile_id = "agf:pending"
    assert runtime.status == "pending"
    runtime.pending_profile_id = None
    runtime.support_state = SUPPORT_CONFIRMED
    assert runtime.status == "available"
    runtime.support_state = SUPPORT_UNKNOWN
    assert runtime.region_for_code(None) is None
    assert runtime.region_code_for_label("") is None
    assert runtime.region_code_for_label("VIC") == "VIC"
    assert runtime.region_code_for_label("VIC, AU - Victoria") == "VIC"
    assert runtime.region_code_for_label("missing") is None
    assert runtime.profile_for_id("agf:fallback") == profile
    assert runtime.profile_for_id("missing") is None
    assert runtime.profile_for_id_in_region(None, "VIC") is None
    runtime.current_profile_id = "agf:uncached"
    assert runtime.current_profile_display() == "agf:uncached"
    assert runtime.profile_id_for_label("") is None
    assert runtime.profile_id_for_label("VIC, AU: Victoria profile") == "agf:vic"
    assert runtime.profile_id_for_label("Victoria profile") == "agf:vic"
    assert runtime.profile_id_for_label("missing") is None
    runtime.set_search_query("vic")
    assert runtime.staged_query == "vic"

    runtime.activation_record = {}
    runtime.coordinator.battery_country_code = None
    runtime.coordinator.battery_region = None
    assert runtime._derive_country() is None  # noqa: SLF001
    assert runtime._find_country_code("Australia") is None  # noqa: SLF001
    assert runtime._find_country_code(["Australia", "AU"]) == "AU"  # noqa: SLF001
    assert runtime._find_region_code("") is None  # noqa: SLF001
    assert runtime._find_region_code("Victoria") == "VIC"  # noqa: SLF001
    assert runtime._find_region_code("VIC, AU - Victoria") == "VIC"  # noqa: SLF001
    assert runtime._find_region_code(["missing", "VIC"]) == "VIC"  # noqa: SLF001


def test_grid_profile_runtime_parser_edge_paths() -> None:
    runtime = GridProfileRuntime(_FakeCoordinator(_FakeGridProfileClient()))

    assert (
        runtime._envoy_serial_from_record(  # noqa: SLF001
            {"serial_num": "122532006376"}
        )
        == "122532006376"
    )
    assert (
        runtime._envoy_serial_from_record(  # noqa: SLF001
            {"envoyCombiner": {"IQ Gateway": "122532006376"}}
        )
        == "122532006376"
    )
    assert (
        runtime._envoy_serial_from_record({"envoyCombiner": {}}) is None  # noqa: SLF001
    )
    assert (
        runtime._envoy_serial_from_record(  # noqa: SLF001
            {"envoyCombiner": {"IQ Gateway": []}}
        )
        is None
    )

    with pytest.raises(ValueError, match="not an object"):
        runtime._parse_reference(None)  # noqa: SLF001
    with pytest.raises(ValueError, match="country regions"):
        runtime._parse_reference({})  # noqa: SLF001
    runtime._parse_reference(  # noqa: SLF001
        {
            "regions": {
                "": [],
                "NZ": "invalid",
                "AU": [
                    None,
                    {"countryCode": "AU"},
                    {
                        "countryCode": "AU",
                        "regionCode": "VIC",
                        "regionName": "",
                        "id": "invalid",
                    },
                ],
            }
        }
    )
    assert runtime.regions_by_country["AU"] == [ActivationRegion("AU", "VIC", "VIC")]

    runtime._parse_activation_record(  # noqa: SLF001
        {
            "envoy_serial_numbers": ["122532006376"],
            "grid_profile": "agf:current",
        }
    )
    assert runtime.gateway_targets["122532006376"].current_profile_id == "agf:current"
    runtime._parse_activation_devices([None])  # noqa: SLF001
    runtime._parse_activation_devices("invalid")  # noqa: SLF001
    runtime._parse_activation_devices(  # noqa: SLF001
        {
            "requested_profile_id": "agf:requested",
            "requested_grid_profile_name": "Requested profile",
        }
    )
    assert runtime.requested_profile_id == "agf:requested"
    runtime_without_target = GridProfileRuntime(
        _FakeCoordinator(_FakeGridProfileClient())
    )
    runtime_without_target._parse_activation_devices(  # noqa: SLF001
        {"requested_profile_id": "agf:requested-without-target"}
    )
    assert runtime_without_target.requested_profile_id == "agf:requested-without-target"

    with pytest.raises(ValueError, match="not an object"):
        runtime._parse_profiles(  # noqa: SLF001
            None,
            country="AU",
            state="VIC",
            commonly_used=False,
        )
    profiles = runtime._parse_profiles(  # noqa: SLF001
        {
            "grid_profiles": {
                "VIC": "invalid",
                "VIC, AU": [
                    None,
                    {},
                    {"profile_id": "agf:valid", "name": "Valid profile"},
                    {"profile_id": "agf:valid", "name": "Duplicate profile"},
                ],
            }
        },
        country="AU",
        state="VIC",
        commonly_used=False,
    )
    assert [profile.profile_id for profile in profiles] == ["agf:valid"]


async def test_grid_profile_runtime_optional_and_browse_edge_paths() -> None:
    client = _FakeGridProfileClient()
    coordinator = _FakeCoordinator(client)
    runtime = GridProfileRuntime(coordinator)

    runtime._mark_denied(  # noqa: SLF001
        aiohttp.ClientResponseError(None, (), status=403)
    )
    assert runtime.support_state == SUPPORT_DENIED

    coordinator._endpoint_family_should_run = lambda _family, force=False: False  # type: ignore[method-assign]
    assert (await runtime.async_refresh()).support_state == SUPPORT_DENIED
    assert await GridProfileRuntime(coordinator).async_load_profiles() == []

    coordinator._endpoint_family_should_run = lambda _family, force=False: True  # type: ignore[method-assign]
    runtime.support_state = SUPPORT_CONFIRMED
    runtime.country_code = None
    runtime.staged_region_code = None
    assert await runtime.async_load_profiles() == []

    await runtime.async_refresh(force=True)
    result = runtime.browse(
        region_code="VIC",
        query="common",
        commonly_used=True,
    )
    assert result.profiles
    assert runtime.browse_dict()["support_state"] == SUPPORT_CONFIRMED
    assert runtime._target_for_serial("122532006376") is not None  # noqa: SLF001
    assert runtime.diagnostics()["profile_count"] == 1


async def test_grid_profile_runtime_pending_and_apply_error_paths() -> None:
    client = _FakeGridProfileClient()
    runtime = GridProfileRuntime(_FakeCoordinator(client))

    with pytest.raises(ServiceValidationError) as raised:
        await runtime.async_apply_grid_profile("agf:common")
    assert raised.value.translation_key == "grid_profile_unavailable"

    runtime.support_state = SUPPORT_DENIED
    with pytest.raises(ServiceValidationError) as raised:
        await runtime.async_apply_grid_profile("agf:common")
    assert raised.value.translation_key == "grid_profile_installer_required"

    await runtime.async_refresh(force=True)
    runtime.set_staged_profile("agf:common")
    task = asyncio.create_task(asyncio.sleep(60))
    runtime._pending_refresh_task = task  # noqa: SLF001
    runtime._clear_pending_profile()  # noqa: SLF001
    await asyncio.sleep(0)
    assert task.cancelled()

    runtime.pending_profile_id = "agf:pending"
    runtime._pending_poll_interval_s = 0  # noqa: SLF001

    async def clear_pending_after_refresh(*, force: bool = False) -> object:
        runtime.pending_profile_id = None
        return runtime.browse()

    runtime.async_refresh_device_status = AsyncMock(  # type: ignore[method-assign]
        side_effect=clear_pending_after_refresh
    )
    runtime._pending_refresh_task = asyncio.current_task()  # noqa: SLF001
    await runtime._async_poll_pending_profile("agf:pending")  # noqa: SLF001
    runtime.async_refresh_device_status.assert_awaited_once_with(force=True)
    assert runtime._pending_refresh_task is None  # noqa: SLF001

    runtime.pending_profile_id = "agf:break"

    async def clear_during_sleep(_delay: float) -> None:
        runtime.pending_profile_id = None

    with patch(
        "custom_components.enphase_ev.grid_profile_runtime.asyncio.sleep",
        side_effect=clear_during_sleep,
    ):
        await runtime._async_poll_pending_profile("agf:break")  # noqa: SLF001

    runtime.async_refresh_device_status.reset_mock()
    runtime.pending_profile_id = "agf:denied"
    runtime.support_state = SUPPORT_DENIED
    await runtime._async_poll_pending_profile("agf:denied")  # noqa: SLF001
    runtime.async_refresh_device_status.assert_not_awaited()

    runtime.pending_profile_id = "agf:denied-during-sleep"
    runtime.support_state = SUPPORT_CONFIRMED

    async def deny_during_sleep(_delay: float) -> None:
        runtime.support_state = SUPPORT_DENIED

    with patch(
        "custom_components.enphase_ev.grid_profile_runtime.asyncio.sleep",
        side_effect=deny_during_sleep,
    ):
        await runtime._async_poll_pending_profile(  # noqa: SLF001
            "agf:denied-during-sleep"
        )
    runtime.async_refresh_device_status.assert_not_awaited()
    runtime.support_state = SUPPORT_CONFIRMED

    runtime.pending_profile_id = "agf:cancel"
    runtime._pending_poll_interval_s = 60  # noqa: SLF001
    poll_task = asyncio.create_task(
        runtime._async_poll_pending_profile("agf:cancel")  # noqa: SLF001
    )
    await asyncio.sleep(0)
    poll_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await poll_task

    runtime.async_refresh = AsyncMock(return_value=runtime.browse())  # type: ignore[method-assign]
    client.async_apply_grid_profile = AsyncMock(  # type: ignore[method-assign]
        side_effect=ServiceValidationError("invalid")
    )
    with pytest.raises(ServiceValidationError, match="invalid"):
        await runtime.async_apply_staged()

    client.async_apply_grid_profile = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("failed")
    )
    with pytest.raises(ServiceValidationError) as raised:
        await runtime.async_apply_staged()
    assert raised.value.translation_key == "grid_profile_apply_failed"
    with pytest.raises(ServiceValidationError):
        await runtime.async_apply_staged()
