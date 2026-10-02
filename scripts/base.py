# Public module aliases retained for existing output-lock and recovery callers.
import os
import time
from abc import ABC, abstractmethod
from typing import Dict, Optional
from pathlib import Path

import pandas as pd

from scripts.core.models import FetchState
from scripts.adapters.provider import FetcherProvider
from scripts.core.names import sanitize_name
from scripts.application.frame_policy import select_columns, null_density
from scripts.composition import (fetch_use_case, interaction_for, artifact_store_for,
                                 payload_reader_for, file_sha256)
from scripts.http_utils import request_with_retry
from scripts.provision import provision_data_directory, PLATFORMS  # re-exported for compat

__all__ = ["sanitize_name", "provision_data_directory", "PLATFORMS", "BaseFetcher"]

class BaseFetcher(FetchState, ABC):
    """Provider hooks and compatible entry points composed around the application."""

    def __init__(self, query: str, outdir: str, config: Dict[str, str], auto_approve: bool = False, source: Optional[str] = None) -> None:
        super().__init__(query=query, provider_name=self.__class__.__name__, auto_approve=auto_approve)
        self.config = config
        self.MAX_PROFILE_COLUMNS = type(self).MAX_PROFILE_COLUMNS

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
        self.outdir = str(Path(outdir) / clean_source / sanitize_name(self.query))
        Path(self.outdir).mkdir(parents=True, exist_ok=True)

    def _artifact_store(self):
        return artifact_store_for(self)

    def _payload_reader(self):
        return payload_reader_for(self, request_with_retry)

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
        FetcherProvider(self).preview_default()

    PREVIEW_BYTES = 256 * 1024

    def _bounded_csv_preview(self, url: str) -> pd.DataFrame:
        return self._payload_reader()._bounded_csv_preview(url)

    def _select_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        return select_columns(df, self.required_columns)

    def show_preview(self, df: pd.DataFrame) -> None:
        self.validate_payload(df)
        self.preview_frame = self._select_columns(df).head(5)
        interaction_for(self).display_preview(self.preview_frame)

    def preview_transfer_scope(self) -> str:
        if self._payload_url() and not self._payload_compression():
            return 'bounded CSV/TSV prefix up to 256 KB; a later full fetch may request the file again'
        if self.lightweight_preview:
            return 'small provider API sample; final extraction is separate'
        return self.transfer_scope or 'full transfer/extraction needed for preview; cached until final approval'

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
        return self._payload_reader()._download_to_tempfile(url, headers)

    def _consume_payload_csv(self, **read_kwargs) -> pd.DataFrame:
        return self._payload_reader()._consume_payload_csv(**read_kwargs)

    def pre_flight_authorization(self, metadata: dict) -> int:
        return interaction_for(self).approve_sample(metadata, self.row_limit)

    def validate_payload(self, df: pd.DataFrame) -> None:
        interaction_for(self).show_validation(null_density(df))

    MAX_PROFILE_COLUMNS = 30

    def generate_markdown_profile(self, df: pd.DataFrame, csv_filepath: str) -> None:
        return self._artifact_store().generate_markdown_profile(df, csv_filepath)

    def _write_markdown_profile(self, md_filepath: Path, content: str) -> None:
        return self._artifact_store()._write_markdown_profile(md_filepath, content)

    @staticmethod
    def _sha256_file(path: Path) -> str:
        return file_sha256(path)

    def _retain_original_payload(self) -> None:
        return self._artifact_store()._retain_original_payload()

    def _append_manifest(self, csv_path: Path) -> None:
        return self._artifact_store()._append_manifest(csv_path)

    def _reconcile_incomplete_commit(self, filepath: Path, tmp_path: Path, prev_path: Path) -> None:
        return self._artifact_store()._reconcile_incomplete_commit(filepath, tmp_path, prev_path)

    def save_csv(self, df: pd.DataFrame, filename: str) -> str:
        return self._artifact_store().save_csv(df, filename)

    def run(self) -> str:
        artifacts = fetch_use_case(self).run()
        return artifacts.csv_path if artifacts else ""
