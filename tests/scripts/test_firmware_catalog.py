from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys

import pytest


@pytest.fixture(scope="module")
def firmware_catalog_module():
    script_path = (
        Path(__file__).resolve().parents[2] / "scripts" / "firmware_catalog.py"
    )
    spec = importlib.util.spec_from_file_location("firmware_catalog", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["firmware_catalog"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fixture_dir() -> Path:
    return Path(__file__).parent / "fixtures"


def test_discover_apps_entrypoint(firmware_catalog_module, fixture_dir: Path) -> None:
    html = (fixture_dir / "enphase_root.html").read_text(encoding="utf-8")
    apps_path, product_type = firmware_catalog_module.discover_apps_entrypoint(html)
    assert apps_path == "/installers/resources/documentation/apps"
    assert product_type == "216"


def test_parse_facets_and_languages(firmware_catalog_module, fixture_dir: Path) -> None:
    apps_html = (fixture_dir / "enphase_apps_facets.html").read_text(encoding="utf-8")

    assert firmware_catalog_module.parse_product_type_from_apps_page(apps_html) == "216"

    product_facets = firmware_catalog_module.parse_facet_values(
        apps_html, "product_media_name"
    )
    assert product_facets["IQ Gateway software"] == 5002
    assert product_facets["IQ Microinverter software"] == 7738

    topic_facets = firmware_catalog_module.parse_facet_values(apps_html, "document")
    assert topic_facets["Release notes"] == 217

    search_locales = firmware_catalog_module.parse_language_options(
        apps_html, "search_api_language"
    )
    assert search_locales["en"] == "United States (EN)"
    assert search_locales["en-au"] == "Australia (EN)"

    alt_locales = firmware_catalog_module.parse_language_options(
        apps_html, "field_alternative_language"
    )
    assert alt_locales["ja-jp"] == "Japan (JP)"


def test_parse_release_cards_and_pagination(
    firmware_catalog_module, fixture_dir: Path, monkeypatch
) -> None:
    page0 = (fixture_dir / "enphase_release_page_0.html").read_text(encoding="utf-8")
    page1 = (fixture_dir / "enphase_release_page_1.html").read_text(encoding="utf-8")

    cards = firmware_catalog_module.parse_release_cards(page0)
    assert len(cards) == 2
    assert cards[0].version == "8.2.4401"
    assert cards[0].release_date == "2025-11-20"
    assert cards[0].media_id == "22469"
    assert cards[0].countries_text == "USA"

    next_url = firmware_catalog_module.find_next_page_url(
        "https://enphase.com/installers/resources/documentation/apps?product_type=216",
        page0,
    )
    assert next_url is not None and "page=1" in next_url

    fixtures_by_page = {"0": page0, "1": page1}

    def _fake_fetch(url: str, *, timeout: int = 30) -> str:  # noqa: ARG001
        if "page=1" in url:
            return fixtures_by_page["1"]
        return fixtures_by_page["0"]

    monkeypatch.setattr(firmware_catalog_module, "fetch_text", _fake_fetch)

    crawled_cards, visited_pages = firmware_catalog_module.crawl_release_cards(
        apps_url="https://enphase.com/installers/resources/documentation/apps",
        product_type="216",
        topic_id=217,
        product_media_name_id=5002,
        search_locale="en",
        timeout=5,
        max_pages=5,
    )
    assert len(crawled_cards) == 3
    assert len(visited_pages) == 2
    assert any("page=1" in url for url in visited_pages)


def test_country_applicability_parsing(firmware_catalog_module) -> None:
    alias_map = {
        "usa": "US",
        "unitedstates": "US",
        "puertorico": "PR",
        "canada": "CA",
        "ireland": "IE",
        "italy": "IT",
        "unitedkingdom": "GB",
        "brazil": "BR",
        "france": "FR",
        "germany": "DE",
    }

    explicit = firmware_catalog_module.parse_country_applicability(
        "United States, Puerto Rico, Canada, Brazil", alias_map=alias_map
    )
    assert explicit.include == {"US", "PR", "CA", "BR"}
    assert explicit.ambiguous is False
    assert firmware_catalog_module.country_applicability_matches(explicit, "US")
    assert not firmware_catalog_module.country_applicability_matches(explicit, "AU")
    assert not firmware_catalog_module.country_applicability_is_global_fallback(
        explicit
    )

    all_except = firmware_catalog_module.parse_country_applicability(
        "All countries except Ireland, Italy, and the United Kingdom",
        alias_map=alias_map,
    )
    assert all_except.all_countries is True
    assert all_except.exclude == {"IE", "IT", "GB"}
    assert all_except.ambiguous is False
    assert firmware_catalog_module.country_applicability_matches(all_except, "AU")
    assert not firmware_catalog_module.country_applicability_matches(all_except, "IE")
    assert not firmware_catalog_module.country_applicability_is_global_fallback(
        all_except
    )

    european = firmware_catalog_module.parse_country_applicability(
        "All European countries except Ireland", alias_map=alias_map
    )
    assert european.ambiguous is False
    assert "IE" not in european.include
    assert {"DE", "FR", "GB"}.issubset(european.include)
    assert firmware_catalog_module.country_applicability_matches(european, "DE")
    assert firmware_catalog_module.country_applicability_matches(european, "FR")
    assert not firmware_catalog_module.country_applicability_matches(european, "AU")
    assert not firmware_catalog_module.country_applicability_matches(european, "IE")

    unknown_region = firmware_catalog_module.parse_country_applicability(
        "All Caribbean countries except Puerto Rico", alias_map=alias_map
    )
    assert unknown_region.ambiguous is True
    assert unknown_region.exclude == {"PR"}
    assert not firmware_catalog_module.country_applicability_matches(
        unknown_region, "US"
    )

    worldwide = firmware_catalog_module.parse_country_applicability(
        "Worldwide", alias_map=alias_map
    )
    assert firmware_catalog_module.country_applicability_is_global_fallback(worldwide)
    assert firmware_catalog_module.country_applicability_matches(worldwide, None)


def test_pick_latest_release_and_urls(firmware_catalog_module) -> None:
    card_old = firmware_catalog_module.ReleaseCard(
        title="IQ Gateway software release notes (8.2.4300)",
        version="8.2.4300",
        release_date="2025-09-01",
        media_id="22000",
        langcode="en",
        summary="Old",
        countries_text="USA",
    )
    card_new = firmware_catalog_module.ReleaseCard(
        title="IQ Gateway software release notes (8.2.4401)",
        version="8.2.4401",
        release_date="2025-11-20",
        media_id="22469",
        langcode="und",
        summary="New",
        countries_text="USA",
    )
    card_recent_global = firmware_catalog_module.ReleaseCard(
        title="IQ Gateway software release notes (8.2.4398)",
        version="8.2.4398",
        release_date="2025-10-20",
        media_id="global-8-2",
        langcode="und",
        summary="Countries: Worldwide",
        countries_text="Worldwide",
    )
    card_stale_global = firmware_catalog_module.ReleaseCard(
        title="IQ Gateway software release notes (7.0.86)",
        version="7.0.86",
        release_date="2021-12-21",
        media_id="global-7-0",
        langcode="und",
        summary="Countries: Worldwide",
        countries_text="Worldwide",
    )

    latest = firmware_catalog_module.pick_latest_release([card_old, card_new])
    assert latest == card_new
    assert firmware_catalog_module._version_family("8.2.4401") == (8, 2)
    assert firmware_catalog_module._version_family("7") == (7,)
    assert firmware_catalog_module._version_family("release") is None
    assert firmware_catalog_module._version_family(None) is None
    assert (
        firmware_catalog_module.pick_latest_release_for_country(
            [card_old, card_new], "US", alias_map={"usa": "US"}
        )
        == card_new
    )
    assert (
        firmware_catalog_module.pick_latest_release_for_country(
            [card_old, card_new], "AU", alias_map={"usa": "US"}
        )
        is None
    )
    assert (
        firmware_catalog_module.pick_latest_global_release(
            [card_old, card_new], alias_map={"usa": "US"}
        )
        is None
    )
    assert firmware_catalog_module.release_applies_to_country(
        card_new, "US", alias_map={"usa": "US"}
    )
    assert (
        firmware_catalog_module.pick_latest_release_for_country(
            [card_stale_global, card_new],
            "AU",
            alias_map={"usa": "US"},
            latest_known=card_new,
        )
        is None
    )
    assert (
        firmware_catalog_module.pick_latest_release_for_country(
            [card_recent_global, card_new],
            "AU",
            alias_map={"usa": "US"},
            latest_known=card_new,
        )
        == card_recent_global
    )
    assert (
        firmware_catalog_module.pick_latest_global_release(
            [card_stale_global, card_new],
            alias_map={"usa": "US"},
            latest_known=card_new,
        )
        is None
    )
    assert (
        firmware_catalog_module.pick_latest_global_release(
            [card_recent_global, card_new],
            alias_map={"usa": "US"},
            latest_known=card_new,
        )
        == card_recent_global
    )
    assert not firmware_catalog_module._is_stale_global_fallback(
        card_old, card_new, alias_map={"usa": "US"}
    )

    urls = firmware_catalog_module.build_release_urls_by_locale(
        locales=["en", "fr-fr", "en-au"],
        apps_url="https://enphase.com/en-au/installers/resources/documentation/apps",
        product_type="216",
        topic_id=217,
        product_media_name_id=5002,
    )
    assert urls["en"].startswith(
        "https://enphase.com/en-au/installers/resources/documentation/apps?"
    )
    assert urls["fr-fr"].startswith(
        "https://enphase.com/en-au/installers/resources/documentation/apps?"
    )
    assert "product_type=216" in urls["en-au"]
    assert "f%5B0%5D=document%3A217" in urls["en-au"]
    assert "f%5B1%5D=product_media_name%3A5002" in urls["en-au"]
    assert "search_api_language=en-au" in urls["en-au"]
    assert "field_alternative_language=en-au" in urls["en-au"]
    urls_without_product_filter = firmware_catalog_module.build_release_urls_by_locale(
        locales=["en-au"],
        apps_url=(
            "https://enphase.com/en-au/installers/resources/documentation/"
            "communication"
        ),
        product_type="216",
        topic_id=217,
        product_media_name_id=None,
    )
    assert "f%5B0%5D=document%3A217" in urls_without_product_filter["en-au"]
    assert "f%5B1%5D" not in urls_without_product_filter["en-au"]


def test_catalogs_equal_ignoring_generated_at(firmware_catalog_module) -> None:
    current = {
        "schema_version": 1,
        "generated_at": "2026-03-01T00:00:00Z",
        "devices": {"envoy": {"latest_global": {"version": "8.2.4401"}}},
    }
    previous_same = {
        "schema_version": 1,
        "generated_at": "2026-02-28T00:00:00Z",
        "devices": {"envoy": {"latest_global": {"version": "8.2.4401"}}},
    }
    previous_changed = {
        "schema_version": 1,
        "generated_at": "2026-02-28T00:00:00Z",
        "devices": {"envoy": {"latest_global": {"version": "8.2.4300"}}},
    }

    assert (
        firmware_catalog_module.catalogs_equal_ignoring_generated_at(
            current, previous_same
        )
        is True
    )
    assert (
        firmware_catalog_module.catalogs_equal_ignoring_generated_at(
            current, previous_changed
        )
        is False
    )
    assert (
        firmware_catalog_module.catalogs_equal_ignoring_generated_at(current, None)
        is False
    )


def test_choose_generated_at_reuses_previous_when_catalog_unchanged(
    firmware_catalog_module,
) -> None:
    current = {
        "schema_version": 1,
        "generated_at": "2026-03-01T00:00:00Z",
        "devices": {"envoy": {"latest_global": {"version": "8.2.4401"}}},
    }
    previous = {
        "schema_version": 1,
        "generated_at": "2026-02-28T12:00:00Z",
        "devices": {"envoy": {"latest_global": {"version": "8.2.4401"}}},
    }

    reused = firmware_catalog_module.choose_generated_at(
        current_catalog=current,
        previous_catalog=previous,
        fallback_generated_at="2026-03-01T00:00:00Z",
    )
    assert reused == "2026-02-28T12:00:00Z"

    changed = firmware_catalog_module.choose_generated_at(
        current_catalog=current,
        previous_catalog={
            "schema_version": 1,
            "generated_at": "2026-02-28T12:00:00Z",
            "devices": {"envoy": {"latest_global": {"version": "8.2.4300"}}},
        },
        fallback_generated_at="2026-03-01T00:00:00Z",
    )
    assert changed == "2026-03-01T00:00:00Z"


def test_helper_edge_branches(
    firmware_catalog_module, tmp_path: Path, monkeypatch
) -> None:
    assert firmware_catalog_module._now_utc_iso().endswith("Z")
    assert firmware_catalog_module._parse_date_to_iso("") is None
    assert firmware_catalog_module._parse_date_to_iso("not-a-date") is None
    assert firmware_catalog_module._version_sort_key(None) == ()
    assert firmware_catalog_module._version_sort_key("1..A") == (1, "a")
    assert firmware_catalog_module._version_sort_key(".1-") == (1,)
    assert firmware_catalog_module._normalize_locale(None) == "en"
    assert firmware_catalog_module._normalize_locale("   ") == "en"
    assert firmware_catalog_module._with_query(
        "https://example.com/path?x=1", {"y": "2"}
    ).startswith("https://example.com/path?")

    parser = firmware_catalog_module.ReleaseCardParser()
    assert parser._extract_country_text("") is None
    assert parser._extract_country_text("Countries:") is None
    assert (
        parser._extract_country_text("Countries : United States, Canada")
        == "United States, Canada"
    )
    assert (
        parser._extract_country_text(
            "Countries: United States. Platforms supported: IQ8"
        )
        == "United States"
    )
    assert (
        parser._extract_country_text(
            "Countries: United States, Canada. Feature classification Major"
        )
        == "United States, Canada"
    )
    assert (
        parser._extract_country_text(
            "Supported regions : United States. # Feature classification Feature"
        )
        == "United States"
    )
    assert (
        parser._extract_country_text(
            "Supported countries: The United States of America, Puerto Rico. "
            "Gateways supported: IQ Gateway"
        )
        == "The United States of America, Puerto Rico"
    )
    assert parser._extract_country_text("No labels here") is None
    parser._flush_card()
    assert parser.cards == []

    parser._title_parts = ["IQ Gateway software release notes (1.0.0)"]
    parser._note_parts = [
        f"{'x' * 600} Countries: United States, Canada. Feature classification Major"
    ]
    parser._date_parts = ["November 20, 2025"]
    parser._flush_card()
    assert parser.cards
    assert parser.cards[0].summary.endswith("...")
    assert parser.cards[0].countries_text == "United States, Canada"

    assert firmware_catalog_module.discover_apps_entrypoint("<html/>") == (
        "/installers/resources/documentation/apps",
        "216",
    )
    apps_path, product_type = firmware_catalog_module.discover_apps_entrypoint("""
        <a href="/installers/resources/documentation/apps?product_type=111" aria-label="Data sheets"></a>
        <a href="/installers/resources/documentation/apps?product_type=216" aria-label="Apps and software"></a>
        """)
    assert apps_path == "/installers/resources/documentation/apps"
    assert product_type == "216"
    assert (
        firmware_catalog_module.parse_product_type_from_apps_page(
            "<a href='?product_type=777'>x</a>"
        )
        == "777"
    )
    assert firmware_catalog_module.parse_product_type_from_apps_page("<html/>") is None
    assert firmware_catalog_module.parse_facet_values("<html/>", "document") == {}
    facet_with_blank = """
    <ul data-drupal-facet-alias="document">
      <li><a data-drupal-facet-item-value="217"><span class="facet-item__value"> </span></a></li>
      <li><a data-drupal-facet-item-value="218"><span class="facet-item__value">Release notes</span></a></li>
    </ul>
    """
    assert firmware_catalog_module.parse_facet_values(facet_with_blank, "document") == {
        "Release notes": 218
    }
    assert (
        firmware_catalog_module.parse_language_options("<html/>", "search_api_language")
        == {}
    )
    assert (
        firmware_catalog_module.find_next_page_url("https://example.com", "<html/>")
        is None
    )

    assert firmware_catalog_module._country_label_to_names("") == []
    assert firmware_catalog_module._country_label_to_names("Latin America (EN)") == []
    assert firmware_catalog_module._country_label_to_names(
        "Germany and Austria (DE)"
    ) == [
        "Germany",
        "Austria",
    ]
    assert firmware_catalog_module._token_to_iso("   ") is None
    assert firmware_catalog_module._token_to_iso("au") == "AU"

    residential_gateway = firmware_catalog_module.ReleaseCard(
        title="IQ Gateway software release notes (8.3.5421)",
        version="8.3.5421",
        release_date="2026-05-29",
        media_id="residential-gateway",
        langcode="en",
        summary=(
            "The IQ Gateway software version 8.3.5421 introduces support for "
            "IQ9N Microinverters (residential) in North America."
        ),
        countries_text="United States",
    )
    commercial_gateway = firmware_catalog_module.ReleaseCard(
        title="IQ Gateway software release notes (9.1.1188)",
        version="9.1.1188",
        release_date="2026-06-10",
        media_id="commercial-gateway",
        langcode="en",
        summary=(
            "IQ Gateway software version 9.1.1188 addresses stability issues "
            "for grid-tied systems with IQ9N Microinverters (277 V) and "
            "IQ Gateway Commercial Pro. Supported regions: United States."
        ),
        countries_text="United States",
    )
    assert (
        firmware_catalog_module.release_applies_to_device(residential_gateway, "envoy")
        is True
    )
    assert (
        firmware_catalog_module.release_applies_to_device(commercial_gateway, "envoy")
        is False
    )
    assert (
        firmware_catalog_module.release_applies_to_device(commercial_gateway, "iqevse")
        is True
    )

    mapping = firmware_catalog_module.build_region_country_mapping(
        {
            "en": "United States (EN)",
            "de-de": "Germany and Austria (DE)",
            "en-lac": "Latin America (EN)",
            "fr-ft": "French Territories",
            "en-au": "Unknown Region",
        }
    )
    assert mapping["United States (EN)"]["type"] == "country_variant"
    assert mapping["Germany and Austria (DE)"]["type"] == "multi_country_region"
    assert mapping["Latin America (EN)"]["type"] == "aggregate_region"
    assert mapping["French Territories"]["type"] == "aggregate_region"
    assert mapping["Unknown Region"]["iso_codes"] == ["AU"]

    alias_map = firmware_catalog_module.build_country_alias_map(mapping)
    assert alias_map["germany"] == "DE"
    assert alias_map["austria"] == "AT"
    assert (
        firmware_catalog_module.build_country_alias_map(
            {"A and B": {"iso_codes": ["AA"], "locale": "aa-aa"}}
        )["a"]
        == "AA"
    )

    applicability_alias_map = {"unitedstates": "US", "canada": "CA"}
    assert firmware_catalog_module.parse_country_applicability(
        None, alias_map=applicability_alias_map
    ).ambiguous
    assert firmware_catalog_module.parse_country_applicability(
        "   ", alias_map=applicability_alias_map
    ).ambiguous
    assert firmware_catalog_module.parse_country_applicability(
        "United States...", alias_map=applicability_alias_map
    ).ambiguous
    assert firmware_catalog_module.parse_country_applicability(
        "Worldwide", alias_map=applicability_alias_map
    ).all_countries
    assert firmware_catalog_module.parse_country_applicability(
        "All countries except Atlantis", alias_map=applicability_alias_map
    ).ambiguous
    assert firmware_catalog_module.parse_country_applicability(
        "United States, Atlantis", alias_map=applicability_alias_map
    ).ambiguous
    assert firmware_catalog_module.parse_country_applicability(
        "Atlantis", alias_map=applicability_alias_map
    ).ambiguous

    no_date = firmware_catalog_module.ReleaseCard(
        title="A",
        version=None,
        release_date=None,
        media_id=None,
        langcode="en",
        summary="a",
        countries_text=None,
    )
    assert firmware_catalog_module.pick_latest_release([]) is None
    assert firmware_catalog_module.pick_latest_release([no_date]) == no_date

    entry = firmware_catalog_module.card_to_runtime_entry(
        card=no_date,
        product_type="216",
        topic_id=217,
        product_media_name_id=5002,
        locales=["en"],
        apps_url="https://enphase.com/installers/resources/documentation/apps",
    )
    assert entry["urls_by_locale"] == {}

    normalized_routes = firmware_catalog_module.build_region_site_routes(
        [
            {
                "label": "Vietnam",
                "country_code": "VN",
                "locale": "vi-vn",
                "site_url": "/vi-vn/",
            },
            {
                "label": "Australia",
                "country_code": "AU",
                "locale": "en-au",
                "site_url": "https://enphase.com/en-au/",
            },
            {
                "label": "New Zealand",
                "country_code": "NZ",
                "locale": "en-au",
                "site_url": "https://enphase.com/en-au/",
            },
        ]
    )
    assert normalized_routes[0]["site_url"] == "https://enphase.com/vi-vn/"
    crawl_targets = firmware_catalog_module.build_crawl_targets(normalized_routes)
    assert len(crawl_targets) == 2
    au_target = next(
        target
        for target in crawl_targets
        if target["site_url"] == "https://enphase.com/en-au/"
    )
    assert au_target["countries"] == ["AU", "NZ"]
    assert au_target["locales"] == ["en-au"]

    output_json = tmp_path / "x" / "y.json"
    firmware_catalog_module.write_json(output_json, {"ok": True})
    assert json.loads(output_json.read_text(encoding="utf-8")) == {"ok": True}

    monkeypatch.setattr(
        firmware_catalog_module, "fetch_text", lambda _url, timeout=30: '{"a":1}'
    )
    assert firmware_catalog_module.fetch_json("https://example.com") == {"a": 1}

    monkeypatch.setattr(
        firmware_catalog_module,
        "fetch_json",
        lambda _url, timeout=30: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert firmware_catalog_module.fetch_previous_runtime_catalog() is None
    monkeypatch.setattr(
        firmware_catalog_module, "fetch_json", lambda _url, timeout=30: [1, 2, 3]
    )
    assert firmware_catalog_module.fetch_previous_runtime_catalog() is None
    monkeypatch.setattr(
        firmware_catalog_module,
        "fetch_json",
        lambda _url, timeout=30: {"schema_version": 1},
    )
    assert firmware_catalog_module.fetch_previous_runtime_catalog() == {
        "schema_version": 1
    }
    assert firmware_catalog_module._previous_catalog_device(None, "envoy") is None
    assert (
        firmware_catalog_module._previous_catalog_source_device(
            {"source": {"type": "x"}}, "envoy"
        )
        is None
    )

    assert (
        firmware_catalog_module.choose_generated_at(
            current_catalog={"schema_version": 1, "generated_at": "new", "devices": {}},
            previous_catalog={
                "schema_version": 1,
                "generated_at": "   ",
                "devices": {},
            },
            fallback_generated_at="fallback",
        )
        == "fallback"
    )


def test_fetch_text_handles_charset_and_defaults(
    firmware_catalog_module, monkeypatch
) -> None:
    class _Resp:
        def __init__(self, content_type: str, body: bytes):
            self.headers = {"content-type": content_type}
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return self._body

    responses = iter(
        [
            _Resp("text/html; charset=iso-8859-1", b"caf\xe9"),
            _Resp("text/html", b"hello"),
        ]
    )
    monkeypatch.setattr(
        firmware_catalog_module, "urlopen", lambda _request, timeout=30: next(responses)
    )

    assert firmware_catalog_module.fetch_text("https://example.com") == "caf\xe9"
    assert firmware_catalog_module.fetch_text("https://example.com") == "hello"


def test_build_catalog_success_and_error_paths(
    firmware_catalog_module, fixture_dir: Path, monkeypatch, tmp_path: Path
) -> None:
    root_html = (fixture_dir / "enphase_root.html").read_text(encoding="utf-8")
    apps_html = (fixture_dir / "enphase_apps_facets.html").read_text(encoding="utf-8")

    def _fake_fetch_text(url: str, *, timeout: int = 30) -> str:  # noqa: ARG001
        if url.endswith("/installers/resources/documentation"):
            return root_html
        return apps_html

    gateway_cards = [
        firmware_catalog_module.ReleaseCard(
            title="IQ Gateway software release notes (8.2.4401)",
            version="8.2.4401",
            release_date="2025-11-20",
            media_id="22469",
            langcode="und",
            summary="Gateway release",
            countries_text=None,
        ),
        firmware_catalog_module.ReleaseCard(
            title="IQ Gateway software release notes (8.2.4300)",
            version="8.2.4300",
            release_date="2025-10-20",
            media_id="22000",
            langcode="en",
            summary="Countries: All countries except Ireland",
            countries_text="All countries except Ireland",
        ),
    ]
    charger_cards = [
        firmware_catalog_module.ReleaseCard(
            title="IQ EV Charger software release notes (25.37.1.14)",
            version="25.37.1.14",
            release_date="2025-11-22",
            media_id="33002",
            langcode="und",
            summary="Charger release",
            countries_text=None,
        )
    ]

    def _fake_crawl_release_cards(**kwargs):
        if kwargs["product_media_name_id"] == 5002:
            return gateway_cards, ["https://example.com/p0", "https://example.com/p1"]
        if kwargs["product_media_name_id"] == 6001:
            return charger_cards, ["https://example.com/p0"]
        return [], ["https://example.com/p0"]

    monkeypatch.setattr(
        firmware_catalog_module, "_now_utc_iso", lambda: "2026-03-01T00:00:00Z"
    )
    monkeypatch.setattr(
        firmware_catalog_module,
        "REGION_SITE_ROUTE_ROWS",
        [
            {
                "label": "United States",
                "country_code": "US",
                "locale": "en",
                "site_url": "https://enphase.com/",
            },
            {
                "label": "Puerto Rico",
                "country_code": "PR",
                "locale": "es-pr",
                "site_url": "https://enphase.com/",
            },
            {
                "label": "Australia",
                "country_code": "AU",
                "locale": "en-au",
                "site_url": "https://enphase.com/en-au/",
                "query_locale": "en-au",
            },
        ],
    )
    monkeypatch.setattr(firmware_catalog_module, "fetch_text", _fake_fetch_text)
    original_parse_facet_values = firmware_catalog_module.parse_facet_values
    monkeypatch.setattr(
        firmware_catalog_module,
        "parse_facet_values",
        lambda html, alias: (
            {
                **original_parse_facet_values(html, alias),
                "IQ EV Charger software": 6001,
            }
            if alias == "product_media_name"
            else original_parse_facet_values(html, alias)
        ),
    )
    monkeypatch.setattr(
        firmware_catalog_module, "crawl_release_cards", _fake_crawl_release_cards
    )
    call_log: list[tuple[int, str, str]] = []

    def _logged_crawl_release_cards(**kwargs):
        call_log.append(
            (
                kwargs["product_media_name_id"],
                kwargs["search_locale"],
                kwargs["apps_url"],
            )
        )
        return _fake_crawl_release_cards(**kwargs)

    monkeypatch.setattr(
        firmware_catalog_module,
        "crawl_release_cards",
        _logged_crawl_release_cards,
    )
    monkeypatch.setattr(
        firmware_catalog_module,
        "fetch_previous_runtime_catalog",
        lambda timeout=30: {
            "schema_version": 1,
            "generated_at": "2026-02-28T00:00:00Z",
            "source": {
                "type": "enphase_documentation_center",
                "entrypoint": "https://enphase.com/installers/resources/documentation",
                "apps_url": "https://enphase.com/installers/resources/documentation/apps",
                "product_type": 216,
                "crawl": {
                    "envoy": {
                        "pages": ["https://example.com/p0", "https://example.com/p1"],
                        "count": 2,
                    },
                    "iqevse": {"pages": ["https://example.com/p0"], "count": 1},
                },
            },
            "devices": {
                "envoy": {
                    "product_media_name_id": 5002,
                    "document_topic_id": 217,
                    "latest_by_country": {},
                    "latest_global": None,
                },
                "iqevse": {
                    "product_media_name_id": 6001,
                    "document_topic_id": 217,
                    "latest_by_country": {},
                    "latest_global": None,
                },
            },
        },
    )

    firmware_catalog_module.build_catalog(tmp_path, timeout=5, max_pages=2)
    runtime_path = tmp_path / "catalog" / "v1" / "runtime_catalog.json"
    assert runtime_path.exists()
    runtime_payload = json.loads(runtime_path.read_text(encoding="utf-8"))
    assert runtime_payload["schema_version"] == 1
    assert runtime_payload["devices"]["envoy"]["product_media_name_id"] == 5002
    assert runtime_payload["devices"]["iqevse"]["product_media_name_id"] == 6001
    assert (
        runtime_payload["devices"]["envoy"]["latest_by_country"]["PR"]["version"]
        == "8.2.4401"
    )
    assert (
        runtime_payload["devices"]["envoy"]["latest_by_locale"]["en-au"]["version"]
        == "8.2.4401"
    )
    assert (
        runtime_payload["devices"]["iqevse"]["latest_by_country"]["AU"]["version"]
        == "25.37.1.14"
    )
    envoy_crawl = runtime_payload["source"]["crawl"]["envoy"]
    charger_crawl = runtime_payload["source"]["crawl"]["iqevse"]
    assert (
        5002,
        "en-au",
        "https://enphase.com/en-au/installers/resources/documentation/communication",
    ) in call_log
    assert (
        6001,
        "en-au",
        "https://enphase.com/en-au/installers/resources/documentation/apps",
    ) in call_log
    assert envoy_crawl["used_global_product_media_id_targets"] == []
    assert charger_crawl["used_global_product_media_id_targets"] == []
    assert (tmp_path / "sources" / "enphase_doc_center" / "entrypoints.json").exists()
    assert (tmp_path / "data" / "PR" / "catalog.json").exists()
    assert (
        tmp_path / "sources" / "enphase_doc_center" / "region_site_routes.json"
    ).exists()

    monkeypatch.setattr(
        firmware_catalog_module,
        "parse_facet_values",
        lambda _apps_html, alias: (
            {}
            if alias == "document"
            else {
                "IQ Gateway software": 5002,
                "IQ EV Charger software": 6001,
                "IQ Microinverter software": 7738,
            }
        ),
    )
    with pytest.raises(RuntimeError, match="release-notes topic id"):
        firmware_catalog_module.build_catalog(tmp_path, timeout=5, max_pages=1)

    monkeypatch.setattr(
        firmware_catalog_module,
        "parse_facet_values",
        lambda _apps_html, alias: (
            {"Release notes": 217} if alias == "document" else {}
        ),
    )
    firmware_catalog_module.build_catalog(tmp_path, timeout=5, max_pages=1)
    runtime_payload = json.loads(runtime_path.read_text(encoding="utf-8"))
    assert runtime_payload["devices"]["envoy"]["product_media_name_id"] is None


def test_build_catalog_skips_later_missing_product_id(
    firmware_catalog_module,
    fixture_dir: Path,
    monkeypatch,
    tmp_path: Path,
) -> None:
    root_html = (fixture_dir / "enphase_root.html").read_text(encoding="utf-8")
    apps_html = (fixture_dir / "enphase_apps_facets.html").read_text(encoding="utf-8")
    gateway_card = firmware_catalog_module.ReleaseCard(
        title="IQ Gateway software release notes (8.3.5286)",
        version="8.3.5286",
        release_date="2026-04-20",
        media_id="24000",
        langcode="und",
        summary="Gateway release",
        countries_text=None,
    )

    monkeypatch.setattr(
        firmware_catalog_module, "_now_utc_iso", lambda: "2026-04-29T00:00:00Z"
    )
    monkeypatch.setattr(
        firmware_catalog_module,
        "REGION_SITE_ROUTE_ROWS",
        [
            {
                "label": "United States",
                "country_code": "US",
                "locale": "en",
                "site_url": "https://enphase.com/",
            }
        ],
    )
    monkeypatch.setattr(
        firmware_catalog_module,
        "fetch_text",
        lambda url, timeout=30: (
            root_html
            if url.endswith("/installers/resources/documentation")
            else apps_html
        ),
    )
    monkeypatch.setattr(
        firmware_catalog_module,
        "parse_facet_values",
        lambda _html, alias: (
            {"Release notes": 217}
            if alias == "document"
            else {"IQ Gateway software": 5002}
        ),
    )
    monkeypatch.setattr(
        firmware_catalog_module,
        "crawl_release_cards",
        lambda **kwargs: (
            [gateway_card] if kwargs["product_media_name_id"] == 5002 else [],
            ["https://example.test/p0"],
        ),
    )
    monkeypatch.setattr(
        firmware_catalog_module,
        "fetch_previous_runtime_catalog",
        lambda timeout=30: {
            "schema_version": 1,
            "generated_at": "2026-04-28T00:00:00Z",
            "source": {
                "devices": {
                    "iqevse": {
                        "docs_url": "https://enphase.com/installers/resources/documentation/apps",
                        "docs_path": "/installers/resources/documentation/apps",
                        "product_type": 216,
                        "document_topic_id": 217,
                        "product_media_name_id": 6001,
                    }
                }
            },
            "devices": {
                "iqevse": {
                    "product_media_name_id": 6001,
                    "document_topic_id": 217,
                    "latest_by_locale": {
                        "en": {
                            "version": "25.37.1.14",
                            "urls_by_locale": {"en": "https://example.test/charger"},
                        }
                    },
                    "latest_by_country": {},
                    "latest_global": {
                        "version": "25.37.1.14",
                        "urls_by_locale": {"en": "https://example.test/charger"},
                    },
                }
            },
        },
    )

    firmware_catalog_module.build_catalog(tmp_path, timeout=5, max_pages=1)
    runtime = json.loads(
        (tmp_path / "catalog" / "v1" / "runtime_catalog.json").read_text(
            encoding="utf-8"
        )
    )
    product_sources = json.loads(
        (
            tmp_path / "sources" / "enphase_doc_center" / "product_media_name_ids.json"
        ).read_text(encoding="utf-8")
    )

    assert runtime["devices"]["envoy"]["latest_global"]["version"] == "8.3.5286"
    assert runtime["devices"]["iqevse"]["latest_global"]["version"] == "25.37.1.14"
    assert runtime["source"]["crawl"]["iqevse"]["skipped"] is True
    assert runtime["source"]["crawl"]["iqevse"]["using_previous_catalog_device"] is True
    assert (
        "IQ EV Charger software" in runtime["source"]["crawl"]["iqevse"]["skip_reason"]
    )
    assert product_sources["targets"] == {
        "envoy": {"label": "IQ Gateway software", "product_media_name_id": 5002},
        "iqevse": {"label": "IQ EV Charger software", "product_media_name_id": 6001},
    }


def test_build_catalog_crawls_gateway_without_product_media_filter(
    firmware_catalog_module,
    fixture_dir: Path,
    monkeypatch,
    tmp_path: Path,
) -> None:
    root_html = (fixture_dir / "enphase_root.html").read_text(encoding="utf-8")
    apps_html = (fixture_dir / "enphase_apps_facets.html").read_text(encoding="utf-8")
    gateway_card = firmware_catalog_module.ReleaseCard(
        title="IQ Gateway software release notes (8.3.5232)",
        version="8.3.5232",
        release_date="2026-04-28",
        media_id="24523",
        langcode="und",
        summary="Countries: Australia",
        countries_text="Australia",
    )
    global_gateway_card = firmware_catalog_module.ReleaseCard(
        title="IQ Gateway software release notes (8.3.5427)",
        version="8.3.5427",
        release_date="2026-04-30",
        media_id="24527",
        langcode="und",
        summary="Global Gateway release",
        countries_text=None,
    )

    monkeypatch.setattr(
        firmware_catalog_module, "_now_utc_iso", lambda: "2026-05-01T00:00:00Z"
    )
    monkeypatch.setattr(
        firmware_catalog_module,
        "REGION_SITE_ROUTE_ROWS",
        [
            {
                "label": "United States",
                "country_code": "US",
                "locale": "en",
                "site_url": "https://enphase.com/",
            },
            {
                "label": "Australia",
                "country_code": "AU",
                "locale": "en-au",
                "site_url": "https://enphase.com/en-au/",
                "query_locale": "en-au",
            },
        ],
    )
    monkeypatch.setattr(
        firmware_catalog_module,
        "fetch_text",
        lambda url, timeout=30: (
            root_html
            if url.endswith("/installers/resources/documentation")
            else apps_html
        ),
    )
    monkeypatch.setattr(
        firmware_catalog_module,
        "parse_facet_values",
        lambda _html, alias: {"Release notes": 217} if alias == "document" else {},
    )
    crawl_calls: list[dict[str, object]] = []

    def _fake_crawl_release_cards(**kwargs):
        crawl_calls.append(dict(kwargs))
        if kwargs["search_locale"] == "en":
            return [global_gateway_card], [
                (
                    "https://enphase.com/installers/resources/documentation/"
                    "communication?search_api_language=en&f%5B0%5D=document%3A217"
                )
            ]
        if kwargs["search_locale"] == "en-au":
            return [gateway_card], [
                (
                    "https://enphase.com/en-au/installers/resources/documentation/"
                    "communication?search_api_language=en-au&f%5B0%5D=document%3A217"
                )
            ]
        return [], [f"https://example.test/empty/{kwargs['search_locale']}"]

    monkeypatch.setattr(
        firmware_catalog_module, "crawl_release_cards", _fake_crawl_release_cards
    )
    monkeypatch.setattr(
        firmware_catalog_module,
        "fetch_previous_runtime_catalog",
        lambda timeout=30: None,
    )

    firmware_catalog_module.build_catalog(tmp_path, timeout=5, max_pages=1)
    runtime = json.loads(
        (tmp_path / "catalog" / "v1" / "runtime_catalog.json").read_text(
            encoding="utf-8"
        )
    )
    product_sources = json.loads(
        (
            tmp_path / "sources" / "enphase_doc_center" / "product_media_name_ids.json"
        ).read_text(encoding="utf-8")
    )
    envoy = runtime["devices"]["envoy"]

    assert {call["product_media_name_id"] for call in crawl_calls} == {None}
    assert envoy["product_media_name_id"] is None
    assert envoy["latest_global"]["version"] == "8.3.5427"
    assert envoy["latest_by_country"]["AU"]["version"] == "8.3.5232"
    au_url = envoy["latest_by_locale"]["en-au"]["urls_by_locale"]["en-au"]
    assert envoy["latest_by_locale"]["en-au"]["version"] == "8.3.5232"
    assert "f%5B0%5D=document%3A217" in au_url
    assert "f%5B1%5D" not in au_url
    assert "en-au/installers" in au_url
    assert product_sources["targets"]["envoy"] == {
        "label": "IQ Gateway software",
        "product_media_name_id": None,
    }


def test_build_catalog_uses_newest_release_when_families_differ(
    firmware_catalog_module,
    fixture_dir: Path,
    monkeypatch,
    tmp_path: Path,
) -> None:
    root_html = (fixture_dir / "enphase_root.html").read_text(encoding="utf-8")
    apps_html = (fixture_dir / "enphase_apps_facets.html").read_text(encoding="utf-8")
    global_card = firmware_catalog_module.ReleaseCard(
        title="IQ Gateway software release notes (9.0.100)",
        version="9.0.100",
        release_date="2026-04-30",
        media_id="global-9",
        langcode="und",
        summary="Countries: Australia",
        countries_text="Australia",
    )
    global_nz_card = firmware_catalog_module.ReleaseCard(
        title="IQ Gateway software release notes (9.0.200)",
        version="9.0.200",
        release_date="2026-04-30",
        media_id="global-nz-9",
        langcode="und",
        summary="Countries: New Zealand",
        countries_text="New Zealand",
    )
    older_regional_card = firmware_catalog_module.ReleaseCard(
        title="IQ Gateway software release notes (8.3.5232)",
        version="8.3.5232",
        release_date="2026-04-28",
        media_id="au-8",
        langcode="und",
        summary="Countries: Australia",
        countries_text="Australia",
    )
    newer_regional_card = firmware_catalog_module.ReleaseCard(
        title="IQ Gateway software release notes (10.0.1)",
        version="10.0.1",
        release_date="2026-05-01",
        media_id="nz-10",
        langcode="und",
        summary="Countries: New Zealand",
        countries_text="New Zealand",
    )

    monkeypatch.setattr(
        firmware_catalog_module, "_now_utc_iso", lambda: "2026-05-01T00:00:00Z"
    )
    monkeypatch.setattr(
        firmware_catalog_module,
        "REGION_SITE_ROUTE_ROWS",
        [
            {
                "label": "United States",
                "country_code": "US",
                "locale": "en",
                "site_url": "https://enphase.com/",
            },
            {
                "label": "Australia",
                "country_code": "AU",
                "locale": "en-au",
                "site_url": "https://enphase.com/en-au/",
                "query_locale": "en-au",
            },
            {
                "label": "New Zealand",
                "country_code": "NZ",
                "locale": "en-nz",
                "site_url": "https://enphase.com/en-nz/",
                "query_locale": "en-nz",
            },
        ],
    )
    monkeypatch.setattr(
        firmware_catalog_module,
        "fetch_text",
        lambda url, timeout=30: (
            root_html
            if url.endswith("/installers/resources/documentation")
            else apps_html
        ),
    )
    monkeypatch.setattr(
        firmware_catalog_module,
        "parse_facet_values",
        lambda _html, alias: {"Release notes": 217} if alias == "document" else {},
    )

    def _fake_crawl_release_cards(**kwargs):
        cards_by_locale = {
            "en": [global_card, global_nz_card],
            "en-au": [older_regional_card],
            "en-nz": [newer_regional_card],
        }
        return cards_by_locale.get(kwargs["search_locale"], []), [
            f"https://example.test/{kwargs['search_locale']}"
        ]

    monkeypatch.setattr(
        firmware_catalog_module, "crawl_release_cards", _fake_crawl_release_cards
    )
    monkeypatch.setattr(
        firmware_catalog_module,
        "fetch_previous_runtime_catalog",
        lambda timeout=30: None,
    )

    firmware_catalog_module.build_catalog(tmp_path, timeout=5, max_pages=1)
    runtime = json.loads(
        (tmp_path / "catalog" / "v1" / "runtime_catalog.json").read_text(
            encoding="utf-8"
        )
    )
    envoy = runtime["devices"]["envoy"]

    assert envoy["latest_by_country"]["AU"]["version"] == "9.0.100"
    assert envoy["latest_by_country"]["NZ"]["version"] == "10.0.1"


def test_build_catalog_required_product_is_explicit(
    firmware_catalog_module,
    fixture_dir: Path,
    monkeypatch,
    tmp_path: Path,
) -> None:
    root_html = (fixture_dir / "enphase_root.html").read_text(encoding="utf-8")
    apps_html = (fixture_dir / "enphase_apps_facets.html").read_text(encoding="utf-8")

    monkeypatch.setattr(
        firmware_catalog_module,
        "TARGET_PRODUCTS",
        {
            "iqevse": {
                "label": "IQ EV Charger software",
                "docs_path": firmware_catalog_module.TARGET_CATEGORY_PATH,
                "required": False,
            },
            "envoy": {
                "label": "IQ Gateway software",
                "docs_path": firmware_catalog_module.COMMUNICATION_CATEGORY_PATH,
                "product_media_name_required": True,
                "required": True,
            },
        },
    )
    monkeypatch.setattr(
        firmware_catalog_module,
        "REGION_SITE_ROUTE_ROWS",
        [
            {
                "label": "United States",
                "country_code": "US",
                "locale": "en",
                "site_url": "https://enphase.com/",
            }
        ],
    )
    monkeypatch.setattr(
        firmware_catalog_module,
        "fetch_text",
        lambda url, timeout=30: (
            root_html
            if url.endswith("/installers/resources/documentation")
            else apps_html
        ),
    )
    monkeypatch.setattr(
        firmware_catalog_module,
        "parse_facet_values",
        lambda _html, alias: {"Release notes": 217} if alias == "document" else {},
    )
    monkeypatch.setattr(
        firmware_catalog_module,
        "fetch_previous_runtime_catalog",
        lambda timeout=30: {
            "devices": {"iqevse": {"product_media_name_id": 6001}},
            "source": {"devices": {"iqevse": {"product_media_name_id": 6001}}},
        },
    )

    with pytest.raises(RuntimeError, match="IQ Gateway software"):
        firmware_catalog_module.build_catalog(tmp_path, timeout=5, max_pages=1)


def test_country_entry_prefers_non_global_over_global_fallback(
    firmware_catalog_module,
    monkeypatch,
) -> None:
    global_entry = {"version": "1.0.0", "media_id": "g"}
    regional_entry = {"version": "2.0.0", "media_id": "r"}
    assert firmware_catalog_module._should_replace_country_entry(
        existing=None,
        candidate=global_entry,
        global_entry=global_entry,
    )
    assert firmware_catalog_module._should_replace_country_entry(
        existing=global_entry,
        candidate=regional_entry,
        global_entry=global_entry,
    )
    assert not firmware_catalog_module._should_replace_country_entry(
        existing=regional_entry,
        candidate=global_entry,
        global_entry=global_entry,
    )
    assert firmware_catalog_module._should_replace_country_entry(
        existing={"version": "1.0.0", "media_id": "a"},
        candidate={"version": "1.1.0", "media_id": "b"},
        global_entry=global_entry,
    )
    assert not firmware_catalog_module._should_replace_country_entry(
        existing={"version": "2.0.0", "media_id": "a"},
        candidate={"version": "1.1.0", "media_id": "b"},
        global_entry=global_entry,
    )
    assert not firmware_catalog_module._is_same_release_entry(None, global_entry)
    assert firmware_catalog_module._is_same_release_entry(
        {"version": "2.0.0", "media_id": "shared"},
        {"version": "2.0.0", "media_id": "shared"},
    )
    assert not firmware_catalog_module._is_same_release_entry(
        {"version": "2.0.0"},
        {"version": "2.0.0"},
    )
    assert firmware_catalog_module._is_same_release_entry(
        {
            "version": "2.0.0",
            "release_date": "2025-12-01",
            "title": "Release",
        },
        {
            "version": "2.0.0",
            "release_date": "2025-12-01",
            "title": "Localized release",
        },
    )
    assert firmware_catalog_module._is_same_release_entry(
        {
            "version": "2.0.0",
            "release_date": "2025-12-01",
            "title": "Release",
        },
        {
            "version": "2.0.0",
            "release_date": "2025-12-01",
            "title": "Release",
        },
    )
    assert firmware_catalog_module._is_same_release_entry(
        {
            "version": "2.0.0",
            "title": "Release",
        },
        {
            "version": "2.0.0",
            "title": "Release",
        },
    )
    assert firmware_catalog_module._is_same_release_entry(
        {
            "version": "2.0.0",
            "release_date": "2025-12-01",
            "title": "Release",
            "media_id": "localized-a",
        },
        {
            "version": "2.0.0",
            "release_date": "2025-12-01",
            "title": "Release",
            "media_id": "localized-b",
        },
    )


def test_bootstrap_target_discovers_apps_entrypoint_for_apps_docs(
    firmware_catalog_module,
    monkeypatch,
) -> None:
    target = {
        "site_url": "https://enphase.com/en-au/",
        "query_locale": "en-au",
        "key": "https://enphase.com/en-au/|en-au",
    }

    monkeypatch.setattr(
        firmware_catalog_module,
        "fetch_text",
        lambda url, timeout=30: "ROOT" if url.endswith("/documentation") else "APPS",
    )
    monkeypatch.setattr(
        firmware_catalog_module,
        "discover_apps_entrypoint",
        lambda html: ("/installers/resources/documentation/apps", "216"),
    )
    monkeypatch.setattr(
        firmware_catalog_module,
        "parse_product_type_from_apps_page",
        lambda html: "216",
    )
    monkeypatch.setattr(
        firmware_catalog_module,
        "parse_facet_values",
        lambda html, alias: (
            {"Release notes": 217}
            if alias == "document"
            else {"IQ Gateway software": 5002, "IQ EV Charger software": 6001}
        ),
    )

    bootstrap = firmware_catalog_module._bootstrap_target(
        target,
        timeout=5,
        docs_path=firmware_catalog_module.TARGET_CATEGORY_PATH,
    )

    assert bootstrap["docs_path"] == "/installers/resources/documentation/apps"
    assert bootstrap["apps_bootstrap_url"].endswith("product_type=216")
    assert bootstrap["product_ids"]["envoy"] == 5002
    assert bootstrap["product_ids"]["iqevse"] == 6001


def test_locale_fallback_uses_best_country_entry_not_global(
    firmware_catalog_module,
    fixture_dir: Path,
    monkeypatch,
    tmp_path: Path,
) -> None:
    root_html = (fixture_dir / "enphase_root.html").read_text(encoding="utf-8")
    apps_html = (fixture_dir / "enphase_apps_facets.html").read_text(encoding="utf-8")

    def _fake_fetch_text(url: str, *, timeout: int = 30) -> str:  # noqa: ARG001
        if url.endswith("/installers/resources/documentation"):
            return root_html
        return apps_html

    global_envoy = firmware_catalog_module.ReleaseCard(
        title="IQ Gateway software release notes (8.2.4401)",
        version="8.2.4401",
        release_date="2025-11-20",
        media_id="global-envoy",
        langcode="und",
        summary="Global envoy release",
        countries_text=None,
    )
    be_regional_envoy_fr = firmware_catalog_module.ReleaseCard(
        title="IQ Gateway notes de version du logiciel (8.2.4500)",
        version="8.2.4500",
        release_date="2025-12-01",
        media_id="be-regional-envoy-fr",
        langcode="und",
        summary="Belgium envoy release",
        countries_text=None,
    )
    be_regional_envoy_nl = firmware_catalog_module.ReleaseCard(
        title="IQ Gateway softwareversie-opmerkingen (8.2.4500)",
        version="8.2.4500",
        release_date="2025-12-01",
        media_id="be-regional-envoy-nl",
        langcode="und",
        summary="Belgium envoy release",
        countries_text=None,
    )

    def _fake_crawl_release_cards(**kwargs):
        product_media_name_id = kwargs["product_media_name_id"]
        search_locale = kwargs["search_locale"]
        if product_media_name_id == 5002 and search_locale == "en":
            return [global_envoy], ["https://example.test/envoy/global"]
        if product_media_name_id == 5002 and search_locale == "fr-be":
            return [be_regional_envoy_fr], ["https://example.test/envoy/fr-be"]
        if product_media_name_id == 5002 and search_locale == "nl-be":
            return [be_regional_envoy_nl], ["https://example.test/envoy/nl-be"]
        return [], [
            f"https://example.test/empty/{product_media_name_id}/{search_locale}"
        ]

    monkeypatch.setattr(
        firmware_catalog_module, "_now_utc_iso", lambda: "2026-03-01T00:00:00Z"
    )
    monkeypatch.setattr(
        firmware_catalog_module,
        "REGION_SITE_ROUTE_ROWS",
        [
            {
                "label": "United States",
                "country_code": "US",
                "locale": "en",
                "site_url": "https://enphase.com/",
            },
            {
                "label": "Canada",
                "country_code": "CA",
                "locale": None,
                "site_url": "https://enphase.com/en-ca/",
                "query_locale": "en-ca",
            },
            {
                "label": "Belgium - Francais",
                "country_code": "BE",
                "locale": "fr-be",
                "site_url": "https://enphase.com/fr-be/",
            },
            {
                "label": "Belgium - Deutsch",
                "country_code": "BE",
                "locale": "de-be",
                "site_url": "https://enphase.com/de-be/",
            },
            {
                "label": "Belgium - Nederlands",
                "country_code": "BE",
                "locale": "nl-be",
                "site_url": "https://enphase.com/nl-be/",
            },
            {
                "label": "French Territories",
                "country_code": None,
                "locale": "fr-fot",
                "site_url": "https://enphase.com/fr-fot/",
            },
        ],
    )
    monkeypatch.setattr(firmware_catalog_module, "fetch_text", _fake_fetch_text)
    original_parse_facet_values = firmware_catalog_module.parse_facet_values
    monkeypatch.setattr(
        firmware_catalog_module,
        "parse_facet_values",
        lambda html, alias: (
            {
                **original_parse_facet_values(html, alias),
                "IQ EV Charger software": 6001,
            }
            if alias == "product_media_name"
            else original_parse_facet_values(html, alias)
        ),
    )
    monkeypatch.setattr(
        firmware_catalog_module, "crawl_release_cards", _fake_crawl_release_cards
    )
    monkeypatch.setattr(
        firmware_catalog_module,
        "fetch_previous_runtime_catalog",
        lambda timeout=30: None,
    )

    firmware_catalog_module.build_catalog(tmp_path, timeout=5, max_pages=1)
    runtime_payload = json.loads(
        (tmp_path / "catalog" / "v1" / "runtime_catalog.json").read_text(
            encoding="utf-8"
        )
    )
    envoy = runtime_payload["devices"]["envoy"]

    assert envoy["latest_global"]["version"] == "8.2.4401"
    assert envoy["latest_by_country"]["BE"]["version"] == "8.2.4500"
    assert envoy["latest_by_locale"]["nl-be"]["version"] == "8.2.4500"
    assert envoy["latest_by_locale"]["fr-be"]["version"] == "8.2.4500"
    assert envoy["latest_by_locale"]["fr-be"]["media_id"] == "be-regional-envoy-fr"
    assert envoy["latest_by_locale"]["de-be"]["version"] == "8.2.4500"
    assert envoy["latest_by_locale"]["de-be"]["media_id"] == "be-regional-envoy-fr"
    assert envoy["latest_by_locale"]["nl-be"]["media_id"] == "be-regional-envoy-nl"
    assert "/fr-be/" in envoy["latest_by_locale"]["fr-be"]["urls_by_locale"]["fr-be"]
    assert "/nl-be/" in envoy["latest_by_locale"]["nl-be"]["urls_by_locale"]["nl-be"]
    assert envoy["latest_by_locale"]["fr-fot"]["version"] == "8.2.4401"


def test_build_catalog_filters_country_scoped_releases(
    firmware_catalog_module,
    fixture_dir: Path,
    monkeypatch,
    tmp_path: Path,
) -> None:
    root_html = (fixture_dir / "enphase_root.html").read_text(encoding="utf-8")
    apps_html = (fixture_dir / "enphase_apps_facets.html").read_text(encoding="utf-8")

    def _fake_fetch_text(url: str, *, timeout: int = 30) -> str:  # noqa: ARG001
        if url.endswith("/installers/resources/documentation"):
            return root_html
        return apps_html

    scoped_envoy = firmware_catalog_module.ReleaseCard(
        title="IQ Gateway software release notes (8.2.4401)",
        version="8.2.4401",
        release_date="2025-11-20",
        media_id="zz-us-scoped-envoy",
        langcode="und",
        summary=(
            "Countries: United States, Puerto Rico, Canada, Mexico, Bermuda, "
            "Brazil, and the Philippines. Feature classification: Major"
        ),
        countries_text=(
            "United States, Puerto Rico, Canada, Mexico, Bermuda, Brazil, "
            "and the Philippines"
        ),
    )
    commercial_envoy = firmware_catalog_module.ReleaseCard(
        title="IQ Gateway software release notes (9.1.1188)",
        version="9.1.1188",
        release_date="2026-06-10",
        media_id="commercial-envoy",
        langcode="und",
        summary=(
            "IQ Gateway software version 9.1.1188 addresses stability issues "
            "for grid-tied systems with IQ9N Microinverters (277 V) and "
            "IQ Gateway Commercial Pro. Supported regions: United States."
        ),
        countries_text="United States",
    )
    br_scoped_envoy = firmware_catalog_module.ReleaseCard(
        title="Notas de versao do software IQ Gateway (8.2.4401)",
        version="8.2.4401",
        release_date="2025-11-20",
        media_id="aa-br-scoped-envoy",
        langcode="und",
        summary="Countries: Brazil",
        countries_text="Brazil",
    )
    worldwide_envoy = firmware_catalog_module.ReleaseCard(
        title="IQ Gateway software release notes (8.2.4398)",
        version="8.2.4398",
        release_date="2025-10-20",
        media_id="worldwide-envoy",
        langcode="und",
        summary="Countries: Worldwide",
        countries_text="Worldwide",
    )

    def _fake_crawl_release_cards(**kwargs):
        if kwargs["product_media_name_id"] == 5002 and kwargs["search_locale"] == "en":
            return [commercial_envoy, scoped_envoy, worldwide_envoy], [
                f"https://example.test/envoy/{kwargs['search_locale']}"
            ]
        if (
            kwargs["product_media_name_id"] == 5002
            and kwargs["search_locale"] == "pt-br"
        ):
            return [br_scoped_envoy, worldwide_envoy], [
                f"https://example.test/envoy/{kwargs['search_locale']}"
            ]
        if kwargs["product_media_name_id"] == 5002:
            return [worldwide_envoy], [
                f"https://example.test/envoy/{kwargs['search_locale']}"
            ]
        return [], [f"https://example.test/empty/{kwargs['search_locale']}"]

    monkeypatch.setattr(
        firmware_catalog_module, "_now_utc_iso", lambda: "2026-03-01T00:00:00Z"
    )
    monkeypatch.setattr(
        firmware_catalog_module,
        "REGION_SITE_ROUTE_ROWS",
        [
            {
                "label": "United States",
                "country_code": "US",
                "locale": "en",
                "site_url": "https://enphase.com/",
            },
            {
                "label": "Australia",
                "country_code": "AU",
                "locale": "en-au",
                "site_url": "https://enphase.com/en-au/",
                "query_locale": "en-au",
            },
            {
                "label": "Brazil",
                "country_code": "BR",
                "locale": "pt-br",
                "site_url": "https://enphase.com/pt-br/",
                "query_locale": "pt-br",
            },
            {
                "label": "Philippines",
                "country_code": "PH",
                "locale": "en-ph",
                "site_url": "https://enphase.com/en-ph/",
                "query_locale": "en-ph",
            },
        ],
    )
    monkeypatch.setattr(firmware_catalog_module, "fetch_text", _fake_fetch_text)
    original_parse_facet_values = firmware_catalog_module.parse_facet_values
    monkeypatch.setattr(
        firmware_catalog_module,
        "parse_facet_values",
        lambda html, alias: (
            {
                **original_parse_facet_values(html, alias),
                "IQ EV Charger software": 6001,
            }
            if alias == "product_media_name"
            else original_parse_facet_values(html, alias)
        ),
    )
    monkeypatch.setattr(
        firmware_catalog_module, "crawl_release_cards", _fake_crawl_release_cards
    )
    monkeypatch.setattr(
        firmware_catalog_module,
        "fetch_previous_runtime_catalog",
        lambda timeout=30: None,
    )

    firmware_catalog_module.build_catalog(tmp_path, timeout=5, max_pages=1)
    runtime_payload = json.loads(
        (tmp_path / "catalog" / "v1" / "runtime_catalog.json").read_text(
            encoding="utf-8"
        )
    )
    envoy = runtime_payload["devices"]["envoy"]

    assert envoy["latest_global"]["version"] == "8.2.4398"
    assert envoy["latest_by_country"]["US"]["version"] == "8.2.4401"
    assert envoy["latest_by_locale"]["en"]["version"] == "8.2.4401"
    assert envoy["latest_by_country"]["AU"]["version"] == "8.2.4398"
    assert envoy["latest_by_locale"]["en-au"]["version"] == "8.2.4398"
    assert envoy["latest_by_country"]["BR"]["version"] == "8.2.4401"
    assert envoy["latest_by_locale"]["pt-br"]["version"] == "8.2.4401"
    assert envoy["latest_by_locale"]["pt-br"]["media_id"] == "aa-br-scoped-envoy"
    assert "/pt-br/" in envoy["latest_by_locale"]["pt-br"]["urls_by_locale"]["pt-br"]
    assert envoy["latest_by_country"]["PH"]["version"] == "8.2.4401"
    assert envoy["latest_by_locale"]["en-ph"]["version"] == "8.2.4401"
    assert envoy["latest_by_locale"]["en-ph"]["media_id"] == "zz-us-scoped-envoy"


def test_build_catalog_suppresses_stale_worldwide_fallback(
    firmware_catalog_module,
    fixture_dir: Path,
    monkeypatch,
    tmp_path: Path,
) -> None:
    root_html = (fixture_dir / "enphase_root.html").read_text(encoding="utf-8")
    apps_html = (fixture_dir / "enphase_apps_facets.html").read_text(encoding="utf-8")

    def _fake_fetch_text(url: str, *, timeout: int = 30) -> str:  # noqa: ARG001
        if url.endswith("/installers/resources/documentation"):
            return root_html
        return apps_html

    scoped_envoy = firmware_catalog_module.ReleaseCard(
        title="IQ Gateway software release notes (8.2.4401)",
        version="8.2.4401",
        release_date="2025-11-20",
        media_id="us-scoped-envoy",
        langcode="und",
        summary="Countries: United States",
        countries_text="United States",
    )
    stale_worldwide_envoy = firmware_catalog_module.ReleaseCard(
        title="IQ Gateway software release notes (7.0.86)",
        version="7.0.86",
        release_date="2021-12-21",
        media_id="stale-worldwide-envoy",
        langcode="und",
        summary="Countries: Worldwide",
        countries_text="Worldwide",
    )

    def _fake_crawl_release_cards(**kwargs):
        if kwargs["product_media_name_id"] == 5002:
            return [scoped_envoy, stale_worldwide_envoy], [
                f"https://example.test/envoy/{kwargs['search_locale']}"
            ]
        return [], [f"https://example.test/empty/{kwargs['search_locale']}"]

    monkeypatch.setattr(
        firmware_catalog_module, "_now_utc_iso", lambda: "2026-03-01T00:00:00Z"
    )
    monkeypatch.setattr(
        firmware_catalog_module,
        "REGION_SITE_ROUTE_ROWS",
        [
            {
                "label": "United States",
                "country_code": "US",
                "locale": "en",
                "site_url": "https://enphase.com/",
            },
            {
                "label": "Australia",
                "country_code": "AU",
                "locale": "en-au",
                "site_url": "https://enphase.com/en-au/",
                "query_locale": "en-au",
            },
        ],
    )
    monkeypatch.setattr(firmware_catalog_module, "fetch_text", _fake_fetch_text)
    original_parse_facet_values = firmware_catalog_module.parse_facet_values
    monkeypatch.setattr(
        firmware_catalog_module,
        "parse_facet_values",
        lambda html, alias: (
            {
                **original_parse_facet_values(html, alias),
                "IQ EV Charger software": 6001,
            }
            if alias == "product_media_name"
            else original_parse_facet_values(html, alias)
        ),
    )
    monkeypatch.setattr(
        firmware_catalog_module, "crawl_release_cards", _fake_crawl_release_cards
    )
    monkeypatch.setattr(
        firmware_catalog_module,
        "fetch_previous_runtime_catalog",
        lambda timeout=30: None,
    )

    firmware_catalog_module.build_catalog(tmp_path, timeout=5, max_pages=1)
    runtime_payload = json.loads(
        (tmp_path / "catalog" / "v1" / "runtime_catalog.json").read_text(
            encoding="utf-8"
        )
    )
    envoy = runtime_payload["devices"]["envoy"]

    assert envoy["latest_global"] is None
    assert envoy["latest_by_country"]["US"]["version"] == "8.2.4401"
    assert envoy["latest_by_locale"]["en"]["version"] == "8.2.4401"
    assert "AU" not in envoy["latest_by_country"]
    assert "en-au" not in envoy["latest_by_locale"]


def test_route_helper_edge_branches(firmware_catalog_module) -> None:
    assert firmware_catalog_module._normalize_country_code(None) is None
    assert firmware_catalog_module._normalize_country_code("AUS") is None
    assert firmware_catalog_module._normalize_country_code("au") == "AU"

    with pytest.raises(ValueError, match="site_url is required"):
        firmware_catalog_module._normalize_site_url("   ")
    with pytest.raises(ValueError, match="invalid site_url"):
        firmware_catalog_module._normalize_site_url("https:///x")
    assert (
        firmware_catalog_module._normalize_site_url("enphase.com/en-au")
        == "https://enphase.com/en-au/"
    )
    assert (
        firmware_catalog_module._normalize_site_url("/vi-vn/")
        == "https://enphase.com/vi-vn/"
    )

    assert (
        firmware_catalog_module._infer_locale_from_site_url("https://enphase.com/")
        == "en"
    )
    assert (
        firmware_catalog_module._infer_locale_from_site_url(
            "https://enphase.com/en-AU/"
        )
        == "en-au"
    )
    assert (
        firmware_catalog_module._infer_locale_from_site_url(
            "https://enphase.com/installers/resources"
        )
        is None
    )

    assert (
        firmware_catalog_module.build_region_site_routes(
            [
                {
                    "label": "   ",
                    "country_code": "US",
                    "locale": "en",
                    "site_url": "https://enphase.com/",
                }
            ]
        )
        == []
    )

    targets = firmware_catalog_module.build_crawl_targets(
        [
            {
                "target_key": "https://enphase.com/|en",
                "site_url": "https://enphase.com/",
                "query_locale": "en",
                "label": "United States",
                "country_code": "US",
                "locale": None,
            }
        ]
    )
    assert targets[0]["locales"] == ["en"]

    assert (
        firmware_catalog_module._resolve_release_notes_topic_id({"Release-notes": 999})
        == 999
    )
    assert (
        firmware_catalog_module._resolve_release_notes_topic_id({"Anything": 217})
        == 217
    )

    assert (
        firmware_catalog_module._is_global_fallback_entry(None, {"media_id": "x"})
        is False
    )
    assert (
        firmware_catalog_module._should_replace_country_entry(
            existing={"version": "1.0.0"},
            candidate=None,
            global_entry={"media_id": "x"},
        )
        is False
    )


def test_authoritative_region_routes_cover_current_public_sites(
    firmware_catalog_module,
) -> None:
    routes = firmware_catalog_module.build_region_site_routes(
        firmware_catalog_module.REGION_SITE_ROUTE_ROWS
    )
    route_by_country_and_locale = {
        (route["country_code"], route["locale"]): route for route in routes
    }

    assert route_by_country_and_locale[("CL", "es-lac")]["site_url"] == (
        "https://enphase.com/es-lac/"
    )
    assert route_by_country_and_locale[("JM", "en-lac")]["site_url"] == (
        "https://enphase.com/en-lac/"
    )
    assert route_by_country_and_locale[("CH", "de-ch")]["site_url"] == (
        "https://enphase.com/de-ch/"
    )
    assert route_by_country_and_locale[("CH", "it-ch")]["site_url"] == (
        "https://enphase.com/it-ch/"
    )


def test_build_catalog_edge_error_and_degradation_paths(
    firmware_catalog_module,
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        firmware_catalog_module, "build_region_site_routes", lambda _rows: []
    )
    with pytest.raises(
        RuntimeError, match="No authoritative region-site routes configured"
    ):
        firmware_catalog_module.build_catalog(tmp_path, timeout=5, max_pages=1)

    monkeypatch.setattr(
        firmware_catalog_module,
        "build_region_site_routes",
        lambda _rows: [{"target_key": "missing", "country_code": "US"}],
    )
    monkeypatch.setattr(
        firmware_catalog_module, "build_crawl_targets", lambda _routes: []
    )
    with pytest.raises(RuntimeError, match="Global routing target is missing"):
        firmware_catalog_module.build_catalog(tmp_path, timeout=5, max_pages=1)

    routes = [
        {
            "label": "United States",
            "country_code": "US",
            "locale": "en",
            "site_url": "https://global.example/",
            "query_locale": "en",
            "target_key": "https://global.example/|en",
        },
        {
            "label": "Australia",
            "country_code": "AU",
            "locale": "en-au",
            "site_url": "https://regional.example/",
            "query_locale": "en-au",
            "target_key": "https://regional.example/|en-au",
        },
    ]
    targets = [
        {
            "key": "https://global.example/|en",
            "site_url": "https://global.example/",
            "query_locale": "en",
            "locales": ["en"],
            "countries": ["US"],
            "labels": ["United States"],
        },
        {
            "key": "https://regional.example/|en-au",
            "site_url": "https://regional.example/",
            "query_locale": "en-au",
            "locales": ["en-au"],
            "countries": ["AU"],
            "labels": ["Australia"],
        },
    ]

    monkeypatch.setattr(
        firmware_catalog_module, "build_region_site_routes", lambda _rows: routes
    )
    monkeypatch.setattr(
        firmware_catalog_module, "build_crawl_targets", lambda _routes: targets
    )

    def _fake_fetch_text(url: str, timeout: int = 30) -> str:  # noqa: ARG001
        if "regional.example" in url:
            raise RuntimeError("HTTP Error 404: Not Found")
        return "GLOBAL"

    monkeypatch.setattr(firmware_catalog_module, "fetch_text", _fake_fetch_text)
    monkeypatch.setattr(
        firmware_catalog_module,
        "discover_apps_entrypoint",
        lambda _html: ("/installers/resources/documentation/apps", "216"),
    )
    monkeypatch.setattr(
        firmware_catalog_module,
        "parse_product_type_from_apps_page",
        lambda _html: "216",
    )
    monkeypatch.setattr(
        firmware_catalog_module,
        "parse_facet_values",
        lambda html, alias: (
            {"Release notes": 217}
            if alias == "document"
            else (
                {
                    "IQ Gateway software": 5002,
                    "IQ EV Charger software": 6001,
                }
                if html == "GLOBAL"
                else {}
            )
        ),
    )
    monkeypatch.setattr(
        firmware_catalog_module, "parse_language_options", lambda _html, _name: {}
    )

    def _fake_crawl_release_cards(**kwargs):
        if kwargs["search_locale"] == "en-au":
            raise RuntimeError("HTTP Error 404: Not Found")
        return [], ["https://example.test/p0"]

    monkeypatch.setattr(
        firmware_catalog_module,
        "crawl_release_cards",
        _fake_crawl_release_cards,
    )
    monkeypatch.setattr(
        firmware_catalog_module,
        "fetch_previous_runtime_catalog",
        lambda timeout=30: None,
    )
    monkeypatch.setattr(
        firmware_catalog_module, "_now_utc_iso", lambda: "2026-03-01T00:00:00Z"
    )

    firmware_catalog_module.build_catalog(tmp_path, timeout=5, max_pages=1)
    runtime = json.loads(
        (tmp_path / "catalog" / "v1" / "runtime_catalog.json").read_text(
            encoding="utf-8"
        )
    )
    assert runtime["devices"]["envoy"]["latest_global"] is None
    assert runtime["devices"]["envoy"]["latest_by_country"] == {}
    envoy_meta = runtime["source"]["crawl"]["envoy"]
    assert envoy_meta["bootstrap_error_targets"] == ["https://regional.example/|en-au"]
    assert envoy_meta["crawl_error_targets"] == ["https://regional.example/|en-au"]
    assert (
        envoy_meta["targets"]["https://regional.example/|en-au"]["bootstrap_error"]
        == "HTTP Error 404: Not Found"
    )
    assert (
        envoy_meta["targets"]["https://regional.example/|en-au"]["crawl_error"]
        == "HTTP Error 404: Not Found"
    )


def test_build_catalog_tracks_missing_regional_product_ids(
    firmware_catalog_module,
    monkeypatch,
    tmp_path: Path,
) -> None:
    routes = [
        {
            "label": "United States",
            "country_code": "US",
            "locale": "en",
            "site_url": "https://global.example/",
            "query_locale": "en",
            "target_key": "https://global.example/|en",
        },
        {
            "label": "Australia",
            "country_code": "AU",
            "locale": "en-au",
            "site_url": "https://regional.example/",
            "query_locale": "en-au",
            "target_key": "https://regional.example/|en-au",
        },
    ]
    targets = [
        {
            "key": "https://global.example/|en",
            "site_url": "https://global.example/",
            "query_locale": "en",
            "locales": ["en"],
            "countries": ["US"],
            "labels": ["United States"],
        },
        {
            "key": "https://regional.example/|en-au",
            "site_url": "https://regional.example/",
            "query_locale": "en-au",
            "locales": ["en-au"],
            "countries": ["AU"],
            "labels": ["Australia"],
        },
    ]

    monkeypatch.setattr(
        firmware_catalog_module, "build_region_site_routes", lambda _rows: routes
    )
    monkeypatch.setattr(
        firmware_catalog_module, "build_crawl_targets", lambda _routes: targets
    )
    monkeypatch.setattr(
        firmware_catalog_module,
        "fetch_text",
        lambda url, timeout=30: "GLOBAL" if "global.example" in url else "REGIONAL",
    )
    monkeypatch.setattr(
        firmware_catalog_module,
        "discover_apps_entrypoint",
        lambda _html: ("/installers/resources/documentation/apps", "216"),
    )
    monkeypatch.setattr(
        firmware_catalog_module,
        "parse_product_type_from_apps_page",
        lambda _html: "216",
    )
    monkeypatch.setattr(
        firmware_catalog_module,
        "parse_facet_values",
        lambda html, alias: (
            {"Release notes": 217}
            if alias == "document"
            else (
                {
                    "IQ Gateway software": 5002,
                    "IQ EV Charger software": 6001,
                }
                if html == "GLOBAL"
                else {}
            )
        ),
    )
    monkeypatch.setattr(
        firmware_catalog_module, "parse_language_options", lambda _html, _name: {}
    )
    monkeypatch.setattr(
        firmware_catalog_module,
        "crawl_release_cards",
        lambda **kwargs: ([], [f"https://example.test/{kwargs['search_locale']}"]),
    )
    monkeypatch.setattr(
        firmware_catalog_module,
        "fetch_previous_runtime_catalog",
        lambda timeout=30: None,
    )
    monkeypatch.setattr(
        firmware_catalog_module, "_now_utc_iso", lambda: "2026-03-01T00:00:00Z"
    )

    firmware_catalog_module.build_catalog(tmp_path, timeout=5, max_pages=1)
    runtime = json.loads(
        (tmp_path / "catalog" / "v1" / "runtime_catalog.json").read_text(
            encoding="utf-8"
        )
    )
    envoy_meta = runtime["source"]["crawl"]["envoy"]
    assert envoy_meta["missing_product_media_id_targets"] == [
        "https://regional.example/|en-au"
    ]
    assert envoy_meta["used_global_product_media_id_targets"] == [
        "https://regional.example/|en-au"
    ]


def test_parse_args_and_main_paths(
    firmware_catalog_module, monkeypatch, tmp_path: Path, capsys
) -> None:
    default_args = firmware_catalog_module.parse_args([])
    assert default_args.output_dir == "."
    assert default_args.timeout == firmware_catalog_module.DEFAULT_TIMEOUT
    assert default_args.max_pages == firmware_catalog_module.DEFAULT_MAX_PAGES

    custom_args = firmware_catalog_module.parse_args(
        ["--output-dir", str(tmp_path), "--timeout", "11", "--max-pages", "5"]
    )
    assert custom_args.output_dir == str(tmp_path)
    assert custom_args.timeout == 11
    assert custom_args.max_pages == 5

    monkeypatch.setattr(
        firmware_catalog_module,
        "parse_args",
        lambda _argv: argparse.Namespace(
            output_dir=str(tmp_path), timeout=7, max_pages=3
        ),
    )
    monkeypatch.setattr(
        firmware_catalog_module, "build_catalog", lambda *_args, **_kwargs: None
    )
    assert firmware_catalog_module.main(["--dummy"]) == 0
    assert "Firmware catalog generated at" in capsys.readouterr().out

    monkeypatch.setattr(
        firmware_catalog_module,
        "build_catalog",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert firmware_catalog_module.main(None) == 1
    assert "Failed to build firmware catalog: boom" in capsys.readouterr().err
