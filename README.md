# data-fetcher-pipeline

[![skills.sh](https://skills.sh/b/zyadmad56-spec/data-fetcher-pipeline)](https://skills.sh/zyadmad56-spec/data-fetcher-pipeline)

A focused data-fetching pipeline for coding agents: A professional-grade retrieval tool for fetching, downloading, and streaming datasets for Data Science, ML, and BI workflows.

Best use: let your agent use this pipeline to securely retrieve data from global sources, managing rate limits and environment keys before starting any downstream analysis. This is strictly a retrieval tool; it does not clean or impute data.

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

Configure your API keys as environment variables (`export KAGGLE_KEY="..."`) before running. Run the pipeline by asking your agent for data:

```text
Use the data-fetcher-pipeline to get the latest COVID-19 dataset from WHO.
Use the data-fetcher-pipeline to download the SEC EDGAR 10-K filings for AAPL.
Fetch the housing prices dataset from Kaggle using the data-fetcher-pipeline.
```

## Which pipeline to run

| Source | Retrieves | Typical use case |
| --- | --- | --- |
| `Kaggle` | Datasets and competition data | **Data Scientists** and **ML Engineers** training machine learning models or testing predictive algorithms. |
| `SEC EDGAR` | Financial filings (10-K, 10-Q) and corporate data | **Data Analysts** and **Business Analysts** performing financial modeling or market analysis. |
| `World Bank & WHO` | Socioeconomic and health metrics | **Data Engineers** building macro-level data warehouses and researchers running global analyses. |

The pipeline enforces sequential fetching and polite-request delays across all these sources to comply with server limitations and ensure stable, continuous extraction.

## Repository Shape

```text
data-fetcher-pipeline/
├── README.md
├── SKILL.md
├── LICENSE
├── requirements.txt
├── data-fetcher-pipeline.skill
├── references/
│   └── source-constraints.md
└── scripts/
    └── fetcher_engine.py
```
