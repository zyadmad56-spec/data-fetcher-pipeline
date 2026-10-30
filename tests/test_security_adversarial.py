"""Phase 13 — end-to-end adversarial security suite.

Drives the INTEGRATED product through its real seams: agent-facing channels
are captured around ``scripts.cli.main`` in ``--json-output`` mode, custom
flows run offline fixture extractors through the REAL out-of-process worker
(``--extractor-script`` / ``--approve-preview``), and built-in providers are
exercised with fully mocked transports (no test dials the network).

Honest scope: these tests document tested attack vectors and tested limits.
They do not prove containment; where a vector is neutralized only partially
(sanitized external metadata), a ``test_documented_limit_*`` test records the
exact residual behavior. No "sandbox"/"proof" claims are made anywhere.
"""
import json
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from scripts.adapters.extractor_protocol import PreviewReceipt, WorkerRequest, sha256_bytes
from scripts.adapters.script_runner import SubprocessExtractorRunner, inspect_extractor_ast
from scripts.errors import DataFetchError

CANARY = 'CANARY_7F9A2B4C6D8E'
PUBLIC_DNS = [(2, 1, 6, '', ('93.184.216.34', 0))]


# ---------------------------------------------------------------------------
# Offline fixture extractors (run in the real worker; zero network/IO)
# ---------------------------------------------------------------------------

def extractor(body: str) -> str:
    return textwrap.dedent(body)


CELL_CANARY_EXTRACTOR = extractor("""
    import pandas as pd
    def extract_frame(query_url, columns=None, table_index=None, row_limit=None):
        frame = pd.DataFrame({'__HEADER__': ['__CELL__', 'plain', 'plain']})
        if columns:
            missing = [name for name in columns if name not in frame.columns]
            if missing:
                raise ValueError('missing columns: ' + ', '.join(missing))
        if row_limit and row_limit > 0:
            frame = frame.head(int(row_limit))
        return frame
""").replace('__HEADER__', CANARY + '_header').replace('__CELL__', CANARY + '_cell')

HTML_CANARY_EXTRACTOR = extractor("""
    import pandas as pd
    from bs4 import BeautifulSoup
    def extract_frame(query_url, columns=None, table_index=None, row_limit=None):
        html = ('<table><tr><th>model</th></tr>'
                '<tr><td>__CELL__</td></tr>'
                '<tr><td>plain</td></tr></table>')
        soup = BeautifulSoup(html, 'html.parser')
        table = soup.find_all('table')[0]
        rows = [[cell.get_text(' ', strip=True) for cell in tr.find_all(['th', 'td'])]
                for tr in table.find_all('tr')]
        frame = pd.DataFrame(rows[1:], columns=rows[0])
        if row_limit and row_limit > 0:
            frame = frame.head(int(row_limit))
        return frame
""").replace('__CELL__', CANARY + '_html_cell')

ROW_EXTRACTOR = extractor("""
    import pandas as pd
    def extract_frame(query_url, columns=None, table_index=None, row_limit=None):
        frame = pd.DataFrame({'value': [1, 2, 3], 'label': ['real', 'real', 'real']})
        if columns:
            missing = [name for name in columns if name not in frame.columns]
            if missing:
                raise ValueError('missing columns: ' + ', '.join(missing))
        if row_limit and row_limit > 0:
            frame = frame.head(int(row_limit))
        return frame
""")

FAILING_CANARY_EXTRACTOR = extractor("""
    import pandas as pd
    def extract_frame(query_url, columns=None, table_index=None, row_limit=None):
        # Multi-line error: the second line mimics a leaked CSV row.
        raise ValueError('SIZE_LIMIT: payload refused\\n__CANARY__,offending,row')
""").replace('__CANARY__', CANARY)


# ---------------------------------------------------------------------------
# Shared fixtures and CLI driver
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def registry_root(tmp_path):
    with patch('scripts.source_registry.registry_root', return_value=tmp_path / 'config'):
        yield tmp_path / 'config'


def registry_root_path():
    from scripts.source_registry import registry_root
    return registry_root()


def response(body, mime='text/csv', status=200, extra_headers=None):
    r = MagicMock(status_code=status, headers={'Content-Type': mime,
                                               'Content-Length': str(len(body)),
                                               **(extra_headers or {})})
    r.json.return_value = body
    r.iter_content.side_effect = lambda *a, **k: iter(
        [body[i:i + 16384] for i in range(0, len(body), 16384)])
    return r


