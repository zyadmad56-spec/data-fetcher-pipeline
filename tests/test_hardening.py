import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests
import pandas as pd

from scripts.config import setup_wizard
from scripts.errors import DataFetchError
from scripts.format_alchemy import FormatAlchemyEngine
from scripts.fetchers.generic import GenericFetcher
from scripts.fetchers.fred import FREDFetcher
from scripts.fetchers.sec import SECFetcher
from scripts.http_utils import request_with_retry
from scripts.web_analyzer import AnalysisReport, WebAnalyzer


def test_specific_robots_denial_overrides_wildcard_allow(tmp_path: Path) -> None:
    analyzer = WebAnalyzer("https://example.com/private.csv", str(tmp_path))
    response = MagicMock()
    response.status_code = 200
    response.text = "User-agent: data-fetcher-pipeline\nDisallow: /\nUser-agent: *\nAllow: /"

    with patch("scripts.web_analyzer.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
        with patch("scripts.web_analyzer.request_with_retry", return_value=response) as request:
            report = analyzer.analyze()

    assert report.difficulty == "blocked"
    assert request.call_count == 1


def test_unavailable_robots_policy_blocks_custom_fetch(tmp_path: Path) -> None:
    analyzer = WebAnalyzer("https://example.com/data.csv", str(tmp_path))
    with patch("scripts.web_analyzer.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
        with patch("scripts.web_analyzer.request_with_retry", side_effect=DataFetchError("offline", code="NETWORK")) as request:
            report = analyzer.analyze()

    assert report.difficulty == "blocked"
    assert request.call_count == 1


def test_custom_analysis_streams_direct_file_without_reading_body(tmp_path: Path) -> None:
    analyzer = WebAnalyzer("https://example.com/large.csv", str(tmp_path))
    response = MagicMock(status_code=200, headers={"Content-Type": "text/csv", "Content-Length": "900000000"})
    with patch("scripts.web_analyzer.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
        with patch.object(analyzer, "_check_robots_txt", return_value=True):
            with patch("scripts.web_analyzer.request_with_retry", return_value=response) as request:
                report = analyzer.analyze()

    assert report.difficulty == "easy"
    assert request.call_args.kwargs["stream"] is True
    response.iter_content.assert_not_called()
    response.close.assert_called_once()


def test_sqlite_roundtrip_does_not_replace_existing_csv(tmp_path: Path) -> None:
    csv_path = tmp_path / "codes.csv"
    original = b"code,amount\n00123,1.00\n"
    csv_path.write_bytes(original)
    database_path = FormatAlchemyEngine.convert(str(csv_path), "sqlite")

    with pytest.raises(DataFetchError) as error:
        FormatAlchemyEngine.convert(database_path, "csv")

    assert error.value.code == "INVALID_OUTPUT"
    assert csv_path.read_bytes() == original


def test_raw_csv_conversion_protects_existing_database(tmp_path: Path) -> None:
    csv_path = tmp_path / "sales_raw.csv"
    csv_path.write_text("id\n1\n", encoding="utf-8")
    database_path = tmp_path / "sales.db"
    database_path.write_bytes(b"existing database")

    with pytest.raises(DataFetchError) as error:
        FormatAlchemyEngine.convert(str(csv_path), "sqlite")

    assert error.value.code == "INVALID_OUTPUT"
    assert database_path.read_bytes() == b"existing database"


def test_json_conversion_preserves_float_precision(tmp_path: Path) -> None:
    csv_path = tmp_path / "precise.csv"
    csv_path.write_text("identifier,price\n1.2345678901234568e+18,0.12345678901234566\n", encoding="utf-8")

    json_path = Path(FormatAlchemyEngine.convert(str(csv_path), "json"))
    original = pd.read_csv(csv_path)
    converted = pd.read_json(json_path, convert_dates=False, precise_float=True)

    assert converted["identifier"].iloc[0] == original["identifier"].iloc[0]
    assert converted["price"].iloc[0] == original["price"].iloc[0]


def test_config_permission_failure_is_reported_without_crashing(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("scripts.config.get_config_paths", return_value=("blocked", "blocked/config.json")):
        with patch.object(Path, "exists", side_effect=PermissionError("denied")):
            assert setup_wizard(non_interactive=True) == {}

    assert "WARNING" in capsys.readouterr().err


def test_missing_data_dependency_still_returns_json(tmp_path: Path) -> None:
    cli_path = Path(__file__).resolve().parents[1] / "scripts" / "cli.py"
    environment = os.environ.copy()
    environment["USERPROFILE"] = str(tmp_path)
    command = [
        sys.executable, "-S", str(cli_path), "--source", "worldbank",
        "--query", "NY.GDP.MKTP.CD", "--yes", "--non-interactive",
        "--json-output", "--outdir", str(tmp_path / "output"),
    ]

    run = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=30)

    assert run.returncode == 1
    assert json.loads(run.stdout)["code"] == "DEPENDENCY_MISSING"
    assert "Traceback" not in run.stderr


def test_network_error_never_exposes_url_query_secret() -> None:
    url = "https://api.example.com/series?api_key=secret-canary&series_id=GDP"

    with patch("scripts.http_utils.session.get", side_effect=requests.ConnectionError("offline")):
        with pytest.raises(DataFetchError) as error:
            request_with_retry(url, max_retries=1)

    assert error.value.code == "NETWORK"
    assert "secret-canary" not in str(error.value)
    assert "api_key" not in str(error.value)


def test_direct_payload_preserves_original_bytes(tmp_path: Path) -> None:
    fetcher = GenericFetcher("codes", str(tmp_path), {}, source="generic")
    original = b"code,amount\n00123,1.00\n"
    payload_path = Path(fetcher.outdir) / "download.tmp"
    payload_path.write_bytes(original)
    fetcher._temp_payload = payload_path

    fetcher.save_csv(pd.DataFrame({"code": [123], "amount": [1.0]}), "codes_raw.csv")

    retained = Path(fetcher.last_original_path)
    assert retained.read_bytes() == original
    assert retained != Path(fetcher.outdir) / "codes_raw.csv"
    assert fetcher.last_original_sha256 == fetcher._sha256_file(retained)


def test_generated_scraper_rejects_private_redirect(tmp_path: Path) -> None:
    analyzer = WebAnalyzer("https://example.com/data.csv", str(tmp_path))
    script = analyzer.generate_script(AnalysisReport(url=analyzer.url, difficulty="easy"))
    fetch = runpy.run_path(script, run_name="generated_module")["_fetch_with_retry"]
    fetch.__globals__["_robots_allowed"] = lambda url: True
    response = MagicMock()
    response.status_code = 302
    response.headers = {"Location": "http://127.0.0.1/private"}

    with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
        with patch("requests.get", return_value=response) as request:
            with pytest.raises(ValueError):
                fetch(analyzer.url, {}, retries=1)

    assert request.call_count == 1
    assert request.call_args.kwargs["allow_redirects"] is False


def test_sdk_fetch_does_not_claim_zero_network_requests(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from scripts.cli import main

    fetcher = MagicMock()
    fetcher.run.return_value = str(tmp_path / "data.csv")
    fetcher.rows_count = 2
    fetcher.cols_count = 1
    fetcher.complete = True
    fetcher.completeness_warnings = []
    fetcher.truncation = None
    with patch("sys.argv", ["cli", "--source", "openml", "--query", "61", "--outdir", str(tmp_path), "--yes", "--non-interactive", "--json-output"]):
        with patch("scripts.cli.setup_wizard", return_value={}):
            with patch("scripts.cli.get_fetcher", return_value=fetcher):
                main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["provider_requests"] is None
    assert payload["retries"] is None
    assert payload["bytes_transferred"] is None
    assert "SDK" in payload["metrics_scope"]


def test_fred_uses_public_csv_when_no_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    fetcher = FREDFetcher("CPIAUCSL", str(tmp_path), {})
    metadata = fetcher.scout()
    with patch.object(fetcher, "_consume_payload_csv", return_value=pd.DataFrame({
        "observation_date": ["2020-01-01"], "CPIAUCSL": ["258.906"],
    })):
        data = fetcher.extract()

    assert "fredgraph.csv" in metadata["url"]
    assert data.columns.tolist() == ["date", "value"]
    assert data["value"].iloc[0] == 258.906


def test_sec_rejects_non_contact_token_before_network(tmp_path: Path) -> None:
    fetcher = SECFetcher("AAPL", str(tmp_path), {"SEC_API_KEY": "not-a-contact"})
    with patch("scripts.fetchers.sec.request_with_retry") as request:
        with pytest.raises(DataFetchError) as error:
            fetcher.scout()

    assert error.value.code == "AUTH_INVALID"
    request.assert_not_called()
