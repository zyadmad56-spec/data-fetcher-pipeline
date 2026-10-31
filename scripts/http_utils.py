import math
import time
import logging
import email.utils
from datetime import timezone
from typing import Optional, Dict
from urllib.parse import urlparse, urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter

from scripts.errors import DataFetchError
from scripts.net_guard import PinnedPublicHTTPAdapter, validate_public_url

logger = logging.getLogger(__name__)

session = requests.Session()
adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10)
session.mount("http://", adapter)
session.mount("https://", adapter)

# Untrusted/public transport: no ambient environment (no ~/.netrc auto-auth,
# no proxy variables) and a connect-pinned adapter that revalidates every
# DNS answer at connect time.  Used only when callers opt in via
# request_with_retry(..., public_only=True); the default session above keeps
# its historical semantics for built-in provider traffic.
public_session = requests.Session()
public_session.trust_env = False
public_adapter = PinnedPublicHTTPAdapter(pool_connections=10, pool_maxsize=10)
public_session.mount("http://", public_adapter)
public_session.mount("https://", public_adapter)

# Per-host politeness floor: enforced in EVERY mode within a single process
# (scope note: state is per-process — back-to-back CLI invocations do NOT share
# it; cross-process pacing is the invoking agent's responsibility)
MIN_HOST_INTERVALS = {
    "api.coingecko.com": 6.0,
}
_last_request_ts: Dict[str, float] = {}

# Cost metering: one run's provider spend, disclosed in the JSON envelope
_METRICS = {"requests": 0, "retries": 0, "bytes": 0}

def reset_metrics() -> None:
    global _METRICS
    _METRICS = {"requests": 0, "retries": 0, "bytes": 0}

def get_metrics() -> Dict[str, int]:
    return dict(_METRICS)

def add_metrics_bytes(n: int) -> None:
    """Count streamed payload bytes (callers reading iter_content chunk by chunk)."""
    _METRICS["bytes"] += n


def _public_error_url(url: str) -> str:
    """Keep endpoint context while omitting URL credentials and query secrets."""
    parts = urlsplit(url)
    host = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, host, parts.path, "", ""))

def _enforce_host_pacing(url: str) -> None:
    host = urlparse(url).netloc
    min_interval = MIN_HOST_INTERVALS.get(host, 0.0)
    if min_interval <= 0:
        return
    now = time.time()
    last = _last_request_ts.get(host)
    if last is not None:
        remaining = min_interval - (now - last)
        if remaining > 0:
            logger.info("Pacing: sleeping %.1fs to respect %s interval.", remaining, host)
            time.sleep(remaining)
    _last_request_ts[host] = time.time()