def run_cli(capsys, argv, inputs=None, patches=None):
    """Drive scripts.cli.main with --json-output and capture agent channels.

    Custom-source flows get the same offline treatment the migrated workflow
    tests use: ``WebAnalyzer.analyze`` is patched to a controlled easy report
    (no network); fixture extractors do the work inside the real worker.
    """
    from scripts.cli import main
    from scripts.web_analyzer import AnalysisReport
    patches = list(patches or [])
    if any(arg == '--source' and value == 'custom'
           for arg, value in zip(argv, argv[1:])):
        patches.append(('scripts.web_analyzer.WebAnalyzer.analyze',
                        lambda self: AnalysisReport(url=self.url, difficulty='easy')))
    started = [patch(target, side_effect_or_value(value)) for target, value in patches]
    exit_code = 0
    with patch('sys.argv', argv), patch('scripts.cli.setup_wizard', return_value={}):
        prompt = inputs if isinstance(inputs, list) else AssertionError('Machine mode prompted')
        with patch('builtins.input', side_effect=prompt):
            for p in started:
                p.start()
            try:
                main()
            except SystemExit as exc:
                exit_code = exc.code or 0
            finally:
                for p in reversed(started):
                    p.stop()
    captured = capsys.readouterr()
    return captured.out, captured.err, exit_code


def side_effect_or_value(value):
    return value if callable(value) else MagicMock(return_value=value)


def custom_argv(tmp_path, *flags):
    return ['data-fetcher', '--source', 'custom',
            '--query', 'https://example.com/data.csv',
            '--outdir', str(tmp_path / 'data'), *flags]


def analyze_argv(tmp_path):
    return ['data-fetcher', '--source', 'custom', '--query', 'https://example.com/data.csv',
            '--outdir', str(tmp_path / 'data'), '--analyze-only', '--yes', '--json-output']


def write_extractor(tmp_path, source: str, name: str) -> Path:
    script = tmp_path / name
    script.write_text(source, encoding='utf-8')
    return script


def assert_parent_healthy():
    """The host process must stay fully functional after every attack."""
    probe = subprocess.run([sys.executable, '-c', 'print("parent-ok")'],
                           capture_output=True, timeout=60)
    assert probe.returncode == 0 and b'parent-ok' in probe.stdout


# ---------------------------------------------------------------------------
# 1. Prompt-injection canaries — agent channels stay cell-free
# ---------------------------------------------------------------------------

def test_canary_cells_and_headers_never_reach_agent_channels(tmp_path, capsys):
    script = write_extractor(tmp_path, CELL_CANARY_EXTRACTOR, 'canary_cells.py')
    out, err, code = run_cli(capsys, custom_argv(
        tmp_path, '--yes', '--non-interactive', '--json-output', '--preview-only',
        '--extractor-script', str(script)))
    assert code == 0 and out.strip()
    preview = json.loads(out)
    assert preview['mode'] == 'preview'
    # The human-only artifact is SUPPOSED to carry the sample; the agent channels are not.
    human = Path(preview['preview_path']).read_text(encoding='utf-8')
    assert CANARY + '_cell' in human and CANARY + '_header' in human
    assert CANARY not in out and CANARY not in err
    out, err, code = run_cli(capsys, custom_argv(
        tmp_path, '--yes', '--non-interactive', '--json-output', '--rows', '2',
        '--approve-preview', preview['preview_id']))
    assert code == 0, out + err
    envelope = json.loads(out)
    assert envelope['status'] == 'success'
    assert CANARY not in out and CANARY not in err


def test_canary_in_html_table_cells_never_reaches_agent_channels(tmp_path, capsys):
    script = write_extractor(tmp_path, HTML_CANARY_EXTRACTOR, 'canary_html.py')
    out, err, code = run_cli(capsys, custom_argv(
        tmp_path, '--yes', '--non-interactive', '--json-output', '--preview-only',
        '--extractor-script', str(script)))
    assert code == 0, out + err
    preview = json.loads(out)
    assert CANARY + '_html_cell' in Path(preview['preview_path']).read_text(encoding='utf-8')
    assert CANARY not in out and CANARY not in err


def test_canary_error_exit_channel_stays_clean_for_custom_failure(tmp_path, capsys):
    """The failing extraction runs during the bounded preview. Error prose is
    bounded to its first line (pandas offending-row leakage is cut) and the
    coded envelope carries a recovery hint. First-party extractor messages are
    the operator's feedback channel and are not content-free by design; the
    tested vector is the multi-line row leak, which must not survive."""
    script = write_extractor(tmp_path, FAILING_CANARY_EXTRACTOR, 'canary_fail.py')
    out, err, code = run_cli(capsys, custom_argv(
        tmp_path, '--yes', '--non-interactive', '--json-output', '--preview-only',
        '--extractor-script', str(script)))
    assert code == 1
    envelope = json.loads(out)
    assert envelope['status'] == 'error' and envelope['code'] == 'SIZE_LIMIT'
    assert CANARY not in out and CANARY not in err      # second-line row leak is cut
    assert 'offending' not in out and 'offending' not in err
    assert 'Recovery hint' in envelope['message']


