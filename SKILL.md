---
name: data-fetcher-pipeline
description: A professional-grade, architecture-driven retrieval pipeline for fetching, downloading, and streaming raw datasets across diverse APIs for Data Science, ML, and BI workflows.
---

# data-fetcher-pipeline

A highly modular data-fetching pipeline tailored for coding agents. It ensures rate-limit compliance, raw data integrity, and strict environment configuration across multiple backend architectures.

## 1. High-Level Architecture & Value Proposition

- **Zero-Imputation Data Purity:** Extracts data payloads accurately and completely, without enforcing unauthorized data manipulation. Preserves exact state and dimensions.
- **Automated Data Dictionaries:** Generates robust `dataset_description.txt` metadata dictionaries profiling schema structure and null densities.
- **Secure Configuration (Lazy-Loaded State):** Enforces strict OS-standard credential management (`~/.config/data-fetcher-pipeline/config.json`), resolving previous shell-injection/SAST vulnerabilities and prompting for API credentials only at execution time.
- **Operational Efficiency:** Reduces boilerplate API extraction logic, accelerating machine learning CI/CD pipelines.

## 2. Workspace Visualization & File Management

Enforces a rigid output schema directly on your workstation to maintain strict filesystem health:

```text
📦 ~/Desktop/datasets_of_data-fetcher-pipeline/
 ┣ 📂 kaggle/
 ┃ ┗ 📂 CSV/
 ┃   ┗ 📂 retail_sales_data_2024/
 ┃     ┣ 📜 _raw.csv
 ┃     ┗ 📜 dataset_description.txt
 ┗ 📂 openml_org/
   ┗ 📂 JSON/
     ┗ 📂 healthcare_metrics/
       ┣ 📜 _raw.json
       ┗ 📜 dataset_description.txt
```

## 3. Deep Technical Guide

### Setup Instructions

If executing within a legacy or automated container environment, manually map the credentials:
```bash
export KAGGLE_USERNAME="your_username"
export KAGGLE_KEY="your_api_key"
export SEC_API_KEY="your_sec_key"
```
For standard CLI executions, allow the `fetcher_engine.py` interactive Setup Wizard to construct your secure JSON configuration file.

### Execution Protocol

Pass natural language instructions to your automated agent, specifying the target endpoint:

- "Use the data-fetcher-pipeline to get the latest COVID-19 dataset from WHO."
- "Use the data-fetcher-pipeline to download the SEC EDGAR 10-K filings for AAPL."
- "Fetch the housing prices dataset from Kaggle using the data-fetcher-pipeline."

The pipeline enforces execution serialization and implements algorithmic delays to bypass automated rate-limiting algorithms.
