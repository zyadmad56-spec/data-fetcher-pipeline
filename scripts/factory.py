import importlib
from typing import TYPE_CHECKING, Dict, Tuple

from scripts.errors import DataFetchError
from scripts.source_catalog import SOURCE_INFO  # re-exported for existing callers

if TYPE_CHECKING:
    from scripts.base import BaseFetcher


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
        if source.lower() == 'custom':
            from scripts.custom_source import CustomFetcher
            return CustomFetcher(query, outdir, config)
        from scripts.source_registry import load_sources
        descriptor = load_sources().get(source.lower())
        if descriptor:
            from scripts.custom_source import CustomFetcher
            return CustomFetcher(query, outdir, config, source=source.lower(), descriptor=descriptor)
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
    return sorted(source_info())


def source_info() -> dict:
    from scripts.source_registry import load_sources
    info = {k: dict(v) for k, v in SOURCE_INFO.items()}
    for key, descriptor in load_sources().items():
        info[key] = {'platform': descriptor['name'], 'query_format': 'public URL on registered host',
                     'example': descriptor['url'], 'auth': 'none', 'saved': True}
    return info
