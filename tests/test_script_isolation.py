"""Phase 4/8 acceptance: AST gate, isolated worker, registry byte integrity.

Every subprocess test here exercises the REAL worker boundary: a fresh
``python -I`` process with scrubbed environment, audit hooks and capped
capture. These are fail-fast/defense-in-depth verifications, never sandbox
proofs (see scripts/isolated_worker.py's honest scope note).
"""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from scripts.adapters.extractor_protocol import (
    WorkerRequest, WorkerResult, sha256_bytes,
)
from scripts.adapters.script_runner import (
    SubprocessExtractorRunner, _sandboxed_env, inspect_extractor_ast,
)
from scripts.errors import DataFetchError

DEADLINE = 30.0


def make_request(script: bytes, deadline: float = DEADLINE, **overrides) -> WorkerRequest:
    fields = {
        'script_sha256': sha256_bytes(script),
        'query_url': 'https://example.com/dataset.csv',
        'columns': (),
        'table_index': None,
        'row_limit': 0,
        'deadline_sec': deadline,
        'allowed_origin': '',
    }
    fields.update(overrides)
    return WorkerRequest(**fields)


def run_extractor(script: bytes, deadline: float = DEADLINE, **overrides):
    runner = SubprocessExtractorRunner()
    request = make_request(script, deadline=deadline, **overrides)
    result = runner.run(script, request)
    return runner, result


def extractor(body: str) -> bytes:
    return textwrap.dedent(body).encode('utf-8')


def wrapped(snippet: str) -> bytes:
    return extractor(f"""
        import pandas as pd
        {snippet}
        def extract_frame(url=None, columns=None, table_index=None, row_limit=0):
            return pd.DataFrame({{'v': [1]}})
    """)


def decode_result(runner: SubprocessExtractorRunner, result: WorkerResult):
    assert result.payload_csv, result.to_json()
    from scripts.adapters.extractor_protocol import decode_frame
    return decode_frame(runner.last_payload_csv, runner.last_payload_sidecar)


TRIVIAL = extractor("""
    import pandas as pd
    def extract_frame(url=None, columns=None, table_index=None, row_limit=0):
        return pd.DataFrame({'v': [1, 2, 3]})
""")


# ---------------------------------------------------------------------------
# Static AST gate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('snippet', [
    'import subprocess',
    'import ctypes',
    'import _ctypes',
    'from subprocess import run',
    'from ctypes import c_int',
    'import multiprocessing',
    'import pty',
    'import webbrowser',
    'import socketserver',
    'value = eval("1+1")',
    'value = exec("1+1")',
    'value = compile("1+1", "<s>", "eval")',
    'value = __import__("os")',
])
def test_ast_gate_rejects_banned_modules_and_calls(snippet):
    with pytest.raises(DataFetchError) as caught:
        inspect_extractor_ast(wrapped(snippet))
    assert caught.value.code == 'EXTRACTOR_REJECTED'


@pytest.mark.parametrize('call', [
    'os.system("x")', 'os.popen("x")', 'os.execv("/bin/ls", [])',
    'os.execvp("ls", ["ls"])', 'os.spawnl(0, "ls")', 'os.posix_spawn("ls", [], {})',
])
def test_ast_gate_rejects_os_process_calls(call):
    with pytest.raises(DataFetchError) as caught:
        inspect_extractor_ast(wrapped('import os\n' + call))
    assert caught.value.code == 'EXTRACTOR_REJECTED'


def test_ast_gate_requires_top_level_extract_frame():
    with pytest.raises(DataFetchError) as caught:
        inspect_extractor_ast(extractor("""
            import pandas as pd
            def other():
                def extract_frame(url=None):
                    return pd.DataFrame()
        """))
    assert caught.value.code == 'EXTRACTOR_REJECTED'
    with pytest.raises(DataFetchError):
        inspect_extractor_ast(b'import pandas\n')


