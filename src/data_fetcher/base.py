import os
import re
from abc import ABC, abstractmethod
from typing import Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

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
    def extract(self) -> 'pd.DataFrame':
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

    def validate_payload(self, df: 'pd.DataFrame') -> None:
        """Universal Payload Validator (Null-Density Check)"""
        if df.empty:
            raise ValueError("Graceful Fallback Triggered: Dataframe is completely empty.")
        
        null_ratio = df.isnull().sum().sum() / df.size
        if null_ratio > 0.8:
            raise ValueError(f"Graceful Fallback Triggered: Null density exceeds 80% ({null_ratio:.2%}).")
        
        print("[Validator] Payload passed Null-Density Check.")

    def save_csv(self, df: 'pd.DataFrame', filename: str) -> None:
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
        
        if metadata is not None:
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


