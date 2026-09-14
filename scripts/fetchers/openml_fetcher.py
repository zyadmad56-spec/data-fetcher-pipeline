from typing import Dict, Optional, Any
import pandas as pd
from scripts.base import BaseFetcher

class OpenMLFetcher(BaseFetcher):
    """Fetcher for machine learning datasets from OpenML.

    Query semantics:
    - numeric query (e.g. '61') targets that dataset ID directly, no search;
    - text query is a name-substring search ranked by relevance: an exact name
      match always wins, then smaller datasets are preferred over larger ones
      (search is for exploration; the biggest match is usually a bloated mirror);
    - candidates over SIZE_CELL_BUDGET cells are skipped in auto mode so a
      vague query can never silently download a multi-gigabyte dataset.
    """

    SIZE_CELL_BUDGET = 5_000_000  # max instances x features auto-downloaded

    def __init__(self, query: str, outdir: str, config: Dict[str, str]) -> None:
        super().__init__(query, outdir, config)
        self.target_dataset_id: int = -1
        self.dataset_name: str = ""

    def _load_openml(self) -> Any:
        try:
            import openml
        except ImportError as exc:
            raise ImportError("OpenML package is missing. Please install dependencies from pyproject.toml.") from exc
        return openml

    def scout(self) -> Dict[str, str]:
        # Numeric query -> direct dataset ID; no listing search involved
        if self.query.strip().isdigit():
            self.target_dataset_id = int(self.query.strip())
            self.dataset_name = f"dataset-{self.target_dataset_id}"
            self.resolved_title = f"OpenML dataset {self.target_dataset_id}"
            print(f"[Scout] Numeric query detected — targeting OpenML dataset ID {self.target_dataset_id} directly.")
            return {
                "url": f"https://www.openml.org/d/{self.target_dataset_id}",
                "size_info": "Determined at download (direct ID)"
            }

        print(f"[Scout] Searching OpenML for query '{self.query}'...")
        openml = self._load_openml()

        try:
            datasets_df = openml.datasets.list_datasets(output_format="dataframe")
        except (ValueError, KeyError, RuntimeError) as exc:
            raise RuntimeError(f"Failed to fetch dataset list from OpenML: {exc}") from exc

        names = datasets_df['name'].astype(str)
        matched_df = datasets_df[names.str.contains(self.query, case=False, na=False, regex=False)]
        if matched_df.empty:
            raise ValueError(f"No datasets found on OpenML matching query: '{self.query}'.")

        matched_df = matched_df.dropna(subset=["NumberOfInstances", "NumberOfFeatures"])

        # Relevance ranking: exact name match wins; otherwise smallest first
        exact = matched_df[matched_df['name'].astype(str).str.lower() == self.query.strip().lower()]
        pool = exact if not exact.empty else matched_df
        pool = pool.sort_values(by="NumberOfInstances", ascending=True)

        candidates = pool.head(5)
        print("\n" + "=" * 74)
        print(f" OPENML CANDIDATES FOR '{self.query}' (best match first)")
        print("=" * 74)
        for rank, (_, row) in enumerate(candidates.iterrows(), start=1):
            print(f"  {rank}. {str(row['name'])[:45]:<45} | ID {int(row['did']):>7,} | "
                  f"{int(row['NumberOfInstances']):>10,} rows x {int(row['NumberOfFeatures']):>5,} features")
        print("=" * 74)

        chosen: Optional[pd.Series] = None
        if not self.auto_approve:
            chosen = self._prompt_candidate_choice(candidates)

        if chosen is None:
            chosen = self._pick_within_budget(pool)
            if chosen is None:
                raise ValueError(
                    f"All {len(pool)} matching datasets exceed the {self.SIZE_CELL_BUDGET:,}-cell size budget. "
                    "Use a numeric OpenML dataset ID to target a specific large dataset."
                )

        self.target_dataset_id = int(chosen['did'])
        self.dataset_name = str(chosen['name'])
        self.resolved_title = f"{self.dataset_name} (OpenML {self.target_dataset_id})"
        print(f"[Scout] Selected '{self.dataset_name}' (ID {self.target_dataset_id}): "
              f"{int(chosen['NumberOfInstances']):,} rows x {int(chosen['NumberOfFeatures']):,} features.")
        return {
            "url": f"https://www.openml.org/d/{self.target_dataset_id}",
            "size_info": f"{int(chosen['NumberOfInstances']):,} rows, {int(chosen['NumberOfFeatures']):,} columns"
        }

    def _prompt_candidate_choice(self, candidates: pd.DataFrame) -> Optional[pd.Series]:
        """Interactive selection among the shown candidates; empty input = best match."""
        top_n = len(candidates)
        while True:
            ans = input(f"Select dataset 1-{top_n} (or press Enter for best match): ").strip()
            if ans == "":
                return None
            if ans.isdigit() and 1 <= int(ans) <= top_n:
                return candidates.iloc[int(ans) - 1]
            print(f"[Error] Enter a number between 1 and {top_n}, or press Enter for best match.")

    def _pick_within_budget(self, pool: pd.DataFrame) -> Optional[pd.Series]:
        """Take the smallest candidate whose cell count fits the budget; skip and report the rest."""
        for _, row in pool.iterrows():
            cells = int(row['NumberOfInstances']) * int(row['NumberOfFeatures'])
            if cells <= self.SIZE_CELL_BUDGET:
                return row
            print(f"[Scout] Skipping '{row['name']}' (ID {int(row['did'])}): "
                  f"{int(row['NumberOfInstances']):,} rows x {int(row['NumberOfFeatures']):,} features "
                  f"({cells:,} cells) exceeds the size budget.")
        return None

    def extract(self) -> pd.DataFrame:
        print(f"[Extract] Downloading dataset '{self.dataset_name}' from OpenML...")
        if self.target_dataset_id == -1:
            raise ValueError("Target dataset ID is not set. Execute scout() first.")

        openml = self._load_openml()

        try:
            dataset = openml.datasets.get_dataset(self.target_dataset_id, download_data=True)
            X, y, _, _ = dataset.get_data(dataset_format="dataframe")
        except (ValueError, KeyError, RuntimeError) as exc:
            raise RuntimeError(f"Failed to download OpenML dataset ID {self.target_dataset_id}: {exc}") from exc

        if y is not None:
            if isinstance(y, pd.Series):
                X[y.name] = y
            elif isinstance(y, pd.DataFrame):
                X = pd.concat([X, y], axis=1)

        print(f"[Extract] Dataset downloaded. Routing to centralized output directory: {self.outdir}")
        return X
