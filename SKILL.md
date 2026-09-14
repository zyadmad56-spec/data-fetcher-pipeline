---
name: data-fetcher-pipeline
description: Automates sourcing, previewing, and converting datasets from pre-configured platforms and custom web sources for data professionals.
---

# Data Fetcher Pipeline

## Overview & Core Purpose
**Data Fetcher Pipeline** is designed specifically for Data Analysts, Data Engineers, Students, and Data Science professionals. Finding reliable datasets, Excel sheets, and clean database samples traditionally takes hours or days. This skill automates and drastically accelerates the data-sourcing process into a streamlined CLI workflow whose primary consumers are autonomous AI agents — every contract below is machine-readable and verified against the implementation.

---

## Key Features & Architecture

### 1. Pre-Configured Sources & Setup Workflow
* **Supported Platforms:** 12 CLI keys across 11 data platforms — run `--list-sources` for the authoritative list: `airbnb`, `coingecko`, `datagov`, `eurostat`, `fred`, `github`, `kaggle`, `openml`, `sec`, `worldbank`, `yahoo`/`yfinance` (aliases). Unknown source keys raise a coded error (`UNSUPPORTED_SOURCE`) listing all supported keys.
* **Guided API Setup:**
  * Credentials live in a central global `config.json` (see `config_template.json` for all consumed keys).
  * On first interactive setup the CLI collects keys with masked input for secrets and saves them automatically. Corrupt config files are reported loudly (stderr warning) instead of silently behaving as "no keys".

### 2. Smart Data Handling & Format Conversion
* **Format Conversion:** on-the-fly conversion between CSV, Excel, Parquet, JSON, and SQLite. The matrix is asymmetric (e.g. Parquet→JSON is unsupported; a SQLite *target* requires a CSV source) — unsupported pairs raise `UNSUPPORTED_CONVERSION`.
* **Cheap previews:** for direct-payload sources the preview costs at most ONE small request. Payload sources are streamed to a temp file and parsed with `nrows=5`; `extract()` reuses the same download (one body download per run, guaranteed). Sources without a direct payload (Kaggle, OpenML, SEC, FRED) preview from the full extract, which is cached for reuse.
* **Row pushdown:** `--rows N` limits fetching at the PROVIDER level where supported (WorldBank fetches one page of N; CSV sources stop parsing at N) instead of downloading everything and slicing.

### 3. Custom Web Source Analysis & Anti-Blocking Protection
* **Scraping Assessment:** analyzes custom URLs for scraping difficulty (EASY, MEDIUM, HARD, BLOCKED) based on robots.txt, Cloudflare, CAPTCHAs, and rate-limiting headers.
* **Safety hardening:** robots.txt denial is a HARD STOP (zero requests are made). SSRF guard blocks non-http(s) schemes and loopback/link-local/private/resolved-private targets; every redirect hop is re-validated. Generated scrapers embed the URL as serialized data (never raw interpolation) and include retry/backoff.

---

## Technical Architecture & Agent Usage

### Recommended Execution Interface
```bash
python scripts/cli.py --source <source> --query <query> --yes --non-interactive --json-output [flags]
```

**Agent recipe:** always pass `--yes --non-interactive --json-output` together. `--non-interactive` disables every prompt, `--yes` auto-approves downloads, and with `--json-output` **stdout carries only the final JSON envelope** — all progress and diagnostic logs go to stderr, so read stdout as a single JSON document. Bare `--json-output` without `--yes`/`--non-interactive` is rejected up-front with `INTERACTIVE_REQUIRED` (JSON mode never prompts).

**Interpreter note:** use `python3` (Linux/macOS) or `py -3` (Windows) — plain `python` may point at an environment without pandas/pyarrow.

