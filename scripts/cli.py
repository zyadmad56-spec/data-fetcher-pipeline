import sys
from pathlib import Path

# Bootstrap path when executed directly as a script
if __name__ == "__main__":
    parent_dir = str(Path(__file__).resolve().parent.parent)
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)

import json
import argparse
from dataclasses import dataclass, field
from typing import Optional

# Guaranteed startup envelope: if the engine itself fails to import (broken
# dependency, corrupt install), the CLI must still answer in JSON with exit 3
# rather than dying with a raw traceback and an empty stdout.
_startup_error: Optional[Exception] = None
try:
    from scripts.config import setup_wizard
    from scripts.errors import DataFetchError
    from scripts.factory import get_fetcher, list_sources
except Exception as _exc:  # pragma: no cover - exercised via import-failure tests
    _startup_error = _exc

def _fallback_code(exc: Exception) -> str:
    """Derive a stable error code for exceptions raised without one."""
    if isinstance(exc, ImportError):
        return "DEPENDENCY_MISSING"
    msg = str(exc).lower()
    if isinstance(exc, OSError) and ("file exists" in msg or "cannot create" in msg):
        return "INVALID_OUTPUT"
    if msg.startswith("blocked"):
        return "BLOCKED_URL"
    if "rate limit" in msg or "429" in msg or "too many requests" in msg:
        return "RATE_LIMITED"
    if "api key" in msg or "credentials" in msg:
        return "AUTH_MISSING"
    if "unsupported data source" in msg:
        return "UNSUPPORTED_SOURCE"
    if "requires a" in msg and "source" in msg:
        # "Conversion from X to Y requires a ... source." — an unsupported pair
        return "UNSUPPORTED_CONVERSION"
    if "not supported" in msg and ("format" in msg or "conversion" in msg):
        return "UNSUPPORTED_CONVERSION"
    if "not found" in msg or "does not exist" in msg or "no cryptocurrency" in msg or "invalid indicator" in msg:
        return "NOT_FOUND"
    if "connection" in msg or "timeout" in msg or "network" in msg:
        return "NETWORK"
    return "PROVIDER_ERROR"

def _gc_orphan_dir(fetcher) -> None:
    """Remove the per-fetch directory when a failed run left it empty (no debris)."""
    try:
        outdir = getattr(fetcher, "outdir", None)
        if not outdir:
            return
        d = Path(outdir)
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
            parent = d.parent
            if parent.is_dir() and parent.name != Path(outdir).drive and not any(parent.iterdir()) and parent != Path(outdir).anchor:
                try:
                    parent.rmdir()
                except OSError:
                    pass
    except (OSError, TypeError):
        pass

def _scrubbed_warnings(warnings_list: list) -> list:
    """Neutralize externally sourced text inside envelope warnings."""
    from scripts.adapters.sanitization import sanitize_display_text
    return [sanitize_display_text(str(w), max_len=500) for w in warnings_list]

def _withheld_advisories(warnings_list: list) -> list:
    """Replace fetcher advisory prose with a controlled count.

    Fetcher warnings interleave locally authored text with external content
    (provider bodies, repo paths, guard exceptions).  Escaping is not semantic
    neutralization, so the automatic envelope reports only the count; the
    escaped detail stays in the human-only preview artifact.
    """
    count = len(warnings_list)
    if not count:
        return []
    return [f'{count} retrieval advisory(ies) withheld from automatic channels; '
            'the human preview artifact carries the escaped detail.']

