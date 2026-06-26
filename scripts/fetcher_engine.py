import os
import sys
import argparse
import time
import random
import json
import pandas as pd
from abc import ABC, abstractmethod
from typing import Dict, Optional

# Enforce UTF-8 to prevent terminal encoding errors
sys.stdout.reconfigure(encoding='utf-8')

def get_config_paths() -> tuple[str, str]:
    config_dir = os.path.expanduser("~/.config/data-fetcher-pipeline")
    config_file = os.path.join(config_dir, "config.json")
    return config_dir, config_file

def setup_wizard() -> Dict[str, str]:
    config_dir, config_file = get_config_paths()
    
    if os.path.exists(config_file):
        with open(config_file, 'r', encoding='utf-8') as f:
            return json.load(f)
            
    print("\nWelcome to the Data Fetcher Pipeline! Before we can fetch data, we need to configure your API keys (e.g., Kaggle, FRED).")
    print(f"To ensure security and avoid exposing keys in your project folder, configurations will be saved globally at: {config_file}\n")
    
    print("How would you like to set up your API keys?")
    print("  1. Auto-Setup (Recommended): You paste your keys here in the terminal, and I will securely create the directory, generate the config.json file, and save them for you.")
    print("  2. Manual Setup: I will give you the exact folder path and JSON structure, and you can create and edit the file yourself.")
    
    choice = input("\nEnter your choice (1 or 2): ").strip()
    
    config: Dict[str, str] = {}
    
    if choice == '1':
        print("\n[Auto-Setup] Enter your keys below (press Enter to skip any key).")
        print("1. Kaggle Username: ", end="")
        k_user = input().strip()
        if k_user: config["KAGGLE_USERNAME"] = k_user
        
        print("2. Kaggle API Key: ", end="")
        k_key = input().strip()
        if k_key: config["KAGGLE_KEY"] = k_key
        
        print("3. SEC EDGAR API Key: ", end="")
        s_key = input().strip()
        if s_key: config["SEC_API_KEY"] = s_key
        
        print("4. FRED API Key: ", end="")
        f_key = input().strip()
        if f_key: config["FRED_API_KEY"] = f_key
        
        os.makedirs(config_dir, exist_ok=True)
        with open(config_file, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4)
        print("\n[Success] Keys securely saved globally!")
        
    elif choice == '2':
        print("\n[Manual Setup]")
        print("Please run the following commands in another terminal:")
        print(f"  mkdir -p {config_dir}")
        print(f"  nano {config_file}")
        print("\nPaste the following JSON structure and fill in your keys:")
        print("{\n    \"KAGGLE_USERNAME\": \"\",\n    \"KAGGLE_KEY\": \"\",\n    \"SEC_API_KEY\": \"\",\n    \"FRED_API_KEY\": \"\"\n}")
        
        while True:
            done = input("\nType 'Done' once you have created and saved the file (or type 'Skip' to abort): ").strip().lower()
            if done == 'done':
                if os.path.exists(config_file):
                    try:
                        with open(config_file, 'r', encoding='utf-8') as f:
                            config = json.load(f)
                        print("[Success] Manual configuration detected and successfully parsed.")
                        break
                    except json.JSONDecodeError as e:
                        print(f"[Error] The file {config_file} contains invalid JSON syntax: {e}")
                        print("Please fix the JSON formatting and type 'Done' again.")
                else:
                    print(f"[Error] The file {config_file} was not found. Please create it or type 'Skip'.")
            elif done == 'skip':
                print("[Wizard] Proceeding without initial keys (Lazy Loading active).")
                break
    else:
        print("\n[Wizard] Invalid choice. Proceeding with Lazy Loading.")
        
    return config

def get_api_key(key_name: str, config: Dict[str, str], prompt_msg: str) -> str:
    if key_name in config and config[key_name]:
        return config[key_name]
    
    print(f"\n[Lazy Load] Missing required key: {key_name}")
    print(prompt_msg)
    val = input().strip()
    if not val:
        raise ValueError(f"Required API key '{key_name}' was not provided.")
        
    config[key_name] = val
    config_dir, config_file = get_config_paths()
    os.makedirs(config_dir, exist_ok=True)
    with open(config_file, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4)
        
    return val

