import hashlib
import json
import os
import re
import shutil
import sys
import time
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Dict, List, Optional
from pathlib import Path

import pandas as pd

from scripts.errors import DataFetchError
from scripts.http_utils import request_with_retry, add_metrics_bytes
from scripts.provision import provision_data_directory, PLATFORMS  # re-exported for compat

__all__ = ["sanitize_name", "provision_data_directory", "PLATFORMS", "BaseFetcher"]

logger = logging.getLogger(__name__)

def sanitize_name(text: str) -> str:
    """Sanitize string for safe directory and file names.

    Caps length at 60 chars (Windows MAX_PATH headroom) and falls back to
    'dataset' when nothing survives sanitizing (e.g. pure-Unicode queries).
    """
    cleaned = re.sub(r'[^a-zA-Z0-9_-]+', '_', text).strip('_').lower()
    if not cleaned:
        return "dataset"
    return cleaned[:60]

class BaseFetcher(ABC):
    """Abstract Base Class enforcing the architectural constraints of the data-fetcher-pipeline."""
    
    def __init__(self, query: str, outdir: str, config: Dict[str, str], auto_approve: bool = False, source: Optional[str] = None) -> None:
        self.query = query
        self.config = config
        self.auto_approve = auto_approve
        self.row_limit = 0
        self._cached_df: Optional[pd.DataFrame] = None
        self.dataset_url: str = "N/A"
        self.rows_count: int = 0
        self.cols_count: int = 0

        # Completeness tracking: fetchers call mark_degraded() when the saved
        # artifact is known to be less than what was requested
        self.complete: bool = True
        self.completeness_warnings: List[str] = []
        self.resolved_title: Optional[str] = None
        self.last_sha256: str = ""
        self.last_bytes: int = 0
        self.last_description_path: str = ""
        # User-requested truncation record (None = full population fetched)
        self.truncation: Optional[Dict] = None
        # Temp payload path shared between preview() and extract() (one download per run)
        self._temp_payload: Optional[Path] = None

        if source is not None:
            clean_source = sanitize_name(source)
        else:
            source_name = self.__class__.__name__.replace("Fetcher", "")
            clean_source = sanitize_name(source_name)
            if clean_source == "githubdata":
                clean_source = "github"
            elif clean_source == "yahoofinance":
                clean_source = "yahoo"

        self.source_key = clean_source
        # Standard isolated folder: outdir / clean_source / clean_topic
        self.outdir = str(Path(outdir) / clean_source / sanitize_name(self.query))
        Path(self.outdir).mkdir(parents=True, exist_ok=True)

    def mark_degraded(self, warning: str) -> None:
        """Flag the fetch as incomplete; surfaces in the JSON envelope and manifest."""
        self.complete = False
        self.completeness_warnings.append(warning)

    @abstractmethod
    def scout(self) -> dict:
        """Phase 1 (Scouting): Pre-flight validation. Returns metadata dict: {'url': str, 'size_info': str}"""
        pass

    @abstractmethod
    def extract(self) -> pd.DataFrame:
        """Phase 2 (Extraction): Download and format raw payloads."""
        pass

    def preview(self) -> None:
        """Cheap by contract: at most one small network cost.

        For sources with a direct payload URL, streams it to a temp file once
        and previews the first 5 rows via nrows-capped parsing — extract()
        reuses the same temp file (no second download). Sources without a
        direct URL fall back to extract() and cache the frame.
        """
        print("[Preview] Generating default dataset preview...")
        url = self._payload_url()
        if url:
            try:
                temp_path = self._download_to_tempfile(url, headers=self._payload_headers())
                self._temp_payload = temp_path
                df = pd.read_csv(
                    temp_path, sep=self._payload_sep(), nrows=5,
                    compression=self._payload_compression(), low_memory=False,
                )
                if not df.empty:
                    print("\n" + "="*50)
                    print(" DATASET PREVIEW (First 5 Rows)")
                    print("="*50)
                    print(df.head(5).to_string())
                    print("="*50 + "\n")
                return
            except (ValueError, RuntimeError, OSError) as exc:
                print(f"[Preview] Cheap preview unavailable ({exc}) — full extraction will fetch the payload.")
                self._temp_payload = None
                return

        # No direct payload URL: preview requires the full extract (cached for reuse)
        print("[Preview] No lightweight preview available for this source — running full extraction (result cached).")
        df = self.extract()
        self._cached_df = df
        if not df.empty:
            print("\n" + "="*50)
            print(" DATASET PREVIEW (First 5 Rows)")
            print("="*50)
            print(df.head(5).to_string())
            print("="*50 + "\n")

    # -- Payload hooks: sources with a direct download URL override these ------

    def _payload_url(self) -> Optional[str]:
        return getattr(self, "download_url", None) or None

    def _payload_headers(self) -> Dict[str, str]:
        return {"User-Agent": "data-fetcher-pipeline/2.0"}

    def _payload_sep(self) -> str:
        return ","

    def _payload_compression(self) -> Optional[str]:
        return None

    MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024  # hard ceiling for any single payload

    def _download_to_tempfile(self, url: str, headers: Optional[Dict[str, str]] = None) -> Path:
        """Stream a payload to a temp file with a Content-Length precheck and a
        hard byte cap. Returns the temp path; extract() reuses it instead of
        re-downloading. Memory stays flat regardless of payload size."""
        target = Path(self.outdir) / (sanitize_name(self.query) + "_payload.tmp")
        response = request_with_retry(url, headers=headers or self._payload_headers(), stream=True)
        try:
            if response.status_code != 200:
                raise ValueError(f"HTTP {response.status_code} while fetching payload.")
            content_length = response.headers.get("Content-Length")
            if content_length and content_length.isdigit() and int(content_length) > self.MAX_DOWNLOAD_BYTES:
                raise DataFetchError(
                    f"Payload exceeds the {self.MAX_DOWNLOAD_BYTES // (1024*1024)} MB download cap "
                    f"(Content-Length: {content_length}).", code="SIZE_LIMIT",
                )
            written = 0
            with open(target, "wb") as f:
                for chunk in response.iter_content(1024 * 1024):
                    written += len(chunk)
                    add_metrics_bytes(len(chunk))
                    if written > self.MAX_DOWNLOAD_BYTES:
                        raise DataFetchError(
                            f"Payload exceeded the {self.MAX_DOWNLOAD_BYTES // (1024*1024)} MB download cap mid-stream.",
                            code="SIZE_LIMIT",
                        )
                    f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            response.close()
            target.unlink(missing_ok=True)
            raise
        response.close()
        self._temp_payload = target
        return target

    def _consume_payload_csv(self, **read_kwargs) -> pd.DataFrame:
        """Parse the payload CSV — reusing the preview's temp file when present
        (one download per run), otherwise downloading it now. Applies the
        requested row limit at parse time so slicing happens before full load."""
        temp = getattr(self, "_temp_payload", None)
        if not (temp and Path(temp).exists()):
            url = self._payload_url()
            if not url:
                raise ValueError("No payload URL available for this fetcher.")
            temp = self._download_to_tempfile(url, headers=self._payload_headers())
        limit = self.row_limit if self.row_limit and self.row_limit > 0 else None
        df = pd.read_csv(
            temp, sep=self._payload_sep(), compression=self._payload_compression(),
            nrows=limit, low_memory=False, **read_kwargs,
        )
        return df

    def pre_flight_authorization(self, metadata: dict) -> int:
        """Pre-flight check asking user confirmation or specific row limit."""
        if self.auto_approve:
            return 0
            
        print("\n" + "="*50)
        print(" PRE-FLIGHT AUTHORIZATION REQUIRED")
        print("="*50)
        print(f"Target URL : {metadata.get('url', 'Unknown')}")
        print(f"Data Size  : {metadata.get('size_info', 'Unknown (Determined at runtime)')}")
        print("-" * 50)
        
        while True:
            ans = input("Do you want to extract this entire dataset? (y/n) or type a number to extract a specific number of rows: ").strip().lower()
            if ans in ['y', 'yes']:
                return 0
            elif ans in ['n', 'no']:
                raise DataFetchError(
                    "Extraction aborted by user during Pre-Flight Authorization.",
                    code="ABORTED",
                )
            elif ans.isdigit():
                return int(ans)
            else:
                print("[Error] Invalid input. Please type 'y', 'n', or a number.")

    def validate_payload(self, df: pd.DataFrame) -> None:
        """Universal Payload Validator checking null density."""
        if df.empty:
            raise ValueError("Graceful Fallback Triggered: Dataframe is completely empty.")
        
        null_ratio = df.isnull().sum().sum() / df.size
        if null_ratio > 0.8:
            logger.warning("High null density detected (%.2f%%). Dataset may be sparse by design.", null_ratio * 100)
            print(f"[Validator] Warning: Null density is {null_ratio:.2%}. Proceeding — dataset may be sparse by design (e.g. SEC XBRL, Eurostat pivot).")
        else:
            print("[Validator] Payload passed Null-Density Check.")

    def _df_to_markdown_table(self, df_slice: pd.DataFrame) -> str:
        """Convert a DataFrame slice to a Markdown table without external dependencies."""
        columns = list(df_slice.columns)
        headers = " | ".join(str(col).replace("\n", " ").replace("|", "\\|") for col in columns)
        separator = " | ".join("---" for _ in columns)
        
        rows = []
        for _, row in df_slice.iterrows():
            row_str = " | ".join(str(val).replace("\n", " ").replace("|", "\\|") for val in row)
            rows.append(row_str)
            
        return f"| {headers} |\n| {separator} |\n" + "\n".join(f"| {r} |" for r in rows)

    MAX_PROFILE_COLUMNS = 30  # wide frames: profile the first N columns, roll up the rest

    def generate_markdown_profile(self, df: pd.DataFrame, csv_filepath: str) -> None:
        """Generate a rich Markdown descriptive profile of the dataset.

        Profiling cost is column-driven, not row-driven: schema/stats cover the
        first MAX_PROFILE_COLUMNS columns (with a rollup note for the rest) so
        1,000+-column frames stay sub-second instead of multi-second.
        """
        csv_path = Path(csv_filepath)
        dataset_name = csv_path.stem.replace("_raw", "")
        md_filepath = csv_path.parent / f"{dataset_name}_description.md"
        self.last_description_path = str(md_filepath)

        source_name = self.__class__.__name__.replace("Fetcher", "")

        # 1. Data Story / Overview
        story = (
            f"This dataset contains raw data related to **{self.query}**, sourced dynamically from **{source_name}**.\n\n"
            f"As a raw extract, it represents an unmodified snapshot of the target domain entity. "
            f"This profile was automatically compiled by the Data Fetcher Pipeline agent interface to assist in exploratory data analysis and database ingestion workflows."
        )

        # 2. Metadata Summary
        total_rows = len(df)
        total_cols = len(df.columns)
        profiled_cols = df.columns[: self.MAX_PROFILE_COLUMNS]
        truncated_note = (
            f"\n\n*Note: showing the first {self.MAX_PROFILE_COLUMNS} of {total_cols} columns in schema/statistics tables.*"
            if total_cols > self.MAX_PROFILE_COLUMNS else ""
        )
        null_count = df.isnull().sum().sum()
        total_cells = df.size
        null_density = null_count / total_cells if total_cells > 0 else 0

        metadata_md = (
            f"* **Source URL:** {self.dataset_url}\n"
            f"* **Original Filename:** {csv_path.name}\n"
            f"* **Generated At (UTC):** {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
            f"* **Total Rows:** {total_rows}\n"
            f"* **Total Columns:** {total_cols}\n"
            f"* **Overall Missing Value Density:** {null_density:.2%}\n"
            + (f"* **Truncation:** {json.dumps(self.truncation)}\n" if self.truncation else "")
        )

        # 3. Data Preview
        try:
            preview_df = df.head(5).copy()
            for col in preview_df.columns:
                preview_df[col] = preview_df[col].astype(str)
        except (TypeError, ValueError):
            preview_df = df.head(5).fillna("N/A").astype(str)
        preview_md = self._df_to_markdown_table(preview_df)

        # Use sampled data for both schema and statistics to avoid O(n) scans on large datasets
        sample = df.sample(n=min(100_000, len(df)), random_state=42) if len(df) > 100_000 else df

        # 4. Column Schema & Health Table (bounded to the first N columns)
        schema_rows = []
        for col in profiled_cols:
            dtype = str(df[col].dtype)
            unique_count = sample[col].nunique(dropna=True)
            col_nulls = df[col].isnull().sum()
            col_null_pct = col_nulls / total_rows if total_rows > 0 else 0
            null_str = f"{col_nulls} ({col_null_pct:.2%})"
            schema_rows.append(f"| {col} | {dtype} | {unique_count} | {null_str} |")

        schema_table = (
            "| Column Name | Data Type (Dtype) | Unique Values Count | Null Density (Count & Percentage) |\n"
            "| --- | --- | --- | --- |\n" + "\n".join(schema_rows) + truncated_note
        )

        # 5. Summary Statistics (bounded to the same N columns)
        if df.empty or len(df.columns) == 0:
            stats_md = "*No data available for statistics.*"
        else:
            desc_df = sample[profiled_cols].describe(include='all').reset_index().fillna("N/A").astype(str)
            stats_md = self._df_to_markdown_table(desc_df)

        # Assemble Content
        content = (
            f"# Dataset Profile: {dataset_name}\n\n"
            f"## Overview\n{story}\n\n"
            f"## Metadata Summary\n{metadata_md}\n"
            f"## Data Preview (First 5 Rows)\n{preview_md}\n\n"
            f"## Column Schema & Health\n{schema_table}\n\n"
            f"## Summary Statistics\n{stats_md}\n"
        )

        try:
            md_tmp = md_filepath.with_name(md_filepath.name + ".tmp")
            with open(md_tmp, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(md_tmp, md_filepath)
            print(f"[Success] Automated Dataset Markdown Profile generated at {md_filepath}")
        except OSError as e:
            print(f"[Warning] Failed to generate markdown profile: {e}")

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _append_manifest(self, csv_path: Path) -> None:
        """Record the provenance entry for this fetch (restatement/truncation forensics).

        The manifest is rewritten atomically (tmp + os.replace): a crash can never
        leave a torn last line, and stale torn lines from older versions are
        silently repaired on the next append.
        """
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": self.source_key,
            "query": self.query,
            "url": self.dataset_url,
            "rows": self.rows_count,
            "cols": self.cols_count,
            "bytes": self.last_bytes,
            "sha256": self.last_sha256,
            "complete": self.complete,
            "warnings": list(self.completeness_warnings),
            "truncation": self.truncation,
        }
        manifest = csv_path.parent / "manifest.jsonl"
        try:
            lines: List[str] = []
            if manifest.exists():
                for line in manifest.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        json.loads(line)
                    except json.JSONDecodeError:
                        # Torn line from a pre-atomic-era crash — drop and note
                        print(f"[Recovery] Dropped torn manifest line: {line[:50]}...", file=sys.stderr)
                        continue
                    lines.append(line)
            lines.append(json.dumps(entry))
            tmp_manifest = manifest.with_name(manifest.name + ".tmp")
            with open(tmp_manifest, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_manifest, manifest)
        except OSError as e:
            print(f"[Warning] Failed to update manifest: {e}", file=sys.stderr)

    def _reconcile_incomplete_commit(self, filepath: Path, tmp_path: Path, prev_path: Path) -> None:
        """Self-heal a prior crash between commit steps: a missing canonical file
        with a .tmp or .prev sibling is restored before anything else runs."""
        if filepath.exists():
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)  # stale debris from a crashed write
            return
        if tmp_path.exists():
            os.replace(tmp_path, filepath)
            print(f"[Recovery] Canonical file was missing — promoted stranded temp from a previous crash.", file=sys.stderr)
        elif prev_path.exists():
            shutil.copyfile(prev_path, filepath)
            print(f"[Recovery] Canonical file was missing — restored from .prev.", file=sys.stderr)

    def save_csv(self, df: pd.DataFrame, filename: str) -> str:
        """Atomically save raw data, retain the prior version, log provenance, profile.

        Commit order guarantees the canonical path is never absent: the new file
        is written+fsynced as .tmp, the prior version is refreshed into .prev via
        a copy (canonical never moves), then one os.replace() promotes .tmp.
        A crash can therefore leave: canonical intact (pre-promote) or canonical
        new (post-promote) — never missing, never torn. A crash BEFORE the
        promote leaves the canonical old and .tmp debris, which the next run's
        self-heal reconciles.
        """
        self.rows_count = len(df)
        self.cols_count = len(df.columns)
        filepath = Path(self.outdir) / filename
        tmp_path = filepath.with_name(filepath.name + ".tmp")
        prev_path = filepath.with_name(filepath.name + ".prev")
        self._reconcile_incomplete_commit(filepath, tmp_path, prev_path)

        try:
            df.to_csv(tmp_path, index=False)
            # fsync needs a writable handle on Windows ("rb" fails with EBADF)
            with open(tmp_path, "ab") as f:
                os.fsync(f.fileno())
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        self.last_sha256 = self._sha256_file(tmp_path)

        if filepath.exists():
            prev_tmp = prev_path.with_name(prev_path.name + ".tmp")
            shutil.copyfile(filepath, prev_tmp)
            os.replace(prev_tmp, prev_path)
            print(f"[Retained] Prior version kept as {prev_path.name}", file=sys.stderr)
        try:
            os.replace(tmp_path, filepath)
        except PermissionError:
            # Windows share-lock (Excel/AV holding the file): brief retry, then a
            # coded failure that names the stranded temp holding the new dataset
            for wait in (0.5, 1.0, 2.0):
                time.sleep(wait)
                try:
                    os.replace(tmp_path, filepath)
                    break
                except PermissionError:
                    continue
            else:
                raise DataFetchError(
                    f"Output file is locked by another process: {filepath}. "
                    f"The complete new dataset is preserved at {tmp_path} — close the "
                    "file and retry, or move it manually.",
                    code="OUTPUT_LOCKED",
                )
        self.last_bytes = filepath.stat().st_size
        print(f"[Success] Raw dataset saved atomically to {filepath}")

        # Generate the new Markdown description profile
        self.generate_markdown_profile(df, str(filepath))
        self._append_manifest(filepath)

        return str(filepath)

    def run(self) -> str:
        """Core execution pipeline coordinating scout, preview, preflight, extract, validate, and save."""
        print(f"[{self.__class__.__name__}] Initiating extraction sequence for query: '{self.query}'")
        metadata = self.scout()
        
        if metadata:
            self.dataset_url = metadata.get("url", "N/A")
        
        # Pre-flight authorization BEFORE any download; a preset row limit
        # (e.g. --rows N) survives auto-approve instead of being clobbered
        if metadata:
            self.row_limit = self.row_limit or self.pre_flight_authorization(metadata)
        else:
            self.row_limit = self.row_limit or 0
        
        # Preview (lightweight) — only if interactive
        if not self.auto_approve:
            try:
                self.preview()
            except (ValueError, RuntimeError, ImportError, OSError) as exc:
                print(f"[Warning] Failed to generate dataset preview: {exc}")
        
        # Full extraction through save: the streamed payload temp is consumed
        # anywhere in this span, so cleanup must cover failures in ALL of it
        try:
            if self._cached_df is not None:
                df = self._cached_df
            else:
                df = self.extract()

            if self.row_limit > 0 and len(df) > self.row_limit:
                print(f"[Engine] Slicing dataset to requested {self.row_limit} rows...")
                self.truncation = {
                    "rows": self.row_limit,
                    "population": len(df),
                    "by": "user",
                }
                df = df.head(self.row_limit)
            elif self.row_limit > 0 and self.truncation is None and len(df) == self.row_limit:
                # Provider/parse pushed the cut: record the slice with the known
                # population (worldbank sets it) or counted from the temp payload
                population = getattr(self, "_population_estimate", None)
                if population is None:
                    temp = getattr(self, "_temp_payload", None)
                    if temp and Path(temp).exists():
                        try:
                            with open(temp, "rb") as f:
                                population = max(sum(1 for _ in f) - 1, 0)  # minus header
                        except OSError:
                            population = None
                if population is None or population > len(df):
                    # rows == population means the source itself was exhausted —
                    # that is a FULL fetch, not a truncation
                    self.truncation = {"rows": len(df), "population": population, "by": "user"}

            self.validate_payload(df)

            safe_filename = sanitize_name(self.query)
            csv_filename = f"{safe_filename}_raw.csv"
            return self.save_csv(df, csv_filename)
        finally:
            # The streamed payload temp served preview + extract; it must not
            # linger on disk next to the dataset — even on extract/validate failure
            temp = getattr(self, "_temp_payload", None)
            if temp:
                try:
                    Path(temp).unlink(missing_ok=True)
                except OSError:
                    pass
                self._temp_payload = None