def test_canary_analyze_only_channel_stays_clean(tmp_path, capsys):
    """A redirect Location smuggling an OSC-wrapped canary must vanish from the
    analysis envelope: the whole escape sequence (payload included) is stripped."""
    hijack_location = 'https://second.example.com/\x1b]0;' + CANARY + '\x07'
    redirect = response(b'', 'text/html', status=302, extra_headers={'Location': hijack_location})
    final = response(b'value\n1\n', 'text/csv')
    patches = [
        ('scripts.web_analyzer.validate_public_url', lambda url: None),
        ('scripts.web_analyzer.WebAnalyzer._check_robots_txt', lambda self: True),
        ('scripts.web_analyzer.request_with_retry',
         MagicMock(side_effect=lambda url, **k: redirect if url == 'https://example.com/data.csv'
                   else final)),
    ]
    out, err, code = run_cli(capsys, analyze_argv(tmp_path), patches=patches)
    assert code == 0, out + err
    envelope = json.loads(out)
    assert envelope['mode'] == 'analysis'
    assert CANARY not in out and CANARY not in err
    assert '\x1b' not in out and '\x1b' not in err


def test_canary_dcat_title_osc_smuggling_is_neutralized(tmp_path, capsys):
    """A DCAT title smuggling the canary inside an OSC terminal-hijack must be
    stripped entirely from every agent-facing channel."""
    catalog = {'results': [{'dcat': {
        'title': 'Budget \x1b]0;' + CANARY + '\x07 \u202e tail',
        'distribution': [{'format': 'CSV',
                          'downloadURL': 'https://data.example.gov/budget.csv'}]}}]}
    transport = MagicMock(side_effect=lambda url, **k: (
        response(catalog, 'application/json') if 'api.gsa.gov' in url
        else response(b'value\n1\n', 'text/csv')))
    patches = [
        ('socket.getaddrinfo', lambda *a, **k: PUBLIC_DNS),
        ('scripts.fetchers.datagov.request_with_retry', transport),
        ('scripts.base.request_with_retry', transport),
    ]
    argv = ['data-fetcher', '--source', 'datagov', '--query', 'budget',
            '--outdir', str(tmp_path / 'dg'), '--yes', '--non-interactive',
            '--json-output', '--rows', '2']
    out, err, code = run_cli(capsys, argv, patches=patches)
    assert code == 0, out + err
    envelope = json.loads(out)
    assert envelope['status'] == 'success'
    assert CANARY not in out and CANARY not in err
    assert '\x1b' not in out and '\x1b' not in err
    assert '\u202e' not in out and '\u202e' not in err  # raw BiDi override never survives


def test_canary_blocked_dcat_warning_is_sanitized(tmp_path, capsys):
    catalog = {'results': [{'dcat': {
        'title': 'Budget \x1b]0;' + CANARY + '\x07 \u202e tail',
        'distribution': [{'format': 'CSV',
                          'downloadURL': 'http://169.254.169.254/creds.csv'}]}}]}
    transport = MagicMock(return_value=response(catalog, 'application/json'))
    patches = [
        ('scripts.fetchers.datagov.request_with_retry', transport),
        ('scripts.base.request_with_retry', transport),
    ]
    argv = ['data-fetcher', '--source', 'datagov', '--query', 'budget',
            '--outdir', str(tmp_path / 'dgb'), '--yes', '--non-interactive',
            '--json-output', '--preview-only']
    out, err, code = run_cli(capsys, argv, patches=patches)
    envelope = json.loads(out)
    assert envelope['status'] == 'error'
    assert CANARY not in out and CANARY not in err
    assert '\x1b' not in out and '\x1b' not in err
    assert '\u202e' not in out and '\u202e' not in err


