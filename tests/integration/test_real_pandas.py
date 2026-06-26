import subprocess
import sys
import os
import pathlib

import pytest

INTEGRATION_SCRIPT = r'''
import os
import sys
import tempfile

src_path = sys.argv[1]
sys.path.insert(0, src_path)

import pandas as pd
import numpy as np

from data_fetcher.base import BaseFetcher

class TestFetcher(BaseFetcher):
    def scout(self) -> dict:
        return {"url": "http://test", "size_info": "1 KB"}
    def extract(self) -> pd.DataFrame:
        return pd.DataFrame({"A": [1, 2, 3], "B": [4, 5, 6]})

with tempfile.TemporaryDirectory() as tmpdir:
    f = TestFetcher("test", tmpdir, {})

    df_clean = pd.DataFrame({"A": [1, 2], "B": [3, 4]})
    f.validate_payload(df_clean)
    print("PASS: validate_payload clean")

    df_null = pd.DataFrame({"A": [1, np.nan, np.nan]})
    try:
        f.validate_payload(df_null)
        print("FAIL: should have raised ValueError for high null density")
        sys.exit(1)
    except ValueError as e:
        if "Null density" in str(e):
            print("PASS: validate_payload high null density rejected")
        else:
            print(f"FAIL: wrong error: {e}")
            sys.exit(1)

    df = pd.DataFrame({"col1": [10, 20], "col2": ["a", "b"]})
    f.save_csv(df, "test_raw.csv")
    csv_path = os.path.join(f.outdir, "test_raw.csv")
    df_read = pd.read_csv(csv_path)
    assert list(df_read.columns) == ["col1", "col2"], f"Got columns: {list(df_read.columns)}"
    assert len(df_read) == 2
    print("PASS: save_csv round-trip")

    df_empty = pd.DataFrame()
    try:
        f.validate_payload(df_empty)
        print("FAIL: should have raised ValueError for empty df")
        sys.exit(1)
    except ValueError as e:
        if "completely empty" in str(e):
            print("PASS: validate_payload empty df rejected")
        else:
            print(f"FAIL: wrong error: {e}")
            sys.exit(1)

print("ALL INTEGRATION TESTS PASSED")
'''

@pytest.mark.slow
@pytest.mark.slow
def test_real_pandas_roundtrip() -> None:
    src_dir = str(pathlib.Path(__file__).resolve().parent.parent.parent / "src")
    result = subprocess.run(
        [sys.executable, "-c", INTEGRATION_SCRIPT, src_dir],
        capture_output=True,
        text=True,
        timeout=300,
    )

    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr)
        pytest.fail(f"Integration subprocess failed with code {result.returncode}")

    assert "ALL INTEGRATION TESTS PASSED" in result.stdout
