"""Data fidelity across conversion and worker transport."""

from openpyxl import load_workbook
from pathlib import Path
import csv
import io
import json
import numpy as np
import pandas as pd
import pytest
import random
import sqlite3
from scripts.adapters.extractor_protocol import (
    WorkerRequest, decode_frame, encode_frame, sha256_bytes,
)
from scripts.adapters.script_runner import SubprocessExtractorRunner
from scripts.errors import DataFetchError
from scripts.format_alchemy import FormatAlchemyEngine


def write_lexical_csv(path):
    expected = [["001", "NA", "line\nnext"], ["0007", "null", "=1+1"]]
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(["id", "marker", "text"])
    writer.writerows(expected)
    path.write_bytes(stream.getvalue().encode("utf-8"))
    return expected


def read_converted_rows(path, target):
    if target == "json":
        return [list(record.values()) for record in json.loads(path.read_text(encoding="utf-8"))]
    if target == "sqlite":
        with sqlite3.connect(path) as connection:
            return [list(row) for row in connection.execute('SELECT * FROM "sample"')]
    if target == "parquet":
        return pd.read_parquet(path).values.tolist()
    workbook = load_workbook(path, read_only=True, data_only=False)
    try:
        return [list(row) for row in workbook.active.iter_rows(min_row=2, values_only=True)]
    finally:
        workbook.close()


@pytest.mark.parametrize("target", ["json", "sqlite", "parquet", "excel"])
def test_standalone_csv_conversion_preserves_lexical_values_and_source(tmp_path, target):
    source = tmp_path / "sample.csv"
    expected = write_lexical_csv(source)
    original = source.read_bytes()

    result = Path(FormatAlchemyEngine.convert(str(source), target))

    assert read_converted_rows(result, target) == expected
    assert source.read_bytes() == original
    if target == "excel":
        workbook = load_workbook(result, read_only=True, data_only=False)
        try:
            assert workbook.active["C3"].value == "=1+1"
            assert workbook.active["C3"].data_type == "s"
        finally:
            workbook.close()


@pytest.mark.parametrize("target", ["json", "sqlite", "parquet"])
def test_empty_string_and_literal_na_do_not_become_missing_on_conversion(tmp_path, target):
    source = tmp_path / "sample.csv"
    source.write_bytes(b'id,marker\n"",NA\n001,null\n')

    result = Path(FormatAlchemyEngine.convert(str(source), target))

    assert read_converted_rows(result, target) == [["", "NA"], ["001", "null"]]


@pytest.mark.parametrize("preexisting", [False, True], ids=["new-output", "existing-output"])
def test_duplicate_headers_fail_before_loss_or_output_replacement(tmp_path, preexisting):
    """JSON record keys cannot retain two distinct columns with the same name."""
    source = tmp_path / "duplicate.csv"
    source.write_bytes(b'same,same\nleft,right\n')
    output = source.with_suffix(".json")
    previous = b'[{"old": "must remain"}]\n'
    if preexisting:
        output.write_bytes(previous)
    original = source.read_bytes()

    with pytest.raises(DataFetchError) as caught:
        FormatAlchemyEngine.convert(str(source), "json", overwrite=True)

    assert caught.value.code == "CONVERSION_LOSS_RISK"
    assert source.read_bytes() == original
    if preexisting:
        assert output.read_bytes() == previous
    else:
        assert not output.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_existing_destination_is_protected_without_overwrite(tmp_path):
    source = tmp_path / "sample.csv"
    write_lexical_csv(source)
    output = source.with_suffix(".json")
    previous = b'[{"existing":true}]\n'
    output.write_bytes(previous)
    original = source.read_bytes()

    with pytest.raises(DataFetchError) as caught:
        FormatAlchemyEngine.convert(str(source), "json")

    assert caught.value.code == "INVALID_OUTPUT"
    assert output.read_bytes() == previous
    assert source.read_bytes() == original


def test_unsupported_route_leaves_input_and_destination_untouched(tmp_path):
    source = tmp_path / "sample.parquet"
    pd.DataFrame({"id": [1, 2]}).to_parquet(source)
    original = source.read_bytes()

    with pytest.raises((DataFetchError, ValueError)):
        FormatAlchemyEngine.convert(str(source), "json")

    assert source.read_bytes() == original
    assert not source.with_suffix(".json").exists()
    assert not list(tmp_path.glob("*.tmp"))


