---
name: data-fetcher-pipeline
description: Automates sourcing, previewing, and converting datasets from pre-configured platforms and custom web sources for data professionals.
---

# Data Fetcher Pipeline

## Required conversation before execution

**Read and follow [the conversation workflow](references/conversation-workflow.md)
before fetching.** On a bare skill invocation, first ask ready/saved sources vs
an external platform. Show a numbered ready-source list or ask the external name/
URL, collect 3-5 short requirements, assess fit/restrictions, show real sample rows
before final approval, then fetch/convert. After a successful new external fetch,
ask whether to save the platform and extractor; no deletes only its pending script.

Use `--preview-only` for agent previews. The `--yes` recipe below is a technical
prompt-free interface, not authorization to skip the conversation or sample review.
`--goal` is context, not an automatic semantic/date/country filter.

## Overview & Core Purpose
Data Fetcher Pipeline helps analysts, engineers, students, and agents find datasets, review samples, and save data with source metadata. It supports built-in platforms and reviewed custom extractors through a shared CLI workflow.

---

## Key Features & Architecture

### 1. Pre-Configured Sources & Setup Workflow
* **Supported Platforms:** 12 CLI keys across 11 data platforms , run `--list-sources` for the authoritative list: `airbnb`, `coingecko`, `datagov`, `eurostat`, `fred`, `github`, `kaggle`, `openml`, `sec`, `worldbank`, `yahoo`/`yfinance` (aliases). Unknown source keys raise a coded error (`UNSUPPORTED_SOURCE`) listing all supported keys.
* **Guided API Setup:**
  * Credentials live in a central global `config.json` (see `config_template.json` for all consumed keys).
  * The CLI reads the local config. Interactive providers can ask for missing credentials with masked input and save them; a bare CLI invocation starts source selection rather than a full credential setup wizard. Corrupt config files are reported loudly (stderr warning) instead of silently behaving as "no keys".

### 2. Smart Data Handling & Format Conversion
* **Format Conversion:** on-the-fly conversion between CSV, Excel, Parquet, JSON, and SQLite. The matrix is asymmetric (e.g. Parquet→JSON is unsupported; a SQLite *target* requires a CSV source) , unsupported pairs raise `UNSUPPORTED_CONVERSION`.
* **Preview and approval:** interactive runs scout, disclose transfer cost, show actual sample rows, then ask final approval. Direct uncompressed CSV/TSV previews read at most a 256 KB prefix; small complete files are cached. Compressed/SDK/custom sources may need full transfer for preview. `--yes --non-interactive` skips prompts; agents must follow the conversation gates above.
* **Row limits:** `--rows N` limits the saved rows. World Bank can request a limited page; direct CSV sources may still transfer the complete file before reading N rows, and SDK sources may download the full dataset.

### 3. Custom Web Source Analysis & Anti-Blocking Protection
* **Scraping Assessment:** analyzes custom URLs for scraping difficulty (EASY, MEDIUM, HARD, BLOCKED) based on robots.txt, Cloudflare, CAPTCHAs, and rate-limiting headers.
* **Safety checks:** a robots.txt denial stops the target fetch after checking robots.txt. The analyzer and generated script reject private IPs and check redirects. These URL checks are best-effort because DNS can change between validation and connection; do not treat generated scripts as a hardened sandbox.

---

## Technical Architecture & Agent Usage

### Recommended Execution Interface
Install the package with `python -m pip install -e <skill-directory>`, then run from any directory:
```bash
data-fetcher --source <source> --query <query> --yes --non-interactive --json-output [flags]
```
`python scripts/cli.py` works when the current directory is the skill root.

**Agent execution recipe:** after the relevant conversation/transfer approval, pass `--yes --non-interactive --json-output` together. Use `--preview-only` for the sample stage and a separate full fetch after final approval. `--non-interactive` disables prompts; **stdout carries only the final JSON envelope** and progress goes to stderr. Bare `--json-output` without `--yes`/`--non-interactive` is rejected for fetches with `INTERACTIVE_REQUIRED`; discovery, conversion and pending-source choices need no download approval.

**Interpreter note:** install and run with the same Python environment. On Windows, `py -3 -m pip install -e <skill-directory>` is an alternative if `py` is configured.