def test_ast_gate_is_a_review_aid_not_containment_by_contract():
    doc = inspect_extractor_ast.__doc__ or ''
    assert 'fail-fast' in doc and 'not a sandbox' in doc
    # Documented bypass: an aliased module hides os.system from the static gate.
    aliased = extractor("""
        import os as platform
        import pandas as pd
        def extract_frame(url=None, columns=None, table_index=None, row_limit=0):
            platform.system('echo hi')
            return pd.DataFrame({'v': [1]})
    """)
    inspect_extractor_ast(aliased)  # no raise: static analysis cannot see it
    # Documented bypass: dynamic attribute construction is equally invisible.
    dynamic = wrapped("""
        import importlib
        target = getattr(importlib.import_module('os'), 'get' + 'cwd')
    """)
    inspect_extractor_ast(dynamic)  # no raise; the runtime hook is the second layer


def test_ast_gate_rejects_syntax_errors_and_non_utf8():
    with pytest.raises(DataFetchError) as caught:
        inspect_extractor_ast(b'def broken(:\n')
    assert caught.value.code == 'EXTRACTOR_REJECTED'
    with pytest.raises(DataFetchError):
        inspect_extractor_ast(b'\xff\xfe\x00bad')


def test_generated_template_bytes_pass_the_ast_gate(tmp_path):
    from scripts.web_analyzer import AnalysisReport, WebAnalyzer
    analyzer = WebAnalyzer('https://example.com/data.csv', str(tmp_path))
    path = analyzer.generate_script(AnalysisReport(url=analyzer.url, difficulty='easy'))
    script_bytes = Path(path).read_bytes()
    inspect_extractor_ast(script_bytes)  # must not raise


# ---------------------------------------------------------------------------
# Runner contract
# ---------------------------------------------------------------------------

def test_runner_rejects_hash_mismatch_and_bad_request():
    runner = SubprocessExtractorRunner()
    request = make_request(TRIVIAL)
    bogus = WorkerRequest(**{**request.to_json(), 'script_sha256': 'f' * 64})
    with pytest.raises(DataFetchError):
        runner.run(TRIVIAL, bogus)
    malformed = WorkerRequest(**{**request.to_json(), 'row_limit': -3})
    with pytest.raises(DataFetchError):
        runner.run(TRIVIAL, malformed)


def test_runner_defaults_point_at_the_real_worker():
    runner = SubprocessExtractorRunner()
    assert runner.worker_path.is_file()
    assert runner.worker_path.name == 'isolated_worker.py'


def test_sandboxed_env_strips_credentials_and_redirects_home(tmp_path):
    env = _sandboxed_env(tmp_path)
    assert 'home' in str(env['HOME']) and 'home' in str(env['USERPROFILE'])
    assert str(tmp_path / 'tmp') == env['TMP'] == env['TEMP']
    assert env['OMP_NUM_THREADS'] == env['OPENBLAS_NUM_THREADS'] == env['MKL_NUM_THREADS'] == '1'
    with patch.dict(os.environ, {
        'GITHUB_TOKEN': 'canary', 'KAGGLE_KEY': 'canary', 'AWS_SECRET_ACCESS_KEY': 'canary',
        'MY_SERVICE_PASSWORD': 'canary', 'FRED_API_KEY': 'canary', 'SAFE_VAR': 'keepme',
    }):
        scrubbed = _sandboxed_env(tmp_path)
    assert 'GITHUB_TOKEN' not in scrubbed and 'KAGGLE_KEY' not in scrubbed
    assert 'AWS_SECRET_ACCESS_KEY' not in scrubbed and 'MY_SERVICE_PASSWORD' not in scrubbed
    assert 'FRED_API_KEY' not in scrubbed and scrubbed['SAFE_VAR'] == 'keepme'


# ---------------------------------------------------------------------------
# Isolated worker: real subprocess acceptance
# ---------------------------------------------------------------------------

def test_valid_extractor_round_trips_faithfully_through_the_worker():
    script = extractor("""
        import pandas as pd
        def extract_frame(url=None, columns=None, table_index=None, row_limit=0):
            left = pd.Series(['x', '', None, 'a,comma'], name='dup')
            right = pd.Series([9223372036854775807, 2, 3, 4], name='dup')
            return pd.concat([left, right], axis=1)
    """)
    runner, result = run_extractor(script)
    assert result.error_code is None, result.error_summary
    assert result.rows == 4 and result.payload_csv == 'output.csv'
    frame = decode_result(runner, result)
    assert list(frame.columns) == ['dup', 'dup']
    assert pd.isna(frame.iloc[2, 0]) and frame.iloc[1, 0] == ''
    assert frame.iloc[0, 1] == 9223372036854775807


