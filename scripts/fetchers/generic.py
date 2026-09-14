from typing import Dict, Any
import pandas as pd
from scripts.base import BaseFetcher

class GenericFetcher(BaseFetcher):
    """Fallback fetcher for generic custom or unsupported data sources."""

    def __init__(self, query: str, outdir: str, config: Dict[str, Any], source: str = "generic") -> None:
        super().__init__(query, outdir, config, source=source)
        self.source = source

    def scout(self) -> Dict[str, str]:
        print(f"[Scout] Running generic pre-flight validation for '{self.query}'...")
        return {}

    def extract(self) -> pd.DataFrame:
        from scripts.factory import list_sources
        supported = ", ".join(list_sources())
        raise ValueError(
            f"Unsupported data source '{self.source}'. Supported sources: {supported}"
        )
