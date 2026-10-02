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

def _fetch_envelope_meta(fetcher) -> dict:
    """Build envelope metadata from fetcher state, defensively (mock-safe)."""
    def typed(attr, default):
        val = getattr(fetcher, attr, default)
        return val if isinstance(val, type(default)) else default

    warnings_raw = getattr(fetcher, "completeness_warnings", [])
    warnings_list = [str(w) for w in warnings_raw] if isinstance(warnings_raw, list) else []
    artifact_warnings = getattr(fetcher, 'artifact_warnings', [])
    if isinstance(artifact_warnings, list):
        warnings_list.extend(str(w) for w in artifact_warnings)
    complete_raw = getattr(fetcher, "complete", True)
    complete = complete_raw if isinstance(complete_raw, bool) else True

    trunc_raw = getattr(fetcher, "truncation", None)

    return {
        "source_url": typed("dataset_url", ""),
        "resolved_title": typed("resolved_title", "") or None,
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


def run_conversion(target_format: str, filepath: str, overwrite: bool = False) -> str:
    from scripts.composition import convert_file
    return convert_file(target_format, filepath, overwrite)

@dataclass
class _RunState:
    fetcher: object = None
    warnings: list = field(default_factory=list)


def _parse_arguments():
    parser = argparse.ArgumentParser(description="Data Fetcher Background Engine")
    parser.add_argument("--source", required=False, help="Target data platform (e.g., yfinance, fred, airbnb)")
    parser.add_argument("--query", required=False, help="Topic, ticker symbol, or series ID")
    parser.add_argument("--outdir", default=str(Path.cwd() / "data_raw"), help="Destination directory")

    parser.add_argument("--non-interactive", action="store_true", help="Run without user input prompt blocks")
    parser.add_argument("--yes", action="store_true", help="Auto-approve pre-flight data downloads")
    parser.add_argument("--rows", type=int, default=0, help="Save only the first N rows (transfer savings depend on provider)")
    parser.add_argument("--preview-only", action="store_true", help="Return five sample rows; do not save dataset")
    parser.add_argument("--analyze-only", action="store_true", help="Assess custom URL without extraction")
    parser.add_argument("--save-source", metavar="NAME", help="Register a custom source after successful fetch")
    parser.add_argument("--defer-source-choice", action="store_true", help="Keep a pending custom extractor while the agent asks whether to register it")
    parser.add_argument("--keep-source", metavar="ID", help="Register a pending extractor using --save-source NAME; no new download")
    parser.add_argument("--discard-source", metavar="ID", help="Discard a pending extractor; preserve downloaded datasets")
    parser.add_argument("--columns", default="", help="Required column names, comma separated; project exact names")
    parser.add_argument("--table", type=int, help="Zero-based HTML table index for custom extraction")
    parser.add_argument("--output-format", default="csv", choices=['csv', 'json', 'excel', 'xlsx', 'parquet', 'sqlite', 'db'])
    parser.add_argument("--goal", default="", help="Human-readable request; not an automatic content filter")
    parser.add_argument("--list-sources", action="store_true", help="Print supported data sources and exit")
    parser.add_argument("--json-output", action="store_true", help="Provide machine-readable JSON output")
    parser.add_argument("--convert", nargs=2, metavar=("FORMAT", "FILE"), help="Convert FILE to FORMAT")
    parser.add_argument("--overwrite", action="store_true", help="Allow --convert to replace an existing output file")

    return parser.parse_args()


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
         'example': catalog[key].get('example', ''), 'auth': catalog[key].get('auth', 'none')}
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


def _configure_fetcher(fetcher, args):
    fetcher.auto_approve = args.yes or args.non_interactive
    if args.rows > 0:
        fetcher.row_limit = args.rows
    fetcher.preview_only = args.preview_only
    fetcher.required_columns = list(dict.fromkeys(c.strip() for c in args.columns.split(',') if c.strip()))
    fetcher.requested_goal = args.goal
    fetcher.table_index = args.table


