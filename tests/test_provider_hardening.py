"""Built-in provider hardening tests: SSRF guards on scraped/DCAT URLs,
public-only payload transport, gh resolution and token hygiene, Kaggle
archive validation and credential restore, credential-safe error messages,
and the atomic config write."""
import json
import os
import stat
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from scripts.adapters.payload import PayloadReader
from scripts.config import _write_config
from scripts.core.models import FetchState, PayloadSpec
from scripts.factory import get_fetcher, list_sources
from scripts.fetchers.airbnb import AirbnbFetcher
from scripts.fetchers.datagov import DataGovFetcher
from scripts.fetchers.fred import FREDFetcher
from scripts.fetchers.github_data import GitHubDataFetcher, _gh_executable, _resolve_github_token
from scripts.fetchers.kaggle_fetcher import ARCHIVE_UNCOMPRESSED_LIMIT, KaggleFetcher
from scripts.fetchers.worldbank import WorldBankFetcher
from scripts.fetchers.yahoo import YahooFinanceFetcher

PUBLIC_DNS = [(2, 1, 6, "", ("93.184.216.34", 0))]
TEST_KEY = "a" * 32


# ---------------------------------------------------------------------------
# config.py — atomic key-file write
# ---------------------------------------------------------------------------

def test_config_write_is_atomic_and_leaves_no_temp_files(tmp_path):
    config_file = str(tmp_path / "config.json")
    with patch("scripts.config.os.chmod") as chmod:
        _write_config({"KAGGLE_KEY": "k"}, config_file)
    chmod.assert_called_once_with(config_file, 0o600)
    assert json.loads(Path(config_file).read_text(encoding="utf-8")) == {"KAGGLE_KEY": "k"}
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]


def test_config_write_failure_keeps_previous_config_and_cleans_temp(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text('{"previous": true}', encoding="utf-8")
    with patch("scripts.config.os.replace", side_effect=OSError("target locked")):
        with pytest.raises(OSError):
            _write_config({"KAGGLE_KEY": "k"}, str(config_file))
    assert json.loads(config_file.read_text(encoding="utf-8")) == {"previous": True}
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]


# ---------------------------------------------------------------------------
# airbnb — https index, host allowlist, public-URL validation
# ---------------------------------------------------------------------------

def test_airbnb_scout_uses_https_index_and_allows_only_insideairbnb(tmp_path):
    fetcher = AirbnbFetcher("lisbon", str(tmp_path), {})
    html = (
        '<a href="https://evil.example.com/lisbon/listings.csv.gz">offsite</a>'
        '<a href="http://insideairbnb.com/lisbon/listings.csv.gz">plaintext</a>'
        '<a href="lisbon/listings.csv.gz">relative</a>'
    )
    response = MagicMock(status_code=200, text=html)
    with patch("scripts.fetchers.airbnb.request_with_retry", return_value=response) as request:
        with patch("scripts.fetchers.airbnb.validate_public_url") as validate:
            fetcher.scout()

    assert request.call_args.args[0] == "https://insideairbnb.com/get-the-data/"
    # The relative href resolves onto the https index host and is accepted.
    assert fetcher.download_url == "https://insideairbnb.com/get-the-data/lisbon/listings.csv.gz"
    validate.assert_called_once_with("https://insideairbnb.com/get-the-data/lisbon/listings.csv.gz")
    joined = " ".join(fetcher.completeness_warnings)
    assert "https://evil.example.com/lisbon/listings.csv.gz" in joined  # offsite host
    assert "http://insideairbnb.com/lisbon/listings.csv.gz" in joined   # plaintext scheme
    assert metadata_ok(fetcher)


def metadata_ok(fetcher):
    return isinstance(fetcher, AirbnbFetcher) and fetcher.download_url.startswith("https://")


def test_airbnb_scout_fails_closed_without_an_allowlisted_link(tmp_path):
    fetcher = AirbnbFetcher("lisbon", str(tmp_path), {})
    response = MagicMock(status_code=200,
                         text='<a href="https://mirror.example.net/lisbon/listings.csv.gz">mirror</a>')
    with patch("scripts.fetchers.airbnb.request_with_retry", return_value=response):
        with pytest.raises(ValueError, match="safe insideairbnb.com"):
            fetcher.scout()
    assert fetcher.download_url == ""


# ---------------------------------------------------------------------------
# datagov — DCAT downloadURL/accessURL public-URL guard
# ---------------------------------------------------------------------------

def test_datagov_blocks_private_dcat_targets_and_keeps_scanning(tmp_path):
    fetcher = DataGovFetcher("weather", str(tmp_path), {})
    payload = {"results": [{
        "dcat": {"title": "Sensitive Dataset", "distribution": [
            {"format": "CSV", "downloadURL": "http://169.254.169.254/latest/creds.csv"},
            {"format": "CSV", "downloadURL": "http://10.0.0.5/internal.csv"},
        ]}
    }]}
    fetcher._resolve_csv_url(payload)
    assert fetcher.download_url == ""
    assert any("Blocked DCAT distribution" in warning for warning in fetcher.completeness_warnings)


