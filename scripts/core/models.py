"""Run state contains values only; paths are opaque to the application."""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FetchRequest:
    source: str
    query: str
    outdir: str
    goal: str = ''
    columns: list[str] = field(default_factory=list)
    rows: int = 0
    output_format: str = 'csv'


@dataclass(frozen=True)
class SavedArtifacts:
    csv_path: str
    sha256: str
    bytes: int
    description_path: str
    manifest_path: str
    original_path: str
    original_sha256: str
    original_bytes: int
    warnings: tuple[str, ...]


@dataclass(eq=False)
class FetchState:
    query: str = ''
    provider_name: str = ''
    source_key: str = ''
    outdir: str = ''
    auto_approve: bool = False
    row_limit: int = 0
    preview_only: bool = False
    preview_frame: Any = None
    required_columns: list[str] = field(default_factory=list)
    requested_goal: str = ''
    transfer_scope: str = ''
    lightweight_preview: bool = False
    _cached_df: Any = None
    _temp_payload: Any = None
    dataset_url: str = 'N/A'
    rows_count: int = 0
    cols_count: int = 0
    complete: bool = True
    completeness_warnings: list[str] = field(default_factory=list)
    artifact_warnings: list[str] = field(default_factory=list)
    resolved_title: str | None = None
    truncation: dict | None = None
    last_sha256: str = ''
    last_bytes: int = 0
    last_description_path: str = ''
    last_manifest_path: str = ''
    last_original_path: str = ''
    last_original_sha256: str = ''
    last_original_bytes: int = 0
    payload_sep: str = ','
    payload_compression: str | None = None
    MAX_PROFILE_COLUMNS: int = 30

    def saved_artifacts(self, csv_path: str) -> SavedArtifacts:
        return SavedArtifacts(
            csv_path, self.last_sha256, self.last_bytes,
            self.last_description_path, self.last_manifest_path,
            self.last_original_path, self.last_original_sha256,
            self.last_original_bytes, tuple(self.artifact_warnings),
        )


@dataclass(frozen=True)
class PayloadSpec:
    url: str | None
    headers: dict[str, str]
    separator: str
    compression: str | None
    preview_bytes: int
    max_bytes: int
