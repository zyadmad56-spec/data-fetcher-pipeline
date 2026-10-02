"""Conversion precedes source retention; a failed conversion never registers a source."""
from typing import Callable


def complete_dataset(csv_path: str, output_format: str, convert: Callable[[str, str], str],
                     retain: Callable[[], tuple[str | None, str | None]]) -> tuple[str | None, str | None, str | None]:
    converted = convert(output_format, csv_path) if output_format != 'csv' else None
    registered, pending = retain()
    return converted, registered, pending
