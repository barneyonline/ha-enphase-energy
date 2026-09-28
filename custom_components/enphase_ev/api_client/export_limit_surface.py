"""Installer PEL forms; never replay a configuration mutation."""

from __future__ import annotations

from html.parser import HTMLParser
from typing import TYPE_CHECKING
from urllib.parse import urlencode, urljoin, urlsplit

from ..const import BASE_URL
from .errors import ActivationSessionExpired, Unauthorized

if TYPE_CHECKING:
    from ..api import EnphaseEVClient


class InvalidExportLimitForm(ValueError):
    """The response cannot safely be submitted as a PEL form."""


class _FormParser(HTMLParser):
    """Collect successful controls without flattening repeated field names."""

    def __init__(self, action: str) -> None:
        super().__init__(convert_charrefs=True)
        self.action = action
        self.matches = 0
        self.inside = False
        self.fields: list[tuple[str, str]] = []
        self.radio_fields: dict[str, tuple[str, str]] = {}
        self.textarea: str | None = None
        self.text = ""
        self.select: str | None = None
        self.multiple = False
        self.options: list[tuple[str, bool, bool]] = []
        self.option: dict[str, str | None] | None = None
        self.invalid = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if tag == "form":
            self.invalid |= self.inside
            self.inside = urljoin(BASE_URL, attr.get("action") or "") == self.action
            if self.inside:
                self.matches += 1
            return
        if not self.inside:
            return
        # Explicit form ownership can override the enclosing form. Do not guess.
        self.invalid |= "form" in attr
        if tag == "fieldset":
            # Disabled fieldset/legend exceptions are not part of the verified form.
            self.invalid |= "disabled" in attr
        disabled = "disabled" in attr
        name = attr.get("name")
        if tag == "input" and name:
            kind = (attr.get("type") or "text").lower()
            if kind == "radio" and "checked" in attr:
                # HTML radio groups select the last checked control, including
                # disabled controls (which are then omitted from submission).
                previous = self.radio_fields.pop(name, None)
                if previous is not None:
                    self.fields = [
                        field for field in self.fields if field is not previous
                    ]
            if disabled:
                return
            if kind in {"submit", "button", "reset", "image"}:
                return
            if kind in {"checkbox", "radio"} and "checked" not in attr:
                return
            if kind in {"password", "file"}:
                self.invalid = True
                return
            field = (
                name,
                (
                    (attr.get("value") or "")
                    if "value" in attr
                    else ("on" if kind in {"checkbox", "radio"} else "")
                ),
            )
            self.fields.append(field)
            if kind == "radio":
                self.radio_fields[name] = field
        elif tag == "textarea" and name and not disabled:
            self.textarea, self.text = name, ""
        elif tag == "select" and name and not disabled:
            self.select, self.multiple, self.options = name, "multiple" in attr, []
        elif tag == "option" and self.select:
            self.handle_endtag("option")
            self.option, self.text = attr, ""
        elif tag == "optgroup" and disabled:
            self.invalid = True

    def handle_data(self, data: str) -> None:
        if self.textarea is not None or self.option is not None:
            self.text += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.invalid |= self.textarea is not None or self.select is not None
            self.inside = False
        elif tag == "textarea" and self.textarea is not None:
            self.fields.append((self.textarea, self.text))
            self.textarea = None
        elif tag == "option" and self.option is not None:
            value = self.option.get("value")
            self.options.append(
                (
                    value if value is not None else self.text,
                    "selected" in self.option,
                    "disabled" in self.option,
                )
            )
            self.option = None
        elif tag == "select" and self.select is not None:
            self.handle_endtag("option")
            selected = [item for item in self.options if item[1]]
            if not self.multiple:
                selected = (
                    selected[-1:] or [item for item in self.options if not item[2]][:1]
                )
            self.fields.extend(
                (self.select, value) for value, _, disabled in selected if not disabled
            )
            self.select = None


def _required_fields(fields: list[tuple[str, str]]) -> dict[str, str]:
    """Require one unambiguous successful control for each verified PEL input."""
    required = ["authenticity_token"] + [
        f"info_pel_settings_info[{name}]"
        for name in (
            "enable_dynamic_limiting",
            "export_limit",
            "reference_value",
            "free_limit_value",
            "slew_rate",
        )
    ]
    result = {}
    for name in required:
        values = [value for key, value in fields if key == name]
        if len(values) != 1 or not values[0].strip():
            raise InvalidExportLimitForm("Export Limit form unavailable")
        result[name] = values[0]
    return result


