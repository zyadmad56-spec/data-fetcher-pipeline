"""Built-in source metadata shared by discovery and registration."""
from typing import Dict

# Per-source discovery metadata for --list-sources (the agent-facing contract):
# what a query means, an example, and whether credentials are needed.
SOURCE_INFO: Dict[str, Dict[str, str]] = {
    "yahoo":      {"platform": "Yahoo Finance", "query_format": "ticker symbol", "example": "AAPL", "auth": "none"},
    "yfinance":   {"platform": "Yahoo Finance", "query_format": "ticker symbol", "example": "AAPL", "auth": "none"},
    "fred":       {"platform": "Federal Reserve Economic Data", "query_format": "series ID", "example": "CPIAUCSL", "auth": "optional (public graph CSV without key)"},
    "sec":        {"platform": "SEC EDGAR XBRL", "query_format": "ticker symbol", "example": "AAPL", "auth": "contact email in SEC_API_KEY"},
    "worldbank":  {"platform": "World Bank Indicators", "query_format": "indicator code", "example": "NY.GDP.MKTP.CD", "auth": "none"},
    "eurostat":   {"platform": "Eurostat Bulk TSV", "query_format": "dataset code", "example": "nama_10_gdp", "auth": "none"},
    "datagov":    {"platform": "Data.gov Catalog API v4", "query_format": "free text", "example": "climate", "auth": "optional (DATAGOV_API_KEY, DEMO_KEY fallback)"},
    "github":     {"platform": "GitHub repositories", "query_format": "free text", "example": "covid", "auth": "recommended (GITHUB_TOKEN or gh CLI)"},
    "kaggle":     {"platform": "Kaggle Datasets", "query_format": "owner/dataset-slug", "example": "uciml/iris", "auth": "KAGGLE_USERNAME + KAGGLE_KEY"},
    "openml":     {"platform": "OpenML", "query_format": "name fragment or numeric dataset ID", "example": "iris", "auth": "none"},
    "airbnb":     {"platform": "Inside Airbnb", "query_format": "city name", "example": "amsterdam", "auth": "none"},
    "coingecko":  {"platform": "CoinGecko Crypto Markets", "query_format": "coin id or symbol", "example": "bitcoin", "auth": "none (365-day history cap without key)"},
}
