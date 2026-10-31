"""Isolated extractor worker (Phase 4).

Run as ``[sys.executable, '-I', isolated_worker.py, job.json]`` from
``scripts/adapters/script_runner.py``. Loads one reviewed extractor whose bytes
were staged by the runner, executes ``extract_frame`` under PEP 578 audit
hooks, and returns the frame as CSV + ``WorkerResult`` JSON — never a pickle.

This module is the LAST link of the receipt-gated execution chain. Extractor
bytes reach this file only after ``scripts/custom_source.py`` has proven human
approval of a bounded preview — the interactive ``_pending_receipt`` minted at
the sample prompt, or the ``--approve-preview ID`` flag validated against the
receipt store. Missing approval fails closed upstream with
``APPROVAL_REQUIRED``; a receipt that drifted from this fetch (staged bytes,
URL, columns, table index) fails with ``APPROVAL_MISMATCH``; an expired
receipt fails with ``PREVIEW_EXPIRED``. The staged bytes are read exactly
once and pinned by SHA-256; this worker re-verifies the hash
(``SCRIPT_HASH_MISMATCH``) and re-runs the static AST gate
(``EXTRACTOR_REJECTED``) before executing anything, so the single
``compile()``/``exec()`` site in ``main()`` below is the only dynamic code
execution site in the entire pipeline.

The parent process adds further layers before this file ever starts: the
AST gate runs pre-spawn, the child environment is scrubbed of
credential-shaped variables with HOME/TMP redirected into the disposable
workspace, stdout/stderr capture is capped at 64 KiB per stream, and the
whole job (interpreter start included) is bounded by the request deadline.

Defense-in-depth layers installed here, in order:

1. Heavy dependencies (pandas/requests/bs4/openpyxl) are pre-imported BEFORE
   any policy exists, so neither imports nor their filesystem reads are
   constrained.
2. Best-effort POSIX ``RLIMIT_CPU`` / ``RLIMIT_NOFILE`` (skipped on platforms
   where the resource module or the limit is unavailable — Windows, and some
   macOS configurations).
3. A PEP 578 audit hook installed BEFORE the target script is loaded. It
   blocks ``subprocess.Popen``/``os.system``/``os.exec*``/``os.spawn*``/``ctypes.*``,
   imports of the AST-banned modules, ``socket.bind``, any ``socket.connect``
   whose target is not a public IP, and filesystem writes outside the worker
   workspace (plus reads outside the workspace, the Python prefixes, and the
   OS TLS/DNS configuration files).

HONEST SCOPE — this is fail-fast runtime defense-in-depth, NOT a sandbox and
NOT hostile-code containment. Same-user code can in principle bypass audit
hooks (e.g. via C extensions or interpreter-internal tricks); descendants and
memory are not bounded portably. The hook blocks naive and hallucinated
dangerous behaviour cheaply; it makes no stronger claim. Note that modules
already cached by the pre-imported dependencies (e.g. ``ctypes`` via numpy)
are caught by their *usage* audit events (``ctypes.dlopen`` and friends)
rather than the ``import`` event — both layers exist for that reason.

PARITY NOTE: ``_is_public_ip`` deliberately duplicates the global-only IP
policy of ``scripts/net_guard.py`` (the pipeline's URL/DNS egress guard on its
own library fetch paths) because this worker runs ``-I`` isolated and must not
import pipeline modules beyond the data protocol. Keep the two in sync (unwrap
``ipv4_mapped``/``sixtofour``/``teredo``, then require ``is_global``).
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 1. Pre-import heavy dependencies before any policy exists.
# ---------------------------------------------------------------------------
_PREIMPORT_WARNINGS: list[str] = []
try:
    import pandas as pd
except ImportError as _exc:  # pragma: no cover - pandas is a hard project dep
    pd = None  # type: ignore[assignment]
    _PREIMPORT_WARNINGS.append(f'pandas unavailable at worker start: {_exc}')
try:
    import requests  # noqa: F401
except ImportError as _exc:  # pragma: no cover
    _PREIMPORT_WARNINGS.append(f'requests unavailable at worker start: {_exc}')
try:
    import bs4  # noqa: F401
except ImportError as _exc:  # pragma: no cover
    _PREIMPORT_WARNINGS.append(f'bs4 unavailable at worker start: {_exc}')
try:
    import openpyxl  # noqa: F401
except ImportError:
    _PREIMPORT_WARNINGS.append('openpyxl not importable; xlsx extraction disabled')

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.adapters.extractor_protocol import (  # noqa: E402
    WorkerRequest, WorkerResult, encode_frame, sha256_bytes,
)
from scripts.adapters.script_runner import (  # noqa: E402
    OUTPUT_NAME, RESULT_NAME, SCRIPT_NAME, SIDECAR_NAME, inspect_extractor_ast,
)
from scripts.core.errors import DataFetchError  # noqa: E402

# ---------------------------------------------------------------------------
# 3. Policy definitions (installed after pre-imports; see main()).
# ---------------------------------------------------------------------------

_BANNED_IMPORTS = ('subprocess', 'ctypes', '_ctypes', 'multiprocessing', 'pty',
                   'webbrowser', 'socketserver')
_BANNED_EVENTS = frozenset({
    'subprocess.Popen', 'os.system', 'os.exec', 'os.spawn', 'os.posix_spawn',
    'os.fork', 'os.forkpty', 'socket.bind',
})
# POSIX-only TLS/DNS configuration the child may need to read.
_POSIX_TLS_DNS_ROOTS = (
    '/etc/ssl', '/etc/pki', '/etc/ca-certificates', '/usr/lib/ssl',
    '/usr/local/etc/ssl', '/usr/share/ca-certificates', '/etc/resolv.conf',
    '/etc/hosts', '/etc/nsswitch.conf', '/etc/services', '/etc/gai.conf',
    '/etc/hosts.equiv', '/etc/krb5.conf',
)
_WRITE_OPEN_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
_PATH_EVENTS = {
    'os.remove': (0,), 'os.rmdir': (0,), 'os.truncate': (0,), 'os.chmod': (0,),
    'os.chown': (0,), 'os.mkdir': (0,), 'os.makedirs': (0,), 'os.unlink': (0,),
}
_RENAME_EVENTS = ('os.rename', 'os.replace', 'os.link', 'os.symlink')


class WorkerPolicyViolation(RuntimeError):
    """Raised by the audit hook when the extractor touches forbidden machinery."""


def _is_public_ip(ip) -> bool:
    """PARITY with scripts/net_guard.py: global-only, unwrapped, classic-blocked."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip.sixtofour is not None:
            ip = ip.sixtofour
        elif ip.teredo is not None:
            server, client = ip.teredo
            return _is_public_ip(server) and _is_public_ip(client)
    try:
        # Classic blocklist in addition to ``is_global``: special ranges whose
        # classification differs across Python versions (notably 100.64.0.0/10
        # shared space, and multicast which recent CPython classifies global)
        # can never slip through. Mirrors scripts/net_guard.py.
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_unspecified or ip.is_multicast):
            return False
        return bool(ip.is_global)
    except ValueError:  # pragma: no cover - malformed address object
        return False


