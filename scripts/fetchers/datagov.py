import io
import os
from typing import Dict
from urllib.parse import quote
import pandas as pd
from scripts.base import BaseFetcher
from scripts.http_utils import request_with_retry

class DataGovFetcher(BaseFetcher):
    """Fetcher for open government datasets from Data.gov (GSA Catalog API v4).

    Data.gov retired the legacy CKAN action API at catalog.data.gov; the catalog
    now lives behind https://api.gsa.gov/technology/datagov/v4 (DCAT-based).
    A DATAGOV_API_KEY is optional — the shared DEMO_KEY is used as fallback
    with a lower rate limit (request a free key at https://api.data.gov).
    """

    API_BASE = "https://api.gsa.gov/technology/datagov/v4/search"

    def __init__(self, query: str, outdir: str, config: Dict[str, str]) -> None:
        super().__init__(query, outdir, config)
        self.download_url: str = ""
        self.dataset_title: str = ""

    def _api_key(self) -> str:
        key = os.environ.get("DATAGOV_API_KEY") or self.config.get("DATAGOV_API_KEY", "")
        if not key:
            print("[Scout] No DATAGOV_API_KEY configured — using shared DEMO_KEY (limited rate). "
                  "Get a free key at https://api.data.gov to raise limits.")
            return "DEMO_KEY"
        return key

    def _resolve_csv_url(self, payload: dict) -> None:
        """Walk v4 search results and pick the first http(s) CSV distribution."""
        for item in payload.get("results", []):
            dcat = item.get("dcat", {})
            title = dcat.get("title", "Untitled Dataset")
            for dist in dcat.get("distribution", []):
                fmt = str(dist.get("format", "")).upper()
                access_url = dist.get("downloadURL") or dist.get("accessURL") or ""
                if (fmt == "CSV" or access_url.lower().endswith(".csv")) and access_url.lower().startswith("http"):
                    self.download_url = access_url
                    self.dataset_title = title
                    return

    def scout(self) -> Dict[str, str]:
        print(f"[Scout] Searching Data.gov Catalog API v4 for '{self.query}'...")

        headers = {
            "X-Api-Key": self._api_key(),
            "User-Agent": "data-fetcher-pipeline/2.0",
        }

        # CSV distributions are sparse in search results — paginate until one is found
        after = ""
        for _ in range(3):
            url = f"{self.API_BASE}?q={quote(self.query)}&per_page=100"
            if after:
                url += f"&after={quote(after)}"

            try:
                response = request_with_retry(url, headers=headers)
            except ConnectionError as exc:
                raise RuntimeError(f"Data.gov catalog query failed: {exc}") from exc

            if response.status_code in (401, 403):
                raise ValueError("Data.gov API rejected the API key. Check DATAGOV_API_KEY or remove it to use DEMO_KEY.")
            if response.status_code != 200:
                raise ValueError(f"Data.gov API returned status code {response.status_code}")

            payload = response.json()
            results = payload.get("results", [])
            if not results and not after:
                raise ValueError(f"No packages found on Data.gov matching query: '{self.query}'.")

            self._resolve_csv_url(payload)
            if self.download_url:
                break
            after = payload.get("after", "")
            if not after:
                break

        if not self.download_url:
            raise ValueError(f"Could not locate a CSV resource for packages matching: '{self.query}'.")

        self.resolved_title = self.dataset_title
        print(f"[Scout] Resolved dataset '{self.dataset_title}' with CSV resource: {self.download_url}")
        return {
            "url": self.download_url,
            "size_info": "External CSV Dataset"
        }

    def preview(self) -> None:
        # Inherited base preview: streams the payload once to a temp file and
        # parses 5 rows — extract() reuses the same download (no double fetch)
        super().preview()

    def extract(self) -> pd.DataFrame:
        print(f"[Extract] Loading CSV dataset (row limit: {self.row_limit or 'all'})...")
        if not self.download_url:
            raise ValueError("Download URL is not set. Run scout() first.")
        # Reuses the preview's temp payload when present — one download per run
        return self._consume_payload_csv()
