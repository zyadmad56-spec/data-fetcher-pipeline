# 🚀 Data Fetcher Pipeline

**The Ultimate Time Saver for Data Science & ML.**

Welcome to the smartest, most user-friendly data fetching pipeline ever built. We designed this to give you back your most valuable asset: **time**. Say goodbye to manual searching, endless clicking, messy downloads, and writing boilerplate extraction code.

The Data Fetcher Pipeline handles everything from guided setup to beautifully organized workspaces, so you can focus entirely on what you do best: analyzing data and building models.

## ✨ Why You Will Love This

* **The Ultimate Time Saver:** We've completely eliminated the tedious hours of downloading and organizing data. The pipeline automatically searches, extracts, and organizes data directly for you.
* **Purity of Data (Raw & Untampered):** Data integrity is everything. The tool fetches your data exactly as it is from the source—100% RAW—without any hidden manipulations, imputations, or alterations. You always know exactly what you are starting with.
* **Format Flexibility & Conversion:** Download data in your preferred formats. The pipeline handles all necessary format conversions seamlessly, ensuring your data is ready to use immediately.
* **Automated Data Dictionaries:** Never wonder what a column means again! We automatically generate a highly descriptive explanation file (data dictionary) alongside every downloaded dataset.
* **Smart Desktop Organization:** A cluttered Desktop is a cluttered mind. Our pipeline takes over file management and creates an incredibly clean, structured directory natively on your machine.

## 🛠️ The All-in-One Setup Wizard

We've completely overhauled our onboarding experience. No more manually editing hidden `.env` files. 

Upon your first run, our **Smart Interactive Setup Wizard** will ask if you'd like to configure your API keys. You can set them all up at once or use our **Lazy Loading** feature to enter them only when you need them. All credentials are saved securely in an OS-standard JSON config file (`~/.config/data-fetcher-pipeline/config.json`) keeping your system absolutely safe.

## 📂 Visualizing the Magic

Curious where your files go? Here is exactly how elegantly the pipeline organizes your data directly on your Desktop. No more "Downloads" folder chaos!

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

## 🚀 How to Use It

Just ask your agent for data!

```text
Use the data-fetcher-pipeline to get the latest COVID-19 dataset from WHO.
Use the data-fetcher-pipeline to download the SEC EDGAR 10-K filings for AAPL.
Fetch the housing prices dataset from Kaggle using the data-fetcher-pipeline.
```

### 🧠 OpenML Intelligent Search
Looking for something specific but don't know the dataset name? Just run the interactive `run_pipeline.sh` script, enter your search query (like "finance"), and our intelligent OpenML integration will find the top-ranked dataset and pull it down instantly!

## ⚙️ Installation

Browse the package:
```bash
npx skills add zyadmad56-spec/data-fetcher-pipeline --list
```

Install globally:
```bash
npx skills add zyadmad56-spec/data-fetcher-pipeline --global
```
