"""Fixed-function supervised HTTP child.

Run as ``[sys.executable, '-I', http_worker.py]`` by
``scripts/adapters/http_transport.py``.  The job arrives as one bounded JSON
frame on stdin; exactly one bounded JSON result goes to stdout.  This is
trusted fixed code: it spawns nothing, imports no user code, ignores the
inherited environment for network purposes (``trust_env=False`` — no ambient
proxies or netrc), applies the public-target DNS/redirect policy through
:mod:`scripts.net_guard` inside the child, and writes the body only to the
spool path the parent handed it.

Deadlines: the parent's kill is the hard wall-clock bound.  Inside the child,
socket budgets are clamped to the remaining job budget (cooperative), retries
are bounded, and server ``Retry-After`` values are deliberately not honored —
a hostile server cannot stretch the job; the parent deadline governs.

The child never spawns descendants and never reads credentials from the
environment; request authentication travels inside the job frame in memory.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import requests  # noqa: E402

from scripts.errors import DataFetchError  # noqa: E402
from scripts.net_guard import (  # noqa: E402
    PinnedPublicHTTPAdapter, UnresolvableHostError, reject_url_fragments, validate_public_url)

_JOB_MAX_BYTES = 64 * 1024
_RESULT_MAX_BYTES = 8 * 1024
_CHUNK = 65536

#: Headers that survive result-frame shrinking; server text beyond these is
#: dropped before the frame is emitted.
_ESSENTIAL_HEADERS = ('content-type', 'content-length', 'location',
                      'content-encoding', 'last-modified', 'etag')

_ERROR_CODES = {'BLOCKED_URL', 'SIZE_LIMIT', 'NETWORK', 'TIMEOUT',
                'PROVIDER_ERROR', 'RATE_LIMITED'}


def _emit(result: dict) -> None:
    data = json.dumps(result, ensure_ascii=False).encode('utf-8')
    if len(data) > _RESULT_MAX_BYTES and 'headers' in result:
        kept = {k: v for k, v in result['headers'].items() if k.lower() in _ESSENTIAL_HEADERS}
        data = json.dumps({**result, 'headers': kept}, ensure_ascii=False).encode('utf-8')
    if len(data) > _RESULT_MAX_BYTES:
        data = json.dumps({k: v for k, v in result.items() if k != 'headers'},
                          ensure_ascii=False).encode('utf-8')
    sys.stdout.buffer.write(data[:_RESULT_MAX_BYTES])
    sys.stdout.buffer.flush()


def _fail(code: str) -> int:
    if code not in _ERROR_CODES:
        code = 'NETWORK'
    _emit({'status': 'error', 'code': code})
    return 0


def _hardened_session(public_only: bool) -> requests.Session:
    """trust_env=False everywhere; the pinned adapter only for public-only."""
    session = requests.Session()
    session.trust_env = False
    if public_only:
        session.mount('http://', PinnedPublicHTTPAdapter())
        session.mount('https://', PinnedPublicHTTPAdapter())
    else:
        from requests.adapters import HTTPAdapter
        session.mount('http://', HTTPAdapter())
        session.mount('https://', HTTPAdapter())
    return session


def _child_job(raw: bytes) -> dict:
    job = json.loads(raw.decode('utf-8'))
    if not isinstance(job, dict):
        raise ValueError('job frame is not an object')
    return job


def _fetch_with_retries(job: dict, started: float):
    """Bounded retry loop with socket budgets clamped to the job deadline."""
    max_retries = max(1, int(job.get('max_retries', 3)))
    backoff_base = float(job.get('backoff_base', 2))
    timeout = float(job.get('timeout', 30))
    session = _hardened_session(bool(job.get('public_only', True)))

    attempt = 0
    while attempt < max_retries:
        remaining = float(job['deadline_sec']) - (time.monotonic() - started)
        if remaining <= 0:
            return None, 'TIMEOUT'
        try:
            response = session.get(
                job['url'], headers=dict(job.get('headers') or {}),
                timeout=max(0.05, min(timeout, remaining)),
                stream=True, allow_redirects=bool(job.get('allow_redirects', True)))
        except requests.exceptions.ConnectionError as exc:
            # The pinned adapter voices every policy refusal as this type with
            # a locally authored 'Blocked...' prefix; keep the coded split.
            if str(exc).startswith('Blocked'):
                return None, 'BLOCKED_URL'
            attempt += 1
            if attempt >= max_retries:
                return None, 'NETWORK'
        except requests.exceptions.Timeout:
            attempt += 1
            if attempt >= max_retries:
                return None, 'TIMEOUT'
        except ValueError:
            # The pinned adapter translates every policy refusal into this
            # requests type; keep the coded distinction.
            return None, 'BLOCKED_URL'
        else:
            if response.status_code == 429 or response.status_code >= 500:
                attempt += 1
                if attempt >= max_retries:
                    # Retry budget exhausted inside the child: hand the status
                    # response back and let the caller own the decision.
                    return response, None
                response.close()
            else:
                return response, None
        remaining = float(job['deadline_sec']) - (time.monotonic() - started)
        if remaining > 0:
            time.sleep(min(backoff_base ** attempt, remaining))
    return None, 'NETWORK'


def main() -> int:
    started = time.monotonic()
    raw = sys.stdin.buffer.read(_JOB_MAX_BYTES + 1)
    if len(raw) > _JOB_MAX_BYTES:
        return _fail('SIZE_LIMIT')
    try:
        job = _child_job(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return _fail('NETWORK')

    url = job.get('url')
    spool_path = job.get('spool_path')
    if not isinstance(url, str) or not url or not isinstance(spool_path, str) or not spool_path:
        return _fail('NETWORK')
    if urlsplit(url).scheme not in ('http', 'https'):
        return _fail('BLOCKED_URL')
    if bool(job.get('public_only', True)):
        # The full public-target policy runs here, inside the supervised child,
        # before any connection: DNS answers included.
        try:
            validate_public_url(url)
        except UnresolvableHostError:
            return _fail('NETWORK')
        except ValueError:
            return _fail('BLOCKED_URL')
    else:
        try:
            reject_url_fragments(url)
        except ValueError:
            return _fail('BLOCKED_URL')

    response, error = _fetch_with_retries(job, started)
    if error is not None:
        return _fail(error)

    max_body_bytes = job.get('max_body_bytes')
    max_body = None if max_body_bytes is None else int(max_body_bytes)
    prefix_bytes = job.get('prefix_bytes')
    prefix = None if prefix_bytes is None else int(prefix_bytes)
    total = 0
    body_complete = False
    try:
        content_length = response.headers.get('Content-Length', '')
        # The declared-size fast reject enforces the FULL-transfer cap only:
        # a prefix request is not a full download and must not be rejected
        # for a large declared tail it never intends to read.
        if (prefix is None and max_body is not None and content_length.isdigit()
                and int(content_length) > max_body):
            response.close()
            return _fail('SIZE_LIMIT')
        cap = prefix if prefix is not None else max_body
        read_size = _CHUNK if prefix is None else max(1, min(_CHUNK, prefix))
        with open(spool_path, 'wb') as out:
            for chunk in response.iter_content(read_size):
                if not chunk:
                    continue
                room = None if cap is None else cap - total
                if room is not None and room <= 0:
                    # The requested prefix is satisfied: never read an
                    # unapproved tail merely to claim completeness.
                    if prefix is None:
                        # Strict full-transfer cap: one byte beyond is a
                        # coded failure, never a truncated success.
                        response.close()
                        return _fail('SIZE_LIMIT')
                    break
                take = chunk if room is None or len(chunk) <= room else chunk[:room]
                out.write(take)
                total += len(take)
                if prefix is not None and total >= prefix:
                    break
                if prefix is None and room is not None and len(chunk) > room:
                    # A full download never swallows an over-cap byte, even
                    # when the cap is crossed inside the final chunk: a
                    # capped transfer that does not fit is a coded failure,
                    # not a silently truncated success.
                    response.close()
                    return _fail('SIZE_LIMIT')
            else:
                body_complete = True
            if content_length.isdigit() and total == int(content_length):
                body_complete = True
        result = {
            'status': 'ok',
            'status_code': int(response.status_code),
            'headers': {str(k): str(v) for k, v in response.headers.items()},
            'url': str(response.url or url),
            'spool_path': spool_path,
            'bytes': total,
            'body_complete': bool(body_complete),
        }
        response.close()
        _emit(result)
        return 0
    except (OSError, requests.exceptions.RequestException):
        return _fail('NETWORK')
    finally:
        try:
            response.close()
        except Exception:  # noqa: BLE001 - best-effort socket release on every path
            pass


if __name__ == '__main__':
    sys.exit(main())