def form_matches_configuration(
    fields: list[tuple[str, str]],
    *,
    enabled: bool,
    watts: float,
    slew: float,
    export_target: bool = True,
    reference: float = 3,
    allow_zero_slew: bool = False,
) -> bool:
    """Block a stale or unsupported live form before recording a write intent."""
    values = _required_fields(fields)
    prefix = "info_pel_settings_info"
    try:
        return (
            values[f"{prefix}[enable_dynamic_limiting]"]
            in ({"false"} if enabled else {"false", "disable_settings"})
            and values[f"{prefix}[export_limit]"]
            == ("true" if export_target else "false")
            and float(values[f"{prefix}[reference_value]"]) == reference
            and float(values[f"{prefix}[free_limit_value]"]) == watts
            and (
                float(values[f"{prefix}[slew_rate]"]) == slew
                or (allow_zero_slew and float(values[f"{prefix}[slew_rate]"]) == 0)
            )
        )
    except ValueError:
        return False


def parse_form(html: str, site_id: str) -> list[tuple[str, str]]:
    """Validate the intended same-origin form and its live authenticity token."""
    parser = _FormParser(f"{BASE_URL}/site_pel_settings/{site_id}")
    parser.feed(html)
    fields = parser.fields
    if parser.matches != 1 or parser.inside or parser.invalid:
        raise InvalidExportLimitForm("Export Limit form unavailable")
    _required_fields(fields)
    return fields


async def read_settings(client: EnphaseEVClient) -> object:
    """Read settings using the authenticated Enlighten session."""
    return await client._json(
        "POST",
        f"{BASE_URL}/service/site-device/api/v1/{client._site}/site-device-settings",
        json={},
        headers=client._system_dashboard_headers,
    )


async def read_form(client: EnphaseEVClient) -> list[tuple[str, str]]:
    """Fetch a fresh form, without following login redirects."""
    try:
        response = await client._text_response(
            "GET",
            f"{BASE_URL}/site_pel_settings/{client._site}/edit?settings_view=true",
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "X-Requested-With": None,
            },
            allow_redirects=False,
            use_cookie_header_only=True,
            mark_payload_success=False,
        )
    except Unauthorized as err:
        raise ActivationSessionExpired("PEL session rejected") from err

    if response.location and urlsplit(response.location).path.startswith("/login"):
        raise ActivationSessionExpired("PEL form redirected to login")
    if response.status != 200:
        raise InvalidExportLimitForm("Export Limit form unavailable")
    return parse_form(response.text, str(client._site))


async def write_settings(
    client: EnphaseEVClient,
    fields: list[tuple[str, str]],
    watts: int | None,
    slew_rate: float,
) -> None:
    """Submit once; only subsequent settings readback establishes success."""
    _required_fields(fields)
    overrides = {
        "commit": "Save",
        "info_pel_settings_info[enable_dynamic_limiting]": (
            "disable_settings" if watts is None else "false"
        ),
        "info_pel_settings_info[slew_rate]": str(slew_rate),
    }
    if watts is not None:
        overrides.update(
            {
                "info_pel_settings_info[export_limit]": "true",
                "info_pel_settings_info[reference_value]": "3",
                "info_pel_settings_info[free_limit_value]": str(watts),
            }
        )
    body = [(k, v) for k, v in fields if k not in overrides and k != "_method"]
    body.extend(overrides.items())
    try:
        response = await client._text_response(
            "PUT",
            f"{BASE_URL}/site_pel_settings/{client._site}",
            data=urlencode(body),
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "X-Requested-With": None,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            allow_redirects=False,
            allow_reauth=False,
            allow_replay=False,
            use_cookie_header_only=True,
            mark_payload_success=False,
        )
    except Unauthorized as err:
        raise ActivationSessionExpired("PEL session rejected") from err

    if response.location and urlsplit(response.location).path.startswith("/login"):
        raise ActivationSessionExpired("PEL form redirected to login")