def test_error_prose_neutralizes_escape_sequences_and_bidi(tmp_path, capsys):
    """FIXED (was a reported defect): on ERROR exits the message comes from
    ``sanitization.scrub_exception_message``, which now runs the scrubbed
    base through ``sanitize_display_text`` — ANSI/OSC escape sequences are
    stripped entirely and BiDi overrides visibly encoded — before the length
    cap. A hostile repository name smuggled into a GitHub skip note reaches
    the agent-facing error envelope neutralized: no raw escapes, no smuggled
    payload."""
    search = {'items': [{'full_name': '\x1b]0;' + CANARY + '\x07\u202e/evil',
                         'default_branch': 'main'}]}
    transport = MagicMock(side_effect=lambda url, **k: (
        response(search, 'application/json') if '/search/repositories' in url
        else response(b'forbidden', 'text/html', status=403)))
    patches = [
        ('scripts.fetchers.github_data.request_with_retry', transport),
        ('scripts.base.request_with_retry', transport),
    ]
    argv = ['data-fetcher', '--source', 'github', '--query', 'datasets',
            '--outdir', str(tmp_path / 'gh'), '--yes', '--non-interactive', '--json-output']
    out, err, code = run_cli(capsys, argv, patches=patches)
    envelope = json.loads(out)
    assert envelope['status'] == 'error'
    # Neutralized: escape sequences stripped entirely (the OSC payload, canary
    # included, is gone) and the BiDi override visibly encoded, never raw.
    message = envelope['message']
    assert '\x1b' not in message
    assert CANARY not in message
    assert '\u202e' not in message
    # The bounding that also holds: first line only, recovery hint present.
    assert '\n' not in message and 'Recovery hint' in message
    assert len(message) <= 500


def test_canary_http_500_body_never_reaches_agent_channels(tmp_path, capsys):
    """A 5xx error body is withheld from the agent-facing error envelope."""
    body = '<html><body>Gateway exploded: ' + CANARY + ' and secrets</body></html>'
    transport = MagicMock(return_value=MagicMock(
        status_code=502, text=body, headers={}))
    patches = [
        ('scripts.fetchers.github_data.request_with_retry', transport),
        ('scripts.fetchers.github_data._resolve_github_token', lambda config: ''),
    ]
    argv = ['data-fetcher', '--source', 'github', '--query', 'datasets',
            '--outdir', str(tmp_path / 'gh5'), '--yes', '--non-interactive', '--json-output']
    out, err, code = run_cli(capsys, argv, patches=patches)
    envelope = json.loads(out)
    assert envelope['status'] == 'error'
    assert CANARY not in out and CANARY not in err
    assert 'Gateway exploded' not in out and 'Gateway exploded' not in err
    assert 'secrets' not in out and 'secrets' not in err


# ---------------------------------------------------------------------------
# 2. Worker attacks — real child process, fail closed, parent stays healthy
# ---------------------------------------------------------------------------

def make_request(script: str, deadline: float = 30.0) -> WorkerRequest:
    script_bytes = script.encode('utf-8')
    return WorkerRequest(script_sha256=sha256_bytes(script_bytes),
                         query_url='https://example.com/dataset.csv',
                         deadline_sec=deadline)


def run_attack(script: str, deadline: float = 30.0):
    runner = SubprocessExtractorRunner()
    result = runner.run(script.encode('utf-8'), make_request(script, deadline))
    return runner, result


def test_worker_attack_env_secret_is_absent_from_child(monkeypatch):
    monkeypatch.setenv('DFP_CANARY_SECRET', 'canary-value-9f2c')
    script = extractor("""
        import os
        import pandas as pd
        def extract_frame(query_url, columns=None, table_index=None, row_limit=None):
            blob = '\\n'.join(f'{k}={v}' for k, v in sorted(os.environ.items()))
            return pd.DataFrame({'env': [blob]})
    """)
    runner, result = run_attack(script)
    assert result.error_code is None, result.error_summary
    combined = (runner.last_stdout + runner.last_stderr
                + runner.last_payload_csv + runner.last_payload_sidecar)
    assert b'canary-value-9f2c' not in combined
    assert b'DFP_CANARY_SECRET' not in combined
    assert_parent_healthy()


def test_worker_attack_real_config_path_open_fails_closed():
    config_path = Path.home() / '.config' / 'data-fetcher-pipeline' / 'config.json'
    script = extractor(f"""
        import pandas as pd
        def extract_frame(query_url, columns=None, table_index=None, row_limit=None):
            with open({str(config_path)!r}, encoding='utf-8') as stream:
                return pd.DataFrame({{'content': [stream.read()[:100]]}})
    """)
    runner, result = run_attack(script)
    assert result.error_code == 'WORKER_POLICY_VIOLATION', result.to_json()
    assert not runner.last_payload_csv
    assert_parent_healthy()


def test_worker_attack_subprocess_use_fails_closed():
    script = extractor("""
        import importlib
        import pandas as pd
        def extract_frame(query_url, columns=None, table_index=None, row_limit=None):
            subprocess = importlib.import_module('subprocess')
            subprocess.Popen(['echo', 'pwned'])
            return pd.DataFrame({'v': [1]})
    """)
    runner, result = run_attack(script)
    assert result.error_code == 'WORKER_POLICY_VIOLATION', result.to_json()
    assert_parent_healthy()