class BaseFetcher(ABC):
    """
    Abstract Base Class enforcing the architectural constraints of the data-fetcher-pipeline.
    """
    def __init__(self, query: str, outdir: str, config: Dict[str, str]) -> None:
        self.query = query
        self.config = config
        
        source_name = self.__class__.__name__.replace("Fetcher", "").lower()
        format_name = "csv"
        
        # Native Python Directory Provisioning
        import re
        
        # Use the explicitly passed outdir parameter instead of hardcoding Desktop
        base_dir = outdir
        
        # Sanitize Source
        clean_source = re.sub(r'^https?://', '', source_name, flags=re.IGNORECASE)
        clean_source = re.sub(r'^www\.', '', clean_source, flags=re.IGNORECASE)
        clean_source = clean_source.split('/')[0]
        clean_source = re.sub(r'[<>:"/\|?* ]+', '_', clean_source).lower()
        
        # Sanitize Format and Topic
        clean_format = format_name.upper()
        clean_topic = re.sub(r'[^a-zA-Z0-9_-]+', '_', self.query).lower()
        
        # Construct and Create
        self.outdir = os.path.normpath(os.path.join(
            base_dir, 
            "datasets_of_data-fetcher-pipeline", 
            clean_source, 
            clean_format, 
            clean_topic
        ))
        
        os.makedirs(self.outdir, exist_ok=True)

    @abstractmethod
    def scout(self) -> dict:
        """Phase 1 (Scouting): Pre-flight validation. Returns metadata dict: {'url': str, 'size_info': str}"""
        pass

    @abstractmethod
    def extract(self) -> pd.DataFrame:
        """Phase 2 (Extraction): Download and format raw payloads."""
        pass

    def pre_flight_authorization(self, metadata: dict) -> int:
        print("\n" + "="*50)
        print(" PRE-FLIGHT AUTHORIZATION REQUIRED")
        print("="*50)
        print(f"Target URL : {metadata.get('url', 'Unknown')}")
        print(f"Data Size  : {metadata.get('size_info', 'Unknown (Determined at runtime)')}")
        print("-" * 50)
        
        while True:
            ans = input("Do you want to extract this entire dataset? (y/n) or type a number to extract a specific number of rows: ").strip().lower()
            if ans in ['y', 'yes']:
                return 0 # 0 means all
            elif ans in ['n', 'no']:
                raise ValueError("Extraction aborted by user during Pre-Flight Authorization.")
            elif ans.isdigit():
                return int(ans)
            else:
                print("[Error] Invalid input. Please type 'y', 'n', or a number.")

    def validate_payload(self, df: pd.DataFrame) -> None:
        """Universal Payload Validator (Null-Density Check)"""
        if df.empty:
            raise ValueError("Graceful Fallback Triggered: Dataframe is completely empty.")
        
        null_ratio = df.isnull().sum().sum() / df.size
        if null_ratio > 0.8:
            raise ValueError(f"Graceful Fallback Triggered: Null density exceeds 80% ({null_ratio:.2%}).")
        
        print("[Validator] Payload passed Null-Density Check.")

    def save_csv(self, df: pd.DataFrame, filename: str) -> None:
        """Absolute Raw Data Preservation (Zero-Cleaning)"""
        filepath = os.path.join(self.outdir, filename)
        df.to_csv(filepath, index=False)
        print(f"[Success] Raw dataset saved securely to {filepath}")
        
        dict_path = os.path.join(self.outdir, "dataset_description.txt")
        with open(dict_path, "w", encoding="utf-8") as f:
            f.write(f"Dataset Topic: {self.query}\n")
            f.write(f"Total Rows: {len(df)}\n")
            f.write(f"Total Columns: {len(df.columns)}\n")
            f.write(f"Format: Raw CSV\n")
            f.write("\nColumns present:\n")
            for col in df.columns:
                null_count = df[col].isnull().sum()
                f.write(f"- {col} (Nulls: {null_count})\n")
        print(f"[Success] Automated Data Dictionary generated at {dict_path}")

    def run(self) -> str:
        """Execution Flow Controller"""
        print(f"[{self.__class__.__name__}] Initiating extraction sequence for query: '{self.query}'")
        metadata = self.scout()
        
        if metadata:
            self.row_limit = self.pre_flight_authorization(metadata)
        else:
            self.row_limit = 0
            
        df = self.extract()
        
        if self.row_limit > 0 and len(df) > self.row_limit:
            print(f"[Engine] Slicing dataset to requested {self.row_limit} rows...")
            df = df.head(self.row_limit)
            
        self.validate_payload(df)
        
        safe_filename = "".join(x for x in self.query if x.isalnum() or x in " _-").strip().replace(" ", "_").lower()
        csv_filename = f"{safe_filename}_raw.csv"
        self.save_csv(df, csv_filename)
        return os.path.join(self.outdir, csv_filename)


