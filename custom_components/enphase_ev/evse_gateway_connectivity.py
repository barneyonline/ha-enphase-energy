"""Conservative, identifier-free interpretation of cached charger telemetry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from homeassistant.util import dt as dt_util


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    parsed = dt_util.parse_datetime(value.replace("[UTC]", ""))
    return dt_util.as_utc(parsed) if parsed is not None and parsed.tzinfo else None


def _code(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        return int(value)
    return None


@dataclass(frozen=True)
class GatewayConnectivity:
    """One cloud observation, not a direct measurement of the local link."""

    connected: bool | None
    gateway_count: int | None
    connected_count: int | None
    last_connection: datetime | None
    received_at: datetime | None
    reported_at: datetime | None


def gateway_connectivity(
    item: dict[str, object], received_at: object
) -> GatewayConnectivity:
    """Use the explicit local flag; never decode undocumented failure codes."""
    received = received_at if isinstance(received_at, datetime) else None
    if received is not None and received.tzinfo is None:
        received = None
    reported = _timestamp(item.get("lastReportedAt"))
    local = item.get("isLocallyConnected")
    if isinstance(local, str):
        local = {"true": True, "false": False, "1": True, "0": False}.get(
            local.strip().lower()
        )
    elif type(local) is int and local in (0, 1):
        local = bool(local)
    if not isinstance(local, bool):
        local = None

    gateways = item.get("gatewayConnectivityDetails")
    count = connected_count = None
    last_connection = None
    state = local
    if isinstance(gateways, list):
        count = len(gateways)
        codes = [
            _code(gateway.get("gwConnStatus")) if isinstance(gateway, dict) else None
            for gateway in gateways
        ]
        connected_count = codes.count(0)
        # Only status 0 has an observed connected meaning. Unknown codes cannot
        # establish disconnection or override conflicting evidence.
        if not codes or None in codes:
            state = None
        elif local is False:
            state = False if connected_count == 0 else None
        elif connected_count == count:
            state = True
        else:
            state = None
        for gateway in gateways:
            if not isinstance(gateway, dict):
                continue
            stamp = _code(gateway.get("lastConnTime"))
            if stamp is None:
                continue
            try:
                # summary_v2 documents this field in epoch milliseconds.
                connection = dt_util.utc_from_timestamp(stamp / 1000)
            except (OverflowError, OSError, ValueError):
                continue
            if stamp > 0 and (received is None or connection <= received):
                last_connection = max(last_connection or connection, connection)
    elif gateways is not None:
        state = None
    return GatewayConnectivity(
        state, count, connected_count, last_connection, received, reported
    )
