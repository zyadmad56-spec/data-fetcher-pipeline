"""Custom sources participate in the same preview/validate/save lifecycle.

Execution model (Phase 9): extractors never run in this process. ``scout``
locks the extractor bytes (saved source, constructor-provided bytes, or the
generated template) into memory; every execution goes through
``SubprocessExtractorRunner`` with those exact bytes. Disk edits after scout
have zero effect. A full fetch requires a valid :class:`PreviewReceipt`
binding script bytes + canonical URL + columns + table index; interactive
flows create their receipt internally during the bounded preview and confirm
it at the human approval prompt, while non-interactive runs must present one
via the ``approved_receipt`` constructor parameter (the ``--approve-preview
ID`` CLI flag wiring lands in a later wave; the parameter seam is ready).
"""
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from scripts.base import BaseFetcher
from scripts.errors import DataFetchError
from scripts.adapters.extractor_protocol import PreviewReceipt, WorkerRequest, sha256_bytes
from scripts.adapters.script_runner import ExtractorRejectedError, SubprocessExtractorRunner, inspect_extractor_ast
from scripts.source_registry import (
    load_preview_receipt, save_preview_receipt, validate_registered_origin,
    verified_script_bytes,
)
from scripts.web_analyzer import WebAnalyzer

#: Worker codes translated to the historical envelope codes.
_ERROR_CODE_MAP = {'EXTRACTION_FAILED': 'PROVIDER_ERROR'}
_PREVIEW_ROW_BOUND = 5
_EXTRACTOR_MAX_BYTES = 50 * 1024 * 1024
_EXTRACTOR_DEADLINE_SEC = 120.0


