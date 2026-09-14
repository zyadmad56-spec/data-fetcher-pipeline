import io
from typing import Dict
import pandas as pd
from scripts.base import BaseFetcher
from scripts.http_utils import request_with_retry

class EurostatFetcher(BaseFetcher):
    """Fetcher for economic and demographic datasets from Eurostat bulk API."""

    def __init__(self, query: str, outdir: str, config: Dict[str, str]) -> None:
        super().__init__(query, outdir, config)
        self.dataset_code: str = self.query.strip().lower()
        self.download_url: str = ""

    def scout(self) -> Dict[str, str]:
        print(f"[Scout] Validating Eurostat Dataset Code '{self.dataset_code}'...")
        self.download_url = f"https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/{self.dataset_code}?format=TSV&compressed=false"
        
        headers = {
            "User-Agent": "data-fetcher-pipeline/2.0"
        }
        
        try:
            # Lightweight probe: fetch status/headers without downloading the TSV body
            response = request_with_retry(self.download_url, headers=headers, timeout=15, stream=True)
            response.close()
        except ConnectionError as exc:
            raise RuntimeError(f"Eurostat API connection failed: {exc}") from exc
            
        if response.status_code == 404 or response.status_code == 400:
            raise ValueError(f"Eurostat Dataset Code '{self.dataset_code}' does not exist or is invalid.")
        elif response.status_code != 200:
            raise ValueError(f"Eurostat API responded with status {response.status_code}")
            
        print(f"[Scout] Dataset verified. Download Target: {self.download_url}")
        return {
            "url": self.download_url,
            "size_info": "Bulk TSV Time Series"
        }

    def _payload_sep(self) -> str:
        return "\t"

    def extract(self) -> pd.DataFrame:
        print(f"[Extract] Loading bulk TSV (row limit: {self.row_limit or 'all'})...")
        if not self.download_url:
            raise ValueError("Download URL is not set. Run scout() first.")
        # Reuses the preview's temp payload when present — one download per run
        return self._consume_payload_csv()