def _fetch_envelope_meta(fetcher) -> dict:
    """Build envelope metadata from fetcher state, defensively (mock-safe)."""
    def typed(attr, default):
        val = getattr(fetcher, attr, default)
        return val if isinstance(val, type(default)) else default

    # Trusted channel: locally authored degraded/artifact-write notes keep
    # their sanitized text.  Mixed-origin slots (external content can land in
    # completeness/artifact warnings) are withheld to a controlled count.
    degraded = getattr(fetcher, 'degraded_warnings', [])
    degraded_texts = [str(w) for w in degraded] if isinstance(degraded, list) else []
    warnings_list = _scrubbed_warnings(degraded_texts)
    mixed_count = 0
    for slot in ('completeness_warnings', 'artifact_warnings'):
        slot_raw = getattr(fetcher, slot, [])
        if isinstance(slot_raw, list):
            # Notes already surfaced through the trusted degraded channel are
            # not double-counted as withheld.
            mixed_count += sum(1 for w in slot_raw if str(w) not in degraded_texts)
    if mixed_count:
        warnings_list.extend(_withheld_advisories(['x'] * mixed_count))
    complete_raw = getattr(fetcher, "complete", True)
    complete = complete_raw if isinstance(complete_raw, bool) else True

    trunc_raw = getattr(fetcher, "truncation", None)

    from scripts.adapters.sanitization import _origin_only_url
    return {
        # Metadata boundary: titles are arbitrary external text and URLs may
        # carry token-bearing query AND path segments — the automatic envelope
        # keeps a scheme://host origin reference only, and withholds advisory
        # prose from automatic channels.
        "source_url": _origin_only_url(typed("dataset_url", "")),
        "resolved_title": None,
        "sha256": typed("last_sha256", ""),
        "bytes": typed("last_bytes", 0),
        "description_path": typed("last_description_path", ""),
        "manifest_path": typed("last_manifest_path", ""),
        "original_path": typed("last_original_path", ""),
        "original_sha256": typed("last_original_sha256", ""),
        "original_bytes": typed("last_original_bytes", 0),
        "complete": complete,
        "warnings": warnings_list,
        "truncation": trunc_raw if isinstance(trunc_raw, dict) else None,
    }

def interactive_flow(outdir=None):
    from scripts.workflow import interview
    return interview(outdir)


def run_conversion(target_format: str, filepath: str, overwrite: bool = False,
                   lexical: bool = True) -> str:
    from scripts.composition import convert_file
    return convert_file(target_format, filepath, overwrite, lexical)

@dataclass
class _RunState:
    fetcher: object = None
    warnings: list = field(default_factory=list)


def _build_argument_parser():
    """Assemble the CLI parser (split from parsing so tests can inspect flags)."""
    parser = argparse.ArgumentParser(description="Data Fetcher Background Engine")
    parser.add_argument("--source", required=False, help="Target data platform (e.g., yfinance, fred, airbnb)")
    parser.add_argument("--query", required=False, help="Topic, ticker symbol, or series ID")
    parser.add_argument("--outdir", default=str(Path.cwd() / "data_raw"), help="Destination directory")

    parser.add_argument("--non-interactive", action="store_true", help="Run without user input prompt blocks")
    parser.add_argument("--yes", action="store_true", help="Auto-approve pre-flight data downloads")
    parser.add_argument("--rows", type=int, default=0, help="Save only the first N rows (transfer savings depend on provider)")
    parser.add_argument("--preview-only", action="store_true", help="Write the human-only preview artifact; do not save dataset")
    parser.add_argument("--analyze-only", action="store_true", help="Assess custom URL without extraction")
    parser.add_argument("--extractor-script", metavar="PATH", help="Run this reviewed extractor file for --source custom (bounded preview first; approval uses the staged bytes)")
    parser.add_argument("--approve-preview", metavar="ID", help="Approve a bounded preview by its preview ID for a full custom fetch (runs the staged extractor bytes)")
    parser.add_argument("--resume-preview", metavar="ID", help="Resume a cached built-in preview from --outdir/.previews by ID (claims it once; no source/query re-entry)")
    parser.add_argument("--discard-preview", metavar="ID", help="Remove one cached built-in preview record from --outdir/.previews (no network, no source/query)")
    parser.add_argument("--save-source", metavar="NAME", help="Register a custom source after successful fetch")
    parser.add_argument("--defer-source-choice", action="store_true", help="Keep a pending custom extractor while the agent asks whether to register it")
    parser.add_argument("--keep-source", metavar="ID", help="Register a pending extractor using --save-source NAME; no new download")
    parser.add_argument("--discard-source", metavar="ID", help="Discard a pending extractor; preserve downloaded datasets")
    parser.add_argument("--columns", default="", help="Required column names, comma separated; project exact names")
    parser.add_argument("--table", type=int, help="Zero-based HTML table index for custom extraction")
    class _ExplicitOutputFormat(argparse.Action):
        """Record whether the caller actually passed --output-format: the
        resume continuation must never let the parser default silently
        override a preview's recorded output choice."""
        def __call__(self, parser, namespace, values, option_string=None):
            setattr(namespace, self.dest, values)
            setattr(namespace, 'output_format_explicit', True)

    parser.add_argument("--output-format", default="csv", action=_ExplicitOutputFormat,
                        choices=['csv', 'json', 'excel', 'xlsx', 'parquet', 'sqlite', 'db'])
    parser.set_defaults(output_format_explicit=False)
    parser.add_argument("--goal", default="", help="Human-readable request; not an automatic content filter")
    parser.add_argument("--list-sources", action="store_true", help="Print supported data sources and exit")
    parser.add_argument("--json-output", action="store_true", help="Provide machine-readable JSON output")
    parser.add_argument("--convert", nargs=2, metavar=("FORMAT", "FILE"), help="Convert FILE to FORMAT")
    parser.add_argument("--overwrite", action="store_true", help="Allow --convert to replace an existing output file")
    return parser


