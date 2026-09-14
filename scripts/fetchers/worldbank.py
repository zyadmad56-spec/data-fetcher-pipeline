from typing import Dict
import pandas as pd
from scripts.base import BaseFetcher
from scripts.http_utils import request_with_retry

class WorldBankFetcher(BaseFetcher):
    """Fetcher for macroeconomic development indicators from the World Bank API."""

    def __init__(self, query: str, outdir: str, config: Dict[str, str]) -> None:
        super().__init__(query, outdir, config)
        self.indicator_id: str = self.query
        self.total_records: int = 0

    def scout(self) -> Dict[str, str]:
        print(f"[Scout] Validating World Bank Indicator '{self.indicator_id}'...")
        url = f"https://api.worldbank.org/v2/country/all/indicator/{self.indicator_id}?format=json&per_page=1"
        
        try:
            response = request_with_retry(url)
        except ConnectionError as exc:
            raise RuntimeError(f"World Bank API validation failed: {exc}") from exc
            
        if response.status_code != 200:
            raise ValueError(f"World Bank API responded with status {response.status_code}")
            
        payload = response.json()
        
        # World Bank API returns a list where the first element is paging info
        if not isinstance(payload, list) or len(payload) < 2 or not isinstance(payload[0], dict):
            # Check for error message
            if isinstance(payload, dict) and "message" in payload:
                error_msg = payload["message"][0].get("value", "Unknown API error")
                raise ValueError(f"World Bank API Error: {error_msg}")
            raise ValueError(f"Invalid indicator '{self.indicator_id}' or no data returned.")
            
        self.total_records = int(payload[0].get("total", 0))
        print(f"[Scout] Found World Bank Indicator with {self.total_records} total observations.")
        
        return {
            "url": f"https://api.worldbank.org/v2/country/all/indicator/{self.indicator_id}?format=json",
            "size_info": f"{self.total_records} Observations"
        }

    def preview(self) -> None:
        print("[Preview] Sourcing World Bank indicator observations...")
        url = f"https://api.worldbank.org/v2/country/all/indicator/{self.indicator_id}?format=json&per_page=5"
        try:
            response = request_with_retry(url)
            if response.status_code == 200:
                payload = response.json()
                if isinstance(payload, list) and len(payload) > 1 and isinstance(payload[1], list):
                    records = []
                    for obs in payload[1]:
                        records.append({
                            "country": obs.get("country", {}).get("value"),
                            "countryiso3code": obs.get("countryiso3code"),
                            "date": obs.get("date"),
                            "value": obs.get("value"),
                            "indicator_id": obs.get("indicator", {}).get("id"),
                            "indicator_name": obs.get("indicator", {}).get("value")
                        })
                    df = pd.DataFrame(records)
                    if not df.empty:
                        print("\n" + "="*50)
                        print(" DATASET PREVIEW (First 5 Rows)")
                        print("="*50)
                        print(df.to_string())
                        print("="*50 + "\n")
        except (ValueError, KeyError, ConnectionError, IndexError) as exc:
            print(f"[Warning] Failed to generate World Bank dataset preview: {exc}")

    MAX_PAGES = 200  # stall guard: 200 pages x 10000 rows is far beyond any indicator

    def extract(self) -> pd.DataFrame:
        # Row-limit pushdown: a user asking for N rows fetches one page of N
        # records instead of the entire indicator across all pages
        per_page = min(self.row_limit, 10000) if self.row_limit and self.row_limit > 0 else 10000
        print(f"[Extract] Fetching pages for World Bank Indicator '{self.indicator_id}' (per_page={per_page})...")
        records = []
        page = 1
        total = 0

        while True:
            url = (
                f"https://api.worldbank.org/v2/country/all/indicator/{self.indicator_id}"
                f"?format=json&per_page={per_page}&page={page}"
            )
            response = request_with_retry(url)

            if response.status_code != 200:
                if records:
                    # Mid-loop failure with data in hand: keep the partial, visibly
                    self.mark_degraded(
                        f"World Bank API returned HTTP {response.status_code} at page {page} — "
                        f"kept {len(records):,} of {total:,} observations."
                    )
                    break
                raise ValueError(f"Failed to fetch World Bank dataset. Status: {response.status_code}")

            payload = response.json()
            if not isinstance(payload, list) or len(payload) < 2 or not isinstance(payload[1], list):
                raise ValueError("Invalid format returned by World Bank API.")

            # Page-echo validation: a proxy repeating a page would duplicate rows
            returned_page = payload[0].get("page")
            if returned_page is not None and int(returned_page) != page:
                self.mark_degraded(
                    f"Provider returned page {returned_page} when page {page} was requested — "
                    f"stopped to avoid duplication; kept {len(records):,} of {total:,} observations."
                )
                break

            total = int(payload[0].get("total", 0))
            for obs in payload[1]:
                records.append({
                    "country": obs.get("country", {}).get("value"),
                    "countryiso3code": obs.get("countryiso3code"),
                    "date": obs.get("date"),
                    "value": obs.get("value"),
                    "indicator_id": obs.get("indicator", {}).get("id"),
                    "indicator_name": obs.get("indicator", {}).get("value")
                })

            if not payload[1] or (total and len(records) >= total) or (self.row_limit and len(records) >= self.row_limit):
                if not payload[1] and total and len(records) < total:
                    # HTTP 200 with an empty page mid-loop: partial data that
                    # must never masquerade as a complete fetch
                    self.mark_degraded(
                        f"Provider returned an empty page mid-loop — fetched "
                        f"{len(records):,} of {total:,} observations."
                    )
                break
            page += 1
            if page > self.MAX_PAGES:
                self.mark_degraded(
                    f"Stopped at the {self.MAX_PAGES}-page safety cap with {len(records):,} of {total:,} observations."
                )
                break

        print(f"[Extract] Retrieved {len(records):,} of {total:,} observations across {min(page, self.MAX_PAGES)} page(s).")
        if total and len(records) < total:
            print(f"[Warning] Fetched {len(records):,} of {total:,} observations — dataset may be incomplete.")
            if self.row_limit and len(records) <= self.row_limit:
                # Provider-pushed user slice: complete:true but recorded as truncated
                self.truncation = {"rows": len(records), "population": total, "by": "user"}

        df = pd.DataFrame(records)
        return df
