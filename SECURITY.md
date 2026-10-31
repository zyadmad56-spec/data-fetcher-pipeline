# Security notes

This document describes, honestly, what the pipeline does and does not protect
against. It makes no claims about what any security scanner may or may not
report.

## What this tool fetches, and where the content goes

The pipeline fetches **data only**: dataset files (CSV/TSV/XLSX/Parquet/JSON),
HTML tables, and documented provider API responses. Fetched content is parsed,
hashed, and saved as data. It is never executed, never scanned for or treated as
instructions, and never forwarded to any model or third party — the CLI has no
model integration, no telemetry, and no outbound call other than the fetches
described under *Network egress* below.

**Third-party content reaches the human only.** Every fetched payload path ends
in on-disk artifacts the human opens themselves: `_human_preview.md` (the
bounded preview), `*_raw.csv` (the normalized dataset), `*_source_*` (retained
original bytes), `*_description.md` (the profile), and `manifest.jsonl`.
Agent-facing channels carry metadata only: row/column counts, dtypes, file
paths, SHA-256 hashes, transfer scope, HTTP status codes, and sanitized,
length-capped warnings. Progress prints from providers are captured in memory
in `--json-output` mode (16 KiB cap) and never reach stdout or stderr.

**Code execution is receipt-gated and separate from fetching.** The only code
that runs beyond this repository's own modules is a custom extractor written
and reviewed locally by the agent/human — never downloaded from a target site.
It executes out-of-process only after the human approved a bounded preview:
non-interactive full fetches must present `--approve-preview PREVIEW_ID` (with
`--yes`), and the approval runs the exact bytes staged with that preview, never
the original file. There is no remote configuration, no remote instruction
loading, and no runtime code download anywhere in the pipeline.

## Endpoints and allowed sources

Built-in fetches target only the documented vendor endpoints of the supported
platforms:

- `api.github.com` (repository search + git trees + CSV blobs) and
  `raw.githubusercontent.com` (CSV blob download, token withheld — see
  *GitHub source scope*)
- `insideairbnb.com` and its subdomains (index page + `.csv.gz` downloads)
- `api.gsa.gov/technology/datagov/v4` (Data.gov GSA Catalog API v4; the
  `api.data.gov` host appears only as the key-portal reference) — the CSV
  distribution URL selected from catalog results is user-chosen content and is
  validated by the public-URL guard before any request
- `api.worldbank.org`, `ec.europa.eu` (Eurostat), `api.coingecko.com`
- `api.stlouisfed.org` and `fred.stlouisfed.org` (FRED)
- `www.sec.gov` and `data.sec.gov` (SEC EDGAR)
- `www.openml.org`, `www.kaggle.com`, `finance.yahoo.com` (metadata page URL;
  the yfinance SDK uses its own Yahoo endpoints)
- `vincentarelbundock.github.io` (Rdatasets)

Everything else is a **user-chosen** custom URL. Custom targets and
Data.gov-distribution URLs run on the dedicated public-only transport described
below (scheme, embedded-credential, control-character, and public-IP checks on
every hop and DNS answer). There are no undocumented fetch endpoints: a custom
fetch downloads only from the single URL the caller passed, built-in fetchers
talk only to the hosts listed above, and there is no remote configuration or
instruction loading anywhere in the pipeline.

## Custom extractor execution

Custom extraction (the `custom` source, saved `custom_*` sources, and
`--extractor-script`) never runs extractor code inside the CLI process. The
trust chain is:

1. **Generator provenance.** Generated extractors come from one fixed template
   in `scripts/custom_template.py`. Site-specific values (URL, difficulty,
   table hints) are interpolated only through `json.dumps(...)` string
   literals, so fetched content cannot escape the literals into code.
2. **Byte locking.** The extractor's bytes are read exactly once, bound to a
   SHA-256 hash, and every later execution uses those exact in-memory bytes.
   Disk edits after scouting have no effect. An approved fetch (`--approve-preview
   ID`) runs the bytes staged with that preview, never the original file.
3. **Static AST gate.** A fail-fast review aid rejects extractors referencing
   `subprocess`, `ctypes`, `multiprocessing`, `eval`/`exec`/`compile`,
   `os.system`/`os.popen`/`os.exec*`/`os.spawn*`, and similar machinery, and
   requires a top-level `extract_frame` entry point. Aliases, dynamic
   construction and importlib indirection are not visible to static analysis.
4. **Out-of-process worker.** Each execution runs
   `[sys.executable, -I, isolated_worker.py, job.json]` in a fresh child with a
   scrubbed environment (credential-looking variables stripped, home/tmp
   redirected into a disposable workspace), `stdin` closed, 64 KiB-capped
   stdout/stderr capture, and a wall-clock deadline that kills the child.
   Results return as CSV + validated JSON, never a pickle.
5. **PEP 578 audit hooks (runtime defense-in-depth).** Inside the worker, an
   audit hook blocks subprocess/process spawning, `ctypes` usage, banned
   imports, non-public `socket.connect` targets, and filesystem writes (and
   most reads) outside the disposable workspace.

