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

### Hybrid Installation Strategy

The pipeline now supports dual installation vectors to maximize OS compatibility and user preference.

**Method 1: Global CLI (Recommended / Windows-Friendly)**
Deploy the Python package globally. This automatically resolves dependencies and exposes the `data-fetcher` executable to your system path.
```bash
pip install .
```
You can now execute the engine directly from any directory:
```bash
data-fetcher --source openml --query "finance"
```

**Method 2: Interactive Bash (Linux/Mac/Advanced Users)**
Retain the classic Unix approach by invoking the guided shell wizard directly. This method seamlessly manages interactive prompts and Python handoffs.
```bash
./scripts/run_pipeline.sh
```

#### Legacy / Automated Environments

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

### 🌐 Supported Data Sources

| Source | Description | Typical use case |
| --- | --- | --- |
| **OpenML** | An inclusive, open-source machine learning platform for dynamically searching and retrieving rich datasets and experiments. | **Data Scientists** and **ML Engineers** querying global index for top-ranked ML datasets. |
| **Kaggle** | The premier platform for data science competitions and massive, diverse machine learning datasets. | **Data Scientists** and **ML Engineers** training machine learning models or testing predictive algorithms. |
| **SEC (EDGAR)** | The US Securities and Exchange Commission database, essential for fetching raw corporate financial filings (10-K, 10-Q) and deep market analysis data. | **Data Analysts** and **Business Analysts** performing financial modeling or market analysis. |
| **FRED** | Federal Reserve Economic Data, the ultimate source for macroeconomic time-series data, socioeconomic metrics, and financial health indicators. | **Data Engineers** building macro-level data warehouses and researchers running global analyses. |

### 🤖 Antigravity's Architectural Assessment

*An objective architectural evaluation of the `data-fetcher-pipeline`.*

**Hybrid Installation Viability:** 
Deploying both a native Python package (`pip install .`) and a bash-wrapper (`run_pipeline.sh`) introduces a dual-maintenance burden. Shell scripts often suffer POSIX incompatibility on Windows environments, while `pip install` forces users to manage virtual environments to avoid globally scoped dependency conflicts. However, this architectural duality significantly maximizes cross-platform accessibility. Python-native deployments empower robust CLI toolchains (`data-fetcher`), whereas bash entrypoints gracefully handle rapid interactive wizarding. The trade-off is heavily weighted toward UX superiority at the cost of codebase redundancy.

**Data Source Efficacy:**
The selected data architectures are fundamentally robust and deeply relevant for modern data engineering. 
- **OpenML** and **Kaggle** successfully accommodate highly specific, heavily dimensional payloads required for advanced predictive modeling.
- **SEC (EDGAR)** provides unfiltered statutory records, requiring high parsing complexity but yielding unmatched institutional alpha.
- **FRED** supplies rigorous, standardized macroeconomic telemetry.
Collectively, these pipelines construct a formidable, institutionally viable data lake generation tool. Enforcing rigid "Zero-Imputation" rules across these endpoints proves that this repository is built strictly for enterprise-level manipulation, offloading data cleansing duties accurately back to the analytics layer.
