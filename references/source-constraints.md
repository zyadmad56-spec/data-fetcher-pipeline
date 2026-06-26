# Source-Specific Target Constraints & Heuristics

Credential testing is complete.
- **GitHub Constraints [PLANNED — NOT YET IMPLEMENTED]:** Restrict API searches to repositories containing data identifiers in `README.md`. Force the scraper to look exclusively for tabular file extensions (`.csv`, `.xlsx`) in paths containing `/data/`, `/datasets/`, or `/raw/`. Do not download source code files.
- **Yahoo Finance Adaptation:** 
  - **Phase 1 (Scouting):** Validate the existence of the Ticker Symbol (e.g., AMZN, TM). Fetch a small sample (e.g., 5 days history or basic info) to ensure the ticker is active and not delisted.
  - **Phase 2 (Extraction):** Download the requested historical market data or financial statements and save them strictly as flat `.csv` files.
- **SEC EDGAR Constraints:**
  - **Mandatory Identity & Throttle:** All requests MUST include a valid, descriptive `User-Agent` header (e.g., AppName/Version ContactEmail). The pipeline must explicitly enforce a sleep mechanism to stay strictly below the SEC's 10-requests-per-second limit to prevent permanent IP bans.
  - **Raw XBRL/JSON Flattening Only:** The pipeline must parse the deeply nested 10-K/10-Q financial facts and flatten them into structured DataFrames (CSV). It is strictly forbidden to normalize, impute, or logically interpret accounting standards; extraction must be 100% literal to the filed document.
- **FRED Constraints:**
  - **Environment-Isolated Credentials:** The pipeline must authenticate using a `FRED_API_KEY`. This key must NEVER be hardcoded. It must exclusively be retrieved from secure environment variables (`.env`).
  - **Strict Frequency Preservation:** FRED provides macro-data in monthly, quarterly, or annual frequencies. The pipeline is strictly forbidden from upsampling, forward-filling, or interpolating this data to match daily schedules (e.g., Yahoo Finance). Time-series frequencies must remain completely raw and unaligned during the fetch phase.
- **Inside Airbnb Constraints:**
  - **HTML Scouting & Progressive Resiliency Protocol:** Inside Airbnb does not have a standard REST API. Phase 1 (Scouting) must scrape/parse the "Get the Data" HTML page to resolve the exact static download URL (`.csv.gz`) for the user's requested City and Date compiled. To survive cosmetic CSS/layout changes, implement these three fallback tactics in order:
    1. **Regex & Attribute Targeting (The Core Strategy):** Bypass visual DOM elements. Scan raw HTML strictly for `href` attributes terminating in `.csv.gz` or containing the exact string "Get the Data".
    2. **Hidden APIs / Network Interception:** If static HTML parsing fails, prioritize locating background XHR/Fetch JSON endpoints that populate the page.
    3. **AI Vision Agent Fallback:** If code-based scraping completely fails, acknowledge that the system architecture should theoretically trigger multimodal vision models (like Manus AI or Claude Computer Use) to visually identify and click download coordinates.
  - **Safe Decompression Protocol:** Phase 2 (Extraction) must download the `.gz` file and safely decompress it into a flat `.csv` file. 
  - **Absolute Raw Data Preservation (Zero-Cleaning):** The dataset contains messy columns (e.g., prices formatted as `$1,200.00`, long host IDs, HTML in descriptions). The agent is STRICTLY FORBIDDEN from attempting to clean, parse, or cast data types (e.g., do not convert currency strings to floats). Save the decompressed CSV exactly as it was compiled by the source.
- **DoltHub Constraints [PLANNED — NOT YET IMPLEMENTED]:**
  - **Pre-flight Schema Validation (Anti-Hallucination):** During Phase 1 (Scouting), the agent must execute `SHOW TABLES;` and `DESCRIBE target_table;`. It is strictly forbidden to execute a data extraction query without first explicitly verifying that the table and required columns exist in the current schema.
  - **Commit Hash State-Locking (ACID Compliance):** To prevent data corruption from mid-extraction merges, Phase 1 must resolve the default branch to its latest Commit Hash. All Phase 2 extraction queries MUST target that exact commit hash (using `AS OF 'commit_hash'`) rather than a floating branch pointer.
  - **System Table Exclusion:** The pipeline must explicitly ignore all internal metadata tables beginning with `dolt_` (e.g., `dolt_commits`, `dolt_branches`) unless the user explicitly requests version-control metadata.
  - **Mandatory Query Pagination (Timeout Prevention):** A blind `SELECT *` is strictly forbidden. Phase 2 (Extraction) MUST append `LIMIT` and `OFFSET` clauses to all queries to fetch data in chunks, or enforce a hard row-limit ceiling (e.g., `LIMIT 100000`) to prevent API timeouts and memory exhaustion.
  - **Read-Only SQL-to-CSV Flattening:** The pipeline must exclusively execute `SELECT` statements. Any mutations (`INSERT`, `UPDATE`, `DROP`, branch creation) are absolutely forbidden. Data must be exported as flat `.csv` files.
- **Notebookcheck Constraints [PLANNED — NOT YET IMPLEMENTED]:**
  - **Tabular Targeting:** The pipeline must use `pandas.read_html` targeting explicit `table` or `class="multisort"` elements. Vision-based fallbacks are strictly prohibited to avoid optical character recognition errors on decimal frame rates.
  - **Granular TGP/Chassis Isolation:** The extractor must capture the exact laptop model alongside the FPS metric. It is strictly forbidden to merge or average scores from different chassis (e.g., HP Pavilion vs ASUS Vivobook).
  - **Zero-Imputation Protocol:** If a specific game/hardware configuration was not explicitly tested, the field must remain `NULL`. It is strictly forbidden to guess or extrapolate mobile FPS from desktop equivalents.
  - **Aggressive IP-Ban Prevention:** Because there is no public API, the pipeline MUST enforce an aggressive `time.sleep(15)` cooldown between page requests and utilize a descriptive `User-Agent`.
