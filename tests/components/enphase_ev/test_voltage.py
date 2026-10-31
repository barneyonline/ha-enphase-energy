from __future__ import annotations

from custom_components.enphase_ev.voltage import (
    coerce_nominal_voltage,
    preferred_operating_voltage,
    resolve_nominal_voltage_for_hass,
    resolve_nominal_voltage_for_locale,
)


def test_resolve_nominal_voltage_for_locale_prefers_country() -> None:
    assert resolve_nominal_voltage_for_locale(country="US", language="fr-FR") == 120
    assert resolve_nominal_voltage_for_locale(country="BR") == 220
    assert resolve_nominal_voltage_for_locale(country="gb") == 230


def test_resolve_nominal_voltage_for_locale_uses_language_when_country_missing() -> (
    None
):
    assert resolve_nominal_voltage_for_locale(language="en-CA") == 120
    assert resolve_nominal_voltage_for_locale(language="pt-BR") == 220
    assert resolve_nominal_voltage_for_locale(language="de-DE") == 230


def test_resolve_nominal_voltage_for_locale_defaults_when_unknown() -> None:
    assert resolve_nominal_voltage_for_locale(country="XX", language="zz") == 230
    assert resolve_nominal_voltage_for_locale(language="en-XX") == 230


def test_locale_with_no_configured_country_voltage_uses_default(monkeypatch):
    from custom_components.enphase_ev import voltage

    monkeypatch.setitem(voltage.LANGUAGE_DEFAULT_COUNTRY, "test", "XX")
    assert resolve_nominal_voltage_for_locale(language="test") == 230


def test_resolve_nominal_voltage_for_hass_handles_unfriendly_config(hass) -> None:
    class BadConfig:
        @property
        def country(self):
            raise RuntimeError("boom")

        @property
        def language(self):
            raise RuntimeError("boom")

    hass.config = BadConfig()
    assert resolve_nominal_voltage_for_hass(hass) == 230


def test_resolve_nominal_voltage_for_locale_handles_bad_country_and_language() -> None:
    class BadStr:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    assert (
        resolve_nominal_voltage_for_locale(country=BadStr(), language=BadStr()) == 230
    )
    assert resolve_nominal_voltage_for_locale(country=" ", language=" ") == 230


def test_coerce_nominal_voltage_and_preferred_operating_voltage() -> None:
    assert coerce_nominal_voltage(" 229.6 ") == 230
    assert coerce_nominal_voltage("bad") is None
    assert coerce_nominal_voltage(-1) is None
    assert preferred_operating_voltage(["230", 230.2, None, "bad", 120]) == 230
