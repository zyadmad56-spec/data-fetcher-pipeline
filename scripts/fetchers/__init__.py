"""Fetcher package.

Modules are imported lazily by scripts.factory (importlib on demand) so a
missing or broken optional dependency only affects its own source. Do NOT
add eager imports here — importing this package must stay cheap and cannot
fail on any optional dependency.
"""
