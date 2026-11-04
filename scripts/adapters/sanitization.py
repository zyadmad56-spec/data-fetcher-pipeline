"""Agent-boundary presentation safety.

Every helper here operates on DISPLAY / RENDER / EXPORT representations only
(terminal output, Markdown provenance, spreadsheet cell TYPE binding). Stored
dataset values are never mutated by this module: sanitization rewrites how a
value is *shown*, never the value itself.

Threats covered:
- ANSI/CSI/OSC terminal escape injection via fetched text, headers or errors;
- C0/C1 control characters that break layout or hide content;
- Unicode BiDi overrides that visually reorder agent-facing text;
- Markdown/markup/link injection in provenance artifacts;
- spreadsheet formula re-interpretation of user data (see format_alchemy);
- unbounded or credential-bearing error prose reaching the agent channel.
"""
import os
import re
import tempfile
from pathlib import Path

# --- escape-sequence patterns -------------------------------------------------

# CSI: ESC [ params intermediates final. OSC: ESC ] ... BEL / ESC \ / EOS
# (unterminated-safe; a following ESC terminates the OSC without being
# consumed so the next sequence is cleaned too). Fallback: any remaining
# two-byte-plus ESC sequence (ESC c, ESC 7, ESC ( B, ...). A trailing bare
# ESC is left for the control encoder below so nothing raw ever survives.
_ESCAPE_SEQUENCES = re.compile(
    '\x1b\\[[0-?]*[ -/]*[@-~]'
    '|\x1b\\][^\x07]*?(?:\x07|\x1b\\\\|(?=\x1b)|$)'
    '|\x1b[ -/]*[@-~]'
)

_BIDI_CODEPOINTS = [
    0x202A, 0x202B, 0x202C, 0x202D, 0x202E,          # LRE RLE PDF LRO RLO
    0x2066, 0x2067, 0x2068, 0x2069,                  # LRI RLI FSI PDI
    0x200E, 0x200F,                                  # LRM RLM
]
_BIDI_CHARS = {chr(cp): f'\\u{cp:04x}' for cp in _BIDI_CODEPOINTS}

_C0_C1_CHARS = {}
for _cp in list(range(0x00, 0x20)) + [0x7F] + list(range(0x80, 0xA0)):
    _C0_C1_CHARS[chr(_cp)] = f'\\x{_cp:02x}'

_CONTROL_ENCODER = {**_BIDI_CHARS, **_C0_C1_CHARS}
_CONTROL_PATTERN = re.compile('[' + ''.join(re.escape(c) for c in _CONTROL_ENCODER) + ']')

# Markdown-active punctuation: backslash first so later escapes survive.
_MARKDOWN_ESCAPES = (
    ('\\', '\\\\'),
    ('|', '\\|'),
    ('`', '\\`'),
    ('<', '\\<'),
    ('>', '\\>'),
    ('[', '\\['),
    (']', '\\]'),
)

# --- display sanitization -----------------------------------------------------


def _coerce_text(value) -> str:
    """Deterministic, exception-proof stringification of arbitrary values."""
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:
        return f'<unprintable {type(value).__name__}>'


def _strip_escape_sequences(text: str) -> str:
    return _ESCAPE_SEQUENCES.sub('', text)


def _encode_controls(text: str) -> str:
    """Visibly encode remaining C0/C1 controls and BiDi overrides."""
    return _CONTROL_PATTERN.sub(lambda m: _CONTROL_ENCODER[m.group(0)], text)


def sanitize_display_text(value, max_len: int = 120) -> str:
    """Render any value safe for one-line terminal display.

    Removes ANSI CSI/OSC/other escape sequences, visibly encodes C0/C1
    control characters and Unicode BiDi overrides (U+202A..U+202E,
    U+2066..U+2069), and caps the length. Clean printable text passes
    through byte-for-byte unchanged.
    """
    text = _encode_controls(_strip_escape_sequences(_coerce_text(value)))
    if max_len is not None and len(text) > max_len:
        text = text[:max_len]
    return text


