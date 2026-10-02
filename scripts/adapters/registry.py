"""Existing atomic local source registry implements the retention contract."""
from scripts import source_registry


class LocalSourceRegistry:
    def save(self, name: str, url: str, script) -> str:
        return source_registry.save_source(name, url, script)

    def defer(self, url: str, script) -> str:
        return source_registry.defer_source_choice(url, script)

    def finish(self, token: str, name: str | None) -> str | None:
        return source_registry.finish_source_choice(token, name)
