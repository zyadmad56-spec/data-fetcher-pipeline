# Fetch architecture

The conversation, questions, preview approvals, flags and provider selectors
retain their existing contract in `conversation-workflow.md`.

## Dependency direction

```text
cli / BaseFetcher compatibility entry points
               |
         composition.py
               |
      application use cases <---- concrete adapters
               |                       |
               +------> core <----------+
```

`core` contains value models, naming policy and the stable domain error. It has
no pandas, terminal, HTTP, SDK or filesystem dependencies. `FetchState` holds
opaque frames and payload handles without interpreting their storage.

`application/fetch.py` owns scout -> transfer consent -> actual sample -> final
approval -> extraction -> selection/validation -> save. It receives provider,
interaction and artifact contracts defined in `application/contracts.py`.
`frame_policy.py` uses pandas for actual frame rules without network or disk
effects. Preview-only returns no saved artifacts. Provider resource release runs
in a finally block. Population estimates are supplied by the provider adapter;
application rules never open payload files to count records.

`application/complete.py` orders conversion before retention.
`application/retention.py` chooses explicit keep, defer or discard behavior using
an injected registry. The terminal retention question remains after successful
fetch and conversion. CLI owns the final response and temporary extractor cleanup.

## Effects and compatibility

- `adapters/artifacts.py`: atomic CSV commits, `.prev`, crash recovery, locked
  output retries, original payload hashes, profiles and provenance manifests.
  It accepts a value state directly and needs no fetcher/provider. Artifact
  metadata is exposed as `SavedArtifacts`; metadata warnings do not change
  dataset completeness.
- `adapters/payload.py`: bounded preview reads, streamed download caps and cache
  parsing. The HTTP callable is injected to preserve the public HTTP test seam.
- `adapters/provider.py`: existing provider hooks, full-preview cache, population
  observation and streamed-cache release. It preserves provider preview overrides
  and public `save_csv` overrides.
- `adapters/terminal.py`: existing prompts, progress and sample rendering; explicit
  noninteractive interaction never asks for input. JSON alone grants no approval.
- `adapters/registry.py` and `adapters/conversion.py`: adapt the existing local
  registry and conversion implementations, preserving their public import paths,
  locks, checksums, path checks and supported conversion matrix.
- `composition.py`: selects concrete adapters. The existing lazy `factory.py`
  selects provider classes without loading unrelated optional SDKs.

`BaseFetcher` retains its constructor, hooks and public methods as compatibility
delegates. It has no second pipeline, file commit, HTTP streaming implementation
or prompt loop. Providers remain in their current import paths. `errors.py`
re-exports the core error; `workflow.FetchRequest` re-exports the core request.
Generated extractor bytes remain standalone and do not import the application.

## Verification

`tests/test_architecture.py` runs the application with memory adapters while
rejecting terminal input and filesystem opening, checks approval/cache/count
ordering and failure behavior, verifies artifact storage directly, and checks
core/application import direction. Existing behavioral and provider tests remain
unchanged. Live HTTP and stdin transcripts are recorded separately from tests.
