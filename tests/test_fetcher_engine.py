import json
import time
import pytest
import requests as requests_lib
from pathlib import Path
from unittest.mock import patch, mock_open, MagicMock

import pandas as pd

from scripts.config import setup_wizard
from scripts.errors import DataFetchError
from scripts.factory import get_fetcher, list_sources
from scripts.fetchers.fred import FREDFetcher
from scripts.fetchers.openml_fetcher import OpenMLFetcher
from scripts.fetchers.worldbank import WorldBankFetcher
from scripts.fetchers.github_data import GitHubDataFetcher
from scripts.fetchers.datagov import DataGovFetcher
from scripts.fetchers.eurostat import EurostatFetcher
from scripts.fetchers.generic import GenericFetcher
from scripts.format_alchemy import FormatAlchemyEngine
from scripts.web_analyzer import WebAnalyzer, AnalysisReport

def test_setup_wizard_existing_valid_config() -> None:
    """Verify that an existing valid config.json bypasses the wizard and loads state."""
    mock_config = '{"FRED_API_KEY": "test_key"}'
    with patch.object(Path, 'exists', return_value=True):
        with patch('builtins.open', mock_open(read_data=mock_config)):
            config = setup_wizard()
            assert config.get("FRED_API_KEY") == "test_key"

def test_setup_wizard_manual_invalid_json() -> None:
    """Verify JSONDecodeError resilience during manual wizard configuration."""
    with patch.object(Path, 'exists', side_effect=[False, True, True]):
        # Mock inputs: select manual (2), enter 'done' (json fails), enter 'skip' to exit loop
        with patch('builtins.input', side_effect=['2', 'done', 'skip']):
            with patch('builtins.open', mock_open(read_data='{invalid_json:')):
                with patch('json.load', side_effect=json.JSONDecodeError("Expecting value", "", 0)):
                    config = setup_wizard()
                    assert isinstance(config, dict)
                    assert len(config) == 0

def test_get_fetcher_strategy_routing() -> None:
    """Verify polymorphic Strategy pattern extraction routing."""
    from scripts.fetchers.yahoo import YahooFinanceFetcher
    config: dict[str, str] = {}
    
    fetcher_fred = get_fetcher("fred", "GDP", "/tmp/out", config)
    assert isinstance(fetcher_fred, FREDFetcher)
    
    fetcher_openml = get_fetcher("openml", "finance", "/tmp/out", config)
    assert isinstance(fetcher_openml, OpenMLFetcher)
    
    fetcher_wb = get_fetcher("worldbank", "NY.GDP.MKTP.CD", "/tmp/out", config)
    assert isinstance(fetcher_wb, WorldBankFetcher)

    fetcher_gh = get_fetcher("github", "covid", "/tmp/out", config)
    assert isinstance(fetcher_gh, GitHubDataFetcher)

    fetcher_dg = get_fetcher("datagov", "weather", "/tmp/out", config)
    assert isinstance(fetcher_dg, DataGovFetcher)

    fetcher_es = get_fetcher("eurostat", "nama_10_gdp", "/tmp/out", config)
    assert isinstance(fetcher_es, EurostatFetcher)
    
    fetcher_yf = get_fetcher("yahoo", "AAPL", "/tmp/out", config)
    assert isinstance(fetcher_yf, YahooFinanceFetcher)

    fetcher_generic = get_fetcher("unknown_source", "data", "/tmp/out", config)
    assert isinstance(fetcher_generic, GenericFetcher)

def test_generic_fetcher_raises_actionable_error() -> None:
    """Verify GenericFetcher raises ValueError with actionable message including supported sources."""
    fetcher = get_fetcher("unknown_source", "data", "/tmp/out", {})
    with pytest.raises(ValueError) as exc_info:
        fetcher.extract()
    err_msg = str(exc_info.value)
    assert "Unsupported data source 'unknown_source'" in err_msg
    assert "Supported sources:" in err_msg
    assert "openml" in err_msg

def test_format_alchemy_convert_dispatcher() -> None:
    """Test format alchemy conversion dispatcher mocks."""
    with patch.object(Path, 'exists', return_value=True):
        with patch.object(FormatAlchemyEngine, 'xlsx_to_csv', return_value="out.csv") as mock_xlsx:
            res = FormatAlchemyEngine.convert("test.xlsx", "csv")
            assert res == "out.csv"
            mock_xlsx.assert_called_once_with("test.xlsx", "test.csv")

        with patch.object(FormatAlchemyEngine, 'json_to_csv', return_value="out.csv") as mock_json:
            res = FormatAlchemyEngine.convert("test.json", "csv")
            assert res == "out.csv"
            mock_json.assert_called_once_with("test.json", "test.csv")

        with patch.object(FormatAlchemyEngine, 'parquet_to_csv', return_value="out.csv") as mock_parquet:
            res = FormatAlchemyEngine.convert("test.parquet", "csv")
            assert res == "out.csv"
            mock_parquet.assert_called_once_with("test.parquet", "test.csv")

