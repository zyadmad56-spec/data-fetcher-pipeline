import importlib
from typing import TYPE_CHECKING, Dict, Tuple

from scripts.errors import DataFetchError

if TYPE_CHECKING:
    from scripts.base import BaseFetcher

# Per-source discovery metadata for --list-sources (the agent-facing contract):
# what a query means, an example, and whether credentials are needed.
SOURCE_INFO: Dict[str, Dict[str, str]] = {
    "yahoo":      {"platform": "Yahoo Finance", "query_format": "ticker symbol", "example": "AAPL", "auth": "none"},
    "yfinance":   {"platform": "Yahoo Finance", "query_format": "ticker symbol", "example": "AAPL", "auth": "none"},
    "fred":       {"platform": "Federal Reserve Economic Data", "query_format": "series ID", "example": "CPIAUCSL", "auth": "FRED_API_KEY"},
    "sec":        {"platform": "SEC EDGAR XBRL", "query_format": "ticker symbol", "example": "AAPL", "auth": "SEC_API_KEY"},
    "worldbank":  {"platform": "World Bank Indicators", "query_format": "indicator code", "example": "NY.GDP.MKTP.CD", "auth": "none"},
    "eurostat":   {"platform": "Eurostat Bulk TSV", "query_format": "dataset code", "example": "nama_10_gdp", "auth": "none"},
    "datagov":    {"platform": "Data.gov Catalog API v4", "query_format": "free text", "example": "climate", "auth": "optional (DATAGOV_API_KEY, DEMO_KEY fallback)"},
    "github":     {"platform": "GitHub repositories", "query_format": "free text", "example": "covid", "auth": "recommended (GITHUB_TOKEN or gh CLI)"},
    "kaggle":     {"platform": "Kaggle Datasets", "query_format": "owner/dataset-slug", "example": "uciml/iris", "auth": "KAGGLE_USERNAME + KAGGLE_KEY"},
    "openml":     {"platform": "OpenML", "query_format": "name fragment or numeric dataset ID", "example": "iris", "auth": "none"},
    "airbnb":     {"platform": "Inside Airbnb", "query_format": "city name", "example": "amsterdam", "auth": "none"},
    "coingecko":  {"platform": "CoinGecko Crypto Markets", "query_format": "coin id or symbol", "example": "bitcoin", "auth": "none (365-day history cap without key)"},
}

# Lazy provider registry: CLI key -> (module path, class name). Modules are
# imported on demand so a missing/broken optional dependency (e.g. yfinance)
# only affects its own source, never --list-sources, --convert, or others.
_LAZY_REGISTRY: Dict[str, Tuple[str, str]] = {
    "yfinance": ("scripts.fetchers.yahoo", "YahooFinanceFetcher"),
    "yahoo": ("scripts.fetchers.yahoo", "YahooFinanceFetcher"),
    "fred": ("scripts.fetchers.fred", "FREDFetcher"),
    "airbnb": ("scripts.fetchers.airbnb", "AirbnbFetcher"),
    "openml": ("scripts.fetchers.openml_fetcher", "OpenMLFetcher"),
    "kaggle": ("scripts.fetchers.kaggle_fetcher", "KaggleFetcher"),
    "sec": ("scripts.fetchers.sec", "SECFetcher"),
    "worldbank": ("scripts.fetchers.worldbank", "WorldBankFetcher"),
    "github": ("scripts.fetchers.github_data", "GitHubDataFetcher"),
    "datagov": ("scripts.fetchers.datagov", "DataGovFetcher"),
    "eurostat": ("scripts.fetchers.eurostat", "EurostatFetcher"),
    "coingecko": ("scripts.fetchers.coingecko", "CoinGeckoFetcher"),
}

def get_fetcher(source: str, query: str, outdir: str, config: Dict[str, str]) -> "BaseFetcher":
    """Resolve a source name to its fetcher class (lazy import) and instantiate it."""
    spec = _LAZY_REGISTRY.get(source.lower())
    if spec is None:
        # Unknown sources fall through to the dynamic GenericFetcher (custom dirs)
        from scripts.fetchers.generic import GenericFetcher
        return GenericFetcher(query, outdir, config, source=source)

    module_path, class_name = spec
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise DataFetchError(
            f"Failed to load fetcher for '{source}' ({module_path}): {exc}. "
            "Install the dependencies listed in pyproject.toml.",
            code="PROVIDER_UNAVAILABLE",
            exit_code=3,
        ) from exc
    return getattr(module, class_name)(query, outdir, config)

def list_sources() -> list:
    """Return all registered source names."""
    return sorted(_LAZY_REGISTRY.keys())