def _parse_arguments():
    return _build_argument_parser().parse_args()


def _source_choice(args):
    from scripts.composition import finish_pending_source as finish_source_choice
    if (args.keep_source and args.discard_source) or (args.keep_source and not args.save_source):
        raise DataFetchError('Choose --keep-source ID --save-source NAME or --discard-source ID.', code='ARG_MISSING')
    key = finish_source_choice(args.keep_source or args.discard_source,
                               args.save_source if args.keep_source else None)
    return {'status': 'success', 'registered_source': key,
            'source_choice': 'kept' if key else 'discarded'}


def _source_listing():
    from scripts.factory import source_info
    catalog = source_info()
    return {'status': 'success', 'sources': [
        {'key': key, 'platform': catalog[key].get('platform', key.title()),
         'query_format': catalog[key].get('query_format', 'free text'),
         'example': catalog[key].get('example', ''), 'auth': catalog[key].get('auth', 'none'),
         **({'website': catalog[key]['website']} if 'website' in catalog[key] else {})}
        for key in list_sources()
    ]}


def _fetch_config(args):
    config = setup_wizard(non_interactive=True)
    config['non_interactive'] = args.non_interactive or args.json_output or args.yes
    if args.json_output and not (args.yes or args.non_interactive):
        raise DataFetchError(
            '--json-output requires --yes or --non-interactive: interactive '
            'pre-flight prompts cannot be served in JSON mode.', code='INTERACTIVE_REQUIRED')
    return config


def _resolve_request(args):
    if args.source and args.query:
        return
    if args.non_interactive or args.json_output:
        raise DataFetchError('Source and Query arguments are required in non-interactive mode.',
                             code='ARG_MISSING')
    request = interactive_flow(args.outdir)
    args.source, args.query, args.outdir = request.source, request.query, request.outdir
    args.rows, args.output_format = request.rows, request.output_format
    args.goal, args.columns = request.goal, ','.join(request.columns)


def _validate_fetch_options(args):
    if args.save_source and (args.source.lower() != 'custom' or args.preview_only or args.analyze_only):
        raise DataFetchError('--save-source needs a successful full custom fetch.', code='ARG_MISSING')
    if args.defer_source_choice and (args.source.lower() != 'custom' or args.preview_only
                                    or args.analyze_only or args.save_source):
        raise DataFetchError('--defer-source-choice needs a full custom fetch without --save-source.', code='ARG_MISSING')
    if args.table is not None and args.table < 0:
        raise DataFetchError('--table must be a non-negative index.', code='ARG_MISSING')
    if args.preview_only and args.analyze_only:
        raise DataFetchError('Choose preview or analysis, not both.', code='ARG_MISSING')
    if args.analyze_only and args.source.lower() != 'custom':
        raise DataFetchError('--analyze-only requires --source custom.', code='ARG_MISSING')
    is_custom = args.source is not None and (
        args.source.lower() == 'custom' or args.source.lower().startswith('custom_'))
    if args.extractor_script:
        if args.source is None or args.source.lower() != 'custom':
            raise DataFetchError('--extractor-script requires --source custom.', code='ARG_MISSING')
        if args.analyze_only:
            raise DataFetchError('--extractor-script cannot be combined with --analyze-only.', code='ARG_MISSING')
        if args.approve_preview:
            raise DataFetchError('--approve-preview runs the extractor bytes stored with the approved '
                                 'preview; do not pass --extractor-script with it.', code='ARG_MISSING')
    if args.approve_preview:
        if not is_custom:
            raise DataFetchError('--approve-preview requires a custom source (--source custom or a saved custom_* key).', code='ARG_MISSING')
        if args.preview_only:
            raise DataFetchError('--preview-only creates a new preview; --approve-preview belongs to the full fetch.', code='ARG_MISSING')
        if args.analyze_only:
            raise DataFetchError('--analyze-only performs no extraction; --approve-preview is not needed.', code='ARG_MISSING')


def _configure_fetcher(fetcher, args):
    fetcher.auto_approve = args.yes or args.non_interactive
    if args.rows > 0:
        fetcher.row_limit = args.rows
    fetcher.preview_only = args.preview_only
    fetcher.required_columns = list(dict.fromkeys(c.strip() for c in args.columns.split(',') if c.strip()))
    fetcher.requested_goal = args.goal
    fetcher.table_index = args.table