def test_web_analyzer_report_generation() -> None:
    """Test custom scraper analysis logic."""
    analyzer = WebAnalyzer("https://test.com/data.csv", "/tmp/out")
    with patch('scripts.web_analyzer.request_with_retry') as mock_req:
        mock_resp = mock_req.return_value
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "text/csv", "Content-Length": "1024"}

        with patch.object(analyzer, '_check_robots_txt', return_value=True), \
             patch("scripts.web_analyzer.socket.getaddrinfo",
                   return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
            report = analyzer.analyze()
            assert report.difficulty == "easy"
            assert report.status_code == 200
            assert report.content_type == "text/csv"
            assert report.content_length == 1024

def test_provision_data_directory(tmp_path: Path) -> None:
    """Verify that provision_data_directory successfully builds the target subfolders."""
    from scripts.base import provision_data_directory
    outdir = tmp_path / "data_raw"
    provision_data_directory(str(outdir))
    
    expected_subfolders = [
        "kaggle", "worldbank", "sec", "fred", "eurostat",
        "datagov", "openml", "airbnb", "github", "yahoo", "custom"
    ]
    for sub in expected_subfolders:
        assert (outdir / sub).is_dir()

def test_markdown_description_generation(tmp_path: Path) -> None:
    """Verify the creation of dataset description markdown profiles with proper contents."""
    from scripts.fetchers.fred import FREDFetcher
    config: dict[str, str] = {}
    
    fetcher = FREDFetcher("GDP", str(tmp_path), config)
    fetcher.dataset_url = "https://fred.stlouisfed.org/series/GDP"
    
    df = pd.DataFrame({
        "date": ["2023-01-01", "2023-04-01"],
        "value": [26000.0, 26200.0]
    })
    
    csv_file = Path(fetcher.outdir) / "gdp_raw.csv"
    df.to_csv(csv_file, index=False)
    
    fetcher.generate_markdown_profile(df, str(csv_file))
    
    md_file = Path(fetcher.outdir) / "gdp_description.md"
    assert md_file.is_file()
    
    content = md_file.read_text(encoding="utf-8")
    assert "Dataset Profile: gdp" in content
    assert "Metadata Summary" in content
    assert "Data Preview" in content
    assert "Column Schema & Health" in content
    assert "Summary Statistics" in content
    assert "GDP" in content

def test_markdown_profile_empty_dataframe(tmp_path: Path) -> None:
    """Verify MD profiler handles empty DataFrame gracefully."""
    from scripts.fetchers.fred import FREDFetcher
    fetcher = FREDFetcher("GDP", str(tmp_path), {})
    df = pd.DataFrame()
    csv_file = Path(fetcher.outdir) / "empty_raw.csv"
    df.to_csv(csv_file, index=False)
    
    # generate_markdown_profile should handle empty df gracefully without crashing
    fetcher.generate_markdown_profile(df, str(csv_file))
    md_file = Path(fetcher.outdir) / "empty_description.md"
    assert md_file.is_file()
    content = md_file.read_text(encoding="utf-8")
    assert "**Total Rows:** 0" in content

def test_markdown_profile_special_characters_in_columns(tmp_path: Path) -> None:
    """Verify pipe characters in column names don't break markdown tables."""
    from scripts.fetchers.fred import FREDFetcher
    fetcher = FREDFetcher("GDP", str(tmp_path), {})
    df = pd.DataFrame({
        "Col|With|Pipes": [1, 2],
        "Normal": ["A|B", "C"]
    })
    csv_file = Path(fetcher.outdir) / "special_raw.csv"
    df.to_csv(csv_file, index=False)
    
    fetcher.generate_markdown_profile(df, str(csv_file))
    md_file = Path(fetcher.outdir) / "special_description.md"
    assert md_file.is_file()
    content = md_file.read_text(encoding="utf-8")
    # Pipes in headers and cells should be escaped in the markdown table
    assert "Col\\|With\\|Pipes" in content
    assert "A\\|B" in content

def test_save_csv_readonly_directory(tmp_path: Path) -> None:
    """Verify save_csv raises OSError on read-only/invalid directories."""
    from scripts.fetchers.fred import FREDFetcher
    fetcher = FREDFetcher("GDP", str(tmp_path), {})
    # Set an invalid destination path that cannot be created or written to
    fetcher.outdir = "/invalid_directory_path_that_does_not_exist/subdir"
    df = pd.DataFrame({"A": [1]})
    with pytest.raises((OSError, FileNotFoundError)):
        fetcher.save_csv(df, "test.csv")

def test_provision_data_directory_custom_source(tmp_path: Path) -> None:
    """Verify dynamic source folders are auto-created for unknown sources."""
    from scripts.factory import get_fetcher
    # Use get_fetcher with a non-standard source name
    fetcher = get_fetcher("custom_api", "query", str(tmp_path), {})
    assert Path(fetcher.outdir).is_dir()
    assert Path(fetcher.outdir).parent.name == "custom_api"

def test_csv_to_sqlite_roundtrip(tmp_path: Path) -> None:
    """Create CSV, convert to SQLite, verify table exists and row count matches."""
    import sqlite3
    csv_file = tmp_path / "test_raw.csv"
    df = pd.DataFrame({
        "id": [1, 2, 3],
        "name": ["Alice", "Bob", "Charlie"]
    })
    df.to_csv(csv_file, index=False)
    
    engine = FormatAlchemyEngine(str(csv_file))
    engine.execute_pipeline()
    
    assert Path(engine.db_filepath).is_file()
    assert Path(engine.excel_filepath).is_file()
    
    # Read from SQLite and verify data matches
    with sqlite3.connect(engine.db_filepath) as conn:
        result_df = pd.read_sql_query(f"SELECT * FROM {engine._safe_table_name()}", conn)
        assert len(result_df) == 3
        assert list(result_df["name"]) == ["Alice", "Bob", "Charlie"]

def test_json_to_csv_conversion(tmp_path: Path) -> None:
    """Create JSON array, convert to CSV, verify column mapping."""
    json_file = tmp_path / "data.json"
    json_data = [
        {"id": 1, "details": {"name": "Alice", "age": 30}},
        {"id": 2, "details": {"name": "Bob", "age": 25}}
    ]
    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(json_data, f)
        
    csv_file = tmp_path / "data.csv"
    FormatAlchemyEngine.convert(str(json_file), "csv")

    assert csv_file.is_file()
    result_df = pd.read_csv(csv_file)
    assert len(result_df) == 2
    # Normalization check
    assert "details.name" in result_df.columns
    assert list(result_df["details.name"]) == ["Alice", "Bob"]

# ---------------------------------------------------------------------------
# Pass 6 — Agent safety, download hygiene, HTTP resilience, format roundtrips
# ---------------------------------------------------------------------------

def test_agent_mode_no_yes_flag_json_error(tmp_path: Path, capsys) -> None:
    """--non-interactive --json-output without --yes must complete without input()
    and always emit machine-readable JSON. Previously this scenario died on a
    silent EOFError with empty output (the agent-killer)."""
    from scripts.cli import main

    mock_fetcher = MagicMock()
    mock_fetcher.run.return_value = str(tmp_path / "weather_raw.csv")
    mock_fetcher.rows_count = 10
    mock_fetcher.cols_count = 3

    argv = [
        "cli", "--source", "datagov", "--query", "weather",
        "--outdir", str(tmp_path), "--non-interactive", "--json-output",
    ]
    # If any code path still reaches input(), EOFError is what a closed stdin
    # would produce — the fix must make this unreachable.
    with patch('sys.argv', argv), \
         patch('scripts.cli.setup_wizard', return_value={}), \
         patch('scripts.cli.get_fetcher', return_value=mock_fetcher), \
         patch('builtins.input', side_effect=EOFError("stdin closed")):
        main()

    assert mock_fetcher.auto_approve is True, "non-interactive mode must auto-approve"
    mock_fetcher.run.assert_called_once()

    json_lines = [l for l in capsys.readouterr().out.strip().splitlines() if l.startswith("{")]
    payload = json.loads(json_lines[-1])
    assert payload["status"] == "success"
    assert payload["rows"] == 10

def test_agent_mode_eoferror_emits_json_hint(tmp_path: Path, capsys) -> None:
    """Any residual EOFError in agent mode must emit a JSON error carrying the
    --yes recovery hint instead of exiting silently."""
    from scripts.cli import main

    mock_fetcher = MagicMock()
    mock_fetcher.run.side_effect = EOFError("stdin closed")

    argv = [
        "cli", "--source", "datagov", "--query", "weather",
        "--outdir", str(tmp_path), "--non-interactive", "--json-output",
    ]
    with patch('sys.argv', argv), \
         patch('scripts.cli.setup_wizard', return_value={}), \
         patch('scripts.cli.get_fetcher', return_value=mock_fetcher):
        with pytest.raises(SystemExit) as exc_info:
            main()

    assert exc_info.value.code == 1
    json_lines = [l for l in capsys.readouterr().out.strip().splitlines() if l.startswith("{")]
    payload = json.loads(json_lines[-1])
    assert payload["status"] == "error"
    assert "--yes" in payload["message"]

def test_request_call_count_during_run(tmp_path: Path) -> None:
    """A full run() must download the dataset payload exactly once — the preview
    cache prevents the historical double download."""
    search_response = MagicMock()
    search_response.status_code = 200
    search_response.json.return_value = {
        # GSA Catalog API v4 shape (DCAT), replacing the retired CKAN action API
        "results": [{
            "dcat": {
                "title": "Test Dataset",
                "distribution": [{"format": "CSV", "downloadURL": "https://data.example.com/dataset.csv"}]
            }
        }],
        "after": ""
    }
    csv_payload = b"city,temp\nCairo,30\nOslo,5\n"
    csv_response = MagicMock(status_code=200, headers={})
    csv_response.text = csv_payload.decode()
    csv_response.iter_content.side_effect = lambda *a, **k: iter([csv_payload])  # fresh iterator per call

    def fake_request(url: str, **kwargs):
        return csv_response if url.startswith("https://data.example.com") else search_response

    def download_calls(mock_req):
        return [c for c in mock_req.call_args_list if str(c.args[0]).startswith("https://data.example.com")]

    # Scenario A: auto-approved agent run — extract() downloads once.
    # One shared mock for BOTH import sites (fetcher scout + base downloader).
    shared_mock = MagicMock(side_effect=fake_request)
    with patch("scripts.fetchers.datagov.request_with_retry", shared_mock), \
         patch("scripts.base.request_with_retry", shared_mock):
        fetcher = DataGovFetcher("weather", str(tmp_path), {})
        fetcher.auto_approve = True
        csv_path = fetcher.run()
    assert len(download_calls(shared_mock)) == 1
    assert Path(csv_path).is_file()

    # Scenario B: interactive run — preview() must cache, extract() must not re-download
    shared_mock = MagicMock(side_effect=fake_request)
    with patch("scripts.fetchers.datagov.request_with_retry", shared_mock), \
         patch("scripts.base.request_with_retry", shared_mock), \
         patch("builtins.input", return_value="y"):
        fetcher = DataGovFetcher("weather", str(tmp_path / "interactive"), {})
        fetcher.run()
    assert len(download_calls(shared_mock)) == 1

def test_429_last_attempt_no_pointless_sleep() -> None:
    """After the final 429 the retry loop must exit without a pointless sleep."""
    from scripts import http_utils

    mock_session = MagicMock()
    mock_session.get.return_value = MagicMock(status_code=429, headers={})

    with patch.object(http_utils, 'session', mock_session):
        with patch.object(http_utils.time, 'sleep') as mock_sleep:
            with pytest.raises(DataFetchError) as exc_info:
                http_utils.request_with_retry("https://rate-limited.test", max_retries=3)

    assert mock_sleep.call_count == 2  # between attempts only, never after the last
    assert "429" in str(exc_info.value)
    assert exc_info.value.code == "RATE_LIMITED"

def test_retry_after_http_date_parsing() -> None:
    """_parse_retry_after handles integer seconds, RFC 7231 HTTP-dates, and garbage."""
    import email.utils
    from scripts.http_utils import _parse_retry_after

    assert _parse_retry_after("5", 9.0) == 5.0

    http_date = email.utils.formatdate(time.time() + 120, usegmt=True)
    delay = _parse_retry_after(http_date, 9.0)
    assert 100 < delay <= 125

    assert _parse_retry_after("not-a-date", 9.0) == 9.0

def test_sparse_dataframe_warning_not_error(tmp_path: Path, capsys) -> None:
    """>80% null DataFrame must pass validation with a warning, not crash —
    SEC XBRL and Eurostat pivots are legitimately sparse."""
    from scripts.fetchers.fred import FREDFetcher

    fetcher = FREDFetcher("SPARSE", str(tmp_path), {})
    df = pd.DataFrame({
        "a": [1.0, None, None, None, None],
        "b": [None, None, None, None, None],
    })
    fetcher.validate_payload(df)  # must not raise

    assert "sparse by design" in capsys.readouterr().out

def test_sqlite_to_csv_roundtrip(tmp_path: Path) -> None:
    """CSV -> SQLite -> CSV must preserve the data."""
    csv_file = tmp_path / "roundtrip_raw.csv"
    df = pd.DataFrame({"id": [1, 2, 3], "name": ["Alice", "Bob", "Charlie"]})
    df.to_csv(csv_file, index=False)

    engine = FormatAlchemyEngine(str(csv_file))
    engine.csv_to_sqlite()
    assert Path(engine.db_filepath).is_file()

    out_path = FormatAlchemyEngine.convert(engine.db_filepath, "csv")
    assert Path(out_path).is_file()
    pd.testing.assert_frame_equal(pd.read_csv(out_path), df)

def test_eurostat_scout_probes_and_downloads_once(tmp_path: Path) -> None:
    """Eurostat scout must validate with a lightweight streamed probe (no body
    download); the dataset itself must be downloaded exactly once per run()."""
    tsv_payload = b"unit,geo\\time\t2023\nNR,EG\t1\nNR,FR\t2\n"
    tsv_response = MagicMock(status_code=200, headers={})
    tsv_response.text = tsv_payload.decode()
    tsv_response.iter_content.side_effect = lambda *a, **k: iter([tsv_payload])  # fresh iterator per call

    def fake_request(url: str, **kwargs):
        return tsv_response

    # Scenario A: auto-approved run — scout probe (streamed) + ONE streamed body
    # download into the temp file; extract parses the temp, never re-fetching.
    shared_mock = MagicMock(side_effect=fake_request)
    with patch("scripts.fetchers.eurostat.request_with_retry", shared_mock), \
         patch("scripts.base.request_with_retry", shared_mock):
        fetcher = EurostatFetcher("nama_10_gdp", str(tmp_path), {})
        fetcher.auto_approve = True
        csv_path = fetcher.run()
    dissemination = [c for c in shared_mock.call_args_list if "dissemination" in str(c.args[0])]
    assert len(dissemination) == 2, f"probe + exactly one body download, got {len(dissemination)}"
    assert all(c.kwargs.get("stream") for c in dissemination), "all eurostat fetches are streamed (memory-flat)"
    assert Path(csv_path).is_file()

    # Scenario B: interactive run — preview streams the payload to the temp
    # file; extract REUSES it (still exactly one body download per run)
    shared_mock = MagicMock(side_effect=fake_request)
    with patch("scripts.fetchers.eurostat.request_with_retry", shared_mock), \
         patch("scripts.base.request_with_retry", shared_mock), \
         patch("builtins.input", return_value="y"):
        fetcher = EurostatFetcher("nama_10_gdp", str(tmp_path / "interactive"), {})
        fetcher.run()
    dissemination = [c for c in shared_mock.call_args_list if "dissemination" in str(c.args[0])]
    assert len(dissemination) == 2, f"probe + exactly one body download, got {len(dissemination)}"
    assert Path(csv_path).is_file()

def test_generated_scraper_retry_never_returns_none(tmp_path: Path) -> None:
    """The generated _fetch_with_retry must raise on exhaustion (never return
    None) and survive HTTP-date Retry-After headers without crashing."""
    analyzer = WebAnalyzer("https://test.com/data.csv", str(tmp_path))
    report = AnalysisReport(url="https://test.com/data.csv", difficulty="easy", status_code=200)
    script_path = analyzer.generate_script(report)

    content = Path(script_path).read_text(encoding="utf-8")
    func_src = content[content.index("def _fetch_with_retry"):content.index("def run_extraction")]

    fake_requests = MagicMock()
    rate_limited = MagicMock(status_code=429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})
    fake_requests.get.return_value = rate_limited
    fake_time = MagicMock()
    namespace = {"requests": fake_requests, "time": fake_time}
    exec(func_src, namespace)

    with pytest.raises(RuntimeError):
        namespace["_fetch_with_retry"]("https://test.com/data.csv", {})

    # HTTP-date Retry-After must fall back to a numeric exponential delay
    assert fake_time.sleep.call_count == 2  # between attempts only, never after the last
    for call in fake_time.sleep.call_args_list:
        assert isinstance(call.args[0], (int, float))

