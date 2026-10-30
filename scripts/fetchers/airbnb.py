from typing import Dict, Optional
from urllib.parse import urljoin, urlparse
import pandas as pd
from scripts.base import BaseFetcher
from scripts.errors import DataFetchError
from scripts.http_utils import request_with_retry
from scripts.net_guard import UnresolvableHostError, validate_public_url

INDEX_URL = "https://insideairbnb.com/get-the-data/"
ALLOWED_DOWNLOAD_SUFFIX = ".insideairbnb.com"
ALLOWED_DOWNLOAD_HOST = "insideairbnb.com"

class AirbnbFetcher(BaseFetcher):
    """Fetcher for Inside Airbnb dataset listings."""

    def __init__(self, query: str, outdir: str, config: Dict[str, str]) -> None:
        super().__init__(query, outdir, config)
        self.download_url: str = ""

    def _allowlisted_download_url(self, href: str) -> Optional[str]:
        """Accept only https:// links on insideairbnb.com (or a subdomain)."""
        absolute = urljoin(INDEX_URL, href)
        parsed = urlparse(absolute)
        host = (parsed.hostname or "").lower()
        if parsed.scheme == "https" and (host == ALLOWED_DOWNLOAD_HOST or host.endswith(ALLOWED_DOWNLOAD_SUFFIX)):
            return absolute
        return None

    def scout(self) -> Dict[str, str]:
        try:
            from bs4 import BeautifulSoup  # deferred: only needed for the index scrape
        except ImportError as exc:
            raise DataFetchError(
                f"The airbnb source requires beautifulsoup4: {exc}. Install dependencies from pyproject.toml.",
                code="PROVIDER_UNAVAILABLE", exit_code=3,
            ) from exc
        print(f"[Scout] Initiating HTML Scouting & Progressive Resiliency Protocol for '{self.query}'...")

        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

        try:
            response = request_with_retry(INDEX_URL, headers=headers, public_only=True)
        except ConnectionError as exc:
            raise RuntimeError(f"Failed to download Inside Airbnb index page: {type(exc).__name__}.") from exc

        if response.status_code != 200:
            raise ValueError(f"Failed to fetch Inside Airbnb index: {response.status_code}")

        soup = BeautifulSoup(response.text, "html.parser")
        self.download_url = ""

        candidates = [
            a["href"] for a in soup.find_all("a", href=True)
            if self.query.lower() in a["href"].lower() and a["href"].endswith("listings.csv.gz")
        ]
        if not candidates:
            raise ValueError(f"Could not resolve static .csv.gz download link for city: '{self.query}'.")

        for href in candidates:
            absolute = self._allowlisted_download_url(href)
            if absolute is None:
                self.completeness_warnings.append(
                    f"Blocked scraped link (only https:// on {ALLOWED_DOWNLOAD_HOST} is accepted): {href}"
                )
                continue
            try:
                validate_public_url(absolute)
            except UnresolvableHostError:
                # DNS can be transiently unavailable; the public-only transport
                # revalidates the URL before any byte is fetched.
                pass
            except ValueError as exc:
                self.completeness_warnings.append(f"Blocked scraped link by the public-URL guard: {exc}")
                continue
            self.download_url = absolute
            break

        if not self.download_url:
            raise ValueError(
                f"Could not resolve a safe insideairbnb.com .csv.gz download link for city: '{self.query}'."
            )

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
