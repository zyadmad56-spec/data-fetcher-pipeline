"""Ordered scout, sample, approval, extraction and save lifecycle."""
from scripts.application.contracts import FetchPorts
from scripts.application.frame_policy import limit_rows, null_density, select_columns
from scripts.core.errors import DataFetchError
from scripts.core.models import FetchState, SavedArtifacts
from scripts.core.names import sanitize_name


class FetchDataset:
    def __init__(self, state: FetchState, ports: FetchPorts):
        self.state = state
        self.ports = ports

    def run(self) -> SavedArtifacts | None:
        self.ports.interaction.report(
            f"[{self.state.provider_name}] Initiating extraction sequence for query: '{self.state.query}'")
        try:
            metadata = self.ports.provider.scout() or {}
            self.state.dataset_url = metadata.get('url', 'N/A')
            if not self.state.auto_approve or self.state.preview_only:
                self._review_sample(metadata)
                if self.state.preview_only:
                    return None
            return self._extract_and_save()
        finally:
            self.ports.provider.release_payload()

    def _review_sample(self, metadata: dict) -> None:
        self.state.transfer_scope = self.ports.provider.preview_transfer_scope()
        self.ports.interaction.report('[Preview transfer] ' + self.state.transfer_scope)
        if self.ports.provider.needs_full_transfer() and not self.state.auto_approve:
            self.ports.interaction.consent_to_transfer()
        self.ports.provider.preview()
        sample = self.state.preview_frame
        if sample is None or sample.empty:
            raise DataFetchError('No actual sample available; cannot approve a dataset blindly.', code='NOT_FOUND')
        self.state.preview_frame = select_columns(sample, self.state.required_columns)
        if self.state.preview_only:
            return
        requested = self.ports.interaction.approve_sample(metadata, self.state.row_limit)
        if requested == -1:
            self.state.row_limit = 0
        elif requested:
            self.state.row_limit = requested

    def _extract_and_save(self) -> SavedArtifacts:
        frame = self.state._cached_df
        if frame is None:
            frame = self.ports.provider.extract()
        population = None
        if self.state.row_limit > 0 and len(frame) > self.state.row_limit:
            self.ports.interaction.report(f'[Engine] Slicing dataset to requested {self.state.row_limit} rows...')
        elif self.state.row_limit > 0 and self.state.truncation is None and len(frame) == self.state.row_limit:
            population = self.ports.provider.population()
        frame = limit_rows(frame, self.state, population)
        frame = select_columns(frame, self.state.required_columns)
        self.ports.interaction.show_validation(null_density(frame))
        return self.ports.artifacts.save(frame, sanitize_name(self.state.query) + '_raw.csv')
