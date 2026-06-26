from __future__ import annotations
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    import pandas as pd

from ..base import BaseFetcher

class GenericFetcher(BaseFetcher):
    def scout(self) -> dict:
        raise ValueError(
            f"GenericFetcher has no scouting logic for '{self.query}'. "
            f"Register a concrete fetcher in data_fetcher/factory.py."
        )

    def extract(self) -> pd.DataFrame:
        import pandas as pd
        print("[Extract] Fetching tabular data...")
        raise NotImplementedError("This source logic is not yet implemented.")


