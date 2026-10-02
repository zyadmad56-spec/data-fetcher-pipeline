"""Small terminal interview; agents follow the same contract in SKILL.md."""
from scripts.core.models import FetchRequest
from pathlib import Path

from scripts.errors import DataFetchError


def _required(prompt: str) -> str:
    while True:
        value = input(prompt).strip()
        if value:
            return value
        print("Please enter a value.")


def choose_source() -> str:
    from scripts.factory import list_sources, source_info
    sources, info = list_sources(), source_info()
    for i, key in enumerate(sources, 1):
        print(f"  {i}. {info[key]['platform']} ({key})")
    while True:
        choice = input("Choose a source number or name: ").strip().lower()
        if choice.isdigit() and 1 <= int(choice) <= len(sources):
            return sources[int(choice) - 1]
        aliases = {v['platform'].lower(): k for k, v in info.items()}
        aliases['kaggel'] = 'kaggle'
        choice = aliases.get(choice, choice)
        if choice in sources:
            return choice
        print("Choose one of the listed sources.")


def relevance_warning(source: str, goal: str) -> str:
    """Advisory only: a domain can overlap another (e.g. aircraft company finances)."""
    domains = {
        'airbnb': 'short-term rental listings and reviews, not general aviation or electronics',
        'sec': 'public company financial filings, not general topic datasets',
        'yahoo': 'market prices and company tickers', 'yfinance': 'market prices and company tickers',
        'coingecko': 'cryptocurrency market prices', 'fred': 'economic time series',
        'worldbank': 'country development indicators', 'eurostat': 'European statistics',
    }
    unrelated = ('aircraft', 'airplane', 'aviation', 'movie', 'anime', 'electronics',
                 'طيارات', 'طائرات', 'افلام', 'أفلام', 'الكترونيات', 'إلكترونيات')
    financial = ('financial', 'finance', 'stock', 'revenue', 'econom', 'مالي', 'أسهم', 'ايراد', 'إيراد')
    text = goal.lower()
    if source in domains and any(w in text for w in unrelated) and not any(w in text for w in financial):
        return f"This source covers {domains[source]}. We must inspect the sample for your goal before saving."
    return ''


def interview(outdir: str | None = None) -> FetchRequest:
    print("Data Fetcher Pipeline — choose where to get your data.")
    while True:
        route = input("1. Ready/saved sources  2. External platform — choose 1 or 2: ").strip().lower()
        if route in ('1', '2', 'ready', 'external'):
            break
        print("Choose 1 or 2.")
    source = 'custom' if route in ('2', 'external') else choose_source()
    url = ''
    if source == 'custom':
        # A platform name alone needs human/agent discovery; the CLI does not invent a URL.
        url = _required("Platform name or full data/page URL: ")
        if not url.lower().startswith(('https://', 'http://')):
            print(f"Platform: {url}. Enter its public dataset or table page URL.")
            url = _required("Full URL: ")
    goal = _required("1/5. What data do you need? Include any country/date range and purpose: ")
    while True:
        warning = relevance_warning(source, goal)
        if not warning:
            break
        print("[Source scope] " + warning)
        choice = input("Inspect a sample here, choose another source, or cancel? (inspect/switch/cancel): ").strip().lower()
        if choice == 'inspect':
            break
        if choice == 'cancel':
            raise DataFetchError("Source selection cancelled.", code='ABORTED')
        if choice == 'switch':
            source = choose_source()
    if source == 'custom':
        query = url
        print("2/5. Using the supplied URL; HTML tables are selected by required columns.")
    else:
        from scripts.factory import source_info
        info = source_info()[source]
        query = input(f"2/5. Dataset selector ({info['query_format']}; e.g. {info['example']}): ").strip()
        if not query and info['query_format'] in ('free text', 'name fragment or numeric dataset ID'):
            query = goal
        elif not query and info.get('saved'):
            query = info['example']
        while not query:
            query = _required("Enter the selector required by this platform (a topic is not an ID): ")
    columns = list(dict.fromkeys(c.strip() for c in input("3/5. Required column names, comma separated (Enter for all): ").split(',') if c.strip()))
    while True:
        count = input("4/5. How many rows? (Enter/all for all): ").strip().lower()
        if count in ('', 'all') or (count.isdigit() and int(count) > 0):
            rows = int(count) if count.isdigit() else 0
            break
        print("Enter a positive row count or all.")
    while True:
        fmt = input("5/5. Output format: csv/json/excel/parquet/sqlite (Enter for csv): ").strip().lower() or 'csv'
        if fmt in ('csv', 'json', 'excel', 'xlsx', 'parquet', 'sqlite', 'db'):
            break
        print("Choose a listed format.")
    print("We will show actual rows before final approval. The sample may require a full transfer; you will be told first.")
    return FetchRequest(source, query, outdir or str(Path.cwd() / 'data_raw'), goal, columns, rows, fmt)