def _resolve_public_targets(address) -> bool:
    """True when every address behind ``address`` is a public IP."""
    if isinstance(address, (str, bytes)):
        # Unix-domain or raw socket targets are never public endpoints.
        return False
    if not isinstance(address, tuple) or not address:
        return False
    host = address[0]
    if isinstance(host, bytes):
        try:
            host = host.decode('idna')
        except UnicodeDecodeError:
            return False
    if not isinstance(host, str) or not host:
        return False
    try:
        candidates = [ipaddress.ip_address(host)]
    except ValueError:
        import socket
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError:
            return False
        candidates = []
        for info in infos:
            try:
                candidates.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                return False
    return bool(candidates) and all(_is_public_ip(candidate) for candidate in candidates)


def _realpath(path) -> str | None:
    if not isinstance(path, (str, bytes, os.PathLike)):
        return None
    try:
        real = os.path.realpath(os.fspath(path))
    except (OSError, ValueError, TypeError):
        return None
    if isinstance(real, bytes):
        real = os.fsdecode(real)
    return real


class _WorkspacePolicy:
    """Filesystem + process policy for one worker job."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = os.path.realpath(workspace)
        self.read_roots = [self.workspace, os.devnull]
        for prefix in {sys.prefix, sys.base_prefix, sys.executable}:
            real = _realpath(prefix)
            if real:
                self.read_roots.append(real)
        if os.name == 'posix':
            # macOS/systemd symlink indirection: /etc -> /private/etc etc. The
            # read check compares realpaths, so allowed roots must be added in
            # realpath form too (falling back to the raw path when unresolved).
            for root in _POSIX_TLS_DNS_ROOTS:
                real = _realpath(root)
                self.read_roots.append(real if real else root)

    def _within(self, path: str, roots: list[str]) -> bool:
        return any(path == root or path.startswith(root + os.sep) for root in roots)

    def write_allowed(self, path) -> bool:
        real = _realpath(path)
        return real is not None and self._within(real, [self.workspace])

    def read_allowed(self, path) -> bool:
        real = _realpath(path)
        return real is not None and self._within(real, self.read_roots)


def _install_audit_hook(policy: _WorkspacePolicy) -> None:
    def hook(event, args):
        if event in _BANNED_EVENTS:
            raise WorkerPolicyViolation(
                f'Extractor blocked from {event} (runtime defense-in-depth, not a sandbox).')
        if event.startswith('ctypes.'):
            raise WorkerPolicyViolation(
                'Extractor blocked from ctypes machinery (runtime defense-in-depth, '
                'not a sandbox).')
        if event == 'import':
            module = args[0] if args else None
            if isinstance(module, str) and any(
                    module == banned or module.startswith(banned + '.')
                    for banned in _BANNED_IMPORTS):
                raise WorkerPolicyViolation(
                    f"Extractor blocked from importing '{module}' (runtime "
                    'defense-in-depth, not a sandbox).')
            return
        if event == 'open':
            path, mode, flags = (list(args) + [None, None, None])[:3]
            if isinstance(path, int):
                return
            write_intent = bool(flags) and isinstance(flags, int) and (flags & _WRITE_OPEN_FLAGS)
            if not write_intent and isinstance(mode, str):
                write_intent = any(ch in mode for ch in ('w', 'a', 'x', '+'))
            if write_intent:
                if not policy.write_allowed(path):
                    raise WorkerPolicyViolation(
                        f'Extractor blocked from writing {path!r} outside the worker '
                        'workspace (runtime defense-in-depth, not a sandbox).')
            else:
                if not policy.read_allowed(path):
                    raise WorkerPolicyViolation(
                        f'Extractor blocked from reading {path!r} outside the worker '
                        'workspace and system configuration (runtime defense-in-depth, '
                        'not a sandbox).')
            return
        if event == 'socket.connect':
            address = args[-1] if args else None
            if not _resolve_public_targets(address):
                raise WorkerPolicyViolation(
                    f'Extractor blocked from connecting to {address!r}: targets must be '
                    'public IP addresses (runtime defense-in-depth, not a sandbox).')
            return
        if event in _PATH_EVENTS:
            for index in _PATH_EVENTS[event]:
                if not policy.write_allowed(args[index] if args else None):
                    raise WorkerPolicyViolation(
                        f'Extractor blocked from {event} outside the worker workspace '
                        '(runtime defense-in-depth, not a sandbox).')
            return
        if event in _RENAME_EVENTS:
            for index in (0, 1):
                if args and index < len(args) and not policy.write_allowed(args[index]):
                    raise WorkerPolicyViolation(
                        f'Extractor blocked from {event} outside the worker workspace '
                        '(runtime defense-in-depth, not a sandbox).')
            return

    sys.addaudithook(hook)


def _apply_posix_rlimits(cpu_seconds: float) -> list[str]:
    """Best-effort POSIX resource limits; returns warnings for anything skipped."""
    warnings: list[str] = []
    if os.name != 'posix':
        warnings.append('POSIX rlimits unavailable on this platform')
        return warnings
    try:
        import resource
    except ImportError:  # pragma: no cover - platform dependent
        warnings.append('resource module unavailable; no CPU/fd limits applied')
        return warnings
    try:
        limit = max(int(cpu_seconds), 1)
        resource.setrlimit(resource.RLIMIT_CPU, (limit, limit + 1))
    except (OSError, ValueError, OverflowError):
        warnings.append('RLIMIT_CPU could not be applied')
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = 256 if hard in (resource.RLIM_INFINITY, -1) else min(256, hard)
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(target, hard), hard))
    except (OSError, ValueError, OverflowError):
        warnings.append('RLIMIT_NOFILE could not be applied')
    return warnings


def _classify_error(exc: BaseException) -> str:
    if isinstance(exc, WorkerPolicyViolation):
        return 'WORKER_POLICY_VIOLATION'
    if isinstance(exc, DataFetchError) and re.fullmatch(r'[A-Z0-9_]{1,64}', exc.code or ''):
        return exc.code
    text = str(exc)
    if 'SIZE_LIMIT' in text:
        return 'SIZE_LIMIT'
    if text.lower().startswith('blocked'):
        return 'BLOCKED_URL'
    if 'NOT_FOUND' in text:
        return 'NOT_FOUND'
    return 'EXTRACTION_FAILED'


def _write_result(workspace: Path, result: WorkerResult) -> None:
    payload = json.dumps(result.to_json(), ensure_ascii=False)
    with open(workspace / RESULT_NAME, 'w', encoding='utf-8') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print('usage: isolated_worker.py job.json', file=sys.stderr)
        return 3
    workspace = Path(argv[1]).resolve().parent
    warnings = list(_PREIMPORT_WARNINGS)
    try:
        with open(argv[1], 'r', encoding='utf-8') as stream:
            raw_job = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        print(f'worker cannot read job: {exc}', file=sys.stderr)
        return 3
    try:
        request = WorkerRequest.from_json(raw_job)
    except Exception as exc:
        print(f'worker rejected job description: {exc}', file=sys.stderr)
        return 3

    warnings.extend(_apply_posix_rlimits(request.deadline_sec))
    policy = _WorkspacePolicy(workspace)
    _install_audit_hook(policy)

    try:
        script_bytes = (workspace / SCRIPT_NAME).read_bytes()
    except OSError as exc:
        _write_result(workspace, WorkerResult(
            error_code='WORKER_SETUP_FAILED', error_summary=str(exc)[:4000],
            warnings=warnings))
        return 0
    if sha256_bytes(script_bytes) != request.script_sha256:
        _write_result(workspace, WorkerResult(
            error_code='SCRIPT_HASH_MISMATCH',
            error_summary='Staged extractor bytes do not match the job sha256.',
            warnings=warnings))
        return 0
    try:
        inspect_extractor_ast(script_bytes)
    except DataFetchError as exc:
        _write_result(workspace, WorkerResult(
            error_code='EXTRACTOR_REJECTED', error_summary=str(exc)[:4000],
            warnings=warnings))
        return 0

    try:
        # THE single dynamic-execution site of the whole pipeline: compile +
        # exec of the receipt-gated, hash-verified extractor entry point.
        # Guards already applied to these exact bytes in THIS process: the
        # SHA-256 re-check and the AST gate above; the PEP 578 audit hook has
        # been active since before the staged script was loaded. Deliberately
        # plain and unobfuscated so code-reading scanners see exactly what
        # runs here; no second site exists anywhere in the pipeline.
        module_vars: dict = {
            '__name__': 'dfp_generated_extractor',
            '__file__': str(workspace / SCRIPT_NAME),
            '__builtins__': __builtins__,
        }
        code = compile(script_bytes.decode('utf-8'), str(workspace / SCRIPT_NAME), 'exec')
        exec(code, module_vars)  # noqa: S102 - the reviewed extractor entry point
        extract = module_vars.get('extract_frame')
        if not callable(extract):
            raise RuntimeError("extractor did not expose a callable 'extract_frame'.")
        frame = extract(request.query_url,
                        columns=list(request.columns) or None,
                        table_index=request.table_index,
                        row_limit=request.row_limit)
        if not isinstance(frame, pd.DataFrame):
            raise RuntimeError('extract_frame must return a pandas DataFrame.')
        csv_bytes, sidecar_bytes = encode_frame(frame)
        (workspace / OUTPUT_NAME).write_bytes(csv_bytes)
        (workspace / SIDECAR_NAME).write_bytes(sidecar_bytes)
        dtypes = [(str(name), str(dtype)) for name, dtype in zip(frame.columns, frame.dtypes)]
        _write_result(workspace, WorkerResult(
            payload_csv=OUTPUT_NAME, payload_sidecar=SIDECAR_NAME, dtypes=dtypes,
            rows=int(len(frame)), warnings=warnings))
        return 0
    except BaseException as exc:  # noqa: BLE001 - the worker owns every failure path
        summary = f'{type(exc).__name__}: {exc}'
        try:
            _write_result(workspace, WorkerResult(
                error_code=_classify_error(exc), error_summary=summary[:4000],
                warnings=warnings))
            return 0
        except OSError:
            return 3  # pragma: no cover - workspace vanished under us


if __name__ == '__main__':
    sys.exit(main(sys.argv))