def _parse_retry_after(header_value: str, fallback: float) -> float:
    """Parse a Retry-After header (delta-seconds or RFC 7231 HTTP-date).

    Hostile or malformed values (NaN, negative, garbage) fall back to the
    exponential estimate — they must never crash the retry loop. Naive
    HTTP-dates are interpreted as UTC.
    """
    try:
        seconds = float(header_value)
        return seconds if math.isfinite(seconds) and seconds > 0 else fallback
    except ValueError:
        try:
            dt = email.utils.parsedate_to_datetime(header_value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            delay = dt.timestamp() - time.time()
            return delay if delay > 0 else fallback
        except (TypeError, ValueError):
            return fallback

def request_with_retry(
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    max_retries: int = 3,
    backoff_base: float = 2.0,
    timeout: int = 30,
    stream: bool = False,
    allow_redirects: bool = True,
    public_only: bool = False,
    max_body_bytes: Optional[int] = None,
    deadline_sec: Optional[float] = None,
) -> requests.Response:
    """HTTP GET with exponential backoff retry for transient failures.

    With stream=True the response body is not downloaded — use for lightweight
    probes; the caller must call close() on the returned response.
    With allow_redirects=False, 3xx responses are returned as-is so the caller
    can validate each redirect target (SSRF guard).
    With public_only=True the request runs on the hardened transport: a
    trust_env=False session (no ~/.netrc auto-auth, no ambient proxy
    variables) whose pinned adapter validates every DNS answer at connect
    time.  The target URL is checked by validate_public_url before the first
    attempt, and each redirect hop is revalidated by the adapter itself.
    With max_body_bytes set, non-streamed bodies larger than the cap (or a
    Content-Length larger than the cap) raise a coded SIZE_LIMIT error;
    streamed bodies remain the caller's responsibility to bound while reading.
    With deadline_sec set, the whole call — retries and backoff sleeps
    included — must complete within that many seconds (monotonic clock) or a
    coded TIMEOUT error is raised; sleeps are additionally clamped to the
    remaining budget.
    """
    last_error: Optional[Exception] = None
    last_response: Optional[requests.Response] = None
    attempt = 0
    started = time.monotonic()

    def _deadline_exceeded() -> bool:
        return deadline_sec is not None and (time.monotonic() - started) >= deadline_sec

    def _remaining() -> Optional[float]:
        if deadline_sec is None:
            return None
        return max(0.0, deadline_sec - (time.monotonic() - started))

    def _sleep_bounded(seconds: float) -> None:
        """Sleep for a backoff interval, clamped to the caller's deadline.

        With no budget left the sleep is skipped entirely (the loop top then
        raises TIMEOUT); a zero-length sleep call here would be a second,
        pointless time.sleep invocation.
        """
        remaining = _remaining()
        if remaining is None:
            time.sleep(seconds)
        elif remaining > 0:
            time.sleep(min(seconds, remaining))

    if public_only:
        validate_public_url(url)

    while attempt < max_retries:
        if _deadline_exceeded():
            raise DataFetchError(
                f"Deadline of {deadline_sec:.0f}s exceeded for {_public_error_url(url)} before completion.",
                code="TIMEOUT",
            )
        _enforce_host_pacing(url)
        if _deadline_exceeded():
            raise DataFetchError(
                f"Deadline of {deadline_sec:.0f}s exceeded for {_public_error_url(url)} before completion.",
                code="TIMEOUT",
            )
        try:
            transport = public_session if public_only else session
            response = transport.get(url, headers=headers, timeout=timeout, stream=stream, allow_redirects=allow_redirects)
            _METRICS["requests"] += 1
            if not stream:
                _METRICS["bytes"] += len(response.content or b"")
                if max_body_bytes is not None and len(response.content or b"") > max_body_bytes:
                    response.close()
                    raise DataFetchError(
                        f"Response body exceeds the {max_body_bytes // 1024} KiB cap for {_public_error_url(url)}.",
                        code="SIZE_LIMIT",
                    )
            elif max_body_bytes is not None:
                content_length = response.headers.get("Content-Length", "")
                if content_length.isdigit() and int(content_length) > max_body_bytes:
                    response.close()
                    raise DataFetchError(
                        f"Content-Length exceeds the {max_body_bytes // 1024} KiB cap for {_public_error_url(url)}.",
                        code="SIZE_LIMIT",
                    )
            last_response = response
            
            if response.status_code == 429:
                attempt += 1
                _METRICS["retries"] += 1
                if attempt >= max_retries:
                    break
                retry_after_str = response.headers.get("Retry-After", "")
                fallback = backoff_base ** attempt
                retry_after = _parse_retry_after(retry_after_str, fallback) if retry_after_str else fallback
                # Cap server-controlled sleeps: a hostile/misbehaving server must not
                # be able to hang the CLI for hours via an oversized Retry-After
                retry_after = min(retry_after, 60.0)
                logger.warning("Rate limited (429). Sleeping %.1fs before retry.", retry_after)
                response.close()
                _sleep_bounded(retry_after)
                continue

            if response.status_code >= 500:
                attempt += 1
                _METRICS["retries"] += 1
                if attempt >= max_retries:
                    break
                wait = backoff_base ** attempt
                logger.warning("Server error %d. Retrying in %.1fs.", response.status_code, wait)
                response.close()
                _sleep_bounded(wait)
                continue
                
            # Non-429 and non-5xx responses (e.g., 200, 401, 403, 404)
            # are returned immediately without retrying.
            return response
            
        except requests.ConnectionError as exc:
            last_error = exc
            attempt += 1
            _METRICS["retries"] += 1
            if attempt >= max_retries:
                break
            wait = backoff_base ** attempt
            logger.warning("Connection error on attempt %d. Retrying in %.1fs.", attempt, wait)
            _sleep_bounded(wait)

        except requests.Timeout as exc:
            last_error = exc
            attempt += 1
            _METRICS["retries"] += 1
            if attempt >= max_retries:
                break
            wait = backoff_base ** attempt
            logger.warning("Timeout on attempt %d. Retrying in %.1fs.", attempt, wait)
            _sleep_bounded(wait)
            
    if last_response is not None and last_response.status_code == 429:
        last_response.close()
        raise DataFetchError(
            f"Rate limited (429) by {urlparse(url).netloc} after {max_retries} attempts. "
            "Wait ~60s and re-run.",
            code="RATE_LIMITED",
        )
    if last_response is not None and last_response.status_code >= 500:
        status = last_response.status_code
        last_response.close()
        raise DataFetchError(
            f"All {max_retries} retries exhausted for {_public_error_url(url)} with status {status}.",
            code="PROVIDER_ERROR",
        )
    if last_error:
        raise DataFetchError(
            f"All {max_retries} retries exhausted for {_public_error_url(url)}: {type(last_error).__name__}. "
            "Check network connectivity and retry.",
            code="NETWORK",
        ) from last_error
    raise DataFetchError(f"All {max_retries} retries exhausted for {_public_error_url(url)}.", code="NETWORK")
