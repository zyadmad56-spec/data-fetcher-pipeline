from __future__ import annotations
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    import pandas as pd
from typing import Dict

import requests
import requests.exceptions

from ..base import BaseFetcher
from ..config import get_api_key, DEFAULT_REQUEST_TIMEOUT_SECONDS

class SECFetcher(BaseFetcher):
    def __init__(self, query: str, outdir: str, config: Dict[str, str]) -> None:
        super().__init__(query, outdir, config)
        self.session = requests.Session()

    def scout(self) -> dict:
        self.api_key = get_api_key("SEC_API_KEY", self.config, "Please provide your SEC API Key or Email for User-Agent: ")
        print("[Scout] SEC EDGAR credentials validated.")
        
        ua = self.api_key if "@" in self.api_key else f"data-fetcher-pipeline/1.0 ({self.api_key})"
        self.session.headers.update({"User-Agent": ua})
        
        cik_url = "https://www.sec.gov/files/company_tickers.json"
        try:
            resp = self.session.get(cik_url, timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS)
        except requests.exceptions.Timeout as exc:
            raise RuntimeError(f"SEC CIK index request timed out after {DEFAULT_REQUEST_TIMEOUT_SECONDS}s") from exc
        if resp.status_code != 200:
            raise ValueError(f"Failed to fetch SEC CIK index. Status: {resp.status_code}")
            
        tickers = resp.json()
        self.cik_str = None
        for k, v in tickers.items():
            if str(v.get('ticker', '')).lower() == self.query.lower():
                self.cik_str = str(v['cik_str']).zfill(10)
                break
                
        if not self.cik_str:
            raise ValueError(f"Ticker '{self.query}' not found in SEC database.")
            
        print(f"[Scout] Resolved ticker '{self.query}' to CIK {self.cik_str}.")
        self.facts_url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{self.cik_str}.json"
        
        return {
            "url": self.facts_url,
            "size_info": "Full XBRL Corporate Taxonomy"
        }

    def extract(self) -> pd.DataFrame:
        print("[Extract] Interfacing with SEC EDGAR API...")
        
        print(f"[Extract] Fetching company facts...")
        try:
            resp = self.session.get(self.facts_url, timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS)
        except requests.exceptions.Timeout as exc:
            raise RuntimeError(f"SEC EDGAR facts request timed out after {DEFAULT_REQUEST_TIMEOUT_SECONDS}s") from exc
        if resp.status_code != 200:
            raise ValueError(f"Failed to fetch facts for CIK {self.cik_str}. Status: {resp.status_code}")
            
        data = resp.json()
        
        def facts_generator():
            for taxonomy, concepts in data.get("facts", {}).items():
                for concept_name, concept_data in concepts.items():
                    for unit, observations in concept_data.get("units", {}).items():
                        for obs in observations:
                            yield {
                                "taxonomy": taxonomy,
                                "concept": concept_name,
                                "unit": unit,
                                "val": obs.get("val"),
                                "fy": obs.get("fy"),
                                "fp": obs.get("fp"),
                                "form": obs.get("form"),
                                "filed": obs.get("filed"),
                                "end": obs.get("end")
                            }
                            
        import pandas as pd
        df = pd.DataFrame(facts_generator())
        
        if df.empty:
            raise ValueError("No financial facts found for this company.")
            
        return df


