"""Bounded HTTP previews and streamed payload cache; no approval policy."""
import os
from pathlib import Path
from typing import Dict, Optional

import pandas as pd

from scripts.core.errors import DataFetchError
from scripts.core.models import FetchState, PayloadSpec
from scripts.core.names import sanitize_name
from scripts.http_utils import add_metrics_bytes


class PayloadReader:
    def __init__(self, state: FetchState, spec: PayloadSpec, request):
        self.state = state
        self.spec = spec
        self.request = request

    def _bounded_csv_preview(self, url: str) -> pd.DataFrame:
        import io
        response = self.request(url, headers=self.spec.headers, stream=True)
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

    def _preview_prefix(self, response) -> tuple[bytearray, bool]:
        payload = bytearray()
        for chunk in response.iter_content(16384):
            add_metrics_bytes(len(chunk))
            remaining = self.spec.preview_bytes - len(payload)
            payload.extend(chunk[:remaining])
            if len(payload) >= self.spec.preview_bytes:
                return payload, False
        return payload, True

    def _complete_record_prefix(self, payload: bytearray) -> bytearray:
        # Parsing a bounded incomplete quoted record must fail rather than invent rows.
        last_newline = payload.rfind(b'\n')
        if last_newline < 0:
            raise DataFetchError('No complete CSV records within the 256 KB preview cap.', code='NOT_FOUND')
        return payload[:last_newline + 1]

    def _download_to_tempfile(self, url: str, headers: Optional[Dict[str, str]] = None) -> Path:
        """Reuse a capped streamed payload instead of downloading it for each phase."""
        target = Path(self.state.outdir) / (sanitize_name(self.state.query) + '_payload.tmp')
        response = self.request(url, headers=headers or self.spec.headers, stream=True)
        try:
            if response.status_code != 200:
                raise ValueError(f'HTTP {response.status_code} while fetching payload.')
            content_length = response.headers.get('Content-Length')
            if content_length and content_length.isdigit() and (int(content_length) > self.spec.max_bytes):
                raise DataFetchError(f'Payload exceeds the {self.spec.max_bytes // (1024 * 1024)} MB download cap (Content-Length: {content_length}).', code='SIZE_LIMIT')
            self._stream_payload(response, target)
        except BaseException:
            response.close()
            target.unlink(missing_ok=True)
            raise
        response.close()
        self.state._temp_payload = target
        return target

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

    def _stream_payload(self, response, target: Path) -> None:
        written = 0
        with open(target, 'wb') as stream:
            for chunk in response.iter_content(1024 * 1024):
                written += len(chunk)
                add_metrics_bytes(len(chunk))
                if written > self.spec.max_bytes:
                    raise DataFetchError(
                        f'Payload exceeded the {self.spec.max_bytes // (1024 * 1024)} MB download cap mid-stream.',
                        code='SIZE_LIMIT')
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
