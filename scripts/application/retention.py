"""Retention decisions do not read scripts, mutate a registry or ask questions."""
from dataclasses import dataclass

from scripts.application.contracts import SourceRegistry


@dataclass(frozen=True)
class RetentionRequest:
    name: str | None
    url: str
    script: object
    deferred: bool = False


def retain_source(request: RetentionRequest, registry: SourceRegistry) -> tuple[str | None, str | None]:
    if request.name:
        return registry.save(request.name, request.url, request.script), None
    if request.deferred:
        return None, registry.defer(request.url, request.script)
    return None, None


def finish_source_choice(token: str, name: str | None, registry: SourceRegistry) -> str | None:
    return registry.finish(token, name)