def test_worker_attack_ctypes_use_fails_closed():
    script = extractor("""
        import os
        import importlib
        import pandas as pd
        def extract_frame(query_url, columns=None, table_index=None, row_limit=None):
            ctypes = importlib.import_module('ctypes')
            ctypes.CDLL('user32.dll' if os.name == 'nt' else 'libc.so.6')
            return pd.DataFrame({'v': [1]})
    """)
    runner, result = run_attack(script)
    assert result.error_code == 'WORKER_POLICY_VIOLATION', result.to_json()
    assert_parent_healthy()


@pytest.mark.parametrize('target', ['127.0.0.1', '169.254.169.254',
                                    '100.100.100.200', '::ffff:127.0.0.1'])
def test_worker_attack_non_public_connect_fails_closed(target):
    script = extractor(f"""
        import socket
        import pandas as pd
        def extract_frame(query_url, columns=None, table_index=None, row_limit=None):
            socket.create_connection(({target!r}, 80), timeout=2)
            return pd.DataFrame({{'v': [1]}})
    """)
    runner, result = run_attack(script)
    assert result.error_code == 'WORKER_POLICY_VIOLATION', result.to_json()
    assert_parent_healthy()


def test_worker_attack_stdout_flood_is_capped():
    script = extractor("""
        import sys
        import pandas as pd
        def extract_frame(query_url, columns=None, table_index=None, row_limit=None):
            blob = 'A' * 65536
            for _ in range(160):
                sys.stdout.write(blob)
            sys.stdout.flush()
            return pd.DataFrame({'v': [1]})
    """)
    runner, result = run_attack(script)
    assert result.error_code is None, result.error_summary
    assert len(runner.last_stdout) <= 64 * 1024
    assert result.rows == 1
    assert_parent_healthy()


def test_worker_attack_deadline_stall_is_killed():
    script = extractor("""
        import pandas as pd
        def extract_frame(query_url, columns=None, table_index=None, row_limit=None):
            while True:
                pass
    """)
    runner, result = run_attack(script, deadline=6.0)
    assert result.error_code == 'WORKER_TIMEOUT', result.to_json()
    assert not runner.last_payload_csv
    assert_parent_healthy()


# ---------------------------------------------------------------------------
# 3. Receipt binding — drift, expiry, tamper, absence
# ---------------------------------------------------------------------------

def build_custom_fetcher(tmp_path, monkeypatch, extractor_source=ROW_EXTRACTOR, query=None, **kwargs):
    from scripts.custom_source import CustomFetcher
    from scripts.web_analyzer import AnalysisReport
    monkeypatch.setattr('scripts.web_analyzer.WebAnalyzer.analyze',
                        lambda self: AnalysisReport(url=self.url, difficulty='easy'))
    return CustomFetcher(query or 'https://example.com/data.csv', str(tmp_path / 'out'), {},
                         extractor_bytes=extractor_source.encode('utf-8'), **kwargs)


def make_preview(tmp_path, monkeypatch):
    from scripts.adapters.provider import FetcherProvider
    fetcher = build_custom_fetcher(tmp_path, monkeypatch)
    fetcher.scout()
    FetcherProvider(fetcher)._full_preview_frame()
    return fetcher, fetcher._pending_receipt


def test_receipt_rejects_swapped_script_bytes(tmp_path, monkeypatch):
    _, receipt = make_preview(tmp_path, monkeypatch)
    modified = ROW_EXTRACTOR.replace("[1, 2, 3]", "[9, 9, 9]")
    fetcher = build_custom_fetcher(tmp_path, monkeypatch, extractor_source=modified,
                                   approved_receipt=receipt)
    fetcher.scout()
    with pytest.raises(DataFetchError) as caught:
        fetcher.extract()
    assert caught.value.code == 'APPROVAL_MISMATCH'


def test_receipt_rejects_url_drift(tmp_path, monkeypatch):
    _, receipt = make_preview(tmp_path, monkeypatch)
    fetcher = build_custom_fetcher(tmp_path, monkeypatch,
                                   query='https://other.example.com/data.csv',
                                   approved_receipt=receipt)
    fetcher.scout()
    with pytest.raises(DataFetchError) as caught:
        fetcher.extract()
    assert caught.value.code == 'APPROVAL_MISMATCH'


def test_receipt_rejects_column_and_table_drift(tmp_path, monkeypatch):
    from scripts.adapters.provider import FetcherProvider
    fetcher = build_custom_fetcher(tmp_path, monkeypatch)
    fetcher.required_columns = ['value']
    fetcher.scout()
    FetcherProvider(fetcher)._full_preview_frame()
    fetcher.required_columns = ['label']
    with pytest.raises(DataFetchError) as caught:
        fetcher.extract()
    assert caught.value.code == 'APPROVAL_MISMATCH'
    fetcher.required_columns = ['value']
    fetcher.table_index = 1
    with pytest.raises(DataFetchError) as caught:
        fetcher.extract()
    assert caught.value.code == 'APPROVAL_MISMATCH'


