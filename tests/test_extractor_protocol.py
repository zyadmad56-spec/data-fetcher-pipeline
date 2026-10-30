"""Phase 1 acceptance: worker IPC fidelity, strict validation, no approval leakage."""
import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from scripts.adapters.extractor_protocol import (
    IPC_APPROVAL_FIELD_REJECTED, IPC_PAYLOAD_CORRUPT, IPC_SCHEMA_INVALID,
    IPC_UNREPRESENTABLE_VALUES, MAX_COLUMNS, PreviewReceipt, WorkerRequest,
    WorkerResult, decode_frame, encode_frame, sha256_bytes,
)


def round_trip(frame: pd.DataFrame) -> pd.DataFrame:
    csv_bytes, sidecar_bytes = encode_frame(frame)
    return decode_frame(csv_bytes, sidecar_bytes)


# ---------------------------------------------------------------------------
# Message validation: WorkerRequest
# ---------------------------------------------------------------------------

def valid_request_json() -> dict:
    return WorkerRequest(script_sha256='a' * 64, query_url='https://example.com/d.csv',
                         columns=['a', 'b'], table_index=1, row_limit=5,
                         max_bytes=1024, deadline_sec=12.5,
                         allowed_origin='https://example.com').to_json()


def test_worker_request_round_trips_and_is_frozen():
    request = WorkerRequest.from_json(valid_request_json())
    assert request.columns == ('a', 'b') and request.table_index == 1
    assert WorkerRequest.from_json(request.to_json()) == request
    with pytest.raises(Exception):
        request.row_limit = 3  # frozen dataclass


@pytest.mark.parametrize('mutation', [
    {'extra': 1},
    {'script_sha256': 'A' * 64},
    {'script_sha256': 'a' * 63},
    {'query_url': 'ftp://example.com/d.csv'},
    {'query_url': 'https:///no-host'},
    {'query_url': 'x' * 3000},
    {'columns': ['ok', 7]},
    {'columns': ['x' * 2000]},
    {'table_index': -1},
    {'table_index': True},
    {'row_limit': -5},
    {'max_bytes': 0},
    {'deadline_sec': 0},
    {'deadline_sec': 'soon'},
    {'allowed_origin': 'https://example.com/path/deep'},
])
def test_worker_request_rejects_unknown_keys_and_malformed_states(mutation):
    payload = valid_request_json()
    payload.update(mutation)
    with pytest.raises(Exception) as caught:
        WorkerRequest.from_json(payload)
    assert caught.value.code == IPC_SCHEMA_INVALID


def test_worker_request_accepts_null_table_index_and_empty_origin():
    payload = valid_request_json()
    payload['table_index'] = None
    payload['allowed_origin'] = ''
    request = WorkerRequest.from_json(payload)
    assert request.table_index is None and request.allowed_origin == ''


def test_worker_request_rejects_non_object_and_oversized_columns():
    with pytest.raises(Exception) as caught:
        WorkerRequest.from_json([valid_request_json()])
    assert caught.value.code == IPC_SCHEMA_INVALID
    payload = valid_request_json()
    payload['columns'] = [f'c{index}' for index in range(MAX_COLUMNS + 1)]
    with pytest.raises(Exception) as caught:
        WorkerRequest.from_json(payload)
    assert caught.value.code == IPC_SCHEMA_INVALID


# ---------------------------------------------------------------------------
# Message validation: WorkerResult (no approval fields, ever)
# ---------------------------------------------------------------------------

def valid_result_json() -> dict:
    return WorkerResult(payload_csv='output.csv', payload_sidecar='sidecar.json',
                        dtypes=[('a', 'int64'), ('b', 'str')], rows=3,
                        warnings=['truncated']).to_json()


def test_worker_result_round_trips():
    result = WorkerResult.from_json(valid_result_json())
    assert result.rows == 3 and result.dtypes == (('a', 'int64'), ('b', 'str'))
    assert WorkerResult.from_json(result.to_json()) == result


@pytest.mark.parametrize('mutation', [
    {'extra': 1},
    {'payload_csv': '../escape.csv'},
    {'payload_csv': 'sub/dir/output.csv'},
    {'payload_csv': '.hidden.csv'},
    {'payload_csv': 5},
    {'dtypes': [['a']]},
    {'dtypes': [['a', 3]]},
    {'rows': -1},
    {'rows': True},
    {'warnings': [None]},
    {'error_code': 'lower case'},
    {'error_summary': 'x' * 5000},
])
def test_worker_result_rejects_malformed_states(mutation):
    payload = valid_result_json()
    payload.update(mutation)
    with pytest.raises(Exception) as caught:
        WorkerResult.from_json(payload)
    assert caught.value.code == IPC_SCHEMA_INVALID


