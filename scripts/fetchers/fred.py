import re
from typing import Dict
import pandas as pd
from scripts.base import BaseFetcher
from scripts.config import get_api_key
from scripts.errors import DataFetchError
from scripts.http_utils import request_with_retry

class FREDFetcher(BaseFetcher):
    """Fetcher for St. Louis Fed Economic Data (FRED)."""

    def __init__(self, query: str, outdir: str, config: Dict[str, str]) -> None:
        super().__init__(query, outdir, config)
        self.api_key: str = ""

    def scout(self) -> Dict[str, str]:
        self.api_key = get_api_key(
            "FRED_API_KEY",
            self.config,
            "Please go to https://fred.stlouisfed.org/docs/api/api_key.html and paste your FRED API Key: "
        )
        # FRED keys are 32-char lowercase alphanumeric — validate early so a
        # stale/placeholder key fails in seconds with recovery steps, not a cryptic API 400
        if not re.fullmatch(r"[a-z0-9]{32}", self.api_key):
            raise DataFetchError(
                f"Stored FRED_API_KEY is invalid (expected 32 lowercase alphanumeric characters, "
                f"got {len(self.api_key)} character{'s' if len(self.api_key) != 1 else ''}).\n"
                "  Recovery options:\n"
                "   1. Request a free key at https://fred.stlouisfed.org/docs/api/api_key.html\n"
                "   2. Update the key in the global config file or export FRED_API_KEY.",
                code="AUTH_INVALID",
            )
        print("[Scout] Credentials validated.")
        return {
            "url": f"https://fred.stlouisfed.org/series/{self.query}",
            "size_info": "Full Time-Series History"
        }

    def extract(self) -> pd.DataFrame:
        print("[Extract] Interfacing with FRED API...")
        if not self.api_key:
            raise ValueError("API key not set. Execute scout() first.")
            
        url = f"https://api.stlouisfed.org/fred/series/observations?series_id={self.query}&api_key={self.api_key}&file_type=json"
        
        try:
            response = request_with_retry(url)
        except ConnectionError as exc:
            raise RuntimeError(f"FRED API request failed: {exc}") from exc
            
        if response.status_code != 200:
            raise ValueError(f"FRED API Error {response.status_code}: {response.text}")
            
        api_response_payload = response.json()
        if "observations" not in api_response_payload or not api_response_payload["observations"]:
            raise ValueError(f"No observations returned for series '{self.query}'.")
        
        df = pd.DataFrame(api_response_payload["observations"])
        df = df[['date', 'value']]
        # FRED ships values as text with '.' for missing — cast for downstream numerics
        df['value'] = pd.to_numeric(df['value'], errors='coerce')
        return df
