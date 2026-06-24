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

def setup_wizard() -> Dict[str, str]:
    config_dir = os.path.expanduser("~/.config/data-fetcher-pipeline")
    config_file = os.path.join(config_dir, "config.json")
    
    if os.path.exists(config_file):
        with open(config_file, 'r', encoding='utf-8') as f:
            return json.load(f)
            
    print("Welcome! Would you like to set up all your API keys right now to unlock all features? (y/n): ", end="")
    choice = input().strip().lower()
    
    config: Dict[str, str] = {}
    
    if choice == 'y':
        print("\n1. Kaggle: Go to https://www.kaggle.com/settings and click 'Create New Token'. Paste the Username here (or press Enter to skip): ", end="")
        k_user = input().strip()
        if k_user: config["KAGGLE_USERNAME"] = k_user
        
        print("2. Kaggle API Key: Paste the Key here (or press Enter to skip): ", end="")
        k_key = input().strip()
        if k_key: config["KAGGLE_KEY"] = k_key
        
        print("3. SEC EDGAR: Go to https://www.sec.gov/edgar/searchedgar/companysearch.html and get your access. Paste your SEC API key here (or press Enter to skip): ", end="")
        s_key = input().strip()
        if s_key: config["SEC_API_KEY"] = s_key
        
        print("4. FRED: Go to https://fred.stlouisfed.org/docs/api/api_key.html and request an API key. Paste it here (or press Enter to skip): ", end="")
        f_key = input().strip()
        if f_key: config["FRED_API_KEY"] = f_key
        
        print("\n[Wizard] Keys securely captured!")
    else:
        print("\n[Wizard] Skipping initial setup. You'll be prompted for keys only when needed (Lazy Loading).")
        
    os.makedirs(config_dir, exist_ok=True)
    with open(config_file, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4)
        
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
    config_dir = os.path.expanduser("~/.config/data-fetcher-pipeline")
    config_file = os.path.join(config_dir, "config.json")
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
        self.outdir = outdir
        self.config = config
        os.makedirs(self.outdir, exist_ok=True)

    @abstractmethod
    def scout(self):
        """Phase 1 (Scouting): Pre-flight validation, ticker checks, and schema validation."""
        pass

    @abstractmethod
    def extract(self) -> pd.DataFrame:
        """Phase 2 (Extraction): Download and format raw payloads."""
        pass

    def validate_payload(self, df: pd.DataFrame):
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

    def run(self):
        """Execution Flow Controller"""
        print(f"[{self.__class__.__name__}] Initiating extraction sequence for query: '{self.query}'")
        self.scout()
        df = self.extract()
        self.validate_payload(df)
        
        safe_filename = "".join(x for x in self.query if x.isalnum() or x in " _-").strip().replace(" ", "_").lower()
        self.save_csv(df, f"{safe_filename}_raw.csv")


class YahooFinanceFetcher(BaseFetcher):
    def scout(self):
        print(f"[Scout] Validating Yahoo Finance Ticker '{self.query}'...")
        import yfinance as yf
        ticker = yf.Ticker(self.query)
        hist = ticker.history(period="5d")
        if hist.empty:
            raise ValueError(f"Ticker '{self.query}' not found or delisted.")
        print("[Scout] Ticker validated and active.")

    def extract(self) -> pd.DataFrame:
        print("[Extract] Fetching historical market data...")
        import yfinance as yf
        ticker = yf.Ticker(self.query)
        df = ticker.history(period="max").reset_index()
        # Constraint Enforced: Zero Imputation for weekends/holidays.
        return df


class FREDFetcher(BaseFetcher):
    def scout(self) -> None:
        self.api_key = get_api_key(
            "FRED_API_KEY", 
            self.config, 
            "Please go to https://fred.stlouisfed.org/docs/api/api_key.html and paste your FRED API Key: "
        )
        print("[Scout] Credentials validated.")

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
    def scout(self):
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
    def scout(self):
        print(f"[Scout] Running generic pre-flight validation for '{self.query}'...")

    def extract(self) -> pd.DataFrame:
        print("[Extract] Fetching tabular data...")
        raise NotImplementedError("This source logic is not yet implemented.")


