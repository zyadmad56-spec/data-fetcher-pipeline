from __future__ import annotations

from typing import Dict


def get_fetcher(source: str, query: str, outdir: str, config: Dict[str, str]) -> BaseFetcher:
    """Strategy Factory mapping CLI sources to OOP Classes."""
    from .base import BaseFetcher
    from .fetchers.yahoo import YahooFinanceFetcher
    from .fetchers.fred import FREDFetcher
    from .fetchers.airbnb import AirbnbFetcher
    from .fetchers.kaggle import KaggleFetcher
    from .fetchers.sec import SECFetcher
    from .fetchers.openml import OpenMLFetcher

    fetchers = {
        "yfinance": YahooFinanceFetcher,
        "fred": FREDFetcher,
        "airbnb": AirbnbFetcher,
        "openml": OpenMLFetcher,
        "kaggle": KaggleFetcher,
        "sec": SECFetcher,
    }
    source_key = source.lower()
    if source_key not in fetchers:
        raise ValueError(
            f"Source '{source}' is not registered. "
            f"Available sources: {sorted(fetchers.keys())}"
        )
    fetcher_class = fetchers[source_key]
    return fetcher_class(query, outdir, config)
