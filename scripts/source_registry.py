"""Saved local extractors. No imports or writes occur during source discovery.

Registry integrity model (Phase 8):

- ``verified_script_bytes`` reads a managed extractor's bytes exactly ONCE,
  refuses symlinked folders/files, verifies the registration SHA-256 and the
  static AST gate on that in-memory buffer, and returns ``(path, bytes)``.
  Callers must keep using the returned bytes — there is no second disk read,
  which closes the historical check-then-use race.
- Legacy v1 descriptors stay readable. A descriptor whose bytes are
  checksum-valid AND AST-clean keeps working inside the isolated worker; a
  descriptor whose bytes fail the AST gate fails closed with an explicit
  ``REGISTRY_INVALID`` review-required error. Bytes are never deleted or
  silently migrated.
- ``pending_previews/`` stores preview receipts together with the exact staged
  extractor bytes so a later approval never re-reads the original file.
- Honest scope: same-user tampering with local state remains out of scope.
  These checks defend against accidents and stale registrations, not against
  a malicious user account.
"""
import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from scripts.config import get_config_paths
from scripts.errors import DataFetchError


def registry_root() -> Path:
    return Path(get_config_paths()[0])


def load_sources() -> dict:
    path = registry_root() / 'sources.json'
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(payload, dict) or payload.get('version') != 1 or not isinstance(payload.get('sources'), dict):
            raise ValueError('unsupported registry structure/version')
        for key, entry in payload['sources'].items():
            if not re.fullmatch(r'custom_[a-z0-9_]{1,48}', key):
                raise ValueError('invalid source key')
            if (not isinstance(entry, dict) or entry.get('script') != key + '.py'
                    or not re.fullmatch(r'[a-f0-9]{64}', entry.get('sha256', ''))
                    or not isinstance(entry.get('name'), str)
                    or not isinstance(entry.get('url'), str)
                    or urlparse(entry.get('url', '')).scheme not in ('http', 'https')
                    or not urlparse(entry['url']).hostname):
                raise ValueError('invalid source descriptor')
        return payload['sources']
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise DataFetchError(f'Cannot read saved sources at {path}: {exc}. Repair or restore that registry.', code='REGISTRY_INVALID') from exc


def _refuse_symlink(path: Path, label: str) -> None:
    if path.is_symlink():
        raise DataFetchError(
            f'{label} is a symlink; managed registry paths must be real files/folders.',
            code='REGISTRY_INVALID')
    is_junction = getattr(path, 'is_junction', None)
    if callable(is_junction):
        try:
            if is_junction():
                raise DataFetchError(
                    f'{label} is a junction; managed registry paths must be real '
                    'files/folders.', code='REGISTRY_INVALID')
        except OSError:  # pragma: no cover - platform dependent
            pass


def verified_script_bytes(entry: dict) -> tuple[Path, bytes]:
    """Single-read verification of a managed extractor.

    Refuses symlinks on ``source_scripts/`` and on the target file, reads the
    bytes ONCE, verifies the registration SHA-256 and the static AST gate on
    that buffer, and returns ``(path, bytes)``. The caller must execute the
    returned bytes — never re-read the path.
    """
    from scripts.adapters.script_runner import ExtractorRejectedError, inspect_extractor_ast
    folder = registry_root() / 'source_scripts'
    if folder.exists():
        _refuse_symlink(folder, f'Managed folder {folder}')
    root = folder.resolve()
    path = (root / entry['script']).resolve()
    if path.parent != root or not path.is_file():
        raise DataFetchError('Saved source script is missing or outside its managed folder.', code='REGISTRY_INVALID')
    _refuse_symlink(folder / entry['script'], f'Managed script {entry["script"]}')
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != entry['sha256']:
        raise DataFetchError('Saved source script changed since registration. Review and register it again.', code='REGISTRY_INVALID')
    try:
        inspect_extractor_ast(data)
    except ExtractorRejectedError as exc:
        raise DataFetchError(
            f'Saved extractor no longer passes the safety review: {exc} '
            'The source is quarantined until it is reviewed and registered again; '
            'the stored bytes were not modified or deleted.',
            code='REGISTRY_INVALID') from exc
    return path, data


def verified_script(entry: dict) -> Path:
    """Compatibility wrapper around :func:`verified_script_bytes` (path only)."""
    return verified_script_bytes(entry)[0]