def _preview_envelope(fetcher, source, record=None):
    """Agent-facing preview envelope: ZERO cell values, ZERO column names.

    The bounded display sample (first 5 rows x first 30 columns) is written
    to the human-only Markdown table artifact, which the chat host renders
    natively inside the conversation.  The envelope carries only bounded
    counts, at most 30 dtype tokens, the validated display reference, the
    validated direct source URL for the human table, and (for flows with a
    persistent record or receipt) the opaque preview ID that a later
    resume/approve command must present.
    """
    from scripts.adapters.sanitization import (
        write_human_preview, _origin_only_url, _validated_source_link)
    frame = fetcher.preview_frame
    warnings = _fetch_envelope_meta(fetcher)['warnings']
    try:
        scope = fetcher.preview_transfer_scope()
    except Exception:
        scope = getattr(fetcher, 'transfer_scope', '') or ''
    title = f'{source} dataset preview'
    if record is not None:
        # Validated presentation reference at the handoff boundary: identity,
        # containment and artifact hash are re-checked before it is returned.
        preview_path = record._presentation_validated()
    else:
        preview_path = write_human_preview(
            frame, str(getattr(fetcher, 'dataset_url', '')), title,
            Path(getattr(fetcher, 'outdir', '.')), scope, warnings)
    dtypes = [str(dtype) for dtype in frame.dtypes]
    dataset_url = str(getattr(fetcher, 'dataset_url', ''))
    # The human chat table may carry the full URL path only when the source
    # URL is verified public (verbatim from a shipped reviewed registry);
    # user-typed URLs keep origin-only display there, since private tokens
    # can appear in URL paths.
    if bool(getattr(fetcher, 'dataset_url_verified_public', False)):
        display_url = _validated_source_link(dataset_url)
    else:
        display_url = _origin_only_url(dataset_url)
    envelope = {
        'status': 'success', 'mode': 'preview', 'source': source,
        'source_url': _origin_only_url(dataset_url),
        'source_url_display': display_url,
        'preview_path': str(Path(preview_path).resolve()),
        'preview_display': 'native-markdown-table',
        'rows_sampled': int(len(frame)),
        'column_count': int(len(frame.columns)),
        'dtypes': dtypes[:30],
        'transfer_scope': scope, 'output_path': None,
        'warnings': warnings,
    }
    if len(dtypes) > 30:
        envelope['dtypes_truncated'] = True
    receipt = getattr(fetcher, '_pending_receipt', None)
    if receipt is not None:
        envelope['preview_id'] = receipt.preview_id
    elif record is not None:
        envelope['preview_id'] = record.preview_id
        envelope['cache'] = {'complete': bool(record.complete),
                             'scope': 'complete cached bytes' if record.complete
                             else 'bounded sample only; a resumed fetch transfers the full dataset'}
    return envelope


def _retain_custom_source(fetcher, args):
    from scripts.composition import retain_custom_source
    return retain_custom_source(fetcher, args)


def _network_envelope(source, metrics):
    custom = source.lower() == 'custom' or source.lower().startswith('custom_')
    sdk = source.lower() in {'yahoo', 'yfinance', 'openml', 'kaggle'}
    scope = ('unavailable: extractor runs in an isolated worker' if custom else
             'unavailable: provider SDK uses its own HTTP client' if sdk else
             'instrumented HTTP client only')
    return {'provider_requests': None if sdk or custom else metrics['requests'],
            'retries': None if sdk or custom else metrics['retries'],
            'bytes_transferred': None if sdk or custom else metrics['bytes'],
            'metrics_scope': scope}


