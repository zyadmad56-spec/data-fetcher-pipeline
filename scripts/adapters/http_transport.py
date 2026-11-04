"""Parent-side supervisor for fixed-function HTTP operations.

Contract (CONTRACTS.md "Core HTTP supervision"): a repository HTTP operation
that explicitly requests ``deadline_sec`` runs inside one disposable worker
process whose total wall-clock — interpreter start, DNS, connect, headers,
body, retries and shutdown — is supervised here, before and outside the
worker.  The supervisor starts its clock before spawning, hands the job to
the child through a bounded stdin pipe (never argv or a persisted job file),
creates the task-owned spool file itself, caps the child's captured output,
kills and reaps the child on deadline, and removes the spool on every failure
path.

The default worker (``scripts/adapters/http_worker.py``) is fixed trusted
code: it spawns nothing, imports no user code, performs the public-only DNS
and redirect policy via :mod:`scripts.net_guard` inside the child, and writes
the body to exactly the spool path the parent handed it.  Constructor
injection of ``worker_path`` is a process/packaging boundary for fixtures,
like ``SubprocessExtractorRunner``; it is not a way to run extractor code.

Honest limits: the supervisor bounds the child it owns.  SDK-based providers
and the standalone generated-template scripts that keep cooperative timeouts
are outside this boundary and say so in their own documentation.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from scripts.errors import DataFetchError
from scripts.adapters.script_runner import _CREDENTIAL_PATTERN, _drain, _kill_process

DEFAULT_WORKER_PATH = Path(__file__).resolve().parent / 'http_worker.py'

#: Hard caps for the pipe frames; the child enforces the same job limit.
_JOB_MAX_BYTES = 64 * 1024
_RESULT_MAX_BYTES = 64 * 1024
_SPOOL_PREFIX = 'dfp-http-'

#: Codes the trusted child may report; anything else maps to NETWORK.
_CHILD_ERROR_CODES = {'BLOCKED_URL', 'SIZE_LIMIT', 'NETWORK', 'TIMEOUT', 'PROVIDER_ERROR', 'RATE_LIMITED'}

_CHILD_MESSAGES = {
    'BLOCKED_URL': 'The target URL was refused by the local safety policy.',
    'SIZE_LIMIT': 'The response body exceeds the configured size cap.',
    'NETWORK': 'The source could not be reached over the network.',
    'TIMEOUT': 'The supervised HTTP operation exceeded its time budget.',
    'PROVIDER_ERROR': 'The provider returned an error while serving the request.',
    'RATE_LIMITED': 'The provider rate limit was reached.',
}


@dataclass
class HTTPRequest:
    """One supervised HTTP operation (frozen public contract)."""

    url: str
    deadline_sec: float
    public_only: bool = True
    headers: dict = field(default_factory=dict)
    timeout: float = 30
    stream: bool = False
    allow_redirects: bool = True
    max_body_bytes: Optional[int] = None
    #: Bounded prefix intent: read at most this many bytes and close the
    #: upstream promptly.  Distinct from full-body ``max_body_bytes``; a huge
    #: declared Content-Length is not rejected in prefix mode.
    prefix_bytes: Optional[int] = None
    max_retries: int = 3
    backoff_base: float = 2


def _remove_quiet(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _child_env() -> dict[str, str]:
    """Child environment: credential-shaped variables stripped.

    TMP/TEMP/TMPDIR stay inherited so the child shares the parent's task-owned
    temporary root; the spool path itself is created by the parent and handed
    through the job frame.
    """
    child = {name: value for name, value in os.environ.items()
             if not _CREDENTIAL_PATTERN.search(name)}
    child['PYTHONDONTWRITEBYTECODE'] = '1'
    child['PYTHONIOENCODING'] = 'utf-8'
    return child


class _SpoolReader:
    """File-backed bounded body reader that owns and deletes its spool file.

    Implements the small urllib3-style surface ``requests.Response`` uses
    (``read`` / ``stream`` / ``release_conn`` / ``close``).  Closing releases
    the file handle and unlinks the spool exactly once, so a closed response
    leaves no owned artifact behind.
    """

    def __init__(self, spool: Path) -> None:
        self._spool = spool
        self._handle = open(spool, 'rb')  # noqa: SIM115 - task-owned spool
        self._closed = False

    def read(self, amt: int = -1) -> bytes:
        if self._closed:
            return b''
        return self._handle.read(amt)

    def stream(self, chunk_size: int = 1, decode_content: bool = False):
        if self._closed:
            return
        while True:
            chunk = self._handle.read(chunk_size)
            if not chunk:
                return
            yield chunk

    def release_conn(self) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._handle.close()
        finally:
            _remove_quiet(self._spool)


class SupervisedHTTPTransport:
    """Runs one :class:`HTTPRequest` inside a supervised fixed-function child."""

    def __init__(self, worker_path=None) -> None:
        self.worker_path = Path(worker_path) if worker_path else DEFAULT_WORKER_PATH
        self.last_stdout: bytes = b''
        self.last_stderr: bytes = b''

    def fetch(self, request: HTTPRequest):
        deadline = float(request.deadline_sec)
        if not math.isfinite(deadline) or deadline <= 0:
            # Infinity/NaN/zero are not budgets; reject before any spawn or
            # owned resource exists.
            raise DataFetchError(
                f'Supervised HTTP operations require a finite positive deadline_sec (got {request.deadline_sec!r}).',
                code='TIMEOUT')
        self._validate_request_shape(request)
        fd, spool_name = tempfile.mkstemp(prefix=_SPOOL_PREFIX)
        os.close(fd)
        spool = Path(spool_name)
        try:
            return self._supervise(request, spool, deadline)
        except BaseException:
            _remove_quiet(spool)
            raise

    def _validate_request_shape(self, request: HTTPRequest) -> None:
        from urllib.parse import urlsplit
        from scripts.net_guard import reject_url_fragments
        if not isinstance(request.url, str) or not request.url:
            raise DataFetchError('Supervised HTTP requires a target URL.', code='ARG_INVALID')
        # DNS-free checks only: any parent-side resolution would break the
        # "supervision starts before DNS" boundary.  The child applies the
        # full public-target policy.
        reject_url_fragments(request.url)
        if urlsplit(request.url).scheme not in ('http', 'https'):
            raise DataFetchError(
                f"Blocked URL scheme '{urlsplit(request.url).scheme or '(none)'}' — "
                'only http/https are allowed.', code='BLOCKED_URL')
        if request.headers is not None and not isinstance(request.headers, dict):
            raise DataFetchError('Supervised HTTP headers must be a mapping.', code='ARG_INVALID')

    def _supervise(self, request: HTTPRequest, spool: Path, deadline: float):
        job = {
            'url': request.url,
            'public_only': bool(request.public_only),
            'headers': {str(k): str(v) for k, v in dict(request.headers or {}).items()},
            'timeout': float(request.timeout),
            'stream': bool(request.stream),
            'allow_redirects': bool(request.allow_redirects),
            'max_body_bytes': None if request.max_body_bytes is None else int(request.max_body_bytes),
            'prefix_bytes': None if request.prefix_bytes is None else int(request.prefix_bytes),
            'max_retries': max(1, int(request.max_retries)),
            'backoff_base': float(request.backoff_base),
            'deadline_sec': deadline,
            'spool_path': str(spool),
        }
        job_bytes = json.dumps(job, ensure_ascii=False).encode('utf-8')
        if len(job_bytes) > _JOB_MAX_BYTES:
            raise DataFetchError('The supervised request exceeds the bounded job frame.', code='SIZE_LIMIT')

        argv = [sys.executable, '-I', str(self.worker_path)]
        stdout: bytearray = bytearray()
        stderr: bytearray = bytearray()
        started = time.monotonic()
        try:
            popen_kwargs: dict = {
                'stdin': subprocess.PIPE,
                'stdout': subprocess.PIPE,
                'stderr': subprocess.PIPE,
                'shell': False,
                'close_fds': True,
                'cwd': str(self.worker_path.resolve().parent),
                'env': _child_env(),
            }
            if os.name == 'posix':
                # Own the whole child tree on POSIX; Windows kills the child directly.
                popen_kwargs['start_new_session'] = True
            proc = subprocess.Popen(argv, **popen_kwargs)
        except OSError as exc:
            raise DataFetchError('The supervised HTTP worker could not start.', code='NETWORK') from exc
        threads = [
            threading.Thread(target=_drain, args=(proc.stdout, stdout, _RESULT_MAX_BYTES), daemon=True),
            threading.Thread(target=_drain, args=(proc.stderr, stderr, _RESULT_MAX_BYTES), daemon=True),
        ]
        for thread in threads:
            thread.start()
        def _submit_job():
            # A near-64KiB job can exceed the pipe buffer: a worker that never
            # reads would block this write, so it runs off the wait path and
            # the deadline kill unblocks it.
            try:
                proc.stdin.write(job_bytes)
                proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass  # the child died early; the wait/collect path reports it

        writer = threading.Thread(target=_submit_job, daemon=True)
        writer.start()
        timed_out = False
        remaining = max(0.0, deadline - (time.monotonic() - started))
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_process(proc)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - kill escalation failed
                pass
        except KeyboardInterrupt:
            # Cancellation must not leak the owned worker or its pipes.
            _kill_process(proc)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                pass
            for thread in threads:
                thread.join(timeout=10)
            writer.join(timeout=5)
            self.last_stdout = bytes(stdout)
            self.last_stderr = bytes(stderr)
            raise
        for thread in threads:
            thread.join(timeout=10)
        writer.join(timeout=5)
        self.last_stdout = bytes(stdout)
        self.last_stderr = bytes(stderr)

        if timed_out:
            raise DataFetchError(
                f'Supervised HTTP operation exceeded its {deadline:g}s deadline and the worker was stopped.',
                code='TIMEOUT')
        if proc.returncode != 0:
            raise DataFetchError('The supervised HTTP worker crashed before producing a result.',
                                 code='NETWORK')
        return self._collect(request, spool)

    def _collect(self, request: HTTPRequest, spool: Path):
        def _invalid() -> DataFetchError:
            return DataFetchError('The supervised HTTP worker returned a malformed result.',
                                  code='NETWORK')

        try:
            result = json.loads(self.last_stdout.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise _invalid() from None
        if not isinstance(result, dict):
            raise _invalid()
        status = result.get('status')
        if status == 'error':
            code = result.get('code')
            code = code if code in _CHILD_ERROR_CODES else 'NETWORK'
            raise DataFetchError(_CHILD_MESSAGES.get(code, _CHILD_MESSAGES['NETWORK']), code=code)
        if status != 'ok':
            raise _invalid()
        status_code = result.get('status_code')
        if isinstance(status_code, bool) or not isinstance(status_code, int) or not 100 <= status_code < 600:
            raise _invalid()
        headers = result.get('headers')
        if not isinstance(headers, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
            raise _invalid()
        if not spool.is_file():
            raise _invalid()
        actual = spool.stat().st_size
        if request.max_body_bytes is not None and request.prefix_bytes is None                 and actual > request.max_body_bytes:
            # The child is trusted, but the full-transfer cap is revalidated
            # fail-closed.  Prefix intent is not subject to it.
            raise DataFetchError('The response body exceeds the configured size cap.', code='SIZE_LIMIT')
        return self._build_response(spool, status_code, headers, result.get('url'),
                                    body_complete=bool(result.get('body_complete', False)),
                                    body_bytes=actual)

    @staticmethod
    def _build_response(spool: Path, status_code: int, headers: dict, final_url,
                        body_complete: bool = False, body_bytes: int = 0):
        import requests
        from requests.structures import CaseInsensitiveDict

        response = requests.Response()
        response.status_code = int(status_code)
        response.headers = CaseInsensitiveDict(headers)
        response.url = final_url if isinstance(final_url, str) else ''
        response.reason = ''
        content_type = next((str(v) for k, v in headers.items()
                     if str(k).lower() == 'content-type'), '')
        charset = 'utf-8'
        for part in content_type.split(';'):
            part = part.strip().lower()
            if part.startswith('charset='):
                charset = part.split('=', 1)[1].strip(' \t"\'') or 'utf-8'
        response.encoding = charset
        response.raw = _SpoolReader(spool)
        response.body_complete = bool(body_complete)
        response.body_bytes = int(body_bytes)
        return response