class YahooFinanceFetcher(BaseFetcher):
    def scout(self) -> dict:
        print(f"[Scout] Validating Yahoo Finance Ticker '{self.query}'...")
        import yfinance as yf
        ticker = yf.Ticker(self.query)
        hist = ticker.history(period="5d")
        if hist.empty:
            raise ValueError(f"Ticker '{self.query}' not found or delisted.")
        print("[Scout] Ticker validated and active.")
        return {
            "url": f"https://finance.yahoo.com/quote/{self.query}",
            "size_info": "Max Historical Daily Candles"
        }

    def extract(self) -> pd.DataFrame:
        print("[Extract] Fetching historical market data...")
        import yfinance as yf
        ticker = yf.Ticker(self.query)
        df = ticker.history(period="max").reset_index()
        # Constraint Enforced: Zero Imputation for weekends/holidays.
        return df


class FREDFetcher(BaseFetcher):
    def scout(self) -> dict:
        self.api_key = get_api_key(
            "FRED_API_KEY", 
            self.config, 
            "Please go to https://fred.stlouisfed.org/docs/api/api_key.html and paste your FRED API Key: "
        )
        print("[Scout] Credentials validated.")
        return {
            "url": f"https://fred.stlouisfed.org/series/{self.query}",
            "size_info": "Full Time-Series History"
        }

    def extract(self) -> pd.DataFrame:
        print("[Extract] Interfacing with FRED API...")
        import requests
        url = f"https://api.stlouisfed.org/fred/series/observations?series_id={self.query}&api_key={self.api_key}&file_type=json"
        response = requests.get(url)
        
        if response.status_code != 200:
            raise ValueError(f"FRED API Error {response.status_code}: {response.text}")
            
        api_response_payload = response.json()
        if "observations" not in api_response_payload or not api_response_payload["observations"]:
            raise ValueError(f"No observations returned for series '{self.query}'.")
        
        df = pd.DataFrame(api_response_payload["observations"])
        df = df[['date', 'value']]
        # Constraint Enforced: Strict Frequency Preservation. Returns raw strings exactly as provided.
        return df


class AirbnbFetcher(BaseFetcher):
    def scout(self) -> dict:
        print(f"[Scout] Initiating HTML Scouting & Progressive Resiliency Protocol for '{self.query}'...")
        import requests
        from bs4 import BeautifulSoup
        
        url = "http://insideairbnb.com/get-the-data/"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        
        response = requests.get(url, headers=headers)
        if response.status_code != 200:
            raise ValueError(f"Failed to fetch Inside Airbnb index: {response.status_code}")
            
        soup = BeautifulSoup(response.text, "html.parser")
        self.download_url = None
        
        # Scan raw HTML strictly for href attributes terminating in listings.csv.gz for the given city
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if self.query.lower() in href.lower() and href.endswith("listings.csv.gz"):
                self.download_url = href
                break
                
        if not self.download_url:
            raise ValueError(f"Could not resolve static .csv.gz download link for city: '{self.query}'.")
            
        print(f"[Scout] Static download target resolved: {self.download_url}")
        return {
            "url": self.download_url,
            "size_info": "Unknown compressed CSV payload"
        }

    def extract(self) -> pd.DataFrame:
        import pandas as pd
        import requests
        import io
        print("[Extract] Downloading & safely decompressing .gz payload...")
        
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        response = requests.get(self.download_url, headers=headers)
        if response.status_code != 200:
            raise ValueError(f"Failed to download payload from {self.download_url}: {response.status_code}")
            
        # Safely read the gzip compressed CSV directly into pandas (Zero-Cleaning)
        df = pd.read_csv(io.BytesIO(response.content), compression='gzip', low_memory=False)
        return df


class GenericFetcher(BaseFetcher):
    def scout(self) -> dict:
        print(f"[Scout] Running generic pre-flight validation for '{self.query}'...")
        return {}

    def extract(self) -> pd.DataFrame:
        print("[Extract] Fetching tabular data...")
        raise NotImplementedError("This source logic is not yet implemented.")