### Success Envelope (fetch)
```json
{
  "status": "success",
  "source": "worldbank", "query": "SP.DYN.CBRT.IN",
  "output_path": "C:\\abs\\path\\sp_dyn_cbrt_in_raw.csv",
  "rows": 10, "columns": 6,
  "fetched_at": "2026-09-06T10:00:00+00:00",
  "provider_requests": 2, "retries": 0, "bytes_transferred": 851040,
  "metrics_scope": "instrumented HTTP client only",
  "source_url": "https://api.worldbank.org/...", "resolved_title": "...",
  "sha256": "<hex of the exact file bytes>", "bytes": 12345,
  "description_path": "C:\\abs\\path\\sp_dyn_cbrt_in_description.md",
  "manifest_path": "C:\\abs\\path\\manifest.jsonl",
  "original_path": null, "original_sha256": "", "original_bytes": 0,
  "complete": true,
  "warnings": [],
  "truncation": null
}
```
* `sha256` always describes exactly the bytes written to `output_path`.
* `complete: false` + non-empty `warnings` = known-partial artifact (e.g. CoinGecko's keyless 365-day cap, a provider returning fewer rows than expected) , read `warnings` for why.
* `warnings` also reports metadata-write or temporary-cleanup failures even when the saved data is complete. `description_path` and `manifest_path` are empty strings if their current writes fail; the saved CSV remains available. Cleanup completes or reports its warning before the final envelope is emitted.
* `truncation` records known row limits. `null` does not prove a source has no other resources or older data.
* Metrics count only calls through the internal HTTP client. Yahoo/yfinance, OpenML, Kaggle and generated custom extractors use separate clients; their three network metrics are `null` and `metrics_scope` explains why.
* `output_path` is the normalized CSV. For direct CSV/TSV/gzip downloads, `original_path` and its hash identify the preserved source bytes. API and SDK sources have no retained original payload.

### Error Contract
Failures return `{"status": "error", "code": "<STABLE_CODE>", "message": "<prose with recovery hints>"}` and exit 1.
An error envelope may also include `warnings` if cleanup failed; the extraction error retains its original code.
Codes: `AUTH_MISSING`, `AUTH_INVALID`, `RATE_LIMITED`, `NETWORK`, `PROVIDER_ERROR`, `PROVIDER_UNAVAILABLE`, `NOT_FOUND`, `UNSUPPORTED_SOURCE`, `UNSUPPORTED_CONVERSION`, `DEPENDENCY_MISSING`, `BLOCKED_URL`, `SIZE_LIMIT`, `OUTPUT_LOCKED`, `ABORTED`, `INTERACTIVE_REQUIRED`, `ARG_MISSING`, `INVALID_OUTPUT`, `REGISTRY_INVALID`, `REGISTRY_BUSY`, `SOURCE_EXISTS`, `INTERNAL`.

**Exit codes:** `0` success · `1` error (envelope on stdout) · `2` argparse usage (shell-level) · `3` engine/provider failed to load (`PROVIDER_UNAVAILABLE` or `STARTUP_FAILURE`) , the JSON envelope is guaranteed even then. The provider registry is lazy: one broken optional dependency affects only its own source.

### What `--query` Means Per Source
| Source | Query format | Example |
| --- | --- | --- |
| `yahoo`/`yfinance` | ticker symbol | `AAPL` |
| `fred` | series ID; public graph CSV without a key, JSON API with `FRED_API_KEY` | `CPIAUCSL`, `GDP` |
| `sec` | ticker symbol (resolved to CIK) | `AAPL` |
| `worldbank` | indicator code | `NY.GDP.MKTP.CD` |
| `eurostat` | dataset code | `nama_10_gdp` |
| `kaggle` | `owner/dataset-slug` | `uciml/iris` |
| `coingecko` | coin id or symbol (auto-resolved); no key needed, free tier caps history at 365 days | `bitcoin`, `sol` |
| `openml` | dataset name fragment (exact match wins, then smallest; ranked candidates shown) or numeric dataset ID | `iris`, `61` |
| `github` | free text: searches repos, then prefers the largest CSV within a 50 MB advisory budget; may select the smallest larger CSV with a warning | `covid` |
| `datagov` | free text (GSA Catalog API v4; key optional, DEMO_KEY fallback) | `climate` |
| `airbnb` | city name on insideairbnb.com | `amsterdam` |
| `custom` | full URL , assess and fetch a supported public file/table | `https://.../data.csv` |
| `custom_<name>` | URL on the saved platform's registered hostname | shown in `--list-sources` |

### Supported Flags
* `--source` / `--query`: target platform and query (semantics above).
* `--outdir DIR`: destination root. **Set it explicitly** , the default is `<current working directory>/data_raw`, which varies by shell.
* `--rows N`: save the first N rows; transfer savings depend on the provider (recorded as `truncation` when known).
* `--preview-only`: `mode: preview`, `sample` (up to five rows/30 displayed columns), full column names, `transfer_scope`, warnings and `output_path: null`; no saved dataset/profile/manifest or source registration.
* `--analyze-only`: custom assessment with `mode: analysis`; no generated script or extraction.
* `--columns a,b`: require and retain exact column names; missing fields fail with `NOT_FOUND`.
* `--table INDEX`: zero-based simple HTML table selection for external sources.
* `--goal TEXT`: request context only, not a content filter.
* `--output-format FORMAT`: convert the fetched CSV; envelope includes `converted_file`. CSV remains available.
* `--save-source NAME`: explicit consent to register a successful full custom fetch, or pair with `--keep-source ID` to resolve a pending choice.
* `--defer-source-choice`: keep a pending custom extractor after full fetch; envelope includes `pending_source_id`. Ask the user, then use `--keep-source ID --save-source NAME` or `--discard-source ID`.
* `--non-interactive`: runs without any input prompts (wizard, confirmations).
* `--yes`: auto-approves pre-flight downloads (full row extraction unless `--rows`).
* `--list-sources`: prints all supported sources and exits (JSON mode returns `{"status":"success","sources":[{"key","platform","query_format","example","auth"} ×12]}`).
* `--json-output`: emits the machine-readable JSON envelope as the only stdout artifact.
* `--convert FORMAT FILE`: converts FILE to FORMAT. Targets: `csv`, `json`, `parquet`, `sqlite`/`db`, `excel`/`xlsx`. Returns `{"status","converted_file","source_file"}`. An existing destination is protected unless `--overwrite` is passed. Excel accepts at most 16,384 columns; SQLite targets accept at most 2,000 columns.

### Output Layout & Provenance
Fetches land at `<outdir>/<source>/<sanitized-query>/`:
* `<name>_raw.csv` , normalized dataset CSV, written with an atomic replacement.
* `<name>_source_<sha256>.<ext>` , original downloaded bytes for direct CSV, TSV, or gzip sources; absent for API/SDK responses.
* `<name>_raw.csv.prev` , the previous normalized CSV version when a prior version exists.
* `manifest.jsonl` , one append-only JSON line per fetch: `{ts, source, query, url, rows, cols, bytes, sha256, complete, warnings, truncation}`. Rewritten atomically; torn legacy lines are repaired automatically. Diff a re-fetch against prior versions via the manifest hashes.
* `<name>_description.md` , auto-generated profile (source URL, generated-at UTC timestamp, row/column counts, null density, first-5 preview, per-column schema; bounded to the first 30 columns for wide frames).

`--convert` writes its artifact next to the source file. Re-running a fetch retains the old file as `.prev` (noted on stderr).

### Performance Guarantees
* Some direct downloads stream to disk with a 500 MB cap. Parsing and SDK sources can still use substantial memory, and not every network path has the same cap.
* Excel export uses openpyxl write-only streaming with a 16,384-column guard.
* `--list-sources` does not import the data stack and creates no directories. `--convert` runs conversions locally and imports the required data libraries.

### Package Layout
```text
scripts/
├── cli.py                  # Entry point: flags, JSON envelopes, guarded main
├── base.py                 # Compatible provider hooks; delegates to composed use cases
├── core/                   # Effect-free request, state, artifact metadata and errors
├── application/            # Fetch lifecycle, frame policy and source retention
├── adapters/               # Terminal, payload, artifact, registry and conversion effects
├── composition.py          # Selects concrete adapters outside application policy
├── factory.py              # Lazy provider registry (per-source isolation)
├── errors.py               # DataFetchError: stable error codes + exit codes
├── http_utils.py           # Session pooling, retries, pacing, cost metering
├── format_alchemy.py       # CSV/Excel/SQLite/JSON/Parquet conversions
├── provision.py            # Directory provisioning
├── config.py               # Credential wizard and lookup
├── web_analyzer.py         # Custom-URL scraping analyzer (SSRF-guarded)
└── fetchers/               # One module per source (12 keys, lazy-imported)
```

---

## Setup Notes
* Keys are needed only for sources that require them. FRED can use public graph CSV without a key; SEC needs a contact identity and Kaggle needs account credentials. CoinGecko, World Bank, Eurostat, Data.gov, OpenML, Airbnb, and Yahoo work without configuration.

---

## Background & References
* [Ralph-style agent loops](https://ghuntley.com/ralph/) and [Effective Harnesses for Long-Running Agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) , the engineering patterns behind the preview/verification design.
* `references/source-constraints.md` , per-provider rate/pacing constraints.

Internal dependency direction and compatibility seams: [fetch architecture](references/architecture.md).
