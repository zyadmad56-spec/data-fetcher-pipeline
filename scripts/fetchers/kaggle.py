import os

import pandas as pd

from ..base import BaseFetcher
from ..config import get_api_key

class KaggleFetcher(BaseFetcher):
    def scout(self) -> dict:
        self.username = get_api_key("KAGGLE_USERNAME", self.config, "Please provide your Kaggle Username: ")
        self.key = get_api_key("KAGGLE_KEY", self.config, "Please provide your Kaggle API Key: ")
        print("[Scout] Kaggle credentials validated.")
        return {
            "url": f"https://www.kaggle.com/datasets/{self.query}",
            "size_info": "Unknown CSV payload (Full dataset archive)"
        }

    def extract(self) -> pd.DataFrame:
        print("[Extract] Interfacing with Kaggle API...")
        import tempfile
        import glob

        os.environ['KAGGLE_USERNAME'] = self.username
        os.environ['KAGGLE_KEY'] = self.key

        try:
            try:
                import kaggle
                kaggle.api.authenticate()
            except ImportError as exc:
                raise ImportError("Kaggle package is missing. Please run `pip install kaggle`.") from exc

            with tempfile.TemporaryDirectory() as tmpdir:
                print(f"[Extract] Downloading dataset '{self.query}'...")
                try:
                    kaggle.api.dataset_download_files(self.query, path=tmpdir, unzip=True)
                except Exception as e:
                    raise RuntimeError(f"Kaggle download failed: {e}") from e

                csv_files = glob.glob(os.path.join(tmpdir, "**", "*.csv"), recursive=True)
                if not csv_files:
                    raise ValueError("No CSV files found in the Kaggle dataset.")

                print(f"[Extract] Found {len(csv_files)} CSV file(s). Reading the primary payload...")
                largest_csv = max(csv_files, key=os.path.getsize)
                df = pd.read_csv(largest_csv, low_memory=False)

            return df
        finally:
            os.environ.pop('KAGGLE_USERNAME', None)
            os.environ.pop('KAGGLE_KEY', None)