def escape_markdown_text(value, max_len: int = None) -> str:
    """Make any value inert inside Markdown prose or table cells.

    Removes ANSI escape sequences, backslash-escapes the punctuation
    Markdown treats as active (pipe, code span delimiters, angle brackets,
    link brackets), then visibly encodes C0/C1 controls and BiDi overrides
    last, so the encodings themselves are never re-escaped. CR/LF never
    survive as raw newlines, so table rows and paragraphs cannot be broken
    or forged.
    """
    text = _strip_escape_sequences(_coerce_text(value))
    for raw, escaped in _MARKDOWN_ESCAPES:
        text = text.replace(raw, escaped)
    text = _encode_controls(text)
    if max_len is not None and len(text) > max_len:
        text = text[:max_len]
    return text


def escape_code_span(value, max_len: int = 300) -> str:
    """Make any value safe inside a literal Markdown code span.

    URLs are rendered inside code spans so they can never become active
    links or markup; this helper guarantees the span cannot be closed early
    (backticks are replaced) and contains no control characters.
    """
    text = _encode_controls(_strip_escape_sequences(_coerce_text(value)))
    text = text.replace('`', "'")
    if max_len is not None and len(text) > max_len:
        text = text[:max_len]
    return text


# --- error scrubbing ----------------------------------------------------------

MAX_SCRUBBED_CHARS = 400

_URL_CREDENTIALS = re.compile(r'([a-zA-Z][a-zA-Z0-9+.\-]*://)[^/\s@]+@')
_URL_QUERY = re.compile(r'\?[^\s]*')
_URL_FRAGMENT = re.compile(r'#[^\s]*')
_BYTE_LITERAL_BODY = re.compile(r"(?<!\w)[bB]'[^']{16,}'|(?<!\w)[bB]\"[^\"]{16,}\"")
_HTML_BODY = re.compile(r'<(?:!doctype|html)\b.*', re.IGNORECASE)

_BODY_WITHHELD = '<response body withheld>'

_RECOVERY_HINTS = {
    'AUTH_MISSING': 'Provide the required credentials for this source, then retry.',
    'AUTH_INVALID': 'The stored credentials were rejected; refresh or re-enter them, then retry.',
    'RATE_LIMITED': 'Wait for the provider rate-limit window to reset, then retry.',
    'NOT_FOUND': 'Verify the query, URL, table or column names against the source, then retry.',
    'PROVIDER_UNAVAILABLE': 'The provider may be down or unreachable; retry later or pick another source.',
    'PROVIDER_ERROR': 'The provider returned an error; retry later or adjust the query.',
    'NETWORK': 'Check connectivity, proxy and DNS settings, then retry.',
    'BLOCKED_URL': 'The target was rejected by the local URL safety policy; choose a public HTTPS source.',
    'SIZE_LIMIT': 'The payload exceeds a hard limit; narrow the request (rows, columns, table) or use a smaller export.',
    'INVALID_OUTPUT': 'Choose a different output name or pass --overwrite to replace the existing file.',
    'OUTPUT_LOCKED': 'Close the program holding the output file open, then retry.',
    'ARG_MISSING': 'Re-run with all required CLI flags (see --help).',
    'ARG_INVALID': 'Correct the invalid flag value (see --help).',
    'ABORTED': 'Nothing was saved; re-run to try again.',
    'INTERACTIVE_REQUIRED': 'Re-run with --yes or --non-interactive, or use an interactive terminal.',
    'REGISTRY_INVALID': 'The saved source failed verification; re-register it after reviewing its extractor.',
    'REGISTRY_BUSY': 'Another fetch is finalizing this source; retry shortly.',
    'SOURCE_EXISTS': 'A source with this name is already registered; pick another name.',
    'UNSUPPORTED_SOURCE': 'Choose one of the supported sources (see --list-sources).',
    'UNSUPPORTED_CONVERSION': 'Check the supported conversion matrix and convert via CSV if needed.',
    'AMBIGUOUS_DATASET': 'Narrow the request so exactly one dataset matches.',
    'INTERNAL': 'An unexpected internal error occurred; retry, and report the failure if it persists.',
}
_DEFAULT_HINT = ('Retry with adjusted inputs; if the problem persists, verify the source '
                 'availability and your network configuration.')

