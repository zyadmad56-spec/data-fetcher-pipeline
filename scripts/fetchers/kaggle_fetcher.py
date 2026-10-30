import os
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Dict
import pandas as pd
from scripts.base import BaseFetcher
from scripts.config import get_api_key

_KAGGLE_ENV_VARS = ("KAGGLE_USERNAME", "KAGGLE_KEY")
ARCHIVE_UNCOMPRESSED_LIMIT = 500 * 1024 * 1024  # hard ceiling before extraction
ARCHIVE_MAX_ENTRIES = 10_000


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

    @staticmethod
    def _restore_environment(previous: Dict[str, str]) -> None:
        """Put the ambient environment back exactly as it was — the fetcher's
        credentials must never persist into the host process beyond the call."""
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    @staticmethod
    def _validate_archive(archive: Path) -> None:
        """Fail fast on ZipSlip paths, symlink entries and unbounded expansion
        BEFORE anything is extracted."""
        total_uncompressed = 0
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            if len(members) > ARCHIVE_MAX_ENTRIES:
                raise ValueError(
                    f"SIZE_LIMIT: Kaggle archive contains more than {ARCHIVE_MAX_ENTRIES} entries.")
            for member in members:
                parts = PurePosixPath(member.filename.replace("\\", "/")).parts
                if (
                    not parts
                    or any(part == ".." for part in parts)
                    or any(":" in part for part in parts)
                    or member.filename.startswith(("/", "\\"))
                ):
                    raise ValueError(
                        "BLOCKED: Kaggle archive member escapes the extraction directory: "
                        + repr(member.filename))
                if stat.S_ISLNK(member.external_attr >> 16):
                    raise ValueError(
                        "BLOCKED: Kaggle archive contains a symlink entry: " + repr(member.filename))
                total_uncompressed += member.file_size
        if total_uncompressed > ARCHIVE_UNCOMPRESSED_LIMIT:
            raise ValueError(
                "SIZE_LIMIT: Kaggle archive expands beyond the 500 MB uncompressed limit.")

    def _download_and_parse(self) -> pd.DataFrame:
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
                # unzip=False: the archive is validated (ZipSlip, symlinks,
                # expansion ceiling) before a single member is written out.
                kaggle.api.dataset_download_files(self.query, path=tmpdir, unzip=False)
            except (ValueError, KeyError, RuntimeError, OSError) as e:
                raise RuntimeError(f"Kaggle download failed: {e}") from e

            tmp_path = Path(tmpdir)
            archives = sorted(tmp_path.glob("*.zip"))
            if not archives:
                raise ValueError("No dataset archive was downloaded from Kaggle.")

            data_root = tmp_path / "extracted"
            data_root.mkdir()
            for archive in archives:
                self._validate_archive(archive)
                with zipfile.ZipFile(archive) as bundle:
                    bundle.extractall(data_root)

            # Search for data files in priority order: CSV > Parquet > JSON
            csv_files = sorted(data_root.rglob("*.csv"))
            parquet_files = sorted(data_root.rglob("*.parquet"))
            json_files = sorted(data_root.rglob("*.json"))

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

    def extract(self) -> pd.DataFrame:
        print("[Extract] Interfacing with Kaggle API...")
        previous = {name: os.environ.get(name) for name in _KAGGLE_ENV_VARS}
        os.environ["KAGGLE_USERNAME"] = self.username
        os.environ["KAGGLE_KEY"] = self.key
        try:
            return self._download_and_parse()
        finally:
            self._restore_environment(previous)
