"""Bridge entry-owned Repairs to the advertised Home Assistant 2026.10 minimum."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import CONF_SITE_ID, DOMAIN


def supports_entry_issues() -> bool:
    """Ownership and refresh APIs are available together from Core 2026.11."""
    return hasattr(ir, "async_refresh_issues")


def entry_issue_kwargs(entry: Any) -> dict[str, Any]:
    """Pass ownership only on Core versions which accept it."""
    if entry is not None and supports_entry_issues():
        return {"config_entry_id": entry.entry_id}
    return {}


def async_remove_entry_issues(hass: HomeAssistant, entry: Any) -> None:
    """Remove entry-scoped legacy issues as well as native owned issues.

    Keep this cleanup while 2026.10 is supported, and for issues created before
    ownership was available. Unloading is deliberately not a removal.
    """
    entry_id = entry.entry_id
    site_id = entry.data.get(CONF_SITE_ID)
    suffix = "".join(
        char.lower() if char.isalnum() else "_" for char in entry_id.strip()
    ).strip("_")
    for (domain, issue_id), issue in list(ir.async_get(hass).issues.items()):
        owner = getattr(issue, "config_entry_id", None)
        if owner is not None and owner != entry_id:
            continue
        data = issue.data
        metrics = data.get("site_metrics") if isinstance(data, dict) else None
        legacy_site_match = (
            site_id is not None
            and isinstance(metrics, dict)
            and str(metrics.get("site_id")) == str(site_id)
        )
        if domain == DOMAIN and (
            owner == entry_id
            or legacy_site_match
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
    refresh = getattr(ir, "async_refresh_issues", None)
    if refresh is not None:
        with refresh(hass, DOMAIN, issue_ids=issue_ids) as issues:
            issues.async_keep(keep)
            yield
    else:
        yield
        for issue_id in issue_ids - keep:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
