from __future__ import annotations
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    import pandas as pd

from ..base import BaseFetcher

class YahooFinanceFetcher(BaseFetcher):
    def scout(self) -> dict:
        print(f"[Scout] Validating Yahoo Finance Ticker '{self.query}'...")
        import yfinance as yf
        import pandas as pd
        ticker = yf.Ticker(self.query)
        hist = ticker.history(period="5d")
        if hist.empty:
            raise ValueError(f"Ticker '{self.query}' not found or delisted.")
        print("[Scout] Ticker validated and active.")
        return {
            "url": f"https://finance.yahoo.com/quote/{self.query}",
            "size_info": "Max Historical Daily Candles"
        }

    def extract(self) -> pd.DataFrame:
        print("[Extract] Fetching historical market data...")
        import yfinance as yf
        import pandas as pd
        ticker = yf.Ticker(self.query)
        df = ticker.history(period="max").reset_index()
        # Constraint Enforced: Zero Imputation for weekends/holidays.
        return df