def test_expired_receipt_is_rejected_end_to_end(tmp_path, capsys):
    from datetime import datetime, timedelta, timezone
    from scripts.source_registry import save_preview_receipt
    receipt = PreviewReceipt(
        preview_id=PreviewReceipt.new_id(), script_sha256=sha256_bytes(ROW_EXTRACTOR.encode()),
        canonical_url='https://example.com/data.csv',
        created_at=(datetime.now(timezone.utc) - timedelta(hours=25)).isoformat())
    save_preview_receipt(receipt, ROW_EXTRACTOR.encode())
    out, err, code = run_cli(capsys, custom_argv(
        tmp_path, '--yes', '--non-interactive', '--json-output',
        '--approve-preview', receipt.preview_id))
    envelope = json.loads(out)
    assert envelope['status'] == 'error' and envelope['code'] == 'PREVIEW_EXPIRED'
    assert code == 1


def test_tampered_receipt_store_entry_fails_closed_end_to_end(tmp_path, capsys):
    from scripts.source_registry import save_preview_receipt
    receipt = PreviewReceipt(preview_id=PreviewReceipt.new_id(),
                             script_sha256=sha256_bytes(ROW_EXTRACTOR.encode()),
                             canonical_url='https://example.com/data.csv')
    save_preview_receipt(receipt, ROW_EXTRACTOR.encode())
    staged = registry_root_path() / 'pending_previews' / (receipt.preview_id + '.py')
    staged.write_bytes((ROW_EXTRACTOR + '\n# tampered after staging\n').encode('utf-8'))
    out, err, code = run_cli(capsys, custom_argv(
        tmp_path, '--yes', '--non-interactive', '--json-output',
        '--approve-preview', receipt.preview_id))
    envelope = json.loads(out)
    assert envelope['status'] == 'error' and envelope['code'] == 'PREVIEW_TAMPERED'
    assert code == 1


def test_missing_receipt_non_interactive_full_fetch_fails_closed(tmp_path, capsys):
    out, err, code = run_cli(capsys, custom_argv(
        tmp_path, '--yes', '--non-interactive', '--json-output', '--rows', '2'))
    envelope = json.loads(out)
    assert envelope['status'] == 'error' and envelope['code'] == 'APPROVAL_REQUIRED'
    assert code == 1


# ---------------------------------------------------------------------------
# 4. Registry races — staged bytes win, corrupt store fails closed, origin pin
# ---------------------------------------------------------------------------

def test_modified_on_disk_script_between_preview_and_approval_uses_staged_bytes(tmp_path, capsys):
    script = write_extractor(tmp_path, ROW_EXTRACTOR, 'staged.py')
    out, err, code = run_cli(capsys, custom_argv(
        tmp_path, '--yes', '--non-interactive', '--json-output', '--preview-only',
        '--extractor-script', str(script)))
    preview = json.loads(out)
    assert preview['mode'] == 'preview'
    # Disk edits after the preview have zero effect: the staged bytes win.
    script.write_text(
        ROW_EXTRACTOR.replace("'real', 'real', 'real'", "'SWAPPED', 'SWAPPED', 'SWAPPED'"),
        encoding='utf-8')
    out, err, code = run_cli(capsys, custom_argv(
        tmp_path, '--yes', '--non-interactive', '--json-output', '--rows', '2',
        '--approve-preview', preview['preview_id']))
    envelope = json.loads(out)
    assert envelope['status'] == 'success', envelope.get('message')
    saved = Path(envelope['output_path']).read_text(encoding='utf-8')
    assert 'real' in saved and 'SWAPPED' not in saved


def test_corrupt_pending_previews_receipt_json_fails_closed(tmp_path, capsys):
    from scripts.source_registry import save_preview_receipt
    receipt = PreviewReceipt(preview_id=PreviewReceipt.new_id(),
                             script_sha256=sha256_bytes(ROW_EXTRACTOR.encode()),
                             canonical_url='https://example.com/data.csv')
    save_preview_receipt(receipt, ROW_EXTRACTOR.encode())
    entry = registry_root_path() / 'pending_previews' / (receipt.preview_id + '.json')
    entry.write_text('{not json at all', encoding='utf-8')
    out, err, code = run_cli(capsys, custom_argv(
        tmp_path, '--yes', '--non-interactive', '--json-output',
        '--approve-preview', receipt.preview_id))
    envelope = json.loads(out)
    assert envelope['status'] == 'error' and envelope['code'] == 'PREVIEW_TAMPERED'


