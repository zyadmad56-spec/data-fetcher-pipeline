from typing import Dict
import pandas as pd
from scripts.base import BaseFetcher
from scripts.errors import DataFetchError

def _is_yahoo_rate_limit(exc: Exception) -> bool:
    """True only for genuine Yahoo throttling — never for arbitrary failures."""
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    return "ratelimit" in name or "rate limit" in msg or "429" in msg or "too many requests" in msg


class YahooFinanceFetcher(BaseFetcher):
    """Fetcher for Yahoo Finance market data."""

    def __init__(self, query: str, outdir: str, config: Dict[str, str]) -> None:
        super().__init__(query, outdir, config)

    def scout(self) -> Dict[str, str]:
        try:
            import yfinance as yf  # deferred: heavy, historically import-fragile
        except ImportError as exc:
            raise DataFetchError(
                f"The yahoo source requires yfinance: {exc}. Install dependencies from pyproject.toml.",
                code="PROVIDER_UNAVAILABLE", exit_code=3,
            ) from exc
        print(f"[Scout] Validating Yahoo Finance Ticker '{self.query}'...")
        try:
            ticker = yf.Ticker(self.query)
            hist = ticker.history(period="5d")
        except Exception as exc:
            if _is_yahoo_rate_limit(exc):
                raise DataFetchError(
                    f"Yahoo Finance rate limit hit while validating '{self.query}': {exc}\n"
                    "  Hint: Yahoo throttles repeated requests — wait ~60s and retry.",
                    code="RATE_LIMITED",
                ) from exc
            raise ValueError(
                f"Yahoo Finance request for '{self.query}' failed: {type(exc).__name__}: {exc}"
            ) from exc
        if hist.empty:
            raise ValueError(f"Ticker '{self.query}' not found or delisted.")
        print("[Scout] Ticker validated and active.")
        return {
            "url": f"https://finance.yahoo.com/quote/{self.query}",
            "size_info": "Max Historical Daily Candles"
        }

    def extract(self) -> pd.DataFrame:
        try:
            import yfinance as yf  # deferred: heavy, historically import-fragile
        except ImportError as exc:
            raise DataFetchError(
                f"The yahoo source requires yfinance: {exc}. Install dependencies from pyproject.toml.",
                code="PROVIDER_UNAVAILABLE", exit_code=3,
            ) from exc
        print("[Extract] Fetching historical market data...")
        try:
            ticker = yf.Ticker(self.query)
            df = ticker.history(period="max").reset_index()
        except Exception as exc:
            if _is_yahoo_rate_limit(exc):
                raise DataFetchError(
                    f"Yahoo Finance rate limit hit while downloading '{self.query}': {exc}\n"
                    "  Hint: rate limits usually clear within a minute.",
                    code="RATE_LIMITED",
                ) from exc
            raise ValueError(
                f"Yahoo Finance download for '{self.query}' failed: {type(exc).__name__}: {exc}"
            ) from exc
        return df
