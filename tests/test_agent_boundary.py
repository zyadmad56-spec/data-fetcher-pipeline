"""Phase 3 — agent-boundary presentation safety.

Hostile headers, cells, links, URLs, control characters and error prose must
render inert on every automatic agent channel (stdout/stderr), while clean
display text stays byte-for-byte intact for the interactive human.
"""
import contextlib
import io
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from scripts import cli
from scripts.adapters.sanitization import (
    MAX_SCRUBBED_CHARS,
    PREVIEW_FILENAME,
    escape_code_span,
    escape_markdown_text,
    sanitize_display_text,
    scrub_exception_message,
    write_human_preview,
)
from scripts.adapters.terminal import NonInteractiveInteraction, TerminalInteraction
from scripts.base import BaseFetcher
from scripts.cli import main
from scripts.core.errors import DataFetchError
from scripts.web_analyzer import AnalysisReport, WebAnalyzer


# --- sanitize_display_text ----------------------------------------------------


def test_clean_display_text_passes_through_unchanged():
    text = 'real record 123, col|name <ok> (safe) `x`'
    assert sanitize_display_text(text) == text


@pytest.mark.parametrize('hostile,expected_keep', [
    ('\x1b[31mRED\x1b[0m plain', 'RED plain'),                     # CSI
    ('\x1b]0;title steal\x07plain', 'plain'),                      # OSC + BEL
    ('\x1b]0;title\x1b\\plain', 'plain'),                          # OSC + ST
    ('\x1b]0;swallowed by unterminated osc', ''),                  # unterminated OSC
    ('\x1b[2J\x1b[Hclear', 'clear'),                               # screen clear + cursor home
    ('\x1b]8;;https://evil.example\x1b\\link text', 'link text'),  # terminal hyperlink OSC
])
def test_ansi_escape_sequences_are_removed(hostile, expected_keep):
    out = sanitize_display_text(hostile)
    assert '\x1b' not in out
    assert out == expected_keep


def test_bidi_and_other_esc_sequences_removed_or_encoded():
    out = sanitize_display_text('\x1bcreset \x1b(Bnext')
    assert 'reset' in out and 'next' in out and '\x1b' not in out
    # A trailing bare ESC must not survive raw either.
    assert sanitize_display_text('text\x1b') == 'text\\x1b'


def test_c0_c1_controls_are_visibly_encoded():
    out = sanitize_display_text('a\x00b\x07c\x7fd\x85e')
    assert out == 'a\\x00b\\x07c\\x7fd\\x85e'
    for raw in ('\x00', '\x07', '\x7f', '\x85', '\n', '\r', '\t'):
        assert raw not in out


def test_bidi_overrides_are_visibly_encoded():
    hostile = 'safe\u202ainline\u202ereordered\u2066tail\u2069'
    out = sanitize_display_text(hostile)
    assert out == ('safe\\u202ainline\\u202ereordered'
                   '\\u2066tail\\u2069')
    for raw in '\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200e\u200f':
        assert raw not in out


def test_display_text_length_is_capped():
    assert len(sanitize_display_text('a' * 10_000)) == 120
    assert sanitize_display_text('ab', max_len=5) == 'ab'


def test_display_text_handles_non_string_input():
    assert sanitize_display_text(None) == ''
    assert sanitize_display_text(42) == '42'
    assert sanitize_display_text(1.5) == '1.5'

    class Boom:
        def __str__(self):
            raise RuntimeError('no')

    out = sanitize_display_text(Boom())
    assert 'unprintable' in out and 'Boom' in out


def test_escape_markdown_text_neutralizes_markup_and_links():
    out = escape_markdown_text('[click](https://evil.example) <script>`x`|y')
    assert out == '\\[click\\](https://evil.example) \\<script\\>\\`x\\`\\|y'


def test_escape_markdown_text_keeps_newlines_out_of_table_rows():
    out = escape_markdown_text('line1\nline2\rl3')
    assert '\n' not in out and '\r' not in out
    assert out == 'line1\\x0aline2\\x0dl3'


