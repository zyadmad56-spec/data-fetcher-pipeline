from typing import Dict, Optional
import pandas as pd
from scripts.base import BaseFetcher
from scripts.errors import DataFetchError
from scripts.http_utils import request_with_retry

class AirbnbFetcher(BaseFetcher):
    """Fetcher for Inside Airbnb dataset listings."""

    def __init__(self, query: str, outdir: str, config: Dict[str, str]) -> None:
        super().__init__(query, outdir, config)
        self.download_url: str = ""

    def scout(self) -> Dict[str, str]:
        try:
            from bs4 import BeautifulSoup  # deferred: only needed for the index scrape
        except ImportError as exc:
            raise DataFetchError(
                f"The airbnb source requires beautifulsoup4: {exc}. Install dependencies from pyproject.toml.",
                code="PROVIDER_UNAVAILABLE", exit_code=3,
            ) from exc
        print(f"[Scout] Initiating HTML Scouting & Progressive Resiliency Protocol for '{self.query}'...")
        
        url = "http://insideairbnb.com/get-the-data/"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        
        try:
            response = request_with_retry(url, headers=headers)
        except ConnectionError as exc:
            raise RuntimeError(f"Failed to download Inside Airbnb index page: {exc}") from exc
            
        if response.status_code != 200:
            raise ValueError(f"Failed to fetch Inside Airbnb index: {response.status_code}")
            
        soup = BeautifulSoup(response.text, "html.parser")
        self.download_url = ""
        
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if self.query.lower() in href.lower() and href.endswith("listings.csv.gz"):
                self.download_url = href
                break
                
        if not self.download_url:
            raise ValueError(f"Could not resolve static .csv.gz download link for city: '{self.query}'.")

        # Governance: Inside Airbnb listings contain personal data — flag it so
        # downstream consumers see the compliance posture in envelope + manifest
        self.completeness_warnings.append(
            "GOVERNANCE: Inside Airbnb listings may contain personal data (host names, "
            "host IDs, exact coordinates) — review privacy obligations before redistribution."
        )

        print(f"[Scout] Static download target resolved: {self.download_url}")
        return {
            "url": self.download_url,
            "size_info": "Unknown compressed CSV payload"
        }

    def _payload_compression(self) -> Optional[str]:
        return "gzip"

    def extract(self) -> pd.DataFrame:
        print(f"[Extract] Downloading & safely decompressing .gz payload (row limit: {self.row_limit or 'all'})...")
        if not self.download_url:
            raise ValueError("Download URL is not set. Execute scout() first.")
        # Streamed to disk with the gzip decompressed on parse — memory-flat
        return self._consume_payload_csv()