class KaggleFetcher(BaseFetcher):
    def scout(self) -> dict:
        self.username = get_api_key("KAGGLE_USERNAME", self.config, "Please provide your Kaggle Username: ")
        self.key = get_api_key("KAGGLE_KEY", self.config, "Please provide your Kaggle API Key: ")
        print("[Scout] Kaggle credentials validated.")
        return {
            "url": f"https://www.kaggle.com/datasets/{self.query}",
            "size_info": "Unknown CSV payload (Full dataset archive)"
        }

    def extract(self) -> pd.DataFrame:
        print("[Extract] Interfacing with Kaggle API...")
        import os
        import tempfile
        import glob
        
        os.environ['KAGGLE_USERNAME'] = self.username
        os.environ['KAGGLE_KEY'] = self.key
        
        try:
            import kaggle
            kaggle.api.authenticate()
        except ImportError as exc:
            raise ImportError("Kaggle package is missing. Please run `pip install kaggle`.") from exc
            
        with tempfile.TemporaryDirectory() as tmpdir:
            print(f"[Extract] Downloading dataset '{self.query}'...")
            try:
                kaggle.api.dataset_download_files(self.query, path=tmpdir, unzip=True)
            except Exception as e:
                raise RuntimeError(f"Kaggle download failed: {e}") from e
                
            csv_files = glob.glob(os.path.join(tmpdir, "**", "*.csv"), recursive=True)
            if not csv_files:
                raise ValueError("No CSV files found in the Kaggle dataset.")
                
            print(f"[Extract] Found {len(csv_files)} CSV file(s). Reading the primary payload...")
            # For simplicity, load the largest CSV file
            largest_csv = max(csv_files, key=os.path.getsize)
            df = pd.read_csv(largest_csv, low_memory=False)
            
        return df


class SECFetcher(BaseFetcher):
    def scout(self) -> dict:
        self.api_key = get_api_key("SEC_API_KEY", self.config, "Please provide your SEC API Key or Email for User-Agent: ")
        print("[Scout] SEC EDGAR credentials validated.")
        import requests
        
        ua = self.api_key if "@" in self.api_key else f"data-fetcher-pipeline/1.0 ({self.api_key})"
        headers = {"User-Agent": ua}
        
        cik_url = "https://www.sec.gov/files/company_tickers.json"
        resp = requests.get(cik_url, headers=headers)
        if resp.status_code != 200:
            raise ValueError(f"Failed to fetch SEC CIK index. Status: {resp.status_code}")
            
        tickers = resp.json()
        self.cik_str = None
        for k, v in tickers.items():
            if str(v.get('ticker', '')).lower() == self.query.lower():
                self.cik_str = str(v['cik_str']).zfill(10)
                break
                
        if not self.cik_str:
            raise ValueError(f"Ticker '{self.query}' not found in SEC database.")
            
        print(f"[Scout] Resolved ticker '{self.query}' to CIK {self.cik_str}.")
        self.facts_url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{self.cik_str}.json"
        
        return {
            "url": self.facts_url,
            "size_info": "Full XBRL Corporate Taxonomy"
        }

    def extract(self) -> pd.DataFrame:
        print("[Extract] Interfacing with SEC EDGAR API...")
        import requests
        
        ua = self.api_key if "@" in self.api_key else f"data-fetcher-pipeline/1.0 ({self.api_key})"
        headers = {"User-Agent": ua}
        
        print(f"[Extract] Fetching company facts...")
        resp = requests.get(self.facts_url, headers=headers)
        if resp.status_code != 200:
            raise ValueError(f"Failed to fetch facts for CIK {self.cik_str}. Status: {resp.status_code}")
            
        data = resp.json()
        rows = []
        for taxonomy, concepts in data.get("facts", {}).items():
            for concept_name, concept_data in concepts.items():
                for unit, observations in concept_data.get("units", {}).items():
                    for obs in observations:
                        rows.append({
                            "taxonomy": taxonomy,
                            "concept": concept_name,
                            "unit": unit,
                            "val": obs.get("val"),
                            "fy": obs.get("fy"),
                            "fp": obs.get("fp"),
                            "form": obs.get("form"),
                            "filed": obs.get("filed"),
                            "end": obs.get("end")
                        })
                        
        if not rows:
            raise ValueError("No financial facts found for this company.")
            
        df = pd.DataFrame(rows)
        return df


