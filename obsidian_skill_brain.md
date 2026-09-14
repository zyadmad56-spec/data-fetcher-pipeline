---
type: skill-brain
date: 2026-08-11
tags:
  - data-engineering
  - pipeline
  - skill-memory
  - agent-tool
  - python
  - ai-first
ai-first: true
status: audited
confidence: high
source: Claude Opus 4.6 full codebase audit
---

# Data Fetcher Pipeline — Skill Brain

## For future Claude

This note is the **permanent knowledge base** for the `data-fetcher-pipeline` agent skill. Read this before touching ANY file in the skill. It contains the full architectural map, every decision made, every bug found, and every gotcha that will bite you if you ignore it.

---

## What This Skill Does

The [[data-fetcher-pipeline]] is a CLI + agent-callable tool that:

1. **Fetches datasets** from 10 pre-configured platforms ([[Kaggle]], [[WorldBank]], [[SEC]], [[FRED]], [[Eurostat]], [[DataGov]], [[OpenML]], [[Airbnb]], [[GitHub]], [[Yahoo Finance]]) plus custom URLs.
2. **Saves them** in an isolated directory hierarchy: `data_raw/<source>/<dataset_name>/`.
3. **Auto-generates** a rich Markdown profile (`<dataset>_description.md`) with schema, statistics, and preview.
4. **Converts** between formats (CSV ↔ SQLite ↔ Excel ↔ Parquet ↔ JSON) via [[FormatAlchemy]].
5. **Analyzes** arbitrary URLs for scrapability via [[WebAnalyzer]].

---

## Architecture Map

```
data-fetcher-pipeline/
├── scripts/
│   ├── base.py           # BaseFetcher ABC + sanitize_name + provision_data_directory + MD profiler
│   ├── cli.py            # argparse entry point + interactive wizard
│   ├── config.py         # API key setup wizard + JSON config persistence
│   ├── factory.py        # Strategy router: source string → fetcher class
│   ├── format_alchemy.py # Format conversion engine (CSV↔SQL↔Excel↔Parquet↔JSON)
│   ├── http_utils.py     # request_with_retry (backoff, 429/5xx handling)
│   ├── web_analyzer.py   # URL scrapability assessment + script generator
│   └── fetchers/         # 10 concrete BaseFetcher implementations
│       ├── yahoo.py, fred.py, airbnb.py, openml_fetcher.py
│       ├── kaggle_fetcher.py, sec.py, generic.py
│       ├── worldbank.py, github_data.py, datagov.py, eurostat.py
├── tests/
│   └── test_fetcher_engine.py  # 7 tests (config, routing, alchemy, web, dirs, MD)
├── pyproject.toml
├── SKILL.md
└── AUDIT_AND_FIX_PLAN.md
```

---

## Execution Pipeline (Template Method)

The `BaseFetcher.run()` method orchestrates **six phases** in strict order:

```
scout() → preview() → pre_flight_authorization() → extract() → validate_payload() → save_csv()
                                                                                       ↓
                                                                          generate_markdown_profile()
                                                                          + legacy .txt description
```

**Key behavioral contract:**
- `scout()` returns `{'url': str, 'size_info': str}` — sets `self.dataset_url`.
- `preview()` calls `extract()` internally and caches the result in `self._cached_df` to avoid double-fetching.
- `pre_flight_authorization()` blocks on `input()` unless `auto_approve=True`.
- `validate_payload()` rejects empty DataFrames and >80% null density.
- `save_csv()` synchronously triggers `generate_markdown_profile()` as a post-hook.

---

## Directory Hierarchy Design

```
data_raw/
├── kaggle/
│   └── titanic/
│       ├── titanic_raw.csv
│       └── titanic_description.md
├── yahoo/
│   └── msft/
│       ├── msft_raw.csv
│       └── msft_description.md
├── custom/
│   └── <sanitized_url>/
└── ...10 more source folders...
```

**How source names are resolved (in `BaseFetcher.__init__`):**
```python
source_name = self.__class__.__name__.replace("Fetcher", "")
clean_source = sanitize_name(source_name)
# Special cases:
#   "GithubData" → "githubdata" → remapped to "github"
#   "YahooFinance" → "yahoofinance" → remapped to "yahoo"
```

**`sanitize_name()`** strips non-alphanumeric characters, replaces with `_`, lowercases. This means:
- Query `"S&P 500 Index"` → folder `s_p_500_index`
- Query `"Microsoft (MSFT)"` → folder `microsoft__msft_`

---

## Markdown Profiler Output Structure

The generated `<dataset>_description.md` contains exactly 5 sections:

| Section | Content |
|---|---|
| `## Overview` | Template text with query and source name |
| `## Metadata Summary` | Source URL, filename, row/col counts, null density |
| `## Data Preview (First 5 Rows)` | Markdown table of `.head(5)` |
| `## Column Schema & Health` | Per-column dtype, unique count, null count + percentage |
| `## Summary Statistics` | `df.describe(include='all')` as markdown table |

---

## Known Bugs & Gotchas (as of 2026-08-11 audit)

> [!CAUTION]
> These are real bugs found during the audit. Fix them before extending the skill.

### 🔴 Critical

