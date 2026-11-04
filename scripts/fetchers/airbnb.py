"""Fetcher for Inside Airbnb verified official exports.

Selection never scrapes the official index (the published data policy asks
for no site scraping and minimal downloads).  Instead a small curated
registry of human-verified direct exports (``scripts/resources/
insideairbnb_exports.json``) resolves a city query, and an explicit official
``https://data.insideairbnb.com/...`` URL chosen by a human from the official
page is accepted after DNS-free structural validation.  Unknown or ambiguous
requests raise ``SOURCE_SELECTION_REQUIRED`` with the official page instead
of inventing dates or guessing a "latest" link.
"""
from __future__ import annotations

import json
import unicodedata
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlsplit

import pandas as pd

from scripts.base import BaseFetcher
from scripts.errors import DataFetchError
from scripts.net_guard import reject_url_fragments
from scripts.http_utils import request_with_retry  # noqa: F401  # stable module port; system-boundary fixtures patch this name
from scripts.source_catalog import INSIDE_AIRBNB_DATA_PAGE

#: Official pages for HUMAN selection — never fetched automatically.
INDEX_URL = INSIDE_AIRBNB_DATA_PAGE
POLICY_URL = "https://insideairbnb.com/data-policies/"

_OFFICIAL_DATA_HOST = "data.insideairbnb.com"
_REGISTRY_PATH = Path(__file__).resolve().parents[1] / "resources" / "insideairbnb_exports.json"
_REGISTRY_SCHEMA = "insideairbnb-registry.v1"


def _load_registry() -> dict:
    try:
        data = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DataFetchError(
            "The Inside Airbnb export registry is missing or corrupt.",
            code="PROVIDER_UNAVAILABLE", exit_code=3) from exc
    if (not isinstance(data, dict) or data.get("schema_version") != _REGISTRY_SCHEMA
            or not isinstance(data.get("resources"), list)):
        raise DataFetchError(
            "The Inside Airbnb export registry has an unsupported schema.",
            code="PROVIDER_UNAVAILABLE", exit_code=3)
    return data


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value or "").strip().lower()


class AirbnbFetcher(BaseFetcher):
    """Provider for the existing built-in ``airbnb`` source (Inside Airbnb)."""

    def __init__(self, query: str, outdir: str, config: Dict[str, str]) -> None:
        super().__init__(query, outdir, config)
        self.download_url: str = ""
        self.resource_type: str = ""
        #: True only when the resolved URL is verbatim from the shipped
        #: reviewed registry. Human-display channels may then carry the full
        #: path; user-typed URLs stay origin-only there, since private
        #: tokens can appear in URL paths.
        self.dataset_url_verified_public: bool = False

    def _accept_explicit_export(self, url: str) -> str:
        """DNS-free structural validation of a human-selected official export.

        Only ``https://data.insideairbnb.com/...`` CSV exports qualify: the
        exact host (no lookalike suffix match), no embedded userinfo or
        control characters, no explicit port.  DNS and redirect hops are
        revalidated by the public-only transport at connect time.
        """
        try:
            reject_url_fragments(url)
        except ValueError as exc:
            raise DataFetchError(
                "The explicit export URL was refused by the local safety policy.",
                code="BLOCKED_URL") from exc
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or host != _OFFICIAL_DATA_HOST:
            raise DataFetchError(
                f"Only https://{_OFFICIAL_DATA_HOST} export URLs are accepted; "
                f"choose a resource on the official page: {INDEX_URL}",
                code="BLOCKED_URL")
        if ":" in parts.netloc:
            raise DataFetchError(
                "The explicit export URL must not carry an explicit port.",
                code="BLOCKED_URL")
        path = parts.path or ""
        if not (path.endswith(".csv") or path.endswith(".csv.gz")):
            raise DataFetchError(
                "The explicit export must be a listings CSV export (.csv or "
                f".csv.gz) on {_OFFICIAL_DATA_HOST}.", code="BLOCKED_URL")
        return url

    def _resolve_selection(self) -> None:
        query = (self.query or "").strip()
        if "://" in query or query.lower().startswith("file:"):
            self.download_url = self._accept_explicit_export(query)
            self.resource_type = ("detailed_listings" if "/data/" in urlsplit(self.download_url).path
                                  else "summary_listings")
            # An explicit URL keeps its full path out of human-display
            # channels unless the exact URL is also a registry-verified
            # export; paths can carry private tokens.
            registry_urls = {entry.get("url") for entry in _load_registry()["resources"]}
            self.dataset_url_verified_public = self.download_url in registry_urls
            return
        wanted = _normalize(query)
        matches = []
        for entry in _load_registry()["resources"]:
            names = [_normalize(entry.get("region_id", ""))]
            names += [_normalize(alias) for alias in entry.get("aliases", [])]
            if wanted in names:
                matches.append(entry)
        if not matches:
            raise DataFetchError(
                f"City '{query}' is not in the verified export registry. Choose a "
                f"resource on the official page ({INDEX_URL}) and pass its exact "
                "export URL.", code="SOURCE_SELECTION_REQUIRED")
        detailed = [entry for entry in matches if entry.get("resource_type") == "detailed_listings"]
        if not detailed:
            # A city-only request needs a verified detailed default; summary
            # resources are explicit choices, never silent replacements.
            summary_urls = ", ".join(entry["url"] for entry in matches
                                     if entry.get("resource_type") == "summary_listings")
            raise DataFetchError(
                f"No verified detailed listings default exists for '{query}'. "
                f"Pass the exact summary export URL explicitly ({summary_urls}) "
                f"or choose on the official page: {INDEX_URL}",
                code="SOURCE_SELECTION_REQUIRED")
        chosen = detailed[0]
        self.download_url = chosen["url"]
        self.resource_type = chosen.get("resource_type", "detailed_listings")
        self.dataset_url_verified_public = True

    def scout(self) -> Dict[str, str]:
        print(f"[Scout] Resolving a verified Inside Airbnb export for '{self.query}'...")
        self._resolve_selection()
        size_info = "Unknown CSV payload"
        for entry in _load_registry()["resources"]:
            if entry.get("url") == self.download_url and entry.get("observed"):
                observed = entry["observed"]
                size_info = f"{observed.get('bytes', 'Unknown')} bytes (observed {observed.get('observed_at', 'unknown date')})"
                break
        # Governance: Inside Airbnb listings contain personal data — flag it so
        # downstream consumers see the compliance posture in envelope + manifest
        self.completeness_warnings.append(
            "GOVERNANCE: Inside Airbnb listings may contain personal data (host names, "
            "host IDs, exact coordinates) — review privacy obligations before redistribution."
        )
        print(f"[Scout] Official export selected ({self.resource_type}): {self.download_url}")
        return {
            "url": self.download_url,
            "size_info": size_info,
        }

    def _payload_compression(self) -> Optional[str]:
        return "gzip" if self.download_url.endswith(".gz") else None

    def extract(self) -> pd.DataFrame:
        compression = self._payload_compression()
        print(f"[Extract] Downloading official export"
              f"{' & safely decompressing .gz payload' if compression else ''}"
              f" (row limit: {self.row_limit or 'all'})...")
        if not self.download_url:
            raise ValueError("Download URL is not set. Execute scout() first.")
        # Streamed to disk with the gzip decompressed on parse — memory-flat
        return self._consume_payload_csv()
