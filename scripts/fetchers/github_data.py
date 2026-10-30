import os
import subprocess
from pathlib import Path
from typing import Dict, List, Tuple
from urllib.parse import quote
import pandas as pd
from scripts.base import BaseFetcher
from scripts.http_utils import request_with_retry

def _gh_executable() -> str:
    """Resolve the gh CLI from PATH while never returning a working-directory hit.

    On Windows, spawning a bare name searches the working directory first, so
    a planted gh.exe in the task folder would be executed — and modern CPython
    shutil.which re-inserts the cwd into its Windows search, so the resolution
    is done here directly: each PATH entry is probed for an executable ``gh``
    (PATHEXT extensions on Windows), entries and candidates that land in the
    current directory are refused, and the resolved absolute path is returned.
    Returns "" when no safe candidate exists.
    """
    try:
        current = Path.cwd().resolve()
    except OSError:
        current = None
    if os.name == "nt":
        extensions = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep)
    else:
        extensions = [""]
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        for extension in extensions:
            candidate = Path(entry) / ("gh" + extension)
            try:
                if not candidate.is_file() or not os.access(candidate, os.X_OK):
                    continue
                resolved = candidate.resolve()
            except OSError:
                continue
            if current is not None and resolved.parent == current:
                continue  # never execute gh from the working directory
            return str(resolved)
    return ""

def _resolve_github_token(config: Dict[str, str]) -> str:
    """GitHub search requires authentication. Resolve a token from
    environment, config, or the gh CLI — empty string if none found."""
    token = os.environ.get("GITHUB_TOKEN") or config.get("GITHUB_TOKEN", "")
    if token:
        return token
    executable = _gh_executable()
    if not executable:
        return ""
    try:
        result = subprocess.run(
            [executable, "auth", "token"], capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""

class GitHubDataFetcher(BaseFetcher):
    """Fetcher for public datasets hosted in GitHub repositories.

    Discovery strategy: search repositories by query (sorted by stars), then
    walk each repo's git tree to locate CSV data files. This avoids the REST
    code-search endpoint, which no longer returns result items.
    """

    MAX_CSV_BYTES = 50 * 1024 * 1024  # prefer CSVs up to 50 MB

    def __init__(self, query: str, outdir: str, config: Dict[str, str]) -> None:
        super().__init__(query, outdir, config)
        self.download_url: str = ""
        self.repo_name: str = ""
        self.file_name: str = ""

    def _api_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "data-fetcher-pipeline/2.0",
        }
        token = _resolve_github_token(self.config)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _list_repo_csvs(self, full_name: str, branch: str, skip_notes: List[str]) -> List[Tuple[str, int]]:
        """Return (path, size_bytes) for every CSV blob in a repo tree; records
        skip reasons instead of failing silently — selection must be transparent."""
        url = f"https://api.github.com/repos/{full_name}/git/trees/{quote(branch, safe='/')}?recursive=1"
        try:
            # allow_redirects=False: a redirect must never be followed with the
            # Authorization header attached (the token stays on api.github.com).
            response = request_with_retry(url, headers=self._api_headers(), allow_redirects=False)
        except Exception as exc:  # coded DataFetchError or unexpected — either way, note it
            skip_notes.append(f"{full_name}: skipped ({type(exc).__name__})")
            return []
        if response.status_code != 200:
            skip_notes.append(f"{full_name}: skipped (HTTP {response.status_code})")
            return []
        payload = response.json()
        if payload.get("truncated", False):
            skip_notes.append(f"{full_name}: skipped (file tree too large to enumerate)")
            return []
        return [
            (t["path"], t.get("size", 0))
            for t in payload.get("tree", [])
            if t.get("type") == "blob" and t["path"].lower().endswith(".csv")
        ]

    def scout(self) -> Dict[str, str]:
        print(f"[Scout] Searching GitHub repositories for '{self.query}' datasets...")

        url = f"https://api.github.com/search/repositories?q={quote(self.query)}&sort=stars&per_page=5"
        has_token = bool(_resolve_github_token(self.config))

        try:
            # allow_redirects=False: a redirect must never be followed with the
            # Authorization header attached (the token stays on api.github.com).
            response = request_with_retry(url, headers=self._api_headers(), allow_redirects=False)
        except ConnectionError as exc:
            raise RuntimeError(f"GitHub search request failed: {type(exc).__name__}.") from exc

        if response.status_code == 403:
            raise ValueError(
                "GitHub rate limit hit. Try again in a few minutes or provide a token "
                "(gh auth login, GITHUB_TOKEN env var, or config entry)."
            )
        elif response.status_code == 401:
            raise ValueError("GitHub rejected the provided token. Check GITHUB_TOKEN.")
        elif response.status_code != 200:
            # The raw body is deliberately omitted: provider error pages are
            # attacker-influenced content and must not reach error channels.
            raise ValueError(f"GitHub API returned status {response.status_code}.")

        repos = response.json().get("items", [])
        if not repos:
            raise ValueError(f"No repositories found on GitHub matching: '{self.query}'.")

        skip_notes: List[str] = []
        for repo in repos:
            full_name = repo.get("full_name", "")
            branch = repo.get("default_branch", "main")
            csvs = self._list_repo_csvs(full_name, branch, skip_notes)
            if not csvs:
                continue

            # Prefer the largest CSV within budget; if everything is oversized,
            # settle for the smallest so extraction can still succeed (visibly)
            in_budget = [(p, s) for p, s in csvs if s <= self.MAX_CSV_BYTES]
            if in_budget:
                path, size = max(in_budget, key=lambda ps: ps[1])
            else:
                path, size = min(csvs, key=lambda ps: ps[1])
                self.completeness_warnings.append(
                    f"All CSVs in '{full_name}' exceed the {self.MAX_CSV_BYTES // (1024*1024)} MB advisory budget — "
                    f"selected the smallest ({size / 1e6:.1f} MB), which will be fully downloaded."
                )

            if skip_notes:
                self.completeness_warnings.append(
                    "Repository selection was degraded by: " + "; ".join(skip_notes)
                )

            self.repo_name = full_name
            self.file_name = path
            self.resolved_title = f"{full_name} — {path}"
            self.download_url = (
                f"https://raw.githubusercontent.com/{full_name}/"
                f"{quote(branch, safe='/')}/{quote(path, safe='/')}"
            )
            print(f"[Scout] Resolved '{path}' ({size / 1e6:.1f} MB) from repository '{full_name}'.")
            return {
                "url": self.download_url,
                "size_info": f"{size / 1e6:.1f} MB CSV"
            }

        if skip_notes:
            raise ValueError(
                f"No CSV data files could be verified in the top repositories matching '{self.query}'. "
                f"Repositories skipped: {'; '.join(skip_notes)}"
            )
        raise ValueError(f"No CSV data files found in the top repositories matching: '{self.query}'.")

    def extract(self) -> pd.DataFrame:
        print(f"[Extract] Loading CSV payload (row limit: {self.row_limit or 'all'})...")
        if not self.download_url:
            raise ValueError("Download URL is not set. Run scout() first.")
        # Reuses the preview's temp payload when present — one download per run
        return self._consume_payload_csv()
