---
name: data-fetcher-pipeline
description: Executes an automated data retrieval pipeline connecting to Kaggle, OpenML, SEC EDGAR, and FRED to extract raw datasets for machine learning and data engineering workloads. Use when a user needs to fetch, download, or search for raw datasets, market data, or macro-economic statistics.
---

# Data Fetcher Pipeline

A highly modular data-fetching pipeline tailored for coding agents. It ensures rate-limit compliance, raw data integrity, and strict environment configuration across multiple backend architectures.

## Quick start

Execute the pipeline via natural language mapping to the target data endpoint.

**Example Invocations:**
- "Use the data-fetcher-pipeline to get the latest COVID-19 dataset from WHO."
- "Use the data-fetcher-pipeline to download the SEC EDGAR 10-K filings for AAPL."
- "Fetch the housing prices dataset from Kaggle using the data-fetcher-pipeline."

## Workflows

### 1. Hybrid Installation and Deployment
The pipeline supports dual deployment vectors to maximize OS compatibility and user preference.
- **Global CLI (Recommended):** Deploy the package globally (`pip install .`) to expose the `data-fetcher` executable natively to the system path.
- **Interactive Bash:** Invoke `./scripts/run_pipeline.sh` directly to utilize the guided shell wizard.

### 2. Configuration Management (Lazy-Loaded State)
The system enforces strict OS-standard credential management to resolve SAST vulnerabilities. Configurations are managed dynamically via an isolated JSON dictionary (`~/.config/data-fetcher-pipeline/config.json`).
If executing within a legacy or automated container environment, manually map credentials or use the provided `config_template.json`.

### 3. Engine Execution and Zero-Imputation Purity
The strategy-pattern Python engine parses arguments (`--source`, `--query`) and handles polymorphic instantiation of API extraction handlers. The payload is extracted precisely as served by the upstream source, preserving exact dimensions without unapproved imputation.

### 4. Post-Processing ETL (Format Alchemy)
After raw data extraction, the pipeline seamlessly integrates with an optional ETL module (`format_alchemy.py`). This engine chunks massive CSV payloads directly into memory-safe SQLite databases (`.db`) and exports capped Excel workbooks natively, preventing out-of-memory errors.

## Advanced features

**Automated Metadata Generation**
Upon successful data retrieval, the engine generates a robust `dataset_description.txt` metadata dictionary profiling schema structure and null densities. 

**Zero-Args Interactive Wizard**
Executing the engine without specific arguments triggers an interactive CLI wizard, implementing a full state-machine fallback to guide parameter collection before returning to the core execution loop.

### Supported Data Sources

| Source | Description | Typical use case |
| --- | --- | --- |
| **OpenML** | An inclusive, open-source machine learning platform for dynamically searching and retrieving rich datasets and experiments. | Data Scientists and ML Engineers querying global index for top-ranked ML datasets. |
| **Kaggle** | The premier platform for data science competitions and massive, diverse machine learning datasets. | Data Scientists and ML Engineers training machine learning models or testing predictive algorithms. |
| **SEC (EDGAR)** | The US Securities and Exchange Commission database, essential for fetching raw corporate financial filings (10-K, 10-Q) and deep market analysis data. | Data Analysts and Business Analysts performing financial modeling or market analysis. |
| **FRED** | Federal Reserve Economic Data, the ultimate source for macroeconomic time-series data, socioeconomic metrics, and financial health indicators. | Data Engineers building macro-level data warehouses and researchers running global analyses. |

The pipeline enforces sequential fetching and polite-request delays across all these sources to comply with server limitations and ensure stable, continuous extraction.

### Internal Code Architecture

```text
data-fetcher-pipeline/
├── README.md
├── SKILL.md
├── LICENSE
├── requirements.txt
├── .env.example
├── config_template.json
├── references/
│   └── source-constraints.md
└── scripts/
    ├── fetcher_engine.py
    ├── format_alchemy.py
    └── run_pipeline.sh
└── tests/
    └── test_fetcher_engine.py
```

For a comprehensive evaluation of the architecture, refer to the assessment section inside `README.md`.