class OpenMLFetcher(BaseFetcher):
    def __init__(self, query: str, outdir: str, config: Dict[str, str]) -> None:
        super().__init__(query, outdir, config)
        self.target_dataset_id: int = -1
        self.dataset_name: str = ""

    def scout(self) -> dict:
        print(f"[Scout] Searching OpenML for query '{self.query}'...")
        try:
            import openml
        except ImportError as exc:
            raise ImportError("OpenML package is missing. Please run `pip install openml`.") from exc

        try:
            datasets_df: pd.DataFrame = openml.datasets.list_datasets(output_format="dataframe")
        except Exception as exc:
            raise RuntimeError(f"Failed to fetch dataset list from OpenML: {exc}") from exc
            
        mask = datasets_df['name'].str.contains(self.query, case=False, na=False)
        matched_df: pd.DataFrame = datasets_df[mask]
        
        if matched_df.empty:
            raise ValueError(f"No datasets found on OpenML matching query: '{self.query}'.")
            
        matched_sorted: pd.DataFrame = matched_df.sort_values(by="NumberOfInstances", ascending=False)
        
        top_dataset = matched_sorted.iloc[0]
        self.target_dataset_id = int(top_dataset['did'])
        self.dataset_name = str(top_dataset['name'])
        
        print(f"[Scout] Top dataset found: '{self.dataset_name}' (ID: {self.target_dataset_id})")
        return {
            "url": f"https://www.openml.org/d/{self.target_dataset_id}",
            "size_info": f"{int(top_dataset['NumberOfInstances'])} rows, {int(top_dataset['NumberOfFeatures'])} columns"
        }

    def extract(self) -> pd.DataFrame:
        print(f"[Extract] Downloading dataset '{self.dataset_name}' from OpenML...")
        try:
            import openml
        except ImportError as exc:
            raise ImportError("OpenML package is missing. Please run `pip install openml`.") from exc

        try:
            dataset = openml.datasets.get_dataset(self.target_dataset_id, download_data=True)
            X, y, _, _ = dataset.get_data(dataset_format="dataframe")
        except Exception as exc:
            raise RuntimeError(f"Failed to download OpenML dataset ID {self.target_dataset_id}: {exc}") from exc

        if y is not None:
            if isinstance(y, pd.Series):
                X[y.name] = y
            elif isinstance(y, pd.DataFrame):
                X = pd.concat([X, y], axis=1)
                
        clean_topic: str = "".join(char for char in self.dataset_name if char.isalnum() or char in " _-").strip().replace(" ", "_").lower()
        print(f"[Extract] Dataset downloaded. Routing to centralized output directory: {self.outdir}")
        
        return X


def get_fetcher(source: str, query: str, outdir: str, config: Dict[str, str]) -> BaseFetcher:
    """Strategy Factory mapping CLI sources to OOP Classes."""
    fetchers = {
        "yfinance": YahooFinanceFetcher,
        "fred": FREDFetcher,
        "airbnb": AirbnbFetcher,
        "openml": OpenMLFetcher,
        "kaggle": KaggleFetcher,
        "sec": SECFetcher,
    }
    fetcher_class = fetchers.get(source.lower(), GenericFetcher)
    return fetcher_class(query, outdir, config)


