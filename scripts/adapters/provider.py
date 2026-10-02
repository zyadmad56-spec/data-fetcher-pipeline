"""Compatibility with current provider hooks and streamed payload resources."""
from pathlib import Path

import pandas as pd


class FetcherProvider:
    def __init__(self, fetcher):
        self.fetcher = fetcher

    def scout(self) -> dict:
        return self.fetcher.scout()

    def preview(self) -> None:
        self.fetcher.preview()

    def preview_default(self) -> None:
        url = self.fetcher._payload_url()
        if url and (not self.fetcher._payload_compression()):
            df = self.fetcher._bounded_csv_preview(url)
        elif url:
            temp = self.fetcher._download_to_tempfile(url, headers=self.fetcher._payload_headers())
            df = pd.read_csv(
                temp, sep=self.fetcher._payload_sep(), nrows=5,
                compression=self.fetcher._payload_compression(), low_memory=False)
        else:
            df = self._full_preview_frame()
        self.fetcher.show_preview(df)

    def _full_preview_frame(self):
        limit = self.fetcher.row_limit
        try:
            # Approval can increase the preset count, so cache the complete frame.
            self.fetcher.row_limit = 0
            frame = self.fetcher.extract()
        finally:
            self.fetcher.row_limit = limit
        self.fetcher._cached_df = frame
        return frame

    def extract(self):
        return self.fetcher.extract()

    def preview_transfer_scope(self) -> str:
        return self.fetcher.preview_transfer_scope()

    def needs_full_transfer(self) -> bool:
        return not self.fetcher.lightweight_preview and not (
            self.fetcher._payload_url() and not self.fetcher._payload_compression())

    def population(self) -> int | None:
        population = getattr(self.fetcher, '_population_estimate', None)
        if population is not None:
            return population
        payload = self.fetcher._temp_payload
        if payload and Path(payload).exists():
            try:
                with open(payload, 'rb') as stream:
                    return max(sum(1 for _ in stream) - 1, 0)
            except OSError:
                return None
        return None

    def release_payload(self) -> None:
        payload = self.fetcher._temp_payload
        if payload:
            try:
                Path(payload).unlink(missing_ok=True)
            except OSError:
                # Preserve the existing best-effort streamed-cache cleanup contract.
                pass
            self.fetcher._temp_payload = None


class FetcherArtifacts:
    """Preserve public save_csv overrides while the default delegates to storage."""
    def __init__(self, fetcher):
        self.fetcher = fetcher

    def save(self, frame, filename):
        path = self.fetcher.save_csv(frame, filename)
        return self.fetcher.saved_artifacts(path)
