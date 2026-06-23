# data-fetcher-pipeline

[![skills.sh](https://skills.sh/b/zyadmad56-spec/data-fetcher-pipeline)](https://skills.sh/zyadmad56-spec/data-fetcher-pipeline)

A focused **ETL pipeline skill** for coding agents: a safety-first extractor that safely fetches, sanitizes, and converts massive datasets without breaking your local machine.

Best use: let your agent use this skill whenever you need to fetch data from global sources (Kaggle, WHO, World Bank, SEC EDGAR), safely scrape behind rate limits, or convert massive files. It runs as a self-contained, protective ETL system.

## Install

Browse the package first:

```bash
npx skills add zyadmad56-spec/data-fetcher-pipeline --list
```

Install the package:

```bash
npx skills add zyadmad56-spec/data-fetcher-pipeline
```

Install for a specific agent:

```bash
npx skills add zyadmad56-spec/data-fetcher-pipeline --agent codex
npx skills add zyadmad56-spec/data-fetcher-pipeline --agent claude-code
npx skills add zyadmad56-spec/data-fetcher-pipeline --agent cursor
```

Install globally:

```bash
npx skills add zyadmad56-spec/data-fetcher-pipeline --global
```

Works with Claude Code, Codex, Cursor, OpenCode, and other supported agents via the [Skills CLI](https://github.com/vercel-labs/skills).

## How to use it

Run the pipeline by simply asking your agent for data:

```text
Fetch the Inside Airbnb data for London and save it as Parquet.
Download the 3-million-row SEC EDGAR dataset for 2024.
Get the WHO dataset, skip the conversion, and extract a data dictionary.
```

## How it protects your workspace

| Feature | Kicks in when | Catches | Pair with |
| --- | --- | --- | --- |
| `OOM Protection` | Converting massive datasets | Out-of-memory crashes on Excel conversions >1M rows | - |
| `Safe Dependencies` | The script needs external libraries | Silent, global Python environment corruption | - |
| `Zero-Clutter` | Quick data validations & row counts | Leftover `test.py` or temporary scripts in your root | `clean-code-guard` |
| `Auto-Doc` | Finalizing dataset extraction | `fpdf` Unicode crashes; missing data dictionaries | - |

## The skill

### data-fetcher-pipeline

Extracts data through a strict 7-stage execution funnel (Routing → Scouting → Approval → Stealth Extraction → Validation → Format Standardization → Documentation). It strictly aborts fatal conversions, prevents silent dependency installs, and natively generates Kaggle-grade Markdown data dictionaries.
