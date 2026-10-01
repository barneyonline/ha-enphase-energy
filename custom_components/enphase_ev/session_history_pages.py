"""Validate and accumulate bounded session-history responses."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, cast

from homeassistant.util import dt as dt_util

from .api_client.errors import InvalidPayloadError


def parse_session_timestamp(value: object) -> datetime | None:
    """Parse the same provider timestamps for validation and normalization."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return cast(
                datetime,
                dt_util.as_local(datetime.fromtimestamp(float(value), tz=timezone.utc)),
            )
        except Exception:  # noqa: BLE001 - invalid timestamp or timezone conversion
            return None
    if isinstance(value, str):
        cleaned = value.strip().replace("[UTC]", "")
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(cleaned)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        try:
            return cast(datetime, dt_util.as_local(parsed))
        except Exception:  # noqa: BLE001 - defensive timezone conversion
            return None
    return None


class SessionHistoryPages:
    """Keep incomplete or repeated pages from becoming authoritative totals."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self.page_count = 0
        self.duplicate_count = 0
        self.complete = False
        self.outcome = "in_progress"
        self._identities: set[str] = set()
        self._pages: set[tuple[str, ...]] = set()

    def _invalid(self, outcome: str) -> InvalidPayloadError:
        self.outcome = outcome
        return InvalidPayloadError(
            "Incomplete or invalid session history",
            failure_kind=f"session_history_{outcome}",
        )

    def add(self, payload: object) -> bool:
        """Return whether another page is required after validating this one."""
        self.page_count += 1
        data = payload.get("data") if isinstance(payload, dict) else None
        rows = data.get("result") if isinstance(data, dict) else None
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise self._invalid("invalid_payload")
        more = cast(dict[str, Any], data).get("hasMore", None if rows else False)
        if not isinstance(more, bool):
            raise self._invalid("invalid_payload")
        if any(
            parse_session_timestamp(row.get("startTime")) is None
            and parse_session_timestamp(row.get("endTime")) is None
            for row in rows
        ):
            raise self._invalid("invalid_rows")
        identities = [str(row.get("sessionId") or row.get("id") or "") for row in rows]
        signature = tuple(
            f"id:{identity}" if identity else json.dumps(row, sort_keys=True)
            for row, identity in zip(rows, identities, strict=True)
        )
        if rows and signature in self._pages:
            raise self._invalid("repeated_page")
        self._pages.add(signature)
        for row, session_id in zip(rows, identities, strict=True):
            if session_id and session_id in self._identities:
                self.duplicate_count += 1
                continue
            if session_id:
                self._identities.add(session_id)
            self.items.append(row)
        if more and not rows:
            raise self._invalid("incomplete_page")
        self.complete = not more
        if self.complete:
            self.outcome = "success"
        return more

    def finish(self) -> list[dict[str, Any]]:
        """Return rows only when the last page confirmed completeness."""
        if not self.complete:
            raise self._invalid("page_limit")
        return self.items

    def diagnostics(self) -> dict[str, object]:
        """Expose counts and categorical outcomes without session identifiers."""
        return {
            "pages": self.page_count,
            "unique_rows": len(self.items),
            "duplicates": self.duplicate_count,
            "complete": self.complete,
            "outcome": self.outcome,
        }