def test_parent_env_secrets_absent_and_home_resides_in_sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv('DFP_TEST_SECRET_TOKEN', 'canary-token')
    monkeypatch.setenv('DFP_TEST_API_KEY', 'canary-key')
    real_home = str(Path.home())
    script = extractor("""
        import os
        from pathlib import Path
        import pandas as pd
        def extract_frame(url=None, columns=None, table_index=None, row_limit=0):
            variables = '\\n'.join(f'{k}={v}' for k, v in sorted(os.environ.items()))
            return pd.DataFrame({'env': [variables], 'home': [str(Path.home())]})
    """)
    runner, result = run_extractor(script)
    assert result.error_code is None, result.error_summary
    frame = decode_result(runner, result)
    blob = frame['env'].iloc[0]
    assert 'canary-token' not in blob and 'canary-key' not in blob
    assert 'DFP_TEST_SECRET_TOKEN' not in blob and 'DFP_TEST_API_KEY' not in blob
    assert 'OMP_NUM_THREADS=1' in blob
    child_home = frame['home'].iloc[0]
    assert child_home != real_home
    assert child_home.replace('/', os.sep).endswith(os.sep + 'home')
    assert 'dfp-worker-' in child_home


def test_reading_real_config_json_path_fails_closed():
    config_path = Path.home() / '.config' / 'data-fetcher-pipeline' / 'config.json'
    script = extractor(f"""
        import pandas as pd
        def extract_frame(url=None, columns=None, table_index=None, row_limit=0):
            with open({str(config_path)!r}, encoding='utf-8') as stream:
                return pd.DataFrame({{'content': [stream.read()[:200]]}})
    """)
    runner, result = run_extractor(script)
    assert result.error_code == 'WORKER_POLICY_VIOLATION', result.to_json()
    assert not runner.last_payload_csv


@pytest.mark.parametrize('attack_script', [
    extractor("""
        import importlib
        import pandas as pd
        def extract_frame(url=None, columns=None, table_index=None, row_limit=0):
            subprocess = importlib.import_module('subprocess')
            subprocess.Popen(['echo', 'pwned'])
            return pd.DataFrame({'v': [1]})
    """),
    extractor("""
        import os
        import importlib
        import pandas as pd
        def extract_frame(url=None, columns=None, table_index=None, row_limit=0):
            ctypes = importlib.import_module('ctypes')
            # ctypes is typically already cached by numpy; usage events still fire.
            ctypes.CDLL('user32.dll' if os.name == 'nt' else 'libc.so.6')
            return pd.DataFrame({'v': [1]})
    """),
])
def test_runtime_hooks_block_ast_bypass_attempts(attack_script):
    inspect_extractor_ast(attack_script)  # bypasses the static gate by construction
    runner, result = run_extractor(attack_script)
    assert result.error_code == 'WORKER_POLICY_VIOLATION', result.to_json()
    assert not runner.last_payload_csv


def test_runtime_hook_blocks_direct_os_system_via_getattr():
    script = extractor("""
        import os
        import pandas as pd
        def extract_frame(url=None, columns=None, table_index=None, row_limit=0):
            hidden = getattr(os, 'sys' + 'tem')
            hidden('echo pwned')
            return pd.DataFrame({'v': [1]})
    """)
    runner, result = run_extractor(script)
    assert result.error_code == 'WORKER_POLICY_VIOLATION', result.to_json()


@pytest.mark.parametrize('target', ['127.0.0.1', '169.254.169.254', '100.100.100.200'])
def test_non_public_connect_targets_blocked(target):
    script = extractor(f"""
        import socket
        import pandas as pd
        def extract_frame(url=None, columns=None, table_index=None, row_limit=0):
            socket.create_connection(({target!r}, 80), timeout=2)
            return pd.DataFrame({{'v': [1]}})
    """)
    runner, result = run_extractor(script)
    assert result.error_code == 'WORKER_POLICY_VIOLATION', result.to_json()


