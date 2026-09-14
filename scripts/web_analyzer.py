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
import pandas as pd
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
        robots.txt falls back to the permissive default (fetch allowed); an
        explicitly retrieved policy is parsed and enforced.
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
            if response.status_code != 200:
                return True  # no retrievable policy → permissive default
            lines = response.text.splitlines()
            rp.parse(lines)
            return rp.can_fetch("data-fetcher-pipeline", self.url) or rp.can_fetch("*", self.url)
        except (ValueError, OSError) as exc:
            # Blocked SSRF target or network failure: permissive default keeps
            # behavior identical to an unreachable robots.txt
            logger.debug("robots.txt fetch failed (%s); defaulting to allowed.", exc)
            return True

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
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

        url = self.url
        try:
            # Follow redirects manually so every hop passes the SSRF guard
            for _ in range(4):
                response = request_with_retry(url, headers=headers, allow_redirects=False)
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location", "")
                    if not location:
                        break
                    url = urljoin(url, location)
                    validate_public_url(url)
                    report.warnings.append(f"Redirected to {url}")
                    continue
                break
        except (ConnectionError, DataFetchError) as exc:
            # request_with_retry raises coded DataFetchError on exhaustion —
            # report it as a blocked target instead of crashing the analysis
            report.difficulty = "blocked"
            report.status_code = 0
            report.warnings.append(f"Connection failed: {exc}")
            return report

        report.status_code = response.status_code
        report.content_type = response.headers.get("Content-Type", "").lower()
        
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
            return report

        # HTML parsing
        if "text/html" in report.content_type:
            soup = BeautifulSoup(response.text, "html.parser")
            
            # Check for CAPTCHA
            html_content = response.text.lower()
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

        return report

    def generate_script(self, report: AnalysisReport) -> str:
        """Generate a custom, safe extractor script based on difficulty and save to file."""
        safe_url = re.sub(r'[^a-zA-Z0-9_]', '_', self.url.split("://")[-1][:50])
        script_filename = f"scrape_{safe_url}.py"
        script_path = str(Path(self.output_dir) / script_filename)
        
        script_content = f"""# Generated Scraper Script for: {self.url}
import os
import sys
import time
import pandas as pd
import requests

# Target URL embedded as safely-serialized data, never raw interpolation
URL = {json.dumps(self.url)}
HEADERS = {{
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}}

def _fetch_with_retry(url, headers, retries=3):
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=headers, timeout=30)
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** (attempt + 1))
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == retries - 1:
                raise RuntimeError("Request failed with status " + str(resp.status_code) + " after " + str(retries) + " attempts: " + url)
            retry_after = resp.headers.get('Retry-After', '')
            try:
                delay = float(retry_after)
            except ValueError:
                delay = 2 ** (attempt + 1)
            time.sleep(delay)
            continue
        return resp

def run_extraction():
    print(f"Starting extraction for URL: {{URL}}")
"""
        
        if report.difficulty == "easy":
            script_content += """    try:
        response = _fetch_with_retry(URL, HEADERS)
        if response.status_code != 200:
            print(f"[Error] Failed to fetch data: {response.status_code}")
            sys.exit(1)
            
        output_file = "downloaded_data.csv"
        # Determine format
        if "json" in response.headers.get("Content-Type", ""):
            import json
            data = response.json()
            df = pd.json_normalize(data)
            df.to_csv(output_file, index=False)
        else:
            with open(output_file, 'wb') as f:
                f.write(response.content)
                
        print(f"[Success] Data extracted successfully to: {os.path.abspath(output_file)}")
    except (requests.RequestException, ValueError, OSError) as e:
        print(f"[Error] Execution failed: {e}")
        sys.exit(1)
"""
        elif report.difficulty == "medium":
            script_content += """    try:
        response = _fetch_with_retry(URL, HEADERS)
        if response.status_code != 200:
            print(f"[Error] Failed to fetch HTML: {response.status_code}")
            sys.exit(1)
            
        tables = pd.read_html(response.text)
        print(f"Found {len(tables)} tables on page.")
        for idx, table in enumerate(tables):
            out_file = f"table_{idx}.csv"
            table.to_csv(out_file, index=False)
            print(f"Saved table {idx} to: {os.path.abspath(out_file)}")
    except (requests.RequestException, ValueError, OSError) as e:
        print(f"[Error] Extraction failed: {e}")
        sys.exit(1)
"""
        elif report.difficulty == "hard":
            script_content += """    print("[Warning] This site requires JavaScript rendering or session authentication.")
    print("Please use standard browser orchestration frameworks like Playwright or Selenium.")
    print("Example layout:")
    print(\"\"\"
    # from playwright.sync_api import sync_playwright
    # with sync_playwright() as p:
    #     browser = p.chromium.launch(headless=True)
    #     page = browser.new_page()
    #     page.goto(URL)
    #     # interact with elements
    #     html = page.content()
    #     # extract text/tables
    #     browser.close()
    \"\"\")
"""
        else: # blocked
            script_content += """    print("[Blocked] This website is protected by anti-bot measures (Cloudflare/CAPTCHA) or disallowed by robots.txt.")
    print("Automation through scripts is blocked to protect the infrastructure and avoid IP bans.")
    sys.exit(1)
"""
            
        script_content += """
if __name__ == "__main__":
    run_extraction()
"""
        
        try:
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(script_content)
        except OSError as exc:
            raise RuntimeError(f"Failed to save generated script to disk: {exc}") from exc
            
        return script_path