@pytest.mark.parametrize('target', ['http://example.com/data.csv',
                                    'https://example.com:8443/data.csv'])
def test_saved_source_origin_downgrade_and_port_change_refused(tmp_path, capsys, target):
    from scripts.source_registry import save_source
    with patch('scripts.web_analyzer.validate_public_url'):
        key = save_source('Portal', 'https://example.com/data.csv',
                          ROW_EXTRACTOR.encode('utf-8'))
    argv = ['data-fetcher', '--source', key, '--query', target,
            '--outdir', str(tmp_path / 'reuse'), '--yes', '--non-interactive', '--json-output']
    out, err, code = run_cli(capsys, argv)
    envelope = json.loads(out)
    assert envelope['status'] == 'error' and envelope['code'] == 'BLOCKED_URL'
    assert code == 1


# ---------------------------------------------------------------------------
# 5. Archive and formula vectors
# ---------------------------------------------------------------------------

def test_xlsx_entry_bomb_rejected_with_size_limit(tmp_path):
    from scripts.format_alchemy import FormatAlchemyEngine
    workbook = tmp_path / 'bomb.xlsx'
    with zipfile.ZipFile(workbook, 'w', zipfile.ZIP_STORED) as bundle:
        bundle.writestr('[Content_Types].xml', '<Types/>')
        for index in range(1001):
            bundle.writestr(f'xl/worksheets/sheet{index}.xml', '<worksheet/>')
    with pytest.raises(DataFetchError) as caught:
        FormatAlchemyEngine.convert(str(workbook), 'csv')
    assert caught.value.code == 'SIZE_LIMIT'


def test_kaggle_zip_slip_member_is_refused(tmp_path):
    from scripts.fetchers.kaggle_fetcher import KaggleFetcher

    def hostile_download(handle, path, unzip):
        assert unzip is False
        with zipfile.ZipFile(Path(path) / 'dataset.zip', 'w') as bundle:
            bundle.writestr('../evil.csv', 'stolen')

    module = MagicMock()
    module.api.dataset_download_files.side_effect = hostile_download
    fetcher = KaggleFetcher('owner/dataset', str(tmp_path), {})
    fetcher.username = 'unit-user'
    fetcher.key = 'unit-key'
    with patch.dict(sys.modules, {'kaggle': module}):
        with pytest.raises(ValueError, match='BLOCKED'):
            fetcher.extract()


def test_excel_formula_string_round_trips_as_text(tmp_path):
    from openpyxl import load_workbook

    from scripts.format_alchemy import FormatAlchemyEngine
    source = tmp_path / 'formulas_raw.csv'
    payload = '=CMD|\'/C calc\'!A0'
    source.write_text(f'payload\n"{payload}"\n', encoding='utf-8')
    converted = Path(FormatAlchemyEngine.convert(str(source), 'excel'))
    assert converted.is_file()
    with zipfile.ZipFile(converted) as bundle:
        sheet_xml = bundle.read('xl/worksheets/sheet1.xml').decode('utf-8')
    assert '<f>' not in sheet_xml  # no formula element may exist
    frame = pd.read_excel(converted, engine='openpyxl')
    assert frame['payload'].iloc[0] == payload  # exact string, zero mutation


# ---------------------------------------------------------------------------
# 6. Documented limits — the tested residual behavior, stated plainly
# ---------------------------------------------------------------------------

def test_documented_limit_ast_gate_accepts_alias_but_runtime_blocks_use():
    """The static gate is a review aid, not containment: an aliased os.system
    call passes the AST check; the worker's runtime hook still blocks the
    actual os.system event. Both layers are bypassable in principle; neither
    is claimed to be complete."""
    aliased = extractor("""
        import os as platform
        import pandas as pd
        def extract_frame(query_url, columns=None, table_index=None, row_limit=None):
            platform.system('echo pwned')
            return pd.DataFrame({'v': [1]})
    """)
    inspect_extractor_ast(aliased.encode('utf-8'))  # no raise: the gate cannot see it
    runner, result = run_attack(aliased)
    assert result.error_code == 'WORKER_POLICY_VIOLATION', result.to_json()


def test_documented_limit_worker_reads_inside_python_prefix_are_allowed_by_design():
    """Same-user code in the worker can read files inside the Python
    installation prefix by design (TLS bundles, stdlib). That boundary is
    documented, not a containment claim."""
    script = extractor("""
        import sys
        from pathlib import Path
        import pandas as pd
        def extract_frame(query_url, columns=None, table_index=None, row_limit=None):
            marker = Path(sys.prefix) / 'marker-that-may-not-exist.txt'
            return pd.DataFrame({'inside_prefix': [str(marker).startswith(str(Path(sys.prefix)))]})
    """)
    runner, result = run_attack(script)
    assert result.error_code is None, result.error_summary