**Honest scope:** layers 3-5 are deterministic fail-fast checks and runtime
defense-in-depth. They are **NOT a sandbox** and are **not** proof against
deliberately hostile same-user code, interpreter-internal bypasses, or
C-extension exploits; descendants and memory are not bounded portably. They
block naive and hallucinated dangerous behavior cheaply, and make no stronger
claim.

## Approval chain for custom fetches

Every non-interactive custom full fetch requires `--approve-preview ID` bound
to a bounded preview receipt (script hash, canonical URL, columns, table
index, 24-hour TTL). Any drift fails with `APPROVAL_MISMATCH`; expired or
tampered receipts fail with `PREVIEW_EXPIRED` / `PREVIEW_TAMPERED`. Interactive
runs approve at the sample prompt and create the receipt internally.

## Human-only preview

`--preview-only` writes the escaped sample (5 rows, all column names, source
URL, transfer notes) to a private `_human_preview.md` artifact and returns a
JSON envelope with **zero sample cell values and zero column names**. Error
messages reaching JSON envelopes are scrubbed (URL credentials/query strings,
response bodies, offending-CSV-row text removed; length-capped; recovery hint
appended). Two sanitized plain-text residuals remain by design and are stated
here rather than hidden: a failure may quote a capped excerpt of the isolated
worker's stderr (an exception summary, escape-neutralized, single line, hard
length cap), and metadata fields such as `resolved_title` or `source_url` can
carry a provider-supplied plain-text title/URL (escape-neutralized, never
markup-active). Dataset cell values never reach those channels. Escaping is
presentation-layer only — dataset cell values are never
modified. Note honestly: a file on disk cannot stop a host agent with explicit
file-read tools from opening `_human_preview.md`; the conversation workflow
forbids it, and enforcement of "human alone" for automatic channels
(stdout/stderr/JSON) is technical, while file access is cooperative.

## SHA-256 limits

Saved extractor checksums detect *change*, not *malice*: a byte-identical
script passes verification regardless of what it contains. Registration
additionally runs the AST gate; neither check proves an extractor is safe.
Receipt and registry stores verify their own files against recorded hashes and
fail closed on mismatch (`PREVIEW_TAMPERED`, `REGISTRY_INVALID`).

## Network egress

Untrusted/public fetches — custom sources and external payload downloads —
run on a dedicated HTTP transport that validates that every direct target
(and redirect hop) resolves only to globally routable public IP addresses,
unwrapping IPv4-mapped/6to4/Teredo forms, and ignores ambient proxy and
credential configuration (`trust_env=False`, explicit empty proxies covering
`ALL_PROXY`). First-party provider API calls to fixed vendor endpoints use
the shared default client. `robots.txt` is honored with a bounded
512 KiB read; whole jobs run under a monotonic deadline. DNS validation and
connection remain separate steps, so a rebinding race is narrowed, not
eliminated. Vendor SDK transports (yfinance, Kaggle, OpenML) use their own
clients and are outside this instrumentation (`metrics_scope` discloses this).

## GitHub source scope

GitHub discovery performs repository search, a recursive git-tree walk, and
CSV blob downloads. It never reads Issues or pull requests. The token is
resolved from `GITHUB_TOKEN` (environment or config) first and falls back to
`gh auth token` via the local `gh` CLI; the `gh` output is captured in memory
and never echoed. The resolved token exists only in memory and in the
`Authorization` header: it is attached exclusively to `api.github.com` API
calls, withheld from `raw.githubusercontent.com` and every other host, never
printed, logged, or persisted, and those API calls are made with redirects
disabled so a redirect can never carry the header to another host. Error paths
report HTTP status codes only — provider bodies and headers are not echoed.

## Spreadsheet formula handling

Excel exports bind every string cell as literal text (`data_type='s'`), so
values such as `=SUM(1,1)` or `=CMD|'/C calc'!A0` are stored — and read back —
exactly as written, never as formulas; zero characters of user data are
modified. Plain **CSV** files carry no type metadata: a hostile CSV cell opened
directly in a spreadsheet application may still be interpreted as a formula by
that application. The pipeline does not mutate CSV bytes; treat untrusted CSVs
with your spreadsheet application's import-time protections.

## Kaggle archive hardening

Kaggle datasets arrive as ZIP archives and are treated as untrusted. Before a
single member is written out, the archive is validated fail-fast: member count
and total uncompressed size are capped, and `..`-traversal, absolute paths,
drive-letter components, and symlink entries are rejected. Violations raise
coded errors with stable message prefixes (`SIZE_LIMIT: Kaggle …`,
`BLOCKED: Kaggle …`) so agents and humans can branch on them. Downloads run in
a temporary directory and the ambient `KAGGLE_USERNAME`/`KAGGLE_KEY`
environment values are restored exactly after every attempt, so credentials
never persist into the host process.

## Verified residual limits

- Bespoke Python extractors still run with the user's OS privileges; nothing
  here contains deliberately hostile same-user code.
- Same-user tampering with local state (config, registry, artifacts) is out of
  scope.
- Preview budgets can make some indivisible exports un-previewable; they fail
  with an explicit `SIZE_LIMIT` rather than transferring silently.
- Quarantined legacy extractors that fail the AST gate require re-review and
  re-registration before they run again.
