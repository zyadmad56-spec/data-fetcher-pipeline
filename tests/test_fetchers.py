import os
import pathlib
from unittest.mock import MagicMock, patch
import pytest

# NO MODULE LEVEL HEAVY IMPORTS to prevent collection hangs!

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fred_fetcher(tmp_path: pathlib.Path):
    from data_fetcher.fetchers.fred import FREDFetcher
    return FREDFetcher("GDP", str(tmp_path), {"FRED_API_KEY": "test_key"})


@pytest.fixture
def sec_fetcher(tmp_path: pathlib.Path):
    from data_fetcher.fetchers.sec import SECFetcher
    return SECFetcher("AAPL", str(tmp_path), {"SEC_API_KEY": "test@example.com"})


@pytest.fixture
def airbnb_fetcher(tmp_path: pathlib.Path):
    from data_fetcher.fetchers.airbnb import AirbnbFetcher
    return AirbnbFetcher("amsterdam", str(tmp_path), {})


@pytest.fixture
def kaggle_fetcher(tmp_path: pathlib.Path):
    from data_fetcher.fetchers.kaggle import KaggleFetcher
    return KaggleFetcher("user/dataset", str(tmp_path), {"KAGGLE_USERNAME": "u", "KAGGLE_KEY": "k"})


@pytest.fixture
def yahoo_fetcher(tmp_path: pathlib.Path):
    from data_fetcher.fetchers.yahoo import YahooFinanceFetcher
    return YahooFinanceFetcher("AAPL", str(tmp_path), {})


@pytest.fixture
def mock_yfinance():
    import sys
    from unittest.mock import MagicMock
    mock_yf = MagicMock()
    with patch.dict(sys.modules, {"yfinance": mock_yf}):
        yield mock_yf


@pytest.fixture
def mock_openml():
    import sys
    from unittest.mock import MagicMock
    mock_om = MagicMock()
    with patch.dict(sys.modules, {"openml": mock_om}):
        yield mock_om


@pytest.fixture
def openml_fetcher(tmp_path: pathlib.Path):
    from data_fetcher.fetchers.openml import OpenMLFetcher
    return OpenMLFetcher("iris", str(tmp_path), {})


@pytest.fixture
def generic_fetcher(tmp_path: pathlib.Path):
    from data_fetcher.fetchers.generic import GenericFetcher
    return GenericFetcher("test", str(tmp_path), {})


# ---------------------------------------------------------------------------
# FREDFetcher
# ---------------------------------------------------------------------------

def test_fred_scout_returns_metadata(fred_fetcher) -> None:
    metadata = fred_fetcher.scout()
    assert "url" in metadata
    assert "size_info" in metadata
    assert fred_fetcher.api_key == "test_key"


@patch("data_fetcher.fetchers.fred.requests.get")
def test_fred_extract_happy_path(mock_get: MagicMock, fred_fetcher) -> None:
    fred_fetcher.api_key = "test_key"
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "observations": [
            {"date": "2024-01-01", "value": "100.0"},
            {"date": "2024-02-01", "value": "101.5"},
        ]
    }
    mock_get.return_value = mock_response

    df = fred_fetcher.extract()

    assert len(df) == 2
    assert list(df.columns) == ["date", "value"]
    mock_get.assert_called_once()


def test_fred_extract_timeout(fred_fetcher) -> None:
    import requests.exceptions
    with patch("data_fetcher.fetchers.fred.requests.get", side_effect=requests.exceptions.Timeout) as mock_get:
        fred_fetcher.api_key = "test_key"
        with pytest.raises(RuntimeError, match="timed out"):
            fred_fetcher.extract()


@patch("data_fetcher.fetchers.fred.requests.get")
def test_fred_extract_bad_status(mock_get: MagicMock, fred_fetcher) -> None:
    fred_fetcher.api_key = "test_key"
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.text = "Internal Server Error"
    mock_get.return_value = mock_response

    with pytest.raises(ValueError, match="FRED API Error 500"):
        fred_fetcher.extract()


@patch("data_fetcher.fetchers.fred.requests.get")
def test_fred_extract_empty_observations(mock_get: MagicMock, fred_fetcher) -> None:
    fred_fetcher.api_key = "test_key"
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"observations": []}
    mock_get.return_value = mock_response

    with pytest.raises(ValueError, match="No observations returned"):
        fred_fetcher.extract()