def test_mapped_ipv4_loopback_connect_blocked():
    script = extractor("""
        import socket
        import pandas as pd
        def extract_frame(url=None, columns=None, table_index=None, row_limit=0):
            socket.create_connection(('::ffff:127.0.0.1', 80), timeout=2)
            return pd.DataFrame({'v': [1]})
    """)
    runner, result = run_extractor(script)
    assert result.error_code == 'WORKER_POLICY_VIOLATION', result.to_json()


def test_stdout_flood_is_capped_not_buffered():
    script = extractor("""
        import sys
        import pandas as pd
        def extract_frame(url=None, columns=None, table_index=None, row_limit=0):
            blob = 'A' * 65536
            for _ in range(160):  # ~10 MB in total
                sys.stdout.write(blob)
            sys.stdout.flush()
            return pd.DataFrame({'v': [1]})
    """)
    runner, result = run_extractor(script)
    assert result.error_code is None, result.error_summary
    assert len(runner.last_stdout) <= 64 * 1024
    assert result.rows == 1


def test_infinite_loop_is_killed_at_the_wall_clock_deadline():
    script = extractor("""
        import pandas as pd
        def extract_frame(url=None, columns=None, table_index=None, row_limit=0):
            while True:
                pass
    """)
    runner, result = run_extractor(script, deadline=6.0)
    assert result.error_code == 'WORKER_TIMEOUT', result.to_json()
    assert not runner.last_payload_csv


def test_worker_crash_is_reported_not_fatal_to_the_host():
    script = extractor("""
        import pandas as pd
        def extract_frame(url=None, columns=None, table_index=None, row_limit=0):
            raise RuntimeError('exploded')
    """)
    runner, result = run_extractor(script)
    assert result.error_code == 'EXTRACTION_FAILED'
    assert 'exploded' in (result.error_summary or '')


def test_worker_result_cannot_smuggle_approval_fields(tmp_path):
    (tmp_path / 'result.json').write_text(json.dumps({'approved': True}))
    runner = SubprocessExtractorRunner()
    result = runner._collect(tmp_path, timed_out=False, deadline=1.0)
    assert result.error_code == 'WORKER_RESULT_INVALID'


def test_worker_rejects_staged_bytes_that_do_not_match_the_job(tmp_path):
    job = {'script_sha256': sha256_bytes(TRIVIAL), 'query_url': 'https://example.com/d.csv',
           'columns': [], 'table_index': None, 'row_limit': 0, 'max_bytes': 52428800,
           'deadline_sec': 30.0, 'allowed_origin': ''}
    (tmp_path / 'job.json').write_text(json.dumps(job))
    (tmp_path / 'extractor.py').write_bytes(TRIVIAL + b'\n# appended after checksum')
    worker = SubprocessExtractorRunner().worker_path
    subprocess.run([sys.executable, '-I', str(worker), str(tmp_path / 'job.json')],
                   capture_output=True, timeout=90, cwd=str(tmp_path), check=False)
    result = json.loads((tmp_path / 'result.json').read_text(encoding='utf-8'))
    assert result['error_code'] == 'SCRIPT_HASH_MISMATCH'


# ---------------------------------------------------------------------------
# Phase 8: registry byte integrity, origin pinning, receipt store
# ---------------------------------------------------------------------------

def register(tmp_path, name='Portal', url='https://example.com/data.csv'):
    """Register a generated template extractor under an isolated registry root."""
    from scripts.source_registry import save_source
    from scripts.web_analyzer import AnalysisReport, WebAnalyzer
    analyzer = WebAnalyzer(url, str(tmp_path / 'gen'))
    path = analyzer.generate_script(AnalysisReport(url=url, difficulty='easy'))
    with patch('scripts.web_analyzer.validate_public_url'):
        return save_source(name, url, Path(path))


@pytest.fixture
def registry_root(tmp_path):
    with patch('scripts.source_registry.registry_root', return_value=tmp_path / 'config'):
        yield tmp_path / 'config'


