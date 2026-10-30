"""Worker IPC and preview-receipt data protocol (Phase 1).

Strict, frozen message types exchanged between the host process and the
isolated extractor worker subprocess, plus the CSV payload codec used to move
extracted frames across the process boundary. Results are NEVER transferred as
pickles: the worker writes ``output.csv`` plus a JSON sidecar describing the
exact schema, and the parent re-parses them with :func:`decode_frame`.

Documented error codes (all raised as ``DataFetchError``):

- ``IPC_SCHEMA_INVALID``          — JSON structure violation: unknown key,
                                    wrong type, oversized string, bad enum,
                                    malformed URL/hash/identifier.
- ``IPC_APPROVAL_FIELD_REJECTED`` — a worker-supplied message carries
                                    approval-like fields. The worker is
                                    untrusted: approval state never flows from
                                    the worker to the host.
- ``IPC_UNREPRESENTABLE_VALUES``  — a value class cannot round-trip through
                                    the CSV+sidecar representation. The
                                    operation fails explicitly instead of
                                    silently degrading (e.g. object columns
                                    holding non-string scalars, lists, dicts,
                                    timestamps-as-objects, ``uint64`` values
                                    beyond int64, non-scalar column names,
                                    categorical/interval/period/sparse dtypes).
- ``IPC_PAYLOAD_CORRUPT``         — payload and sidecar disagree at decode
                                    time (row/column count mismatch, bad
                                    boolean token, unparseable numeric or
                                    datetime text).

IPC fidelity contract: the CSV+sidecar representation faithfully round-trips
every supported value class — duplicate column headers, the empty-string vs
missing distinction, large int64 integers, float32/float64 (bit-exact),
booleans, naive and tz-aware ``datetime64`` values, numpy integer widths, and
pandas nullable dtypes (``Int64``, ``Float64``, ``boolean``, ``string``).
Values are written to a headerless CSV (headers travel in the sidecar, so
duplicate names survive verbatim) with every missing value replaced by a
per-encode sentinel that is verified not to collide with any string cell; the
decoder reads raw text (``na_filter=False``) and rebuilds exact dtypes from the
sidecar. Data frames always decode with a fresh positional ``RangeIndex`` —
the payload carries values and schema, never a custom index; this is a
documented property of the transport, not silent degradation.

Honest scope: this module is data plumbing. It does not sandbox the worker and
does not defend against same-user tampering with local files.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import numpy as np
import pandas as pd

from scripts.errors import DataFetchError

__all__ = [
    'WorkerRequest', 'WorkerResult', 'PreviewReceipt',
    'encode_frame', 'decode_frame', 'sha256_bytes',
    'IPC_SCHEMA_INVALID', 'IPC_APPROVAL_FIELD_REJECTED',
    'IPC_UNREPRESENTABLE_VALUES', 'IPC_PAYLOAD_CORRUPT',
    'DEFAULT_PREVIEW_TTL_SECONDS', 'MAX_PAYLOAD_CHARS', 'MAX_COLUMNS',
]

IPC_SCHEMA_INVALID = 'IPC_SCHEMA_INVALID'
IPC_APPROVAL_FIELD_REJECTED = 'IPC_APPROVAL_FIELD_REJECTED'
IPC_UNREPRESENTABLE_VALUES = 'IPC_UNREPRESENTABLE_VALUES'
IPC_PAYLOAD_CORRUPT = 'IPC_PAYLOAD_CORRUPT'

DEFAULT_PREVIEW_TTL_SECONDS = 24 * 60 * 60

#: Hard ceiling for any single string field inside a protocol message.
MAX_PAYLOAD_CHARS = 16384
#: Hard ceiling for URL-ish fields.
MAX_URL_CHARS = 2048
#: Hard ceiling for the number of columns in any message or frame payload.
MAX_COLUMNS = 4096
_MAX_WARNINGS = 64
_MAX_WARNING_CHARS = 2048
_MAX_SUMMARY_CHARS = 4096
_MAX_NAME_CHARS = 1024
_SHA256_RE = re.compile(r'[a-f0-9]{64}')
_PREVIEW_ID_RE = re.compile(r'[a-f0-9]{32}')
_ERROR_CODE_RE = re.compile(r'[A-Z0-9_]{1,64}')
# Approval state must never flow from the untrusted worker to the host.
_APPROVAL_LIKE = ('approv', 'receipt')
_FRAME_FORMAT = 'dfp-frame-v1'
_KINDS = frozenset({
    'int', 'nullable_int', 'float', 'nullable_float', 'bool', 'nullable_bool',
    'datetime', 'datetime_tz', 'string', 'string_dtype',
})
_CANDIDATE_SENTINELS = ('~DFP~MISSING~{}~', '~DFP~ABSENT~{}~', '~DFP~NA~{}~')


class _ProtocolError(DataFetchError):
    """Base for coded protocol failures (still a ValueError for callers)."""


def _reject(message: str, code: str = IPC_SCHEMA_INVALID) -> DataFetchError:
    return _ProtocolError(message, code=code)


def _no_unknown_keys(data: Any, allowed: frozenset[str], label: str) -> dict:
    if not isinstance(data, dict):
        raise _reject(f'{label}: expected a JSON object.')
    keys = set(data)
    approval_hits = sorted(k for k in keys if any(m in k.lower() for m in _APPROVAL_LIKE))
    if approval_hits:
        raise _reject(
            f'{label}: approval-like fields are never accepted from the worker '
            f'({", ".join(approval_hits)}).', code=IPC_APPROVAL_FIELD_REJECTED)
    unknown = sorted(keys - allowed)
    if unknown:
        raise _reject(f'{label}: unknown keys {unknown}.')
    return data


def _strict_str(data: dict, key: str, label: str, max_chars: int = MAX_PAYLOAD_CHARS,
                allow_empty: bool = False) -> str:
    value = data.get(key)
    if not isinstance(value, str) or (not value and not allow_empty):
        raise _reject(f'{label}.{key}: expected a non-empty string.')
    if len(value) > max_chars:
        raise _reject(f'{label}.{key}: string exceeds {max_chars} characters.')
    return value


def _strict_int(data: dict, key: str, label: str, minimum: int, maximum: int) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _reject(f'{label}.{key}: expected an integer.')
    if not minimum <= value <= maximum:
        raise _reject(f'{label}.{key}: value {value} outside [{minimum}, {maximum}].')
    return value


def _strict_url(data: dict, key: str, label: str) -> str:
    value = _strict_str(data, key, label, max_chars=MAX_URL_CHARS)
    parsed = urlparse(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise _reject(f'{label}.{key}: must be an http(s) URL with a hostname.')
    return value


def _strict_columns(data: dict, key: str, label: str) -> tuple[str, ...]:
    value = data.get(key)
    if not isinstance(value, list) or len(value) > MAX_COLUMNS:
        raise _reject(f'{label}.{key}: expected a list of at most {MAX_COLUMNS} names.')
    for item in value:
        if not isinstance(item, str) or len(item) > _MAX_NAME_CHARS:
            raise _reject(f'{label}.{key}: column names must be strings of at most '
                          f'{_MAX_NAME_CHARS} characters.')
    return tuple(value)


def _strict_table_index(data: dict, key: str, label: str) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _reject(f'{label}.{key}: expected null or a non-negative integer.')
    return value


@dataclass(frozen=True)
class WorkerRequest:
    """Host -> worker job description. Carries no secrets and no approval state."""

    script_sha256: str
    query_url: str
    columns: tuple[str, ...] = ()
    table_index: int | None = None
    row_limit: int = 0
    max_bytes: int = 50 * 1024 * 1024
    deadline_sec: float = 120.0
    allowed_origin: str = ''

    def __post_init__(self) -> None:
        object.__setattr__(self, 'columns', tuple(self.columns))

    def to_json(self) -> dict:
        return {
            'script_sha256': self.script_sha256,
            'query_url': self.query_url,
            'columns': list(self.columns),
            'table_index': self.table_index,
            'row_limit': self.row_limit,
            'max_bytes': self.max_bytes,
            'deadline_sec': self.deadline_sec,
            'allowed_origin': self.allowed_origin,
        }

    @classmethod
    def from_json(cls, data: Any) -> 'WorkerRequest':
        allowed = frozenset(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        data = _no_unknown_keys(data, allowed, 'WorkerRequest')
        sha = _strict_str(data, 'script_sha256', 'WorkerRequest', max_chars=64)
        if not _SHA256_RE.fullmatch(sha):
            raise _reject('WorkerRequest.script_sha256: expected a lowercase hex sha256.')
        query_url = _strict_url(data, 'query_url', 'WorkerRequest')
        columns = _strict_columns(data, 'columns', 'WorkerRequest')
        table_index = _strict_table_index(data, 'table_index', 'WorkerRequest')
        row_limit = _strict_int(data, 'row_limit', 'WorkerRequest', 0, 2 ** 40)
        max_bytes = _strict_int(data, 'max_bytes', 'WorkerRequest', 1, 2 ** 40)
        deadline = data.get('deadline_sec')
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) \
                or not 0 < float(deadline) <= 86400:
            raise _reject('WorkerRequest.deadline_sec: expected a positive number of seconds.')
        allowed_origin = _strict_str(data, 'allowed_origin', 'WorkerRequest',
                                     max_chars=MAX_URL_CHARS, allow_empty=True)
        if allowed_origin:
            parsed = urlparse(allowed_origin)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname \
                    or parsed.path not in ('', '/'):
                raise _reject('WorkerRequest.allowed_origin: expected scheme://host[:port].')
        return cls(script_sha256=sha, query_url=query_url, columns=columns,
                   table_index=table_index, row_limit=row_limit, max_bytes=max_bytes,
                   deadline_sec=float(deadline), allowed_origin=allowed_origin)


@dataclass(frozen=True)
class WorkerResult:
    """Worker -> host outcome. Payload locations and diagnostics only.

    NOTE: ``payload_sidecar`` is part of the payload-location field family the
    plan describes as "csv payload path"; the sidecar carries the frame schema
    needed by :func:`decode_frame`. There are deliberately no approval fields:
    approval state can only originate on the host side of the boundary.
    """

    payload_csv: str = ''
    payload_sidecar: str = ''
    dtypes: tuple[tuple[str, str], ...] = ()
    rows: int = 0
    warnings: tuple[str, ...] = ()
    error_code: str | None = None
    error_summary: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, 'dtypes', tuple((n, d) for n, d in self.dtypes))
        object.__setattr__(self, 'warnings', tuple(self.warnings))

    def to_json(self) -> dict:
        return {
            'payload_csv': self.payload_csv,
            'payload_sidecar': self.payload_sidecar,
            'dtypes': [[name, dtype] for name, dtype in self.dtypes],
            'rows': self.rows,
            'warnings': list(self.warnings),
            'error_code': self.error_code,
            'error_summary': self.error_summary,
        }

    @classmethod
    def from_json(cls, data: Any) -> 'WorkerResult':
        allowed = frozenset(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        data = _no_unknown_keys(data, allowed, 'WorkerResult')
        payload_csv = _strict_str(data, 'payload_csv', 'WorkerResult', allow_empty=True)
        payload_sidecar = _strict_str(data, 'payload_sidecar', 'WorkerResult', allow_empty=True)
        for key, value in (('payload_csv', payload_csv), ('payload_sidecar', payload_sidecar)):
            if value and (value != value.replace('\\', '/').split('/')[-1]
                          or value in ('.', '..') or value.startswith('.')):
                raise _reject(f'WorkerResult.{key}: payload paths must be plain file names.')
        raw_dtypes = data.get('dtypes')
        if not isinstance(raw_dtypes, list) or len(raw_dtypes) > MAX_COLUMNS:
            raise _reject('WorkerResult.dtypes: expected a list of at most '
                          f'{MAX_COLUMNS} entries.')
        dtypes = []
        for entry in raw_dtypes:
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                raise _reject('WorkerResult.dtypes: each entry must be [name, dtype].')
            name, dtype = entry
            if not isinstance(name, str) or len(name) > _MAX_NAME_CHARS:
                raise _reject('WorkerResult.dtypes: invalid column name.')
            if not isinstance(dtype, str) or len(dtype) > 128:
                raise _reject('WorkerResult.dtypes: invalid dtype string.')
            dtypes.append((name, dtype))
        rows = _strict_int(data, 'rows', 'WorkerResult', 0, 2 ** 40)
        raw_warnings = data.get('warnings')
        if not isinstance(raw_warnings, list) or len(raw_warnings) > _MAX_WARNINGS:
            raise _reject(f'WorkerResult.warnings: expected a list of at most '
                          f'{_MAX_WARNINGS} strings.')
        for warning in raw_warnings:
            if not isinstance(warning, str) or len(warning) > _MAX_WARNING_CHARS:
                raise _reject('WorkerResult.warnings: strings of at most '
                              f'{_MAX_WARNING_CHARS} characters required.')
        error_code = data.get('error_code')
        if error_code is not None and (
                not isinstance(error_code, str) or not _ERROR_CODE_RE.fullmatch(error_code)):
            raise _reject('WorkerResult.error_code: expected an UPPER_SNAKE token.')
        error_summary = data.get('error_summary')
        if error_summary is not None and (
                not isinstance(error_summary, str) or len(error_summary) > _MAX_SUMMARY_CHARS):
            raise _reject('WorkerResult.error_summary: string too long.')
        return cls(payload_csv=payload_csv, payload_sidecar=payload_sidecar,
                   dtypes=tuple(dtypes), rows=rows, warnings=tuple(raw_warnings),
                   error_code=error_code, error_summary=error_summary)


@dataclass(frozen=True)
class PreviewReceipt:
    """Human-approval token binding one bounded preview to one full fetch.

    Created when a bounded sample has been produced for a specific extractor
    byte stream, canonical URL, column set and table index. A later full fetch
    must present a receipt whose fields still match the request, else the
    fetch fails with ``APPROVAL_MISMATCH``.
    """

    preview_id: str
    script_sha256: str
    canonical_url: str
    columns: tuple[str, ...] = ()
    table_index: int | None = None
    created_at: str = ''
    ttl: int = DEFAULT_PREVIEW_TTL_SECONDS

    def __post_init__(self) -> None:
        if not self.created_at:
            object.__setattr__(self, 'created_at',
                               datetime.now(timezone.utc).isoformat(timespec='seconds'))
        object.__setattr__(self, 'columns', tuple(self.columns))

    def to_json(self) -> dict:
        return {
            'preview_id': self.preview_id,
            'script_sha256': self.script_sha256,
            'canonical_url': self.canonical_url,
            'columns': list(self.columns),
            'table_index': self.table_index,
            'created_at': self.created_at,
            'ttl': self.ttl,
        }

    @classmethod
    def from_json(cls, data: Any) -> 'PreviewReceipt':
        allowed = frozenset(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        data = _no_unknown_keys(data, allowed, 'PreviewReceipt')
        preview_id = _strict_str(data, 'preview_id', 'PreviewReceipt', max_chars=32)
        if not _PREVIEW_ID_RE.fullmatch(preview_id):
            raise _reject('PreviewReceipt.preview_id: expected a 32-char hex token.')
        sha = _strict_str(data, 'script_sha256', 'PreviewReceipt', max_chars=64)
        if not _SHA256_RE.fullmatch(sha):
            raise _reject('PreviewReceipt.script_sha256: expected a lowercase hex sha256.')
        canonical_url = _strict_url(data, 'canonical_url', 'PreviewReceipt')
        columns = _strict_columns(data, 'columns', 'PreviewReceipt')
        table_index = _strict_table_index(data, 'table_index', 'PreviewReceipt')
        created_at = _strict_str(data, 'created_at', 'PreviewReceipt', max_chars=64)
        try:
            stamp = datetime.fromisoformat(created_at)
        except ValueError as exc:
            raise _reject('PreviewReceipt.created_at: expected an ISO-8601 timestamp.') from exc
        if stamp.tzinfo is None:
            raise _reject('PreviewReceipt.created_at: must carry a UTC offset.')
        ttl = _strict_int(data, 'ttl', 'PreviewReceipt', 1, 30 * 86400)
        return cls(preview_id=preview_id, script_sha256=sha, canonical_url=canonical_url,
                   columns=columns, table_index=table_index, created_at=created_at, ttl=ttl)

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex

    def parsed_created_at(self) -> datetime:
        return datetime.fromisoformat(self.created_at)

    def is_expired(self, now: datetime | None = None) -> bool:
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment >= self.parsed_created_at() + timedelta(seconds=self.ttl)


# ---------------------------------------------------------------------------
# Frame payload codec (headerless CSV + JSON schema sidecar).
# ---------------------------------------------------------------------------

def _column_label(index: int) -> str:
    return f'column {index}'


def _json_scalar_name(name: Any, index: int) -> Any:
    if isinstance(name, np.integer):
        name = int(name)
    elif isinstance(name, np.floating):
        name = float(name)
    elif isinstance(name, np.bool_):
        name = bool(name)
    if isinstance(name, (str, int, float, bool)) or name is None:
        if isinstance(name, str) and len(name) > _MAX_NAME_CHARS:
            raise _ProtocolError(f'{_column_label(index)}: name exceeds '
                                 f'{_MAX_NAME_CHARS} characters.', code=IPC_UNREPRESENTABLE_VALUES)
        if isinstance(name, (int, float)) and not isinstance(name, bool) \
                and not np.isfinite(float(name)):
            raise _ProtocolError(f'{_column_label(index)}: non-finite numeric names cannot '
                                 'round-trip through JSON.', code=IPC_UNREPRESENTABLE_VALUES)
        return name
    raise _ProtocolError(
        f'{_column_label(index)}: column name of type {type(name).__name__} cannot round-trip '
        'through the JSON sidecar.', code=IPC_UNREPRESENTABLE_VALUES)


def _is_missing(value: Any) -> bool:
    return value is None or value is pd.NA or value is pd.NaT or (
        isinstance(value, float) and value != value)


def _string_column_kind(series: pd.Series, label: str) -> str:
    for value in series.tolist():
        if _is_missing(value):
            continue
        if type(value) is not str:
            raise _ProtocolError(
                f'{label}: object column holds {type(value).__name__} values; only strings '
                'and missing values round-trip through the CSV payload.',
                code=IPC_UNREPRESENTABLE_VALUES)
    return 'string'


def _column_descriptor(series: pd.Series, name: Any, index: int) -> dict:
    label = _column_label(index)
    descriptor: dict[str, Any] = {'name': _json_scalar_name(name, index)}
    dtype = series.dtype
    if isinstance(dtype, pd.CategoricalDtype) or isinstance(dtype, pd.IntervalDtype) \
            or isinstance(dtype, pd.PeriodDtype) or isinstance(dtype, pd.SparseDtype):
        raise _ProtocolError(f'{label}: {dtype} columns cannot round-trip through the CSV '
                             'payload.', code=IPC_UNREPRESENTABLE_VALUES)
    if pd.api.types.is_bool_dtype(dtype):
        descriptor['kind'] = 'bool' if isinstance(dtype, np.dtype) else 'nullable_bool'
    elif isinstance(dtype, pd.DatetimeTZDtype):
        descriptor['kind'] = 'datetime_tz'
        descriptor['np_dtype'] = str(dtype)
        descriptor['tz'] = str(dtype.tz)
    elif pd.api.types.is_datetime64_any_dtype(dtype):
        descriptor['kind'] = 'datetime'
        descriptor['np_dtype'] = str(dtype)
    elif isinstance(dtype, pd.api.extensions.ExtensionDtype):
        pandas_token = str(dtype)
        if pandas_token == 'Int64':
            descriptor['kind'] = 'nullable_int'
        elif pandas_token == 'Float64':
            descriptor['kind'] = 'nullable_float'
        elif pandas_token in ('string', 'str'):
            # pandas >= 3 defaults text columns to the 'str' extension dtype;
            # older installs report 'string' or plain object. All round-trip.
            descriptor['kind'] = 'string_dtype'
            descriptor['pandas_dtype'] = pandas_token
        else:
            raise _ProtocolError(f'{label}: {pandas_token} columns cannot round-trip through '
                                 'the CSV payload.', code=IPC_UNREPRESENTABLE_VALUES)
    elif pd.api.types.is_integer_dtype(dtype):
        descriptor['kind'] = 'int'
        descriptor['np_dtype'] = str(dtype)
        if str(dtype) == 'uint64' and len(series) and series.max() > np.iinfo(np.int64).max:
            raise _ProtocolError(f'{label}: uint64 values beyond int64 cannot round-trip '
                                 'through the CSV payload.', code=IPC_UNREPRESENTABLE_VALUES)
    elif pd.api.types.is_float_dtype(dtype):
        descriptor['kind'] = 'float'
        descriptor['np_dtype'] = str(dtype)
    elif dtype == object:
        descriptor['kind'] = _string_column_kind(series, label)
    else:
        raise _ProtocolError(f'{label}: {dtype} columns cannot round-trip through the CSV '
                             'payload.', code=IPC_UNREPRESENTABLE_VALUES)
    return descriptor


def _choose_sentinel(frame: pd.DataFrame, descriptors: list[dict]) -> str:
    string_positions = [position for position, descriptor in enumerate(descriptors)
                        if descriptor['kind'] in ('string', 'string_dtype')]
    for template in _CANDIDATE_SENTINELS:
        for _ in range(8):
            candidate = template.format(uuid.uuid4().hex[:12])
            if not any(len(frame) and frame.iloc[:, position].eq(candidate).any()
                       for position in string_positions):
                return candidate
    raise _ProtocolError('no collision-free missing-value sentinel exists for this frame.',
                         code=IPC_UNREPRESENTABLE_VALUES)


def encode_frame(frame: pd.DataFrame) -> tuple[bytes, bytes]:
    """Encode a DataFrame as ``(csv_bytes, sidecar_bytes)`` with full fidelity.

    Raises ``DataFetchError(code='IPC_UNREPRESENTABLE_VALUES')`` when a value
    class cannot round-trip; silent degradation never happens.
    """
    if not isinstance(frame, pd.DataFrame):
        raise _ProtocolError('encode_frame expects a pandas DataFrame.',
                             code=IPC_UNREPRESENTABLE_VALUES)
    if len(frame.columns) > MAX_COLUMNS:
        raise _ProtocolError(f'frames wider than {MAX_COLUMNS} columns are not supported.',
                             code=IPC_UNREPRESENTABLE_VALUES)
    descriptors = [_column_descriptor(frame.iloc[:, index], name, index)
                   for index, name in enumerate(frame.columns)]
    sentinel = _choose_sentinel(frame, descriptors)
    sidecar = {
        'format': _FRAME_FORMAT,
        'columns': descriptors,
        'na_rep': sentinel,
        'rows': int(len(frame)),
    }
    buffer = io.StringIO()
    frame.to_csv(buffer, header=False, index=False, na_rep=sentinel, lineterminator='\n')
    return buffer.getvalue().encode('utf-8'), json.dumps(sidecar, ensure_ascii=False).encode('utf-8')


def _parse_sidecar(sidecar_bytes: bytes) -> dict:
    try:
        data = json.loads(sidecar_bytes.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _reject(f'sidecar is not valid JSON: {exc}') from exc
    if not isinstance(data, dict):
        raise _reject('sidecar: expected a JSON object.')
    unknown = set(data) - {'format', 'columns', 'na_rep', 'rows'}
    if unknown or data.get('format') != _FRAME_FORMAT:
        raise _reject(f'sidecar: unsupported format or unknown keys {sorted(unknown)}.')
    sentinel = data.get('na_rep')
    if not isinstance(sentinel, str) or not 1 <= len(sentinel) <= 64:
        raise _reject('sidecar.na_rep: expected a short sentinel string.')
    rows = data.get('rows')
    if isinstance(rows, bool) or not isinstance(rows, int) or rows < 0:
        raise _reject('sidecar.rows: expected a non-negative integer.')
    columns = data.get('columns')
    if not isinstance(columns, list) or len(columns) > MAX_COLUMNS:
        raise _reject(f'sidecar.columns: expected a list of at most {MAX_COLUMNS} entries.')
    for entry in columns:
        if not isinstance(entry, dict):
            raise _reject('sidecar.columns: expected descriptor objects.')
        unknown = set(entry) - {'name', 'kind', 'np_dtype', 'tz', 'pandas_dtype'}
        if unknown:
            raise _reject(f'sidecar.columns: unknown keys {sorted(unknown)}.')
        if entry.get('kind') not in _KINDS:
            raise _reject(f"sidecar.columns: unsupported kind {entry.get('kind')!r}.")
        name = entry.get('name')
        if not isinstance(name, (str, int, float, bool)) and name is not None:
            raise _reject('sidecar.columns: names must be JSON scalars.')
        if isinstance(name, str) and len(name) > _MAX_NAME_CHARS:
            raise _reject('sidecar.columns: name too long.')
        if 'pandas_dtype' in entry and (
                not isinstance(entry['pandas_dtype'], str)
                or len(entry['pandas_dtype']) > 64):
            raise _reject('sidecar.columns: invalid pandas_dtype token.')
        if entry['kind'] in ('int', 'float', 'datetime') \
                and not isinstance(entry.get('np_dtype'), str):
            raise _reject('sidecar.columns: numeric kinds require np_dtype.')
        if entry['kind'] == 'datetime_tz' and not isinstance(entry.get('tz'), str):
            raise _reject('sidecar.columns: datetime_tz requires tz.')
    return data


def _empty_dtype(entry: dict) -> str:
    kind = entry['kind']
    if kind in ('int', 'float', 'datetime'):
        return entry['np_dtype']
    if kind == 'datetime_tz':
        return entry['np_dtype']
    if kind == 'nullable_int':
        return 'Int64'
    if kind == 'nullable_float':
        return 'Float64'
    if kind == 'bool':
        return 'bool'
    if kind == 'nullable_bool':
        return 'boolean'
    if kind == 'string_dtype':
        return entry.get('pandas_dtype', 'string')
    return 'object'


def _convert_column(series: pd.Series, entry: dict, sentinel: str) -> pd.Series:
    kind = entry['kind']
    if kind != 'bool':
        series = series.mask(series == sentinel)
    try:
        if kind == 'string':
            converted = series.astype(object)
        elif kind == 'string_dtype':
            converted = series.astype(entry.get('pandas_dtype', 'string'))
        elif kind == 'int':
            converted = series.astype(entry['np_dtype'])
        elif kind == 'nullable_int':
            converted = series.astype('Int64')
        elif kind == 'float':
            converted = series.astype(entry['np_dtype'])
        elif kind == 'nullable_float':
            converted = series.astype('Float64')
        elif kind == 'bool':
            mapped = series.map({'True': True, 'False': False})
            if mapped.isna().any():
                raise ValueError('non-boolean token in boolean column')
            converted = mapped.astype(bool)
        elif kind == 'nullable_bool':
            converted = series.map({'True': True, 'False': False}).astype('boolean')
        elif kind == 'datetime':
            converted = pd.to_datetime(series, format='ISO8601', errors='raise')
            if entry['np_dtype'] != 'datetime64[ns]':
                converted = converted.astype(entry['np_dtype'])
        elif kind == 'datetime_tz':
            converted = pd.to_datetime(series, format='ISO8601', utc=True, errors='raise')
            converted = converted.dt.tz_convert(entry['tz']).astype(entry['np_dtype'])
        else:  # pragma: no cover - kind enum already validated
            raise _reject(f'unsupported kind {kind}')
    except (ValueError, TypeError) as exc:
        raise _ProtocolError(
            f"payload column {entry['name']!r} does not match its declared schema "
            f'({kind}): {exc}', code=IPC_PAYLOAD_CORRUPT) from exc
    return converted


def decode_frame(csv_bytes: bytes, sidecar_bytes: bytes) -> pd.DataFrame:
    """Decode ``(csv_bytes, sidecar_bytes)`` produced by :func:`encode_frame`.

    Raises ``DataFetchError(code='IPC_PAYLOAD_CORRUPT')`` when payload and
    sidecar disagree and ``code='IPC_SCHEMA_INVALID'`` for malformed sidecars.
    """
    sidecar = _parse_sidecar(sidecar_bytes)
    columns = sidecar['columns']
    sentinel = sidecar['na_rep']
    if sidecar['rows'] == 0:
        if csv_bytes.strip():
            raise _reject('payload: expected an empty payload for a 0-row frame.',
                          code=IPC_PAYLOAD_CORRUPT)
        if not columns:
            return pd.DataFrame(index=pd.RangeIndex(0))
        parts = [pd.Series(name=f'c{position}', dtype=_empty_dtype(entry))
                 for position, entry in enumerate(columns)]
        frame = pd.concat(parts, axis=1)
        frame.columns = [entry['name'] for entry in columns]
        frame.index = pd.RangeIndex(0)
        return frame
    try:
        raw = pd.read_csv(io.BytesIO(csv_bytes), header=None,
                          names=[f'c{position}' for position in range(len(columns))],
                          dtype=str, na_filter=False, keep_default_na=False, engine='c')
    except (pd.errors.ParserError, pd.errors.EmptyDataError, ValueError) as exc:
        raise _reject(f'payload is not parseable CSV: {exc}', code=IPC_PAYLOAD_CORRUPT) from exc
    if len(raw) != sidecar['rows']:
        raise _reject(f"payload: sidecar declares {sidecar['rows']} rows but payload has "
                      f'{len(raw)}.', code=IPC_PAYLOAD_CORRUPT)
    if len(raw.columns) != len(columns):
        raise _reject('payload: column count mismatch with sidecar.', code=IPC_PAYLOAD_CORRUPT)
    parts = [_convert_column(raw.iloc[:, position], entry, sentinel)
             for position, entry in enumerate(columns)]
    frame = pd.concat(parts, axis=1)
    frame.columns = [entry['name'] for entry in columns]
    frame.index = pd.RangeIndex(len(frame))
    return frame


def sha256_bytes(payload: bytes) -> str:
    """Lowercase hex SHA-256 of a byte string (shared helper for both sides)."""
    return hashlib.sha256(payload).hexdigest()