# ---------------------------------------------------------------------------
# SECFetcher
# ---------------------------------------------------------------------------

def test_sec_scout_resolves_cik(sec_fetcher) -> None:
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "0": {"cik_str": "320193", "ticker": "AAPL", "title": "Apple Inc"}
    }
    sec_fetcher.session.get = MagicMock(return_value=mock_response)

    metadata = sec_fetcher.scout()

    assert sec_fetcher.cik_str == "0000320193"
    assert "url" in metadata
    assert "320193" in metadata["url"]


def test_sec_scout_unknown_ticker(sec_fetcher) -> None:
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "0": {"cik_str": "1", "ticker": "MSFT", "title": "Microsoft"}
    }
    sec_fetcher.session.get = MagicMock(return_value=mock_response)

    with pytest.raises(ValueError, match="not found in SEC database"):
        sec_fetcher.scout()


def test_sec_scout_timeout(sec_fetcher) -> None:
    import requests.exceptions
    sec_fetcher.session.get = MagicMock(side_effect=requests.exceptions.Timeout)

    with pytest.raises(RuntimeError, match="timed out"):
        sec_fetcher.scout()


def test_sec_extract_happy_path(sec_fetcher) -> None:
    sec_fetcher.api_key = "test@example.com"
    sec_fetcher.cik_str = "0000320193"
    sec_fetcher.facts_url = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "facts": {
            "us-gaap": {
                "Revenue": {
                    "units": {
                        "USD": [
                            {"val": 1000, "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2024-01-01", "end": "2023-12-31"}
                        ]
                    }
                }
            }
        }
    }
    sec_fetcher.session.get = MagicMock(return_value=mock_response)

    df = sec_fetcher.extract()

    assert len(df) == 1
    assert "taxonomy" in df.columns
    assert "concept" in df.columns
    assert df.iloc[0]["val"] == 1000


def test_sec_extract_empty_facts(sec_fetcher) -> None:
    sec_fetcher.cik_str = "0000320193"
    sec_fetcher.facts_url = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"facts": {}}
    sec_fetcher.session.get = MagicMock(return_value=mock_response)

    with pytest.raises(ValueError, match="No financial facts found"):
        sec_fetcher.extract()


# ---------------------------------------------------------------------------
# AirbnbFetcher
# ---------------------------------------------------------------------------

@patch("data_fetcher.fetchers.airbnb.requests.get")
def test_airbnb_scout_finds_link(mock_get: MagicMock, airbnb_fetcher) -> None:
    html = (
        '<html><body>'
        '<a href="http://data.insideairbnb.com/the-netherlands/north-holland/amsterdam/'
        '2024-01-01/data/listings.csv.gz">Download</a>'
        '</body></html>'
    )
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = html
    mock_get.return_value = mock_response

    metadata = airbnb_fetcher.scout()

    assert metadata["url"].endswith("listings.csv.gz")
    assert "amsterdam" in metadata["url"].lower()


@patch("data_fetcher.fetchers.airbnb.requests.get")
def test_airbnb_scout_no_link_found(mock_get: MagicMock, airbnb_fetcher) -> None:
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = "<html><body><a href='unrelated.zip'>Other</a></body></html>"
    mock_get.return_value = mock_response

    with pytest.raises(ValueError, match="Could not resolve"):
        airbnb_fetcher.scout()


def test_airbnb_scout_timeout(airbnb_fetcher) -> None:
    import requests.exceptions
    with patch("data_fetcher.fetchers.airbnb.requests.get", side_effect=requests.exceptions.Timeout) as mock_get:
        with pytest.raises(RuntimeError, match="timed out"):
            airbnb_fetcher.scout()


@patch("data_fetcher.fetchers.airbnb.requests.get")
@patch("pandas.read_csv")
def test_airbnb_extract_happy_path(
    mock_read_csv: MagicMock, mock_get: MagicMock, airbnb_fetcher
) -> None:
    import pandas as pd
    airbnb_fetcher.download_url = "http://example.com/listings.csv.gz"

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.iter_content.return_value = [b"fake_gzip_data"]
    mock_get.return_value = mock_response

    expected_df = pd.DataFrame({"id": [1, 2], "price": [100, 200]})
    mock_read_csv.return_value = expected_df

    df = airbnb_fetcher.extract()

    assert len(df) == 2
    assert "id" in df.columns