@pytest.mark.parametrize('approval_key', ['approved', 'approval', 'approve_sample',
                                          'preview_receipt', 'approved_receipt'])
def test_worker_result_rejects_approval_like_fields_explicitly(approval_key):
    payload = valid_result_json()
    payload[approval_key] = {'anything': 'the worker must never grant approval'}
    with pytest.raises(Exception) as caught:
        WorkerResult.from_json(payload)
    assert caught.value.code == IPC_APPROVAL_FIELD_REJECTED


def test_worker_request_and_receipt_also_reject_approval_fields():
    for payload in (valid_request_json(), ):
        payload = dict(payload)
        payload['approval'] = True
        with pytest.raises(Exception) as caught:
            WorkerRequest.from_json(payload)
        assert caught.value.code == IPC_APPROVAL_FIELD_REJECTED
    receipt = PreviewReceipt.from_json(PreviewReceipt(
        preview_id=PreviewReceipt.new_id(), script_sha256='a' * 64,
        canonical_url='https://example.com/d.csv').to_json())
    tampered = receipt.to_json()
    tampered['receipt'] = 'double'
    with pytest.raises(Exception) as caught:
        PreviewReceipt.from_json(tampered)
    assert caught.value.code == IPC_APPROVAL_FIELD_REJECTED


# ---------------------------------------------------------------------------
# Message validation: PreviewReceipt
# ---------------------------------------------------------------------------

def valid_receipt() -> PreviewReceipt:
    return PreviewReceipt(preview_id=PreviewReceipt.new_id(), script_sha256='b' * 64,
                          canonical_url='https://example.com/d.csv', columns=['a'],
                          table_index=None, ttl=3600)


def test_preview_receipt_round_trips_with_defaults():
    receipt = valid_receipt()
    assert receipt.created_at and receipt.ttl == 3600
    restored = PreviewReceipt.from_json(receipt.to_json())
    assert restored == receipt
    assert not receipt.is_expired()


def test_preview_receipt_expiry_honours_ttl():
    receipt = valid_receipt()
    now = datetime.now(timezone.utc)
    assert not receipt.is_expired(now)
    assert receipt.is_expired(now + timedelta(seconds=3601))
    stale = PreviewReceipt.from_json({**receipt.to_json(),
                                      'created_at': (now - timedelta(hours=25)).isoformat(),
                                      'ttl': 3600})
    assert stale.is_expired(now)


@pytest.mark.parametrize('mutation', [
    {'preview_id': 'not-hex'},
    {'preview_id': 'A' * 32},
    {'script_sha256': 'zz'},
    {'canonical_url': 'gopher://x'},
    {'created_at': 'yesterday'},
    {'created_at': '2026-01-01T00:00:00'},
    {'ttl': 0},
    {'ttl': 400 * 86400},
    {'columns': 3},
])
def test_preview_receipt_rejects_malformed_states(mutation):
    payload = valid_receipt().to_json()
    payload.update(mutation)
    with pytest.raises(Exception) as caught:
        PreviewReceipt.from_json(payload)
    assert caught.value.code == IPC_SCHEMA_INVALID


# ---------------------------------------------------------------------------
# Frame codec: faithful round-trip of every supported value class
# ---------------------------------------------------------------------------

def test_round_trip_duplicate_headers_and_empty_vs_missing():
    frame = pd.concat([
        pd.Series(['x', '', None, 'has, comma', 'has "quote"', 'line\nbreak'], name='dup'),
        pd.Series([1, 2, 3, 4, 5, 6], name='dup'),
    ], axis=1)
    restored = round_trip(frame)
    assert list(restored.columns) == ['dup', 'dup']  # duplicates preserved verbatim
    pd.testing.assert_frame_equal(restored, frame, check_exact=True)
    empty_vs_missing = pd.DataFrame({'v': ['filled', '', None]})
    csv_bytes, _ = encode_frame(empty_vs_missing)
    restored = round_trip(empty_vs_missing)
    assert restored['v'].iloc[0] == 'filled'
    assert restored['v'].iloc[1] == ''          # empty string stays empty string
    assert pd.isna(restored['v'].iloc[2])       # missing stays missing


def test_round_trip_large_integers_and_numeric_widths():
    frame = pd.DataFrame({
        'big': [9223372036854775807, -9223372036854775808, 42],
        'small': np.array([1, 2, 3], dtype='int32'),
        'tiny': np.array([250, 251, 252], dtype='uint8'),
        'f64': [0.1, -0.0, 1e308],
        'f32': np.array([0.1, 1.5, 2.5], dtype='float32'),
        'nullable': pd.array([5, None, -7], dtype='Int64'),
    })
    pd.testing.assert_frame_equal(round_trip(frame), frame, check_exact=True)


