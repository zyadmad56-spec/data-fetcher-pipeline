import pandas as pd
from typing import Dict

from ..base import BaseFetcher

class OpenMLFetcher(BaseFetcher):
    def __init__(self, query: str, outdir: str, config: Dict[str, str]) -> None:
        super().__init__(query, outdir, config)
        self.target_dataset_id: int = -1
        self.dataset_name: str = ""

    def scout(self) -> dict:
        print(f"[Scout] Searching OpenML for query '{self.query}'...")
        try:
            import openml
        except ImportError as exc:
            raise ImportError("OpenML package is missing. Please run `pip install openml`.") from exc

        try:
            datasets_df: pd.DataFrame = openml.datasets.list_datasets(output_format="dataframe")
        except Exception as exc:
            raise RuntimeError(f"Failed to fetch dataset list from OpenML: {exc}") from exc
            
        mask = datasets_df['name'].str.contains(self.query, case=False, na=False)
        matched_df: pd.DataFrame = datasets_df[mask]
        
        if matched_df.empty:
            raise ValueError(f"No datasets found on OpenML matching query: '{self.query}'.")
            
        matched_sorted: pd.DataFrame = matched_df.sort_values(by="NumberOfInstances", ascending=False)
        
        top_dataset = matched_sorted.iloc[0]
        self.target_dataset_id = int(top_dataset['did'])
        self.dataset_name = str(top_dataset['name'])
        
        print(f"[Scout] Top dataset found: '{self.dataset_name}' (ID: {self.target_dataset_id})")
        return {
            "url": f"https://www.openml.org/d/{self.target_dataset_id}",
            "size_info": f"{int(top_dataset['NumberOfInstances'])} rows, {int(top_dataset['NumberOfFeatures'])} columns"
        }

    def extract(self) -> pd.DataFrame:
        print(f"[Extract] Downloading dataset '{self.dataset_name}' from OpenML...")
        try:
            import openml
        except ImportError as exc:
            raise ImportError("OpenML package is missing. Please run `pip install openml`.") from exc

        try:
            dataset = openml.datasets.get_dataset(self.target_dataset_id, download_data=True)
            X, y, _, _ = dataset.get_data(dataset_format="dataframe")
        except Exception as exc:
            raise RuntimeError(f"Failed to download OpenML dataset ID {self.target_dataset_id}: {exc}") from exc

        if y is not None:
            if isinstance(y, pd.Series):
                X[y.name] = y
            elif isinstance(y, pd.DataFrame):
                X = pd.concat([X, y], axis=1)
                

        print(f"[Extract] Dataset downloaded. Routing to centralized output directory: {self.outdir}")
        
        return X