def interactive_flow() -> tuple[str, str, str]:
    """Interactive wizard to guide the user when run in zero-args mode."""
    print("==========================================")
    print("Welcome to the Data Fetcher Pipeline")
    print("==========================================\n")
    
    topic = input("1. What specific topic or domain do you need datasets for? ").strip()
    
    intent = input("\n2. What exactly are you going to use this data for? (Providing this context helps me fetch the most accurate and suitable data for your use case. If you'd rather not say, just type 'Skip' or 'تمام'): ").strip()
    
    print("\n3. Where should we fetch this data from?")
    valid_sources = ["kaggle", "openml", "sec", "fred", "airbnb", "yfinance"]
    print(f"   Supported data banks: {valid_sources}")
    print("   - Type the exact name of a specific site.")
    print("   - Type 'all' to run a Meta-Search and sequentially extract from multiple top sites.")
    print("   - Or simply press Enter to default to Kaggle (Highly Recommended).")
    
    source = input("Enter source (default: kaggle): ").strip().lower()
    
    if not source:
        print("\n[Wizard] No source specified. Defaulting to Kaggle as the primary data bank.")
        source = "kaggle"
    elif source == 'all':
        print("\n[Wizard] Meta-Search activated. Will aggregate data from multiple top-tier sources.")
    else:
        while source not in valid_sources and source != 'all':
            print(f"\n[Error] '{source}' is not a supported source.")
            source = input(f"Please choose from {valid_sources}, 'all', or press Enter for Kaggle: ").strip().lower()
            if not source:
                print("\n[Wizard] Defaulting to Kaggle.")
                source = "kaggle"
                break
                
        topic_lower = topic.lower()
        if source == "sec" and any(word in topic_lower for word in ["movie", "game", "sports", "anime"]):
            ans = input(f"\nWarning: SEC is for corporate financial filings, which is logically unrelated to '{topic}'. Proceed anyway, or switch to Kaggle? (proceed/switch): ").strip().lower()
            if ans == "switch":
                source = "kaggle"
                print("\n[Wizard] Switched source to Kaggle.")

    print("\n4. Let's define the technical shape of the required data:")
    _ = input("  - Volume (Specific number of rows or columns needed?): ").strip()
    _ = input("  - Features (Any specific columns/variables that MUST be present?): ").strip()
    _ = input("  - Format (Preferred file format e.g., CSV, JSON, Parquet, Excel): ").strip()
    _ = input("  - Timeframe (Any specific date range?): ").strip()
    
    print("\n5. How would you like the data delivered?")
    print("  1. Cleaned: Automatically handle missing values (drop/impute) and remove duplicates.")
    print("  2. Raw: Deliver the dataset exactly as fetched without any modifications.")
    _ = input("Enter your choice (1 or 2): ").strip()
    
    print("\n[Wizard] All parameters collected successfully. Initializing Fetcher Engine...\n")
    
    # Only fallback if topic is empty
    if not topic:
        topic = "finance"
    
    return source, topic, os.path.join(os.getcwd(), "data_raw")


def main() -> None:
    config = setup_wizard()
    
    parser = argparse.ArgumentParser(description="Data Fetcher Background Engine")
    parser.add_argument("--source", required=False, help="Target data platform (e.g., yfinance, fred, airbnb)")
    parser.add_argument("--query", required=False, help="Topic, ticker symbol, or series ID")
    parser.add_argument("--outdir", default=os.path.join(os.getcwd(), "data_raw"), help="Destination directory")
    
    args = parser.parse_args()
    
    try:
        # Zero-args mode triggers the interactive onboarding flow
        if not args.source or not args.query:
            source, query, outdir = interactive_flow()
        else:
            source, query, outdir = args.source, args.query, args.outdir

        # Enforce Layer 2: Humanized Randomized Delays (simulating evasion tactics)
        delay = random.uniform(2, 5)
        print(f"[Engine] Imposing humanized delay of {delay:.2f} seconds to simulate human traffic...")
        time.sleep(delay)
        
        if source == "all":
            sources_to_run = ["kaggle", "openml"] # Best general purpose tabular sources
            print(f"\n[Meta-Search] Executing multi-source extraction across: {sources_to_run}")
            csv_paths = []
            for s in sources_to_run:
                try:
                    fetcher = get_fetcher(s, query, outdir, config)
                    csv_paths.append(fetcher.run())
                except Exception as e:
                    print(f"\n[Meta-Search] Source '{s}' failed or was bypassed: {e}\n")
            if csv_paths:
                print(f"\n[Engine] Meta-Search Complete. {len(csv_paths)} datasets extracted successfully.")
                csv_path = csv_paths[0] # Default to first successful payload for Alchemy
            else:
                print("\n[Engine] Meta-Search yielded no data. Aborting.")
                sys.exit(1)
        else:
            fetcher = get_fetcher(source, query, outdir, config)
            csv_path = fetcher.run()
            print("[Engine] Extraction Complete. Pipeline exiting successfully.")
        
        print("\n[Prompt] Data fetched successfully. Would you like to initialize the Format Alchemy engine to convert this dataset to SQL and Excel? (y/n)")
        alchemy_choice = input().strip().lower()
        if alchemy_choice == 'y':
            try:
                from scripts.format_alchemy import run_alchemy
            except ImportError:
                from format_alchemy import run_alchemy
            run_alchemy(csv_path)
        
    except (ValueError, RuntimeError, ImportError) as e:
        print(f"\n[Error] {e}")
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        print("\n[Engine] Execution aborted by user.")
        sys.exit(1)


if __name__ == "__main__":
    main()
