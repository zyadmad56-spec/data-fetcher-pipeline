import requests
import requests.exceptions
import pandas as pd

from ..base import BaseFetcher
from ..config import get_api_key, DEFAULT_REQUEST_TIMEOUT_SECONDS

class FREDFetcher(BaseFetcher):
    def scout(self) -> dict:
        self.api_key = get_api_key(
            "FRED_API_KEY", 
            self.config, 
            "Please go to https://fred.stlouisfed.org/docs/api/api_key.html and paste your FRED API Key: "
        )
        print("[Scout] Credentials validated.")
        return {
            "url": f"https://fred.stlouisfed.org/series/{self.query}",
            "size_info": "Full Time-Series History"
        }

    def extract(self) -> pd.DataFrame:
        print("[Extract] Interfacing with FRED API...")
        url = f"https://api.stlouisfed.org/fred/series/observations?series_id={self.query}&api_key={self.api_key}&file_type=json"
        try:
            response = requests.get(url, timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS)
        except requests.exceptions.Timeout as exc:
            raise RuntimeError(f"FRED API request timed out after {DEFAULT_REQUEST_TIMEOUT_SECONDS}s") from exc
        
        if response.status_code != 200:
            raise ValueError(f"FRED API Error {response.status_code}: {response.text}")
            
        api_response_payload = response.json()
        if "observations" not in api_response_payload or not api_response_payload["observations"]:
            raise ValueError(f"No observations returned for series '{self.query}'.")
        
        df = pd.DataFrame(api_response_payload["observations"])
        df = df[['date', 'value']]
        # Constraint Enforced: Strict Frequency Preservation. Returns raw strings exactly as provided.
        return df