# ---------------------------------------------------------------------------
# KaggleFetcher
# ---------------------------------------------------------------------------

def test_kaggle_scout_validates_credentials(kaggle_fetcher) -> None:
    metadata = kaggle_fetcher.scout()

    assert "url" in metadata
    assert kaggle_fetcher.username == "u"
    assert kaggle_fetcher.key == "k"


def test_kaggle_extract_cleans_env_on_success(kaggle_fetcher, tmp_path: pathlib.Path) -> None:
    import pandas as pd
    import sys
    kaggle_fetcher.username = "u"
    kaggle_fetcher.key = "k"

    csv_file = tmp_path / "data.csv"
    pd.DataFrame({"col": [1]}).to_csv(csv_file, index=False)

    mock_kaggle = MagicMock()
    with patch.dict(os.environ, {}, clear=False), \
         patch.dict(sys.modules, {"kaggle": mock_kaggle}), \
         patch("glob.glob", return_value=[str(csv_file)]):

        df = kaggle_fetcher.extract()

    assert "KAGGLE_USERNAME" not in os.environ
    assert "KAGGLE_KEY" not in os.environ
    assert len(df) == 1


def test_kaggle_extract_cleans_env_on_failure(kaggle_fetcher) -> None:
    import sys
    kaggle_fetcher.username = "u"
    kaggle_fetcher.key = "k"

    mock_kaggle = MagicMock()
    mock_kaggle.api.dataset_download_files.side_effect = RuntimeError("Download failed")

    with patch.dict(os.environ, {}, clear=False), \
         patch.dict(sys.modules, {"kaggle": mock_kaggle}), \
         pytest.raises(RuntimeError, match="Download failed"):

        kaggle_fetcher.extract()

    assert "KAGGLE_USERNAME" not in os.environ
    assert "KAGGLE_KEY" not in os.environ


# ---------------------------------------------------------------------------
# YahooFinanceFetcher
# ---------------------------------------------------------------------------

def test_yahoo_scout_valid_ticker(yahoo_fetcher, mock_yfinance) -> None:
    import pandas as pd
    mock_yfinance.Ticker.return_value.history.return_value = pd.DataFrame({"Close": [150.0, 151.0]})

    metadata = yahoo_fetcher.scout()

    assert "url" in metadata
    assert "AAPL" in metadata["url"]


def test_yahoo_scout_invalid_ticker(yahoo_fetcher, mock_yfinance) -> None:
    import pandas as pd
    mock_yfinance.Ticker.return_value.history.return_value = pd.DataFrame()

    with pytest.raises(ValueError, match="not found or delisted"):
        yahoo_fetcher.scout()


# ---------------------------------------------------------------------------
# OpenMLFetcher
# ---------------------------------------------------------------------------

@patch("openml.datasets.list_datasets", create=True)
def test_openml_scout_finds_dataset(mock_list, openml_fetcher, mock_openml) -> None:
    import pandas as pd
    datasets_df = pd.DataFrame({
        "did": [1, 2],
        "name": ["iris", "wine"],
        "NumberOfInstances": [150, 178],
        "NumberOfFeatures": [4, 13],
    })
    mock_list.return_value = datasets_df

    metadata = openml_fetcher.scout()

    assert openml_fetcher.target_dataset_id == 1
    assert openml_fetcher.dataset_name == "iris"
    assert "url" in metadata


@patch("openml.datasets.list_datasets", create=True)
def test_openml_scout_no_match(mock_list, openml_fetcher, mock_openml) -> None:
    import pandas as pd
    datasets_df = pd.DataFrame({
        "did": [1],
        "name": ["completely_unrelated"],
        "NumberOfInstances": [100],
        "NumberOfFeatures": [5],
    })
    mock_list.return_value = datasets_df

    with pytest.raises(ValueError, match="No datasets found"):
        openml_fetcher.scout()


# ---------------------------------------------------------------------------
# GenericFetcher
# ---------------------------------------------------------------------------

def test_generic_scout_raises_valueerror(generic_fetcher) -> None:
    with pytest.raises(ValueError, match="GenericFetcher has no scouting logic"):
        generic_fetcher.scout()


def test_generic_extract_raises_not_implemented(generic_fetcher) -> None:
    with pytest.raises(NotImplementedError, match="not yet implemented"):
        generic_fetcher.extract()


