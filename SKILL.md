---
name: data-fetcher-pipeline
description: A professional-grade retrieval tool for fetching, downloading, and streaming datasets for Data Science, ML, and BI workflows.
---

# data-fetcher-pipeline

A focused data-fetching pipeline for coding agents that extracts, downloads, and streams data while managing rate limits.

## Setup Instructions

1. Configure your API keys as environment variables before running:
   ```bash
   export KAGGLE_USERNAME="your_username"
   export KAGGLE_KEY="your_api_key"
   export SEC_API_KEY="your_sec_key"
   ```

## How to use

Run the pipeline by specifying your target data source:

- "Use the data-fetcher-pipeline to get the latest COVID-19 dataset from WHO."
- "Use the data-fetcher-pipeline to download the SEC EDGAR 10-K filings for AAPL."
- "Fetch the housing prices dataset from Kaggle using the data-fetcher-pipeline."

This is strictly a retrieval tool; it does not clean or impute data. It enforces sequential fetching to prevent IP bans.
