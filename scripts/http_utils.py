import math
import time
import logging
import email.utils
from datetime import datetime, timezone
from typing import Optional, Dict
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

from scripts.errors import DataFetchError

logger = logging.getLogger(__name__)

session = requests.Session()
adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10)
session.mount("http://", adapter)
session.mount("https://", adapter)

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
) -> requests.Response:
    """HTTP GET with exponential backoff retry for transient failures.

    With stream=True the response body is not downloaded — use for lightweight
    probes; the caller must call close() on the returned response.
    With allow_redirects=False, 3xx responses are returned as-is so the caller
    can validate each redirect target (SSRF guard).
    """
    last_error: Optional[Exception] = None
    last_response: Optional[requests.Response] = None
    attempt = 0
    
    while attempt < max_retries:
        _enforce_host_pacing(url)
        try:
            response = session.get(url, headers=headers, timeout=timeout, stream=stream, allow_redirects=allow_redirects)
            _METRICS["requests"] += 1
            if not stream:
                _METRICS["bytes"] += len(response.content or b"")
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
                time.sleep(retry_after)
                continue
                
            if response.status_code >= 500:
                attempt += 1
                _METRICS["retries"] += 1
                if attempt >= max_retries:
                    break
                wait = backoff_base ** attempt
                logger.warning("Server error %d. Retrying in %.1fs.", response.status_code, wait)
                response.close()
                time.sleep(wait)
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
            time.sleep(wait)
            
        except requests.Timeout as exc:
            last_error = exc
            attempt += 1
            _METRICS["retries"] += 1
            if attempt >= max_retries:
                break
            wait = backoff_base ** attempt
            logger.warning("Timeout on attempt %d. Retrying in %.1fs.", attempt, wait)
            time.sleep(wait)
            
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
            f"All {max_retries} retries exhausted for {url} with status {status}.",
            code="PROVIDER_ERROR",
        )
    if last_error:
        raise DataFetchError(
            f"All {max_retries} retries exhausted for {url}: {type(last_error).__name__}. "
            "Check network connectivity and retry.",
            code="NETWORK",
        ) from last_error
    raise DataFetchError(f"All {max_retries} retries exhausted for {url}.", code="NETWORK")
