"""Manage config-entry ownership and legacy Repairs."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import CONF_SITE_ID, DOMAIN


def entry_issue_kwargs(entry: Any) -> dict[str, Any]:
    """Attach an issue to its config entry when one is available."""
    return {"config_entry_id": entry.entry_id} if entry is not None else {}


def legacy_issue_site_id(issue: ir.IssueEntry) -> str | None:
    """Return the site context saved on an old diagnostic issue."""
    data = issue.data
    metrics = data.get("site_metrics") if isinstance(data, dict) else None
    site_id = metrics.get("site_id") if isinstance(metrics, dict) else None
    return str(site_id) if site_id is not None else None


def async_remove_entry_issues(hass: HomeAssistant, entry: Any) -> None:
    """Remove legacy issues associated with a removed config entry.

    Home Assistant clears natively owned issues separately.
    """
    entry_id = entry.entry_id
    site_id = entry.data.get(CONF_SITE_ID)
    suffix = "".join(
        char.lower() if char.isalnum() else "_" for char in entry_id.strip()
    ).strip("_")
    for (domain, issue_id), issue in list(ir.async_get(hass).issues.items()):
        if domain != DOMAIN or issue.config_entry_id is not None:
            continue
        legacy_site_match = site_id is not None and legacy_issue_site_id(issue) == str(
            site_id
        )
        if (
            legacy_site_match
            or issue_id.endswith(f"_{entry_id}")
            or (suffix and issue_id.endswith(f"_{suffix}"))
            or (suffix and issue_id.startswith(f"active_system_event_{suffix}_"))
        ):
            ir.async_delete_issue(hass, DOMAIN, issue_id)


@contextmanager
def async_refresh_issue_set(
    hass: HomeAssistant, issue_ids: set[str], keep: set[str]
) -> Iterator[None]:
    """Reconcile an already checked set, including older unowned issues.

    The caller creates current issues inside the context. Retained checkpoints and
    incomplete observations must be in keep. Exceptions never remove stale issues.
    Explicit IDs isolate entries while also migrating pre-ownership issues.
    """
    with ir.async_refresh_issues(hass, DOMAIN, issue_ids=issue_ids) as issues:
        issues.async_keep(keep)
        yield