def test_verified_script_bytes_reads_once_and_returns_the_buffer(registry_root, tmp_path):
    from scripts.source_registry import load_sources, verified_script_bytes
    register(tmp_path)
    entry = load_sources()['custom_portal']
    script_dir = registry_root / 'source_scripts'
    reads = []
    real_read = Path.read_bytes

    def counting_read(self):
        if self.parent == script_dir:
            reads.append(str(self))
        return real_read(self)

    with patch.object(Path, 'read_bytes', counting_read):
        path, data = verified_script_bytes(entry)
    assert len(reads) == 1  # single read: no check-then-use window
    assert sha256_bytes(data) == entry['sha256']
    assert path.is_file()


def test_verified_script_bytes_fails_closed_on_swapped_bytes(registry_root, tmp_path):
    from scripts.errors import DataFetchError
    from scripts.source_registry import load_sources, verified_script_bytes
    register(tmp_path)
    entry = load_sources()['custom_portal']
    target = registry_root / 'source_scripts' / 'custom_portal.py'
    target.write_bytes(b'def extract_frame():\n    return None\n')
    with pytest.raises(DataFetchError) as caught:
        verified_script_bytes(entry)
    assert caught.value.code == 'REGISTRY_INVALID'


def test_verified_script_bytes_refuses_symlinked_targets(registry_root, tmp_path):
    from scripts.errors import DataFetchError
    from scripts.source_registry import load_sources, verified_script_bytes
    register(tmp_path)
    entry = load_sources()['custom_portal']
    managed = registry_root / 'source_scripts' / 'custom_portal.py'
    secret = tmp_path / 'elsewhere.py'
    secret.write_bytes(managed.read_bytes())
    try:
        managed.unlink()
        managed.symlink_to(secret)
    except OSError:
        pytest.skip('symlink creation unavailable on this platform/account')
    with pytest.raises(DataFetchError) as caught:
        verified_script_bytes(entry)
    assert caught.value.code == 'REGISTRY_INVALID'


def test_symlink_refusal_logic_holds_without_real_symlinks(registry_root, tmp_path):
    """Windows accounts without symlink privilege still get the check, via patching."""
    from scripts.errors import DataFetchError
    from scripts.source_registry import load_sources, verified_script_bytes
    register(tmp_path)
    entry = load_sources()['custom_portal']

    def fake_symlink(self):
        return self.name in ('custom_portal.py', 'source_scripts')

    with patch.object(Path, 'is_symlink', fake_symlink):
        with pytest.raises(DataFetchError) as caught:
            verified_script_bytes(entry)
        assert caught.value.code == 'REGISTRY_INVALID'


def test_legacy_ast_failing_script_is_quarantined_not_deleted(registry_root, tmp_path):
    from scripts.errors import DataFetchError
    from scripts.source_registry import load_sources, verified_script_bytes
    register(tmp_path)
    entry = load_sources()['custom_portal']
    target = registry_root / 'source_scripts' / 'custom_portal.py'
    original = target.read_bytes()
    # Craft bytes with a VALID checksum (descriptor rewritten) but a banned import.
    hostile = b'import subprocess\nimport pandas as pd\n' \
              b'def extract_frame(url=None, columns=None, table_index=None, row_limit=0):\n' \
              b'    return pd.DataFrame()\n'
    target.write_bytes(hostile)
    entry = {**entry, 'sha256': sha256_bytes(hostile)}
    with pytest.raises(DataFetchError) as caught:
        verified_script_bytes(entry)
    assert caught.value.code == 'REGISTRY_INVALID'
    assert 'review' in str(caught.value)
    assert target.read_bytes() == hostile  # bytes never deleted or migrated


@pytest.mark.parametrize('registered,target,allowed', [
    ('https://example.com/d.csv', 'https://example.com/other.csv', True),
    ('http://example.com/d.csv', 'https://example.com/d.csv', True),
    ('https://example.com/d.csv', 'http://example.com/d.csv', False),
    ('https://example.com/d.csv', 'https://example.com:8443/d.csv', False),
    ('http://example.com:8080/d.csv', 'http://example.com/d.csv', False),
    ('https://example.com/d.csv', 'https://another.com/d.csv', False),
])
def test_validate_registered_origin_blocks_downgrade_and_ports(registered, target, allowed):
    from scripts.errors import DataFetchError
    from scripts.source_registry import validate_registered_origin
    if allowed:
        validate_registered_origin(registered, target)
    else:
        with pytest.raises(DataFetchError) as caught:
            validate_registered_origin(registered, target)
        assert caught.value.code == 'BLOCKED_URL'


