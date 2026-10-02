from pathlib import Path
import ipaddress
import json
import logging
import re
import socket
import urllib.robotparser
from urllib.parse import urljoin, urlparse
from dataclasses import dataclass, field
from typing import Dict, List
from bs4 import BeautifulSoup
from scripts.errors import DataFetchError
from scripts.http_utils import request_with_retry

logger = logging.getLogger(__name__)

def _is_blocked_ip(ip: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> bool:
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_unspecified or ip.is_multicast
    )

def validate_public_url(url: str) -> None:
    """SSRF guard: raise ValueError unless the URL is http(s) and targets a public host.

    Blocks non-http(s) schemes, localhost, and any address (literal or DNS-resolved)
    in loopback, link-local (incl. cloud metadata), private, reserved, or multicast
    ranges. Every redirect hop must pass this check before being followed.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Blocked URL scheme '{parsed.scheme or '(none)'}' — only http/https are allowed.")
    host = parsed.hostname or ""
    if not host:
        raise ValueError("Blocked URL: no hostname present.")

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None

    if ip is not None:
        if _is_blocked_ip(ip):
            raise ValueError(f"Blocked target '{host}': loopback, link-local, private, or reserved address.")
        return

    if host.lower() == "localhost" or host.lower().endswith(".localhost"):
        raise ValueError("Blocked target 'localhost'.")

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve host '{host}': {exc}") from exc
    for info in infos:
        try:
            resolved = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if _is_blocked_ip(resolved):
            raise ValueError(
                f"Blocked target '{host}' (resolves to {resolved}): "
                "loopback, link-local, private, or reserved address."
            )

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

    def _check_robots_txt(self) -> bool:
        """Verify robots.txt compliance for the target URL.

        robots.txt is fetched through the SSRF-guarded, timeout-bounded client
        with redirects OFF — urllib's auto-redirect would bypass validation and
        could be abused to probe internal addresses. Any failure to RETRIEVE
        A missing policy (404) permits access; unavailable or redirected
        policies block analysis until they can be checked.
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
            )
            response.close()
            if response.status_code == 404:
                return True
            if response.status_code != 200:
                return False
            lines = response.text.splitlines()
            rp.parse(lines)
            return rp.can_fetch("data-fetcher-pipeline", self.url)
        except (ValueError, OSError, DataFetchError) as exc:
            logger.warning("robots.txt could not be verified: %s", exc)
            return False

    def analyze(self) -> AnalysisReport:
        """Perform request and evaluate scraping difficulty of target URL."""
        report = AnalysisReport(url=self.url)

        # SSRF guard: reject unsafe schemes and private/loopback/link-local targets
        validate_public_url(self.url)

        # robots.txt is a compliance hard stop: a denied URL is never fetched
        report.robots_allowed = self._check_robots_txt()
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
                response = request_with_retry(url, headers=headers, allow_redirects=False, stream=True)
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location", "")
                    if not location:
                        break
                    response.close()
                    url = urljoin(url, location)
                    validate_public_url(url)
                    report.warnings.append(f"Redirected to {url}")
                    redirected = WebAnalyzer(url, self.output_dir)
                    if not redirected._check_robots_txt():
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
        script_content += """
def _validate_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Blocked URL: only public http(s) targets are allowed.")
    try:
        addresses = [ipaddress.ip_address(parsed.hostname)]
    except ValueError:
        addresses = [ipaddress.ip_address(entry[4][0]) for entry in socket.getaddrinfo(parsed.hostname, None)]
    if not addresses or any(
        address.is_private or address.is_loopback or address.is_link_local
        or address.is_reserved or address.is_unspecified or address.is_multicast
        for address in addresses
    ):
        raise ValueError("Blocked URL: target resolves to a non-public address.")


def _robots_allowed(url: str) -> bool:
    parsed = urlparse(url)
    current = parsed.scheme + "://" + parsed.netloc + "/robots.txt"
    for _ in range(4):
        _validate_public_url(current)
        try:
            response = requests.get(current, headers=HEADERS, timeout=10, allow_redirects=False)
        except requests.RequestException:
            return False
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location")
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
        policy = urllib.robotparser.RobotFileParser()
        policy.parse(response.text.splitlines())
        response.close()
        return policy.can_fetch("data-fetcher-pipeline", url)
    return False


def _request_checked(url: str, headers: dict[str, str]) -> requests.Response:
    current = url
    for _ in range(5):
        _validate_public_url(current)
        if not _robots_allowed(current):
            raise ValueError("Blocked URL: robots.txt disallows this target or could not be verified.")
        response = requests.get(current, headers=headers, timeout=30, allow_redirects=False, stream=True)
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise ValueError("Redirect response has no Location header.")
            current = urljoin(current, location)
            continue
        return response
    raise ValueError("Too many redirects while fetching the target URL.")


def _fetch_with_retry(url: str, headers: dict[str, str], retries: int = 3) -> requests.Response:
    for attempt in range(retries):
        try:
            resp = _request_checked(url, headers)
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** (attempt + 1))
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == retries - 1:
                resp.close()
                raise RuntimeError("Request failed with status " + str(resp.status_code) + " after " + str(retries) + " attempts: " + url)
            retry_after = resp.headers.get('Retry-After', '')
            try:
                delay = float(retry_after)
                if not math.isfinite(delay) or delay < 0:
                    delay = 2 ** (attempt + 1)
            except ValueError:
                delay = 2 ** (attempt + 1)
            resp.close()
            time.sleep(min(delay, 60.0))
            continue
        return resp
    raise RuntimeError("No response received after retry attempts.")

"""
        from scripts.custom_template import EXTRACTION_BODY
        script_content += "\nDIFFICULTY = " + json.dumps(report.difficulty) + "\n"
        script_content += EXTRACTION_BODY

        try:
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(script_content)
        except OSError as exc:
            raise RuntimeError(f"Failed to save generated script to disk: {exc}") from exc
            
        return script_path
