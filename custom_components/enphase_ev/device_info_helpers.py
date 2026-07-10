from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN
from .runtime_helpers import coerce_optional_text as _clean_text


def _is_redundant_model_id(model: object, model_id: object) -> bool:
    """Return True when model_id does not add useful information to model."""
    model_text = _clean_text(model)
    model_id_text = _clean_text(model_id)
    if not model_text or not model_id_text:
        return False
    model_cf = model_text.casefold()
    model_id_cf = model_id_text.casefold()
    if model_cf == model_id_cf:
        return True
    if model_id_cf in model_cf:
        return True

    for match in re.finditer(r"\(([^()]+)\)", model_text):
        snippet = match.group(1).strip()
        # Only treat substantial snippets as model identifiers.
        if len(snippet) < 6:
            continue
        if model_id_cf.startswith(snippet.casefold()):
            return True
    return False


def _normalize_evse_model_name(value: object) -> str | None:
    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:
        return None
    if not text:
        return None

    text_upper = text.upper()
    if not text_upper.startswith("IQ-EVSE-"):
        return text

    parts = text_upper.split("-")
    if (
        len(parts) >= 5
        and parts[0] == "IQ"
        and parts[1] == "EVSE"
        and parts[2]
        and parts[3].isdigit()
        and len(parts[3]) == 4
    ):
        return "-".join(parts[:4])

    return text


def _normalize_evse_display_name(value: object) -> str | None:
    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:
        return None
    if not text:
        return None

    if re.match(r"(?i)^q\s+ev charger\b", text):
        text = re.sub(r"(?i)^q(\s+ev charger\b)", r"IQ\1", text, count=1)

    compact = re.match(
        r"^(?P<prefix>.*)\((?P<first>[^()]+)\)\s+\((?P<second>[^()]+)\)\s*$", text
    )
    if compact:
        first = compact.group("first").strip()
        second = compact.group("second").strip()
        if first and second and second.upper().startswith(first.upper()):
            text = f"{compact.group('prefix').strip()} ({first})"

    return text.strip() or None


def _compose_charger_model_display(
    display_name: str | None,
    model_name: object,
    fallback_name: str | None = None,
) -> str | None:
    display = _normalize_evse_display_name(display_name)
    fallback = _normalize_evse_display_name(fallback_name)
    model = _normalize_evse_model_name(model_name)
    if display and model:
        if model.casefold() in display.casefold():
            return display
        return f"{display} ({model})"
    if display:
        return display
    if model:
        return model
    return fallback


@lru_cache(maxsize=1)
def _integration_version() -> str | None:
    manifest_path = Path(__file__).with_name("manifest.json")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    version = payload.get("version")
    if not isinstance(version, str):
        return None
    cleaned = version.strip()
    return cleaned or None


async def async_prime_integration_version(hass: HomeAssistant) -> None:
    """Prime cached integration version off the event loop."""
    await hass.async_add_executor_job(_integration_version)


def _cloud_device_info(site_id: object) -> DeviceInfo | dict[str, object]:
    """Return DeviceInfo for cloud-level connectivity entities."""
    try:
        site_text = str(site_id).strip()
    except Exception:
        site_text = ""
    if not site_text:
        site_text = "unknown"
    payload: dict[str, object] = {
        "identifiers": {(DOMAIN, f"type:{site_text}:cloud")},
        "manufacturer": "Enphase",
        "name": "Enphase Cloud",
        "model": "Cloud Service",
    }
    version = _integration_version()
    if version:
        payload["sw_version"] = version
    try:
        from homeassistant.helpers.entity import DeviceInfo
    except Exception:
        return payload
    return DeviceInfo(**payload)