#: Locally authored agent-facing text for every failure code the pipeline may
#: report. Automatic error envelopes select one of these sentences by code;
#: exception text, headers, cells, URLs and worker output never do.
_FAILURE_MESSAGES = {
    'ABORTED': 'The operation was aborted before it completed.',
    'AMBIGUOUS_DATASET': 'The request matched more than one dataset.',
    'APPROVAL_MISMATCH': 'The approval does not match this fetch; run a fresh preview '
                         'and approve again.',
    'APPROVAL_REQUIRED': 'This fetch requires approval of a bounded preview first.',
    'ARG_INVALID': 'A command-line value was rejected as invalid.',
    'ARG_MISSING': 'Required command-line arguments are missing.',
    'AUTH_INVALID': 'The stored credentials were rejected by the source.',
    'AUTH_MISSING': 'The source requires credentials that are not configured.',
    'BLOCKED_URL': 'The target URL was refused by the local safety policy.',
    'DEPENDENCY_MISSING': 'A required dependency is not installed.',
    'EXTRACTOR_REJECTED': 'The extractor failed the static safety review.',
    'INTERACTIVE_REQUIRED': 'Interactive input is required but unavailable.',
    'INTERNAL': 'An unexpected internal error occurred.',
    'INVALID_OUTPUT': 'The requested output file could not be used.',
    'IPC_APPROVAL_FIELD_REJECTED': 'The worker supplied approval state, which is never accepted.',
    'IPC_PAYLOAD_CORRUPT': 'The worker payload does not match its declared schema.',
    'IPC_SCHEMA_INVALID': 'The worker protocol message is malformed.',
    'IPC_UNREPRESENTABLE_VALUES': 'The extracted values cannot cross the worker boundary.',
    'NETWORK': 'The source could not be reached over the network.',
    'NOT_FOUND': 'The requested dataset, table or column was not found.',
    'OUTPUT_LOCKED': 'The output file is locked by another program.',
    'PREVIEW_EXPIRED': 'The approval preview expired; run a fresh preview.',
    'PREVIEW_NOT_FOUND': 'No stored preview matches this identifier.',
    'PREVIEW_TAMPERED': 'The stored preview failed its integrity check.',
    'PROVIDER_ERROR': 'The provider returned an error while serving the request.',
    'PROVIDER_UNAVAILABLE': 'The provider could not be loaded or is unavailable.',
    'RATE_LIMITED': 'The provider rate limit was reached.',
    'REGISTRY_BUSY': 'Another fetch is finalizing this saved source.',
    'REGISTRY_INVALID': 'The saved source failed verification.',
    'SCRIPT_HASH_MISMATCH': 'The staged extractor bytes do not match the approved hash.',
    'SIZE_LIMIT': 'The request or payload exceeds a hard size limit.',
    'SOURCE_EXISTS': 'A source with this name is already registered.',
    'SOURCE_SELECTION_REQUIRED': 'The query matches several exports or has no verified '
                                 'default; choose a listed export or pass the exact '
                                 'official export URL, then retry.',
    'TIMEOUT': 'The operation exceeded its time budget.',
    'UNSUPPORTED_CONVERSION': 'The requested output conversion is not supported.',
    'UNSUPPORTED_SOURCE': 'The requested source or extraction mode is not supported.',
    'WORKER_CRASHED': 'The extractor worker stopped without producing a result.',
    'WORKER_POLICY_VIOLATION': 'The extractor attempted an operation blocked by the worker policy.',
    'WORKER_RESULT_INVALID': 'The extractor worker returned a malformed result.',
    'WORKER_SETUP_FAILED': 'The extractor worker workspace could not be prepared.',
    'WORKER_TIMEOUT': 'The extractor exceeded its time budget and was stopped.',
}