# Worker transport fidelity


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("\x00suffix", id="leading-nul"),
        pytest.param("left\x00right", id="embedded-nul"),
        pytest.param("prefix\x00", id="trailing-nul"),
        pytest.param("\x00\x00", id="only-nuls"),
        pytest.param("\x01n\x01\x00n", id="literal-escape-and-nul"),
        pytest.param('a,\x00"b\nlast', id="quoted-multiline-nul"),
        pytest.param("\ufefffirst-value", id="leading-bom-is-value"),
        pytest.param("left\rright", id="carriage-return"),
        pytest.param("left\r\nright", id="crlf"),
        pytest.param("\u0645\u0631\u062d\u0628\u0627\U0001f30d", id="unicode"),
        pytest.param("", id="empty-string"),
    ],
)
def test_supported_string_bytes_survive_codec_exactly(text):
    """IPC-001: pandas C decoding silently discarded text following NUL."""
    original = pd.DataFrame({"text": pd.Series([text, "control", None], dtype=object)})

    restored = decode_frame(*encode_frame(original))

    pd.testing.assert_frame_equal(restored, original, check_exact=True)


def test_legacy_payload_with_literal_nul_remains_readable():
    _, sidecar = encode_frame(pd.DataFrame({"text": pd.Series(["placeholder"], dtype=object)}))
    restored = decode_frame(b"left\x00right\x01n\n", sidecar)
    assert restored.iloc[0, 0] == "left\x00right\x01n"


def test_seeded_unicode_and_delimiter_corpus_survives_without_normalization():
    generator = random.Random(20261102)
    alphabet = "abcXYZ019,\t\r\n\x00\"'\\ " + "\u0645\u0627\u00e9\u0301\U0001f642"
    strings = [
        "".join(generator.choice(alphabet) for _ in range(generator.randrange(1, 97)))
        for _ in range(40)
    ]
    original = pd.DataFrame({"text": pd.Series(strings, dtype=object)})

    pd.testing.assert_frame_equal(decode_frame(*encode_frame(original)), original, check_exact=True)


def test_nul_missing_and_duplicate_headers_preserve_combined_schema():
    original = pd.concat(
        [
            pd.Series(["one\x00two", "", None], dtype=object, name="same"),
            pd.Series([np.iinfo(np.int64).max, np.iinfo(np.int64).min, 0], dtype="int64", name="same"),
            pd.Series([True, pd.NA, False], dtype="boolean", name="flag"),
            pd.Series(pd.to_datetime(["2026-01-02T12:00:00Z", None, "2026-02-03T09:00:00Z"]), name="when"),
        ], axis=1,
    )

    pd.testing.assert_frame_equal(decode_frame(*encode_frame(original)), original, check_exact=True)


def test_real_worker_preserves_embedded_nul_and_nullable_values():
    script = (
        "import pandas as pd\n"
        "def extract_frame(url=None, columns=None, table_index=None, row_limit=0):\n"
        "    return pd.DataFrame({'text': pd.Series(['left\\x00right', '', None], dtype=object),\n"
        "                         'count': pd.Series([1, None, 3], dtype='Int64')})\n"
    ).encode("utf-8")
    request = WorkerRequest(
        script_sha256=sha256_bytes(script), query_url="https://example.com/data.csv",
        columns=(), table_index=None, row_limit=0, deadline_sec=30.0, allowed_origin="",
    )
    runner = SubprocessExtractorRunner()

    result, restored = runner.extract(script, request)

    assert result.error_code is None, result.to_json()
    expected = pd.DataFrame({
        "text": pd.Series(["left\x00right", "", None], dtype=object),
        "count": pd.Series([1, None, 3], dtype="Int64"),
    })
    pd.testing.assert_frame_equal(restored, expected, check_exact=True)


@pytest.mark.parametrize("mutation", ["extra_field", "foreign_format"])
def test_corrupt_sidecar_cannot_silently_change_an_otherwise_valid_frame(mutation):
    payload, sidecar = encode_frame(pd.DataFrame({"text": ["unchanged"]}))
    metadata = json.loads(sidecar)
    if mutation == "extra_field":
        metadata["approved"] = True
    else:
        metadata["format"] = "pickle"

    with pytest.raises(DataFetchError) as caught:
        decode_frame(payload, json.dumps(metadata).encode("utf-8"))

    assert caught.value.code in {"IPC_SCHEMA_INVALID", "IPC_PAYLOAD_CORRUPT"}
