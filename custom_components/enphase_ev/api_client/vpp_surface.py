"""Read-only VPP/ELRP HTTP surface for the Enphase cloud client."""

from __future__ import annotations

import re

from yarl import URL

from .protocols import VppClient

_HEX24_RE = re.compile(r"^[0-9a-fA-F]{24}$")


def valid_object_id(value: object) -> str | None:
    """Return a validated Enphase object id."""

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if _HEX24_RE.fullmatch(candidate) else None


async def enrollment_id(client: VppClient, *, gs_base_url: str) -> object:
    """Return the enrollment lookup wrapper for the client's site."""

    url = f"{gs_base_url}/enrollment-mgr/api/v1/enrollment/enrolled/{client._site}"
    return await client._json(
        "GET",
        url,
        headers=client._vpp_headers,
        log_invalid_payload=False,
        use_cookie_header_only=True,
    )


async def enrollment_details(
    client: VppClient,
    enrollment_id: str,
    *,
    gs_base_url: str,
) -> object:
    """Return enrollment details for a validated enrollment id."""

    validated = valid_object_id(enrollment_id)
    if validated is None:
        raise ValueError("Invalid VPP enrollment identifier")
    url = f"{gs_base_url}/enrollment-mgr/api/v1/enrollment/{validated}"
    return await client._json(
        "GET",
        url,
        headers=client._vpp_headers,
        log_invalid_payload=False,
        redaction_identifiers=(validated,),
        use_cookie_header_only=True,
    )


async def events(client: VppClient, program_id: str, *, gs_base_url: str) -> object:
    """Return the default upcoming-and-recent VPP event wrapper."""

    validated = valid_object_id(program_id)
    if validated is None:
        raise ValueError("Invalid VPP program identifier")
    url = str(
        URL(f"{gs_base_url}/vpp-mgr/api/v1/events/get").with_query(
            {
                "site_id": str(client._site),
                "programId": validated,
                "start_date": "",
                "end_date": "",
                "sort_by": "",
                "ascending": "",
                "time": "",
            }
        )
    )
    return await client._json(
        "GET",
        url,
        headers=client._vpp_headers,
        log_invalid_payload=False,
        redaction_identifiers=(validated,),
        use_cookie_header_only=True,
    )