### Success Envelope (fetch)
```json
{
  "status": "success",
  "source": "worldbank", "query": "SP.DYN.CBRT.IN",
  "output_path": "C:\\abs\\path\\sp_dyn_cbrt_in_raw.csv",
  "rows": 10, "columns": 6,
  "fetched_at": "2026-09-06T10:00:00+00:00",
  "provider_requests": 2, "retries": 0, "bytes_transferred": 851040,
  "source_url": "https://api.worldbank.org/...", "resolved_title": "...",
  "sha256": "<hex of the exact file bytes>", "bytes": 12345,
  "description_path": "C:\\abs\\path\\sp_dyn_cbrt_in_description.md",
  "complete": true,
  "warnings": [],
  "truncation": null
}
```
* `sha256` always describes exactly the bytes written to `output_path`.
* `complete: false` + non-empty `warnings` = known-partial artifact (e.g. CoinGecko's keyless 365-day cap, a provider returning fewer rows than expected) — read `warnings` for why.
* `truncation` is `null` for full-population fetches; `{"rows": N, "population": M, "by": "user"}` when `--rows` sliced the result. **Absence of truncation IS the contract** for "this is everything the source serves".
* `provider_requests`/`retries`/`bytes_transferred` disclose the exact provider spend of the run.

### Error Contract
Failures return `{"status": "error", "code": "<STABLE_CODE>", "message": "<prose with recovery hints>"}` and exit 1.
Codes: `AUTH_MISSING`, `AUTH_INVALID`, `RATE_LIMITED`, `NETWORK`, `PROVIDER_ERROR`, `PROVIDER_UNAVAILABLE`, `NOT_FOUND`, `UNSUPPORTED_SOURCE`, `UNSUPPORTED_CONVERSION`, `DEPENDENCY_MISSING`, `BLOCKED_URL`, `SIZE_LIMIT`, `OUTPUT_LOCKED`, `ABORTED`, `INTERACTIVE_REQUIRED`, `ARG_MISSING`, `INVALID_OUTPUT`, `INTERNAL`.

**Exit codes:** `0` success · `1` error (envelope on stdout) · `2` argparse usage (shell-level) · `3` engine/provider failed to load (`PROVIDER_UNAVAILABLE` or `STARTUP_FAILURE`) — the JSON envelope is guaranteed even then. The provider registry is lazy: one broken optional dependency affects only its own source.

### What `--query` Means Per Source
| Source | Query format | Example |
| --- | --- | --- |
| `yahoo`/`yfinance` | ticker symbol | `AAPL` |
| `fred` | series ID | `CPIAUCSL`, `GDP` |
| `sec` | ticker symbol (resolved to CIK) | `AAPL` |
| `worldbank` | indicator code | `NY.GDP.MKTP.CD` |
| `eurostat` | dataset code | `nama_10_gdp` |
| `kaggle` | `owner/dataset-slug` | `uciml/iris` |
| `coingecko` | coin id or symbol (auto-resolved); no key needed, free tier caps history at 365 days | `bitcoin`, `sol` |
| `openml` | dataset name fragment (exact match wins, then smallest; ranked candidates shown) or numeric dataset ID | `iris`, `61` |
| `github` | free text — searches repos, then walks trees for the largest CSV ≤ 50 MB | `covid` |
| `datagov` | free text (GSA Catalog API v4; key optional, DEMO_KEY fallback) | `climate` |
| `airbnb` | city name on insideairbnb.com | `amsterdam` |
| `custom` | full URL — runs the scraping analyzer below | `https://.../data.csv` |

### Supported Flags
* `--source` / `--query`: target platform and query (semantics above).
* `--outdir DIR`: destination root. **Set it explicitly** — the default is `<current working directory>/data_raw`, which varies by shell.
* `--rows N`: fetch only the first N rows (provider-pushed where supported; recorded in the envelope/manifest as `truncation`).
* `--non-interactive`: runs without any input prompts (wizard, confirmations).
* `--yes`: auto-approves pre-flight downloads (full row extraction unless `--rows`).
* `--list-sources`: prints all supported sources and exits (JSON mode returns `{"status":"success","sources":[{"key","platform","query_format","example","auth"} ×12]}`).
* `--json-output`: emits the machine-readable JSON envelope as the only stdout artifact.
* `--convert FORMAT FILE`: converts FILE to FORMAT. Targets: `csv`, `json`, `parquet`, `sqlite`/`db`, `excel`/`xlsx`. Returns `{"status","converted_file","source_file"}` where `converted_file` is the created artifact. Column ceilings are enforced with coded `SIZE_LIMIT` errors: Excel accepts at most 16,384 columns (CSV→Excel streams directly, no SQLite hop), SQLite targets accept at most 2,000 columns (SQLite hard limit).

### Output Layout & Provenance
Fetches land at `<outdir>/<source>/<sanitized-query>/`:
* `<name>_raw.csv` — the dataset (written atomically: tmp → fsync → single `os.replace`; a crash can never leave a torn or missing canonical file; interrupted commits self-heal on the next run).
* `<name>.prev` — the previous version, retained on every re-fetch (upstream restatements never destroy history).
* `manifest.jsonl` — one append-only JSON line per fetch: `{ts, source, query, url, rows, cols, bytes, sha256, complete, warnings, truncation}`. Rewritten atomically; torn legacy lines are repaired automatically. Diff a re-fetch against prior versions via the manifest hashes.
* `<name>_description.md` — auto-generated profile (source URL, generated-at UTC timestamp, row/column counts, null density, first-5 preview, per-column schema; bounded to the first 30 columns for wide frames).

`--convert` writes its artifact next to the source file. Re-running a fetch retains the old file as `.prev` (noted on stderr).

### Performance Guarantees
* Fetches stream to disk (memory-flat regardless of payload size; hard 500 MB cap with `SIZE_LIMIT`).
* Excel export uses openpyxl write-only streaming with a 16,384-column guard.
* `--list-sources` does not import the data stack (sub-second startup) and creates no directories. `--convert` runs conversions locally and therefore pays a one-time data-stack import (~1-2s).

### Package Layout
```text
scripts/
├── cli.py                  # Entry point: flags, JSON envelopes, guarded main
├── base.py                 # BaseFetcher: atomic saves, preview contract, profiler
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
* Keys are optional per source — only the sources you use need credentials. CoinGecko, World Bank, Eurostat, Data.gov, OpenML, and Airbnb work with no configuration.

---

## Background & References
* [Ralph-style agent loops](https://ghuntley.com/ralph/) and [Effective Harnesses for Long-Running Agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) — the engineering patterns behind the preview/verification design.
* `references/source-constraints.md` — per-provider rate/pacing constraints.