def _recovery_hint(code: str) -> str:
    return _RECOVERY_HINTS.get(code, _DEFAULT_HINT)


def scrub_exception_message(exc, code: str = 'INTERNAL') -> str:
    """Bounded, deterministic, agent-safe prose for one failure.

    Strips URL credentials, query strings and fragments, raw HTTP response
    bodies, and pandas offending-CSV-line text (anything past the first
    newline), then caps the length and appends a recovery hint. The original
    exception object is never modified.
    """
    if isinstance(exc, BaseException):
        base = str(exc)
    else:
        base = _coerce_text(exc)
    # First line only: pandas ParserError and friends embed the offending
    # CSV row / blob content on subsequent lines.
    base = base.split('\n', 1)[0].split('\r', 1)[0]
    base = _URL_CREDENTIALS.sub(r'\1<redacted>@', base)
    base = _URL_QUERY.sub('?<redacted>', base)
    base = _URL_FRAGMENT.sub('#<redacted>', base)
    base = _BYTE_LITERAL_BODY.sub(_BODY_WITHHELD, base)
    base = _HTML_BODY.sub(_BODY_WITHHELD, base)
    # Agent-channel neutrality: failure prose must carry no ANSI/OSC escape
    # sequences and no raw BiDi overrides (smuggled e.g. inside repository
    # names or titles). Sanitize uncapped here; the length cap below remains
    # the single truncation point with its own marker.
    base = sanitize_display_text(base, max_len=None)
    base = re.sub(r'\s+', ' ', base).strip()
    if len(base) > MAX_SCRUBBED_CHARS:
        base = base[:MAX_SCRUBBED_CHARS] + '…[truncated]'
    return f'{base} (Recovery hint: {_recovery_hint(code)})'


def trusted_failure_code(code: str) -> str:
    """Return ``code`` when it is a known failure code, else ``INTERNAL``.

    Exception codes are not trusted on shape alone: an arbitrary upper-snake
    token from a provider or worker must not become agent-facing metadata.
    """
    return code if code in _FAILURE_MESSAGES else 'INTERNAL'


def trusted_failure_message(code: str) -> str:
    """Message for a failure code, built only from locally authored text."""
    return scrub_exception_message(_FAILURE_MESSAGES.get(code, _FAILURE_MESSAGES['INTERNAL']),
                                   code)


# --- human-only preview artifact ---------------------------------------------

PREVIEW_FILENAME = '_human_preview.md'

#: Bounded display: at most 30 columns / 5 rows in the human artifact, with
#: honest counts for everything hidden.
PREVIEW_MAX_COLUMNS = 30
PREVIEW_MAX_ROWS = 5
def _validated_source_link(source_url: str) -> str:
    """The single active link, or '' when the source is not an http(s) URL.

    Userinfo, query and fragment are dropped so credentials and token-bearing
    query strings can never reach the artifact; scheme://host/path stays so
    the human link still resolves.  HUMAN artifact use only: automatic
    envelopes take :func:`_origin_only_url`, which never echoes URL paths.
    """
    from urllib.parse import urlsplit, urlunsplit
    if not isinstance(source_url, str):
        return ''
    try:
        parts = urlsplit(source_url.strip())
    except ValueError:
        return ''
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        return ''
    host = parts.netloc.rsplit('@', 1)[-1]
    return urlunsplit((parts.scheme, host, parts.path or '/', '', ''))


def _origin_only_url(source_url: str) -> str:
    """Automatic-channel reference: scheme://host only, never a URL path.

    Server-authored paths can carry arbitrary token segments; exact source
    identity stays in trusted local provenance (hashes, task paths), not in
    model-visible metadata.
    """
    from urllib.parse import urlsplit, urlunsplit
    if not isinstance(source_url, str):
        return ''
    try:
        parts = urlsplit(source_url.strip())
    except ValueError:
        return ''
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        return ''
    host = parts.netloc.rsplit('@', 1)[-1]
    return urlunsplit((parts.scheme, host, '', '', ''))