1. **Pipe in column headers breaks markdown.** `_df_to_markdown_table` escapes `|` in cell values (line 111) but **not in the header row** (line 106). Columns like `Open|High` corrupt the entire table.

2. **`describe(include='all')` on huge DataFrames causes OOM.** No sampling guard. A 10M-row × 50-column DataFrame will spike memory during profiling.

3. **Dead code in `format_alchemy.py` line 192-200.** The `else` branch after `if source_ext == "csv"` in the Excel conversion path re-checks `source_ext == "csv"` inside a ternary — logically unreachable.

### 🟠 Edge Cases

4. **Redundant `provision_data_directory` calls.** Called in both `cli.py:main()` and `config.py:setup_wizard()`. Idempotent but wasteful.

5. **`os.path` still used alongside `pathlib.Path`.** 47 instances of `os.path.*` remain. The codebase is not yet fully migrated.

6. **Generated scraper scripts contain `except Exception as e:`.** Violates the workspace's Clean Code Guard rule.

7. **Humanized delay runs even with `--yes` flag.** Agents auto-approving downloads still wait 2-5 seconds.

8. **CSV is re-read after `run()` in `cli.py` just to get row/col counts.** The DataFrame was already in memory during `save_csv()`.

---

## Agent-Callable Interface

### CLI Flags for Agent Usage

```bash
python -m scripts.cli \
  --source kaggle \
  --query "titanic" \
  --outdir ./data_raw \
  --non-interactive \
  --yes \
  --json-output
```

| Flag | Purpose |
|---|---|
| `--non-interactive` | Skips all `input()` prompts |
| `--yes` | Auto-approves pre-flight authorization |
| `--json-output` | Machine-readable JSON stdout |
| `--list-sources` | Print supported sources and exit |
| `--convert FORMAT FILE` | Standalone format conversion |

### Programmatic Usage (from another agent/skill)

```python
from scripts.factory import get_fetcher
from scripts.base import provision_data_directory

provision_data_directory("./data_raw")
fetcher = get_fetcher("kaggle", "titanic", "./data_raw", config={"kaggle_key": "..."})
fetcher.auto_approve = True
csv_path = fetcher.run()
```

---

## Dependency Map

| Package | Required By | Optional? |
|---|---|---|
| `pandas` | Everything | No |
| `requests` | `http_utils.py`, most fetchers | No |
| `beautifulsoup4` | `web_analyzer.py` | No |
| `openpyxl` | `format_alchemy.py` | No |
| `pyarrow` | Parquet conversion | Yes (fallback: fastparquet) |
| `yfinance` | `yahoo.py` | Yes (lazy import) |
| `fredapi` | `fred.py` | Yes (lazy import) |
| `kaggle` | `kaggle_fetcher.py` | Yes (lazy import) |
| `openml` | `openml_fetcher.py` | Yes (lazy import) |

---

## Test Coverage (Current State)

| Test | What It Covers |
|---|---|
| `test_setup_wizard_existing_valid_config` | Config loading from existing JSON |
| `test_setup_wizard_manual_invalid_json` | Corrupted JSON fallback |
| `test_get_fetcher_strategy_routing` | Factory routes source → correct class |
| `test_format_alchemy_convert_dispatcher` | Format detection dispatching |
| `test_web_analyzer_report_generation` | URL analysis report fields |
| `test_provision_data_directory` | All 11 platform folders created |
| `test_markdown_description_generation` | MD file generated with correct sections |

**Missing coverage:**
- Empty DataFrame → MD profiler
- Special characters in column names
- File permission errors during save
- Actual FormatAlchemy roundtrip (CSV → SQLite → Excel)
- CLI argument parsing
- Any real HTTP fetcher integration

---

## Decision Log

| Decision | Rationale | Date |
|---|---|---|
| Use `BaseFetcher` ABC with template method | Enforces consistent 6-phase pipeline across all 10 sources | Pre-existing |
| Add `sanitize_name()` for folder names | Prevents filesystem errors from special characters in queries | 2026-08-11 |
| Keep legacy `.txt` alongside new `.md` | Backward compatibility with existing workflows | 2026-08-11 |
| Call `provision_data_directory` at CLI startup | Ensures folder tree exists before any fetcher runs | 2026-08-11 |
| Use `df.describe(include='all')` for stats | Comprehensive statistical overview including categorical columns | 2026-08-11 |
| Sample-guard on `describe()` proposed but not yet applied | Prevents OOM on large datasets — **pending fix** | 2026-08-11 |

---

## Recency & Confidence

- **Last full audit:** 2026-08-11 (this note) — confidence: **high**
- **Implementation by:** Gemini 3.5 Flash → reviewed by Claude Opus 4.6
- **Test pass rate:** 7/7 (100%) — but coverage is shallow
- **Production-readiness:** **Medium** — critical bugs identified, no data loss risk but markdown corruption and OOM are possible

---

## Related Notes

- [[AUDIT_AND_FIX_PLAN]] — step-by-step repair instructions for all findings
- [[SKILL.md]] — user-facing skill documentation
- [[FormatAlchemy]] — format conversion engine details
- [[WebAnalyzer]] — URL scrapability assessment system
