import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from scripts.cli import main
from scripts.fetchers.fred import FREDFetcher
from scripts.format_alchemy import FormatAlchemyEngine


def test_excel_write_only_roundtrip(tmp_path: Path) -> None:
    """Verify write-only Excel generation roundtrips accurately without residual temp files."""
    column_payload: Dict[str, List[int]] = {f"metric_{idx}": list(range(150)) for idx in range(5)}
    source_df = pd.DataFrame(column_payload)
    csv_file = tmp_path / "timeseries_raw.csv"
    source_df.to_csv(csv_file, index=False)

    start_time = time.perf_counter()
    converted_path = FormatAlchemyEngine.convert(str(csv_file), "excel")
    elapsed_duration = time.perf_counter() - start_time

    excel_file = Path(converted_path)
    staging_file = excel_file.with_name(excel_file.name + ".tmp")

    assert excel_file.is_file()
    assert not staging_file.exists()
    assert elapsed_duration < 60.0

    roundtrip_df = pd.read_excel(excel_file, engine="openpyxl")
    assert len(roundtrip_df) == 150


def test_excel_column_cap_guard(tmp_path: Path) -> None:
    """Verify Excel converter enforces the 16,384 column specification boundary."""
    oversized_columns: Dict[str, List[int]] = {f"col_{idx}": [1] for idx in range(16385)}
    wide_df = pd.DataFrame(oversized_columns)
    csv_file = tmp_path / "oversized_matrix_raw.csv"
    wide_df.to_csv(csv_file, index=False)

    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (1,)
    mock_cursor.description = [(f"col_{idx}",) for idx in range(16385)]
    mock_connection = MagicMock()
    mock_connection.cursor.return_value = mock_cursor
    mock_connection.__enter__.return_value = mock_connection

    with patch.object(FormatAlchemyEngine, "csv_to_sqlite"), \
         patch("sqlite3.connect", return_value=mock_connection):
        with pytest.raises(ValueError, match="16384"):
            FormatAlchemyEngine.convert(str(csv_file), "excel")


def test_profiler_bounds_wide_frames(tmp_path: Path) -> None:
    """Verify markdown profile generation bounds column scans on ultra-wide dataframes."""
    wide_matrix: Dict[str, List[float]] = {
        f"sensor_{col_idx}": [float(row_idx) for row_idx in range(50)]
        for col_idx in range(3000)
    }
    wide_df = pd.DataFrame(wide_matrix)
    fetcher = FREDFetcher("WIDE_INDICATORS", str(tmp_path), {})

    start_time = time.perf_counter()
    output_path = fetcher.save_csv(wide_df, "wide_indicators_raw.csv")
    elapsed_duration = time.perf_counter() - start_time

    assert elapsed_duration < 5.0
    profile_path = Path(output_path).parent / "wide_indicators_description.md"
    assert profile_path.is_file()

    profile_text = profile_path.read_text(encoding="utf-8")
    assert "showing the first 30 of 3000 columns" in profile_text
    assert "Generated At (UTC)" in profile_text


def test_manifest_truncation_null_by_default(tmp_path: Path) -> None:
    """Verify unsliced dataset acquisitions record explicit null truncation in provenance."""
    fetcher = FREDFetcher("POPULATION", str(tmp_path), {})
    fetcher.auto_approve = True
    complete_df = pd.DataFrame({"observation": [10.5, 20.1, 30.8]})

    with patch.object(FREDFetcher, "extract", return_value=complete_df), \
         patch.object(FREDFetcher, "scout", return_value={"url": "https://fred.stlouisfed.org/series/POPULATION", "size_info": "3"}):
        fetcher.run()

    manifest_file = Path(fetcher.outdir) / "manifest.jsonl"
    manifest_record: Dict[str, Any] = json.loads(manifest_file.read_text(encoding="utf-8").strip())
    assert manifest_record["truncation"] is None
    assert manifest_record["complete"] is True


def test_list_sources_v2_shape(tmp_path: Path) -> None:
    """Verify source catalog CLI discovery schema conforms to v2 machine specifications."""
    pipeline_root = Path(__file__).resolve().parent.parent
    cli_entrypoint = pipeline_root / "scripts" / "cli.py"

    process_result = subprocess.run(
        [sys.executable, str(cli_entrypoint), "--list-sources", "--json-output"],
        cwd=str(pipeline_root),
        capture_output=True,
        text=True,
        check=True,
    )
    catalog_envelope: Dict[str, Any] = json.loads(process_result.stdout.strip().splitlines()[-1])
    assert catalog_envelope.get("status") == "success"

    sources_catalog: List[Dict[str, str]] = catalog_envelope.get("sources", [])
    assert len(sources_catalog) == 12
    required_attributes = {"key", "platform", "query_format", "example", "auth"}
    for source_entry in sources_catalog:
        assert required_attributes.issubset(source_entry.keys())

    coingecko_entry = next(src for src in sources_catalog if src.get("key") == "coingecko")
    assert "none" in coingecko_entry["auth"].lower()


def test_envelope_has_source_query_echo(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Verify CLI success envelope faithfully echoes query and source identity."""
    mock_fetcher = MagicMock()
    mock_fetcher.run.return_value = str(tmp_path / "gdp_raw.csv")
    mock_fetcher.rows_count = 50
    mock_fetcher.cols_count = 2
    mock_fetcher.complete = True
    mock_fetcher.completeness_warnings = []
    mock_fetcher.truncation = None
    mock_fetcher.last_sha256 = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
    mock_fetcher.last_bytes = 1024
    mock_fetcher.last_description_path = ""
    mock_fetcher.dataset_url = "https://fred.stlouisfed.org/series/GDP"
    mock_fetcher.resolved_title = "Gross Domestic Product"

    cli_arguments = [
        "cli", "--source", "fred", "--query", "GDP",
        "--outdir", str(tmp_path), "--yes", "--non-interactive", "--json-output",
    ]
    with patch("sys.argv", cli_arguments), \
         patch("scripts.cli.setup_wizard", return_value={}), \
         patch("scripts.cli.get_fetcher", return_value=mock_fetcher):
        main()

    response_envelope: Dict[str, Any] = json.loads(capsys.readouterr().out.strip())
    assert response_envelope["source"] == "fred"
    assert response_envelope["query"] == "GDP"
