"""Bounded HTTP previews and streamed payload cache; no approval policy.

All transfers run through the supervised HTTP port with explicit deadlines:
the preview is a bounded prefix intent, the full download carries its
``max_bytes`` cap inside the supervised job, and byte/request/retry metrics
are counted once at the HTTP layer.
"""
import os
import time
from pathlib import Path
from typing import Dict, Optional

import pandas as pd

from scripts.core.errors import DataFetchError
from scripts.core.models import FetchState, PayloadSpec
from scripts.core.names import sanitize_name
from scripts.http_utils import add_metrics_bytes

# Whole-download budget (connect + headers + streamed body) on a monotonic
# clock.  Bounded but generous: payload sizes are capped separately by
# spec.max_bytes (500 MB); this deadline stops stalled/hung transfers.
DOWNLOAD_DEADLINE_SECONDS = 300.0


class PayloadReader:
    def __init__(self, state: FetchState, spec: PayloadSpec, request):
        self.state = state
        self.spec = spec
        self.request = request

    def _bounded_csv_preview(self, url: str) -> pd.DataFrame:
        import io
        # Explicit prefix intent: the supervised job reads at most
        # preview_bytes and closes the upstream promptly; a huge declared
        # tail is not rejected and a bounded prefix is never cached as the
        # complete dataset. Exhaustiveness comes from the response contract
        # (``body_complete``); legacy in-process doubles without it keep the
        # conservative not-cached behavior.
        response = self.request(url, headers=self.spec.headers, stream=True,
                                public_only=True, deadline_sec=DOWNLOAD_DEADLINE_SECONDS,
                                prefix_bytes=self.spec.preview_bytes)
        try:
            if response.status_code != 200:
                raise DataFetchError(f'HTTP {response.status_code} fetching preview.', code='PROVIDER_ERROR')
            payload, exhausted = self._preview_prefix(response)
        finally:
            response.close()
        if exhausted:
            target = Path(self.state.outdir) / (sanitize_name(self.state.query) + '_payload.tmp')
            target.write_bytes(payload)
            self.state._temp_payload = target
        else:
            payload = self._complete_record_prefix(payload)
        return pd.read_csv(io.BytesIO(payload), sep=self.spec.separator, nrows=5, low_memory=False)

    @staticmethod
    def _boundary_metered(response) -> bool:
        """True when the HTTP boundary already reported this response's size:
        the local spool read is then a copy, not upstream delivery, and must
        not be re-counted. Boundary fixtures without a reported size count
        their chunk reads here — the only observable delivery point."""
        flag = getattr(response, 'body_bytes', None)
        return isinstance(flag, int) and not isinstance(flag, bool)

    @staticmethod
    def _exhausted(response, default: bool) -> bool:
        # The completeness flag is trusted only as a real bool: boundary
        # fixtures auto-create attributes that must not flip caching.
        flag = getattr(response, 'body_complete', None)
        return flag if isinstance(flag, bool) else default

    def _preview_prefix(self, response) -> tuple[bytearray, bool]:
        payload = bytearray()
        deadline = time.monotonic() + DOWNLOAD_DEADLINE_SECONDS
        meter = not self._boundary_metered(response)
        for chunk in response.iter_content(16384):
            if time.monotonic() >= deadline:
                raise DataFetchError(
                    f'Preview transfer exceeded the {DOWNLOAD_DEADLINE_SECONDS:.0f}s deadline.', code='TIMEOUT')
            if meter:
                add_metrics_bytes(len(chunk))
            remaining = self.spec.preview_bytes - len(payload)
            payload.extend(chunk[:remaining])
            if len(payload) >= self.spec.preview_bytes:
                return payload, self._exhausted(response, False)
        return payload, self._exhausted(response, True)

    def _complete_record_prefix(self, payload: bytearray) -> bytearray:
        # Parsing a bounded incomplete quoted record must fail rather than invent rows.
        last_newline = payload.rfind(b'\n')
        if last_newline < 0:
            raise DataFetchError('No complete CSV records within the 256 KB preview cap.', code='NOT_FOUND')
        return payload[:last_newline + 1]

    def _download_to_tempfile(self, url: str, headers: Optional[Dict[str, str]] = None) -> Path:
        """Reuse a capped streamed payload instead of downloading it for each phase.

        The download runs on the public-only transport (every DNS answer
        revalidated at connect time, redirect hops included) under a monotonic
        deadline.
        """
        target = Path(self.state.outdir) / (sanitize_name(self.state.query) + '_payload.tmp')
        # The full-transfer cap travels INTO the supervised job so the child
        # enforces it during the transfer; the parent loop below stays the
        # same contract legacy boundary fixtures exercise.
        response = self.request(url, headers=headers or self.spec.headers, stream=True,
                                public_only=True, deadline_sec=DOWNLOAD_DEADLINE_SECONDS,
                                max_body_bytes=self.spec.max_bytes)
        try:
            if response.status_code != 200:
                raise ValueError(f'HTTP {response.status_code} while fetching payload.')
            content_length = response.headers.get('Content-Length')
            # Declared-size fast reject (the supervised child enforces the
            # same cap during the transfer; this keeps the parent honest for
            # boundary fixtures and avoids opening any spool for a body the
            # cap forbids outright).
            if content_length and content_length.isdigit() and (int(content_length) > self.spec.max_bytes):
                raise DataFetchError(
                    f'Payload exceeds the {self.spec.max_bytes // (1024 * 1024)} MB download cap '
                    f'(Content-Length: {content_length}).', code='SIZE_LIMIT')
            self._stream_payload(response, target)
        except BaseException:
            response.close()
            target.unlink(missing_ok=True)
            raise
        response.close()
        self.state._temp_payload = target
        return target

    def _stream_payload(self, response, target: Path) -> None:
        written = 0
        deadline = time.monotonic() + DOWNLOAD_DEADLINE_SECONDS
        meter = not self._boundary_metered(response)
        with open(target, 'wb') as stream:
            for chunk in response.iter_content(1024 * 1024):
                if time.monotonic() >= deadline:
                    raise DataFetchError(
                        f'Payload transfer exceeded the {DOWNLOAD_DEADLINE_SECONDS:.0f}s deadline mid-stream.',
                        code='TIMEOUT')
                written += len(chunk)
                if meter:
                    add_metrics_bytes(len(chunk))
                if written > self.spec.max_bytes:
                    raise DataFetchError(
                        f'Payload exceeded the {self.spec.max_bytes // (1024 * 1024)} MB download cap mid-stream.',
                        code='SIZE_LIMIT')
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())

    def _consume_payload_csv(self, **read_kwargs) -> pd.DataFrame:
        """Apply the requested row count while parsing, before materializing the file."""
        temp = self.state._temp_payload
        if not (temp and Path(temp).exists()):
            url = self.spec.url
            if not url:
                raise ValueError('No payload URL available for this fetcher.')
            temp = self._download_to_tempfile(url, headers=self.spec.headers)
        limit = self.state.row_limit if self.state.row_limit and self.state.row_limit > 0 else None
        return pd.read_csv(
            temp, sep=self.spec.separator, compression=self.spec.compression,
            nrows=limit, low_memory=False, **read_kwargs)

