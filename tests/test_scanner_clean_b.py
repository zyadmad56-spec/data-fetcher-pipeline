"""SCANNER-CLEAN acceptance tests for Subagent B's surface (Socket taxonomy + gate integrity).

Masters-only file (anti-gaming rule): authored by the orchestrator implementing
TEST_SPEC_AGENT_B.md (spec by Master B = Gemini Argon) BEFORE/AFTER Subagent B's
diff per the campaign plan. Never modified by subagents.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

FIXTURE_EXTRACTOR = '''
import pandas as pd
def extract_frame(url=None, columns=None, table_index=None, row_limit=0):
    return pd.DataFrame({"city": ["cairo", "rome"], "pop": [9, 4]})
'''


def _scripts_sources():
    return {p: p.read_text(encoding="utf-8", errors="replace")
            for p in (REPO / "scripts").rglob("*.py")}


def _write_extractor(tmp_path: Path, source: str = FIXTURE_EXTRACTOR) -> Path:
    path = tmp_path / "extractor.py"
    path.write_text(source, encoding="utf-8")
    return path


@pytest.fixture()
def offline_analyze(monkeypatch):
    """Replace WebAnalyzer.analyze with an offline report (no network)."""
    from scripts.web_analyzer import AnalysisReport, WebAnalyzer

    def fake_analyze(self):
        return AnalysisReport(url=self.url, status_code=200, difficulty="easy",
                              robots_allowed=True, table_count=1, warnings=[])

    monkeypatch.setattr(WebAnalyzer, "analyze", fake_analyze)


@pytest.fixture()
def patched_registry(tmp_path, monkeypatch):
    """Point the source registry (incl. pending_previews) at a temp store."""
    store = tmp_path / "registry"
    monkeypatch.setattr("scripts.source_registry.registry_root", lambda: store)
    return store


def _run_cli(monkeypatch, capsys, argv):
    from scripts.cli import main
    monkeypatch.setattr("sys.argv", ["data-fetcher", *argv])
    with pytest.raises(SystemExit) as excinfo:
        main()
    captured = capsys.readouterr()
    envelope = {}
    if captured.out.strip():
        envelope = json.loads(captured.out.strip().splitlines()[-1])
    return excinfo.value.code, envelope, captured


def _cli_args(outdir: Path, extra: list[str]) -> list[str]:
    return ["--source", "custom", "--query", "https://example.com/data.csv",
            "--outdir", str(outdir), "--json-output", *extra]


# --- B1: single dynamic-exec site, no disguise --------------------------------

def test_b1_single_compile_exec_site_and_no_disguise():
    sites = []
    for path, text in _scripts_sources().items():
        rel = path.relative_to(REPO).as_posix()
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id in {"compile", "exec", "eval", "__import__"}:
                sites.append((rel, node.func.id))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr in {"compile", "exec", "eval"} \
                    and isinstance(node.func.value, ast.Name) \
                    and node.func.value.id in {"builtins", "importlib"}:
                sites.append((rel, node.func.attr))
    assert sites == [("scripts/isolated_worker.py", "compile"),
                     ("scripts/isolated_worker.py", "exec")], sites
    for rel, text in _scripts_sources().items():
        if rel == "scripts/isolated_worker.py":
            continue
        lowered = text.lower()
        for bad in ("getattr(builtins", "globals()[", "functiontype",
                    "marshal.loads", "codecs.decode", "b64decode"):
            assert bad not in lowered, f"{rel}: disguise pattern {bad!r}"
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    for bad in ("pre-install", "postinstall", "cmdclass", "setup("):
        assert bad not in pyproject, f"pyproject install hook: {bad}"


# --- B2: runpy absent from shipped code ---------------------------------------

def test_b2_runpy_absent_from_shipped_code():
    hits = []
    for rel, text in _scripts_sources().items():
        if "runpy" in text or "run_path" in text:
            hits.append(rel)
    assert hits == [], f"runpy references in shipped code: {hits}"


# --- B3: every custom full-fetch mode fails closed without a receipt ----------

def test_b3_custom_full_fetch_fails_closed_without_receipt(
        tmp_path, monkeypatch, capsys, offline_analyze, patched_registry):
    # (a) generated-template mode
    code, envelope, _ = _run_cli(monkeypatch, capsys, _cli_args(
        tmp_path / "mode-a", ["--yes", "--non-interactive"]))
    assert envelope.get("code") == "APPROVAL_REQUIRED"
    assert not list((tmp_path / "mode-a").rglob("*_raw.csv"))
    # (b) bespoke extractor mode without --approve-preview
    extractor = _write_extractor(tmp_path)
    code, envelope, _ = _run_cli(monkeypatch, capsys, _cli_args(
        tmp_path / "mode-b", ["--rows", "5", "--yes", "--non-interactive",
                              "--extractor-script", str(extractor)]))
    assert envelope.get("code") == "APPROVAL_REQUIRED"
    assert not list((tmp_path / "mode-b").rglob("*_raw.csv"))
    # (c) saved custom source without a receipt (registered via staged bytes;
    # save_source prefixes the key with custom_ itself)
    from scripts.source_registry import save_source
    save_source("unitfix", "https://example.com/data.csv",
                Path(extractor).read_bytes())
    code, envelope, _ = _run_cli(monkeypatch, capsys, _cli_args(
        tmp_path / "mode-c", ["--source", "custom_unitfix", "--rows", "5",
                              "--yes", "--non-interactive"]))
    assert envelope.get("code") == "APPROVAL_REQUIRED"
    assert not list((tmp_path / "mode-c").rglob("*_raw.csv"))


# --- B4: interactive consent ordering -----------------------------------------

def test_b4_interactive_custom_gate_requires_both_consents(
        tmp_path, monkeypatch, capsys, offline_analyze, patched_registry):
    # Interactive prompts cannot be served under --json-output (the CLI
    # rejects that combination by design), so this test drives the human
    # flow in plain output mode and judges exit code + artifacts on disk.
    from scripts.cli import main

    def run(argv):
        monkeypatch.setattr("sys.argv", ["data-fetcher", *argv])
        with pytest.raises(SystemExit) as excinfo:
            main()
        return excinfo.value.code

    def cli_args(outdir: Path, extra: list[str]) -> list[str]:
        return ["--source", "custom", "--query", "https://example.com/data.csv",
                "--outdir", str(outdir), *extra]

    extractor = _write_extractor(tmp_path)

    # (a) declining transfer consent runs nothing and aborts
    monkeypatch.setattr("builtins.input", lambda *a, **k: "n")
    code = run(cli_args(tmp_path / "consent-a",
                        ["--rows", "3", "--extractor-script", str(extractor)]))
    assert code != 0
    assert not list((tmp_path / "consent-a").rglob("*_raw.csv"))
    assert not list(Path(patched_registry, "pending_previews").glob("*.py"))

    # (b) consenting to transfer but declining the sample: only the bounded
    # preview runs (a receipt is staged), the full fetch never happens.
    answers = iter(["y", "n"])
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
    code = run(cli_args(tmp_path / "consent-b",
                        ["--rows", "3", "--extractor-script", str(extractor)]))
    assert code != 0
    assert not list((tmp_path / "consent-b").rglob("*_raw.csv"))
    assert len(list(Path(patched_registry, "pending_previews").glob("*.py"))) == 1

    # (c) consenting to every prompt (transfer consent, sample approval, and
    # the full-fetch pre-flight authorization) runs the full fetch and saves
    # the CSV; the retention decision is deferred (--defer-source-choice).
    monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
    monkeypatch.setattr("sys.argv", ["data-fetcher", *cli_args(
        tmp_path / "consent-c",
        ["--rows", "3", "--extractor-script", str(extractor),
         "--defer-source-choice"])])
    from scripts.cli import main as cli_main
    try:
        cli_main()
        code = 0
    except SystemExit as exit_exc:
        code = exit_exc.code or 0
    assert code == 0
    saved = list((tmp_path / "consent-c").rglob("*_raw.csv"))
    assert len(saved) == 1


# --- B5: CLI custom flag matrix blocks bypass combinations ---------------------

def test_b5_cli_custom_flag_matrix_blocks_bypass_combinations():
    from scripts.cli import _validate_fetch_options
    from scripts.errors import DataFetchError

    def make_args(**overrides):
        defaults = dict(source="custom", query="https://example.com/x.csv",
                        outdir="out", preview_only=False, analyze_only=False,
                        yes=True, non_interactive=True, json_output=True,
                        rows=0, table=None, columns="", goal=None,
                        output_format=None, convert=False, overwrite=False,
                        list_sources=False, save_source=False,
                        defer_source_choice=False, keep_source=False,
                        discard_source=False, extractor_script=None,
                        approve_preview=None)
        defaults.update(overrides)
        from types import SimpleNamespace
        return SimpleNamespace(**defaults)

    combos = [
        dict(extractor_script="x.py", approve_preview="abc"),
        dict(approve_preview="abc", preview_only=True),
        dict(approve_preview="abc", analyze_only=True),
        dict(approve_preview="abc", source="worldbank"),
        dict(extractor_script="x.py", analyze_only=True),
        dict(extractor_script="x.py", source="worldbank"),
        dict(extractor_script="x.py", source="custom_unitfix"),
        dict(save_source=True, preview_only=True),
        dict(save_source=True, analyze_only=True),
        dict(save_source=True, source="worldbank"),
        dict(defer_source_choice=True, preview_only=True),
        dict(defer_source_choice=True, save_source=True),
        dict(table=-1),
    ]
    for overrides in combos:
        with pytest.raises(DataFetchError) as excinfo:
            _validate_fetch_options(make_args(**overrides))
        assert excinfo.value.code == "ARG_MISSING", overrides


# --- B6: receipt drift, tamper, expiry, byte-lock ------------------------------

def test_b6_receipt_drift_tamper_expiry_and_staged_byte_lock(
        tmp_path, monkeypatch, offline_analyze, patched_registry):
    from scripts.adapters.extractor_protocol import PreviewReceipt, sha256_bytes
    from scripts.source_registry import (
        load_preview_receipt, save_preview_receipt)

    store = patched_registry

    extractor = _write_extractor(tmp_path)
    script_bytes = extractor.read_bytes()
    receipt = PreviewReceipt(preview_id=PreviewReceipt.new_id(),
                             script_sha256=sha256_bytes(script_bytes),
                             canonical_url="https://example.com/data.csv",
                             columns=(), table_index=None)
    save_preview_receipt(receipt, script_bytes)

    # (a) deleting/mutating the ORIGINAL extractor file does not matter:
    # the staged bytes execute cleanly (byte-lock).
    receipt_loaded, staged = load_preview_receipt(receipt.preview_id)
    assert staged == script_bytes

    # (b) tampering with the store entry fails closed: the loader re-verifies
    # the inner preview ID and the staged-byte hash (URL drift inside the JSON
    # is caught downstream as APPROVAL_MISMATCH at fetch time).
    stored_json = next((store / "pending_previews").glob("*.json"))
    stored_json.write_text(stored_json.read_text(encoding="utf-8").replace(
        receipt.preview_id, "0" * 32), encoding="utf-8")
    with pytest.raises(Exception) as excinfo:
        load_preview_receipt(receipt.preview_id)
    assert getattr(excinfo.value, "code", "") == "PREVIEW_TAMPERED"

    # (c) expiry fails closed
    receipt2 = PreviewReceipt(preview_id=PreviewReceipt.new_id(),
                              script_sha256=sha256_bytes(script_bytes),
                              canonical_url="https://example.com/data.csv",
                              columns=(), table_index=None)
    save_preview_receipt(receipt2, script_bytes)
    import scripts.source_registry as registry
    entry = next((store / "pending_previews").glob(
        receipt2.preview_id + "*.json"))
    data = json.loads(entry.read_text(encoding="utf-8"))
    receipt_data = data.get("receipt", data)
    receipt_data["created_at"] = "2020-01-01T00:00:00+00:00"
    if "receipt" in data:
        data["receipt"] = receipt_data
    else:
        data = receipt_data
    entry.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(Exception) as excinfo:
        load_preview_receipt(receipt2.preview_id)
    assert getattr(excinfo.value, "code", "") == "PREVIEW_EXPIRED"

    # (d) URL drift between receipt and fetch → APPROVAL_MISMATCH (unit seam);
    # a fresh receipt is used because (b) permanently tampered receipt one.
    from scripts.custom_source import CustomFetcher
    receipt3 = PreviewReceipt(preview_id=PreviewReceipt.new_id(),
                              script_sha256=sha256_bytes(script_bytes),
                              canonical_url="https://example.com/data.csv",
                              columns=(), table_index=None)
    save_preview_receipt(receipt3, script_bytes)
    fetcher = CustomFetcher("https://example.com/other.csv", str(tmp_path / "o"),
                            {}, extractor_bytes=script_bytes,
                            approved_receipt=receipt3.preview_id)
    fetcher.script_bytes = script_bytes
    fetcher.script_sha256 = sha256_bytes(script_bytes)
    with pytest.raises(Exception) as excinfo:
        fetcher.extract()
    assert getattr(excinfo.value, "code", "") == "APPROVAL_MISMATCH"


# --- B7: AST gate + worker hash re-check ---------------------------------------

def test_b7_ast_gate_and_worker_hash_recheck(tmp_path):
    from scripts.adapters.extractor_protocol import WorkerRequest, sha256_bytes
    from scripts.adapters.script_runner import (
        ExtractorRejectedError, SubprocessExtractorRunner, inspect_extractor_ast)

    with pytest.raises(ExtractorRejectedError):
        inspect_extractor_ast(b"def not_extract_frame():\n    pass\n")
    for banned in (b"import subprocess\n", b"import ctypes\n",
                   b"eval('1')\n", b"exec('1')\n", b"compile('1','x','eval')\n",
                   b"__import__('os')\n", b"import os\nos.system('echo')\n",
                   b"import os\nos.popen('echo')\n"):
        with pytest.raises(ExtractorRejectedError):
            inspect_extractor_ast(b"def extract_frame(url=None, columns=None, "
                                  b"table_index=None, row_limit=0):\n    pass\n"
                                  + banned)
    ok = b"import pandas as pd\ndef extract_frame(url=None, columns=None, " \
         b"table_index=None, row_limit=0):\n    return pd.DataFrame({'a': [1]})\n"
    inspect_extractor_ast(ok)  # must not raise

    runner = SubprocessExtractorRunner()
    request = WorkerRequest(script_sha256="0" * 64, query_url="https://example.com",
                            columns=(), table_index=None, row_limit=5,
                            max_bytes=50 * 1024 * 1024, deadline_sec=30.0,
                            allowed_origin="https://example.com")
    with pytest.raises(Exception) as excinfo:
        runner.extract(ok, request)
    # The runner re-verifies the request hash against the staged bytes before
    # spawning; the worker re-checks again inside (defense in depth).
    assert "staged extractor bytes" in str(excinfo.value).lower() or \
        "SCRIPT_HASH_MISMATCH" in str(excinfo.value)


# --- B8: worker audit hooks + IP parity ----------------------------------------

def test_b8_worker_audit_hooks_and_public_ip_parity(tmp_path):
    from scripts.isolated_worker import _is_public_ip
    from scripts.net_guard import is_public_ip
    vectors = ["127.0.0.1", "10.0.0.1", "169.254.169.254", "100.64.0.1",
               "100.100.100.200", "224.0.0.1", "::ffff:127.0.0.1",
               "::ffff:169.254.169.254", "8.8.8.8", "1.1.1.1",
               "2606:4700::1111"]
    for vector in vectors:
        import ipaddress
        addr = ipaddress.ip_address(vector)
        assert _is_public_ip(addr) == is_public_ip(addr), vector

    from scripts.adapters.extractor_protocol import WorkerRequest, sha256_bytes
    from scripts.adapters.script_runner import SubprocessExtractorRunner
    # Each evasion attempt gets its own extractor and lets the audit-hook
    # exception propagate — the worker must classify it WORKER_POLICY_VIOLATION.
    # Usage-based evasion: importing an already-cached module is a silent
    # no-op (no audit event, no code executes), so the vector must USE the
    # dangerous capability — subprocess.Popen fires its audit event.
    importlib_evasion = (
        "import importlib\n"
        "def extract_frame(url=None, columns=None, table_index=None, row_limit=0):\n"
        "    importlib.import_module('subprocess').Popen('echo hi')\n"
    )
    connect_evasion = (
        "import socket\n"
        "def extract_frame(url=None, columns=None, table_index=None, row_limit=0):\n"
        "    socket.socket().connect(('100.64.0.1', 80))\n"
    )
    runner = SubprocessExtractorRunner()
    for name, source in (("evader_import.py", importlib_evasion),
                         ("evader_connect.py", connect_evasion)):
        path = tmp_path / name
        path.write_text(source, encoding="utf-8")
        result, _ = runner.extract(path.read_bytes(), WorkerRequest(
            script_sha256=sha256_bytes(path.read_bytes()),
            query_url="https://example.com", columns=(), table_index=None,
            row_limit=5, max_bytes=50 * 1024 * 1024, deadline_sec=30.0,
            allowed_origin="https://example.com"))
        assert result.error_code == "WORKER_POLICY_VIOLATION", (name, result.error_code)


# --- B9: write confinement, read roots, scrubbed env ---------------------------

def test_b9_worker_write_confinement_read_roots_and_scrubbed_env(tmp_path):
    from scripts.isolated_worker import _WorkspacePolicy
    from scripts.adapters.extractor_protocol import WorkerRequest, sha256_bytes
    from scripts.adapters.script_runner import SubprocessExtractorRunner

    workspace = tmp_path / "ws"
    workspace.mkdir()
    policy = _WorkspacePolicy(workspace)
    real_workspace = str(workspace.resolve())
    assert str(Path(policy.read_roots[0]).resolve()) == real_workspace
    assert os_devnull_in(policy)

    probe = (
        "import os\n"
        "def extract_frame(url=None, columns=None, table_index=None, row_limit=0):\n"
        "    import pandas as pd\n"
        "    leaked = [k for k in os.environ if k in ('GITHUB_TOKEN', "
        "'AWS_SECRET_ACCESS_KEY', 'KAGGLE_KEY', 'API_KEY', 'AUTH_TOKEN')]\n"
        "    return pd.DataFrame({'home': [os.environ.get('HOME', '') or "
        "os.environ.get('USERPROFILE', '')], 'leaked': [','.join(leaked)]})\n"
    )
    path = tmp_path / "probe.py"
    path.write_text(probe, encoding="utf-8")
    runner = SubprocessExtractorRunner()
    monkey_secrets = {"GITHUB_TOKEN": "ghp_x", "AWS_SECRET_ACCESS_KEY": "z",
                      "KAGGLE_KEY": "k", "API_KEY": "a", "AUTH_TOKEN": "t"}
    import os
    for key, value in monkey_secrets.items():
        os.environ[key] = value
    try:
        result, frame = runner.extract(path.read_bytes(), WorkerRequest(
            script_sha256=sha256_bytes(path.read_bytes()),
            query_url="https://example.com", columns=(), table_index=None,
            row_limit=5, max_bytes=50 * 1024 * 1024, deadline_sec=60.0,
            allowed_origin="https://example.com"))
    finally:
        for key in monkey_secrets:
            os.environ.pop(key, None)
    assert result.error_code is None
    assert frame is not None
    assert frame["leaked"].iloc[0] == ""
    # HOME/USERPROFILE are redirected into the runner's disposable sandbox
    # workspace (dfp-worker-*), never the real user profile.
    home_value = str(frame["home"].iloc[0])
    assert "dfp-worker" in home_value and home_value.endswith("home")
    real_home = str(Path.home())
    assert not home_value.startswith(real_home[: len(real_home) // 2]) or \
        "dfp-worker" in home_value


def os_devnull_in(policy):
    return any(str(root) == str(Path(os_devnull())) for root in policy.read_roots)


def os_devnull():
    import os
    return os.devnull


# --- B10: output caps, deadline, error classification --------------------------

def test_b10_worker_output_caps_deadline_and_classified_codes(tmp_path):
    from scripts.adapters.extractor_protocol import WorkerRequest, sha256_bytes
    from scripts.adapters.script_runner import SubprocessExtractorRunner

    runner = SubprocessExtractorRunner()
    flood = ("def extract_frame(url=None, columns=None, table_index=None, row_limit=0):\n"
             "    print('x' * (256 * 1024))\n"
             "    import sys\n"
             "    sys.stderr.write('y' * (256 * 1024))\n"
             "    import pandas as pd\n"
             "    return pd.DataFrame({'a': [1]})\n")
    path = tmp_path / "flood.py"
    path.write_text(flood, encoding="utf-8")
    result, frame = runner.extract(path.read_bytes(), WorkerRequest(
        script_sha256=sha256_bytes(path.read_bytes()),
        query_url="https://example.com", columns=(), table_index=None,
        row_limit=5, max_bytes=50 * 1024 * 1024, deadline_sec=60.0,
        allowed_origin="https://example.com"))
    assert result.error_code is None
    assert len(runner.last_stdout) <= 64 * 1024
    assert len(runner.last_stderr) <= 64 * 1024

    slow = ("import time\n"
            "def extract_frame(url=None, columns=None, table_index=None, row_limit=0):\n"
            "    time.sleep(30)\n")
    path2 = tmp_path / "slow.py"
    path2.write_text(slow, encoding="utf-8")
    result, _ = runner.extract(path2.read_bytes(), WorkerRequest(
        script_sha256=sha256_bytes(path2.read_bytes()),
        query_url="https://example.com", columns=(), table_index=None,
        row_limit=5, max_bytes=50 * 1024 * 1024, deadline_sec=0.5,
        allowed_origin="https://example.com"))
    assert result.error_code == "WORKER_TIMEOUT"

    for message, expected in (("SIZE_LIMIT: too big", "SIZE_LIMIT"),
                              ("BLOCKED: nope", "BLOCKED_URL"),
                              ("NOT_FOUND: gone", "NOT_FOUND")):
        raiser = ("def extract_frame(url=None, columns=None, table_index=None, "
                  "row_limit=0):\n"
                  "    raise ValueError(" + repr(message) + ")\n")
        path3 = tmp_path / (expected + ".py")
        path3.write_text(raiser, encoding="utf-8")
        result, _ = runner.extract(path3.read_bytes(), WorkerRequest(
            script_sha256=sha256_bytes(path3.read_bytes()),
            query_url="https://example.com", columns=(), table_index=None,
            row_limit=5, max_bytes=50 * 1024 * 1024, deadline_sec=30.0,
            allowed_origin="https://example.com"))
        assert result.error_code == expected, (message, result.error_code)


# --- B11: documentation honesty ------------------------------------------------

def test_b11_b_surface_docstrings_state_envelope_and_never_claim_sandbox():
    worker = (REPO / "scripts/isolated_worker.py").read_text(encoding="utf-8")
    custom = (REPO / "scripts/custom_source.py").read_text(encoding="utf-8")
    combined = worker + custom
    lowered = combined.lower()
    for required in ("approve-preview", "approval_required", "approval_mismatch",
                     "sha-256", "ast gate", "pep 578", "defense-in-depth",
                     "fail-fast"):
        assert required in lowered, f"missing documentation: {required}"
    assert "not hostile-code containment" in lowered or \
        "not a sandbox" in lowered
    assert "lands in a later wave" not in lowered
    for occurrence_start in [m.start() for m in
                             __import__("re").finditer(r"sandbox", lowered)]:
        window = lowered[max(0, occurrence_start - 40):occurrence_start + 40]
        assert "not a sandbox" in window or "no sandbox" in window, \
            f"unnegated sandbox mention: ...{window}..."


# --- B12: capability stability --------------------------------------------------

def test_b12_b_surface_capabilities_and_cli_flags_unchanged():
    import inspect as inspect_mod
    sys.path.insert(0, str(REPO))
    from scripts.cli import _build_argument_parser
    parser = _build_argument_parser()
    options = {a for a in parser._option_string_actions if a not in ("-h", "--help")}
    assert len(options) == 22, sorted(options)

    from scripts.custom_source import CustomFetcher
    params = list(inspect_mod.signature(CustomFetcher.__init__).parameters)
    assert params == ["self", "query", "outdir", "config", "source",
                      "descriptor", "extractor_bytes", "approved_receipt"]
    from scripts.isolated_worker import main as worker_main
    assert list(inspect_mod.signature(worker_main).parameters) == ["argv"]

    for rel, text in _scripts_sources().items():
        if rel not in ("scripts/isolated_worker.py", "scripts/custom_source.py"):
            continue
        assert "os.environ" not in text and "os.getenv" not in text, rel
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) \
                    else [node.module or ""]
                for name in names:
                    assert name.split(".")[0] not in {
                        "runpy", "pickle", "marshal", "ctypes", "multiprocessing"}, rel