def validate_registered_origin(registered_url: str, target_url: str) -> None:
    """Block saved-source reuse on a downgraded or moved origin.

    Rejects https→http downgrade, hostname changes, and (within one scheme)
    port changes. An http→https upgrade is allowed — it is strictly stronger.
    """
    registered, target = urlparse(registered_url), urlparse(target_url)
    if registered.scheme not in ('http', 'https') or target.scheme not in ('http', 'https'):
        raise DataFetchError('Saved extractor origins must be http(s).', code='BLOCKED_URL')
    if registered.scheme == 'https' and target.scheme == 'http':
        raise DataFetchError(
            f'Saved extractor is registered for https://{registered.hostname}; refusing the '
            'downgraded http:// origin.', code='BLOCKED_URL')
    if (registered.hostname or '').lower() != (target.hostname or '').lower():
        raise DataFetchError(
            'Saved extractor can only be reused on its registered hostname; use custom for '
            'another platform.', code='BLOCKED_URL')
    if registered.scheme == target.scheme:
        registered_port = registered.port or (443 if registered.scheme == 'https' else 80)
        target_port = target.port or (443 if target.scheme == 'https' else 80)
        if registered_port != target_port:
            raise DataFetchError(
                f'Saved extractor is registered for port {registered_port}; refusing port '
                f'{target_port} on the same host.', code='BLOCKED_URL')