def _preview_envelope(fetcher, source):
    frame = fetcher.preview_frame
    return {'status': 'success', 'mode': 'preview', 'source': source,
            'source_url': fetcher.dataset_url,
            'sample': json.loads(frame.iloc[:, :30].to_json(orient='records', date_format='iso')),
            'columns': [str(c) for c in frame.columns], 'rows': len(frame),
            'transfer_scope': fetcher.transfer_scope, 'output_path': None,
            'warnings': _fetch_envelope_meta(fetcher)['warnings']}


def _retain_custom_source(fetcher, args):
    from scripts.composition import retain_custom_source
    return retain_custom_source(fetcher, args)


def _network_envelope(source, metrics):
    custom = source.lower() == 'custom' or source.lower().startswith('custom_')
    sdk = source.lower() in {'yahoo', 'yfinance', 'openml', 'kaggle'}
    scope = ('unavailable: generated extractor uses its own HTTP client' if custom else
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
    from dataclasses import asdict
    report = WebAnalyzer(args.query, str(Path(args.outdir) / 'custom')).analyze()
    return {'status': 'success', 'mode': 'analysis', **asdict(report)}


def _fetch_dataset(args, state):
    from scripts.http_utils import reset_metrics, get_metrics
    config = _prepare_fetch(args)
    if args.analyze_only:
        return _analyze_source(args)
    state.fetcher = get_fetcher(args.source, args.query, args.outdir, config)
    _configure_fetcher(state.fetcher, args)
    reset_metrics()
    csv_path = state.fetcher.run()
    metrics = get_metrics()
    if args.preview_only:
        return _preview_envelope(state.fetcher, args.source)
    from scripts.application.complete import complete_dataset
    converted, registered, pending = complete_dataset(
        csv_path, args.output_format, run_conversion,
        lambda: _retain_custom_source(state.fetcher, args))
    envelope = _saved_envelope(state.fetcher, args, csv_path)
    envelope.update(_network_envelope(args.source, metrics))
    envelope.update(converted_file=converted, registered_source=registered, pending_source_id=pending)
    return envelope


def _execute(args, state):
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
        return {'status': 'error', 'code': exc.code, 'message': str(exc)}, exc.exit_code
    if isinstance(exc, (KeyboardInterrupt, EOFError)):
        code = 'ABORTED' if isinstance(exc, KeyboardInterrupt) else 'INTERACTIVE_REQUIRED'
        return {'status': 'error', 'code': code, 'message':
                'Interactive input required but unavailable. Add --yes flag for non-interactive mode.'}, (
                    130 if isinstance(exc, KeyboardInterrupt) else 1)
    if isinstance(exc, (ValueError, RuntimeError, ImportError, OSError)):
        return {'status': 'error', 'code': _fallback_code(exc), 'message': str(exc)}, 1
    return {'status': 'error', 'code': 'INTERNAL',
            'message': f'{type(exc).__name__}: {exc}'}, 1


def _cleanup_fetcher(fetcher):
    cleanup = getattr(fetcher, 'cleanup', None)
    if not callable(cleanup):
        return []
    try:
        cleanup()
        return []
    except Exception as exc:
        # A provider cleanup hook may raise any library exception. Keep the
        # primary fetch result and disclose the resource failure to the caller.
        return [f'Temporary extractor cleanup failed ({type(exc).__name__}): {exc}']


def _print_sources(sources, stdout):
    print('\nSupported Data Sources:\n' + '-' * 55, file=stdout)
    print(f"{'CLI Key':<15} | {'Platform / Title':<35}\n" + '-' * 55, file=stdout)
    for source in sources:
        print(f"{source['key']:<15} | {source['platform']:<35}", file=stdout)
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


def main() -> None:
    if _startup_error is not None:
        print(json.dumps({'status': 'error', 'code': 'STARTUP_FAILURE',
                          'message': f'Engine failed to initialize: {_startup_error}. '
                                     'Reinstall dependencies from pyproject.toml.'}))
        sys.exit(3)
    args = _parse_arguments()
    state = _RunState()
    real_stdout, exit_code = sys.stdout, 0
    if args.json_output:
        sys.stdout = sys.stderr
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
        envelope.setdefault('warnings', []).extend(state.warnings)
    _print_response(envelope, args, real_stdout)
    if exit_code or args.list_sources or args.convert:
        sys.exit(exit_code)


if __name__ == '__main__':
    main()
