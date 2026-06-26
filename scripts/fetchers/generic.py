import pandas as pd

from ..base import BaseFetcher

class GenericFetcher(BaseFetcher):
    def scout(self) -> dict:
        raise ValueError(
            f"GenericFetcher has no scouting logic for '{self.query}'. "
            f"Register a concrete fetcher in scripts/factory.py."
        )

    def extract(self) -> pd.DataFrame:
        print("[Extract] Fetching tabular data...")
        raise NotImplementedError("This source logic is not yet implemented.")


