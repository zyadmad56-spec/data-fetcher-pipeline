from typing import Dict
import pandas as pd
from scripts.base import BaseFetcher
from scripts.config import get_api_key
from scripts.http_utils import request_with_retry

class SECFetcher(BaseFetcher):
    """Fetcher for corporate financial taxonomy facts from SEC EDGAR."""

    def __init__(self, query: str, outdir: str, config: Dict[str, str]) -> None:
        super().__init__(query, outdir, config)
        self.api_key: str = ""
        self.cik_str: str = ""
        self.facts_url: str = ""

    def scout(self) -> Dict[str, str]:
        self.api_key = get_api_key("SEC_API_KEY", self.config, "Please provide your SEC API Key or Email for User-Agent: ")
        print("[Scout] SEC EDGAR credentials validated.")
        
        ua = self.api_key if "@" in self.api_key else f"data-fetcher-pipeline/1.0 ({self.api_key})"
        headers = {"User-Agent": ua}
        
        cik_url = "https://www.sec.gov/files/company_tickers.json"
        try:
            resp = request_with_retry(cik_url, headers=headers)
        except ConnectionError as exc:
            raise RuntimeError(f"Failed to fetch SEC CIK index: {exc}") from exc
            
        if resp.status_code != 200:
            raise ValueError(f"Failed to fetch SEC CIK index. Status: {resp.status_code}")
            
        tickers = resp.json()
        self.cik_str = ""
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
        if not self.facts_url:
            raise ValueError("Facts URL is not set. Run scout() first.")
            
        ua = self.api_key if "@" in self.api_key else f"data-fetcher-pipeline/1.0 ({self.api_key})"
        headers = {"User-Agent": ua}
        
        print("[Extract] Fetching company facts...")
        try:
            resp = request_with_retry(self.facts_url, headers=headers)
        except ConnectionError as exc:
            raise RuntimeError(f"Failed to fetch company facts: {exc}") from exc
            
        if resp.status_code != 200:
            raise ValueError(f"Failed to fetch facts for CIK {self.cik_str}. Status: {resp.status_code}")
            
        data = resp.json()
        rows = []
        for taxonomy, concepts in data.get("facts", {}).items():
            for concept_name, concept_data in concepts.items():
                for unit, observations in concept_data.get("units", {}).items():
                    for obs in observations:
                        rows.append({
                            "taxonomy": taxonomy,
                            "concept": concept_name,
                            "unit": unit,
                            "val": obs.get("val"),
                            "fy": obs.get("fy"),
                            "fp": obs.get("fp"),
                            "form": obs.get("form"),
                            "filed": obs.get("filed"),
                            "end": obs.get("end")
                        })
                        
        if not rows:
            raise ValueError("No financial facts found for this company.")
            
        df = pd.DataFrame(rows)
        return df