def _harden_dir(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    except OSError:
        pass  # POSIX-style permissions are not applicable on every filesystem


def _harden_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # POSIX-style permissions are not applicable on every filesystem


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _harden_file(path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _as_bytes(script) -> bytes:
    """Accept raw bytes or a path; bytes are read exactly once at this boundary."""
    if isinstance(script, (bytes, bytearray)):
        return bytes(script)
    return Path(script).read_bytes()


def _lock_contents() -> bytes:
    return json.dumps({'pid': os.getpid(),
                       'created_at': datetime.now(timezone.utc).isoformat(timespec='seconds')},
                      ensure_ascii=False).encode('utf-8')


def save_source(name: str, url: str, script) -> str:
    """One writer at a time; a busy registry fails instead of losing another source.

    ``script`` may be raw bytes (already read and verified by the caller) or a
    path; either way the bytes are read once and reused for checksum, gate and
    final write without a second disk read.
    """
    data = _as_bytes(script)
    root = registry_root()
    _harden_dir(root)
    lock = root / 'sources.lock'
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise DataFetchError(f'Source registry is busy ({lock}). Retry after the other registration finishes; after a crashed writer, remove its stale lock.', code='REGISTRY_BUSY') from exc
    try:
        try:
            os.write(fd, _lock_contents())
        finally:
            os.close(fd)
        return _save_source_unlocked(name, url, data)
    finally:
        lock.unlink(missing_ok=True)


def _save_source_unlocked(name: str, url: str, data: bytes) -> str:
    from scripts.adapters.script_runner import ExtractorRejectedError, inspect_extractor_ast
    from scripts.source_catalog import SOURCE_INFO
    from scripts.web_analyzer import validate_public_url
    validate_public_url(url)
    try:
        inspect_extractor_ast(data)
    except ExtractorRejectedError as exc:
        raise DataFetchError(
            f'Extractor bytes do not pass the safety review: {exc}', code='EXTRACTOR_REJECTED') from exc
    sources = load_sources()
    slug = re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')[:40]
    if not slug:
        # Arabic/other Unicode platform names must not all collide as "platform".
        slug = 'platform_' + hashlib.sha256(name.encode('utf-8')).hexdigest()[:12]
    key = 'custom_' + slug
    if key in sources or key in SOURCE_INFO:
        raise DataFetchError(f'Source {key} already exists. Choose another name; existing sources are not replaced.', code='SOURCE_EXISTS')
    scripts_dir = registry_root() / 'source_scripts'
    _harden_dir(scripts_dir)
    target = scripts_dir / (key + '.py')
    if target.exists():
        raise DataFetchError(f'Managed script {target} already exists; choose another name.', code='SOURCE_EXISTS')
    _atomic_write(target, data)
    sources[key] = {'name': name, 'url': url, 'script': target.name,
                    'sha256': hashlib.sha256(data).hexdigest(),
                    'origin': f"{urlparse(url).scheme}://{urlparse(url).netloc}",
                    'registered_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                    'ast_clean': True}
    try:
        _atomic_write(registry_root() / 'sources.json', json.dumps({'version': 1, 'sources': sources}, indent=2, ensure_ascii=False).encode('utf-8'))
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return key


def defer_source_choice(url: str, script) -> str:
    """Keep a checked pending script while a chat agent asks the retention question."""
    token = uuid.uuid4().hex
    root = registry_root() / 'pending_sources'
    _harden_dir(root)
    data = _as_bytes(script)
    _atomic_write(root / (token + '.py'), data)
    try:
        _atomic_write(root / (token + '.json'), json.dumps({'url': url, 'sha256': hashlib.sha256(data).hexdigest()}).encode())
    except BaseException:
        (root / (token + '.py')).unlink(missing_ok=True)
        raise
    return token


def finish_source_choice(token: str, name: str | None = None) -> str | None:
    """Keep or discard only our pending script; never touch dataset output paths.

    The pending bytes are read once, checksum-verified against the pending
    descriptor and handed to ``save_source`` as in-memory bytes — the final
    registration never re-reads the pending file.
    """
    if not re.fullmatch(r'[a-f0-9]{32}', token):
        raise DataFetchError('Invalid pending source ID.', code='ARG_MISSING')
    root = registry_root() / 'pending_sources'
    script, descriptor = root / (token + '.py'), root / (token + '.json')
    try:
        _refuse_symlink(script, f'Pending script {token}.py')
        _refuse_symlink(descriptor, f'Pending descriptor {token}.json')
        meta = json.loads(descriptor.read_text(encoding='utf-8'))
        if not isinstance(meta, dict) or not isinstance(meta.get('url'), str):
            raise ValueError('invalid pending descriptor')
        data = script.read_bytes()
        if hashlib.sha256(data).hexdigest() != meta['sha256']:
            raise ValueError('pending extractor checksum mismatch')
        key = save_source(name, meta['url'], data) if name else None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, DataFetchError):
            raise
        raise DataFetchError(f'Cannot finish pending source choice: {exc}', code='REGISTRY_INVALID') from exc
    script.unlink()
    descriptor.unlink()
    return key


# ---------------------------------------------------------------------------
# Preview receipt store (pending_previews/)
# ---------------------------------------------------------------------------

_PREVIEW_ID_RE = re.compile(r'[a-f0-9]{32}')


def _preview_root() -> Path:
    return registry_root() / 'pending_previews'


def save_preview_receipt(receipt, script_bytes: bytes) -> Path:
    """Persist a preview receipt together with its staged extractor bytes.

    The bytes stored here are the ONLY bytes a later approval will run, so
    ``--approve-preview ID`` never re-reads the original extractor file.
    Expired receipts are pruned on every save (TTL lives on the receipt).
    """
    from scripts.adapters.extractor_protocol import PreviewReceipt
    if not isinstance(receipt, PreviewReceipt):
        raise DataFetchError('save_preview_receipt expects a PreviewReceipt.', code='ARG_MISSING')
    if not isinstance(script_bytes, (bytes, bytearray)):
        raise DataFetchError('save_preview_receipt expects raw script bytes.', code='ARG_MISSING')
    _prune_expired_previews()
    root = _preview_root()
    _harden_dir(root)
    receipt_path = root / (receipt.preview_id + '.json')
    _atomic_write(receipt_path, json.dumps(receipt.to_json(), ensure_ascii=False).encode('utf-8'))
    try:
        _atomic_write(root / (receipt.preview_id + '.py'), bytes(script_bytes))
    except BaseException:
        receipt_path.unlink(missing_ok=True)
        raise
    return receipt_path


def load_preview_receipt(preview_id: str) -> tuple:
    """Load ``(PreviewReceipt, staged_bytes)``; fail closed on expiry/tamper."""
    from scripts.adapters.extractor_protocol import PreviewReceipt
    if not isinstance(preview_id, str) or not _PREVIEW_ID_RE.fullmatch(preview_id):
        raise DataFetchError('Invalid preview ID.', code='ARG_MISSING')
    root = _preview_root()
    receipt_path = root / (preview_id + '.json')
    if not receipt_path.is_file():
        raise DataFetchError(
            f'No pending preview {preview_id}. Run the bounded preview first; receipts '
            'expire after 24 hours.', code='PREVIEW_NOT_FOUND')
    _refuse_symlink(receipt_path, f'Preview receipt {preview_id}.json')
    try:
        receipt = PreviewReceipt.from_json(json.loads(receipt_path.read_text(encoding='utf-8')))
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise DataFetchError(f'Preview receipt {preview_id} is unreadable or tampered: {exc}',
                             code='PREVIEW_TAMPERED') from exc
    if receipt.preview_id != preview_id:
        raise DataFetchError(f'Preview receipt {preview_id} is unreadable or tampered.',
                             code='PREVIEW_TAMPERED')
    if receipt.is_expired():
        raise DataFetchError(
            f'Preview {preview_id} expired; run the bounded preview again to obtain a '
            'fresh sample.', code='PREVIEW_EXPIRED')
    script_path = root / (preview_id + '.py')
    _refuse_symlink(script_path, f'Staged preview script {preview_id}.py')
    try:
        data = script_path.read_bytes()
    except OSError as exc:
        raise DataFetchError(f'Staged preview script {preview_id} is missing: {exc}',
                             code='PREVIEW_TAMPERED') from exc
    if hashlib.sha256(data).hexdigest() != receipt.script_sha256:
        raise DataFetchError(f'Staged preview script {preview_id} does not match its receipt.',
                             code='PREVIEW_TAMPERED')
    return receipt, data


def _prune_expired_previews() -> None:
    from scripts.adapters.extractor_protocol import PreviewReceipt
    root = _preview_root()
    if not root.is_dir():
        return
    for receipt_path in root.glob('*.json'):
        try:
            receipt = PreviewReceipt.from_json(json.loads(receipt_path.read_text(encoding='utf-8')))
            expired = receipt.is_expired()
        except (ValueError, KeyError, TypeError, OSError):
            expired = True  # unreadable junk cannot become valid by waiting
        if expired:
            receipt_path.unlink(missing_ok=True)
            (root / (receipt_path.stem + '.py')).unlink(missing_ok=True)