def _saved_envelope(fetcher, args, csv_path):
    from datetime import datetime, timezone
    rows = getattr(fetcher, 'rows_count', 0)
    columns = getattr(fetcher, 'cols_count', 0)
    envelope = {'status': 'success', 'source': args.source, 'query': args.query,
                'requested_goal': args.goal, 'output_path': str(Path(csv_path).resolve()),
                'rows': rows if isinstance(rows, int) else 0,
                'columns': columns if isinstance(columns, int) else 0,
                'fetched_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
    envelope.update(_fetch_envelope_meta(fetcher))
    for key in ('description_path', 'manifest_path', 'original_path'):
        if envelope[key]:
            envelope[key] = str(Path(envelope[key]).resolve())
    envelope['original_path'] = envelope['original_path'] or None
    return envelope


def _prepare_fetch(args):
    from scripts.base import provision_data_directory
    config = _fetch_config(args)
    _resolve_request(args)
    if args.rows < 0:
        raise DataFetchError(f'--rows must be a non-negative integer (got {args.rows}).', code='ARG_MISSING')
    source_key = {'yfinance': 'yahoo'}.get(args.source.lower(), args.source.lower())
    provision_data_directory(args.outdir, source_key=source_key)
    _validate_fetch_options(args)
    return config


def _analyze_source(args):
    from scripts.web_analyzer import WebAnalyzer
    from scripts.adapters.sanitization import sanitize_display_text
    from dataclasses import asdict
    report = WebAnalyzer(args.query, str(Path(args.outdir) / 'custom')).analyze()
    data = asdict(report)
    for key, value in data.items():
        if key == 'warnings' and isinstance(value, list):
            data[key] = [sanitize_display_text(str(w), max_len=300) for w in value]
        elif isinstance(value, str):
            data[key] = sanitize_display_text(value, max_len=300)
        elif isinstance(value, dict):
            data[key] = {str(k): sanitize_display_text(str(v), max_len=200) for k, v in value.items()}
    return {'status': 'success', 'mode': 'analysis', **data}


def _fetch_dataset(args, state):
    from scripts.http_utils import reset_metrics, get_metrics
    from scripts.composition import resolve_custom_inputs
    config = _prepare_fetch(args)
    if args.analyze_only:
        return _analyze_source(args)
    extractor_bytes, approved_receipt = resolve_custom_inputs(args)
    state.fetcher = get_fetcher(args.source, args.query, args.outdir, config,
                                extractor_bytes=extractor_bytes,
                                approved_receipt=approved_receipt)
    _configure_fetcher(state.fetcher, args)
    reset_metrics()
    csv_path = state.fetcher.run()
    metrics = get_metrics()
    if args.preview_only:
        record = _persist_builtin_preview(args, state.fetcher)
        return _preview_envelope(state.fetcher, args.source, record=record)
    from scripts.application.complete import complete_dataset
    from functools import partial
    converted, registered, pending = complete_dataset(
        csv_path, args.output_format, partial(run_conversion, lexical=False),
        lambda: _retain_custom_source(state.fetcher, args))
    envelope = _saved_envelope(state.fetcher, args, csv_path)
    envelope.update(_network_envelope(args.source, metrics))
    envelope.update(converted_file=converted, registered_source=registered, pending_source_id=pending)
    return envelope


def _discard_preview(args):
    """No-network decline of one cached built-in preview (task-contained)."""
    from scripts.adapters.preview_store import PreviewStore
    store = PreviewStore(Path(args.outdir) / '.previews')
    discarded = store.discard(args.discard_preview)
    return {'status': 'success', 'discarded': bool(discarded),
            'preview_id': args.discard_preview,
            'note': 'already absent' if not discarded else 'record removed'}


def _persist_builtin_preview(args, fetcher):
    """Persist the built-in preview record with honest completeness.

    When the preview transfer ended within the cap the run already owns the
    complete source bytes (``_temp_payload`` set by the exhausted prefix
    read) and they become the record's payload of record with
    ``complete=True``.  Otherwise the record is a bounded 5-row sample with
    ``complete=False`` — five sample rows are never labeled a complete
    result.  Custom sources keep their receipt flow and create no store
    record.  Returns the record, or None when no record applies.
    """
    source = str(args.source or '')
    if source == 'custom' or source.startswith('custom_'):
        return None
    if getattr(fetcher, '_pending_receipt', None) is not None:
        return None
    from scripts.adapters.preview_store import PreviewStore, PreviewIdentity
    sample = getattr(fetcher, 'preview_frame', None)
    if sample is None:
        return None
    columns = getattr(fetcher, 'required_columns', None)
    identity = PreviewIdentity(
        source=source, query=str(args.query or ''),
        columns=tuple(columns) if isinstance(columns, (list, tuple)) else (),
        row_limit=int(args.rows or 0),
        output_format=str(args.output_format or 'csv').lower())
    complete = False
    payload_bytes = None
    payload_path = getattr(fetcher, '_temp_payload', None)
    if payload_path is not None and getattr(fetcher, '_preview_payload_owned', False):
        try:
            payload_bytes = Path(payload_path).read_bytes()
            complete = True
        finally:
            # The persistence step owns the kept bytes' cleanup.
            try:
                Path(payload_path).unlink(missing_ok=True)
            except OSError:
                pass
            fetcher._temp_payload = None
            fetcher._preview_payload_owned = False
    parse = None
    if payload_bytes is not None:
        sep_fn = getattr(fetcher, '_payload_sep', None)
        comp_fn = getattr(fetcher, '_payload_compression', None)
        parse = {'sep': sep_fn() if callable(sep_fn) else ',',
                 'compression': comp_fn() if callable(comp_fn) else None}
    warnings = []
    for slot in ('completeness_warnings', 'degraded_warnings', 'artifact_warnings'):
        slot_raw = getattr(fetcher, slot, [])
        if isinstance(slot_raw, list):
            warnings.extend(str(w) for w in slot_raw)
    try:
        scope = fetcher.preview_transfer_scope()
    except Exception:
        scope = getattr(fetcher, 'transfer_scope', '') or ''
    store = PreviewStore(Path(args.outdir) / '.previews')
    return store.create(identity, sample, complete=complete,
                        payload_bytes=payload_bytes, parse=parse,
                        source_url=str(getattr(fetcher, 'dataset_url', '') or ''),
                        warnings=warnings, transfer_scope=str(scope or ''))


def _explicit_output_format(args) -> str | None:
    """The output format only when the caller actually passed the flag."""
    explicit = getattr(args, 'output_format_explicit', False)
    return str(args.output_format or '').lower() if explicit else None


def _frame_from_record(record):
    """Typed pipeline frame from a record: exact source bytes re-typed under
    the recorded provider parse contract (separator/compression), or the
    codec frame.  Never the schema-less lexical standalone read."""
    import io
    import pandas as pd
    payload_bytes = getattr(record, 'payload_bytes', None)
    if payload_bytes:
        parse = getattr(record, 'parse', None) or {}
        return pd.read_csv(io.BytesIO(payload_bytes), sep=parse.get('sep', ','),
                           compression=parse.get('compression'), low_memory=False)
    return record.frame


def _restore_provenance(state_fetcher, record):
    """Restore source identity and original bytes before normal persistence
    so the profile, manifest and retained original describe the REAL source.

    The cached bytes are copied to a disposable file inside the record
    directory; the retention step renames it into the canonical
    ``_source_<sha>`` artifact, so the store's own payload is never moved.
    """
    payload_bytes = getattr(record, 'payload_bytes', None)
    parse = getattr(record, 'parse', None) or {}
    if payload_bytes:
        restore = Path(record._directory) / 'restore.tmp'
        restore.write_bytes(payload_bytes)
        state_fetcher._temp_payload = restore
    source_url = getattr(record, 'source_url', '') or ''
    if source_url:
        state_fetcher.dataset_url = source_url
    state_fetcher.payload_compression = parse.get('compression')
    state_fetcher.payload_sep = parse.get('sep', ',')


def _resume_preview(args):
    """Continue an approved built-in preview through the recorded intent.

    One explicit human approval (the claimed record) authorizes exactly the
    recorded source, query, columns, row limit and output format; supplied
    selection flags and output-format drift are rejected before any claim,
    transfer or write.  A complete record is served from its owned bytes with
    zero further transfers through the normal artifact persistence path
    (profile, manifest, provenance); a bounded record runs the recorded
    request in full through the normal fetch pipeline — no manual redo.  A
    recoverable filesystem failure releases the claim instead of losing the
    approval; duplicates cannot double-save.
    """
    from scripts.adapters.preview_store import PreviewStore
    store = PreviewStore(Path(args.outdir) / '.previews')
    record = store.load(args.resume_preview)
    identity = record.identity
    explicit = _explicit_output_format(args)
    if explicit and explicit != identity.output_format:
        raise DataFetchError(
            'The supplied --output-format does not match the approved preview '
            'request; run a fresh preview to change the output intent.',
            code='APPROVAL_MISMATCH')
    requested_format = identity.output_format or 'csv'
    # Approval-handoff revalidation: changed/stale/foreign artifacts fail
    # here, BEFORE the claim, any transfer or any write.
    store.verify_ready(args.resume_preview)
    record = store.claim(args.resume_preview, identity)
    import hashlib as _hashlib
    try:
        config = _fetch_config(args)
        state_fetcher = get_fetcher(identity.source, identity.query, args.outdir, config)
        state_fetcher.required_columns = list(identity.columns)
        state_fetcher.row_limit = int(identity.row_limit or 0)
        state_fetcher.auto_approve = True
        state_fetcher.preview_only = False
        progress = store.load_progress(args.resume_preview)
        if progress:
            # Recovery receipt: the fetch/save transaction already completed
            # from the owned bytes; finish conversion with no re-transfer.
            csv_path = Path(progress['csv_path'])
            digest = _hashlib.sha256(csv_path.read_bytes()).hexdigest()
            if not csv_path.is_file() or digest != progress.get('sha256'):
                raise DataFetchError('The recovery receipt does not match the saved dataset.',
                                     code='PREVIEW_TAMPERED')
            state_fetcher.rows_count = int(progress.get('rows', 0) or 0)
            state_fetcher.cols_count = int(progress.get('cols', 0) or 0)
        elif record.complete:
            from scripts.adapters.artifacts import FileArtifactStore
            from scripts.application.frame_policy import limit_rows, select_columns
            from scripts.core.names import sanitize_name
            frame = _frame_from_record(record)
            frame = select_columns(frame, state_fetcher.required_columns)
            frame = limit_rows(frame, state_fetcher, None)
            _restore_provenance(state_fetcher, record)
            csv_path = FileArtifactStore(state_fetcher).save_csv(
                frame, sanitize_name(identity.query) + '_raw.csv')
        else:
            source_url = getattr(record, 'source_url', '') or ''
            if source_url:
                state_fetcher.dataset_url = source_url
            csv_path = state_fetcher.run()
            if not csv_path:
                raise DataFetchError('The approved continuation produced no dataset.',
                                     code='INTERNAL')
        store.write_progress(args.resume_preview, {
            'stage': 'saved', 'csv_path': str(csv_path),
            'sha256': _hashlib.sha256(Path(csv_path).read_bytes()).hexdigest(),
            'rows': int(getattr(state_fetcher, 'rows_count', 0) or 0),
            'cols': int(getattr(state_fetcher, 'cols_count', 0) or 0)})
        from scripts.application.complete import complete_dataset
        from functools import partial
        converted, registered, pending = complete_dataset(
            str(csv_path), requested_format, partial(run_conversion, lexical=False),
            lambda: (None, None))
        store.clear_progress(args.resume_preview)
    except Exception:
        # A recoverable local fault anywhere in the save/convert lifecycle
        # never consumes the human approval. When the completion receipt was
        # written, a retry finishes from the already owned bytes — releasing
        # the claim cannot duplicate a transfer or commit.
        store.release_claim(args.resume_preview)
        raise
    envelope = {'status': 'success', 'resumed_preview': args.resume_preview,
                'source': identity.source, 'query': identity.query,
                'output_format': requested_format,
                'output_path': str(Path(csv_path).resolve()),
                'rows': int(getattr(state_fetcher, 'rows_count', 0) or 0),
                'columns': int(getattr(state_fetcher, 'cols_count', 0) or 0),
                'complete': bool(getattr(state_fetcher, 'complete', True))}
    envelope.update(_fetch_envelope_meta(state_fetcher))
    envelope.update({'converted_file': converted, 'registered_source': registered,
                     'pending_source_id': pending})
    return envelope


def _execute(args, state):
    if args.discard_preview:
        if any([args.source, args.query, args.convert, args.list_sources,
                args.resume_preview, args.approve_preview]):
            raise DataFetchError(
                '--discard-preview is a no-network cache operation; it accepts '
                'only --outdir and --json-output.', code='ARG_INVALID')
        return _discard_preview(args)
    if args.resume_preview:
        if args.source or args.query or args.rows or args.columns \
                or args.approve_preview or args.convert or args.list_sources \
                or args.preview_only:
            raise DataFetchError(
                '--resume-preview rejects selection changes; the cached request '
                'identity governs the fetch.', code='ARG_INVALID')
        return _resume_preview(args)
    if args.keep_source or args.discard_source:
        return _source_choice(args)
    if args.list_sources:
        return _source_listing()
    if args.convert:
        target_format, filepath = args.convert
        output = run_conversion(target_format, filepath, overwrite=args.overwrite)
        return {'status': 'success', 'converted_file': output, 'source_file': filepath}
    return _fetch_dataset(args, state)


def _failure_envelope(exc):
    if isinstance(exc, DataFetchError):
        code, exit_code = exc.code, exc.exit_code
    elif isinstance(exc, (KeyboardInterrupt, EOFError)):
        code = 'ABORTED' if isinstance(exc, KeyboardInterrupt) else 'INTERACTIVE_REQUIRED'
        return {'status': 'error', 'code': code, 'message':
                'Interactive input required but unavailable. Add --yes flag for non-interactive mode.'}, (
                    130 if isinstance(exc, KeyboardInterrupt) else 1)
    elif isinstance(exc, (ValueError, RuntimeError, ImportError, OSError)):
        code, exit_code = _fallback_code(exc), 1
    else:
        code, exit_code = 'INTERNAL', 1
    # Envelopes carry a known code and locally authored prose only; exception
    # text may hold fetched cells, headers, URLs or worker output.
    from scripts.adapters.sanitization import trusted_failure_code, trusted_failure_message
    code = trusted_failure_code(code)
    return {'status': 'error', 'code': code,
            'message': trusted_failure_message(code)}, exit_code


def _cleanup_fetcher(fetcher):
    cleanup = getattr(fetcher, 'cleanup', None)
    if not callable(cleanup):
        return []
    try:
        cleanup()
        return []
    except Exception:
        # A provider cleanup hook may raise any library exception. Keep the
        # primary fetch result and disclose the failure without any exception
        # detail: both its prose and its class name are attacker-chosen text
        # for reviewed-but-untrusted extractor code.
        return ['Temporary extractor cleanup failed.']


def _print_sources(sources, stdout):
    print('\nSupported Data Sources:\n' + '-' * 55, file=stdout)
    print(f"{'CLI Key':<15} | {'Platform / Title':<35}\n" + '-' * 55, file=stdout)
    for source in sources:
        website = source.get('website')
        label = f"{source['platform']} ({website})" if website else source['platform']
        print(f"{source['key']:<15} | {label:<35}", file=stdout)
    print('-' * 55, file=stdout)


def _print_response(envelope, args, stdout):
    if args.json_output:
        print(json.dumps(envelope), file=stdout)
    elif envelope['status'] == 'error':
        print('\n[Error] ' + envelope['message'], file=stdout)
    elif args.keep_source or args.discard_source:
        print(str(envelope), file=stdout)
    elif args.list_sources:
        _print_sources(envelope['sources'], stdout)
    elif envelope.get('mode') == 'analysis':
        print(json.dumps({k: v for k, v in envelope.items() if k not in ('status', 'mode')},
                         indent=2), file=stdout)
        return
    elif not args.convert and not args.preview_only:
        print('[Engine] Extraction Complete. Pipeline exiting successfully.', file=stdout)
    if not args.json_output:
        for warning in envelope.get('warnings', []):
            print('[Warning] ' + warning, file=stdout)


class _ProgressCapture:
    """Bounded in-memory sink for internal progress prints in --json-output mode.

    Provider ``print()`` progress never reaches the agent-facing channels: the
    first 16 KiB are retained here for diagnostics and the overflow is
    discarded. The JSON envelope printed to the real stdout remains the only
    machine-readable artifact.
    """

    def __init__(self, cap_bytes: int = 16 * 1024) -> None:
        self._cap = cap_bytes
        self._chunks: list = []
        self._kept = 0
        self.discarded = 0

    def write(self, text) -> int:
        text = str(text)
        room = self._cap - self._kept
        if room > 0:
            self._chunks.append(text[:room])
            self._kept += min(len(text), room)
        self.discarded += max(len(text) - room, 0)
        return len(text)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False

    def getvalue(self) -> str:
        return ''.join(self._chunks)


def main() -> None:
    if _startup_error is not None:
        print(json.dumps({'status': 'error', 'code': 'STARTUP_FAILURE',
                          'message': 'Engine failed to initialize. '
                                     'Reinstall dependencies from pyproject.toml.'}))
        sys.exit(3)
    args = _parse_arguments()
    state = _RunState()
    real_stdout, exit_code = sys.stdout, 0
    if args.json_output:
        # Phase 10: internal progress is captured into memory, never aliased
        # to stderr — the agent-facing stderr channel stays clean.
        sys.stdout = _ProgressCapture()
    try:
        try:
            envelope = _execute(args, state)
        except (Exception, KeyboardInterrupt) as exc:
            envelope, exit_code = _failure_envelope(exc)
    finally:
        try:
            state.warnings.extend(_cleanup_fetcher(state.fetcher))
            if exit_code:
                _gc_orphan_dir(state.fetcher)
        finally:
            sys.stdout = real_stdout
    if state.warnings:
        from scripts.adapters.sanitization import sanitize_display_text
        envelope.setdefault('warnings', []).extend(
            sanitize_display_text(str(w), max_len=500) for w in state.warnings)
    _print_response(envelope, args, real_stdout)
    if exit_code or args.list_sources or args.convert:
        sys.exit(exit_code)


if __name__ == '__main__':
    main()
