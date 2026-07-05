#!/usr/bin/env python3
"""Build and publish Enphase firmware catalog artifacts.

This script discovers release-note metadata from Enphase's documentation pages,
then emits source metadata and a runtime catalog used by the integration.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlsplit, urlunsplit
from urllib.request import Request, urlopen

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://enphase.com"
ROOT_PATH = "/installers/resources/documentation"
TARGET_CATEGORY_LABEL = "Apps and software"
TARGET_CATEGORY_PATH = "/installers/resources/documentation/apps"
COMMUNICATION_CATEGORY_PATH = "/installers/resources/documentation/communication"
DEFAULT_PRODUCT_TYPE = "216"

TARGET_PRODUCTS: dict[str, dict[str, Any]] = {
    "envoy": {
        "label": "IQ Gateway software",
        "docs_path": COMMUNICATION_CATEGORY_PATH,
        "product_media_name_required": False,
        "required": True,
    },
    "iqevse": {
        "label": "IQ EV Charger software",
        "docs_path": TARGET_CATEGORY_PATH,
        "product_media_name_required": True,
        "required": False,
    },
}

COMMERCIAL_ONLY_GATEWAY_TERMS = (
    "iq gateway commercial 2",
    "iq gateway commercial pro",
)
RESIDENTIAL_GATEWAY_TERMS = (
    "envoy s",
    "iq combiner",
    "iq gateway metered",
    "residential",
)

DEFAULT_TIMEOUT = 30
DEFAULT_MAX_PAGES = 40
SCHEMA_VERSION = 1
PREVIOUS_RUNTIME_CATALOG_URL = (
    "https://raw.githubusercontent.com/barneyonline/ha-enphase-energy/"
    "firmware-catalog/catalog/v1/runtime_catalog.json"
)

# Small deterministic alias map for parsing country applicability text.
COUNTRY_TOKEN_ALIASES: dict[str, str] = {
    "us": "US",
    "usa": "US",
    "unitedstates": "US",
    "unitedstatesofamerica": "US",
    "puertorico": "PR",
    "canada": "CA",
    "mexico": "MX",
    "australia": "AU",
    "newzealand": "NZ",
    "unitedkingdom": "GB",
    "uk": "GB",
    "greatbritain": "GB",
    "ireland": "IE",
    "germany": "DE",
    "austria": "AT",
    "france": "FR",
    "italy": "IT",
    "spain": "ES",
    "portugal": "PT",
    "netherlands": "NL",
    "belgium": "BE",
    "switzerland": "CH",
    "sweden": "SE",
    "norway": "NO",
    "finland": "FI",
    "denmark": "DK",
    "poland": "PL",
    "czechrepublic": "CZ",
    "czechia": "CZ",
    "slovakia": "SK",
    "romania": "RO",
    "hungary": "HU",
    "bulgaria": "BG",
    "greece": "GR",
    "cyprus": "CY",
    "turkiye": "TR",
    "turkey": "TR",
    "japan": "JP",
    "thailand": "TH",
    "philippines": "PH",
    "malaysia": "MY",
    "vietnam": "VN",
    "india": "IN",
    "croatia": "HR",
    "slovenia": "SI",
    "estonia": "EE",
    "latvia": "LV",
    "lithuania": "LT",
    "luxembourg": "LU",
    "malta": "MT",
    "dominicanrepublic": "DO",
    "bahamas": "BS",
    "colombia": "CO",
    "chile": "CL",
    "costarica": "CR",
    "panama": "PA",
    "brazil": "BR",
    "serbia": "RS",
    "southafrica": "ZA",
    "bermuda": "BM",
    "aruba": "AW",
    "frenchpolynesia": "PF",
    "monaco": "MC",
    "turksandcaicos": "TC",
}

REGION_COUNTRY_CODES: dict[str, set[str]] = {
    "europe": {
        "AL",
        "AT",
        "BE",
        "BG",
        "CH",
        "CY",
        "CZ",
        "DE",
        "DK",
        "EE",
        "ES",
        "FI",
        "FR",
        "GB",
        "GR",
        "HR",
        "HU",
        "IE",
        "IT",
        "LT",
        "LU",
        "LV",
        "MT",
        "NL",
        "NO",
        "PL",
        "PT",
        "RO",
        "SE",
        "SI",
        "SK",
        "TR",
    },
    "european": {
        "AL",
        "AT",
        "BE",
        "BG",
        "CH",
        "CY",
        "CZ",
        "DE",
        "DK",
        "EE",
        "ES",
        "FI",
        "FR",
        "GB",
        "GR",
        "HR",
        "HU",
        "IE",
        "IT",
        "LT",
        "LU",
        "LV",
        "MT",
        "NL",
        "NO",
        "PL",
        "PT",
        "RO",
        "SE",
        "SI",
        "SK",
        "TR",
    },
}

# Authoritative country/locale to region-site mapping supplied by maintainers.
REGION_SITE_ROUTE_ROWS: list[dict[str, str | None]] = [
    {
        "label": "United States",
        "country_code": "US",
        "locale": "en",
        "site_url": "https://enphase.com/",
    },
    {
        "label": "Canada",
        "country_code": "CA",
        "locale": "en-ca",
        "site_url": "https://enphase.com/",
    },
    {
        "label": "Anguilla",
        "country_code": "AI",
        "locale": "en-lac",
        "site_url": "https://enphase.com/en-lac/",
    },
    {
        "label": "Aruba",
        "country_code": "AW",
        "locale": "en-lac",
        "site_url": "https://enphase.com/en-lac/",
    },
    {
        "label": "Bermuda",
        "country_code": "BM",
        "locale": "en-bm",
        "site_url": "https://enphase.com/",
    },
    {
        "label": "Brazil",
        "country_code": "BR",
        "locale": "pt-br",
        "site_url": "https://enphase.com/pt-br/",
    },
    {
        "label": "British Virgin Islands",
        "country_code": "VG",
        "locale": "en-lac",
        "site_url": "https://enphase.com/en-lac/",
    },
    {
        "label": "Cayman Islands",
        "country_code": "KY",
        "locale": "en-lac",
        "site_url": "https://enphase.com/en-lac/",
    },
    {
        "label": "Colombia",
        "country_code": "CO",
        "locale": "es-lac",
        "site_url": "https://enphase.com/es-lac/",
    },
    {
        "label": "Chile",
        "country_code": "CL",
        "locale": "es-lac",
        "site_url": "https://enphase.com/es-lac/",
    },
    {
        "label": "Costa Rica",
        "country_code": "CR",
        "locale": "es-lac",
        "site_url": "https://enphase.com/es-lac/",
    },
    {
        "label": "Dominican Republic",
        "country_code": "DO",
        "locale": "es-do",
        "site_url": "https://enphase.com/es-do/",
    },
    {
        "label": "Jamaica",
        "country_code": "JM",
        "locale": "en-lac",
        "site_url": "https://enphase.com/en-lac/",
    },
    {
        "label": "Mexico",
        "country_code": "MX",
        "locale": "es-mx",
        "site_url": "https://enphase.com/es-mx/",
    },
    {
        "label": "Panama",
        "country_code": "PA",
        "locale": "es-lac",
        "site_url": "https://enphase.com/es-lac/",
    },
    {
        "label": "Puerto Rico",
        "country_code": "PR",
        "locale": "es-pr",
        "site_url": "https://enphase.com/",
    },
    {
        "label": "Sint Eustatius",
        "country_code": "BQ",
        "locale": "en-lac",
        "site_url": "https://enphase.com/en-lac/",
    },
    {
        "label": "Sint Maarten",
        "country_code": "SX",
        "locale": "en-lac",
        "site_url": "https://enphase.com/en-lac/",
    },
    {
        "label": "The Bahamas",
        "country_code": "BS",
        "locale": "en-lac",
        "site_url": "https://enphase.com/en-lac/",
    },
    {
        "label": "Turks & Caicos",
        "country_code": "TC",
        "locale": "en-lac",
        "site_url": "https://enphase.com/en-lac/",
    },
    {
        "label": "Albania",
        "country_code": "AL",
        "locale": "sq-al",
        "site_url": "https://enphase.com/sq-al/",
    },
    {
        "label": "Austria",
        "country_code": "AT",
        "locale": "de-at",
        "site_url": "https://enphase.com/de-at/",
    },
    {
        "label": "Belgium - Francais",
        "country_code": "BE",
        "locale": "fr-be",
        "site_url": "https://enphase.com/fr-be/",
    },
    {
        "label": "Belgium - Nederlands",
        "country_code": "BE",
        "locale": "nl-be",
        "site_url": "https://enphase.com/nl-be/",
    },
    {
        "label": "Bulgaria",
        "country_code": "BG",
        "locale": "bg-bg",
        "site_url": "https://enphase.com/bg-bg/",
    },
    {
        "label": "Croatia",
        "country_code": "HR",
        "locale": "hr-hr",
        "site_url": "https://enphase.com/hr-hr/",
    },
    {
        "label": "Cyprus (EL)",
        "country_code": "CY",
        "locale": "el-cy",
        "site_url": "https://enphase.com/el-cy/",
    },
    {
        "label": "Cyprus (TR)",
        "country_code": "CY",
        "locale": "tr-cy",
        "site_url": "https://enphase.com/tr-cy/",
    },
    {
        "label": "Czech Republic",
        "country_code": "CZ",
        "locale": "cz-cz",
        "site_url": "https://enphase.com/cz-cz/",
    },
    {
        "label": "Denmark",
        "country_code": "DK",
        "locale": "da-dk",
        "site_url": "https://enphase.com/da-dk/",
    },
    {
        "label": "Estonia",
        "country_code": "EE",
        "locale": "et-ee",
        "site_url": "https://enphase.com/et-ee/",
    },
    {
        "label": "Finland",
        "country_code": "FI",
        "locale": "en-fi",
        "site_url": "https://enphase.com/en-fi/",
    },
    {
        "label": "France",
        "country_code": "FR",
        "locale": "fr-fr",
        "site_url": "https://enphase.com/fr-fr/",
    },
    {
        "label": "Germany",
        "country_code": "DE",
        "locale": "de-de",
        "site_url": "https://enphase.com/de-de/",
    },
    {
        "label": "Greece",
        "country_code": "GR",
        "locale": "el-gr",
        "site_url": "https://enphase.com/el-gr/",
    },
    {
        "label": "Hungary",
        "country_code": "HU",
        "locale": "hu-hu",
        "site_url": "https://enphase.com/hu-hu/",
    },
    {
        "label": "Ireland",
        "country_code": "IE",
        "locale": "en-ie",
        "site_url": "https://enphase.com/en-ie/",
    },
    {
        "label": "Italy",
        "country_code": "IT",
        "locale": "it-it",
        "site_url": "https://enphase.com/it-it/",
    },
    {
        "label": "Latvia",
        "country_code": "LV",
        "locale": "lv-lv",
        "site_url": "https://enphase.com/lv-lv/",
    },
    {
        "label": "Lithuania",
        "country_code": "LT",
        "locale": "lt-lt",
        "site_url": "https://enphase.com/lt-lt/",
    },
    {
        "label": "Luxembourg",
        "country_code": "LU",
        "locale": "fr-lu",
        "site_url": "https://enphase.com/fr-lu/",
    },
    {
        "label": "Malta",
        "country_code": "MT",
        "locale": "en-mt",
        "site_url": "https://enphase.com/en-mt/",
    },
    {
        "label": "Netherlands",
        "country_code": "NL",
        "locale": "nl-nl",
        "site_url": "https://enphase.com/nl-nl/",
    },
    {
        "label": "Norway",
        "country_code": "NO",
        "locale": "nb-no",
        "site_url": "https://enphase.com/nb-no/",
    },
    {
        "label": "Poland",
        "country_code": "PL",
        "locale": "pl-pl",
        "site_url": "https://enphase.com/pl-pl/",
    },
    {
        "label": "Portugal",
        "country_code": "PT",
        "locale": "pt-pt",
        "site_url": "https://enphase.com/pt-pt/",
    },
    {
        "label": "Romania",
        "country_code": "RO",
        "locale": "ro-ro",
        "site_url": "https://enphase.com/ro-ro/",
    },
    {
        "label": "Slovakia",
        "country_code": "SK",
        "locale": "sk-sk",
        "site_url": "https://enphase.com/sk-sk/",
    },
    {
        "label": "Slovenia",
        "country_code": "SI",
        "locale": "sl-si",
        "site_url": "https://enphase.com/sl-si/",
    },
    {
        "label": "Spain",
        "country_code": "ES",
        "locale": "es-es",
        "site_url": "https://enphase.com/es-es/",
    },
    {
        "label": "Sweden",
        "country_code": "SE",
        "locale": "sv-se",
        "site_url": "https://enphase.com/sv-se/",
    },
    {
        "label": "Switzerland - Deutsch",
        "country_code": "CH",
        "locale": "de-ch",
        "site_url": "https://enphase.com/de-ch/",
    },
    {
        "label": "Switzerland - Francais",
        "country_code": "CH",
        "locale": "fr-ch",
        "site_url": "https://enphase.com/fr-ch/",
    },
    {
        "label": "Switzerland - Italiano",
        "country_code": "CH",
        "locale": "it-ch",
        "site_url": "https://enphase.com/it-ch/",
    },
    {
        "label": "Turkiye",
        "country_code": "TR",
        "locale": "tr-tr",
        "site_url": "https://enphase.com/tr-tr/",
    },
    {
        "label": "United Kingdom",
        "country_code": "GB",
        "locale": "en-gb",
        "site_url": "https://enphase.com/en-gb/",
    },
    {
        "label": "Australia",
        "country_code": "AU",
        "locale": "en-au",
        "site_url": "https://enphase.com/en-au/",
    },
    {
        "label": "Fiji",
        "country_code": "FJ",
        "locale": "en-au",
        "site_url": "https://enphase.com/en-au/",
    },
    {
        "label": "India",
        "country_code": "IN",
        "locale": "en-in",
        "site_url": "https://enphase.com/en-in/",
    },
    {
        "label": "Japan",
        "country_code": "JP",
        "locale": "ja-jp",
        "site_url": "https://enphase.com/ja-jp/",
    },
    {
        "label": "Malaysia",
        "country_code": "MY",
        "locale": "en-my",
        "site_url": "https://enphase.com/en-my/",
    },
    {
        "label": "New Caledonia",
        "country_code": "NC",
        "locale": "fr-nc",
        "site_url": "https://enphase.com/fr-nc/",
    },
    {
        "label": "New Zealand",
        "country_code": "NZ",
        "locale": "en-au",
        "site_url": "https://enphase.com/en-au/",
    },
    {
        "label": "Philippines",
        "country_code": "PH",
        "locale": "en-ph",
        "site_url": "https://enphase.com/en-ph/",
    },
    {
        "label": "Thailand",
        "country_code": "TH",
        "locale": "th-th",
        "site_url": "https://enphase.com/th-th/",
    },
    {
        "label": "Vietnam",
        "country_code": "VN",
        "locale": "vi-vn",
        "site_url": "https://enphase.com/vi-vn/",
    },
    {
        "label": "South Africa",
        "country_code": "ZA",
        "locale": "en-za",
        "site_url": "https://enphase.com/en-za/",
    },
    {
        "label": "Mauritius",
        "country_code": "MU",
        "locale": "en-sar",
        "site_url": "https://enphase.com/en-sar/",
    },
    {
        "label": "Namibia",
        "country_code": "NA",
        "locale": "en-sar",
        "site_url": "https://enphase.com/en-sar/",
    },
    {
        "label": "French Territories",
        "country_code": None,
        "locale": "fr-fot",
        "site_url": "https://enphase.com/fr-fot/",
    },
]


def _now_utc_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _collapse_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _strip_tags(value: str) -> str:
    return _collapse_ws(re.sub(r"<[^>]+>", " ", html.unescape(value)))


def _parse_date_to_iso(value: str) -> str | None:
    text = _collapse_ws(value).replace("\u00a0", " ")
    if not text:
        return None
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _version_sort_key(value: str | None) -> tuple[Any, ...]:
    if not value:
        return tuple()
    key: list[Any] = []
    for token in re.split(r"[^A-Za-z0-9]+", value):
        if not token:
            continue
        if token.isdigit():
            key.append(int(token))
        else:
            key.append(token.lower())
    return tuple(key)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _normalize_locale(locale: str | None) -> str:
    if not locale:
        return "en"
    text = locale.strip().lower().replace("_", "-")
    if not text:
        return "en"
    return text


def _with_query(url: str, params: dict[str, str]) -> str:
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(params)
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query),
            parsed.fragment,
        )
    )


def _normalize_country_code(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().upper()
    if len(text) == 2 and text.isalpha():
        return text
    return None


def _normalize_site_url(value: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError("site_url is required")
    if text.startswith("/"):
        text = f"{BASE_URL}{text}"
    if "://" not in text:
        text = f"https://{text.lstrip('/')}"

    parsed = urlsplit(text)
    if not parsed.netloc:
        raise ValueError(f"invalid site_url '{value}'")
    path = parsed.path or "/"
    if not path.endswith("/"):
        path += "/"
    return urlunsplit((parsed.scheme or "https", parsed.netloc, path, "", ""))


def _infer_locale_from_site_url(site_url: str) -> str | None:
    path = urlsplit(site_url).path.strip("/")
    if not path:
        return "en"
    if re.fullmatch(r"[a-z]{2}(?:-[a-z]{2,3})?", path.lower()):
        return _normalize_locale(path)
    return None


def build_region_site_routes(
    rows: list[dict[str, str | None]],
) -> list[dict[str, Any]]:
    routes: list[dict[str, Any]] = []
    for row in rows:
        label = _collapse_ws(str(row.get("label") or ""))
        if not label:
            continue

        site_url = _normalize_site_url(str(row.get("site_url") or ""))
        locale_raw = row.get("locale")
        locale = _normalize_locale(str(locale_raw)) if locale_raw else None
        query_locale_raw = row.get("query_locale")
        query_locale = (
            _normalize_locale(str(query_locale_raw))
            if query_locale_raw
            else (locale or _infer_locale_from_site_url(site_url) or "en")
        )

        country_code = _normalize_country_code(row.get("country_code"))
        routes.append(
            {
                "label": label,
                "country_code": country_code,
                "locale": locale,
                "site_url": site_url,
                "query_locale": query_locale,
                "target_key": f"{site_url}|{query_locale}",
            }
        )
    return routes


def build_crawl_targets(routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    targets_by_key: dict[str, dict[str, Any]] = {}
    for route in routes:
        key = str(route["target_key"])
        target = targets_by_key.get(key)
        if target is None:
            target = {
                "key": key,
                "site_url": route["site_url"],
                "query_locale": route["query_locale"],
                "routes": [],
            }
            targets_by_key[key] = target
        target["routes"].append(route)

    targets: list[dict[str, Any]] = []
    for target in targets_by_key.values():
        locales = sorted(
            {
                _normalize_locale(str(route["locale"]))
                for route in target["routes"]
                if route.get("locale")
            }
        )
        if not locales:
            locales = [_normalize_locale(str(target["query_locale"]))]

        countries = sorted(
            {
                str(route["country_code"])
                for route in target["routes"]
                if route.get("country_code")
            }
        )

        labels = [str(route["label"]) for route in target["routes"]]
        target["locales"] = locales
        target["countries"] = countries
        target["labels"] = labels
        targets.append(target)
    return targets


def _resolve_release_notes_topic_id(topic_facets: dict[str, int]) -> int | None:
    direct = topic_facets.get("Release notes")
    if direct is not None:
        return int(direct)

    for label, facet_id in topic_facets.items():
        if _slug(label) == "releasenotes":
            return int(facet_id)

    for facet_id in topic_facets.values():
        if int(facet_id) == 217:
            return 217
    return None


def _entry_with_locale_url(entry: dict[str, Any], locale: str | None) -> dict[str, Any]:
    cloned = dict(entry)
    urls = entry.get("urls_by_locale")
    urls_by_locale: dict[str, str] = dict(urls) if isinstance(urls, dict) else {}
    normalized_locale = _normalize_locale(locale)
    if normalized_locale not in urls_by_locale and urls_by_locale:
        urls_by_locale[normalized_locale] = str(next(iter(urls_by_locale.values())))
    cloned["urls_by_locale"] = urls_by_locale
    return cloned


def _is_global_fallback_entry(
    entry: dict[str, Any] | None,
    global_entry: dict[str, Any] | None,
) -> bool:
    if not isinstance(entry, dict) or not isinstance(global_entry, dict):
        return False
    return entry.get("media_id") == global_entry.get("media_id")


def _is_same_release_values(
    *,
    left_version: Any,
    right_version: Any,
    left_release_date: Any,
    right_release_date: Any,
    left_title: Any,
    right_title: Any,
    left_media_id: Any,
    right_media_id: Any,
) -> bool:
    if (
        left_version
        and left_release_date
        and left_version == right_version
        and left_release_date == right_release_date
    ):
        return True
    if (
        left_version
        and left_title
        and (left_version, left_title)
        == (
            right_version,
            right_title,
        )
    ):
        return True
    if left_media_id or right_media_id:
        return left_media_id == right_media_id
    return False


def _is_same_release_entry(
    left: dict[str, Any] | None,
    right: dict[str, Any] | None,
) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    return _is_same_release_values(
        left_version=left.get("version"),
        right_version=right.get("version"),
        left_release_date=left.get("release_date"),
        right_release_date=right.get("release_date"),
        left_title=left.get("title"),
        right_title=right.get("title"),
        left_media_id=left.get("media_id"),
        right_media_id=right.get("media_id"),
    )


def _should_replace_country_entry(
    existing: dict[str, Any] | None,
    candidate: dict[str, Any] | None,
    global_entry: dict[str, Any] | None,
) -> bool:
    if not isinstance(candidate, dict):
        return False
    if not isinstance(existing, dict):
        return True
    if _is_global_fallback_entry(
        existing, global_entry
    ) and not _is_global_fallback_entry(candidate, global_entry):
        return True
    if _is_global_fallback_entry(existing, global_entry) == _is_global_fallback_entry(
        candidate, global_entry
    ):
        existing_key = _version_sort_key(str(existing.get("version") or ""))
        candidate_key = _version_sort_key(str(candidate.get("version") or ""))
        return candidate_key > existing_key
    return False


@dataclass(slots=True)
class ReleaseCard:
    title: str
    version: str | None
    release_date: str | None
    media_id: str | None
    langcode: str
    summary: str
    countries_text: str | None


@dataclass(slots=True)
class CountryApplicability:
    include: set[str] = field(default_factory=set)
    exclude: set[str] = field(default_factory=set)
    all_countries: bool = False
    ambiguous: bool = False


class ReleaseCardParser(HTMLParser):
    """Parse release cards from the documentation listing HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.cards: list[ReleaseCard] = []
        self._card_depth = 0
        self._stack: list[dict[str, bool]] = []
        self._in_name = 0
        self._in_date = 0
        self._in_note = 0
        self._title_parts: list[str] = []
        self._date_parts: list[str] = []
        self._note_parts: list[str] = []
        self._media_id: str | None = None
        self._langcode: str | None = None

    @staticmethod
    def _class_tokens(attrs: dict[str, str]) -> set[str]:
        return {token for token in attrs.get("class", "").split() if token}

    @staticmethod
    def _parse_x_data(value: str) -> tuple[str | None, str | None]:
        media_match = re.search(r"media_id\s*:\s*['\"]([^'\"]+)['\"]", value)
        lang_match = re.search(r"langcode\s*:\s*['\"]([^'\"]+)['\"]", value)
        media_id = media_match.group(1).strip() if media_match else None
        langcode = lang_match.group(1).strip() if lang_match else None
        return media_id, langcode

    @staticmethod
    def _extract_country_text(note_text: str) -> str | None:
        text = _collapse_ws(note_text)
        if not text:
            return None

        for label in (
            "Countries",
            "Geographies",
            "Supported countries",
            "Supported regions",
        ):
            match = re.search(
                rf"\b{re.escape(label)}\s*:\s*",
                text,
                flags=re.IGNORECASE,
            )
            if match is None:
                continue
            tail = text[match.end() :].strip()
            if not tail:
                continue
            # Stop at likely next field labels in structured release notes.
            stops = [
                "# Feature classification",
                "Batteries supported:",
                "Platforms supported:",
                "Microinverters supported:",
                "Gateways supported:",
                "Supported system configurations:",
                "Release notes:",
                "Release note:",
                "Applicable devices:",
                "Feature classification:",
                "Feature classification",
                "Features:",
            ]
            end = len(tail)
            for stop in stops:
                stop_idx = tail.lower().find(stop.lower())
                if stop_idx >= 0:
                    end = min(end, stop_idx)
            value = tail[:end].strip(" .;,")
            if value:
                return value

        # Fallback for simpler sentence-style notes.
        match = re.search(
            r"\b(?:Countries|Geographies)\s*:\s*([^\n]+)",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            value = match.group(1).strip(" .;,")
            return value or None

        return None

    def _reset_card(self) -> None:
        self._title_parts = []
        self._date_parts = []
        self._note_parts = []
        self._media_id = None
        self._langcode = None

    def _flush_card(self) -> None:
        title = _collapse_ws(" ".join(self._title_parts))
        canonical_title_match = re.search(
            r"(.+?release notes\s*\([^()]+\))",
            title,
            flags=re.IGNORECASE,
        )
        if canonical_title_match:
            title = canonical_title_match.group(1).strip()
        if not title:
            return
        version_match = re.search(r"\(([^()]+)\)\s*$", title)
        version = version_match.group(1).strip() if version_match else None
        release_date = _parse_date_to_iso(" ".join(self._date_parts))
        full_summary = _collapse_ws(" ".join(self._note_parts))
        countries_text = self._extract_country_text(full_summary)
        summary = full_summary
        if len(summary) > 500:
            summary = summary[:497].rstrip() + "..."
        card = ReleaseCard(
            title=title,
            version=version,
            release_date=release_date,
            media_id=self._media_id,
            langcode=self._langcode or "und",
            summary=summary,
            countries_text=countries_text,
        )
        self.cards.append(card)

    def handle_starttag(
        self, tag: str, attrs_tuples: list[tuple[str, str | None]]
    ) -> None:
        attrs = {key: value or "" for key, value in attrs_tuples}
        classes = self._class_tokens(attrs)

        if self._card_depth == 0 and tag == "div" and "release-item" in classes:
            self._card_depth = 1
            self._stack.clear()
            self._in_name = 0
            self._in_date = 0
            self._in_note = 0
            self._reset_card()
            self._stack.append({"name": False, "date": False, "note": False})
            return

        if self._card_depth == 0:
            return

        entered_name = tag == "div" and "release-item__name" in classes
        entered_date = tag == "div" and "release-item__date" in classes
        entered_note = tag == "div" and "release-item__note" in classes
        if entered_name:
            self._in_name += 1
        if entered_date:
            self._in_date += 1
        if entered_note:
            self._in_note += 1

        if tag == "button" and "document-copy-link" in classes:
            media_id, langcode = self._parse_x_data(attrs.get("x-data", ""))
            if media_id:
                self._media_id = media_id
            if langcode:
                self._langcode = langcode

        self._stack.append(
            {"name": entered_name, "date": entered_date, "note": entered_note}
        )
        self._card_depth += 1

    def handle_endtag(self, _tag: str) -> None:
        if self._card_depth == 0:
            return

        self._card_depth -= 1
        context = (
            self._stack.pop()
            if self._stack
            else {"name": False, "date": False, "note": False}
        )
        if context.get("name"):
            self._in_name = max(0, self._in_name - 1)
        if context.get("date"):
            self._in_date = max(0, self._in_date - 1)
        if context.get("note"):
            self._in_note = max(0, self._in_note - 1)

        if self._card_depth == 0:
            self._flush_card()

    def handle_data(self, data: str) -> None:
        if self._card_depth == 0:
            return
        if self._in_name > 0:
            self._title_parts.append(data)
        if self._in_date > 0:
            self._date_parts.append(data)
        if self._in_note > 0:
            self._note_parts.append(data)


def fetch_text(url: str, *, timeout: int = DEFAULT_TIMEOUT) -> str:
    request = Request(url, headers={"User-Agent": "ha-enphase-ev-firmware-catalog/1.0"})
    with urlopen(request, timeout=timeout) as response:
        content_type = response.headers.get("content-type", "")
        charset_match = re.search(r"charset=([\w\-]+)", content_type, flags=re.I)
        charset = charset_match.group(1) if charset_match else "utf-8"
        return response.read().decode(charset, errors="replace")


def discover_apps_entrypoint(root_html: str) -> tuple[str, str]:
    """Return (apps_path, product_type) discovered from root documentation page."""
    matches = re.finditer(
        r"<a[^>]+href=\"([^\"]*product_type=(\d+)[^\"]*)\"[^>]*aria-label=\"([^\"]+)\"",
        root_html,
        flags=re.IGNORECASE,
    )
    for match in matches:
        href, product_type, aria_label = match.groups()
        if TARGET_CATEGORY_LABEL.lower() not in html.unescape(aria_label).lower():
            continue
        parsed = urlparse(html.unescape(href))
        path = parsed.path or TARGET_CATEGORY_PATH
        if not path.endswith("/apps"):
            path = TARGET_CATEGORY_PATH
        return path, product_type

    # Fallback to known category path if card structure changes.
    return TARGET_CATEGORY_PATH, "216"


def parse_product_type_from_apps_page(apps_html: str) -> str | None:
    match = re.search(r"productType\s*:\s*'?(\d+)'?", apps_html)
    if match:
        return match.group(1)
    match = re.search(r"[?&]product_type=(\d+)", apps_html)
    return match.group(1) if match else None


def parse_facet_values(apps_html: str, alias: str) -> dict[str, int]:
    section_match = re.search(
        rf"<ul[^>]+data-drupal-facet-alias=\"{re.escape(alias)}\"[^>]*>(.*?)</ul>",
        apps_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not section_match:
        return {}
    section = section_match.group(1)
    values: dict[str, int] = {}
    for match in re.finditer(
        r"data-drupal-facet-item-value=\"(\d+)\"[^>]*>\s*<span class=\"facet-item__value\">(.*?)</span>",
        section,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        raw_id, raw_label = match.groups()
        label = _strip_tags(raw_label)
        if not label:
            continue
        values[label] = int(raw_id)
    return values


def parse_language_options(apps_html: str, select_name: str) -> dict[str, str]:
    match = re.search(
        rf"<select[^>]*name=\"{re.escape(select_name)}\"[^>]*>(.*?)</select>",
        apps_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return {}

    options_html = match.group(1)
    options: dict[str, str] = {}
    for option_match in re.finditer(
        r"<option[^>]*value=\"([^\"]*)\"[^>]*>(.*?)</option>",
        options_html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        raw_value, raw_label = option_match.groups()
        value = _normalize_locale(html.unescape(raw_value))
        label = _strip_tags(raw_label)
        if not value or value == "all" or not label:
            continue
        options[value] = label
    return options


def find_next_page_url(current_url: str, page_html: str) -> str | None:
    match = re.search(r"<a[^>]+href=\"([^\"]+)\"[^>]*rel=\"next\"", page_html)
    if not match:
        return None
    href = html.unescape(match.group(1))
    return urljoin(current_url, href)


def parse_release_cards(page_html: str) -> list[ReleaseCard]:
    parser = ReleaseCardParser()
    parser.feed(page_html)
    return parser.cards


def _country_tokens(value: str) -> list[str]:
    cleaned = (
        value.replace(" and ", ",")
        .replace("&", ",")
        .replace("/", ",")
        .replace(";", ",")
    )
    return [token.strip() for token in cleaned.split(",") if token.strip()]


def _country_label_to_names(label: str) -> list[str]:
    """Extract one or more country names from a region selector label."""
    name = _collapse_ws(label.split("(", 1)[0])
    if not name:
        return []
    if name.lower() in {"latin america", "southern african region", "europe"}:
        return []
    return _country_tokens(name)


def _token_to_iso(token: str) -> str | None:
    token = re.sub(r"^the\s+", "", token.strip(), flags=re.IGNORECASE)
    normalized = _slug(token)
    if not normalized:
        return None
    if len(normalized) == 2:
        return normalized.upper()
    return COUNTRY_TOKEN_ALIASES.get(normalized)


def build_region_country_mapping(options: dict[str, str]) -> dict[str, dict[str, Any]]:
    mapping: dict[str, dict[str, Any]] = {}
    for locale, label in options.items():
        names = _country_label_to_names(label)
        derived_iso = locale.split("-")[-1].upper() if "-" in locale else "US"
        iso_codes: list[str] = []
        for name in names:
            iso = _token_to_iso(name)
            if iso and iso not in iso_codes:
                iso_codes.append(iso)

        # Fallback for single-country locale variants.
        if not iso_codes and len(derived_iso) == 2 and derived_iso.isalpha():
            if label.split("(", 1)[0].strip().lower() not in {
                "latin america",
                "southern african region",
                "europe",
                "french territories",
            }:
                iso_codes.append(derived_iso)

        if len(iso_codes) == 1:
            mapping[label] = {
                "type": "country_variant",
                "iso_codes": iso_codes,
                "locale": locale,
            }
        elif len(iso_codes) > 1:
            mapping[label] = {
                "type": "multi_country_region",
                "iso_codes": iso_codes,
                "locale": locale,
            }
        else:
            mapping[label] = {
                "type": "aggregate_region",
                "region_code": _slug(label.upper())[:16] or "REGION",
                "locale": locale,
            }
    return mapping


def build_country_alias_map(
    region_mapping: dict[str, dict[str, Any]],
) -> dict[str, str]:
    aliases = dict(COUNTRY_TOKEN_ALIASES)
    for label, info in region_mapping.items():
        iso_codes = [str(code).upper() for code in info.get("iso_codes", []) if code]
        if not iso_codes:
            continue
        names = _country_label_to_names(label)
        for idx, name in enumerate(names):
            if idx >= len(iso_codes):
                break
            aliases[_slug(name)] = iso_codes[idx]
    return aliases


def parse_country_applicability(
    countries_text: str | None,
    *,
    alias_map: dict[str, str],
) -> CountryApplicability:
    if not countries_text:
        return CountryApplicability(ambiguous=True)

    text = _collapse_ws(countries_text)
    lower = text.lower()
    if not text:
        return CountryApplicability(ambiguous=True)
    if "..." in text:
        return CountryApplicability(ambiguous=True)

    if "global" in lower or "worldwide" in lower:
        return CountryApplicability(all_countries=True)

    all_except_match = re.search(
        r"\ball(?P<prefix>[^.]*)countries?\s+except\s+(?P<exclusions>.+)$",
        text,
        flags=re.I,
    )
    if all_except_match:
        prefix = _collapse_ws(all_except_match.group("prefix"))
        excluded_raw = all_except_match.group("exclusions")
        excluded: set[str] = set()
        for token in _country_tokens(excluded_raw):
            iso = alias_map.get(_slug(token)) or _token_to_iso(token)
            if iso:
                excluded.add(iso)
        prefix_is_plain_all = not prefix or prefix.lower() in {"the"}
        if not prefix_is_plain_all:
            region_codes = REGION_COUNTRY_CODES.get(_slug(prefix))
            if region_codes:
                return CountryApplicability(include=region_codes - excluded)
            return CountryApplicability(exclude=excluded, ambiguous=True)
        if excluded:
            return CountryApplicability(all_countries=True, exclude=excluded)
        return CountryApplicability(ambiguous=True)

    includes: set[str] = set()
    unknown_tokens = 0
    for token in _country_tokens(text):
        iso = alias_map.get(_slug(token)) or _token_to_iso(token)
        if iso:
            includes.add(iso)
        else:
            unknown_tokens += 1

    if includes and unknown_tokens == 0:
        return CountryApplicability(include=includes)
    if includes and unknown_tokens > 0:
        return CountryApplicability(include=includes, ambiguous=True)
    return CountryApplicability(ambiguous=True)


def country_applicability_matches(
    applicability: CountryApplicability,
    country_code: str | None,
) -> bool:
    normalized = country_code.upper() if isinstance(country_code, str) else None
    if applicability.include:
        return bool(normalized and normalized in applicability.include)
    if applicability.all_countries:
        return not (normalized and normalized in applicability.exclude)
    if applicability.exclude:
        return False
    return applicability.ambiguous


def country_applicability_is_global_fallback(
    applicability: CountryApplicability,
) -> bool:
    if applicability.include or applicability.exclude:
        return False
    return applicability.all_countries or applicability.ambiguous


def release_applies_to_country(
    card: ReleaseCard,
    country_code: str | None,
    *,
    alias_map: dict[str, str],
) -> bool:
    applicability = parse_country_applicability(
        card.countries_text, alias_map=alias_map
    )
    return country_applicability_matches(applicability, country_code)


def release_applies_to_device(card: ReleaseCard, device_key: str) -> bool:
    if device_key != "envoy":
        return True

    text = _collapse_ws(f"{card.title} {card.summary}").lower()
    if not any(term in text for term in COMMERCIAL_ONLY_GATEWAY_TERMS):
        return True
    return any(term in text for term in RESIDENTIAL_GATEWAY_TERMS)


def _is_same_release_card(left: ReleaseCard, right: ReleaseCard) -> bool:
    return _is_same_release_values(
        left_version=left.version,
        right_version=right.version,
        left_release_date=left.release_date,
        right_release_date=right.release_date,
        left_title=left.title,
        right_title=right.title,
        left_media_id=left.media_id,
        right_media_id=right.media_id,
    )


def _version_family(value: str | None) -> tuple[int, ...] | None:
    family: list[int] = []
    for part in _version_sort_key(value):
        if isinstance(part, int):
            family.append(part)
            if len(family) == 2:
                return tuple(family)
    return tuple(family) if family else None


def pick_latest_release(releases: list[ReleaseCard]) -> ReleaseCard | None:
    if not releases:
        return None

    def _sort_key(card: ReleaseCard) -> tuple[datetime, tuple[Any, ...], str]:
        if card.release_date:
            date_obj = datetime.strptime(card.release_date, "%Y-%m-%d")
        else:
            date_obj = datetime(1970, 1, 1)
        return (date_obj, _version_sort_key(card.version), card.media_id or "")

    return max(releases, key=_sort_key)


def _release_is_newer(candidate: ReleaseCard, baseline: ReleaseCard) -> bool:
    if _is_same_release_card(candidate, baseline):
        return False
    return pick_latest_release([baseline, candidate]) is candidate


def _is_stale_global_fallback(
    card: ReleaseCard,
    latest_known: ReleaseCard | None,
    *,
    alias_map: dict[str, str],
) -> bool:
    if latest_known is None or not _release_is_newer(latest_known, card):
        return False

    applicability = parse_country_applicability(
        card.countries_text, alias_map=alias_map
    )
    if not country_applicability_is_global_fallback(applicability):
        return False

    card_family = _version_family(card.version)
    latest_family = _version_family(latest_known.version)
    return bool(card_family and latest_family and card_family != latest_family)


def _is_global_fallback_card(card: ReleaseCard, *, alias_map: dict[str, str]) -> bool:
    return country_applicability_is_global_fallback(
        parse_country_applicability(card.countries_text, alias_map=alias_map)
    )


def pick_latest_release_for_country(
    releases: list[ReleaseCard],
    country_code: str,
    *,
    alias_map: dict[str, str],
    latest_known: ReleaseCard | None = None,
) -> ReleaseCard | None:
    applicable = [
        card
        for card in releases
        if release_applies_to_country(card, country_code, alias_map=alias_map)
    ]
    latest = pick_latest_release(applicable)
    if latest is not None and _is_stale_global_fallback(
        latest, latest_known, alias_map=alias_map
    ):
        return None
    return latest


def pick_latest_global_release(
    releases: list[ReleaseCard],
    *,
    alias_map: dict[str, str],
    latest_known: ReleaseCard | None = None,
) -> ReleaseCard | None:
    applicable = []
    for card in releases:
        country_applicability = parse_country_applicability(
            card.countries_text, alias_map=alias_map
        )
        if country_applicability_is_global_fallback(country_applicability):
            applicable.append(card)
    latest = pick_latest_release(applicable)
    if latest is not None and _is_stale_global_fallback(
        latest, latest_known, alias_map=alias_map
    ):
        return None
    return latest


def build_release_urls_by_locale(
    *,
    locales: list[str],
    apps_url: str,
    product_type: str,
    topic_id: int,
    product_media_name_id: int | None,
) -> dict[str, str]:
    urls: dict[str, str] = {}
    for locale in locales:
        normalized = _normalize_locale(locale)
        query = {
            "product_type": product_type,
            "f[0]": f"document:{topic_id}",
            "search_api_language": normalized,
            "field_alternative_language": normalized,
        }
        if product_media_name_id is not None:
            query["f[1]"] = f"product_media_name:{product_media_name_id}"
        urls[normalized] = _with_query(
            apps_url,
            query,
        )
    return urls


def card_to_runtime_entry(
    *,
    card: ReleaseCard,
    product_type: str,
    topic_id: int,
    product_media_name_id: int | None,
    locales: list[str],
    apps_url: str,
) -> dict[str, Any]:
    media_id = card.media_id or ""
    urls_by_locale = (
        build_release_urls_by_locale(
            locales=locales,
            apps_url=apps_url,
            product_type=product_type,
            topic_id=topic_id,
            product_media_name_id=product_media_name_id,
        )
        if media_id
        else {}
    )
    return {
        "version": card.version,
        "release_date": card.release_date,
        "media_id": media_id or None,
        "product_type": product_type,
        "document_topic_id": topic_id,
        "product_media_name_id": product_media_name_id,
        "countries_text": card.countries_text,
        "urls_by_locale": urls_by_locale,
        "summary": card.summary,
        "title": card.title,
    }


def crawl_release_cards(
    *,
    apps_url: str,
    product_type: str,
    topic_id: int,
    product_media_name_id: int | None,
    search_locale: str,
    timeout: int,
    max_pages: int,
) -> tuple[list[ReleaseCard], list[str]]:
    query = {
        "product_type": product_type,
        "f[0]": f"document:{topic_id}",
        "search_api_language": _normalize_locale(search_locale),
        "field_alternative_language": _normalize_locale(search_locale),
        "page": "0",
    }
    if product_media_name_id is not None:
        query["f[1]"] = f"product_media_name:{product_media_name_id}"
    page_url = _with_query(
        apps_url,
        query,
    )

    cards: list[ReleaseCard] = []
    seen_pages: set[str] = set()
    visited_pages: list[str] = []

    while page_url and page_url not in seen_pages and len(seen_pages) < max_pages:
        seen_pages.add(page_url)
        visited_pages.append(page_url)
        page_html = fetch_text(page_url, timeout=timeout)
        cards.extend(parse_release_cards(page_html))
        page_url = find_next_page_url(page_url, page_html)

    deduped: dict[tuple[str | None, str | None, str | None, str], ReleaseCard] = {}
    for card in cards:
        key = (card.media_id, card.version, card.release_date, card.title)
        if key not in deduped:
            deduped[key] = card
    return list(deduped.values()), visited_pages


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def fetch_json(url: str, *, timeout: int = DEFAULT_TIMEOUT) -> Any:
    return json.loads(fetch_text(url, timeout=timeout))


def fetch_previous_runtime_catalog(
    *, timeout: int = DEFAULT_TIMEOUT
) -> dict[str, Any] | None:
    try:
        payload = fetch_json(PREVIOUS_RUNTIME_CATALOG_URL, timeout=timeout)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _previous_catalog_device(
    previous_catalog: dict[str, Any] | None, device_key: str
) -> dict[str, Any] | None:
    devices = (
        previous_catalog.get("devices") if isinstance(previous_catalog, dict) else None
    )
    if not isinstance(devices, dict):
        return None
    device = devices.get(device_key)
    return device if isinstance(device, dict) else None


def _previous_catalog_source_device(
    previous_catalog: dict[str, Any] | None, device_key: str
) -> dict[str, Any] | None:
    source = (
        previous_catalog.get("source") if isinstance(previous_catalog, dict) else None
    )
    source_devices = source.get("devices") if isinstance(source, dict) else None
    if not isinstance(source_devices, dict):
        return None
    source_device = source_devices.get(device_key)
    return source_device if isinstance(source_device, dict) else None


def _bootstrap_target(
    target: dict[str, Any], *, timeout: int, docs_path: str
) -> dict[str, Any]:
    root_url = urljoin(str(target["site_url"]), ROOT_PATH.lstrip("/"))
    root_html = fetch_text(root_url, timeout=timeout)
    discovered_product_type = DEFAULT_PRODUCT_TYPE
    if docs_path == TARGET_CATEGORY_PATH:
        discovered_docs_path, discovered_product_type = discover_apps_entrypoint(
            root_html
        )
        docs_path = discovered_docs_path

    apps_url = urljoin(str(target["site_url"]), docs_path.lstrip("/"))
    apps_bootstrap_url = (
        _with_query(apps_url, {"product_type": discovered_product_type})
        if docs_path == TARGET_CATEGORY_PATH
        else apps_url
    )
    apps_html = fetch_text(apps_bootstrap_url, timeout=timeout)

    product_type = (
        parse_product_type_from_apps_page(apps_html) or discovered_product_type
    )
    product_facets = parse_facet_values(apps_html, "product_media_name")
    topic_facets = parse_facet_values(apps_html, "document")
    topic_id = _resolve_release_notes_topic_id(topic_facets)
    if topic_id is None:
        raise RuntimeError(
            f"Could not discover release-notes topic id from apps page: {apps_bootstrap_url}"
        )

    product_ids = {
        device_key: product_facets.get(product_meta["label"])
        for device_key, product_meta in TARGET_PRODUCTS.items()
    }
    return {
        "root_url": root_url,
        "docs_path": docs_path,
        "apps_url": apps_url,
        "apps_html": apps_html,
        "apps_bootstrap_url": apps_bootstrap_url,
        "product_type": product_type,
        "topic_id": int(topic_id),
        "topic_facets": topic_facets,
        "product_facets": product_facets,
        "product_ids": product_ids,
    }


def catalogs_equal_ignoring_generated_at(
    current_catalog: dict[str, Any],
    previous_catalog: dict[str, Any] | None,
) -> bool:
    if not isinstance(previous_catalog, dict):
        return False
    current_normalized = dict(current_catalog)
    previous_normalized = dict(previous_catalog)
    current_normalized.pop("generated_at", None)
    previous_normalized.pop("generated_at", None)
    return current_normalized == previous_normalized


def choose_generated_at(
    *,
    current_catalog: dict[str, Any],
    previous_catalog: dict[str, Any] | None,
    fallback_generated_at: str,
) -> str:
    if not catalogs_equal_ignoring_generated_at(current_catalog, previous_catalog):
        return fallback_generated_at
    if isinstance(previous_catalog, dict):
        previous_generated = previous_catalog.get("generated_at")
        if isinstance(previous_generated, str) and previous_generated.strip():
            return previous_generated.strip()
    return fallback_generated_at


def build_catalog(output_dir: Path, *, timeout: int, max_pages: int) -> None:
    generated_at = _now_utc_iso()
    routes = build_region_site_routes(REGION_SITE_ROUTE_ROWS)
    if not routes:
        raise RuntimeError("No authoritative region-site routes configured")

    crawl_targets = build_crawl_targets(routes)
    global_target_key = next(
        (
            str(route["target_key"])
            for route in routes
            if route.get("country_code") == "US"
        ),
        str(routes[0]["target_key"]),
    )

    targets_by_key: dict[str, dict[str, Any]] = {
        str(target["key"]): target for target in crawl_targets
    }
    global_base_target = targets_by_key.get(global_target_key)
    if not isinstance(global_base_target, dict):
        raise RuntimeError("Global routing target is missing")

    all_country_codes: set[str] = {
        str(route["country_code"]) for route in routes if route.get("country_code")
    }
    country_alias_map = dict(COUNTRY_TOKEN_ALIASES)
    for route in routes:
        route_country = route.get("country_code")
        route_label = route.get("label")
        if not route_country or not route_label:
            continue
        for name in _country_label_to_names(str(route_label)):
            country_alias_map.setdefault(_slug(name), str(route_country))

    devices_catalog: dict[str, Any] = {}
    crawl_meta: dict[str, Any] = {}
    source_devices: dict[str, Any] = {}
    global_root_url = urljoin(
        str(global_base_target["site_url"]), ROOT_PATH.lstrip("/")
    )
    global_apps_url = ""
    global_product_type = DEFAULT_PRODUCT_TYPE
    global_topic_facets: dict[str, Any] = {}
    global_product_facets: dict[str, Any] = {}
    language_options: dict[str, str] = {}
    alt_language_options: dict[str, str] = {}
    previous_runtime_catalog = fetch_previous_runtime_catalog(timeout=timeout)

    for device_key, product_meta in TARGET_PRODUCTS.items():
        device_targets = [dict(target) for target in crawl_targets]
        device_targets_by_key: dict[str, dict[str, Any]] = {
            str(target["key"]): target for target in device_targets
        }
        global_target = device_targets_by_key[global_target_key]

        docs_path = str(product_meta["docs_path"])
        global_target.update(
            _bootstrap_target(global_target, timeout=timeout, docs_path=docs_path)
        )
        global_target["bootstrap_error"] = None

        product_media_name_required = bool(
            product_meta.get("product_media_name_required", True)
        )
        global_product_id_raw = global_target["product_ids"].get(device_key)
        if global_product_id_raw is None and product_media_name_required:
            missing_message = (
                f"Could not discover product id for '{product_meta['label']}'"
            )
            if bool(product_meta.get("required")):
                raise RuntimeError(missing_message)
            previous_device = _previous_catalog_device(
                previous_runtime_catalog, device_key
            )
            if isinstance(previous_device, dict):
                devices_catalog[device_key] = previous_device
            previous_source_device = _previous_catalog_source_device(
                previous_runtime_catalog, device_key
            )
            if isinstance(previous_source_device, dict):
                source_devices[device_key] = previous_source_device
            crawl_meta[device_key] = {
                "count": 0,
                "targets": {},
                "missing_product_media_id_targets": [global_target_key],
                "used_global_product_media_id_targets": [],
                "empty_release_targets": [],
                "bootstrap_error_targets": [],
                "crawl_error_targets": [],
                "skipped": True,
                "skip_reason": missing_message,
                "using_previous_catalog_device": isinstance(previous_device, dict),
            }
            _LOGGER.warning(
                "Firmware catalog skipping device %s: %s",
                device_key,
                missing_message,
            )
            continue
        global_product_id = (
            int(global_product_id_raw) if global_product_id_raw is not None else None
        )

        for target in device_targets:
            if str(target["key"]) == global_target_key:
                continue
            try:
                target.update(
                    _bootstrap_target(target, timeout=timeout, docs_path=docs_path)
                )
                target["bootstrap_error"] = None
            except Exception as err:  # noqa: BLE001
                target.update(
                    {
                        "root_url": global_target["root_url"],
                        "docs_path": global_target["docs_path"],
                        "apps_url": global_target["apps_url"],
                        "apps_html": global_target["apps_html"],
                        "apps_bootstrap_url": global_target["apps_bootstrap_url"],
                        "product_type": global_target["product_type"],
                        "topic_id": global_target["topic_id"],
                        "topic_facets": dict(global_target["topic_facets"]),
                        "product_facets": dict(global_target["product_facets"]),
                        "product_ids": dict(global_target["product_ids"]),
                        "bootstrap_error": str(err),
                    }
                )
                _LOGGER.warning(
                    "Firmware catalog bootstrap failed for target %s device %s; falling back to global metadata: %s",
                    target.get("key"),
                    device_key,
                    err,
                )

        global_topic_id = int(global_target["topic_id"])
        global_product_type = str(global_target["product_type"])
        global_apps_url = str(global_target["apps_url"])
        global_topic_facets = dict(global_target["topic_facets"])
        global_product_facets = dict(global_target["product_facets"])

        if not language_options:
            global_apps_html = str(global_target["apps_html"])
            language_options = parse_language_options(
                global_apps_html, "search_api_language"
            )
            alt_language_options = parse_language_options(
                global_apps_html, "field_alternative_language"
            )

        target_cards: dict[str, list[ReleaseCard]] = {}
        target_product_ids: dict[str, int | None] = {}
        target_crawl: dict[str, Any] = {}
        total_count = 0
        missing_product_ids: list[str] = []
        fallback_id_targets: list[str] = []
        empty_release_targets: list[str] = []
        bootstrap_error_targets: list[str] = []
        crawl_error_targets: list[str] = []

        for target in device_targets:
            target_product_id = target["product_ids"].get(device_key)
            used_global_product_id = (
                target_product_id is None and global_product_id is not None
            )
            if target_product_id is None:
                missing_product_ids.append(str(target["key"]))
            product_id = (
                int(target_product_id)
                if target_product_id is not None
                else global_product_id
            )
            if used_global_product_id and str(target["key"]) != global_target_key:
                fallback_id_targets.append(str(target["key"]))

            if target.get("bootstrap_error"):
                bootstrap_error_targets.append(str(target["key"]))
            try:
                cards, visited_pages = crawl_release_cards(
                    apps_url=str(target["apps_url"]),
                    product_type=str(target["product_type"]),
                    topic_id=int(target["topic_id"]),
                    product_media_name_id=product_id,
                    search_locale=str(target["query_locale"]),
                    timeout=timeout,
                    max_pages=max_pages,
                )
                crawl_error: str | None = None
            except Exception as err:  # noqa: BLE001
                cards, visited_pages = [], []
                crawl_error = str(err)
                crawl_error_targets.append(str(target["key"]))
                _LOGGER.warning(
                    "Firmware catalog crawl failed for target %s device %s; treating as empty result: %s",
                    target.get("key"),
                    device_key,
                    err,
                )
            cards = [
                card for card in cards if release_applies_to_device(card, device_key)
            ]
            total_count += len(cards)
            target_crawl[str(target["key"])] = {
                "site_url": target["site_url"],
                "query_locale": target["query_locale"],
                "apps_url": target["apps_url"],
                "pages": visited_pages,
                "count": len(cards),
                "product_media_name_id": product_id,
                "used_global_product_media_name_id": used_global_product_id,
                "bootstrap_error": target.get("bootstrap_error"),
                "crawl_error": crawl_error,
            }
            if len(cards) == 0:
                empty_release_targets.append(str(target["key"]))

            target_key = str(target["key"])
            target_cards[target_key] = cards
            target_product_ids[target_key] = product_id

        latest_known_card = pick_latest_release(
            [card for cards in target_cards.values() for card in cards]
        )

        def _latest_applicable_card_for_route(
            route: dict[str, Any],
        ) -> tuple[ReleaseCard, str] | None:
            target_key = str(route["target_key"])
            route_country = route.get("country_code")

            def _latest_for_target(source_target_key: str) -> ReleaseCard | None:
                cards = target_cards.get(source_target_key, [])
                if isinstance(route_country, str):
                    return pick_latest_release_for_country(
                        cards,
                        route_country,
                        alias_map=country_alias_map,
                        latest_known=latest_known_card,
                    )
                return pick_latest_global_release(
                    cards,
                    alias_map=country_alias_map,
                    latest_known=latest_known_card,
                )

            route_card = _latest_for_target(target_key)
            if target_key == global_target_key:
                return (route_card, target_key) if route_card is not None else None

            global_card = _latest_for_target(global_target_key)
            if route_card is None:
                return (
                    (global_card, global_target_key)
                    if global_card is not None
                    else None
                )
            if global_card is None or _is_same_release_card(route_card, global_card):
                return route_card, target_key

            route_is_fallback = _is_global_fallback_card(
                route_card, alias_map=country_alias_map
            )
            global_is_fallback = _is_global_fallback_card(
                global_card, alias_map=country_alias_map
            )
            if route_is_fallback and not global_is_fallback:
                return global_card, global_target_key

            route_family = _version_family(route_card.version)
            global_family = _version_family(global_card.version)
            if route_family and route_family == global_family:
                return route_card, target_key

            latest_card = pick_latest_release([route_card, global_card])
            if latest_card is global_card:
                return global_card, global_target_key
            return route_card, target_key

        def _entry_for_route(route: dict[str, Any]) -> dict[str, Any] | None:
            selected_card = _latest_applicable_card_for_route(route)
            if selected_card is None:
                return None
            latest_card, target_key = selected_card
            target = device_targets_by_key[target_key]
            product_id = target_product_ids[target_key]
            return card_to_runtime_entry(
                card=latest_card,
                product_type=str(target["product_type"]),
                topic_id=int(target["topic_id"]),
                product_media_name_id=product_id,
                locales=list(target["locales"]),
                apps_url=str(target["apps_url"]),
            )

        latest_global_card = pick_latest_global_release(
            target_cards.get(global_target_key, []),
            alias_map=country_alias_map,
            latest_known=latest_known_card,
        )
        latest_global_entry = (
            card_to_runtime_entry(
                card=latest_global_card,
                product_type=str(global_target["product_type"]),
                topic_id=int(global_target["topic_id"]),
                product_media_name_id=target_product_ids[global_target_key],
                locales=list(global_target["locales"]),
                apps_url=global_apps_url,
            )
            if latest_global_card
            else None
        )
        latest_global = (
            _entry_with_locale_url(latest_global_entry, "en")
            if isinstance(latest_global_entry, dict)
            else None
        )

        latest_by_locale: dict[str, Any] = {}
        latest_by_country: dict[str, Any] = {}
        for route in routes:
            route_target_entry = _entry_for_route(route)
            selected_entry = route_target_entry or latest_global
            if not isinstance(selected_entry, dict):
                continue

            route_country = route.get("country_code")
            if route_country:
                route_locale = route.get("locale")
                route_country_entry = _entry_with_locale_url(
                    selected_entry, route_locale
                )
                existing_country_entry = latest_by_country.get(route_country)
                if _should_replace_country_entry(
                    existing=existing_country_entry,
                    candidate=route_country_entry,
                    global_entry=latest_global,
                ):
                    latest_by_country[route_country] = route_country_entry

        for route in routes:
            route_locale = route.get("locale")
            if not route_locale:
                continue

            route_target_entry = _entry_for_route(route)
            selected_entry: dict[str, Any] | None = None
            route_country = route.get("country_code")
            country_entry = (
                latest_by_country.get(route_country)
                if isinstance(route_country, str)
                else None
            )
            if isinstance(country_entry, dict):
                if _is_same_release_entry(route_target_entry, country_entry):
                    selected_entry = route_target_entry
                else:
                    selected_entry = country_entry
            elif isinstance(route_target_entry, dict):
                selected_entry = route_target_entry

            if isinstance(selected_entry, dict):
                latest_by_locale[route_locale] = _entry_with_locale_url(
                    selected_entry, route_locale
                )

        devices_catalog[device_key] = {
            "product_media_name_id": global_product_id,
            "document_topic_id": global_topic_id,
            "latest_by_locale": latest_by_locale,
            "latest_by_country": latest_by_country,
            "latest_global": latest_global,
        }
        source_devices[device_key] = {
            "docs_url": global_apps_url,
            "docs_path": str(global_target["docs_path"]),
            "product_type": int(global_product_type),
            "document_topic_id": global_topic_id,
            "product_media_name_id": global_product_id,
        }
        crawl_meta[device_key] = {
            "count": total_count,
            "targets": target_crawl,
            "missing_product_media_id_targets": sorted(set(missing_product_ids)),
            "used_global_product_media_id_targets": sorted(set(fallback_id_targets)),
            "empty_release_targets": sorted(set(empty_release_targets)),
            "bootstrap_error_targets": sorted(set(bootstrap_error_targets)),
            "crawl_error_targets": sorted(set(crawl_error_targets)),
        }

    locale_options = dict(language_options)
    locale_options.update(alt_language_options)
    locale_options.setdefault("en", "United States (EN)")
    region_mapping = build_region_country_mapping(locale_options)

    runtime_catalog = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "source": {
            "type": "enphase_documentation_center",
            "entrypoint": global_root_url,
            "apps_url": global_apps_url,
            "product_type": int(global_product_type),
            "devices": source_devices,
            "routing": "authoritative_region_site_routes",
            "target_count": len(crawl_targets),
            "crawl": crawl_meta,
        },
        "devices": devices_catalog,
    }
    generated_at = choose_generated_at(
        current_catalog=runtime_catalog,
        previous_catalog=previous_runtime_catalog,
        fallback_generated_at=generated_at,
    )
    runtime_catalog["generated_at"] = generated_at

    # Source metadata artifacts.
    sources_dir = output_dir / "sources" / "enphase_doc_center"
    write_json(
        sources_dir / "entrypoints.json",
        {
            "generated_at": generated_at,
            "root": global_root_url,
            "apps": global_apps_url,
            "product_type": int(global_product_type),
            "devices": source_devices,
            "targets": [
                {
                    "key": target["key"],
                    "site_url": target["site_url"],
                    "query_locale": target["query_locale"],
                }
                for target in crawl_targets
            ],
        },
    )
    write_json(
        sources_dir / "facet_ids.json",
        {
            "generated_at": generated_at,
            "document": global_topic_facets,
            "release_notes_topic_id": global_topic_id,
        },
    )
    write_json(
        sources_dir / "product_media_name_ids.json",
        {
            "generated_at": generated_at,
            "products": global_product_facets,
            "targets": {
                key: {
                    "label": TARGET_PRODUCTS[key]["label"],
                    "product_media_name_id": devices_catalog[key][
                        "product_media_name_id"
                    ],
                }
                for key in TARGET_PRODUCTS
                if key in devices_catalog
            },
        },
    )
    write_json(
        sources_dir / "regions_raw.json",
        {
            "generated_at": generated_at,
            "search_api_language": language_options,
            "field_alternative_language": alt_language_options,
        },
    )
    write_json(
        sources_dir / "region_to_country_codes.json",
        {
            "generated_at": generated_at,
            "mapping": region_mapping,
        },
    )
    write_json(
        sources_dir / "region_site_routes.json",
        {
            "generated_at": generated_at,
            "routes": routes,
            "crawl_targets": [
                {
                    "key": target["key"],
                    "site_url": target["site_url"],
                    "query_locale": target["query_locale"],
                    "locales": target["locales"],
                    "countries": target["countries"],
                    "labels": target["labels"],
                }
                for target in crawl_targets
            ],
        },
    )

    write_json(output_dir / "catalog" / "v1" / "runtime_catalog.json", runtime_catalog)

    # Optional per-country debug catalogs.
    for country_code in sorted(all_country_codes):
        per_country = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated_at,
            "country": country_code,
            "source": runtime_catalog["source"],
            "devices": {
                key: {
                    "product_media_name_id": value["product_media_name_id"],
                    "document_topic_id": value["document_topic_id"],
                    "latest": value["latest_by_country"].get(country_code)
                    or value["latest_global"],
                }
                for key, value in devices_catalog.items()
            },
        }
        write_json(output_dir / "data" / country_code / "catalog.json", per_country)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Enphase firmware catalog")
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Output directory for generated sources/catalog data",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help="HTTP timeout seconds",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=DEFAULT_MAX_PAGES,
        help="Maximum paginated result pages per product",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    output_dir = Path(args.output_dir).resolve()
    try:
        build_catalog(output_dir, timeout=args.timeout, max_pages=args.max_pages)
    except Exception as err:  # noqa: BLE001
        print(f"Failed to build firmware catalog: {err}", file=sys.stderr)
        return 1

    print(f"Firmware catalog generated at {output_dir}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
