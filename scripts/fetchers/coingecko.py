from os import environ
from urllib.parse import quote
from typing import Dict, Optional
import pandas as pd
from scripts.base import BaseFetcher
from scripts.http_utils import request_with_retry

class CoinGeckoFetcher(BaseFetcher):
    """Fetcher for cryptocurrency market history from CoinGecko (no API key required).

    Uses the free public REST API (tolerates roughly 10-30 requests/minute).
    Free-tier access covers up to 365 days of history; `days=max` is paywalled,
    so the fetcher tries full history first and falls back to 365 days. Set
    COINGECKO_API_KEY (env or config) to use a demo/pro key for longer ranges.
    The query is a CoinGecko coin id (e.g. 'bitcoin', 'ethereum'); symbols or
    names like 'btc' are auto-resolved via the /search endpoint.
    """

    BASE = "https://api.coingecko.com/api/v3"

    def __init__(self, query: str, outdir: str, config: Dict[str, str]) -> None:
        super().__init__(query, outdir, config)
        self.coin_id: str = ""
        self.market_url: str = ""
        self._probe_payload: Optional[dict] = None  # scout's days=1 response, reused as preview

    def preview(self) -> None:
        """Zero-cost preview: reuses the days=1 probe the scout already fetched."""
        if self._probe_payload:
            print("[Preview] Reusing scout's 1-day probe (no extra request)...")
            df = self._payload_to_frame(self._probe_payload)
            if not df.empty:
                print("\n" + "="*50)
                print(" DATASET PREVIEW (First 5 Rows)")
                print("="*50)
                print(df.head(5).to_string())
                print("="*50 + "\n")
            return
        super().preview()

    @staticmethod
    def _payload_to_frame(payload: dict) -> pd.DataFrame:
        prices = payload.get("prices", [])
        market_caps = dict(payload.get("market_caps", []))
        volumes = dict(payload.get("total_volumes", []))
        df = pd.DataFrame({
            "timestamp_ms": [p[0] for p in prices],
            "price_usd": [p[1] for p in prices],
        })
        df["market_cap_usd"] = df["timestamp_ms"].map(market_caps)
        df["volume_usd"] = df["timestamp_ms"].map(volumes)
        df["date"] = pd.to_datetime(df["timestamp_ms"], unit="ms")
        return df

    def _headers(self) -> Dict[str, str]:
        headers = {"User-Agent": "data-fetcher-pipeline/2.0", "Accept": "application/json"}
        key = environ.get("COINGECKO_API_KEY") or self.config.get("COINGECKO_API_KEY", "")
        if key:
            headers["x-cg-demo-api-key"] = key
            headers["x-cg-pro-api-key"] = key
        return headers

    def scout(self) -> Dict[str, str]:
        candidate = self.query.strip().lower().replace(" ", "-")
        print(f"[Scout] Validating cryptocurrency '{candidate}' on CoinGecko...")

        probe_url = f"{self.BASE}/coins/{quote(candidate)}/market_chart?vs_currency=usd&days=1"
        response = request_with_retry(probe_url, headers=self._headers())

        if response.status_code == 404:            # Not a coin id — resolve a symbol or name via the search endpoint
            search_url = f"{self.BASE}/search?query={quote(candidate)}"
            search_resp = request_with_retry(search_url, headers=self._headers())
            if search_resp.status_code != 200:
                raise ValueError(f"CoinGecko search failed with status {search_resp.status_code}.")
            coins = search_resp.json().get("coins", [])
            if not coins:
                raise ValueError(
                    f"No cryptocurrency found on CoinGecko matching '{self.query}'. "
                    "Queries must be a coin id (e.g. 'bitcoin', 'ethereum') or a symbol/name "
                    "(e.g. 'btc') resolvable via CoinGecko search."
                )
            candidate = coins[0].get("id", "")
            print(f"[Scout] Resolved '{self.query}' to CoinGecko id '{candidate}'.")
        elif response.status_code != 200:
            raise ValueError(f"CoinGecko responded with status {response.status_code}.")

        self.coin_id = candidate
        self.resolved_title = f"{self.coin_id} (CoinGecko)"
        self.market_url = f"{self.BASE}/coins/{self.coin_id}/market_chart?vs_currency=usd&days=max"
        # Keep the 1-day probe payload — preview() reuses it at zero request cost
        self._probe_payload = response.json()
        print(f"[Scout] Coin '{self.coin_id}' validated. Full history target resolved.")
        return {
            "url": self.market_url,
            "size_info": "Full daily market history (USD)"
        }

    def extract(self) -> pd.DataFrame:
        print(f"[Extract] Downloading market history for '{self.coin_id}'...")
        if not self.market_url:
            raise ValueError("Market URL is not set. Run scout() first.")

        has_key = bool(environ.get("COINGECKO_API_KEY") or self.config.get("COINGECKO_API_KEY", ""))
        if has_key:
            response = request_with_retry(self.market_url, headers=self._headers())
        else:
            # No key: `days=max` is a guaranteed 401 — don't spend the request
            print("[Extract] No CoinGecko key configured — fetching 365-day free-tier history directly...")
            self.mark_degraded("Full history requires a CoinGecko API key — fetched last 365 days instead of complete history.")
            fallback_url = f"{self.BASE}/coins/{self.coin_id}/market_chart?vs_currency=usd&days=365"
            # Provenance truth: the manifest must record the URL that produced the bytes
            self.dataset_url = fallback_url
            response = request_with_retry(fallback_url, headers=self._headers())

        if response.status_code == 401:
            # Key present but rejected/max still paywalled — degrade to 365 days, visibly
            print("[Extract] Full history requires a CoinGecko key (401). Falling back to 365 days...")
            self.mark_degraded("Full history requires a CoinGecko API key — fetched last 365 days instead of complete history.")
            fallback_url = f"{self.BASE}/coins/{self.coin_id}/market_chart?vs_currency=usd&days=365"
            self.dataset_url = fallback_url
            response = request_with_retry(fallback_url, headers=self._headers())

        if response.status_code != 200:
            raise ValueError(f"CoinGecko market chart request failed: status {response.status_code}")

        payload = response.json()
        prices = payload.get("prices", [])
        if not prices:
            raise ValueError(f"No market history returned for '{self.coin_id}'.")

        market_caps = dict(payload.get("market_caps", []))
        volumes = dict(payload.get("total_volumes", []))

        df = pd.DataFrame({
            "timestamp_ms": [p[0] for p in prices],
            "price_usd": [p[1] for p in prices],
        })
        df["market_cap_usd"] = df["timestamp_ms"].map(market_caps)
        df["volume_usd"] = df["timestamp_ms"].map(volumes)
        df["date"] = pd.to_datetime(df["timestamp_ms"], unit="ms")
        return df
