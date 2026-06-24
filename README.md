# data-fetcher-pipeline

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

### ✨ New Feature: OpenML Intelligent Search

The pipeline now features **OpenML integration** with a smart, interactive search mechanism! You can now easily search for diverse ML datasets (like "finance" or "healthcare") directly from the interactive `run_pipeline.sh` wizard, and the pipeline will dynamically find the top-ranked dataset, extract it, and cleanly apply our strict `CSV` formatting and sanitized topic-categorization auto-rules!

## Running with Docker

When running the pipeline within a Docker container, the downloaded files will be written to an isolated filesystem unless you explicitly map a volume to your host machine.

1. Create a `.env` file based on `.env.example` and populate your API keys.
2. Run the container, passing the `.env` file and mapping your host's Desktop to the container's `/output` directory. The pipeline will automatically detect the `OUTPUT_DIR` environment variable.

```bash
docker run --env-file .env \
  -e OUTPUT_DIR=/output \
  -v ~/Desktop:/output \
  your-docker-image-name
```

## Smart Auto-Organization

The pipeline automatically sanitizes complex URLs and smartly organizes your downloads into a clean, auto-generated directory structure directly on your Desktop:
`Desktop/datasets_of_data-fetcher-pipeline/{source}/{format}/{topic}`

## Security & Permissions Transparency

When installing this skill, automated security scanners (like Snyk or Socket) may flag the package with a "Medium Risk" alert. **This is an expected false-positive.**

This skill is flagged because of its interactive onboarding script (`run_pipeline.sh`) and auto-organization logic which require:
1. **Dynamic Environment Variable Handling:** The script interactively asks for and auto-populates your local `.env` file with API keys. 
2. **Host File System Access:** It uses standard Unix commands (`mkdir -p`) to safely auto-generate organized folders directly on your host machine's Desktop.

We are fully transparent about this: the code is strictly open-source, contains no trackers, and operates entirely locally without ever transmitting your keys anywhere other than the official data sources you explicitly request.

## Which pipeline to run

| Source | Retrieves | Typical use case |
| --- | --- | --- |
| `OpenML` (NEW!) | Diverse machine learning datasets via intelligent search | **Data Scientists** quickly searching and downloading top-ranked ML datasets. |
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
├── .env.example
├── data-fetcher-pipeline.skill
├── references/
│   └── source-constraints.md
└── scripts/
    ├── fetcher_engine.py
    ├── run_pipeline.sh
    └── setup_dataset_dir.sh
```
