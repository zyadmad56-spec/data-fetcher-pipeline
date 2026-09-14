"""Directory provisioning — kept free of pandas/heavy imports so that
`--list-sources` and `--convert` never pay the data-stack import cost."""
import re
from pathlib import Path
from typing import Optional

PLATFORMS = [
    "kaggle", "worldbank", "sec", "fred", "eurostat",
    "datagov", "openml", "airbnb", "github", "yahoo", "coingecko", "custom"
]

def _sanitize(text: str) -> str:
    return re.sub(r'[^a-zA-Z0-9_-]+', '_', text).strip('_').lower()

def provision_data_directory(outdir_path: str, source_key: Optional[str] = None) -> None:
    """Create the root directory plus the platform subfolder(s) a run touches.

    With source_key=None all known platform folders are provisioned (legacy
    behavior); with a key only that platform is created — discovery and
    conversion commands stay side-effect free.
    """
    base_dir = Path(outdir_path)
    base_dir.mkdir(parents=True, exist_ok=True)

    platforms = PLATFORMS
    if source_key is not None:
        platforms = [source_key] if source_key in PLATFORMS else [_sanitize(source_key)]
    for platform in platforms:
        (base_dir / platform).mkdir(parents=True, exist_ok=True)
