"""Tests for read-only gateway software-update progress."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from custom_components.enphase_ev.gateway_software_update import (
    GATEWAY_UPDATE_MAX_COMPONENTS,
    GATEWAY_UPDATE_MAX_DEVICE_STATUSES,
    GATEWAY_UPDATE_MAX_STATUS_TEXT,
    GatewaySoftwareUpdateManager,
    duration_seconds,
    normalize_gateway_software_update,
)


def _payload(
    *,
    current_status: int = 2,
    current_status_text: str = "Installing software update",
) -> dict:
    return {
        "site": [
            {
                "timestamp": "2026-07-11T05:06:07Z",
                "site_update_info": [
                    {
                        "Current_Status": current_status,
                        "Current_Status_str": current_status_text,
                        "Last_Status": 0,
                        "Last_Status_str": "Previous update completed",
                        "Estimated Time Left": "1 min 30 sec",
                        "Total Duration": "00:04:00",
                        "Current essimg version": "8.3.6000",
                    }
                ],
            }
        ],
        "devices": [
            {
                "serial_num": "must-not-be-retained",
                "update_info": [
                    "Gateway components updating",
                    [
                        {
                            "fw_image": "/tmp/private/image-hash.bin",
                            "name": "Gateway OS",
                            "type": "essimg",
                            "status": 2,
                            "status_str": "Installing",
                            "progress": "40%",
                            "latest_speed_bps": "1024",
                        },
                        {
                            "name": "Controller",
                            "type": "e3",
                            "status": "running",
                            "status_str": "Transferring",
                            "progress": 60,
                            "latest_speed_bps": 512.5,
                        },
                        None,
                    ],
                    {"e3_progress": "55%"},
                ],
            }
        ],
    }


def test_normalize_gateway_software_update_active_payload_is_sanitized() -> None:
    status = normalize_gateway_software_update(_payload())

    assert status == {
        "current_status": 2,
        "current_status_text": "Installing software update",
        "last_status": 0,
        "last_status_text": "Previous update completed",
        "estimated_time_left": "1 min 30 sec",
        "estimated_time_left_seconds": 90,
        "total_duration": "00:04:00",
        "total_duration_seconds": 240,
        "installed_image_version": "8.3.6000",
        "last_reported_at": "2026-07-11T05:06:07Z",
        "device_statuses": ["Gateway components updating"],
        "component_updates": [
            {
                "name": "Gateway OS",
                "type": "essimg",
                "status": 2,
                "status_text": "Installing",
                "progress": 40.0,
                "latest_speed_bps": 1024,
            },
            {
                "name": "Controller",
                "type": "e3",
                "status": "running",
                "status_text": "Transferring",
                "progress": 60.0,
                "latest_speed_bps": 512.5,
            },
        ],
        "e3_progress": 55.0,
        "transfer_speed_bps": 1536.5,
        "in_progress": True,
        "update_percentage": 55.0,
    }
    serialized = repr(status)
    assert "serial_num" not in serialized
    assert "private" not in serialized
    assert "image-hash" not in serialized


def test_normalize_gateway_software_update_redacts_and_bounds_free_text() -> None:
    payload = _payload()
    update_info = payload["site"][0]["site_update_info"][0]
    update_info["Current_Status_str"] = "Installing GW-SERIAL " + ("x" * 300)
    payload["devices"] = [
        {
            "update_info": [
                *(f"GW-SERIAL status {index}" for index in range(40)),
                [
                    {
                        "name": f"GW-SERIAL component {index}",
                        "status_str": "Installing",
                        "progress": index,
                    }
                    for index in range(40)
                ],
            ]
        }
    ]

    status = normalize_gateway_software_update(
        payload,
        identifiers=("GW-SERIAL",),
    )

    assert status is not None
    assert "GW-SERIAL" not in repr(status)
    assert len(status["current_status_text"]) <= GATEWAY_UPDATE_MAX_STATUS_TEXT + 3
    assert len(status["device_statuses"]) == GATEWAY_UPDATE_MAX_DEVICE_STATUSES
    assert len(status["component_updates"]) == GATEWAY_UPDATE_MAX_COMPONENTS


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("90", 90),
        ("01:30", 90),
        ("01:02:03", 3723),
        ("1 day 2 hrs 3 minutes 4 seconds", 93784),
        ("1.5 h", 5400),
        ("unknown", None),
    ],
)
def test_duration_seconds(value, expected) -> None:
    assert duration_seconds(value) == expected


def test_normalize_gateway_software_update_terminal_and_shape_variants() -> None:
    payload = _payload(current_status=0, current_status_text="Software is up to date")
    payload["site"] = payload["site"][0]
    payload["site"]["site_update_info"] = payload["site"]["site_update_info"][0]
    payload["devices"] = {
        "update_info": {
            "name": "Gateway",
            "status_str": "Complete",
            "progress": 120,
            "latest_speed_bps": -1,
            "e3_progress": 100,
        }
    }

    status = normalize_gateway_software_update(payload)

    assert status is not None
    assert status["in_progress"] is False
    assert status["update_percentage"] is None
    assert status["e3_progress"] == 100
    assert status["component_updates"][0]["progress"] == 100
    assert status["component_updates"][0]["latest_speed_bps"] is None


def test_normalize_gateway_software_update_infers_average_and_unknown_state() -> None:
    average_payload = {
        "site": {"site_update_info": {"Current_Status": "unknown"}},
        "devices": [
            {
                "update_info": [
                    [{"name": "A", "progress": 20}, {"name": "B", "progress": 40}]
                ]
            }
        ],
    }
    averaged = normalize_gateway_software_update(average_payload)
    assert averaged is not None
    assert averaged["update_percentage"] == 30
    assert averaged["in_progress"] is True

    unknown = normalize_gateway_software_update(
        {
            "site": {"site_update_info": {"Current_Status": "unknown"}},
            "devices": [],
        }
    )
    assert unknown is not None
    assert unknown["in_progress"] is None
    assert unknown["update_percentage"] is None

    idle = normalize_gateway_software_update(
        {
            "site": {"site_update_info": {"Current_Status": 0}},
            "devices": 123,
        }
    )
    assert idle is not None
    assert idle["in_progress"] is False


def test_normalize_gateway_software_update_ignores_invalid_numeric_values() -> None:
    status = normalize_gateway_software_update(
        {
            "site": {"site_update_info": {"Current_Status": True}},
            "devices": {
                "update_info": {
                    "name": "Gateway",
                    "progress": "unknown",
                    "latest_speed_bps": True,
                }
            },
        }
    )
    assert status is not None
    assert status["current_status"] is None
    assert status["component_updates"][0]["progress"] is None
    assert status["component_updates"][0]["latest_speed_bps"] is None
    assert status["transfer_speed_bps"] is None


def test_normalize_gateway_software_update_prefers_active_component() -> None:
    status = normalize_gateway_software_update(
        {
            "site": {"site_update_info": {"Current_Status": "unknown"}},
            "devices": {
                "update_info": [
                    [
                        {"name": "A", "status_str": "Completed", "progress": 100},
                        {"name": "B", "status_str": "Installing", "progress": 20},
                    ]
                ]
            },
        }
    )
    assert status is not None
    assert status["in_progress"] is True

    failed = normalize_gateway_software_update(
        {
            "site": {
                "site_update_info": {"Current_Status_str": "Software update failed"}
            },
            "devices": [],
        }
    )
    assert failed is not None
    assert failed["in_progress"] is False

    completed_component = normalize_gateway_software_update(
        {
            "site": {"site_update_info": {"Current_Status": "unknown"}},
            "devices": {"update_info": [[{"name": "A", "status_str": "Completed"}]]},
        }
    )
    assert completed_component is not None
    assert completed_component["in_progress"] is False


def test_normalize_gateway_software_update_partial_percentage_overrides_complete() -> (
    None
):
    partial = normalize_gateway_software_update(
        {
            "site": {
                "site_update_info": {
                    "Current_Status_str": "Installing update - 50% complete"
                }
            },
            "devices": {"update_info": [[{"name": "A", "progress": 50}]]},
        }
    )
    assert partial is not None
    assert partial["in_progress"] is True
    assert partial["update_percentage"] == 50

    failed = normalize_gateway_software_update(
        {
            "site": {
                "site_update_info": {
                    "Current_Status_str": "Installing update failed at 50%"
                }
            },
            "devices": {"update_info": [[{"name": "A", "progress": 50}]]},
        }
    )
    assert failed is not None
    assert failed["in_progress"] is False
    assert failed["update_percentage"] is None

    active_text = normalize_gateway_software_update(
        {
            "site": {"site_update_info": {"Current_Status_str": "Installing update"}},
            "devices": [],
        }
    )
    assert active_text is not None
    assert active_text["in_progress"] is True

    active_device = normalize_gateway_software_update(
        {
            "site": {"site_update_info": {"Current_Status": "unknown"}},
            "devices": {"update_info": ["Gateway updating"]},
        }
    )
    assert active_device is not None
    assert active_device["in_progress"] is True


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"site": []},
        {"site": [{}], "devices": [None, {"update_info": [None, 1]}]},
    ],
)
def test_normalize_gateway_software_update_rejects_missing_status(payload) -> None:
    assert normalize_gateway_software_update(payload) is None


@pytest.mark.asyncio
async def test_manager_caches_active_status_and_force_refreshes() -> None:
    client = AsyncMock()
    client.site_livestream_payload.return_value = _payload()
    manager = GatewaySoftwareUpdateManager(
        lambda: client,
        lambda: "GW-SERIAL",
        active_ttl_seconds=15,
        idle_ttl_seconds=60,
    )

    first = await manager.async_get_status()
    second = await manager.async_get_status()
    forced = await manager.async_get_status(force_refresh=True)

    assert first == second == forced
    assert client.site_livestream_payload.await_count == 2
    client.site_livestream_payload.assert_awaited_with(
        "GW-SERIAL", live_debug=True, timeout_s=15.0
    )
    assert manager.cached_status == first
    assert 0 < manager.next_refresh_seconds <= 15
    snapshot = manager.status_snapshot()
    assert snapshot["last_fetch_utc"] is not None
    assert snapshot["last_success_utc"] is not None
    assert snapshot["last_error"] is None
    assert snapshot["using_stale"] is False


@pytest.mark.asyncio
async def test_manager_rechecks_cache_after_waiting_for_refresh_lock() -> None:
    client = AsyncMock()
    release = asyncio.Event()

    async def _payload_after_release(*args, **kwargs):  # noqa: ARG001
        await release.wait()
        return _payload()

    client.site_livestream_payload.side_effect = _payload_after_release
    manager = GatewaySoftwareUpdateManager(lambda: client, lambda: "GW-SERIAL")

    first_task = asyncio.create_task(manager.async_get_status())
    await asyncio.sleep(0)
    second_task = asyncio.create_task(manager.async_get_status())
    await asyncio.sleep(0)
    release.set()

    first, second = await asyncio.gather(first_task, second_task)
    assert first == second
    assert client.site_livestream_payload.await_count == 1


@pytest.mark.asyncio
async def test_manager_uses_idle_ttl_and_stale_status_on_error() -> None:
    client = AsyncMock()
    client.site_livestream_payload.side_effect = [
        _payload(current_status=0, current_status_text="Software is up to date"),
        RuntimeError("request failed for GW-SERIAL"),
    ]
    manager = GatewaySoftwareUpdateManager(
        lambda: client,
        lambda: "GW-SERIAL",
        idle_ttl_seconds=60,
        retry_backoff_seconds=60,
    )

    status = await manager.async_get_status()
    stale = await manager.async_get_status(force_refresh=True)

    assert stale == status
    snapshot = manager.status_snapshot()
    assert snapshot["using_stale"] is True
    assert snapshot["last_error"] == "request failed for GW-S...RIAL"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client", "serial", "error"),
    [
        (None, "GW-SERIAL", "client unavailable"),
        (AsyncMock(), None, "gateway serial unavailable"),
    ],
)
async def test_manager_backs_off_when_dependencies_are_unavailable(
    client, serial, error
) -> None:
    manager = GatewaySoftwareUpdateManager(
        lambda: client,
        lambda: serial,
        retry_backoff_seconds=60,
    )

    assert await manager.async_get_status() is None
    assert manager.status_snapshot()["last_error"] == error


@pytest.mark.asyncio
async def test_manager_rejects_empty_payload_and_bounds_constructor_values() -> None:
    client = AsyncMock()
    client.site_livestream_payload.return_value = {"site": []}
    manager = GatewaySoftwareUpdateManager(
        lambda: client,
        lambda: "GW-SERIAL",
        idle_ttl_seconds=1,
        active_ttl_seconds=1,
        retry_backoff_seconds=1,
        fetch_timeout_seconds=1,
    )

    assert await manager.async_get_status() is None
    assert manager.status_snapshot()["last_error"] == (
        "software-update status unavailable"
    )


@pytest.mark.asyncio
async def test_manager_propagates_cancellation() -> None:
    client = AsyncMock()
    client.site_livestream_payload.side_effect = asyncio.CancelledError
    manager = GatewaySoftwareUpdateManager(lambda: client, lambda: "GW-SERIAL")

    with pytest.raises(asyncio.CancelledError):
        await manager.async_get_status()