def test_kaggle_fetcher_uses_pathlib_exclusively() -> None:
    """Kaggle fetcher must rely on pathlib for path handling, matching the codebase convention."""
    import inspect
    from scripts.fetchers import kaggle_fetcher
    source = inspect.getsource(kaggle_fetcher)
    assert "os.path" not in source
    assert "glob.glob" not in source
    assert "Path" in source

def test_coingecko_routing_and_extract(tmp_path: Path) -> None:
    """CoinGecko routes via the factory and builds a tidy market-history frame."""
    from scripts.fetchers.coingecko import CoinGeckoFetcher

    fetcher = get_fetcher("coingecko", "btc", str(tmp_path), {})
    assert isinstance(fetcher, CoinGeckoFetcher)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "prices": [[1700000000000, 37000.0], [1700086400000, 37250.5]],
        "market_caps": [[1700000000000, 7.2e14], [1700086400000, 7.3e14]],
        "total_volumes": [[1700000000000, 1.5e10], [1700086400000, 1.4e10]],
    }
    fetcher.coin_id = "bitcoin"
    fetcher.market_url = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=max"
    with patch("scripts.fetchers.coingecko.request_with_retry", return_value=mock_resp):
        df = fetcher.extract()

    assert len(df) == 2
    assert list(df.columns) == ["timestamp_ms", "price_usd", "market_cap_usd", "volume_usd", "date"]
    assert df["price_usd"].iloc[1] == 37250.5
    assert df["volume_usd"].notna().all()

# ---------------------------------------------------------------------------
# OpenML scout semantics — ID bypass, relevance ranking, size guard, selection
# ---------------------------------------------------------------------------

def _openml_listing() -> pd.DataFrame:
    """Mocked OpenML dataset listing dataframe (name search + quality columns)."""
    return pd.DataFrame({
        "did": [61, 40678, 55, 42211, 42212],
        "name": ["iris", "iris-modified-huge", "wine", "wide-panel-a", "wide-panel-b"],
        "NumberOfInstances": [150, 4_000_000, 178, 500, 5_000],
        "NumberOfFeatures": [4, 3, 13, 20_000, 100],
    })

def test_openml_numeric_query_bypasses_search(tmp_path: Path) -> None:
    """A numeric query targets the dataset ID directly — the listing API is never called."""
    fetcher = OpenMLFetcher("61", str(tmp_path), {})
    fetcher.auto_approve = True

    with patch("openml.datasets.list_datasets") as mock_list:
        metadata = fetcher.scout()

    mock_list.assert_not_called()
    assert fetcher.target_dataset_id == 61
    assert metadata["url"] == "https://www.openml.org/d/61"

def test_openml_exact_match_wins_over_bigger_substrings(tmp_path: Path) -> None:
    """Exact name match beats a 4M-row substring derivative in auto mode."""
    fetcher = OpenMLFetcher("iris", str(tmp_path), {})
    fetcher.auto_approve = True

    with patch("openml.datasets.list_datasets", return_value=_openml_listing()):
        metadata = fetcher.scout()

    assert fetcher.target_dataset_id == 61
    assert fetcher.dataset_name == "iris"
    assert "150" in metadata["size_info"]

def test_openml_size_guard_skips_oversized_candidates(tmp_path: Path, capsys) -> None:
    """Candidates over the cell budget are skipped with a report, next fit is taken."""
    listing = _openml_listing()
    fetcher = OpenMLFetcher("wide-panel", str(tmp_path), {})
    fetcher.auto_approve = True

    with patch("openml.datasets.list_datasets", return_value=listing):
        fetcher.scout()

    # wide-panel-a: 500 x 20,000 = 10M cells (over budget) -> skipped
    # wide-panel-b: 5,000 x 100 = 500k cells -> selected
    assert fetcher.target_dataset_id == 42212
    out = capsys.readouterr().out
    assert "exceeds the size budget" in out

def test_openml_interactive_candidate_selection(tmp_path: Path) -> None:
    """Interactive mode lets the user pick a candidate by number; invalid input re-prompts."""
    fetcher = OpenMLFetcher("win", str(tmp_path), {})  # substring, no exact match
    assert fetcher.auto_approve is False

    listing = pd.DataFrame({
        "did": [55, 407],
        "name": ["wine", "wine-french"],
        "NumberOfInstances": [178, 1600],
        "NumberOfFeatures": [13, 11],
    })
    with patch("openml.datasets.list_datasets", return_value=listing), \
         patch("builtins.input", side_effect=["9", "2"]):
        fetcher.scout()

    # candidates ascending by instances: rank 1 = wine(55), rank 2 = wine-french(407)
    assert fetcher.target_dataset_id == 407
    assert fetcher.dataset_name == "wine-french"

def test_sanitize_name_edge_cases() -> None:
    """Unicode-only queries fall back to 'dataset'; long queries are capped; traversal is squashed."""
    from scripts.base import sanitize_name
    assert sanitize_name("القاهرة") == "dataset"
    assert sanitize_name("../../evil") == "evil"
    assert len(sanitize_name("x" * 300)) == 60

def test_worldbank_extract_paginates_all_rows() -> None:
    """World Bank extract must page through every result — never silently truncate at per_page."""
    def obs(country: str, code: str, year: str):
        return {"country": {"value": country}, "countryiso3code": code, "date": year,
                "value": 1.0, "indicator": {"id": "X", "value": "Test Indicator"}}

    pages = [
        MagicMock(status_code=200, json=lambda: [{"total": 3}, [obs("Egypt", "EGY", "2024"), obs("France", "FRA", "2024")]]),
        MagicMock(status_code=200, json=lambda: [{"total": 3}, [obs("USA", "USA", "2024")]]),
    ]
    fetcher = WorldBankFetcher("X", "/tmp/out", {})
    with patch("scripts.fetchers.worldbank.request_with_retry", side_effect=pages) as mock_req:
        df = fetcher.extract()

    assert len(df) == 3
    assert mock_req.call_count == 2
    assert "USA" in set(df["countryiso3code"])

