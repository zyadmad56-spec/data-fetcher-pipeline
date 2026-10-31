"""SCANNER-CLEAN acceptance tests for Subagent A's scope (Snyk policy rows 1-9).

Masters-only file (anti-gaming rule): written by Master A (orchestrator chat)
from TEST_SPEC_AGENT_A.md BEFORE reviewing the diff; reconciled against
SECURITY.md's documented endpoint list. Never modified by subagents.
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# Documented allowlist (SECURITY.md "Endpoints and allowed sources").
# api.data.gov is a key-portal REFERENCE string in datagov.py, not a fetch
# endpoint (Subagent A escalation 1) — allowlisted as a documented literal.
ALLOWED_HOSTS = {
    "api.github.com", "raw.githubusercontent.com",
    "insideairbnb.com", "www.insideairbnb.com",
    "api.gsa.gov", "api.data.gov",
    "api.worldbank.org", "ec.europa.eu",
    "api.coingecko.com",
    "api.stlouisfed.org", "fred.stlouisfed.org",
    "www.sec.gov", "data.sec.gov",
    "www.openml.org",
    "www.kaggle.com",
    "finance.yahoo.com", "query1.finance.yahoo.com",
    "vincentarelbundock.github.io",
}

SECRET_PATTERNS = [
    r"AKIA[0-9A-Z]{16}",
    r"ghp_[A-Za-z0-9]{36}",
    r"gho_[A-Za-z0-9]{36}",
    r"github_pat_[A-Za-z0-9_]{22,}",
    r"xox[baprs]-[A-Za-z0-9-]{10,}",
    r"-----BEGIN (RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----",
    r"\bsk-[A-Za-z0-9]{20,}",
]

INJECTION_PATTERNS = [
    r"ignore (all )?(previous|prior|above) instructions?",
    r"disregard (the|all|your) (previous|above|instructions)",
    r"you are now",
    r"\bact as (a|an) ",
    r"system prompt",
]

PROSE_FILES = ["SKILL.md", "README.md", "SECURITY.md"]


def _tracked_files():
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True)
    return [REPO / line for line in out.stdout.splitlines() if line.strip()]


def _script_sources():
    return {p: p.read_text(encoding="utf-8", errors="replace")
            for p in (REPO / "scripts").rglob("*.py")}


def test_a1_no_secret_patterns_anywhere():
    hits = []
    for path in _tracked_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pattern in SECRET_PATTERNS:
            for match in re.finditer(pattern, text):
                hits.append(f"{path.relative_to(REPO)}: {pattern}")
    assert hits == [], f"secret-shaped strings found: {hits}"


def test_a3_no_instruction_like_text_in_docs_and_scripts():
    hits = []
    for name in PROSE_FILES:
        text = (REPO / name).read_text(encoding="utf-8", errors="replace")
        for pattern in INJECTION_PATTERNS:
            if re.search(pattern, text, flags=re.IGNORECASE):
                hits.append(f"{name}: {pattern}")
        for match in re.finditer(r"[A-Za-z0-9+/=]{120,}", text):
            hits.append(f"{name}: long base64-like run")
    for path, text in _script_sources().items():
        rel = path.relative_to(REPO).as_posix()
        for pattern in INJECTION_PATTERNS:
            if re.search(pattern, text, flags=re.IGNORECASE):
                hits.append(f"{rel}: {pattern}")
    assert hits == [], f"instruction-like or obfuscated text found: {hits}"


def test_a5_single_dynamic_exec_site_and_no_remote_loading():
    exec_sites = []
    for path, text in _script_sources().items():
        rel = path.relative_to(REPO).as_posix()
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id in {"exec", "eval", "compile"}:
                exec_sites.append((rel, node.func.id))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "__import__":
                exec_sites.append((rel, "__import__"))
    # File-level assertion (line numbers drift as docstrings evolve): the ONLY
    # dynamic-exec site in the pipeline is the worker's compile+exec pair.
    assert exec_sites == [("scripts/isolated_worker.py", "compile"),
                          ("scripts/isolated_worker.py", "exec")], exec_sites
    for path, text in _script_sources().items():
        lowered = text.lower()
        for bad in ("| bash", "invoke-expression", "iwr -uri", "curl -fssl"):
            assert bad not in lowered, f"{path}: remote-exec pattern {bad!r}"


def test_a6_no_persistence_money_or_system_service_indicators():
    banned = ["systemctl", "schtasks", "crontab", "launchctl", "reg add",
              "setx ", "startuptask", "runkey", "telemetry", "mixpanel",
              "posthog", "segment.io", "analytics.track"]
    hits = []
    for path, text in _script_sources().items():
        rel = path.relative_to(REPO).as_posix()
        lowered = text.lower()
        for bad in banned:
            if bad in lowered:
                hits.append(f"{rel}: {bad!r}")
    assert hits == [], f"persistence/telemetry/system-service indicators: {hits}"


def test_a7_constant_urls_within_allowed_sources_and_kaggle_prefixes():
    host_hits = []
    url_re = re.compile(r"https?://([A-Za-z0-9.-]+)")
    for path, text in _script_sources().items():
        rel = path.relative_to(REPO).as_posix()
        for match in url_re.finditer(text):
            host = match.group(1).lower()
            host = host[4:] if host.startswith("www2.") else host
            if host not in ALLOWED_HOSTS and not any(
                    host.endswith("." + allowed) for allowed in ALLOWED_HOSTS):
                host_hits.append(f"{rel}: {host}")
    assert host_hits == [], f"undocumented constant URL hosts: {host_hits}"
    kaggle = (REPO / "scripts/fetchers/kaggle_fetcher.py").read_text(encoding="utf-8")
    assert "SIZE_LIMIT: Kaggle" in kaggle and "BLOCKED: Kaggle" in kaggle


def test_a8_docs_state_human_only_boundary_and_receipt_gate():
    skill = (REPO / "SKILL.md").read_text(encoding="utf-8")
    security = (REPO / "SECURITY.md").read_text(encoding="utf-8")
    combined = skill + security
    lowered = combined.lower()
    # (a) human-only content destination + data-never-instructions
    assert "data, never instructions" in lowered
    assert "_human_preview.md" in combined
    # (b) receipt gate
    assert "approve-preview" in lowered
    # (c) github csv-only scope + token bound to api.github.com
    assert "never reads issues" in lowered
    assert "api.github.com" in combined
    # (d) honest non-sandbox statement
    assert "not a sandbox" in lowered
    # allowed-sources statement exists in security doc
    assert "allowed sources" in lowered


def test_a9_no_telemetry_imports_or_endpoints():
    hits = []
    for path, text in _script_sources().items():
        rel = path.relative_to(REPO).as_posix()
        for bad in ("import telemetry", "from telemetry", "import analytics",
                    "mixpanel", "posthog", "segment.io"):
            if bad in text.lower():
                hits.append(f"{rel}: {bad}")
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert hits == [], f"telemetry indicators: {hits}"
    for bad in ("telemetry", "analytics", "mixpanel", "posthog"):
        assert bad not in pyproject.lower(), f"pyproject.toml: {bad}"


def test_a2_github_token_never_reaches_outputs(tmp_path, capsys, monkeypatch):
    """Fake token must stay out of stdout, stderr, and every written artifact."""
    from scripts.cli import main

    # Placeholder contains underscores so it can never match a real secret
    # shape in this file's own sweep (A1 scans tracked files, incl. itself).
    fake_token = "ghp_FAKE_TOKEN_PLACEHOLDER_not-a-real-credential"
    monkeypatch.setenv("GITHUB_TOKEN", fake_token)

    class _Resp:
        def __init__(self, status=403, body=b"forbidden"):
            self.status_code = status
            self.text = body.decode("utf-8", "replace")
            self.content = body
            self.headers = {}
            self.url = "https://api.github.com/search/repositories"

        def close(self):
            pass

    def transport(url, **kwargs):
        assert "api.github.com" in url
        auth = (kwargs.get("headers") or {}).get("Authorization", "")
        if auth:
            assert fake_token in auth  # header use is legitimate; nothing else may see it
        return _Resp(403, b"forbidden")

    monkeypatch.setattr("scripts.fetchers.github_data.request_with_retry", transport)
    monkeypatch.setattr("scripts.base.request_with_retry", transport)

    outdir = tmp_path / "gh"
    monkeypatch.setattr("sys.argv", [
        "data-fetcher", "--source", "github", "--query", "datasets",
        "--outdir", str(outdir), "--yes", "--non-interactive",
        "--json-output"])
    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code != 0
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    assert envelope["status"] == "error"
    for channel in (captured.out, captured.err):
        assert fake_token not in channel
    for written in outdir.rglob("*") if outdir.exists() else []:
        if written.is_file():
            assert fake_token not in written.read_text(encoding="utf-8", errors="replace")
