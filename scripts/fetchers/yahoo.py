from typing import Dict
import re
import pandas as pd
from scripts.base import BaseFetcher
from scripts.errors import DataFetchError

def _is_yahoo_rate_limit(exc: Exception) -> bool:
    """True only for genuine Yahoo throttling — never for arbitrary failures."""
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    return "ratelimit" in name or "rate limit" in msg or "429" in msg or "too many requests" in msg

def _safe_reason(exc: Exception) -> str:
    """Sanitized exception prose for error channels.

    Keeps short human-readable failure reasons; drops anything that looks like
    a provider response body (HTML/XML tags, JSON braces, URLs that may embed
    credentials or query secrets) and control characters, with a hard length
    cap.  Returns an empty string when nothing safe remains.
    """
    text = " ".join(str(exc).split())
    text = re.sub(r"<[!a-zA-Z/][^>]{0,400}>", " ", text)
    text = re.sub(r"\S*://\S*", " ", text)
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    text = " ".join(text.split())
    if not text or len(text) > 300 or "{" in text or "}" in text or "<" in text or ">" in text:
        return ""
    return text

def _failure_message(prefix: str, exc: Exception) -> str:
    reason = _safe_reason(exc)
    if reason:
        return f"{prefix}: {type(exc).__name__}: {reason}"
    return f"{prefix}: {type(exc).__name__}. Verify the ticker symbol and retry."


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
                    f"Yahoo Finance rate limit hit while validating '{self.query}'.\n"
                    "  Hint: Yahoo throttles repeated requests — wait ~60s and retry.",
                    code="RATE_LIMITED",
                ) from exc
            # Raw exception text is scrubbed before reaching error channels.
            raise ValueError(_failure_message(
                f"Yahoo Finance request for '{self.query}' failed", exc)) from exc
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
                    f"Yahoo Finance rate limit hit while downloading '{self.query}'.\n"
                    "  Hint: rate limits usually clear within a minute.",
                    code="RATE_LIMITED",
                ) from exc
            raise ValueError(_failure_message(
                f"Yahoo Finance download for '{self.query}' failed", exc)) from exc
        return df