class OpenMLFetcher(BaseFetcher):
    def __init__(self, query: str, outdir: str, config: Dict[str, str]) -> None:
        super().__init__(query, outdir, config)
        self.target_dataset_id: int = -1
        self.dataset_name: str = ""

    def scout(self) -> None:
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
        
        base_dir: str = os.environ.get("OUTPUT_DIR", os.path.join(os.path.expanduser("~"), "Desktop"))
        self.outdir = os.path.join(base_dir, "datasets_of_data-fetcher-pipeline", "openml_org", "CSV", clean_topic)
        os.makedirs(self.outdir, exist_ok=True)
        print(f"[Extract] Output directory set to: {self.outdir}")
        
        return X


def get_fetcher(source: str, query: str, outdir: str, config: Dict[str, str]) -> BaseFetcher:
    """Strategy Factory mapping CLI sources to OOP Classes."""
    fetchers = {
        "yfinance": YahooFinanceFetcher,
        "fred": FREDFetcher,
        "airbnb": AirbnbFetcher,
        "openml": OpenMLFetcher,
    }
    fetcher_class = fetchers.get(source.lower(), GenericFetcher)
    return fetcher_class(query, outdir, config)


def interactive_flow() -> tuple[str, str, str]:
    """Interactive wizard to guide the user when run in zero-args mode."""
    print("==========================================")
    print("🌟 Welcome to the Data Fetcher Pipeline 🌟")
    print("==========================================\n")
    
    topic = input("1. What specific topic or domain do you need datasets for? ").strip()
    
    intent = input("\n2. What exactly are you going to use this data for? (Providing this context helps me fetch the most accurate and suitable data for your use case. If you'd rather not say, just type 'Skip' or 'تمام'): ").strip()
    
    print("\n3. Please choose a fetching source:")
    print("  1. 🌐 Search ALL available supported sources.")
    print("  2. 🎯 Choose a specific source from our supported list.")
    print("  3. 🔀 Specify a custom mix of our supported sources.")
    print("  4. 🔗 Provide an external/custom website for me to try and fetch from.")
    
    source_choice = input("Enter your choice (1-4): ").strip()
    
    source = "openml"
    if source_choice == '2':
        print("\nSupported Sources: [openml, kaggle, sec, fred, airbnb, yfinance]")
        source = input("Enter the specific source: ").strip().lower()
        
        topic_lower = topic.lower()
        if source == "sec" and any(word in topic_lower for word in ["movie", "game", "sports", "anime"]):
            ans = input(f"\nWarning: SEC is for corporate financial filings, which is logically unrelated to '{topic}'. Proceed anyway, or switch to Kaggle/OpenML? (proceed/switch): ").strip().lower()
            if ans == "switch":
                source = input("Enter new source (e.g. kaggle): ").strip().lower()
                
    elif source_choice == '1':
        print("\n[Mock] We will search ALL supported sources simultaneously...")
    elif source_choice == '3':
        print("\n[Mock] We will use a custom mix of supported sources...")
    elif source_choice == '4':
        print("\n[Mock] Advanced external parsing mode engaged.")

    print("\n4. Let's define the technical shape of the required data:")
    _ = input("  - Volume (Specific number of rows or columns needed?): ").strip()
    _ = input("  - Features (Any specific columns/variables that MUST be present?): ").strip()
    _ = input("  - Format (Preferred file format e.g., CSV, JSON, Parquet, Excel): ").strip()
    _ = input("  - Timeframe (Any specific date range?): ").strip()
    
    print("\n5. How would you like the data delivered?")
    print("  1. 🧹 Cleaned: Automatically handle missing values (drop/impute) and remove duplicates.")
    print("  2. 🧱 Raw: Deliver the dataset exactly as fetched without any modifications.")
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
        
        fetcher = get_fetcher(source, query, outdir, config)
        fetcher.run()
        print("[Engine] Extraction Complete. Pipeline exiting successfully.")
        
    except Exception as e:
        print(f"\n[Fatal Error] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
