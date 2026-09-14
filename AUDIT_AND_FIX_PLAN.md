# Audit & Fix Plan: data-fetcher-pipeline

> **Auditor:** Claude Opus 4.6 (Thinking)  
> **Date:** 2026-08-11  
> **Scope:** Full codebase — all 22 files across `scripts/`, `tests/`, and config  
> **Baseline:** 7/7 tests passing, live MSFT fetch verified with MD profiler output

---

## 1. Executive Summary

**What was implemented correctly:**
- Strategy pattern (BaseFetcher → 10 concrete fetchers → factory) is architecturally sound and extensible.
- Directory hierarchy (`data_raw/<source>/<dataset>/`) provisions correctly on startup and on fetch.
- Markdown profiler generates all 5 required sections (Overview, Metadata, Preview, Schema, Stats).
- HTTP retry logic (`http_utils.py`) handles 429/5xx/timeout correctly with exponential backoff.
- Lazy imports for `openml` and `kaggle` prevent factory-level crashes when optional deps are missing.
- `non_interactive` / `auto_approve` flags properly gate all `input()` calls for agent usage.
- Exception handling is specific throughout (no `except Exception:` in production code).

**What has gaps:**
- Mixed `os.path` / `pathlib.Path` usage — the requirement was `pathlib` exclusively.
- Markdown profiler crashes on edge cases (pipe characters in column headers, empty DataFrames).
- `format_alchemy.py` uses `os.path` everywhere despite the pathlib requirement.
- `yahoo.py` has no `__init__` declaring attributes, unlike every other fetcher.
- `describe(include='all')` on million-row DataFrames will spike memory.
- Test coverage is thin (7 tests total, zero negative-path tests).
- `provision_data_directory` is called redundantly from both `cli.py` AND `config.py`.
- Generated scraper scripts from `web_analyzer.py` contain `except Exception as e:` (violates workspace rule).

---

## 2. Detailed Issue Breakdown

### 🔴 Critical Bugs

| # | File | Line(s) | Issue | Impact |
|---|---|---|---|---|
| C1 | `base.py` | 106 | **Pipe characters in column headers break markdown tables.** `_df_to_markdown_table` escapes `|` in cell values but not in the header row. A column named `Open\|High` would corrupt the entire table. | Broken `.md` output |
| C2 | `base.py` | 147 | **`df.head(5).astype(str)` on a DataFrame with mixed timezone-aware datetimes can raise `TypeError`.** The MSFT output showed this works by accident, but timezone-naive vs timezone-aware mixing will crash `.astype(str)`. | Crash during MD generation |
| C3 | `base.py` | 166 | **`df.describe(include='all')` materializes a full statistical summary in memory.** For a 10M-row DataFrame with 50+ columns, this creates an intermediate DataFrame that can exhaust memory. | OOM crash on large datasets |
| C4 | `format_alchemy.py` | 186-200 | **Dead code in `convert()` Excel branch.** The `else` block (line 192-200) can never execute when `source_ext == "csv"` because the `if source_ext == "csv"` branch already returned. The `else` re-checks `source_ext == "csv"` inside a ternary, which is logically unreachable. | Dead code / confusion |

### 🟠 Edge-Case Vulnerabilities

| # | File | Line(s) | Issue | Impact |
|---|---|---|---|---|
| E1 | `base.py` | 116-184 | **`generate_markdown_profile` is called even when `validate_payload` would reject the DataFrame (empty / >80% null).** The call order in `run()` is: validate → save_csv → generate_markdown_profile. This is correct. BUT if `generate_markdown_profile` is called directly (as in the test), an empty DataFrame would produce a malformed `.md` with no rows in preview table and `0.00%` null density. | Misleading MD for empty data |
| E2 | `base.py` | 44 | **`os.path.normpath` on Windows converts forward slashes to backslashes**, making paths non-portable when stored in config or logged. The requirement specified `pathlib.Path` exclusively. | Cross-platform path inconsistency |
| E3 | `config.py` | 13 | **`provision_data_directory` is called inside `setup_wizard`**, and ALSO at line 96 of `cli.py`. When CLI runs `main()`, it calls `provision_data_directory` at line 96, then calls `setup_wizard` at line 121, which calls it again. The function is idempotent so it doesn't crash, but it's wasteful I/O. | Redundant filesystem calls |
| E4 | `yahoo.py` | 6-25 | **`YahooFinanceFetcher` does not declare an `__init__` method.** Every other fetcher properly declares `__init__` calling `super().__init__()` and declaring instance attributes. `yahoo.py` relies entirely on the parent `__init__`, which is fine functionally but inconsistent with the established pattern. | Style inconsistency |
| E5 | `web_analyzer.py` | 167, 184 | **Generated scraper scripts contain `except Exception as e:`.** The workspace global rule in `AGENTS.md` forbids broad exception catches. While these are in *generated* scripts (not production code), an agent running the generated script would violate the rule. | Workspace rule violation in output |
| E6 | `cli.py` | 194-201 | **Re-reading the CSV after `fetcher.run()` for row/col counts is wasteful.** The DataFrame was already in memory during `save_csv()`. The counts could be returned directly. | Unnecessary I/O |

