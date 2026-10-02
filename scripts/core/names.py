"""Dataset naming policy shared by callers and persistence."""
import re


def sanitize_name(text: str) -> str:
    cleaned = re.sub(r'[^a-zA-Z0-9_-]+', '_', text).strip('_').lower()
    return cleaned[:60] or 'dataset'
