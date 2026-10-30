# Security notes

This document describes, honestly, what the pipeline does and does not protect
against. It makes no claims about what any security scanner may or may not
report.

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
appended). Escaping is presentation-layer only — dataset cell values are never
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
CSV blob downloads. It never reads Issues or pull requests. The optional
`gh auth token` is attached only to `api.github.com` API calls — it is
withheld from `raw.githubusercontent.com` and every other host.

## Spreadsheet formula handling

Excel exports bind every string cell as literal text (`data_type='s'`), so
values such as `=SUM(1,1)` or `=CMD|'/C calc'!A0` are stored — and read back —
exactly as written, never as formulas; zero characters of user data are
modified. Plain **CSV** files carry no type metadata: a hostile CSV cell opened
directly in a spreadsheet application may still be interpreted as a formula by
that application. The pipeline does not mutate CSV bytes; treat untrusted CSVs
with your spreadsheet application's import-time protections.

## Verified residual limits

- Bespoke Python extractors still run with the user's OS privileges; nothing
  here contains deliberately hostile same-user code.
- Same-user tampering with local state (config, registry, artifacts) is out of
  scope.
- Preview budgets can make some indivisible exports un-previewable; they fail
  with an explicit `SIZE_LIMIT` rather than transferring silently.
- Quarantined legacy extractors that fail the AST gate require re-review and
  re-registration before they run again.