def test_escape_code_span_cannot_be_closed_or_linked():
    assert escape_code_span('https://example.com/d.csv') == 'https://example.com/d.csv'
    out = escape_code_span('https://evil/`x`\u202eyes')
    assert '`' not in out and '\u202e' not in out
    assert "'x'" in out  # backticks replaced so the span never closes early


# --- scrub_exception_message --------------------------------------------------


def test_scrub_redacts_url_credentials():
    msg = scrub_exception_message(
        DataFetchError('GET https://user:topsecret@example.com/path failed',
                       code='AUTH_MISSING'), 'AUTH_MISSING')
    assert 'user:topsecret' not in msg
    assert 'https://<redacted>@example.com/path' in msg
    assert 'topsecret' not in msg


def test_scrub_redacts_query_strings_and_fragments():
    msg = scrub_exception_message(
        'https://api.example.com/v2/data?api_key=SECRETKEY123&page=2 failed', 'NETWORK')
    assert 'SECRETKEY123' not in msg and 'page=2' not in msg
    assert '?<redacted>' in msg
    msg2 = scrub_exception_message('https://host/page#access_token=FRAGTOKEN', 'NETWORK')
    assert 'FRAGTOKEN' not in msg2 and '#<redacted>' in msg2


def test_scrub_withholds_http_response_bodies():
    byte_body = scrub_exception_message(
        "HTTP 500: b'{\"error\": \"stack trace with lots of body content\"}' at example.com", 'NETWORK')
    assert 'stack trace' not in byte_body and '<response body withheld>' in byte_body
    html_body = scrub_exception_message(
        'Server error: <html><body>HugeCanaryResponseBody</body></html>', 'PROVIDER_ERROR')
    assert 'HugeCanaryResponseBody' not in html_body and '<response body withheld>' in html_body


def test_scrub_drops_pandas_offending_csv_line():
    msg = scrub_exception_message(
        'Error tokenizing data. C error: Expected 1 fields in line 4, saw 3\n'
        'OFFENDING,CANARY,row content here', 'PROVIDER_ERROR')
    assert 'OFFENDING,CANARY' not in msg
    assert 'Error tokenizing data' in msg


def test_scrub_is_bounded_and_deterministic():
    big = 'x' * 50_000
    first, second = scrub_exception_message(big, 'INTERNAL'), scrub_exception_message(big, 'INTERNAL')
    assert first == second
    assert len(first) < MAX_SCRUBBED_CHARS + 200
    assert '…[truncated]' in first


def test_scrub_includes_recovery_hint_for_known_and_unknown_codes():
    known = scrub_exception_message('rate exceeded', 'RATE_LIMITED')
    assert 'rate exceeded' in known and 'rate-limit window' in known
    unknown = scrub_exception_message('mystery failure', 'TOTALLY_UNKNOWN_CODE')
    assert 'mystery failure' in unknown and 'Retry with adjusted inputs' in unknown


def test_scrub_accepts_plain_exceptions_and_strings():
    assert 'boom detail' in scrub_exception_message(RuntimeError('boom detail'), 'INTERNAL')
    assert 'plain text' in scrub_exception_message('plain text', 'INTERNAL')


# --- write_human_preview ------------------------------------------------------


def _hostile_frame() -> pd.DataFrame:
    return pd.DataFrame({
        'name\x1b]0;pwn\x07': ['A|B', 'plain'],
        'col\u202e': ['<script>x', 'l1\nl2'],
        'link': ['[click](https://evil.example)', 'ok'],
    })


