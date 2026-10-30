from pathlib import Path
import json
import logging
import re
import socket  # kept importable: tests patch scripts.web_analyzer.socket.getaddrinfo
import time
import urllib.robotparser
from urllib.parse import urljoin, urlparse
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from bs4 import BeautifulSoup
from scripts.errors import DataFetchError
from scripts.http_utils import request_with_retry
from scripts.net_guard import read_bounded_body, validate_public_url

logger = logging.getLogger(__name__)

# Whole-job budget for one analyze() call (robots check, redirect walk and
# HTML preview combined), enforced on a monotonic clock.
ANALYSIS_DEADLINE_SECONDS = 90.0
ROBOTS_MAX_BYTES = 512 * 1024

@dataclass
class AnalysisReport:
    url: str
    status_code: int = 200
    content_type: str = ""
    content_length: int = 0
    difficulty: str = "easy"  # 'easy', 'medium', 'hard', 'blocked'
    robots_allowed: bool = True
    cloudflare_detected: bool = False
    captcha_detected: bool = False
    rate_limit_headers: Dict[str, str] = field(default_factory=dict)
    table_count: int = 0
    warnings: List[str] = field(default_factory=list)

class WebAnalyzer:
    """Analyzes custom URLs to evaluate scraping difficulty and generates safe scraper scripts."""

    def __init__(self, url: str, output_dir: str) -> None:
        self.url = url
        self.output_dir = output_dir
        Path(self.output_dir).mkdir(parents=True, exist_ok=True)

    def _check_robots_txt(self, deadline: Optional[float] = None) -> bool:
        """Verify robots.txt compliance for the target URL.

        robots.txt is fetched through the SSRF-guarded, public-only transport
        with redirects OFF — urllib's auto-redirect would bypass validation and
        could be abused to probe internal addresses — and is streamed with a
        512 KiB cap so a hostile server cannot push unbounded data.  Any
        failure to RETRIEVE A missing policy (404) permits access; unavailable
        or redirected policies block analysis until they can be checked.
        """
        parsed = urlparse(self.url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        validate_public_url(robots_url)
        rp = urllib.robotparser.RobotFileParser()
        try:
            response = request_with_retry(
                robots_url,
                headers={"User-Agent": "data-fetcher-pipeline/2.0"},
                timeout=10,
                allow_redirects=False,
                stream=True,
                public_only=True,
                deadline_sec=max(0.5, deadline - time.monotonic()) if deadline else None,
            )
            if response.status_code == 404:
                response.close()
                return True
            if response.status_code != 200:
                response.close()
                return False
            lines = read_bounded_body(response, ROBOTS_MAX_BYTES).splitlines()
            response.close()
            rp.parse(lines)
            return rp.can_fetch("data-fetcher-pipeline", self.url)
        except (ValueError, OSError, DataFetchError) as exc:
            logger.warning("robots.txt could not be verified: %s", exc)
            return False

    def analyze(self) -> AnalysisReport:
        """Perform request and evaluate scraping difficulty of target URL."""
        report = AnalysisReport(url=self.url)
        deadline = time.monotonic() + ANALYSIS_DEADLINE_SECONDS

        def budget() -> Optional[float]:
            return max(0.5, deadline - time.monotonic())

        # SSRF guard: reject unsafe schemes and non-public targets
        validate_public_url(self.url)

        # robots.txt is a compliance hard stop: a denied URL is never fetched
        report.robots_allowed = self._check_robots_txt(deadline=deadline)
        if not report.robots_allowed:
            report.difficulty = "blocked"
            report.warnings.append("URL disallowed by target's robots.txt rules — fetch aborted (compliance hard stop).")
            return report

        # Perform request first to check metadata
        headers = {
            "User-Agent": "data-fetcher-pipeline/2.0"
        }

        url = self.url
        try:
            # Follow redirects manually so every hop passes the SSRF guard
            for _ in range(4):
                if time.monotonic() >= deadline:
                    report.difficulty = "blocked"
                    report.status_code = 0
                    report.warnings.append("Analysis deadline exceeded before the target could be fetched.")
                    return report
                response = request_with_retry(
                    url, headers=headers, allow_redirects=False, stream=True,
                    public_only=True, deadline_sec=budget(),
                )
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location", "")
                    if not location:
                        break
                    response.close()
                    url = urljoin(url, location)
                    validate_public_url(url)
                    report.warnings.append(f"Redirected to {url}")
                    redirected = WebAnalyzer(url, self.output_dir)
                    if not redirected._check_robots_txt(deadline=deadline):
                        report.robots_allowed = False
                        report.difficulty = 'blocked'
                        report.warnings.append('Redirect target robots policy disallows or cannot verify access.')
                        return report
                    continue
                break
            else:
                report.difficulty = 'blocked'
                report.warnings.append('Too many redirects; target not fetched.')
                return report
        except (ConnectionError, DataFetchError) as exc:
            # request_with_retry raises coded DataFetchError on exhaustion —
            # report it as a blocked target instead of crashing the analysis
            report.difficulty = "blocked"
            report.status_code = 0
            report.warnings.append(f"Connection failed: {exc}")
            return report

        report.status_code = response.status_code
        report.content_type = response.headers.get("Content-Type", "").lower()
        if response.status_code >= 400:
            report.difficulty = 'blocked'
            report.warnings.append(f'Provider refused access (HTTP {response.status_code}).')
            response.close()
            return report

        try:
            report.content_length = int(response.headers.get("Content-Length", "0"))
        except ValueError:
            report.content_length = 0

        # Check Cloudflare
        server_header = response.headers.get("Server", "").lower()
        if "cloudflare" in server_header or "cf-ray" in response.headers:
            report.cloudflare_detected = True
            report.warnings.append("Cloudflare protection mechanism detected.")

        # Check Rate Limit Headers
        for key, value in response.headers.items():
            key_lower = key.lower()
            if "ratelimit" in key_lower or "retry-after" in key_lower:
                report.rate_limit_headers[key] = value

        if report.rate_limit_headers:
            report.warnings.append(f"Rate limiting headers detected: {list(report.rate_limit_headers.keys())}")

        # If direct file, it's EASY
        is_direct_file = False
        for ext in [".csv", ".xlsx", ".json", ".parquet", ".tsv"]:
            if self.url.lower().split("?")[0].endswith(ext):
                is_direct_file = True
                break

        if "text/csv" in report.content_type or "application/json" in report.content_type or is_direct_file:
            report.difficulty = "easy"
            response.close()
            return report

        # HTML parsing
        if "text/html" in report.content_type:
            preview = bytearray()
            try:
                for chunk in response.iter_content(chunk_size=65536):
                    preview.extend(chunk[:max(0, 1048576 - len(preview))])
                    if len(preview) >= 1048576:
                        report.warnings.append("HTML analysis limited to the first 1 MB.")
                        break
                    if time.monotonic() >= deadline:
                        report.warnings.append("HTML analysis stopped at the analysis deadline.")
                        break
            finally:
                response.close()
            html_text = preview.decode(response.encoding or "utf-8", errors="replace")
            soup = BeautifulSoup(html_text, "html.parser")

            # Check for CAPTCHA
            html_content = html_text.lower()
            if "g-recaptcha" in html_content or "hcaptcha" in html_content or "captcha" in html_content:
                report.captcha_detected = True
                report.warnings.append("CAPTCHA challenge forms detected in HTML payload.")

            tables = soup.find_all("table")
            report.table_count = len(tables)

            if report.captcha_detected or not report.robots_allowed:
                report.difficulty = "blocked"
            elif report.table_count > 0:
                report.difficulty = "medium"
            else:
                report.difficulty = "hard"
        else:
            report.difficulty = "hard"
            response.close()
        return report

    def generate_script(self, report: AnalysisReport) -> str:
        """Generate a custom, safe extractor script based on difficulty and save to file."""
        safe_url = re.sub(r'[^a-zA-Z0-9_]', '_', self.url.split("://")[-1][:50])
        script_filename = f"scrape_{safe_url}.py"
        script_path = str(Path(self.output_dir) / script_filename)

        script_content = f"""# Generated scraper for a checked public URL.
import os
import sys
import time
import math
import codecs
import ipaddress
import socket
import urllib.robotparser
from urllib.parse import urljoin, urlparse
import pandas as pd
import requests

URL = {json.dumps(self.url)}
HEADERS = {{
    "User-Agent": "data-fetcher-pipeline/2.0"
}}
"""
        script_content += '''
# --- Standalone network policy (no pipeline imports allowed here) -----------
# The helpers below mirror scripts/net_guard.py; a parity test asserts both
# implementations agree on hostile addresses so the copies cannot drift.

ROBOTS_MAX_BYTES = 512 * 1024
TRANSFER_DEADLINE_SECONDS = 90.0
MAX_BYTES = 50 * 1024 * 1024
MAX_PREVIEW_BYTES = 10 * 1024 * 1024
XLSX_MAX_ENTRIES = 1000
XLSX_MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024

# Explicit empty proxies/auth: requests.get would otherwise apply ambient
# proxy environment variables and ~/.netrc credentials automatically.
# The 'all' key matters: requests consults ALL_PROXY/all_proxy for any
# scheme as a fallback, so it must be pinned to None explicitly too.
NO_PROXIES = {'http': None, 'https': None, 'all': None}


class _NoCredentials:
    """Explicit empty auth — suppresses silent ~/.netrc credential lookup."""

    def __call__(self, prepared):
        return prepared


NO_CREDENTIALS = _NoCredentials()


def _unwrap_tunneling(address):
    """Collapse IPv4-in-IPv6 encodings (mapped / 6to4 / Teredo) to real IPv4."""
    if address.version != 6:
        return address
    mapped = address.ipv4_mapped
    if mapped is not None:
        return mapped
    sixtofour = address.sixtofour
    if sixtofour is not None:
        return sixtofour
    teredo = address.teredo
    if teredo is not None:
        return teredo[1]
    return address


def _blocked_flags(address):
    return (
        address.is_private or address.is_loopback or address.is_link_local
        or address.is_reserved or address.is_unspecified or address.is_multicast
    )


def _is_public_ip(address):
    """True only for globally routable addresses (RFC 6598 shared space,
    loopback, link-local, private, reserved, multicast and tunneled forms of
    all of them are rejected)."""
    if isinstance(address, str):
        try:
            address = ipaddress.ip_address(address.strip('[]'))
        except ValueError:
            return False
    plain = _unwrap_tunneling(address)
    if _blocked_flags(plain) or not plain.is_global:
        return False
    if plain is not address and _blocked_flags(address):
        return False
    return True


def _validate_public_url(url: str) -> None:
    if any(ord(character) < 32 or ord(character) == 127 for character in url):
        raise ValueError('Blocked URL: ASCII control characters are not allowed in fetched URLs.')
    parsed = urlparse(url)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError('Blocked URL: embedded userinfo (user:pass@host) is not allowed.')
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('Blocked URL: only public http(s) targets are allowed.')
    try:
        literal = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        literal = None
    if literal is not None:
        if not _is_public_ip(literal):
            raise ValueError('Blocked URL: target is not a globally routable public address.')
        return
    host = parsed.hostname.lower()
    if host == 'localhost' or host.endswith('.localhost'):
        raise ValueError("Blocked URL: target 'localhost' is not allowed.")
    addresses = []
    for entry in socket.getaddrinfo(parsed.hostname, None):
        candidate = ipaddress.ip_address(entry[4][0])
        if not _is_public_ip(candidate):
            raise ValueError('Blocked URL: target resolves to a non-public address.')
        if candidate not in addresses:
            addresses.append(candidate)
    if not addresses:
        raise ValueError('Blocked URL: target did not resolve to any address.')


def _public_addresses(host):
    """Resolve once and validate every DNS answer; [] means literal-IP host."""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _is_public_ip(literal):
            raise ValueError('Blocked URL: target is not a globally routable public address.')
        return []
    addresses = []
    for entry in socket.getaddrinfo(host, None):
        candidate = ipaddress.ip_address(entry[4][0])
        if not _is_public_ip(candidate):
            raise ValueError('Blocked URL: target resolves to a non-public address.')
        if candidate not in addresses:
            addresses.append(candidate)
    if not addresses:
        raise ValueError('Blocked URL: target did not resolve to any address.')
    return addresses


def _checked_get(url, **kwargs):
    """requests.get pinned to pre-validated DNS answers.

    The target host is resolved once and every answer is validated; during the
    request socket.getaddrinfo serves ONLY those validated answers, so a TTL=0
    rebinding flip between validation and connection cannot divert the dial.
    The URL itself is never rewritten, preserving the Host header, TLS SNI and
    certificate hostname verification.  Single-threaded script: the resolver
    swap is restored in a finally block.
    """
    parsed = urlparse(url)
    host = parsed.hostname or ''
    if not host:
        raise ValueError('Blocked URL: no hostname present.')
    addresses = _public_addresses(host)
    try:
        host_ascii = host.encode('idna').decode('ascii')
    except UnicodeError:
        host_ascii = host
    real_getaddrinfo = socket.getaddrinfo
    if addresses:
        def pinned_getaddrinfo(host_arg, port_arg=None, family=0, type=0, proto=0, flags=0):
            if host_arg == host or host_arg == host_ascii:
                answers = []
                for address in addresses:
                    answers.extend(real_getaddrinfo(str(address), port_arg, family, type, proto, flags))
                return answers
            return real_getaddrinfo(host_arg, port_arg, family, type, proto, flags)
        socket.getaddrinfo = pinned_getaddrinfo
    try:
        merged = dict(kwargs)
        merged.setdefault('proxies', NO_PROXIES)
        merged.setdefault('auth', NO_CREDENTIALS)
        return requests.get(url, **merged)
    finally:
        socket.getaddrinfo = real_getaddrinfo


def _read_bounded(response, limit):
    """Stream at most `limit` bytes and decode as UTF-8.

    Responses that cannot stream (in-process fakes, non-streaming response
    objects) fall back to their .text attribute truncated to the limit.
    """
    buffer = bytearray()
    for chunk in response.iter_content(65536):
        remaining = limit - len(buffer)
        if remaining <= 0:
            break
        buffer.extend(chunk[:remaining])
    if not buffer:
        text = getattr(response, 'text', '')
        if isinstance(text, str) and text:
            return text[:limit]
    return buffer.decode('utf-8', errors='replace')


def _robots_allowed(url: str) -> bool:
    parsed = urlparse(url)
    current = parsed.scheme + '://' + parsed.netloc + '/robots.txt'
    for _ in range(4):
        _validate_public_url(current)
        try:
            response = _checked_get(current, headers=HEADERS, timeout=10,
                                    allow_redirects=False, stream=True)
        except requests.RequestException:
            return False
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get('Location')
            response.close()
            if not location:
                return False
            current = urljoin(current, location)
            continue
        if response.status_code == 404:
            response.close()
            return True
        if response.status_code != 200:
            response.close()
            return False
        try:
            body = _read_bounded(response, ROBOTS_MAX_BYTES)
        finally:
            response.close()
        policy = urllib.robotparser.RobotFileParser()
        policy.parse(body.splitlines())
        return policy.can_fetch('data-fetcher-pipeline', url)
    return False


def _request_checked(url: str, headers: dict[str, str]) -> requests.Response:
    current = url
    for _ in range(5):
        _validate_public_url(current)
        if not _robots_allowed(current):
            raise ValueError('Blocked URL: robots.txt disallows this target or could not be verified.')
        response = _checked_get(current, headers=headers, timeout=30, allow_redirects=False, stream=True)
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get('Location')
            response.close()
            if not location:
                raise ValueError('Redirect response has no Location header.')
            current = urljoin(current, location)
            continue
        return response
    raise ValueError('Too many redirects while fetching the target URL.')


def _fetch_with_retry(url: str, headers: dict[str, str], retries: int = 3) -> requests.Response:
    started = time.monotonic()

    def remaining():
        return TRANSFER_DEADLINE_SECONDS - (time.monotonic() - started)

    for attempt in range(retries):
        if remaining() <= 0:
            raise RuntimeError('Transfer deadline of 90s exceeded while fetching ' + url)
        try:
            resp = _request_checked(url, headers)
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(min(2 ** (attempt + 1), max(0.0, remaining())))
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == retries - 1:
                resp.close()
                raise RuntimeError('Request failed with status ' + str(resp.status_code) + ' after ' + str(retries) + ' attempts: ' + url)
            retry_after = resp.headers.get('Retry-After', '')
            try:
                delay = float(retry_after)
                if not math.isfinite(delay) or delay < 0:
                    delay = 2 ** (attempt + 1)
            except ValueError:
                delay = 2 ** (attempt + 1)
            resp.close()
            time.sleep(min(min(delay, 60.0), max(0.0, remaining())))
            continue
        return resp
    raise RuntimeError('No response received after retry attempts.')

# --- End standalone network policy ------------------------------------------

'''
        from scripts.custom_template import EXTRACTION_BODY
        script_content += "\nDIFFICULTY = " + json.dumps(report.difficulty) + "\n"
        script_content += EXTRACTION_BODY

        try:
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(script_content)
        except OSError as exc:
            raise RuntimeError(f"Failed to save generated script to disk: {exc}") from exc

        return script_path
