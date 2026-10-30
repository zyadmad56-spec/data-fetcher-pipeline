"""Only this outer module selects concrete fetch, terminal and storage adapters."""
from pathlib import Path


def interaction_for(state):
    from scripts.adapters.terminal import TerminalInteraction, NonInteractiveInteraction
    return NonInteractiveInteraction() if state.auto_approve else TerminalInteraction()


def fetch_use_case(fetcher):
    from scripts.adapters.provider import FetcherProvider, FetcherArtifacts
    from scripts.application.contracts import FetchPorts
    from scripts.application.fetch import FetchDataset
    return FetchDataset(fetcher, FetchPorts(
        FetcherProvider(fetcher), interaction_for(fetcher), FetcherArtifacts(fetcher)))


def resolve_custom_inputs(args):
    """Resolve ``--extractor-script`` / ``--approve-preview`` into in-memory inputs.

    Returns ``(extractor_bytes, approved_receipt)``. Extractor bytes are read
    from disk exactly once and AST-reviewed here. An approved preview loads
    its receipt TOGETHER WITH the staged bytes stored beside it in the
    receipt store, so an approved fetch never re-reads the original extractor
    file from disk.
    """
    from scripts.errors import DataFetchError
    extractor_bytes = None
    approved = None
    script_flag = getattr(args, 'extractor_script', None)
    approve_flag = getattr(args, 'approve_preview', None)
    if script_flag:
        from scripts.adapters.script_runner import ExtractorRejectedError, inspect_extractor_ast
        try:
            extractor_bytes = Path(script_flag).read_bytes()
        except OSError as exc:
            raise DataFetchError(f'Extractor script could not be read: {exc}',
                                 code='ARG_MISSING') from exc
        try:
            inspect_extractor_ast(extractor_bytes)
        except ExtractorRejectedError as exc:
            raise DataFetchError(f'Extractor bytes do not pass the safety review: {exc}',
                                 code='EXTRACTOR_REJECTED') from exc
    if approve_flag:
        from scripts.source_registry import load_preview_receipt
        approved, staged = load_preview_receipt(approve_flag)
        if extractor_bytes is None:
            extractor_bytes = staged
    return extractor_bytes, approved


def retain_custom_source(fetcher, args):
    from scripts.adapters.registry import LocalSourceRegistry
    from scripts.adapters.terminal import TerminalInteraction
    from scripts.application.retention import RetentionRequest, retain_source
    if args.source.lower() != 'custom':
        return None, None
    name = args.save_source
    if not (args.non_interactive or args.yes or args.json_output):
        name = TerminalInteraction().retention_name(fetcher.resolved_title) or name
    # Register the STAGED bytes the fetcher locked in memory (single-read
    # contract: extractor_bytes, generated-template and registered-script
    # modes all land here); fall back to the locked path if bytes are absent.
    script = getattr(fetcher, 'script_bytes', None)
    if script is None:
        script = getattr(fetcher, 'script_path', None)
    choice = RetentionRequest(name, args.query, script, args.defer_source_choice)
    registered, pending = retain_source(choice, LocalSourceRegistry())
    if registered:
        print(f'[Saved source] {registered} is now available in --list-sources.')
    if pending:
        print(f'[Source choice pending] {pending}; keep or discard after asking the user.')
    return registered, pending


def finish_pending_source(token, name):
    from scripts.adapters.registry import LocalSourceRegistry
    from scripts.application.retention import finish_source_choice
    return finish_source_choice(token, name, LocalSourceRegistry())


def artifact_store_for(state):
    from scripts.adapters.artifacts import FileArtifactStore
    state.payload_sep = state._payload_sep()
    state.payload_compression = state._payload_compression()
    return FileArtifactStore(state)


def payload_reader_for(fetcher, request):
    from scripts.adapters.payload import PayloadReader
    from scripts.core.models import PayloadSpec
    spec = PayloadSpec(fetcher._payload_url(), fetcher._payload_headers(),
                       fetcher._payload_sep(), fetcher._payload_compression(),
                       fetcher.PREVIEW_BYTES, fetcher.MAX_DOWNLOAD_BYTES)
    return PayloadReader(fetcher, spec, request)


def file_sha256(path):
    from scripts.adapters.artifacts import FileArtifactStore
    return FileArtifactStore._sha256_file(path)


def convert_file(target_format, filepath, overwrite):
    from scripts.adapters.conversion import convert_file as convert
    return convert(target_format, filepath, overwrite)
