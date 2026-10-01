"""HTTP retry deadlines have one interpretation across endpoint managers."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import aiohttp
import pytest

from custom_components.enphase_ev import cloud_retry


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("", None),
        ("60", 60.0),
        (" 60 ", 60.0),
        ("-60", 0.0),
        ("0", 0.0),
        ("invalid", None),
        ("NaN", None),
        ("1" * 400, None),
        ("Wed, 01 Jan 2025 12:01:30 GMT", 90.0),
        ("Wed, 01 Jan 2025 12:01:30", 90.0),
        ("Wed, 01 Jan 2025 11:00:00 GMT", 0.0),
    ],
)
def test_retry_after_header(header, expected):
    now = datetime(2025, 1, 1, 12, tzinfo=timezone.utc)
    assert cloud_retry.retry_after_seconds(header, now=now) == expected


def test_retry_after_uses_current_time_and_normalizes_naive_clock(monkeypatch):
    now = datetime(2025, 1, 1, 12)
    monkeypatch.setattr(cloud_retry.dt_util, "utcnow", lambda: now)
    header = "Wed, 01 Jan 2025 12:01:30 GMT"
    assert cloud_retry.retry_after_seconds(header) == 90.0
    assert (
        cloud_retry.retry_after_seconds(
            header,
            now=now.replace(tzinfo=timezone(timedelta(hours=1))),
        )
        == 3690.0
    )


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (None, None),
        ({}, None),
        ({"Date": "ignored"}, None),
        ({"Retry-After": "60"}, 60.0),
        ({"retry-after": "60"}, 60.0),
    ],
)
def test_retry_after_http_exception(headers, expected):
    error = aiohttp.ClientResponseError(MagicMock(), (), status=429, headers=headers)
    assert cloud_retry.retry_after_delay(error) == expected


def test_retry_after_non_http_exception():
    assert cloud_retry.retry_after_delay(RuntimeError()) is None


def test_retry_after_wrapped_cloud_exception_and_cycles():
    from custom_components.enphase_ev.api import SiteEnergyUnavailable

    error = aiohttp.ClientResponseError(
        MagicMock(), (), status=503, headers={"Retry-After": "3600"}
    )
    wrapped = SiteEnergyUnavailable("Site service unavailable")
    wrapped.__cause__ = error
    assert cloud_retry.retry_after_delay(wrapped) == 3600.0
    outer = RuntimeError()
    outer.__cause__ = wrapped
    assert cloud_retry.retry_after_delay(outer) == 3600.0
    wrapped.__cause__ = outer
    assert cloud_retry.retry_after_delay(outer) is None
    current = RuntimeError()
    outer = current
    for _ in range(10):
        next_error = RuntimeError()
        current.__cause__ = next_error
        current = next_error
    current.__cause__ = error
    assert cloud_retry.retry_after_delay(outer) is None