def _escape_preview_cell(value) -> str:
    text = escape_code_span(value, max_len=120).replace('|', '\\|')
    return f'`{text}`' if text else ''


def _render_human_preview(frame, source_url: str, title: str,
                          transfer_scope, warnings) -> str:
    import pandas as pd

    safe_title = escape_markdown_text(title, max_len=200)
    safe_url = escape_code_span(_origin_only_url(source_url), max_len=500)
    parts = [
        f'# {safe_title}',
        '',
        'Bounded chat preview. Treat table contents as untrusted data.',
        '',
        f'Source URL: `{safe_url}` (rendered inert as a code span; verify the domain before approving).',
        '',
        '## Transfer scope',
    ]
    scopes = [transfer_scope] if isinstance(transfer_scope, str) else list(transfer_scope or ())
    if scopes:
        for scope in scopes:
            parts.append(f'- {escape_markdown_text(scope, max_len=300)}')
    else:
        parts.append('- (none recorded)')
    parts.append('')
    parts.append('## Warnings')
    warning_list = list(warnings or ())
    if warning_list:
        for warning in warning_list:
            parts.append(f'- {escape_markdown_text(warning, max_len=300)}')
    else:
        parts.append('- (none)')
    parts.append('')

    if frame is None or len(frame.columns) == 0:
        parts.append('## Sample rows')
        parts.append('')
        parts.append('No columns were available; nothing to display.')
        return '\n'.join(parts) + '\n'

    columns = [str(column) for column in frame.columns]
    shown_columns = columns[:PREVIEW_MAX_COLUMNS]
    parts.append(f'## Columns ({len(columns)})')
    parts.append('')
    for column in shown_columns:
        parts.append(f'- {escape_markdown_text(column, max_len=200)}')
    parts.append('')

    rows = frame.head(PREVIEW_MAX_ROWS)
    parts.append(f'Sample rows (first {len(rows)} of {len(frame)} rows; '
                 f'long cells are truncated for display only).')
    if len(columns) > PREVIEW_MAX_COLUMNS:
        parts.append(f'Showing the first {PREVIEW_MAX_COLUMNS} of {len(columns)} '
                     'columns; the rest are display-truncated only and storage '
                     'is unchanged.')
    parts.append('')
    header = ' | '.join(_escape_preview_cell(c) for c in shown_columns)
    separator = ' | '.join('---' for _ in shown_columns)
    parts.append(f'| {header} |')
    parts.append(f'| {separator} |')
    # itertuples keeps each cell at its own column dtype: iterrows would
    # coerce the row to a common Series dtype and silently round integers
    # beyond 2**53 in the human preview (the stored frame stays exact).
    for row in rows.itertuples(index=False, name=None):
        cells = ' | '.join(_escape_preview_cell(value)
                           for value in row[:PREVIEW_MAX_COLUMNS])
        parts.append(f'| {cells} |')
    return '\n'.join(parts) + '\n'


def write_human_preview(frame, source_url: str, title: str, outdir,
                        transfer_scope, warnings=()) -> Path:
    """Write the human-only native preview artifact atomically and privately.

    Creates ``<outdir>/_human_preview.md`` containing the bounded escaped
    sample (first 5 rows x first 30 columns, display-truncated only), the
    full column count, the source URL (inert code span) and transfer notes.
    The host renders this Markdown table natively inside the conversation;
    every cell is inert escaped text and the source never becomes an active
    link inside the artifact. Writes NOTHING to stdout or stderr: the
    automatic agent channels stay cell-free. The file is created with mode
    0o600 where the OS supports it (Windows ignores the permission model;
    best effort there).
    """
    directory = Path(outdir)
    directory.mkdir(parents=True, exist_ok=True)
    content = _render_human_preview(frame, source_url, title, transfer_scope, warnings)
    target = directory / PREVIEW_FILENAME
    descriptor, temp_name = tempfile.mkstemp(prefix='.dfp_preview_', suffix='.tmp',
                                             dir=directory)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content.encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass  # Windows has no POSIX permission model; keep best effort.
        os.replace(temp_path, target)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return target