### 🟡 Code Quality / Standards

| # | File | Line(s) | Issue |
|---|---|---|---|
| Q1 | `base.py`, `config.py`, `cli.py`, `format_alchemy.py`, `web_analyzer.py`, all fetchers | Throughout | **Mixed `os.path` and `pathlib.Path` usage.** The requirement specified `pathlib.Path` exclusively. 47 instances of `os.path.*` remain across the codebase. |
| Q2 | `format_alchemy.py` | 5 | **Unused imports:** `Dict`, `Any`, `List` from `typing` are imported but never used. |
| Q3 | `tests/` | Throughout | **Only 7 tests total.** No tests for: failed HTTP downloads, empty DataFrame handling in MD profiler, `save_csv` file permissions, `format_alchemy` actual conversions (only dispatcher mocking), CLI argument parsing, `interactive_flow`, or any fetcher's `scout()`/`extract()`. |
| Q4 | `base.py` | 195-209 | **Legacy `.txt` description file is still generated alongside `.md`.** The `.md` profiler supersedes it. The `.txt` file should be removed or gated behind a flag. |
| Q5 | `cli.py` | 132-135 | **Humanized delay (`time.sleep(2-5s)`) runs even in `--yes` mode.** An agent auto-approving downloads still waits 2-5 seconds for no reason. Should be skipped when `--yes` is passed. |

---

## 3. Ultra-Granular Action Plan

### Pass 1: Critical Fixes

#### Task 1.1 — Escape pipe characters in markdown column headers
**File:** `scripts/base.py`  
**Function:** `_df_to_markdown_table`  
**Line:** 106  
**Action:** Apply the same `|` → `\|` escaping to column header strings.
```python
# BEFORE
headers = " | ".join(str(col) for col in columns)

# AFTER
headers = " | ".join(str(col).replace("|", "\\|") for col in columns)
```

#### Task 1.2 — Guard markdown preview against dtype conversion failures
**File:** `scripts/base.py`  
**Function:** `generate_markdown_profile`  
**Line:** 147  
**Action:** Wrap `.astype(str)` in a try/except and fall back to `.to_string()`.
```python
# BEFORE
preview_df = df.head(5).astype(str)

# AFTER
try:
    preview_df = df.head(5).copy()
    for col in preview_df.columns:
        preview_df[col] = preview_df[col].astype(str)
except (TypeError, ValueError):
    preview_df = df.head(5).fillna("N/A").astype(str)
```

#### Task 1.3 — Cap `describe()` to prevent OOM on large datasets
**File:** `scripts/base.py`  
**Function:** `generate_markdown_profile`  
**Line:** 166  
**Action:** Sample down to 100K rows before running `describe()`.
```python
# BEFORE
desc_df = df.describe(include='all').reset_index().fillna("N/A").astype(str)

# AFTER
sample = df.sample(n=min(100_000, len(df)), random_state=42) if len(df) > 100_000 else df
desc_df = sample.describe(include='all').reset_index().fillna("N/A").astype(str)
```

#### Task 1.4 — Remove dead code in FormatAlchemy convert() Excel branch
**File:** `scripts/format_alchemy.py`  
**Function:** `convert`  
**Lines:** 192-200  
**Action:** The `else` branch after `if source_ext == "csv"` in the Excel target path should handle non-CSV → Excel conversion (e.g., JSON → Excel). Currently it re-checks CSV which is dead code. Replace with:
```python
elif target_format in ["excel", "xlsx"]:
    out_path = f"{base_path}.xlsx"
    if source_ext == "csv":
        engine = cls(source_path)
        engine.execute_pipeline()
        return engine.excel_filepath
    else:
        raise ValueError(f"Conversion from {source_ext} to Excel requires a CSV source. Convert to CSV first.")
```