def test_round_trip_dates_bools_and_string_dtypes():
    frame = pd.DataFrame({
        'when': pd.to_datetime(['2021-01-01 00:00:00.123456789', '1970-01-01', None],
                               format='ISO8601'),
        'zoned': pd.DatetimeIndex(pd.to_datetime(
            ['2021-06-01T10:00:00+02:00', '2021-06-01T10:00:00Z', None],
            format='ISO8601', utc=True)).tz_convert('Europe/Berlin'),
        'flag': [True, False, True],
        'maybe': pd.array([True, None, False], dtype='boolean'),
        'text': pd.array(['k', None, ''], dtype='string'),
        'plain': pd.Series(['a', None, ''], dtype=object),
    })
    pd.testing.assert_frame_equal(round_trip(frame), frame, check_exact=True)


def test_round_trip_empty_frames_and_sentinel_like_cells():
    empty = pd.DataFrame({'a': pd.Series(dtype='int64'), 'b': pd.Series(dtype=object)})
    pd.testing.assert_frame_equal(round_trip(empty), empty, check_exact=True)
    hostile = pd.DataFrame({'v': ['~DFP~MISSING~', '~DFP~ABSENT~x~', 'ok', None]})
    pd.testing.assert_frame_equal(round_trip(hostile), hostile, check_exact=True)


def test_read_csv_shaped_frames_survive_the_worker_codec():
    frame = pd.read_csv(__import__('io').BytesIO(b'a,b\n001,x\n2.5,y\n'))
    pd.testing.assert_frame_equal(round_trip(frame), frame, check_exact=True)


@pytest.mark.parametrize('frame', [
    pd.DataFrame({'bad': pd.Series([[1], [2]])}),
    pd.DataFrame({'bad': pd.Series([{'a': 1}])}),
    pd.DataFrame({'bad': pd.Series([1, 'a'])}),
    pd.DataFrame({'bad': pd.Series([pd.Timestamp('2021-01-01')], dtype=object)}),
    pd.DataFrame({'bad': pd.Series([pd.Timedelta(seconds=1)])}),
    pd.DataFrame({'bad': pd.Series(['a', 'b'], dtype='category')}),
])
def test_unroundtrippable_value_classes_fail_explicitly(frame):
    with pytest.raises(Exception) as caught:
        encode_frame(frame)
    assert caught.value.code == IPC_UNREPRESENTABLE_VALUES


def test_uint64_beyond_int64_fails_explicitly():
    frame = pd.DataFrame({'u': np.array([2 ** 63, 2 ** 63 + 1], dtype='uint64')})
    with pytest.raises(Exception) as caught:
        encode_frame(frame)
    assert caught.value.code == IPC_UNREPRESENTABLE_VALUES


def test_non_scalar_column_names_fail_explicitly():
    frame = pd.concat([pd.Series(['a']), pd.Series(['b'])], axis=1)
    frame.columns = [('t', 'u'), 'plain']
    with pytest.raises(Exception) as caught:
        encode_frame(frame)
    assert caught.value.code == IPC_UNREPRESENTABLE_VALUES


def test_decode_rejects_row_count_mismatch_and_bad_sidecar():
    frame = pd.DataFrame({'a': [1, 2, 3]})
    csv_bytes, sidecar_bytes = encode_frame(frame)
    tampered_sidecar = json.loads(sidecar_bytes)
    tampered_sidecar['rows'] = 9
    with pytest.raises(Exception) as caught:
        decode_frame(csv_bytes, json.dumps(tampered_sidecar).encode())
    assert caught.value.code == IPC_PAYLOAD_CORRUPT
    with pytest.raises(Exception) as caught:
        decode_frame(csv_bytes, b'not json')
    assert caught.value.code == IPC_SCHEMA_INVALID
    bogus = json.loads(sidecar_bytes)
    bogus['columns'][0]['kind'] = 'quantum'
    with pytest.raises(Exception) as caught:
        decode_frame(csv_bytes, json.dumps(bogus).encode())
    assert caught.value.code == IPC_SCHEMA_INVALID


def test_decode_rejects_boolean_column_with_foreign_token():
    csv_bytes = b'True\nmaybe\n'
    sidecar = json.dumps({'format': 'dfp-frame-v1',
                          'columns': [{'name': 'flag', 'kind': 'bool'}],
                          'na_rep': '~DFP~MISSING~x~', 'rows': 2}).encode()
    with pytest.raises(Exception) as caught:
        decode_frame(csv_bytes, sidecar)
    assert caught.value.code == IPC_PAYLOAD_CORRUPT


def test_sha256_bytes_matches_hashlib():
    import hashlib
    assert sha256_bytes(b'payload') == hashlib.sha256(b'payload').hexdigest()