def test_human_preview_writes_escaped_artifact_without_channel_leaks(tmp_path, capsys):
    path = write_human_preview(
        _hostile_frame(), 'https://example.com/PRIVATE_PATH.csv?token=SECRET_QUERY',
        'Preview <of> evil', tmp_path,
        'full transfer/extraction needed for preview', ['warn | pipe'])
    captured = capsys.readouterr()
    assert captured.out == '' and captured.err == ''
    assert path == tmp_path / PREVIEW_FILENAME and path.is_file()
    content = path.read_text(encoding='utf-8')
    # Only the source origin appears, as an inert code span.
    assert '`https://example.com`' in content
    assert '<https://example.com' not in content
    assert 'PRIVATE_PATH' not in content and 'SECRET_QUERY' not in content
    # Cells, headers, links and control chars are inert.
    assert 'A\\|B' in content
    assert '`<script>x`' in content
    assert '`[click](https://evil.example)`' in content
    assert '\x1b' not in content and '\u202e' not in content
    assert 'l1\\x0al2' in content
    # All column names are listed (the OSC-padded one is stripped to 'name').
    assert '- name' in content
    assert 'col\\u202e' in content
    # Warnings and transfer scope are escaped.
    assert 'warn \\| pipe' in content
    assert 'full transfer/extraction needed for preview' in content
    assert 'Preview \\<of\\> evil' in content


def test_human_preview_is_private_and_leaves_no_temp_files(tmp_path):
    import sys
    path = write_human_preview(_hostile_frame(), 'https://example.com', 'T',
                               tmp_path, 'scope', ())
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != PREVIEW_FILENAME]
    assert leftovers == [], f'temp debris left behind: {leftovers}'
    if sys.platform != 'win32':  # POSIX permission model
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, f'expected 0o600, got {oct(mode)}'
    else:
        assert path.stat().st_size > 0


def test_human_preview_creates_missing_outdir_and_handles_empty_frame(tmp_path, capsys):
    outdir = tmp_path / 'nested' / 'source' / 'query'
    path = write_human_preview(pd.DataFrame(), 'https://example.com', 'T',
                               outdir, 'scope', ())
    assert path.is_file()
    content = path.read_text(encoding='utf-8')
    assert 'No columns were available' in content
    assert capsys.readouterr().out == ''


# --- terminal adapters --------------------------------------------------------


def test_noninteractive_display_preview_is_a_noop(capsys):
    NonInteractiveInteraction().display_preview(_hostile_frame())
    NonInteractiveInteraction().display_preview(pd.DataFrame({'a': ['x']}))
    captured = capsys.readouterr()
    assert captured.out == '' and captured.err == ''


def test_interactive_preview_keeps_clean_text_and_neutralizes_hostile_markup(capsys):
    frame = pd.DataFrame({
        'value': [1],
        'label': ['real record'],
        'evil': ['\x1b[31mCANARY_ANSI\x1b[0m\u202eCANARY_BIDI'],
    })
    TerminalInteraction().display_preview(frame)
    captured = capsys.readouterr()
    assert 'real record' in captured.out
    assert '\x1b' not in captured.out
    assert '\u202e' not in captured.out


def test_terminal_report_is_sanitized(capsys):
    TerminalInteraction().report('[Preview transfer] \x1b]0;pwn\x07https://host\u202e tail')
    out = capsys.readouterr().out
    assert 'https://host' in out and 'tail' in out
    assert '\x1b' not in out and '\u202e' not in out


def test_approve_sample_metadata_is_sanitized(capsys):
    with pytest.raises(DataFetchError):
        with patch('builtins.input', return_value='n'):
            TerminalInteraction().approve_sample(
                {'url': 'https://host/p\x1b]0;x\x07q=1', 'size_info': 'big\u202esize'}, 5)
    out = capsys.readouterr().out
    assert 'https://host' in out and 'big' in out
    assert '\x1b' not in out and '\u202e' not in out


# --- the verified leak is closed end-to-end -----------------------------------


