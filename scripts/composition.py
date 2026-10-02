"""Only this outer module selects concrete fetch, terminal and storage adapters."""


def interaction_for(state):
    from scripts.adapters.terminal import TerminalInteraction, NonInteractiveInteraction
    return NonInteractiveInteraction() if state.auto_approve else TerminalInteraction()


def fetch_use_case(fetcher):
    from scripts.adapters.provider import FetcherProvider, FetcherArtifacts
    from scripts.application.contracts import FetchPorts
    from scripts.application.fetch import FetchDataset
    return FetchDataset(fetcher, FetchPorts(
        FetcherProvider(fetcher), interaction_for(fetcher), FetcherArtifacts(fetcher)))


def retain_custom_source(fetcher, args):
    from scripts.adapters.registry import LocalSourceRegistry
    from scripts.adapters.terminal import TerminalInteraction
    from scripts.application.retention import RetentionRequest, retain_source
    if args.source.lower() != 'custom':
        return None, None
    name = args.save_source
    if not (args.non_interactive or args.yes or args.json_output):
        name = TerminalInteraction().retention_name(fetcher.resolved_title) or name
    choice = RetentionRequest(name, args.query, fetcher.script_path, args.defer_source_choice)
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