# ---------------------------------------------------------------------------
# Hardening pass — startup smoke, SSRF/robots, credentials, CLI polish
# ---------------------------------------------------------------------------

def test_startup_smoke_imports_and_registry() -> None:
    """Smoke test: every module imports cleanly and the lazy registry is fully populated."""
    import scripts.cli
    import scripts.base
    import scripts.config
    import scripts.factory
    import scripts.errors
    import scripts.format_alchemy
    import scripts.http_utils
    import scripts.web_analyzer
    from scripts.base import BaseFetcher
    from scripts.factory import _LAZY_REGISTRY

    expected = {
        "airbnb", "coingecko", "datagov", "eurostat", "fred", "github",
        "kaggle", "openml", "sec", "worldbank", "yahoo", "yfinance",
    }
    assert set(_LAZY_REGISTRY.keys()) == expected
    assert callable(scripts.cli.main)

def test_cli_subprocess_list_sources_smoke() -> None:
    """The real CLI entry point starts, bootstraps, and answers --list-sources."""
    import subprocess
    import sys as _sys
    repo_root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [_sys.executable, str(repo_root / "scripts" / "cli.py"), "--list-sources", "--json-output"],
        capture_output=True, text=True, timeout=120, cwd=str(repo_root),
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["status"] == "success"
    assert len(payload["sources"]) == 12
    assert all({"key", "platform", "query_format", "example", "auth"} <= set(s) for s in payload["sources"])
    assert any(s["key"] == "coingecko" for s in payload["sources"])
    assert any(s["query_format"] == "series ID" for s in payload["sources"])