def test_auto_approved_preview_sends_no_cell_values_to_any_channel(tmp_path, capsys):
    class FrameFetcher(BaseFetcher):
        def scout(self):
            return {'url': 'https://example.com/data.csv'}

        def extract(self):
            return pd.DataFrame({
                'CANARY_H\x1b[eader': ['CANARY_ANSI\x1b[31m', 'plain'],
                'label': ['CANARY_BIDI\u202e', 'real record'],
            })

    fetcher = FrameFetcher('records', str(tmp_path), {}, auto_approve=True)
    fetcher.preview_only = True
    assert fetcher.run() == ''  # preview-only: no artifacts, and '' is not an error
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert 'CANARY' not in combined
    assert '\x1b' not in combined and '\u202e' not in combined
    # Lifecycle reporting still works; only the raw table is gone.
    assert 'Initiating extraction sequence' in captured.out


# --- CLI documentation parity (Phase 12) ---------------------------------------


def test_skill_md_flags_exist_in_cli_parser():
    """Every --flag documented in SKILL.md must exist in the CLI parser."""
    from scripts.cli import _build_argument_parser
    skill = Path(__file__).resolve().parents[1] / 'SKILL.md'
    documented = set(re.findall(r'(--[a-z][a-z0-9-]+)', skill.read_text(encoding='utf-8')))
    assert documented, 'SKILL.md must document the CLI flags'
    parser = _build_argument_parser()
    implemented = set()
    for action in parser._actions:
        implemented.update(action.option_strings)
    missing = sorted(documented - implemented)
    assert not missing, f'Flags documented in SKILL.md but missing from the CLI: {missing}'


class RecordsFetcher(BaseFetcher):
    def scout(self):
        return {'url': 'https://example.com/records.csv'}

    def extract(self):
        return pd.DataFrame({'value': [1, 2, 3]})


def invoke_cli(fetcher, tmp_path):
    stdout, stderr = io.StringIO(), io.StringIO()
    arguments = ['data-fetcher', '--source', 'worldbank', '--query', 'records',
                 '--outdir', str(tmp_path), '--yes', '--json-output']
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
            patch.object(sys, 'argv', arguments), \
            patch.object(cli, 'setup_wizard', return_value={}), \
            patch.object(cli, 'get_fetcher', return_value=fetcher):
        expected_stdout = sys.stdout
        try:
            cli.main()
            exit_code = 0
        except SystemExit as exc:
            exit_code = exc.code
        finally:
            restored = sys.stdout is expected_stdout
            sys.stdout = expected_stdout
    assert restored, 'CLI must restore stdout even when cleanup fails'
    return json.loads(stdout.getvalue()), exit_code


@pytest.mark.parametrize('extraction_fails', [False, True])
def test_cleanup_failure_is_visible_without_masking_extraction(tmp_path, extraction_fails):
    fetcher = RecordsFetcher('records', str(tmp_path), {})
    fetcher.cleanup = MagicMock(side_effect=PermissionError('temporary extractor is locked'))
    if extraction_fails:
        fetcher.extract = MagicMock(side_effect=DataFetchError('dataset absent', code='NOT_FOUND'))
    envelope, exit_code = invoke_cli(fetcher, tmp_path)
    fetcher.cleanup.assert_called_once()
    # Cleanup failure remains visible without forwarding exception-controlled prose.
    assert any('cleanup failed' in warning.lower() for warning in envelope['warnings'])
    assert 'temporary extractor is locked' not in json.dumps(envelope)
    if extraction_fails:
        assert exit_code == 1 and envelope['code'] == 'NOT_FOUND'
    else:
        assert exit_code == 0 and envelope['status'] == 'success'
        assert envelope['complete'] is True
        assert pd.read_csv(envelope['output_path']).value.tolist() == [1, 2, 3]


