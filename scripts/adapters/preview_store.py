"""Persistent bounded built-in preview store and its human artifact.

Contract (CONTRACTS.md "Persistent built-in preview and primary human
artifact"): a successful built-in ``--preview-only`` run writes one private
task-owned record under the task's ``.previews`` root and returns its opaque
``preview_id`` plus controlled metadata; ``--resume-preview`` claims the
cached request without re-typing source/query; ``--discard-preview`` removes
exactly one record with no network and no source/query.  Frames round-trip
losslessly through the repaired CSV+sidecar codec — no pickle.  Claim is
atomic across processes; a changed request identity, an expired record, a
second claim, forged references and tampered artifacts all fail with coded,
visible errors.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from scripts.errors import DataFetchError
from scripts.adapters.sanitization import write_human_preview

# encode_frame/decode_frame (and their pandas dependency) are imported lazily
# inside create()/load(): ``--discard-preview`` must stay runnable on a bare
# interpreter (``python -S``) with no data stack installed.

_STORE_SCHEMA = 'data-fetcher.preview-store.v1'
_ID_PATTERN = re.compile(r'^pv_[0-9a-f]{24}$')
_META_MAX_BYTES = 64 * 1024


@dataclass(frozen=True)
class PreviewIdentity:
    """The request facets a preview is bound to; any change invalidates it."""
    source: str
    query: str
    columns: tuple[str, ...] = ()
    row_limit: int = 0
    output_format: str = 'csv'


class PreviewRecord:
    """Value object returned by the store to trusted local code only."""

    def __init__(self, preview_id: str, identity: PreviewIdentity, frame,
                 complete: bool, preview_path: Path, directory: Path) -> None:
        self.preview_id = preview_id
        self.identity = identity
        self.frame = frame
        self.complete = complete
        self.preview_path = preview_path
        self._directory = directory

    def _presentation_validated(self) -> Path:
        """Re-validate the artifact at the handoff boundary: containment and
        recorded hash are checked again before anything is returned."""
        meta = json.loads((self._directory / 'meta.json').read_text(encoding='utf-8'))
        artifact = self._directory / str(meta['artifact']['name'])
        resolved_root = self._directory.parent.resolve()
        resolved = artifact.resolve()
        if resolved_root != resolved and resolved_root not in resolved.parents:
            raise DataFetchError('Preview artifact escapes the task store root.',
                                 code='PREVIEW_TAMPERED')
        if not artifact.is_file():
            raise DataFetchError('Preview artifact is missing.', code='PREVIEW_TAMPERED')
        if _sha256_file(artifact) != meta['artifact']['sha256']:
            raise DataFetchError('Preview artifact failed its integrity check.',
                                 code='PREVIEW_TAMPERED')
        return artifact


def _identity_dict(identity: PreviewIdentity) -> dict:
    return {
        'source': identity.source,
        'query': identity.query,
        'columns': tuple(identity.columns),
        'row_limit': int(identity.row_limit),
        'output_format': identity.output_format,
    }


def _identity_from_meta(meta_identity: dict) -> PreviewIdentity:
    """Rebuild with exact types: JSON has no tuples."""
    return PreviewIdentity(
        source=meta_identity['source'],
        query=meta_identity['query'],
        columns=tuple(meta_identity['columns']),
        row_limit=meta_identity['row_limit'],
        output_format=meta_identity['output_format'],
    )


_ALLOWED_COMPRESSION = (None, 'gzip', 'bz2', 'xz', 'zip')


def _bounded_text(value, limit: int) -> str:
    if value is None:
        return ''
    if not isinstance(value, str):
        raise DataFetchError('Preview provenance text must be a string.', code='ARG_INVALID')
    return value[:limit]


def _validated_parse(parse: dict | None) -> dict:
    """Strict provider parse contract for source-bytes payloads."""
    if parse is None:
        return {}
    if not isinstance(parse, dict) or set(parse) - {'sep', 'compression'}:
        raise DataFetchError('Preview parse metadata has unsupported keys.', code='ARG_INVALID')
    sep = parse.get('sep', ',')
    if not isinstance(sep, str) or not 1 <= len(sep) <= 8:
        raise DataFetchError('Preview parse separator must be a short string.', code='ARG_INVALID')
    compression = parse.get('compression')
    if compression not in _ALLOWED_COMPRESSION:
        raise DataFetchError('Preview parse compression is unsupported.', code='ARG_INVALID')
    return {'sep': sep, 'compression': compression}


def _validated_identity(identity) -> PreviewIdentity:
    if not isinstance(identity, PreviewIdentity):
        raise DataFetchError('Preview identity must be a PreviewIdentity.',
                             code='ARG_INVALID')
    if not isinstance(identity.source, str) or not isinstance(identity.query, str):
        raise DataFetchError('Preview identity source/query must be strings.',
                             code='ARG_INVALID')
    if isinstance(identity.row_limit, bool) or not isinstance(identity.row_limit, int) \
            or identity.row_limit < 0:
        raise DataFetchError('Preview identity row_limit must be a non-negative integer.',
                             code='ARG_INVALID')
    if not isinstance(identity.output_format, str) or not identity.output_format:
        raise DataFetchError('Preview identity output_format must be a string.',
                             code='ARG_INVALID')
    if not isinstance(identity.columns, tuple) or not all(
            isinstance(c, str) for c in identity.columns):
        raise DataFetchError('Preview identity columns must be a tuple of strings.',
                             code='ARG_INVALID')
    return identity


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _write_atomic(path: Path, data: bytes) -> None:
    descriptor, temp_name = tempfile.mkstemp(prefix='.dfp_store_', suffix='.tmp',
                                             dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


class PreviewStore:
    """File-backed preview cache under one private task root."""

    def __init__(self, root: Path, ttl_seconds: float = 3600) -> None:
        self.root = Path(root)
        self.ttl_seconds = float(ttl_seconds)

    # -- path helpers -------------------------------------------------------

    def _directory_for(self, preview_id: str) -> Path:
        if not isinstance(preview_id, str) or not _ID_PATTERN.fullmatch(preview_id):
            raise DataFetchError('Preview reference is not a valid preview id.',
                                 code='PREVIEW_NOT_FOUND')
        directory = self.root / preview_id
        resolved_root = self.root.resolve()
        resolved = directory.resolve()
        if resolved_root != resolved and resolved_root not in resolved.parents:
            raise DataFetchError('Preview reference escapes the task store root.',
                                 code='PREVIEW_NOT_FOUND')
        return directory

    def _read_meta(self, directory: Path) -> dict:
        meta_path = directory / 'meta.json'
        try:
            raw = meta_path.read_bytes()
        except OSError as exc:
            raise DataFetchError('Preview record is missing its metadata.',
                                 code='PREVIEW_NOT_FOUND') from exc
        if len(raw) > _META_MAX_BYTES:
            raise DataFetchError('Preview metadata exceeds its size bound.',
                                 code='PREVIEW_TAMPERED')
        try:
            meta = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DataFetchError('Preview metadata is corrupt.', code='PREVIEW_TAMPERED') from exc
        if not isinstance(meta, dict) or meta.get('schema') != _STORE_SCHEMA:
            raise DataFetchError('Preview metadata has an unsupported schema.',
                                 code='PREVIEW_TAMPERED')
        return meta

    # -- public API ---------------------------------------------------------

    def create(self, identity: PreviewIdentity, frame, complete: bool = True,
               payload_bytes: bytes | None = None, parse: dict | None = None,
               source_url: str | None = None, warnings: list | None = None,
               transfer_scope: str | None = None) -> PreviewRecord:
        """Persist one record.

        ``payload_bytes`` (the exact complete source bytes the caller already
        owns) is stored verbatim as the payload of record; otherwise the
        frame crosses through the lossless codec.  ``complete=False`` records
        are honest bounded samples and never stand in for final data.
        ``parse`` records the provider's separator/compression contract for
        source-bytes payloads; ``source_url``/``warnings``/``transfer_scope``
        are the real provenance shown in the human artifact (never the bare
        selector string, never dropped governance notices).
        """
        from scripts.adapters.extractor_protocol import encode_frame
        identity = _validated_identity(identity)
        parse = _validated_parse(parse)
        if payload_bytes is not None and not complete:
            raise DataFetchError('Complete source bytes require a complete record.',
                                 code='ARG_INVALID')
        preview_id = 'pv_' + secrets.token_hex(12)
        directory = self.root / preview_id
        directory.mkdir(parents=True, exist_ok=True)
        try:
            if payload_bytes is not None:
                payload_csv = bytes(payload_bytes)
                payload_kind = 'source_bytes'
                payload_sidecar = b''
            else:
                payload_csv, payload_sidecar = encode_frame(frame)
                payload_kind = 'frame_codec'
            (directory / 'payload.csv').write_bytes(payload_csv)
            (directory / 'payload.sidecar.json').write_bytes(payload_sidecar)
            # The store artifact is the native display payload: a bounded,
            # fully escaped Markdown table that the host renders inside the
            # conversation.  It carries the REAL source link (inert code
            # span) and the real advisories/transfer notes.
            artifact = write_human_preview(
                frame, source_url or identity.query,
                f'Built-in preview ({identity.source})', directory,
                transfer_scope or ('complete transfer' if complete
                                   else 'bounded prefix transfer'),
                warnings or [])
            meta = {
                'schema': _STORE_SCHEMA,
                'preview_id': preview_id,
                'identity': _identity_dict(identity),
                'complete': bool(complete),
                'created': time.time(),
                'ttl_seconds': self.ttl_seconds,
                'claimed': False,
                'artifact': {'name': artifact.name,
                             'sha256': _sha256_file(artifact),
                             'bytes': artifact.stat().st_size},
                'payload_kind': payload_kind,
                'payload': {'csv_sha256': hashlib.sha256(payload_csv).hexdigest(),
                            'sidecar_sha256': hashlib.sha256(payload_sidecar).hexdigest()},
                'parse': parse,
                'source_url': _bounded_text(source_url, 2048),
            }
            _write_atomic(directory / 'meta.json',
                          json.dumps(meta, ensure_ascii=False).encode('utf-8'))
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return PreviewRecord(preview_id, identity, frame, bool(complete),
                             artifact, directory)

    def load(self, preview_id: str) -> PreviewRecord:
        from scripts.adapters.extractor_protocol import decode_frame
        directory = self._directory_for(preview_id)
        meta = self._read_meta(directory)
        identity = _identity_from_meta(meta['identity'])
        payload_path = directory / 'payload.csv'
        sidecar_path = directory / 'payload.sidecar.json'
        try:
            payload_csv = payload_path.read_bytes()
            payload_sidecar = sidecar_path.read_bytes()
        except OSError as exc:
            raise DataFetchError('Preview payload files are missing.',
                                 code='PREVIEW_TAMPERED') from exc
        if hashlib.sha256(payload_csv).hexdigest() != meta['payload']['csv_sha256'] \
                or hashlib.sha256(payload_sidecar).hexdigest() != meta['payload']['sidecar_sha256']:
            raise DataFetchError('Preview payload failed its integrity check.',
                                 code='PREVIEW_TAMPERED')
        artifact = directory / meta['artifact']['name']
        if meta.get('payload_kind') == 'source_bytes':
            # The payload of record is the exact complete source bytes; no
            # typed frame is rebuilt here — the continuation reads and types
            # them itself through the normal pipeline path.
            record = PreviewRecord(meta['preview_id'], identity, None,
                                   bool(meta['complete']), artifact, directory)
            record.payload_bytes = payload_csv
            record.parse = meta.get('parse')
            record.source_url = meta.get('source_url')
            return record
        frame = decode_frame(payload_csv, payload_sidecar)
        record = PreviewRecord(meta['preview_id'], identity, frame,
                               bool(meta['complete']), artifact, directory)
        record.payload_bytes = None
        record.parse = meta.get('parse')
        record.source_url = meta.get('source_url')
        return record

    def claim(self, preview_id: str, identity: PreviewIdentity) -> PreviewRecord:
        record = self.load(preview_id)
        meta = self._read_meta(record._directory)
        if record.identity != _validated_identity(identity):
            raise DataFetchError(
                'The approval does not match this preview; run a fresh preview '
                'and approve again.', code='APPROVAL_MISMATCH')
        age = time.time() - float(meta['created'])
        if age > float(meta['ttl_seconds']):
            raise DataFetchError('The approval preview expired; run a fresh preview.',
                                 code='PREVIEW_EXPIRED')
        marker = record._directory / 'claim.lock'
        try:
            descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise DataFetchError('This preview was already consumed; run a fresh '
                                 'preview for another transfer.',
                                 code='PREVIEW_CONSUMED') from exc
        try:
            os.write(descriptor, str(time.time()).encode('ascii'))
        finally:
            os.close(descriptor)
        return record

    def verify_ready(self, preview_id: str) -> None:
        """Re-validate artifact integrity, containment and record freshness
        at the approval handoff — changed, stale or foreign artifacts fail
        with a controlled code BEFORE any claim, transfer or write."""
        directory = self._directory_for(preview_id)
        meta = self._read_meta(directory)
        artifact = directory / str(meta['artifact']['name'])
        resolved_root = self.root.resolve()
        resolved = artifact.resolve()
        if resolved_root != resolved and resolved_root not in resolved.parents:
            raise DataFetchError('Preview artifact escapes the task store root.',
                                 code='PREVIEW_TAMPERED')
        if not artifact.is_file():
            raise DataFetchError('Preview artifact is missing.', code='PREVIEW_TAMPERED')
        if _sha256_file(artifact) != meta['artifact']['sha256']:
            raise DataFetchError('The stored preview failed its integrity check.',
                                 code='PREVIEW_TAMPERED')
        age = time.time() - float(meta['created'])
        if age > float(meta['ttl_seconds']):
            raise DataFetchError('The approval preview expired; run a fresh preview.',
                                 code='PREVIEW_EXPIRED')

    def write_progress(self, preview_id: str, entry: dict) -> None:
        """Minimal transaction state: the fetch/save phase completed, so a
        later conversion failure can finish from the owned bytes without any
        second transfer or duplicate commit."""
        directory = self._directory_for(preview_id)
        if not isinstance(entry, dict) or set(entry) - {'stage', 'csv_path', 'sha256', 'rows', 'cols'}:
            raise DataFetchError('Recovery receipt has unsupported keys.', code='ARG_INVALID')
        _write_atomic(directory / 'progress.json',
                      json.dumps(entry, ensure_ascii=False).encode('utf-8'))

    def load_progress(self, preview_id: str) -> dict | None:
        directory = self._directory_for(preview_id)
        path = directory / 'progress.json'
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        if len(raw) > _META_MAX_BYTES:
            raise DataFetchError('Recovery receipt exceeds its size bound.',
                                 code='PREVIEW_TAMPERED')
        try:
            entry = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DataFetchError('Recovery receipt is corrupt.', code='PREVIEW_TAMPERED') from exc
        if not isinstance(entry, dict) or set(entry) - {'stage', 'csv_path', 'sha256', 'rows', 'cols'}:
            raise DataFetchError('Recovery receipt has unsupported keys.', code='PREVIEW_TAMPERED')
        return entry

    def clear_progress(self, preview_id: str) -> None:
        directory = self._directory_for(preview_id)
        (directory / 'progress.json').unlink(missing_ok=True)

    def release_claim(self, preview_id: str) -> None:
        """Release a claim after a recoverable failure so one human approval
        is not permanently lost; the record itself is preserved."""
        try:
            directory = self._directory_for(preview_id)
        except DataFetchError:
            return
        (directory / 'claim.lock').unlink(missing_ok=True)

    def discard(self, preview_id: str) -> bool:
        """Remove exactly one owned record; no network, idempotent."""
        try:
            directory = self._directory_for(preview_id)
        except DataFetchError:
            return False
        if not directory.is_dir():
            return False
        shutil.rmtree(directory)
        return True

    def presentation_path(self, preview_id: str) -> Path:
        """Validated artifact path for the native/inline viewer handoff."""
        directory = self._directory_for(preview_id)
        meta = self._read_meta(directory)
        artifact = directory / str(meta['artifact']['name'])
        resolved_root = self.root.resolve()
        resolved = artifact.resolve()
        if resolved_root != resolved and resolved_root not in resolved.parents:
            raise DataFetchError('Preview artifact escapes the task store root.',
                                 code='PREVIEW_TAMPERED')
        if not artifact.is_file():
            raise DataFetchError('Preview artifact is missing.', code='PREVIEW_TAMPERED')
        if _sha256_file(artifact) != meta['artifact']['sha256']:
            raise DataFetchError('Preview artifact failed its integrity check.',
                                 code='PREVIEW_TAMPERED')
        return artifact