def test_concurrent_finish_yields_exactly_one_source(registry_root, tmp_path):
    from scripts.source_registry import (
        defer_source_choice, finish_source_choice, load_sources,
    )
    from scripts.web_analyzer import AnalysisReport, WebAnalyzer
    analyzer = WebAnalyzer('https://example.com/data.csv', str(tmp_path / 'gen'))
    path = analyzer.generate_script(AnalysisReport(url=analyzer.url, difficulty='easy'))
    with patch('scripts.web_analyzer.validate_public_url'):
        token = defer_source_choice('https://example.com/data.csv', Path(path))
    outcomes = []

    def finish():
        try:
            outcomes.append(finish_source_choice(token, 'Portal'))
        except Exception as exc:  # noqa: BLE001 - the loser must fail cleanly
            outcomes.append(exc)

    import threading
    threads = [threading.Thread(target=finish), threading.Thread(target=finish)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    successes = [value for value in outcomes if not isinstance(value, Exception)]
    failures = [value for value in outcomes if isinstance(value, Exception)]
    assert len(successes) == 1 and successes[0] == 'custom_portal'
    assert failures, 'the losing racer must receive a coded error'
    assert not list((registry_root / 'pending_sources').iterdir())
    assert set(load_sources()) == {'custom_portal'}


def test_pending_preview_receipt_round_trip_with_staged_bytes(registry_root, tmp_path):
    from scripts.adapters.extractor_protocol import PreviewReceipt
    from scripts.source_registry import load_preview_receipt, save_preview_receipt
    script = TRIVIAL
    receipt = PreviewReceipt(preview_id=PreviewReceipt.new_id(), script_sha256=sha256_bytes(script),
                             canonical_url='https://example.com/d.csv', columns=['v'],
                             table_index=None)
    stored = save_preview_receipt(receipt, script)
    assert stored.is_file()
    loaded, data = load_preview_receipt(receipt.preview_id)
    assert loaded == receipt and data == script
    # Receipt store is 0600/0700 where the OS supports it.
    if os.name == 'posix':
        import stat
        assert stat.S_IMODE(stored.stat().st_mode) & 0o077 == 0


def test_preview_receipt_expiry_and_pruning(registry_root, tmp_path):
    from datetime import datetime, timedelta, timezone
    from scripts.adapters.extractor_protocol import PreviewReceipt
    from scripts.errors import DataFetchError
    from scripts.source_registry import (
        load_preview_receipt, save_preview_receipt, _preview_root,
    )
    stale = PreviewReceipt(preview_id=PreviewReceipt.new_id(), script_sha256=sha256_bytes(TRIVIAL),
                           canonical_url='https://example.com/d.csv',
                           created_at=(datetime.now(timezone.utc) - timedelta(hours=25)).isoformat())
    save_preview_receipt(stale, TRIVIAL)
    with pytest.raises(DataFetchError) as caught:
        load_preview_receipt(stale.preview_id)
    assert caught.value.code == 'PREVIEW_EXPIRED'
    # The next save prunes the expired receipt and its staged bytes.
    fresh = PreviewReceipt(preview_id=PreviewReceipt.new_id(), script_sha256=sha256_bytes(TRIVIAL),
                           canonical_url='https://example.com/d.csv')
    save_preview_receipt(fresh, TRIVIAL)
    assert not (_preview_root() / (stale.preview_id + '.json')).exists()
    assert not (_preview_root() / (stale.preview_id + '.py')).exists()
    assert (_preview_root() / (fresh.preview_id + '.py')).is_file()


def test_preview_receipt_tamper_is_rejected(registry_root, tmp_path):
    from scripts.adapters.extractor_protocol import PreviewReceipt
    from scripts.errors import DataFetchError
    from scripts.source_registry import load_preview_receipt, save_preview_receipt
    receipt = PreviewReceipt(preview_id=PreviewReceipt.new_id(), script_sha256=sha256_bytes(TRIVIAL),
                             canonical_url='https://example.com/d.csv')
    save_preview_receipt(receipt, TRIVIAL)
    staged = registry_root / 'pending_previews' / f'{receipt.preview_id}.py'
    staged.write_bytes(TRIVIAL + b'\n# tampered')
    with pytest.raises(DataFetchError) as caught:
        load_preview_receipt(receipt.preview_id)
    assert caught.value.code == 'PREVIEW_TAMPERED'
    # Unknown keys in the receipt JSON fail closed too.
    (registry_root / 'pending_previews' / f'{receipt.preview_id}.json').write_text(
        json.dumps({**receipt.to_json(), 'approved': True}))
    with pytest.raises(DataFetchError) as caught:
        load_preview_receipt(receipt.preview_id)
    assert caught.value.code == 'PREVIEW_TAMPERED'


def test_preview_id_validation_and_missing_receipt(registry_root):
    from scripts.errors import DataFetchError
    from scripts.source_registry import load_preview_receipt
    with pytest.raises(DataFetchError) as caught:
        load_preview_receipt('../escape')
    assert caught.value.code == 'ARG_MISSING'
    with pytest.raises(DataFetchError) as caught:
        load_preview_receipt('a' * 32)
    assert caught.value.code == 'PREVIEW_NOT_FOUND'


# ---------------------------------------------------------------------------
# Phase 9: byte-locked CustomFetcher, bounded previews, receipt lifecycle
# ---------------------------------------------------------------------------

CUSTOM_EXTRACTOR = extractor("""
    import pandas as pd
    def extract_frame(url=None, columns=None, table_index=None, row_limit=0):
        frame = pd.DataFrame({'a': range(12), 'label': ['real record'] * 12})
        return frame.head(row_limit) if row_limit else frame
""")


def build_custom_fetcher(tmp_path, monkeypatch, extractor_bytes=CUSTOM_EXTRACTOR, **kwargs):
    from scripts.custom_source import CustomFetcher
    from scripts.web_analyzer import AnalysisReport
    monkeypatch.setattr('scripts.web_analyzer.WebAnalyzer.analyze',
                        lambda self: AnalysisReport(url=self.url, difficulty='easy'))
    fetcher = CustomFetcher('https://example.com/data.csv', str(tmp_path / 'out'), {},
                            extractor_bytes=extractor_bytes, **kwargs)
    return fetcher


def test_no_runpy_anywhere_in_custom_source():
    source = Path(__import__('scripts.custom_source', fromlist=['']).__file__).read_text(encoding='utf-8')
    assert 'runpy' not in source
    assert 'run_path' not in source


def test_constructor_rejects_ast_failing_extractor_bytes():
    from scripts.custom_source import CustomFetcher
    from scripts.errors import DataFetchError
    hostile = extractor("""
        import subprocess
        import pandas as pd
        def extract_frame(url=None, columns=None, table_index=None, row_limit=0):
            return pd.DataFrame()
    """)
    with pytest.raises(DataFetchError) as caught:
        CustomFetcher('https://example.com/d.csv', 'unused', {}, extractor_bytes=hostile)
    assert caught.value.code == 'EXTRACTOR_REJECTED'


def test_bounded_preview_precedes_any_full_extraction(tmp_path, monkeypatch, registry_root):
    from scripts.adapters.provider import FetcherProvider
    from scripts.source_registry import load_preview_receipt
    fetcher = build_custom_fetcher(tmp_path, monkeypatch)
    fetcher.row_limit = 4
    fetcher.scout()
    provider = FetcherProvider(fetcher)
    sample = provider._full_preview_frame()
    assert len(sample) == 4  # min(row_limit, 5): bounded sample
    assert fetcher.row_limit == 4  # restored
    assert fetcher._cached_df is None  # sample is never the save cache
    receipt = fetcher._pending_receipt
    assert receipt is not None and receipt.script_sha256 == fetcher.script_sha256
    staged_receipt, staged_bytes = load_preview_receipt(receipt.preview_id)
    assert staged_bytes == fetcher.script_bytes  # approval never re-reads disk
    # The post-approval full fetch runs the SAME locked bytes for the full frame.
    fetcher.row_limit = 12  # the human's final approval may raise the count
    frame = fetcher.extract()
    assert len(frame) == 12


def test_preview_only_bound_applies_without_hook_too(tmp_path, monkeypatch, registry_root):
    from scripts.adapters.provider import FetcherProvider

    class PlainFrameFetcher:
        preview_only = True
        row_limit = 10

        def __init__(self):
            self._cached_df = None
            self.seen = []

        def extract(self):
            self.seen.append(self.row_limit)
            return pd.DataFrame({'v': range(20)}).head(self.row_limit)

    stub = PlainFrameFetcher()
    frame = FetcherProvider(stub)._full_preview_frame()
    assert stub.seen == [5]  # preview_only=True bounds even full-transfer previews
    assert len(frame) == 5 and stub._cached_df is frame
    stub.preview_only = False
    FetcherProvider(stub)._full_preview_frame()
    assert stub.seen == [5, 0]  # full preview still caches the complete frame


def test_non_interactive_full_fetch_requires_a_receipt(tmp_path, monkeypatch, registry_root):
    from scripts.errors import DataFetchError
    fetcher = build_custom_fetcher(tmp_path, monkeypatch)
    fetcher.auto_approve = True
    fetcher.scout()
    with pytest.raises(DataFetchError) as caught:
        fetcher.extract()
    assert caught.value.code == 'APPROVAL_REQUIRED'


def test_approval_mismatch_on_column_or_url_drift(tmp_path, monkeypatch, registry_root):
    from scripts.adapters.provider import FetcherProvider
    from scripts.adapters.extractor_protocol import PreviewReceipt
    from scripts.errors import DataFetchError
    fetcher = build_custom_fetcher(tmp_path, monkeypatch)
    fetcher.required_columns = ['a']
    fetcher.scout()
    FetcherProvider(fetcher)._full_preview_frame()
    fetcher.required_columns = ['b']  # drift after preview
    with pytest.raises(DataFetchError) as caught:
        fetcher.extract()
    assert caught.value.code == 'APPROVAL_MISMATCH'
    # URL drift on an otherwise valid receipt fails closed too.
    fetcher.required_columns = ['a']
    drifted = PreviewReceipt(**{**fetcher._pending_receipt.to_json(),
                                'canonical_url': 'https://other.com/data.csv'})
    fetcher.approved_receipt = drifted
    with pytest.raises(DataFetchError) as caught:
        fetcher.extract()
    assert caught.value.code == 'APPROVAL_MISMATCH'


def test_stored_preview_id_unlocks_a_followup_full_fetch(tmp_path, monkeypatch, registry_root):
    """--approve-preview ID seam: the persisted receipt authorises a later run."""
    from scripts.adapters.provider import FetcherProvider
    fetcher = build_custom_fetcher(tmp_path, monkeypatch)
    fetcher.scout()
    FetcherProvider(fetcher)._full_preview_frame()
    preview_id = fetcher._pending_receipt.preview_id
    # A fresh fetcher instance for the follow-up run (as a second CLI call).
    later = build_custom_fetcher(tmp_path, monkeypatch)
    later.scout()
    later.approved_receipt = preview_id  # bytes come from the receipt store
    assert later.extract().shape[0] == 12


def test_extractor_bytes_and_receipt_disagree_fails_closed(tmp_path, monkeypatch, registry_root):
    from scripts.adapters.extractor_protocol import PreviewReceipt
    from scripts.errors import DataFetchError
    fetcher = build_custom_fetcher(tmp_path, monkeypatch)
    fetcher.scout()
    other = CUSTOM_EXTRACTOR + b'\n# different bytes\n'
    forged = PreviewReceipt(preview_id=PreviewReceipt.new_id(), script_sha256=sha256_bytes(other),
                            canonical_url='https://example.com/data.csv')
    fetcher.approved_receipt = forged
    with pytest.raises(DataFetchError) as caught:
        fetcher.extract()
    assert caught.value.code == 'APPROVAL_MISMATCH'
