from __future__ import annotations
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    import pandas as pd

import os
import tempfile

import requests
import requests.exceptions

from ..base import BaseFetcher
from ..config import DEFAULT_REQUEST_TIMEOUT_SECONDS

class AirbnbFetcher(BaseFetcher):
    def scout(self) -> dict:
        print(f"[Scout] Initiating HTML Scouting & Progressive Resiliency Protocol for '{self.query}'...")
        from bs4 import BeautifulSoup
        
        url = "http://insideairbnb.com/get-the-data/"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        
        try:
            response = requests.get(url, headers=headers, timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS)
        except requests.exceptions.Timeout as exc:
            raise RuntimeError(f"Airbnb scout request timed out after {DEFAULT_REQUEST_TIMEOUT_SECONDS}s") from exc
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
        print("[Extract] Downloading & safely decompressing .gz payload...")
        
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        try:
            response = requests.get(self.download_url, headers=headers, stream=True, timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS)
        except requests.exceptions.Timeout as exc:
            raise RuntimeError(f"Airbnb extract request timed out after {DEFAULT_REQUEST_TIMEOUT_SECONDS}s") from exc
        if response.status_code != 200:
            raise ValueError(f"Failed to download payload from {self.download_url}: {response.status_code}")
            
        with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
            temp_path = tmp_file.name
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    tmp_file.write(chunk)
                    
        try:
            df = pd.read_csv(temp_path, compression='gzip', low_memory=False)
        finally:
            os.remove(temp_path)
            
        return df