@pytest.mark.parametrize('failed_artifacts', [
    ['description'], ['manifest'], ['description', 'manifest'],
])
def test_metadata_write_failure_reports_actual_artifacts(tmp_path, failed_artifacts):
    fetcher = RecordsFetcher('records', str(tmp_path), {})
    real_open = open

    def locked_metadata(path, *args, **kwargs):
        filename = str(path)
        if ('description' in failed_artifacts and filename.endswith('_description.md.tmp')
                or 'manifest' in failed_artifacts and filename.endswith('manifest.jsonl.tmp')):
            raise PermissionError('metadata file locked')
        return real_open(path, *args, **kwargs)

    with patch('builtins.open', side_effect=locked_metadata):
        envelope, exit_code = invoke_cli(fetcher, tmp_path)
    assert exit_code == 0 and envelope['status'] == 'success'
    assert envelope['complete'] is True  # data completeness is independent of metadata
    assert pd.read_csv(envelope['output_path']).value.tolist() == [1, 2, 3]
    assert len(envelope['warnings']) == len(failed_artifacts)
    for artifact in ('description', 'manifest'):
        path = envelope[artifact + '_path']
        assert not path if artifact in failed_artifacts else Path(path).is_file()


def test_human_analysis_preserves_report_json_with_warnings(tmp_path, capsys):
    report = AnalysisReport(url='https://example.com/data.csv', difficulty='easy',
                            warnings=['Check provider license before using the records.'])
    arguments = ['data-fetcher', '--source', 'custom', '--query', report.url,
                 '--outdir', str(tmp_path), '--analyze-only']
    with patch.object(sys, 'argv', arguments), patch.object(cli, 'setup_wizard', return_value={}), \
            patch.object(WebAnalyzer, 'analyze', return_value=report):
        cli.main()
    assert json.loads(capsys.readouterr().out) == asdict(report)


def test_list_sources_v2_shape(tmp_path: Path) -> None:
    """Verify source catalog CLI discovery schema conforms to v2 machine specifications."""
    pipeline_root = Path(__file__).resolve().parent.parent
    cli_entrypoint = pipeline_root / "scripts" / "cli.py"

    process_result = subprocess.run(
        [sys.executable, str(cli_entrypoint), "--list-sources", "--json-output"],
        cwd=str(pipeline_root),
        capture_output=True,
        text=True,
        check=True,
    )
    catalog_envelope: Dict[str, Any] = json.loads(process_result.stdout.strip().splitlines()[-1])
    assert catalog_envelope.get("status") == "success"

    sources_catalog: List[Dict[str, str]] = catalog_envelope.get("sources", [])
    assert len(sources_catalog) == 13
    required_attributes = {"key", "platform", "query_format", "example", "auth"}
    for source_entry in sources_catalog:
        assert required_attributes.issubset(source_entry.keys())

    coingecko_entry = next(src for src in sources_catalog if src.get("key") == "coingecko")
    assert "none" in coingecko_entry["auth"].lower()


def test_envelope_has_source_query_echo(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Verify CLI success envelope faithfully echoes query and source identity."""
    mock_fetcher = MagicMock()
    mock_fetcher.run.return_value = str(tmp_path / "gdp_raw.csv")
    mock_fetcher.rows_count = 50
    mock_fetcher.cols_count = 2
    mock_fetcher.complete = True
    mock_fetcher.completeness_warnings = []
    mock_fetcher.truncation = None
    mock_fetcher.last_sha256 = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
    mock_fetcher.last_bytes = 1024
    mock_fetcher.last_description_path = ""
    mock_fetcher.dataset_url = "https://fred.stlouisfed.org/series/GDP"
    mock_fetcher.resolved_title = "Gross Domestic Product"

    cli_arguments = [
        "cli", "--source", "fred", "--query", "GDP",
        "--outdir", str(tmp_path), "--yes", "--non-interactive", "--json-output",
    ]
    with patch("sys.argv", cli_arguments), \
         patch("scripts.cli.setup_wizard", return_value={}), \
         patch("scripts.cli.get_fetcher", return_value=mock_fetcher):
        main()

    response_envelope: Dict[str, Any] = json.loads(capsys.readouterr().out.strip())
    assert response_envelope["source"] == "fred"
    assert response_envelope["query"] == "GDP"