def test_datagov_accepts_public_csv_distribution(tmp_path):
    fetcher = DataGovFetcher("weather", str(tmp_path), {})
    payload = {"results": [{
        "dcat": {"title": "Public Dataset", "distribution": [
            {"format": "CSV", "downloadURL": "https://catalog.example.gov/dataset.csv"}
        ]}
    }]}
    with patch("socket.getaddrinfo", return_value=PUBLIC_DNS):
        fetcher._resolve_csv_url(payload)
    assert fetcher.download_url == "https://catalog.example.gov/dataset.csv"


def test_datagov_selects_first_valid_after_blocked_one(tmp_path):
    fetcher = DataGovFetcher("weather", str(tmp_path), {})
    payload = {"results": [{
        "dcat": {"title": "Mixed", "distribution": [
            {"format": "CSV", "downloadURL": "http://192.168.1.10/private.csv"},
            {"format": "CSV", "accessURL": "https://catalog.example.gov/ok.csv"},
        ]}
    }]}
    with patch("socket.getaddrinfo", return_value=PUBLIC_DNS):
        fetcher._resolve_csv_url(payload)
    assert fetcher.download_url == "https://catalog.example.gov/ok.csv"
    assert fetcher.dataset_title == "Mixed"


# ---------------------------------------------------------------------------
# payload.py — public-only transport and deadline on streamed downloads
# ---------------------------------------------------------------------------

def _payload_reader(tmp_path, request_mock):
    state = FetchState(query="records", provider_name="TestFetcher", source_key="test",
                       outdir=str(tmp_path))
    spec = PayloadSpec("https://example.com/data.csv", {}, ",", None,
                       256 * 1024, 500 * 1024 * 1024)
    return PayloadReader(state, spec, request_mock)


def test_payload_preview_uses_public_only_transport_with_deadline(tmp_path):
    request = MagicMock()
    response = MagicMock(status_code=200, headers={})
    response.iter_content.side_effect = lambda *a, **k: iter([b"a,b\n1,2\n"])
    request.return_value = response
    reader = _payload_reader(tmp_path, request)

    frame = reader._bounded_csv_preview("https://example.com/data.csv")

    assert frame["a"].tolist() == [1]
    kwargs = request.call_args.kwargs
    assert kwargs["public_only"] is True
    assert kwargs["deadline_sec"] > 0
    assert kwargs["stream"] is True
    response.close.assert_called_once()


def test_payload_download_uses_public_only_transport_with_deadline(tmp_path):
    request = MagicMock()
    response = MagicMock(status_code=200, headers={})
    response.iter_content.side_effect = lambda *a, **k: iter([b"a,b\n1,2\n"])
    request.return_value = response
    reader = _payload_reader(tmp_path, request)

    target = reader._download_to_tempfile("https://example.com/data.csv")

    assert target.read_bytes() == b"a,b\n1,2\n"
    kwargs = request.call_args.kwargs
    assert kwargs["public_only"] is True
    assert kwargs["deadline_sec"] > 0
    response.close.assert_called_once()


# ---------------------------------------------------------------------------
# github_data — gh resolution, redirect/token hygiene, error scrubbing
# ---------------------------------------------------------------------------

def test_gh_resolution_excludes_the_current_directory(tmp_path, monkeypatch):
    fake_name = "gh.cmd" if os.name == "nt" else "gh"
    (tmp_path / fake_name).write_text("", encoding="utf-8")
    if os.name != "nt":
        (tmp_path / fake_name).chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert _gh_executable() == ""  # gh planted in the working directory is ignored

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / fake_name).write_text("", encoding="utf-8")
    if os.name != "nt":
        (outside / fake_name).chmod(0o755)
    monkeypatch.setenv("PATH", str(outside))
    assert Path(_gh_executable()) == (outside / fake_name)


