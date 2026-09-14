import os
import tempfile
from pathlib import Path
from typing import Dict
import pandas as pd
from scripts.base import BaseFetcher
from scripts.config import get_api_key

class KaggleFetcher(BaseFetcher):
    """Fetcher for Kaggle datasets via the Kaggle API."""

    def __init__(self, query: str, outdir: str, config: Dict[str, str]) -> None:
        super().__init__(query, outdir, config)
        self.username: str = ""
        self.key: str = ""

    def scout(self) -> Dict[str, str]:
        self.username = get_api_key("KAGGLE_USERNAME", self.config, "Please provide your Kaggle Username: ")
        self.key = get_api_key("KAGGLE_KEY", self.config, "Please provide your Kaggle API Key: ")
        print("[Scout] Kaggle credentials validated.")
        return {
            "url": f"https://www.kaggle.com/datasets/{self.query}",
            "size_info": "Full dataset archive (CSV, Parquet, or JSON)"
        }

    def extract(self) -> pd.DataFrame:
        print("[Extract] Interfacing with Kaggle API...")
        os.environ['KAGGLE_USERNAME'] = self.username
        os.environ['KAGGLE_KEY'] = self.key
        
        try:
            import kaggle
        except ImportError as exc:
            raise ImportError("Kaggle package is missing. Please install dependencies from pyproject.toml.") from exc

        try:
            kaggle.api.authenticate()
        except (ValueError, KeyError, RuntimeError, OSError) as e:
            raise RuntimeError(f"Kaggle authentication failed: {e}") from e
            
        with tempfile.TemporaryDirectory() as tmpdir:
            print(f"[Extract] Downloading dataset '{self.query}'...")
            try:
                kaggle.api.dataset_download_files(self.query, path=tmpdir, unzip=True)
            except (ValueError, KeyError, RuntimeError, OSError) as e:
                raise RuntimeError(f"Kaggle download failed: {e}") from e
                
            # Search for data files in priority order: CSV > Parquet > JSON
            tmp_path = Path(tmpdir)
            csv_files = sorted(tmp_path.rglob("*.csv"))
            parquet_files = sorted(tmp_path.rglob("*.parquet"))
            json_files = sorted(tmp_path.rglob("*.json"))

            if csv_files:
                print(f"[Extract] Found {len(csv_files)} CSV file(s). Reading the primary payload...")
                largest = max(csv_files, key=lambda p: p.stat().st_size)
                df = pd.read_csv(largest, low_memory=False)
            elif parquet_files:
                print(f"[Extract] No CSV found. Found {len(parquet_files)} Parquet file(s). Reading...")
                largest = max(parquet_files, key=lambda p: p.stat().st_size)
                df = pd.read_parquet(largest)
            elif json_files:
                print(f"[Extract] No CSV/Parquet found. Found {len(json_files)} JSON file(s). Reading...")
                largest = max(json_files, key=lambda p: p.stat().st_size)
                df = pd.read_json(largest)
            else:
                raise ValueError("No supported data files (CSV, Parquet, JSON) found in the Kaggle dataset.")
            
        return df
