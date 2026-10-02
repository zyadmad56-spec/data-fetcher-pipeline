"""Saved local extractors. No imports or writes occur during source discovery."""
import hashlib
import json
import os
import re
import tempfile
import uuid
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


def verified_script(entry: dict) -> Path:
    root = (registry_root() / 'source_scripts').resolve()
    path = (root / entry['script']).resolve()
    if path.parent != root or not path.is_file():
        raise DataFetchError('Saved source script is missing or outside its managed folder.', code='REGISTRY_INVALID')
    if hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
        raise DataFetchError('Saved source script changed since registration. Review and register it again.', code='REGISTRY_INVALID')
    return path


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def save_source(name: str, url: str, script: Path) -> str:
    """One writer at a time; a busy registry fails instead of losing another source."""
    root = registry_root()
    root.mkdir(parents=True, exist_ok=True)
    lock = root / 'sources.lock'
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise DataFetchError(f'Source registry is busy ({lock}). Retry after the other registration finishes; after a crashed writer, remove its stale lock.', code='REGISTRY_BUSY') from exc
    try:
        os.close(fd)
        return _save_source_unlocked(name, url, script)
    finally:
        lock.unlink(missing_ok=True)


def _save_source_unlocked(name: str, url: str, script: Path) -> str:
    from scripts.source_catalog import SOURCE_INFO
    from scripts.web_analyzer import validate_public_url
    validate_public_url(url)
    sources = load_sources()
    slug = re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')[:40]
    if not slug:
        # Arabic/other Unicode platform names must not all collide as "platform".
        slug = 'platform_' + hashlib.sha256(name.encode('utf-8')).hexdigest()[:12]
    key = 'custom_' + slug
    if key in sources or key in SOURCE_INFO:
        raise DataFetchError(f'Source {key} already exists. Choose another name; existing sources are not replaced.', code='SOURCE_EXISTS')
    data = script.read_bytes()
    target = registry_root() / 'source_scripts' / (key + '.py')
    if target.exists():
        raise DataFetchError(f'Managed script {target} already exists; choose another name.', code='SOURCE_EXISTS')
    _atomic_write(target, data)
    sources[key] = {'name': name, 'url': url, 'script': target.name, 'sha256': hashlib.sha256(data).hexdigest()}
    try:
        _atomic_write(registry_root() / 'sources.json', json.dumps({'version': 1, 'sources': sources}, indent=2, ensure_ascii=False).encode('utf-8'))
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return key


def defer_source_choice(url: str, script: Path) -> str:
    """Keep a checked pending script while a chat agent asks the retention question."""
    token = uuid.uuid4().hex
    root = registry_root() / 'pending_sources'
    data = script.read_bytes()
    _atomic_write(root / (token + '.py'), data)
    try:
        _atomic_write(root / (token + '.json'), json.dumps({'url': url, 'sha256': hashlib.sha256(data).hexdigest()}).encode())
    except BaseException:
        (root / (token + '.py')).unlink(missing_ok=True)
        raise
    return token


def finish_source_choice(token: str, name: str | None = None) -> str | None:
    """Keep or discard only our pending script; never touch dataset output paths."""
    if not re.fullmatch(r'[a-f0-9]{32}', token):
        raise DataFetchError('Invalid pending source ID.', code='ARG_MISSING')
    root = registry_root() / 'pending_sources'
    script, descriptor = root / (token + '.py'), root / (token + '.json')
    try:
        meta = json.loads(descriptor.read_text(encoding='utf-8'))
        if not isinstance(meta, dict) or not isinstance(meta.get('url'), str):
            raise ValueError('invalid pending descriptor')
        if hashlib.sha256(script.read_bytes()).hexdigest() != meta['sha256']:
            raise ValueError('pending extractor checksum mismatch')
        key = save_source(name, meta['url'], script) if name else None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, DataFetchError):
            raise
        raise DataFetchError(f'Cannot finish pending source choice: {exc}', code='REGISTRY_INVALID') from exc
    script.unlink()
    descriptor.unlink()
    return key