def test_gh_token_runs_the_resolved_executable(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    seen_argv = []

    def fake_run(argv, **kwargs):
        seen_argv.append(argv)
        return MagicMock(returncode=0, stdout="gh-token-value\n")

    with patch("scripts.fetchers.github_data._gh_executable", return_value=str(Path("tools") / "gh")):
        with patch("scripts.fetchers.github_data.subprocess.run", side_effect=fake_run):
            token = _resolve_github_token({})
    assert token == "gh-token-value"
    assert seen_argv[0][0] == str(Path("tools") / "gh")


def test_github_api_calls_never_follow_redirects(tmp_path):
    fetcher = GitHubDataFetcher("covid", str(tmp_path), {})
    response = MagicMock(status_code=200)
    response.json.return_value = {"items": []}
    with patch("scripts.fetchers.github_data.request_with_retry", return_value=response) as request:
        with patch("scripts.fetchers.github_data._resolve_github_token", return_value=""):
            with pytest.raises(ValueError):
                fetcher.scout()
    assert request.call_args.kwargs["allow_redirects"] is False


def test_github_error_message_hides_response_body(tmp_path):
    fetcher = GitHubDataFetcher("covid", str(tmp_path), {})
    response = MagicMock(status_code=502, text="<html>Gateway exploded with secrets</html>")
    with patch("scripts.fetchers.github_data.request_with_retry", return_value=response):
        with patch("scripts.fetchers.github_data._resolve_github_token", return_value=""):
            with pytest.raises(ValueError) as caught:
                fetcher.scout()
    message = str(caught.value)
    assert "502" in message
    assert "Gateway exploded" not in message


# ---------------------------------------------------------------------------
# kaggle_fetcher — env restore, unzip=False, archive validation
# ---------------------------------------------------------------------------

def _fake_kaggle_module(download_behavior):
    module = MagicMock()
    module.api.dataset_download_files.side_effect = download_behavior
    return module


def _kaggle_fetcher(tmp_path):
    fetcher = KaggleFetcher("owner/dataset", str(tmp_path), {})
    fetcher.username = "unit-user"
    fetcher.key = "unit-key"
    return fetcher


def test_kaggle_credentials_are_restored_after_extract(tmp_path, monkeypatch):
    monkeypatch.setenv("KAGGLE_USERNAME", "ambient-user")
    monkeypatch.delenv("KAGGLE_KEY", raising=False)

    def fake_download(handle, path, unzip):
        assert unzip is False  # the fetcher validates the archive itself
        with zipfile.ZipFile(Path(path) / "dataset.zip", "w") as bundle:
            bundle.writestr("data.csv", "a,b\n1,2\n")

    fetcher = _kaggle_fetcher(tmp_path)
    with patch.dict(sys.modules, {"kaggle": _fake_kaggle_module(fake_download)}):
        frame = fetcher.extract()

    assert frame["a"].tolist() == [1]
    assert os.environ["KAGGLE_USERNAME"] == "ambient-user"
    assert "KAGGLE_KEY" not in os.environ


def test_kaggle_credentials_restored_when_download_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("KAGGLE_USERNAME", "ambient-user")

    def failing_download(handle, path, unzip):
        raise RuntimeError("network gone")

    fetcher = _kaggle_fetcher(tmp_path)
    with patch.dict(sys.modules, {"kaggle": _fake_kaggle_module(failing_download)}):
        with pytest.raises(RuntimeError):
            fetcher.extract()
    assert os.environ["KAGGLE_USERNAME"] == "ambient-user"


def test_kaggle_rejects_zip_slip_members(tmp_path):
    def hostile_download(handle, path, unzip):
        with zipfile.ZipFile(Path(path) / "dataset.zip", "w") as bundle:
            bundle.writestr("../evil.csv", "stolen")

    fetcher = _kaggle_fetcher(tmp_path)
    with patch.dict(sys.modules, {"kaggle": _fake_kaggle_module(hostile_download)}):
        with pytest.raises(ValueError, match="BLOCKED"):
            fetcher.extract()


def test_kaggle_rejects_absolute_and_drive_paths(tmp_path):
    def hostile_download(handle, path, unzip):
        with zipfile.ZipFile(Path(path) / "dataset.zip", "w") as bundle:
            bundle.writestr("/abs/escape.csv", "stolen")

    fetcher = _kaggle_fetcher(tmp_path)
    with patch.dict(sys.modules, {"kaggle": _fake_kaggle_module(hostile_download)}):
        with pytest.raises(ValueError, match="BLOCKED"):
            fetcher.extract()


def test_kaggle_rejects_symlink_entries(tmp_path):
    def hostile_download(handle, path, unzip):
        with zipfile.ZipFile(Path(path) / "dataset.zip", "w") as bundle:
            info = zipfile.ZipInfo("link.csv")
            info.external_attr = (0o120777 << 16) | 0o644  # S_IFLNK mode bits
            bundle.writestr(info, "elsewhere/data.csv")

    fetcher = _kaggle_fetcher(tmp_path)
    with patch.dict(sys.modules, {"kaggle": _fake_kaggle_module(hostile_download)}):
        with pytest.raises(ValueError, match="symlink"):
            fetcher.extract()


def test_kaggle_rejects_archives_beyond_500_mib_uncompressed(tmp_path):
    def bomb_download(handle, path, unzip):
        with zipfile.ZipFile(Path(path) / "dataset.zip", "w", zipfile.ZIP_STORED) as bundle:
            bundle.writestr("data.csv", "a\n1\n")
            bundle.filelist[-1].file_size = ARCHIVE_UNCOMPRESSED_LIMIT + 1

    fetcher = _kaggle_fetcher(tmp_path)
    with patch.dict(sys.modules, {"kaggle": _fake_kaggle_module(bomb_download)}):
        with pytest.raises(ValueError, match="500 MB"):
            fetcher.extract()


def test_kaggle_fetcher_source_avoids_legacy_path_helpers():
    import inspect
    source = inspect.getsource(KaggleFetcher)
    assert "os.path" not in source
    assert "glob.glob" not in source


# ---------------------------------------------------------------------------
# fred / worldbank / yahoo — no credentials or response bodies in errors
# ---------------------------------------------------------------------------

def test_fred_error_never_echoes_key_or_response_body(tmp_path):
    fetcher = FREDFetcher("GDP", str(tmp_path), {"FRED_API_KEY": TEST_KEY})
    fetcher.api_key = TEST_KEY  # as scout() would set it
    leaking = MagicMock(status_code=500,
                        text=f"upstream said: api_key={TEST_KEY}&file_type=json boom")
    with patch("scripts.fetchers.fred.request_with_retry", return_value=leaking):
        with pytest.raises(ValueError) as caught:
            fetcher.extract()
    message = str(caught.value)
    assert TEST_KEY not in message
    assert "upstream said" not in message


def test_fred_connection_error_never_echoes_keyed_url(tmp_path):
    fetcher = FREDFetcher("GDP", str(tmp_path), {"FRED_API_KEY": TEST_KEY})
    fetcher.api_key = TEST_KEY  # as scout() would set it
    # requests.ConnectionError is NOT a builtin ConnectionError, so production
    # code reaches this handler via request_with_retry's coded DataFetchError;
    # ConnectionResetError exercises the fetcher's own scrubbing directly.
    leaking = ConnectionResetError(
        f"Max retries exceeded with url: /fred/series/observations?api_key={TEST_KEY}")
    with patch("scripts.fetchers.fred.request_with_retry", side_effect=leaking):
        with pytest.raises(RuntimeError) as caught:
            fetcher.extract()
    message = str(caught.value)
    assert TEST_KEY not in message
    assert "api_key" not in message


def test_worldbank_error_never_echoes_raw_exception_text(tmp_path):
    fetcher = WorldBankFetcher("NY.GDP.MKTP.CD", str(tmp_path), {})
    leaking = ConnectionResetError(
        "Max retries exceeded with url: /v2/country/all/indicator?secret=ambassador")
    with patch("scripts.fetchers.worldbank.request_with_retry", side_effect=leaking):
        with pytest.raises(RuntimeError) as caught:
            fetcher.scout()
    message = str(caught.value)
    assert "secret=ambassador" not in message
    assert "ConnectionResetError" in message


def test_worldbank_provider_message_is_bounded(tmp_path):
    fetcher = WorldBankFetcher("NY.GDP.MKTP.CD", str(tmp_path), {})
    response = MagicMock(status_code=200)
    response.json.return_value = {"message": [{"value": "y" * 5000}]}
    with patch("scripts.fetchers.worldbank.request_with_retry", return_value=response):
        with pytest.raises(ValueError) as caught:
            fetcher.scout()
    message = str(caught.value)
    assert "y" * 200 in message
    assert "y" * 5000 not in message


def test_yahoo_scrubs_body_like_exception_text(tmp_path):
    fetcher = YahooFinanceFetcher("AAPL", str(tmp_path), {})

    class FakeTicker:
        def __init__(self, query):
            pass

        def history(self, period):
            raise RuntimeError('<html>Gateway error {"credential":"leak"}</html>')

    fake_yf = MagicMock()
    fake_yf.Ticker = FakeTicker
    with patch.dict(sys.modules, {"yfinance": fake_yf}):
        with pytest.raises(ValueError) as caught:
            fetcher.scout()
    message = str(caught.value)
    assert "leak" not in message and "<html>" not in message
    assert "rate limit" not in message.lower()


# ---------------------------------------------------------------------------
# source catalog — every CLI key still resolves
# ---------------------------------------------------------------------------

def test_all_thirteen_cli_keys_still_resolve(tmp_path):
    keys = list_sources()
    assert len(keys) == 13
    affected = ["airbnb", "coingecko", "datagov", "eurostat", "fred",
                "github", "kaggle", "openml", "rdatasets", "sec", "worldbank",
                "yahoo", "yfinance"]
    assert set(keys) == set(affected)
    for key in ("airbnb", "datagov", "fred", "github", "kaggle", "worldbank", "yahoo"):
        assert get_fetcher(key, "query", str(tmp_path), {}) is not None