def test_robots_denial_is_hard_stop(tmp_path: Path) -> None:
    """A robots.txt-disallowed URL must never be fetched (difficulty=blocked, zero requests)."""
    analyzer = WebAnalyzer("https://example.com/private/data.csv", str(tmp_path))
    with patch("scripts.web_analyzer.socket.getaddrinfo",
               return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
        with patch.object(analyzer, "_check_robots_txt", return_value=False):
            with patch("scripts.web_analyzer.request_with_retry") as mock_req:
                report = analyzer.analyze()
    mock_req.assert_not_called()
    assert report.difficulty == "blocked"
    assert any("robots.txt" in w for w in report.warnings)

def test_ssrf_url_validation() -> None:
    """The SSRF guard blocks unsafe schemes and private/loopback/link-local targets."""
    from scripts.web_analyzer import validate_public_url

    blocked = [
        "ftp://example.com/file.csv",
        "http://127.0.0.1/admin",
        "https://192.168.1.1/router",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/internal",
        "http://localhost/data",
        "http://[::1]/data",
    ]
    for url in blocked:
        with pytest.raises(ValueError):
            validate_public_url(url)

    public_dns = [(2, 1, 6, "", ("93.184.216.34", 0))]
    with patch("scripts.web_analyzer.socket.getaddrinfo", return_value=public_dns):
        validate_public_url("https://example.com/data.csv")  # public resolution passes

    rebind_dns = [(2, 1, 6, "", ("10.0.0.5", 0))]
    with patch("scripts.web_analyzer.socket.getaddrinfo", return_value=rebind_dns):
        with pytest.raises(ValueError):
            validate_public_url("https://evil-rebind.example.com/data")

def test_redirect_to_private_ip_blocked(tmp_path: Path) -> None:
    """A redirect hop into private IP space must be rejected before the request is made."""
    analyzer = WebAnalyzer("https://public.example.com/file", str(tmp_path))
    redirect_resp = MagicMock()
    redirect_resp.status_code = 302
    redirect_resp.headers = {"Location": "http://192.168.0.1/steal"}
    with patch("scripts.web_analyzer.socket.getaddrinfo",
               return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
        with patch.object(analyzer, "_check_robots_txt", return_value=True):
            with patch("scripts.web_analyzer.request_with_retry", return_value=redirect_resp):
                with pytest.raises(ValueError):
                    analyzer.analyze()

def test_generated_script_url_safely_escaped(tmp_path: Path) -> None:
    """URLs containing quotes/backslashes embed as serialized data and yield valid Python."""
    tricky = 'https://test.com/a"b\\c.csv?q=1'
    analyzer = WebAnalyzer(tricky, str(tmp_path))
    report = AnalysisReport(url=tricky, difficulty="easy", status_code=200)
    script_path = analyzer.generate_script(report)

    content = Path(script_path).read_text(encoding="utf-8")
    compile(content, script_path, "exec")  # generated source must be syntactically valid

def test_config_wizard_masks_secrets_and_restricts_permissions(tmp_path: Path) -> None:
    """Secrets are collected via masked getpass input and the config is chmod 600."""
    cfg_dir = tmp_path / "cfg"
    cfg_file = str(cfg_dir / "config.json")
    with patch("scripts.config.get_config_paths", return_value=(str(cfg_dir), cfg_file)), \
         patch("builtins.input", side_effect=["1", "ziad"]), \
         patch("scripts.config.getpass", side_effect=["kaggle-key", "", ""]) as mock_gp, \
         patch("scripts.config.os.chmod") as mock_chmod:
        config = setup_wizard()

    assert config["KAGGLE_USERNAME"] == "ziad"
    assert config["KAGGLE_KEY"] == "kaggle-key"
    mock_gp.assert_called()  # secrets must go through masked input, not plain input()
    mock_chmod.assert_called_once_with(cfg_file, 0o600)
    saved = json.loads(Path(cfg_file).read_text(encoding="utf-8"))
    assert saved["KAGGLE_KEY"] == "kaggle-key"

def test_interactive_flow_rejects_empty_topic() -> None:
    """Empty topics are re-prompted, never silently defaulted to 'finance'."""
    from scripts.cli import interactive_flow
    with patch("builtins.input", side_effect=["", "", "covid", "1", "kaggle"]) as mock_in:
        source, topic, _ = interactive_flow()
    assert topic == "covid"
    assert source == "kaggle"
    # 5 inputs: topic, two empty re-prompts, mode, source — the dead Cleaned/Raw prompt is gone
    assert mock_in.call_count == 5

def test_multi_hop_conversion_cleans_temp_on_failure(tmp_path: Path) -> None:
    """Temp intermediates are removed even when a multi-hop conversion fails mid-way."""
    src = tmp_path / "data.parquet"
    pd.DataFrame({"a": [1, 2]}).to_parquet(src)

    with patch.object(FormatAlchemyEngine, "csv_to_excel_direct", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            FormatAlchemyEngine.convert(str(src), "excel")

    assert list(tmp_path.glob("*_temp.csv")) == []

def test_sqlite_column_barrier_fails_fast_and_coded(tmp_path: Path) -> None:
    """Wide tables (>2000 cols) hit SQLite's hard column limit — must fail fast
    with SIZE_LIMIT before chunked ingestion, not a cryptic mid-ingest crash
    (Gemini QA finding, regression-tested UNMOCKED end-to-end)."""
    wide_csv = tmp_path / "wide_raw.csv"
    pd.DataFrame({f"c{i}": [1] for i in range(2001)}).to_csv(wide_csv, index=False)

    engine = FormatAlchemyEngine(str(wide_csv))
    with pytest.raises(DataFetchError) as exc_info:
        engine.csv_to_sqlite()
    assert exc_info.value.code == "SIZE_LIMIT"
    assert "2000" in str(exc_info.value)
    assert "Parquet" in str(exc_info.value)

    # The Excel route must NOT be blocked by the SQLite barrier anymore:
    # csv->excel streams directly, so 2,001 columns export successfully
    out = FormatAlchemyEngine.convert(str(wide_csv), "excel")
    assert Path(out).is_file()
    assert len(pd.read_excel(out, nrows=1).columns) == 2001
    assert not Path(str(wide_csv).replace("_raw.csv", ".db")).exists(), "direct path must not create a SQLite artifact"

# ---------------------------------------------------------------------------
# Tri-model audit remediation — fixes from the DeepSeek/Gemini Phase-1 audits
# ---------------------------------------------------------------------------

def test_excel_row_cap_respected(tmp_path: Path) -> None:
    """sqlite_to_excel must STOP at the row cap — an out-of-spec workbook is never produced
    (DeepSeek audit: warning claimed truncation but the loop streamed everything)."""
    big_csv = tmp_path / "big_raw.csv"
    pd.DataFrame({"v": range(300)}).to_csv(big_csv, index=False)
    engine = FormatAlchemyEngine(str(big_csv))
    engine.csv_to_sqlite()

    with patch("scripts.format_alchemy.MAX_EXCEL_ROWS", 100):
        engine.sqlite_to_excel()

    df_xl = pd.read_excel(engine.excel_filepath)
    assert len(df_xl) == 100, "workbook must contain exactly the capped row count"

def test_csv_to_excel_direct_bypasses_sqlite_barrier(tmp_path: Path) -> None:
    """Wide CSVs (2,001-16,384 cols) must export to Excel directly — the SQLite
    2,000-column choke point must not block them (Gemini audit finding)."""
    wide = tmp_path / "wide_raw.csv"
    ncols = 2500
    pd.DataFrame({f"c{i}": [1, 2] for i in range(ncols)}).to_csv(wide, index=False)

    out = FormatAlchemyEngine.convert(str(wide), "excel")
    assert Path(out).is_file()
    xl = pd.read_excel(out, nrows=2)
    assert len(xl.columns) == ncols
    assert not (tmp_path / "wide.db").exists(), "direct path must not create a SQLite artifact"

def test_multi_hop_excel_canonical_name_no_db_leak(tmp_path: Path) -> None:
    """parquet→excel lands at the canonical <stem>.xlsx with zero temp debris
    (dual-confirmed by both auditors: leaked _temp.db + _temp_export.xlsx name)."""
    src = tmp_path / "dataset_raw.parquet"
    pd.DataFrame({"a": [1, 2, 3]}).to_parquet(src)

    out = FormatAlchemyEngine.convert(str(src), "excel")
    assert Path(out).name == "dataset_raw.xlsx", "canonical output name"
    leftovers = [p.name for p in tmp_path.iterdir() if "_temp" in p.name]
    assert leftovers == [], f"no temp debris expected, found {leftovers}"

def test_convert_corrupt_xlsx_coded_envelope(tmp_path: Path) -> None:
    """A corrupt xlsx source must yield a coded JSON envelope — never a raw
    traceback with empty stdout (DeepSeek reproduced BadZipFile escaping)."""
    import subprocess
    import sys as _sys
    fake = tmp_path / "broken.xlsx"
    fake.write_bytes(b"this is not a zip archive")
    repo_root = Path(__file__).resolve().parent.parent

    result = subprocess.run(
        [_sys.executable, str(repo_root / "scripts" / "cli.py"), "--convert", "csv", str(fake), "--json-output"],
        capture_output=True, text=True, timeout=180, cwd=str(repo_root),
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["status"] == "error"
    assert payload["code"] in ("INTERNAL", "UNSUPPORTED_CONVERSION", "NOT_FOUND")
    assert "Traceback" not in result.stderr, "raw traceback must never surface"

def test_metrics_count_streamed_payload_bytes(tmp_path: Path) -> None:
    """bytes_transferred must include STREAMED downloads (DeepSeek: envelope
    reported 0 for the largest payloads, violating the spend contract)."""
    from scripts import http_utils
    from scripts.fetchers.datagov import DataGovFetcher

    http_utils.reset_metrics()
    fetcher = DataGovFetcher("weather", str(tmp_path), {})
    fetcher.download_url = "https://data.example.com/d.csv"
    payload = b"city,temp\nCairo,30\n"
    resp = MagicMock(status_code=200, headers={"Content-Length": str(len(payload))})
    resp.iter_content.side_effect = lambda *a, **k: iter([payload])

    with patch("scripts.base.request_with_retry", return_value=resp):
        fetcher._download_to_tempfile(fetcher.download_url)

    m = http_utils.get_metrics()
    assert m["bytes"] >= len(payload), f"streamed bytes must count, got {m['bytes']}"

def test_payload_tmp_cleaned_after_run(tmp_path: Path) -> None:
    """The streamed payload temp must not linger next to the dataset after run()
    (DeepSeek: every payload fetch left up to 500 MB of _payload.tmp on disk)."""
    search_response = MagicMock(status_code=200, headers={})
    search_response.json.return_value = {
        "results": [{"dcat": {"title": "T", "distribution": [
            {"format": "CSV", "downloadURL": "https://data.example.com/dataset.csv"}]}}],
        "after": "",
    }
    body = b"city,temp\nCairo,30\n"
    csv_response = MagicMock(status_code=200, headers={})
    csv_response.iter_content.side_effect = lambda *a, **k: iter([body])

    shared = MagicMock(side_effect=lambda url, **kw: (
        csv_response if url.startswith("https://data.example.com") else search_response))
    with patch("scripts.fetchers.datagov.request_with_retry", shared), \
         patch("scripts.base.request_with_retry", shared):
        fetcher = DataGovFetcher("weather", str(tmp_path), {})
        fetcher.auto_approve = True
        fetcher.run()

    leftovers = list(Path(fetcher.outdir).glob("*_payload.tmp"))
    assert leftovers == [], f"payload temp must be cleaned, found {leftovers}"

def test_payload_tmp_cleaned_on_extract_failure(tmp_path: Path) -> None:
    """Cleanup covers the WHOLE post-download span: an extract()/validate crash
    must still unlink the streamed payload (DeepSeek residual finding)."""
    search_response = MagicMock(status_code=200, headers={})
    search_response.json.return_value = {
        "results": [{"dcat": {"title": "T", "distribution": [
            {"format": "CSV", "downloadURL": "https://data.example.com/dataset.csv"}]}}],
        "after": "",
    }
    body = b"city,temp\nCairo,30\n"
    csv_response = MagicMock(status_code=200, headers={})
    csv_response.iter_content.side_effect = lambda *a, **k: iter([body])

    shared = MagicMock(side_effect=lambda url, **kw: (
        csv_response if url.startswith("https://data.example.com") else search_response))
    with patch("scripts.fetchers.datagov.request_with_retry", shared), \
         patch("scripts.base.request_with_retry", shared):
        fetcher = DataGovFetcher("weather", str(tmp_path), {})
        fetcher.auto_approve = True

        def explode():
            fetcher._download_to_tempfile(fetcher.download_url)  # payload lands on disk
            raise RuntimeError("parse exploded mid-flight")

        with patch.object(fetcher, "extract", side_effect=explode):
            with pytest.raises(RuntimeError):
                fetcher.run()

    leftovers = list(Path(fetcher.outdir).glob("*_payload.tmp"))
    assert leftovers == [], f"payload temp must be cleaned even on extract failure, found {leftovers}"

def test_yahoo_generic_error_not_mislabeled_as_rate_limit(tmp_path: Path) -> None:
    """Arbitrary yfinance failures must NOT carry rate-limit wording (which the
    code mapper would flip into a false RATE_LIMITED envelope — Gemini finding)."""
    from scripts.fetchers.yahoo import YahooFinanceFetcher

    class FakeTicker:
        def __init__(self, q): pass
        def history(self, period):
            raise ValueError("symbol string may be badly formatted")

    fake_yf = MagicMock()
    fake_yf.Ticker = FakeTicker
    fetcher = YahooFinanceFetcher("AAPL", str(tmp_path), {})
    with patch.dict("sys.modules", {"yfinance": fake_yf}):
        with pytest.raises(ValueError) as exc_info:
            fetcher.scout()

    msg = str(exc_info.value).lower()
    assert "rate limit" not in msg, "generic failures must not masquerade as rate limits"
    assert "badly formatted" in msg

def test_yahoo_rate_limit_still_coded(tmp_path: Path) -> None:
    """Genuine Yahoo throttling must surface as a coded RATE_LIMITED envelope."""
    from scripts.fetchers.yahoo import YahooFinanceFetcher

    class YFRateLimitError(Exception): pass
    class FakeTicker:
        def __init__(self, q): pass
        def history(self, period):
            raise YFRateLimitError("Too Many Requests. Rate limited.")

    fake_yf = MagicMock()
    fake_yf.Ticker = FakeTicker
    fetcher = YahooFinanceFetcher("AAPL", str(tmp_path), {})
    with patch.dict("sys.modules", {"yfinance": fake_yf}):
        with pytest.raises(DataFetchError) as exc_info:
            fetcher.scout()

    assert exc_info.value.code == "RATE_LIMITED"

def test_robots_fetched_via_validated_client(tmp_path: Path) -> None:
    """robots.txt must flow through the SSRF-guarded client with redirects OFF
    (Gemini: urllib auto-follow was an unvalidated internal-request vector)."""
    analyzer = WebAnalyzer("https://example.com/public/data.csv", str(tmp_path))
    robots_resp = MagicMock(status_code=200, headers={})
    robots_resp.text = "User-agent: *\nAllow: /\n"
    main_resp = MagicMock(status_code=200, headers={"Content-Type": "text/csv", "Content-Length": "10"})
    main_resp.text = "a,b\n1,2\n"

    calls = []
    def fake_request(url, **kwargs):
        calls.append((url, kwargs))
        return robots_resp if url.endswith("/robots.txt") else main_resp

    with patch("scripts.web_analyzer.socket.getaddrinfo",
               return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
        with patch("scripts.web_analyzer.request_with_retry", side_effect=fake_request):
            report = analyzer.analyze()

    robots_call = next(c for c in calls if c[0].endswith("/robots.txt"))
    assert robots_call[1].get("allow_redirects") is False, "robots fetch must not auto-follow redirects"
    assert robots_call[1].get("timeout") == 10, "robots fetch must be timeout-bounded"
    assert report.difficulty == "easy"

def test_json_output_stdout_is_pure(tmp_path: Path, capsys) -> None:
    """Under --json-output, stdout carries ONLY the JSON envelope; progress goes to stderr."""
    from scripts.cli import main

    mock_fetcher = MagicMock()

    def noisy_run():
        print("[Scout] noisy progress line")
        return str(tmp_path / "weather_raw.csv")

    mock_fetcher.run.side_effect = noisy_run
    mock_fetcher.rows_count = 5
    mock_fetcher.cols_count = 2

    argv = [
        "cli", "--source", "datagov", "--query", "weather",
        "--outdir", str(tmp_path), "--non-interactive", "--json-output",
    ]
    with patch('sys.argv', argv), \
         patch('scripts.cli.setup_wizard', return_value={}), \
         patch('scripts.cli.get_fetcher', return_value=mock_fetcher):
        main()

    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip())  # the WHOLE stdout is one JSON document
    assert payload["status"] == "success"
    assert payload["rows"] == 5
    assert "[Scout]" not in captured.out
    assert "[Scout]" in captured.err

# ---------------------------------------------------------------------------
# Tier 1 — Trust Contract: atomic saves, manifest, completeness, lazy registry
# ---------------------------------------------------------------------------

def test_atomic_save_retains_prev_and_writes_manifest(tmp_path: Path) -> None:
    """Re-fetching must keep the prior version as .prev and log provenance to manifest.jsonl."""
    from scripts.fetchers.fred import FREDFetcher

    fetcher = FREDFetcher("GDP", str(tmp_path), {})
    df1 = pd.DataFrame({"date": ["2024-01-01"], "value": [1.0]})
    df2 = pd.DataFrame({"date": ["2024-01-01"], "value": [2.0]})  # restatement

    csv1 = fetcher.save_csv(df1, "gdp_raw.csv")
    csv2 = fetcher.save_csv(df2, "gdp_raw.csv")

    outdir = Path(fetcher.outdir)
    assert (outdir / "gdp_raw.csv").is_file()
    assert (outdir / "gdp_raw.csv.prev").is_file()
    # .prev holds the FIRST version
    assert pd.read_csv(outdir / "gdp_raw.csv.prev")["value"].iloc[0] == 1.0
    assert pd.read_csv(outdir / "gdp_raw.csv")["value"].iloc[0] == 2.0

    manifest = (outdir / "manifest.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(manifest) == 2
    entry1, entry2 = json.loads(manifest[0]), json.loads(manifest[1])
    assert entry1["sha256"] != entry2["sha256"], "restatement must change the hash"
    assert entry2["sha256"] == fetcher.last_sha256
    assert entry1["complete"] is True and entry2["complete"] is True
    assert "fetched" in entry1["ts"] or "T" in entry1["ts"]  # ISO timestamp
    assert Path(csv1) == outdir / "gdp_raw.csv"

def test_atomic_save_never_tears_the_canonical_file(tmp_path: Path) -> None:
    """A crash mid-write must leave the prior good CSV untouched (atomic replace)."""
    from scripts.fetchers.fred import FREDFetcher

    fetcher = FREDFetcher("GDP", str(tmp_path), {})
    good = pd.DataFrame({"date": ["2024-01-01"], "value": [1.0]})
    fetcher.save_csv(good, "gdp_raw.csv")
    outdir = Path(fetcher.outdir)
    good_bytes = (outdir / "gdp_raw.csv").read_bytes()

    # Simulate a crash during serialization of the next fetch
    with patch.object(pd.DataFrame, "to_csv", side_effect=RuntimeError("OOM mid-write")):
        with pytest.raises(RuntimeError):
            fetcher.save_csv(pd.DataFrame({"date": ["2024-04-01"], "value": [9.0]}), "gdp_raw.csv")

    assert (outdir / "gdp_raw.csv").read_bytes() == good_bytes, "prior file must survive a mid-write crash"

def test_degraded_fetch_marks_envelope_incomplete(tmp_path: Path) -> None:
    """mark_degraded must surface as complete:false + warnings in the envelope meta."""
    from scripts.fetchers.coingecko import CoinGeckoFetcher

    fetcher = CoinGeckoFetcher("btc", str(tmp_path), {})
    fetcher.mark_degraded("Full history requires a CoinGecko API key — fetched last 365 days instead of complete history.")

    from scripts.cli import _fetch_envelope_meta
    meta = _fetch_envelope_meta(fetcher)
    assert meta["complete"] is False
    assert len(meta["warnings"]) == 1
    assert "365 days" in meta["warnings"][0]

def test_worldbank_empty_page_is_degraded_not_success(tmp_path: Path) -> None:
    """An empty HTTP-200 page mid-pagination must flag the fetch as incomplete."""
    def obs(country: str, code: str, year: str):
        return {"country": {"value": country}, "countryiso3code": code, "date": year,
                "value": 1.0, "indicator": {"id": "X", "value": "Test Indicator"}}

    # Page 1 has data; page 2 is empty despite total=3 — the silent-truncation trap
    pages = [
        MagicMock(status_code=200, json=lambda: [{"total": 3}, [obs("Egypt", "EGY", "2024"), obs("France", "FRA", "2024")]]),
        MagicMock(status_code=200, json=lambda: [{"total": 3}, []]),
    ]
    fetcher = WorldBankFetcher("X", str(tmp_path), {})
    with patch("scripts.fetchers.worldbank.request_with_retry", side_effect=pages):
        df = fetcher.extract()

    assert len(df) == 2
    assert fetcher.complete is False, "partial fetch must be flagged degraded"
    assert any("empty page" in w for w in fetcher.completeness_warnings)

def test_lazy_registry_isolates_broken_provider(tmp_path: Path) -> None:
    """One broken provider module must not affect other sources or --list-sources.

    Exercises the REAL import chain in a subprocess with yfinance blocked —
    mocks on importlib would hide the eager-__init__ regression (Alpha's audit).
    """
    import subprocess
    import sys as _sys
    repo_root = Path(__file__).resolve().parent.parent
    probe = (
        "import sys; sys.modules['yfinance'] = None\n"
        "import scripts.factory as f\n"
        "import scripts.errors\n"
        "try:\n"
        "    fetcher = f.get_fetcher('worldbank', 'X', r'%s', {})\n"
        "    print('WORLDBANK_OK')\n"
        "except Exception as e:\n"
        "    print('WORLDBANK_FAIL', type(e).__name__, e)\n"
        "try:\n"
        "    fetcher = f.get_fetcher('yahoo', 'AAPL', r'%s', {})\n"
        "    print('YAHOO_INSTANTIATES')\n"
        "    fetcher.scout()\n"
        "    print('YAHOO_SCOUT_OK')\n"
        "except scripts.errors.DataFetchError as e:\n"
        "    print('YAHOO_CODED', e.code, e.exit_code)\n"
        "except Exception as e:\n"
        "    print('YAHOO_FAIL', type(e).__name__)\n"
        "print('SOURCES', len(f.list_sources()))\n"
    ) % (tmp_path, tmp_path)
    result = subprocess.run(
        [_sys.executable, "-c", probe], capture_output=True, text=True,
        timeout=120, cwd=str(repo_root),
    )
    assert "WORLDBANK_OK" in result.stdout, f"isolation broken: {result.stdout} {result.stderr}"
    assert "YAHOO_INSTANTIATES" in result.stdout, "yahoo should instantiate without yfinance"
    assert "YAHOO_CODED PROVIDER_UNAVAILABLE 3" in result.stdout, f"scout must fail coded: {result.stdout}"
    assert "SOURCES 12" in result.stdout

def test_cli_exit_3_on_provider_load_failure(tmp_path: Path, capsys) -> None:
    """A provider that fails to import exits 3 with a coded JSON envelope."""
    from scripts.cli import main
    import scripts.factory as factory

    mock_fetcher = MagicMock()
    argv = ["cli", "--source", "yahoo", "--query", "AAPL",
            "--outdir", str(tmp_path), "--non-interactive", "--json-output"]
    with patch('sys.argv', argv), \
         patch('scripts.cli.setup_wizard', return_value={}), \
         patch('scripts.cli.get_fetcher', side_effect=DataFetchError(
             "Failed to load fetcher for 'yahoo'", code="PROVIDER_UNAVAILABLE", exit_code=3)):
        with pytest.raises(SystemExit) as exc_info:
            main()

    assert exc_info.value.code == 3
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "error"
    assert payload["code"] == "PROVIDER_UNAVAILABLE"

def test_startup_failure_still_emits_json(tmp_path: Path, capsys) -> None:
    """If the engine cannot even import, main() must emit a JSON error and exit 3."""
    from scripts.cli import main
    import scripts.cli as cli_module

    with patch.object(cli_module, "_startup_error", ImportError("No module named 'pandas'")):
        with pytest.raises(SystemExit) as exc_info:
            main()

    assert exc_info.value.code == 3
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "error"
    assert payload["code"] == "STARTUP_FAILURE"

def test_convert_envelope_reports_the_artifact(tmp_path: Path, capsys) -> None:
    """--convert must report the OUTPUT file in converted_file, with source_file echoed."""
    from scripts.cli import main

    src = tmp_path / "data_raw.csv"
    pd.DataFrame({"a": [1, 2]}).to_csv(src, index=False)

    argv = ["cli", "--convert", "parquet", str(src), "--json-output"]
    with patch('sys.argv', argv):
        with pytest.raises(SystemExit) as exc_info:
            main()
    assert exc_info.value.code == 0

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "success"
    assert payload["converted_file"].endswith(".parquet"), "must point at the created artifact"
    assert payload["source_file"] == str(src)
    assert Path(payload["converted_file"]).is_file()

def test_fallback_error_codes() -> None:
    """Plain ValueErrors map to stable codes for the JSON envelope."""
    from scripts.cli import _fallback_code
    assert _fallback_code(ValueError("Ticker 'XX' not found or delisted.")) == "NOT_FOUND"
    assert _fallback_code(ValueError("Invalid indicator 'ZZ' or no data returned.")) == "NOT_FOUND"
    assert _fallback_code(ValueError("No cryptocurrency found on CoinGecko matching 'zz'.")) == "NOT_FOUND"
    assert _fallback_code(ValueError("Target format 'exe' is not supported.")) == "UNSUPPORTED_CONVERSION"
    assert _fallback_code(ValueError("Unsupported data source 'foo'.")) == "UNSUPPORTED_SOURCE"
    assert _fallback_code(RuntimeError("GitHub rate limit hit.")) == "RATE_LIMITED"
    assert _fallback_code(ImportError("openpyxl missing")) == "DEPENDENCY_MISSING"
    assert _fallback_code(ValueError("Blocked target '127.0.0.1'")) == "BLOCKED_URL"
    assert _fallback_code(ValueError("something odd")) == "PROVIDER_ERROR"

def test_retry_after_capped_at_60s() -> None:
    """A hostile Retry-After must never hang the CLI: sleeps are capped at 60s."""
    from scripts import http_utils

    mock_session = MagicMock()
    mock_session.get.return_value = MagicMock(status_code=429, headers={"Retry-After": "86400"})

    with patch.object(http_utils, 'session', mock_session), \
         patch.object(http_utils.time, 'sleep') as mock_sleep:
        with pytest.raises(DataFetchError):
            http_utils.request_with_retry("https://hostile.test", max_retries=3)

    assert mock_sleep.call_count == 2
    for call in mock_sleep.call_args_list:
        assert call.args[0] <= 60.0, f"sleep {call.args[0]}s exceeds the 60s cap"

def test_corrupt_config_surfaces_instead_of_silence(tmp_path: Path, capsys) -> None:
    """A corrupt config.json must warn (stderr) rather than masquerade as 'no keys'."""
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text("{not valid json", encoding="utf-8")

    from scripts.config import setup_wizard
    with patch("scripts.config.get_config_paths", return_value=(str(cfg_dir), str(cfg_dir / "config.json"))):
        config = setup_wizard(non_interactive=True)

    assert config == {}
    err = capsys.readouterr().err
    assert "corrupt" in err
    assert str(cfg_dir / "config.json") in err

# ---------------------------------------------------------------------------
# Phase A — contract restoration (remediation of the four-agent audit)
# ---------------------------------------------------------------------------

def test_json_mode_demands_non_interactive(tmp_path: Path, capsys) -> None:
    "--json-output without --yes/--non-interactive must fail fast with ONE coded envelope — never reach a prompt."
    from scripts.cli import main

    argv = ["cli", "--source", "worldbank", "--query", "X",
            "--outdir", str(tmp_path), "--json-output"]
    with patch('sys.argv', argv), \
         patch('scripts.cli.setup_wizard', return_value={}), \
         patch('scripts.cli.get_fetcher', return_value=MagicMock()) as mock_get, \
         patch('builtins.input', side_effect=AssertionError("input() must be unreachable in JSON mode")):
        with pytest.raises(SystemExit) as exc_info:
            main()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert out.strip().count("{") == 1, "exactly one JSON envelope"
    payload = json.loads(out.strip())
    assert payload["code"] == "INTERACTIVE_REQUIRED"
    mock_get.assert_not_called()

def test_rate_limit_exhaustion_raises_coded_error() -> None:
    """429 exhaustion must surface as RATE_LIMITED, not INTERNAL."""
    from scripts import http_utils

    mock_session = MagicMock()
    mock_session.get.return_value = MagicMock(status_code=429, headers={})

    with patch.object(http_utils, 'session', mock_session), \
         patch.object(http_utils.time, 'sleep'):
        with pytest.raises(DataFetchError) as exc_info:
            http_utils.request_with_retry("https://rate.test", max_retries=2)

    assert exc_info.value.code == "RATE_LIMITED"
    assert exc_info.value.exit_code == 1

def test_network_exhaustion_raises_coded_error() -> None:
    """Connection-error exhaustion must surface as NETWORK."""
    from scripts import http_utils

    mock_session = MagicMock()
    mock_session.get.side_effect = requests_lib.exceptions.ConnectionError("refused")

    with patch.object(http_utils, 'session', mock_session), \
         patch.object(http_utils.time, 'sleep'):
        with pytest.raises(DataFetchError) as exc_info:
            http_utils.request_with_retry("https://down.test", max_retries=2)

    assert exc_info.value.code == "NETWORK"

def test_retry_after_hostile_values_never_crash() -> None:
    """NaN/negative Retry-After fall back to exponential backoff instead of crashing sleep()."""
    from scripts.http_utils import _parse_retry_after
    assert _parse_retry_after("nan", 4.0) == 4.0
    assert _parse_retry_after("-5", 4.0) == 4.0
    assert _parse_retry_after("inf", 4.0) == 4.0
    assert _parse_retry_after("0", 4.0) == 4.0
    assert _parse_retry_after("30", 4.0) == 30.0

def test_retry_after_naive_http_date_is_utc() -> None:
    """A tz-less HTTP-date must be interpreted as UTC, not local time."""
    from scripts.http_utils import _parse_retry_after
    import email.utils
    # A date 10 minutes in the FUTURE (UTC) should yield a delay near 600s, not the fallback
    future = email.utils.formatdate(time.time() + 600, usegmt=False)  # naive-format string
    delay = _parse_retry_after(future, 9.0)
    assert 500 < delay <= 605

def test_user_abort_is_coded_not_provider_error(tmp_path: Path) -> None:
    """Declining pre-flight must produce ABORTED, distinct from upstream breakage."""
    fetcher = WorldBankFetcher("X", str(tmp_path), {})
    with patch("builtins.input", return_value="n"):
        with pytest.raises(DataFetchError) as exc_info:
            fetcher.pre_flight_authorization({"url": "https://x", "size_info": "y"})
    assert exc_info.value.code == "ABORTED"

def test_arg_missing_code(tmp_path: Path, capsys) -> None:
    """Missing query in non-interactive mode maps to ARG_MISSING."""
    from scripts.cli import main
    argv = ["cli", "--source", "worldbank", "--outdir", str(tmp_path),
            "--non-interactive", "--json-output"]
    with patch('sys.argv', argv), \
         patch('scripts.cli.setup_wizard', return_value={}):
        with pytest.raises(SystemExit) as exc_info:
            main()
    assert exc_info.value.code == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["code"] == "ARG_MISSING"

def test_sliced_fetch_records_truncation_in_manifest(tmp_path: Path) -> None:
    """A user-sliced fetch keeps complete:true but records the truncation explicitly."""
    from scripts.fetchers.fred import FREDFetcher

    fetcher = FREDFetcher("POP", str(tmp_path), {})
    fetcher.auto_approve = True
    fetcher.row_limit = 5
    big = pd.DataFrame({"v": range(100)})
    with patch.object(FREDFetcher, "extract", return_value=big), \
         patch.object(FREDFetcher, "scout", return_value={"url": "https://x", "size_info": "100"}):
        fetcher.run()

    manifest = json.loads((Path(fetcher.outdir) / "manifest.jsonl").read_text(encoding="utf-8").strip())
    assert manifest["complete"] is True
    assert manifest["truncation"] == {"rows": 5, "population": 100, "by": "user"}
    assert manifest["rows"] == 5

def test_unsliced_fetch_records_null_truncation(tmp_path: Path) -> None:
    """Full fetches record truncation:null — absence of truncation IS the contract."""
    from scripts.fetchers.fred import FREDFetcher

    fetcher = FREDFetcher("POP", str(tmp_path), {})
    fetcher.auto_approve = True
    small = pd.DataFrame({"v": [1, 2, 3]})
    with patch.object(FREDFetcher, "extract", return_value=small), \
         patch.object(FREDFetcher, "scout", return_value={"url": "https://x", "size_info": "3"}):
        fetcher.run()

    manifest = json.loads((Path(fetcher.outdir) / "manifest.jsonl").read_text(encoding="utf-8").strip())
    assert manifest["truncation"] is None
    assert manifest["warnings"] == []

def test_provision_single_source_only(tmp_path: Path) -> None:
    """Discovery/fetch provisioning must not create all 12 platform dirs."""
    from scripts.base import provision_data_directory
    provision_data_directory(str(tmp_path), source_key="worldbank")
    assert (tmp_path / "worldbank").is_dir()
    assert not (tmp_path / "kaggle").exists()
    assert not (tmp_path / "sec").exists()

def test_coingecko_fallback_records_real_url(tmp_path: Path) -> None:
    """The 365-day fallback must update dataset_url — the manifest URL must describe the bytes."""
    from scripts.fetchers.coingecko import CoinGeckoFetcher

    fetcher = CoinGeckoFetcher("sol", str(tmp_path), {})
    fetcher.coin_id = "solana"
    fetcher.market_url = "https://api.coingecko.com/api/v3/coins/solana/market_chart?vs_currency=usd&days=max"
    fetcher.dataset_url = "https://api.coingecko.com/api/v3/coins/solana/market_chart?vs_currency=usd&days=max"

    max_resp = MagicMock(status_code=401)
    ok_resp = MagicMock(status_code=200)
    ok_resp.json.return_value = {
        "prices": [[1700000000000, 100.0]],
        "market_caps": [[1700000000000, 1e9]],
        "total_volumes": [[1700000000000, 1e6]],
    }
    with patch("scripts.fetchers.coingecko.request_with_retry", side_effect=[max_resp, ok_resp]):
        df = fetcher.extract()

    assert fetcher.dataset_url.endswith("days=365")
    assert fetcher.complete is False
    assert len(df) == 1

# ---------------------------------------------------------------------------
# Phase B — cost honesty & resilience (remediation roadmap)
# ---------------------------------------------------------------------------

def test_worldbank_row_limit_pushdown() -> None:
    """--rows N fetches one page of N records instead of the entire indicator."""
    def obs(i):
        return {"country": {"value": f"C{i}"}, "countryiso3code": f"C{i}", "date": "2024",
                "value": float(i), "indicator": {"id": "X", "value": "Ind"}}

    pages = [MagicMock(status_code=200, json=lambda: [{"total": 17490, "page": 1}, [obs(i) for i in range(5)]])]
    fetcher = WorldBankFetcher("X", "/tmp/out", {})
    fetcher.row_limit = 5
    with patch("scripts.fetchers.worldbank.request_with_retry", side_effect=pages) as mock_req:
        df = fetcher.extract()

    assert len(df) == 5
    assert mock_req.call_count == 1, "row-limited fetch must stop after one page"
    assert "per_page=5" in mock_req.call_args_list[0].args[0]

def test_worldbank_repeated_page_stops_with_degraded() -> None:
    """A proxy repeating a page must not duplicate rows — stop degraded instead."""
    def obs(i):
        return {"country": {"value": f"C{i}"}, "countryiso3code": f"C{i}", "date": "2024",
                "value": float(i), "indicator": {"id": "X", "value": "Ind"}}

    pages = [
        MagicMock(status_code=200, json=lambda: [{"total": 4, "page": 1}, [obs(1), obs(2)]]),
        MagicMock(status_code=200, json=lambda: [{"total": 4, "page": 1}, [obs(1), obs(2)]]),
    ]
    fetcher = WorldBankFetcher("X", "/tmp/out", {})
    with patch("scripts.fetchers.worldbank.request_with_retry", side_effect=pages):
        df = fetcher.extract()

    assert len(df) == 2, "repeated page must not duplicate rows"
    assert fetcher.complete is False
    assert any("page 1" in w for w in fetcher.completeness_warnings)

def test_worldbank_max_pages_cap() -> None:
    """The pagination loop must stop at MAX_PAGES instead of running forever."""
    def obs(i):
        return {"country": {"value": f"C{i}"}, "countryiso3code": f"C{i}", "date": "2024",
                "value": float(i), "indicator": {"id": "X", "value": "Ind"}}

    # Endless non-empty pages with total=0 (falsy total must not loop forever);
    # each page echoes its own number so the page-echo guard stays silent
    counter = {"n": 0}
    def make_page():
        counter["n"] += 1
        n = counter["n"]
        return MagicMock(status_code=200, json=lambda n=n: [{"total": 0, "page": n}, [obs(1), obs(2)]])

    fetcher = WorldBankFetcher("X", "/tmp/out", {})
    fetcher.MAX_PAGES = 5  # shrink the cap so the test stays fast
    with patch("scripts.fetchers.worldbank.request_with_retry", side_effect=[make_page() for _ in range(10)]):
        df = fetcher.extract()

    assert len(df) == 10  # 5 pages x 2 rows
    assert fetcher.complete is False
    assert any("safety cap" in w for w in fetcher.completeness_warnings)

def test_worldbank_midloop_failure_keeps_partial_as_degraded() -> None:
    """A mid-loop HTTP error keeps fetched pages as degraded instead of discarding them."""
    def obs(i):
        return {"country": {"value": f"C{i}"}, "countryiso3code": f"C{i}", "date": "2024",
                "value": float(i), "indicator": {"id": "X", "value": "Ind"}}

    pages = [
        MagicMock(status_code=200, json=lambda: [{"total": 10, "page": 1}, [obs(i) for i in range(5)]]),
        MagicMock(status_code=503),
    ]
    fetcher = WorldBankFetcher("X", "/tmp/out", {})
    with patch("scripts.fetchers.worldbank.request_with_retry", side_effect=pages):
        df = fetcher.extract()

    assert len(df) == 5
    assert fetcher.complete is False
    assert any("503" in w for w in fetcher.completeness_warnings)

def test_output_locked_surfaces_coded_error(tmp_path: Path) -> None:
    """A Windows share-lock on the canonical file must yield OUTPUT_LOCKED naming the temp."""
    from scripts.fetchers.fred import FREDFetcher
    import scripts.base as base_mod

    fetcher = FREDFetcher("GDP", str(tmp_path), {})
    fetcher.save_csv(pd.DataFrame({"date": ["2024-01-01"], "value": [1.0]}), "gdp_raw.csv")

    import os as real_os_module
    real_replace = real_os_module.replace  # captured BEFORE patching (same module object)
    def lock_canonical_only(src, dst):
        if str(dst).endswith("gdp_raw.csv"):  # only the promote target is locked
            raise PermissionError(32, "being used by another process")
        return real_replace(src, dst)

    with patch.object(base_mod.os, "replace", side_effect=lock_canonical_only), \
         patch.object(base_mod.time, "sleep", lambda s: None):
        with pytest.raises(DataFetchError) as exc_info:
            fetcher.save_csv(pd.DataFrame({"date": ["2024-04-01"], "value": [2.0]}), "gdp_raw.csv")

    assert exc_info.value.code == "OUTPUT_LOCKED"
    assert ".tmp" in str(exc_info.value)

def test_self_heal_promotes_stranded_tmp(tmp_path: Path) -> None:
    """A crash between commit steps (canonical missing, tmp present) is reconciled on the next save."""
    from scripts.fetchers.fred import FREDFetcher

    fetcher = FREDFetcher("GDP", str(tmp_path), {})
    outdir = Path(fetcher.outdir)
    stranded = outdir / "gdp_raw.csv.tmp"
    pd.DataFrame({"date": ["2024-04-01"], "value": [9.0]}).to_csv(stranded, index=False)

    fetcher.save_csv(pd.DataFrame({"date": ["2024-07-01"], "value": [3.0]}), "gdp_raw.csv")

    canonical = outdir / "gdp_raw.csv"
    assert canonical.is_file()
    assert pd.read_csv(outdir / "gdp_raw.csv.prev")["value"].iloc[0] == 9.0
    assert pd.read_csv(canonical)["value"].iloc[0] == 3.0

def test_oversized_payload_raises_size_limit(tmp_path: Path) -> None:
    """A payload beyond the download cap fails with SIZE_LIMIT before consuming memory."""
    from scripts.fetchers.datagov import DataGovFetcher

    fetcher = DataGovFetcher("weather", str(tmp_path), {})
    fetcher.download_url = "https://data.example.com/big.csv"

    giant = MagicMock(status_code=200, headers={"Content-Length": str(600 * 1024 * 1024)})
    with patch("scripts.base.request_with_retry", return_value=giant):
        with pytest.raises(DataFetchError) as exc_info:
            fetcher._download_to_tempfile(fetcher.download_url)
    assert exc_info.value.code == "SIZE_LIMIT"

def test_coingecko_pacing_enforced() -> None:
    """CoinGecko requests are spaced >= 6s apart even in agent mode."""
    from scripts import http_utils

    sleeps = []
    http_utils._last_request_ts.clear()
    ok = MagicMock(status_code=200, headers={})
    with patch.object(http_utils, "session") as mock_session, \
         patch.object(http_utils.time, "sleep", sleeps.append):
        mock_session.get.return_value = ok
        http_utils.request_with_retry("https://api.coingecko.com/api/v3/ping")
        http_utils.request_with_retry("https://api.coingecko.com/api/v3/ping")

    assert any(0 < s <= 6.0 for s in sleeps), f"expected a pacing sleep, got {sleeps}"

def test_metrics_reset_and_count() -> None:
    """The metering counters count requests and reset cleanly."""
    from scripts import http_utils

    http_utils.reset_metrics()
    ok = MagicMock(status_code=200, headers={})
    ok.content = b"0123456789"
    with patch.object(http_utils, "session") as mock_session:
        mock_session.get.return_value = ok
        http_utils.request_with_retry("https://metrics.test/a")
        http_utils.request_with_retry("https://metrics.test/b")

    m = http_utils.get_metrics()
    assert m["requests"] == 2
    assert m["bytes"] > 0
    http_utils.reset_metrics()
    assert http_utils.get_metrics() == {"requests": 0, "retries": 0, "bytes": 0}

def test_rows_flag_flows_to_fetcher_and_envelope(tmp_path: Path, capsys) -> None:
    """--rows N reaches the fetcher before run() and the truncation reaches the envelope."""
    from scripts.cli import main

    mock_fetcher = MagicMock()
    mock_fetcher.run.return_value = str(tmp_path / "gdp_raw.csv")
    mock_fetcher.rows_count = 5
    mock_fetcher.cols_count = 2
    mock_fetcher.complete = True
    mock_fetcher.completeness_warnings = []
    mock_fetcher.truncation = {"rows": 5, "population": 100, "by": "user"}
    mock_fetcher.last_sha256 = "abc"
    mock_fetcher.last_bytes = 10
    mock_fetcher.last_description_path = ""
    mock_fetcher.dataset_url = "https://x"
    mock_fetcher.resolved_title = "T"

    argv = ["cli", "--source", "fred", "--query", "GDP", "--outdir", str(tmp_path),
            "--rows", "5", "--yes", "--non-interactive", "--json-output"]
    with patch('sys.argv', argv), \
         patch('scripts.cli.setup_wizard', return_value={}), \
         patch('scripts.cli.get_fetcher', return_value=mock_fetcher):
        main()  # success path returns without exiting

    assert mock_fetcher.row_limit == 5
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["truncation"] == {"rows": 5, "population": 100, "by": "user"}
    assert "provider_requests" in payload
