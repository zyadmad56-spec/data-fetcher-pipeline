# data-fetcher-pipeline

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python Version](https://img.shields.io/badge/python-3.10%2B-blue.svg)
![Data Integrity](https://img.shields.io/badge/Data-Provenance-blue.svg)
![Architecture](https://img.shields.io/badge/Architecture-Clean_Architecture-blue.svg)
![Tests](https://github.com/zyadmad56-spec/data-fetcher-pipeline/actions/workflows/tests.yml/badge.svg)

Fetch datasets, review a sample, and save them with a Markdown profile and a record of their source. The pipeline supports built-in data platforms, saved custom sources, and public files or simple HTML tables.

### Project video

https://github.com/user-attachments/assets/7e0bb809-6606-429c-b597-557ce4bbec78

The guide below describes the current features and limits. [Download the original video](media/data_fetcher_promo_en.mp4).

## 1. High-Level Architecture & Value Proposition

The pipeline separates fetch decisions from network requests, terminal prompts, and file writes. Providers share the same preview, approval, save, and cleanup flow.

- **Dataset provenance:** Every run records a hash of its normalized CSV. Direct CSV, TSV, and gzip downloads also retain their original bytes with a separate hash. SDK and API responses are normalized without a retained source payload.
- **CSV and Markdown profiles:** Providers return tabular data with their own field names. Each saved CSV gets a `<dataset_name>_description.md` with source metadata, sample rows, missing-value counts, inferred types, and statistics. The pipeline does not align schemas across platforms.
- **Local credentials:** Fetch commands read `~/.config/data-fetcher-pipeline/config.json`. This is a plain JSON file, with owner-only permissions where the operating system supports them. The repository contains `config_template.json` with empty values. Keep your filled config on your own computer and out of Git.
- **Shared workflow:** The CLI handles output folders, previews, row limits, conversion, and source registration so each provider can focus on fetching data.

The pipeline does not impute missing values or clean data for analysis. Parsing can infer types and change text formatting in the normalized CSV, such as leading zeros. For direct CSV, TSV, and gzip downloads, the separate original file preserves the downloaded bytes.

## 2. Workspace Visualization & File Management

Outputs use `data_raw/<source>/<topic>/` by default. Use `--outdir` to choose another location. The examples below show CSV and profile files; fetches also write a manifest, and direct file downloads retain an original payload.

```text
data_raw/
 ├── kaggle/
 │   └── retail_sales_2024/
 │       ├── retail_sales_2024_raw.csv
 │       └── retail_sales_2024_description.md
 ├── openml/
 │   └── healthcare_metrics/
 │       ├── healthcare_metrics_raw.csv
 │       └── healthcare_metrics_description.md
 └── yahoo/
     └── msft/
         ├── msft_raw.csv
         └── msft_description.md
```

## 3. Deep Technical Guide

### Guided fetching and custom platforms

Run `data-fetcher` without arguments to choose ready/saved sources or an external
URL first. Ready sources are numbered and accept names or numbers. The interview
collects up to five requirements, shows actual sample rows before final approval,
and asks whether to save a successfully fetched external platform. Rejecting
registration removes its temporary script and preserves the dataset.

For chat agents, follow [the conversation workflow](references/conversation-workflow.md).
Use `--preview-only --yes --non-interactive --json-output` to return actual sample
rows before asking for final approval, then make the approved full fetch. CSV/TSV
prefixes are bounded to 256 KB; SDK/compressed/custom previews can need full
transfer. Separate preview/fetch commands may transfer twice.

`--source custom --query URL` now fetches supported public files/simple HTML tables
through the normal CSV/profile/manifest pipeline. `--analyze-only` requests only
the assessment. Dynamic/authenticated/ambiguous layouts need reviewed tailored
extractors; blocked targets do not yield a successful fetch. `--columns a,b`
validates/projects exact fields; `--table INDEX` selects a simple HTML table;
`--output-format json/excel/parquet/sqlite` converts the saved CSV. `--goal` records
context and is not a country/date/content filter.

For agent retention after a successful custom fetch, pass `--defer-source-choice`,
ask the user, then resolve the returned ID using `--keep-source ID --save-source
"Platform Name" --json-output` or `--discard-source ID --json-output`. No second
download is needed. Saved platforms appear beside built-ins in `--list-sources`,
with checksum-checked scripts under `~/.config/data-fetcher-pipeline/source_scripts`.
Corrupt descriptors and collisions fail explicitly. Scripts are trusted local
code, not a sandbox.

### Installation Strategy

Install the Python package in your active environment. This installs its dependencies and the `data-fetcher` command in that environment.

**Package Installation:**
```bash
pip install .
```
With that environment active, run the command from any directory:
```bash
data-fetcher --source openml --query "finance"
```
Or execute the script interface from the repository root:
```bash
python scripts/cli.py --source openml --query "finance"
```

#### AI Agent Deployment Context

Browse the package registry:

```bash
npx skills add zyadmad56-spec/data-fetcher-pipeline --list
```

Deploy the package via the Skills CLI framework:

```bash
npx skills add zyadmad56-spec/data-fetcher-pipeline
```

Agent-specific installations:

```bash
npx skills add zyadmad56-spec/data-fetcher-pipeline --agent codex
npx skills add zyadmad56-spec/data-fetcher-pipeline --agent claude-code
npx skills add zyadmad56-spec/data-fetcher-pipeline --agent cursor
```

Global provisioning:

```bash
npx skills add zyadmad56-spec/data-fetcher-pipeline --global
```

The skill can be installed for Claude Code, Codex, Cursor, OpenCode, and other agents supported by the [Skills CLI](https://github.com/vercel-labs/skills).

### Engine Execution

Ask your agent to use the skill. It should follow the conversation workflow, show real sample rows, and get approval before the full fetch.

```text
Use data-fetcher-pipeline to preview OpenML dataset 61.
Use data-fetcher-pipeline to fetch SEC XBRL company facts for AAPL.
Use data-fetcher-pipeline to preview a public CSV from this URL: <public-file-url>.
```

#### OpenML Interactive Strategy
Text queries such as `iris` prefer exact name matches, then smaller matching datasets. The CLI shows up to five candidates with their IDs and sizes. Automatic selection skips candidates above its cell budget; an interactive user can choose a larger candidate. A numeric query such as `--query 61` selects that dataset ID directly.

### Containerized Execution (Docker)

The repository does not include a Dockerfile or a published container image. If you build an image with the package installed and `data-fetcher` as its entry point, mount your output folder and local config. For example:

```bash
docker run \
  -e FRED_API_KEY=<your_key> \
  -v ~/.config/data-fetcher-pipeline:/root/.config/data-fetcher-pipeline \
  -v ~/Desktop:/output \
  your-docker-image-name --source openml --query 61 --outdir /output --yes --non-interactive
```

### Supported Data Sources

| Source | CLI Key | Description | Typical use case |
| --- | --- | --- | --- |
| **OpenML** | `openml` | Open-source machine learning platform for searching and retrieving benchmark datasets. | Finding benchmark datasets by name or ID. |
| **Kaggle** | `kaggle` | Dataset downloads using an owner/dataset identifier. | ML Engineers training predictive models and testing algorithms. |
| **SEC EDGAR** | `sec` | SEC XBRL company facts, flattened into rows. Does not download filing documents. | Financial analysts performing corporate research and financial modeling. |
| **FRED** | `fred` | Federal Reserve Economic Data time-series macroeconomic indicators. | Data Engineers building macro-level data warehouses. |
| **World Bank** | `worldbank` | Global development metrics, socioeconomic indicators, and climate data. | Policy researchers conducting global cross-country studies. |
| **Eurostat** | `eurostat` | Official European Union statistical data repository. | Economists analyzing official EU economic statistics. |
| **Data.gov** | `datagov` | US government open data portal via the GSA Catalog API v4 (DCAT). | Public policy analysts querying federal agency datasets. |
| **Inside Airbnb** | `airbnb` | City listings from listings.csv.gz. Reviews and calendars are not fetched. | Real estate analysts investigating housing and tourism trends. |
| **GitHub Data** | `github` | Dataset discovery across public GitHub repositories (stars-ranked repo search + CSV tree walk). | Data engineers mining open-source tabular datasets. |
| **Yahoo Finance** | `yfinance` / `yahoo` | Historical daily open, high, low, close, and volume data. Does not fetch fundamentals. | Quantitative traders building algorithmic trading strategies. |
| **CoinGecko** | `coingecko` | Cryptocurrency price, market cap, and volume history , no API key required. | Crypto analysts tracking digital-asset market history. |

**Pacing & Rate Limits:** the internal HTTP client applies per-host pacing (for example, CoinGecko ≥ 6s within one process) and handles `Retry-After` up to 60s. SDK clients have their own request behavior. JSON network metrics count only calls through the internal client; Yahoo/yfinance, OpenML, Kaggle, and generated custom extractors report `null` for those metrics with a `metrics_scope` explanation.

**Performance Characteristics:** supported direct downloads stream to disk with a 500 MB cap; parsing and SDK extraction can still use substantial memory. Excel export uses openpyxl write-only streaming with a 16,384-column guard. Normalized CSV saves use atomic replacement.

### Internal Code Architecture

```text
data-fetcher-pipeline/
├── pyproject.toml              # Modern package configuration
├── config_template.json        # All consumed credential keys
├── LICENSE
├── README.md
├── SKILL.md
├── references/
│   ├── architecture.md        # Dependencies and compatibility seams
│   ├── conversation-workflow.md # Agent preview and approval steps
│   └── source-constraints.md   # Provider limits and selection rules
└── scripts/
    ├── __init__.py
    ├── base.py                 # Compatible provider hooks and composed public entry points
    ├── core/                   # Effect-free models, naming policy and errors
    ├── application/            # Fetch lifecycle, frame selection and source retention
    ├── adapters/               # Terminal, network payload and persistence effects
    ├── composition.py          # Concrete adapter selection
    ├── config.py               # Setup wizard and credential lookup
    ├── cli.py                  # CLI entry point, JSON envelopes, guarded main
    ├── errors.py               # DataFetchError: coded errors + exit codes
    ├── factory.py              # Lazy provider registry (importlib on demand)
    ├── source_catalog.py       # Shared built-in source discovery metadata
    ├── source_registry.py      # Saved and pending reviewed custom extractors
    ├── dataset_profile.py      # Markdown profile rendering without file writes
    ├── workflow.py             # Source-first terminal interview
    ├── custom_source.py        # Custom extraction with the common fetch lifecycle
    ├── custom_template.py      # Standalone public-format extraction template
    ├── format_alchemy.py       # CSV/Excel/SQLite/JSON/Parquet conversion engine
    ├── http_utils.py           # Session pooling, retries, pacing, cost metering
    ├── provision.py            # Directory provisioning (import-light)
    ├── web_analyzer.py         # Custom URL scraping analyzer (SSRF-guarded)
    └── fetchers/
        ├── airbnb.py           # Inside Airbnb fetcher
        ├── coingecko.py        # CoinGecko crypto market fetcher (no key)
        ├── datagov.py          # Data.gov GSA Catalog API v4 fetcher
        ├── eurostat.py         # Eurostat bulk TSV fetcher
        ├── fred.py             # FRED economic data fetcher
        ├── generic.py          # Generic fallback fetcher
        ├── github_data.py      # GitHub repo search + tree CSV fetcher
        ├── kaggle_fetcher.py   # Kaggle dataset fetcher
        ├── openml_fetcher.py   # OpenML dataset fetcher
        ├── sec.py              # SEC XBRL facts fetcher
        ├── worldbank.py        # World Bank Indicators API fetcher
        └── yahoo.py            # Historical daily prices
```

### Architectural Assessment

**Cross-Platform Package Strategy:**
The Python package installs the CLI in the selected environment. Paths and directory creation use Python APIs. The current verification was run on Windows; it does not establish full compatibility on every operating system. Restricted or dynamic external sources may require a tailored extractor.

**Data Source Efficacy:**
The pipeline supports 11 platforms across 12 CLI keys, including the Yahoo/yfinance aliases. `--rows` limits saved rows; only some providers can limit transfer at the server. GitHub and Data.gov discovery choose a resource heuristically, so inspect `source_url` and the profile before using the result.

## 4. Recent Updates

- **Clean Architecture:** Core models and rules have no file or network effects. Application code owns the fetch lifecycle; adapters handle prompts, HTTP, storage, and conversion. Existing provider entry points remain available.
- **Guided fetching:** Choose built-in or saved sources, or enter an external URL. Review actual sample rows before approving a full fetch.
- **Saved custom sources:** Keep a reviewed extractor after a successful fetch, or discard its temporary script while keeping the dataset. Saved sources appear in `--list-sources`.
- **Provenance and warnings:** Normalized CSVs and retained originals have separate hashes. JSON responses report incomplete data, metadata-write failures, and cleanup failures.
- **Conversion:** Convert saved CSVs to Excel, JSON, Parquet, or SQLite. Parquet needs the optional `parquet` extra. Other input formats support a smaller set of conversion targets.
- **Regression tests:** The repository includes tests for the fetch lifecycle, provider behavior, CLI responses, conversion, and custom-source registration. GitHub Actions runs them on pushes and pull requests.

## 5. How to Install

To run the project locally:

1. **Clone the Repository & Navigate:**
   ```bash
   git clone https://github.com/zyadmad56-spec/data-fetcher-pipeline.git
   cd data-fetcher-pipeline
   ```
2. **Install Dependencies:**
   Ensure your virtual environment is active, then install the package:
   ```bash
   pip install .
   ```
3. **Start the Guided Workflow:**
   Run without arguments to choose ready/saved sources or an external platform, answer up to five data questions, review a sample and approve saving. Provider credentials are requested when needed.
   ```bash
   python scripts/cli.py
   ```

### Artifact and cleanup warnings

The fetch JSON response includes `description_path` and `manifest_path` only when
their current writes succeed (otherwise an empty string). If metadata writing or
temporary extractor cleanup fails, `warnings` explains the failure. A saved CSV
remains available; `complete` describes dataset completeness, independently of
metadata or cleanup warnings. Resource cleanup runs before the final response,
and an extraction error retains its original error code if cleanup also fails.

Generated extractors choose the parser from the current response Content-Type,
then the final URL extension. A saved HTML extractor can therefore read a CSV
export on its registered host. Unknown formats and ambiguous tables still fail
explicitly. Existing saved scripts retain their reviewed bytes and checksums;
regenerate and review a script to adopt a changed template.

Internal dependency direction and compatibility seams: [fetch architecture](references/architecture.md).

### Credentials

`config_template.json` contains credential names with empty values. To use it, copy it to `~/.config/data-fetcher-pipeline/config.json` on your computer, then fill only the values you need. Do not commit that filled file. `SEC_API_KEY` is the legacy name for a contact email, not an SEC API token.

Interactive providers can request missing credentials with masked input. Non-interactive runs use configured credentials or supported environment variables and fail if a required value is missing. Running the CLI without arguments starts source selection; it does not collect all API keys first.

### Tests and continuous integration

Keep `tests/` in the repository so contributors and CI can check behavior after changes. The workflow in `.github/workflows/tests.yml` installs the package and runs:

```bash
python -m pip install ".[dev,parquet]"
python -m pytest -q
```

Tests use fixtures and mocked providers. They do not establish that every external platform is currently available. The workflow checks pushes and pull requests; it does not deploy the project.