def test_documented_limit_plain_external_metadata_is_sanitized_not_removed(tmp_path, capsys):
    """DCAT titles and repository names are injected content. Plain printable
    metadata text reaches the saved envelope's metadata fields AFTER
    sanitization (escape sequences stripped, controls encoded, length
    bounded) — it is NOT content-free. The plan redacts cells/headers/errors;
    metadata titles are a documented residual surface for the orchestrator."""
    catalog = {'results': [{'dcat': {'title': CANARY + ' budget', 'distribution': [
        {'format': 'CSV', 'downloadURL': 'https://data.example.gov/budget.csv'}]}}]}
    transport = MagicMock(side_effect=lambda url, **k: (
        response(catalog, 'application/json') if 'api.gsa.gov' in url
        else response(b'value\n1\n', 'text/csv')))
    patches = [
        ('socket.getaddrinfo', lambda *a, **k: PUBLIC_DNS),
        ('scripts.fetchers.datagov.request_with_retry', transport),
        ('scripts.base.request_with_retry', transport),
    ]
    argv = ['data-fetcher', '--source', 'datagov', '--query', 'budget',
            '--outdir', str(tmp_path / 'dg2'), '--yes', '--non-interactive',
            '--json-output', '--rows', '2']
    out, err, code = run_cli(capsys, argv, patches=patches)
    envelope = json.loads(out)
    assert envelope['status'] == 'success'
    assert CANARY in (envelope.get('resolved_title') or '')  # documented residual
    assert len(envelope['resolved_title']) <= 300


def test_documented_limit_preview_is_bounded_at_five_rows(tmp_path, capsys):
    """A full-transfer preview never exceeds five rows, whatever --rows asks."""
    wide_extractor = extractor("""
        import pandas as pd
        def extract_frame(query_url, columns=None, table_index=None, row_limit=None):
            frame = pd.DataFrame({'value': range(50), 'label': ['real'] * 50})
            if row_limit and row_limit > 0:
                frame = frame.head(int(row_limit))
            return frame
    """)
    script = write_extractor(tmp_path, wide_extractor, 'bounded.py')
    out, err, code = run_cli(capsys, custom_argv(
        tmp_path, '--yes', '--non-interactive', '--json-output', '--preview-only',
        '--rows', '50', '--extractor-script', str(script)))
    preview = json.loads(out)
    assert preview['rows_sampled'] == 5


def test_documented_limit_frame_transport_uses_a_fresh_positional_index():
    """The worker transport carries values and schema, not custom indexes;
    decoded frames always come back with a positional RangeIndex."""
    from scripts.adapters.extractor_protocol import decode_frame, encode_frame
    frame = pd.DataFrame({'a': [1, 2]}, index=[7, 9])
    csv_bytes, sidecar = encode_frame(frame)
    decoded = decode_frame(csv_bytes, sidecar)
    assert decoded['a'].tolist() == [1, 2]
    assert list(decoded.index) == [0, 1]


def test_documented_limit_receipt_ttl_is_twenty_four_hours():
    """Receipts expire after 24 hours; a 23-hour-old receipt is still valid."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    fresh = PreviewReceipt(preview_id=PreviewReceipt.new_id(),
                           script_sha256='a' * 64, canonical_url='https://example.com/d.csv',
                           created_at=(now - timedelta(hours=23)).isoformat())
    stale = PreviewReceipt(preview_id=PreviewReceipt.new_id(),
                           script_sha256='a' * 64, canonical_url='https://example.com/d.csv',
                           created_at=(now - timedelta(hours=25)).isoformat())
    assert not fresh.is_expired(now)
    assert stale.is_expired(now)


def test_documented_limit_human_preview_is_same_user_readable(tmp_path, capsys):
    """The 0o600 preview artifact is a cooperative control: the same user (and
    therefore a file-reading agent harness) can still open it. Enforcement is
    procedural (workflow rules), not technical."""
    script = write_extractor(tmp_path, ROW_EXTRACTOR, 'human.py')
    out, err, code = run_cli(capsys, custom_argv(
        tmp_path, '--yes', '--non-interactive', '--json-output', '--preview-only',
        '--extractor-script', str(script)))
    preview = json.loads(out)
    artifact = Path(preview['preview_path'])
    assert artifact.is_file()
    assert 'real' in artifact.read_text(encoding='utf-8')  # readable by this same user
