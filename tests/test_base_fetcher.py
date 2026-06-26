import os
import pathlib
from unittest import mock

import numpy as np
import pandas as pd
import pytest

from scripts.base import BaseFetcher


class DummyFetcher(BaseFetcher):

    def scout(self) -> dict:
        return {"url": "http://dummy.url", "size_info": "100 MB"}

    def extract(self) -> pd.DataFrame:
        return pd.DataFrame({"A": [1, 2, 3], "B": [4, 5, 6]})


@pytest.fixture
def dummy_fetcher(tmp_path: pathlib.Path) -> DummyFetcher:
    return DummyFetcher(
        query="test query",
        outdir=str(tmp_path),
        config={"API_KEY": "test_key"}
    )


def test_validate_payload_empty_df(dummy_fetcher: DummyFetcher) -> None:
    empty_df = pd.DataFrame()
    with pytest.raises(ValueError, match="Graceful Fallback Triggered: Dataframe is completely empty."):
        dummy_fetcher.validate_payload(empty_df)


def test_validate_payload_high_null_density(dummy_fetcher: DummyFetcher) -> None:
    high_null_df = pd.DataFrame({
        "A": [1, np.nan, np.nan, np.nan, np.nan],
        "B": [np.nan, np.nan, np.nan, np.nan, np.nan]
    })
    
    with pytest.raises(ValueError, match="Graceful Fallback Triggered: Null density exceeds 80%"):
        dummy_fetcher.validate_payload(high_null_df)


def test_validate_payload_passes_clean_data(dummy_fetcher: DummyFetcher) -> None:
    clean_df = pd.DataFrame({"A": [1, 2], "B": [3, 4]})
    try:
        dummy_fetcher.validate_payload(clean_df)
    except ValueError as e:
        pytest.fail(f"validate_payload raised ValueError unexpectedly: {e}")


def test_save_csv_creates_file_and_dictionary(dummy_fetcher: DummyFetcher) -> None:
    df = pd.DataFrame({"A": [1, 2]})
    filename = "test_data_raw.csv"
    
    dummy_fetcher.save_csv(df, filename)
    
    expected_csv_path = os.path.join(dummy_fetcher.outdir, filename)
    expected_txt_path = os.path.join(dummy_fetcher.outdir, "dataset_description.txt")
    
    assert os.path.exists(expected_csv_path)
    assert os.path.exists(expected_txt_path)


@mock.patch("builtins.input", return_value="y")
def test_pre_flight_returns_zero_on_yes(mock_input: mock.MagicMock, dummy_fetcher: DummyFetcher) -> None:
    metadata = dummy_fetcher.scout()
    result = dummy_fetcher.pre_flight_authorization(metadata)
    assert result == 0


@mock.patch("builtins.input", return_value="500")
def test_pre_flight_returns_int_on_number(mock_input: mock.MagicMock, dummy_fetcher: DummyFetcher) -> None:
    metadata = dummy_fetcher.scout()
    result = dummy_fetcher.pre_flight_authorization(metadata)
    assert result == 500


@mock.patch("builtins.input", return_value="n")
def test_pre_flight_raises_on_no(mock_input: mock.MagicMock, dummy_fetcher: DummyFetcher) -> None:
    metadata = dummy_fetcher.scout()
    with pytest.raises(ValueError, match="Extraction aborted by user during Pre-Flight Authorization."):
        dummy_fetcher.pre_flight_authorization(metadata)


@mock.patch("builtins.input", return_value="10")
def test_run_slices_dataframe_to_row_limit(mock_input: mock.MagicMock, dummy_fetcher: DummyFetcher) -> None:
    df_100 = pd.DataFrame({"A": range(100)})
    with mock.patch.object(dummy_fetcher, 'extract', return_value=df_100):
        filepath = dummy_fetcher.run()
        
    df_out = pd.read_csv(filepath)
    assert len(df_out) == 10
