# data-fetcher-pipeline

[![skills.sh](https://skills.sh/b/zyadmad56-spec/data-fetcher-pipeline)](https://skills.sh/zyadmad56-spec/data-fetcher-pipeline)

A focused data fetching pipeline for coding agents that extracts, downloads, and streams data while managing rate limits to prevent IP bans.

Best use: let your agent use this pipeline to reliably retrieve datasets from external sources before starting analysis or model training. This is exclusively a retrieval tool; it does not perform data cleaning or imputation.

## Install

Browse the package first:

```bash
npx skills add zyadmad56-spec/data-fetcher-pipeline --list
```

Install the package:

```bash
npx skills add zyadmad56-spec/data-fetcher-pipeline
```

Install globally:

```bash
npx skills add zyadmad56-spec/data-fetcher-pipeline --global
```

Works with Claude Code, Codex, Cursor, OpenCode, and other supported agents via the [Skills CLI](https://github.com/vercel-labs/skills).

## Setup

You must configure your API keys as environment variables before running the pipeline. The agent will read these variables at runtime.

```bash
export KAGGLE_USERNAME="your_username"
export KAGGLE_KEY="your_api_key"
export SEC_API_KEY="your_sec_key"
```

For Windows PowerShell:
```powershell
$env:KAGGLE_USERNAME="your_username"
$env:KAGGLE_KEY="your_api_key"
```

## How to use it

Run the pipeline by specifying your target data source and query:

```text
Use the data-fetcher-pipeline to get the latest COVID-19 dataset from WHO.
Use the data-fetcher-pipeline to download the SEC EDGAR 10-K filings for AAPL.
Fetch the housing prices dataset from Kaggle using the data-fetcher-pipeline.
```

## Which pipeline to run

| Source | Retrieves | Typical use case |
| --- | --- | --- |
| `Kaggle` | Datasets and competition data | Data Scientists training machine learning models or testing predictive algorithms. |
| `SEC EDGAR` | Financial filings (10-K, 10-Q) and corporate data | Data Analysts and BI teams performing financial modeling or market analysis. |
| `World Bank & WHO` | Socioeconomic and health metrics | Researchers and Economists running macro-level analyses. |

The pipeline enforces sequential fetching and polite-request delays across all these sources to comply with server limitations and ensure stable, continuous extraction.

## Repository shape

```text
data-fetcher-pipeline/
├── README.md
├── SKILL.md
├── requirements.txt
├── data-fetcher-pipeline.skill
├── references/
│   └── source-constraints.md
└── scripts/
    └── fetcher_engine.py
```
