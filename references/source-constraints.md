# Source-Specific Target Constraints & Heuristics

- **GitHub Constraints:**
  - **Discovery Strategy:** The legacy REST code-search endpoint is inactive and does not return result items. Discovery searches repositories by query sorted by stars (top 5), then walks the recursive git tree (`/git/trees/{branch}?recursive=1`) to locate tabular `.csv` data files.
  - **Authentication Chain:** Resolves authentication via `GITHUB_TOKEN` in environment variables first, then checks the global config JSON (`~/.config/data-fetcher-pipeline/config.json`), and falls back to `gh auth token` via the local `gh` CLI. Unauthenticated requests are allowed as a last resort under strict public rate limits.
  - **Advisory Budget & Skip Handling:** Enforces a 50 MB advisory budget (`MAX_CSV_BYTES`). Prefers the largest CSV within budget; if all available CSVs exceed budget, settles for the smallest and emits a completeness warning. Repositories whose git tree returns `truncated: true` or trigger HTTP/network errors are skipped and documented in completeness warnings.

- **Yahoo Finance Adaptation:**
  - **Deferred Dependency:** Uses lazy/deferred import of `yfinance` inside fetch methods to keep startup and discovery fast and avoid eager-import fragility.
  - **Phase 1 (Scouting):** Validates existence and activity of the ticker symbol (e.g., AAPL, AMZN) by fetching a 5-day historical sample (`period="5d"`). Delisted or non-existent tickers raise an immediate error.
  - **Phase 2 (Extraction):** Downloads the full daily candle history (`period="max"`), resets index, and returns flat structured tabular data.

- **SEC EDGAR Constraints:**
  - **Mandatory Identity & Throttle:** EDGAR expects a descriptive User-Agent (email-based), which `scripts/fetchers/sec.py` implements via `SEC_API_KEY` (configured in `~/.config/data-fetcher-pipeline/config.json` or as an environment variable). Pacing: PLANNED. Current enforcement: per-host min-interval table in `scripts/http_utils.py` (`MIN_HOST_INTERVALS`); SEC has no dedicated pacing yet.
  - **CIK Resolution:** Resolves corporate ticker symbols against `https://www.sec.gov/files/company_tickers.json` to a 10-digit zero-padded CIK string.
  - **Raw XBRL/JSON Flattening Only:** Fetches company facts from `https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json` and flattens nested taxonomy concepts and unit observations into structured rows (`taxonomy`, `concept`, `unit`, `val`, `fy`, `fp`, `form`, `filed`, `end`). Extraction is literal to filed facts; normalizing, imputing, or interpreting accounting standards is strictly prohibited.

- **FRED Constraints:**
  - **Key Storage & Validation:** The primary key store is the global config JSON (`~/.config/data-fetcher-pipeline/config.json`); environment variables (`FRED_API_KEY`) are the FALLBACK. Keys are validated as 32-character lowercase alphanumeric strings (`[a-z0-9]{32}`), failing fast with `AUTH_INVALID` if malformed or stale.
  - **Strict Frequency Preservation:** FRED provides macroeconomic time series in monthly, quarterly, or annual frequencies. The pipeline is strictly forbidden from upsampling, forward-filling, or interpolating data to align with daily series. Frequencies remain raw and unaligned. Missing observation markers (`.`) are safely coerced to numeric `NaN`.

- **Inside Airbnb Constraints:**
  - **HTML Scouting:** Inside Airbnb does not provide a public REST API. Phase 1 (Scouting) fetches the `http://insideairbnb.com/get-the-data/` index page and parses links via `BeautifulSoup` to resolve the static `.csv.gz` download link matching the city query and ending in `listings.csv.gz`.
  - **Governance Warning:** Automatically attaches a completeness warning alerting downstream consumers that Inside Airbnb listings contain personal data (host names, host IDs, precise coordinates) requiring privacy review before redistribution.
  - **Safe Decompression & Streaming:** Phase 2 (Extraction) streams the compressed `.csv.gz` payload to a temporary file and decompresses on parse via `_consume_payload_csv(compression="gzip")` with flat memory footprint.
  - **Absolute Raw Data Preservation (Zero-Cleaning):** Columns with formatted currency strings (e.g., `$1,200.00`), long IDs, and unescaped characters are preserved exactly as compiled by the upstream source.

- **CoinGecko Constraints:**
  - **Authentication & History Cap:** No API key required; operates over the free public REST API (`COINGECKO_API_KEY` optional in env or config). The free tier caps market history at 365 days (`days=max` is paywalled with HTTP 401; keyless requests automatically degrade to 365 days with `complete: false` and a completeness warning, recording the fallback URL in provenance).
  - **Pacing:** Per-host pacing of 6.0s is enforced across ALL execution modes via `scripts/http_utils.py` (`MIN_HOST_INTERVALS["api.coingecko.com"]`).
  - **Query Resolution & Preview Cache:** Candidate queries are cleaned and validated against `/coins/{id}/market_chart?days=1`. Non-id inputs (symbols or common names like `btc`) auto-resolve via the `/search` endpoint. The scout's 1-day probe payload is cached and reused by `preview()` at zero additional request cost.

- **Data.gov (GSA Catalog API v4) Constraints:**
  - **API Target & Credentials:** Targets the active GSA Catalog API v4 (`https://api.gsa.gov/technology/datagov/v4/search`). API key is optional (`DATAGOV_API_KEY` in env or config); falls back to the shared `DEMO_KEY` with lower rate limits.
  - **Cursor Pagination & Selection:** Employs cursor pagination (`after` parameter, scanning up to 3 pages with 100 results per page) to identify valid HTTP/HTTPS CSV distributions across sparse metadata. Emits an error if no CSV resource is discovered.
  - **Streaming & Download Reuse:** Preview streams the CSV payload once to a temporary file; extraction reuses the cached temporary payload without repeating the HTTP download.