class CustomFetcher(BaseFetcher):
    def __init__(self, query, outdir, config, source='custom', descriptor=None,
                 extractor_bytes=None, approved_receipt=None):
        if descriptor:
            query = query or descriptor['url']
            validate_registered_origin(descriptor['url'], query)
        super().__init__(query, outdir, config, source=source)
        self.descriptor = descriptor
        if extractor_bytes is not None:
            if not isinstance(extractor_bytes, (bytes, bytearray)):
                raise DataFetchError('extractor_bytes must be raw script bytes.', code='ARG_MISSING')
            try:
                inspect_extractor_ast(bytes(extractor_bytes))
            except ExtractorRejectedError as exc:
                raise DataFetchError(f'Extractor bytes do not pass the safety review: {exc}',
                                     code='EXTRACTOR_REJECTED') from exc
        self.extractor_bytes = bytes(extractor_bytes) if extractor_bytes is not None else None
        self.approved_receipt = approved_receipt  # PreviewReceipt or preview-id string
        self.script_path = None
        self.script_bytes = None
        self.script_sha256 = ''
        self._pending_receipt = None
        self._workspace = None
        self.report = None
        self.transfer_scope = ('reviewed saved extractor; transfer cost depends on its implementation; result cached'
                               if descriptor else 'full custom payload, capped at 50 MB; result cached for final save')

    def scout(self):
        analyzer = WebAnalyzer(self.query, self.outdir)
        self.report = analyzer.analyze()
        print(f'[Custom assessment] {self.report.difficulty}; robots allowed={self.report.robots_allowed}; tables={self.report.table_count}')
        for warning in self.report.warnings:
            print('[Custom assessment] ' + warning)
        if self.report.status_code == 0:
            raise DataFetchError('Custom assessment could not reach the provider. See assessment warnings.', code='NETWORK')
        if self.report.status_code == 404:
            raise DataFetchError('Custom dataset URL was not found (HTTP 404).', code='NOT_FOUND')
        if self.report.status_code == 429:
            raise DataFetchError('Custom provider rate limit reached (HTTP 429).', code='RATE_LIMITED')
        if self.report.status_code >= 400:
            raise DataFetchError(f'Custom provider refused the request (HTTP {self.report.status_code}).', code='BLOCKED_URL' if self.report.status_code in (401,403) else 'PROVIDER_ERROR')
        if self.report.difficulty == 'blocked':
            raise DataFetchError('Target blocked by access policy or anti-bot protection. Use an authorized API/export.', code='BLOCKED_URL')
        if self.report.difficulty == 'hard' and not self.descriptor:
            raise DataFetchError('No supported export/table found; a reviewed platform-specific browser/API extractor is required.', code='UNSUPPORTED_SOURCE')
        self._lock_extractor_bytes()
        self.resolved_title = self.descriptor['name'] if self.descriptor else urlparse(self.query).hostname
        return {'url': self.query, 'size_info': f'{self.report.content_length or "Unknown"} bytes; {self.transfer_scope}'}

    def _lock_extractor_bytes(self) -> None:
        """Read the extractor exactly once; disk edits after scout have no effect."""
        if self.descriptor:
            self.script_path, self.script_bytes = verified_script_bytes(self.descriptor)
        elif self.extractor_bytes is not None:
            self.script_bytes = self.extractor_bytes
        else:
            self._workspace = tempfile.TemporaryDirectory(prefix='dfp-custom-')
            generator = WebAnalyzer(self.query, self._workspace.name)
            generated_path = Path(generator.generate_script(self.report))
            self.script_path = generated_path
            self.script_bytes = generated_path.read_bytes()
        self.script_sha256 = sha256_bytes(self.script_bytes)

    def _allowed_origin(self) -> str:
        parsed = urlparse(self.query)
        return f'{parsed.scheme}://{parsed.netloc}'

    def _table_index(self):
        return getattr(self, 'table_index', None)

    def _run_extractor(self, row_limit: int):
        runner = SubprocessExtractorRunner()
        request = WorkerRequest(script_sha256=self.script_sha256, query_url=self.query,
                                columns=tuple(self.required_columns),
                                table_index=self._table_index(), row_limit=row_limit,
                                max_bytes=_EXTRACTOR_MAX_BYTES,
                                deadline_sec=_EXTRACTOR_DEADLINE_SEC,
                                allowed_origin=self._allowed_origin())
        result, frame = runner.extract(self.script_bytes, request)
        if result.error_code is not None:
            code = _ERROR_CODE_MAP.get(result.error_code, result.error_code)
            raise DataFetchError(result.error_summary or f'Extractor failed ({result.error_code}).',
                                 code=code)
        return frame

    def _bounded_sample_frame(self, limit: int):
        """Pre-approval sample for EVERY custom flow (bounded, never cached).

        Called by ``FetcherProvider._full_preview_frame``. Persists the receipt
        together with the staged bytes so a later approval never re-reads the
        original extractor file.
        """
        if self.script_bytes is None:
            raise DataFetchError('Custom extractor was not prepared.', code='INTERNAL')
        row_limit = min(limit, _PREVIEW_ROW_BOUND) if limit > 0 else _PREVIEW_ROW_BOUND
        receipt = PreviewReceipt(preview_id=PreviewReceipt.new_id(),
                                 script_sha256=self.script_sha256,
                                 canonical_url=self.query,
                                 columns=tuple(self.required_columns),
                                 table_index=self._table_index())
        frame = self._run_extractor(row_limit)
        self._pending_receipt = receipt
        save_preview_receipt(receipt, self.script_bytes)
        return frame

    def _resolve_receipt(self):
        """Receipt for a FULL fetch; fail closed when approval cannot be proven."""
        receipt = self.approved_receipt
        if isinstance(receipt, str):
            receipt, _ = load_preview_receipt(receipt)
        if receipt is None:
            receipt = self._pending_receipt
        if receipt is None:
            raise DataFetchError(
                'Custom full fetch requires approval of a bounded preview. Interactive runs '
                'approve at the sample prompt; non-interactive runs must pass '
                '--approve-preview ID from an earlier preview.', code='APPROVAL_REQUIRED')
        if not isinstance(receipt, PreviewReceipt):
            raise DataFetchError('approved_receipt must be a PreviewReceipt or preview ID.',
                                 code='APPROVAL_MISMATCH')
        drift = []
        if receipt.script_sha256 != self.script_sha256:
            drift.append('extractor bytes')
        if receipt.canonical_url != self.query:
            drift.append('source URL')
        if receipt.columns != tuple(self.required_columns):
            drift.append('required columns')
        if receipt.table_index != self._table_index():
            drift.append('table index')
        if drift:
            raise DataFetchError(
                'Approval does not match this fetch (changed: ' + ', '.join(drift) + '); '
                'run a fresh bounded preview and approve again.', code='APPROVAL_MISMATCH')
        if receipt.is_expired():
            raise DataFetchError('The approval preview expired; run a fresh bounded preview.',
                                 code='PREVIEW_EXPIRED')
        return receipt

    def extract(self):
        if self.script_bytes is None:
            raise DataFetchError('Custom extractor was not prepared.', code='INTERNAL')
        self._resolve_receipt()
        return self._run_extractor(self.row_limit)

    def cleanup(self):
        if self._workspace:
            self._workspace.cleanup()
            self._workspace = None