### Pass 2: Edge-Case Hardening

#### Task 2.1 — Remove redundant `provision_data_directory` from `config.py`
**File:** `scripts/config.py`  
**Lines:** 13-14  
**Action:** Delete the `provision_data_directory` call from `setup_wizard`. Keep only the one in `cli.py:main()`.

#### Task 2.2 — Add `__init__` to `YahooFinanceFetcher`
**File:** `scripts/fetchers/yahoo.py`  
**Action:** Add consistent `__init__` matching the pattern of all other fetchers.
```python
def __init__(self, query: str, outdir: str, config: Dict[str, str]) -> None:
    super().__init__(query, outdir, config)
```

#### Task 2.3 — Replace `except Exception` in generated scraper scripts
**File:** `scripts/web_analyzer.py`  
**Lines:** 167, 184  
**Action:** Replace `except Exception as e:` in generated script templates with `except (requests.RequestException, ValueError, OSError) as e:`.

#### Task 2.4 — Skip humanized delay when `--yes` is passed
**File:** `scripts/cli.py`  
**Lines:** 132-135  
**Action:** Gate the delay behind `not args.yes`:
```python
if not args.json_output and not args.yes:
    delay = random.uniform(2, 5)
    ...
```

#### Task 2.5 — Avoid re-reading CSV for row/col counts
**File:** `scripts/cli.py`  
**Lines:** 194-201  
**Action:** Return `(csv_path, len(df), len(df.columns))` from `save_csv` or `run()` instead of re-reading the file with `pd.read_csv`.

### Pass 3: Pathlib Migration

#### Task 3.1 — Migrate `base.py` to pure pathlib
Replace lines 44, 188, 196 with `Path` equivalents. Remove `import os`.

#### Task 3.2 — Migrate `format_alchemy.py` to pure pathlib
Replace all 15 `os.path.*` calls with `Path` equivalents. Remove `import os`.

#### Task 3.3 — Migrate `config.py` to pure pathlib
Replace `os.path.expanduser`, `os.path.join`, `os.path.exists`, `os.makedirs` with `Path` equivalents.

#### Task 3.4 — Migrate `cli.py` to pure pathlib
Replace `os.path.dirname`, `os.path.abspath`, `os.path.join` with `Path` equivalents.

#### Task 3.5 — Migrate `web_analyzer.py` to pure pathlib
Replace `os.makedirs`, `os.path.join` with `Path` equivalents.

### Pass 4: Test Coverage Expansion

#### Task 4.1 — Add negative-path tests
```python
def test_markdown_profile_empty_dataframe(tmp_path: Path) -> None:
    """Verify MD profiler handles empty DataFrame gracefully."""

def test_markdown_profile_special_characters_in_columns(tmp_path: Path) -> None:
    """Verify pipe characters in column names don't break markdown tables."""

def test_save_csv_readonly_directory(tmp_path: Path) -> None:
    """Verify save_csv raises OSError on read-only directories."""

def test_provision_data_directory_custom_source(tmp_path: Path) -> None:
    """Verify dynamic source folders are auto-created for unknown sources."""
```

#### Task 4.2 — Add FormatAlchemy integration tests
```python
def test_csv_to_sqlite_roundtrip(tmp_path: Path) -> None:
    """Create CSV, convert to SQLite, verify table exists and row count matches."""

def test_json_to_csv_conversion(tmp_path: Path) -> None:
    """Create JSON array, convert to CSV, verify column mapping."""
```

### Pass 5: Cleanup

#### Task 5.1 — Remove legacy `.txt` description generator
**File:** `scripts/base.py`  
**Lines:** 195-209  
**Action:** Delete the `dataset_description.txt` generation block. The `.md` profiler supersedes it entirely.

#### Task 5.2 — Remove unused imports in `format_alchemy.py`
**File:** `scripts/format_alchemy.py`  
**Line:** 5  
**Action:** Change `from typing import Dict, Any, List` to remove unused symbols or delete the line entirely since no typing symbols are actually used in the file.
