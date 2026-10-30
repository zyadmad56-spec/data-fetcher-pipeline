"""Static AST gate and subprocess execution of already-verified extractor bytes.

Honest scope (Phase 4): the AST gate is a deterministic fail-fast check and a
code-review aid — NOT a sandbox and NOT proof against hostile code. Aliases
(``import subprocess as sp``), dynamic construction (``getattr(os, 'sys'+'tem')``),
``importlib`` indirection and C extensions all bypass static analysis. The
isolated worker's PEP 578 audit hooks (``scripts/isolated_worker.py``) provide a
second, runtime layer of defense-in-depth; neither layer contains deliberately
hostile same-user code, and neither claims to.
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from scripts.adapters.extractor_protocol import (
    WorkerRequest, WorkerResult, decode_frame, sha256_bytes,
)
from scripts.errors import DataFetchError

__all__ = ['ExtractorRejectedError', 'inspect_extractor_ast', 'SubprocessExtractorRunner']

#: Modules a reviewed data extractor has no legitimate reason to load.
BANNED_MODULES = frozenset({
    'subprocess', 'ctypes', '_ctypes', 'multiprocessing', 'pty', 'webbrowser',
    'socketserver',
})
#: Builtins a reviewed data extractor has no legitimate reason to call.
BANNED_CALL_NAMES = frozenset({'eval', 'exec', 'compile', '__import__'})
#: Dangerous ``os`` attribute calls (prefix match covers os.exec*/os.spawn*).
BANNED_OS_ATTRS_PREFIX = ('exec', 'spawn')
BANNED_OS_ATTRS_EXACT = frozenset({'system', 'popen'})

SCRIPT_NAME = 'extractor.py'
JOB_NAME = 'job.json'
RESULT_NAME = 'result.json'
OUTPUT_NAME = 'output.csv'
SIDECAR_NAME = 'sidecar.json'

#: 64 KiB-capped stdout/stderr capture (a flood is drained and discarded).
STDOUT_CAP_BYTES = 64 * 1024
STDERR_CAP_BYTES = 64 * 1024

#: Variables redirected into the sandbox workspace.
_SANDBOX_HOME_VARS = ('HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA',
                      'XDG_CONFIG_HOME', 'XDG_CACHE_HOME')
_SANDBOX_TMP_VARS = ('TMP', 'TEMP', 'TMPDIR')
_THREAD_LIMIT_VARS = {'OMP_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1',
                      'MKL_NUM_THREADS': '1'}
# Match credential-ish variable names on underscore-delimited segments:
# GITHUB_TOKEN, AWS_SECRET_ACCESS_KEY, API_KEY, KAGGLE_KEY, AUTH_TOKEN, ...
_CREDENTIAL_PATTERN = re.compile(
    r'(?:^|_)(?:TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?|API_?KEY|KEY|AUTH|'
    r'COOKIE|SESSION|PRIVATEKEY|PRIVATE_KEY|ACCESS_KEY)(?:$|_)', re.IGNORECASE)


class ExtractorRejectedError(DataFetchError):
    """Raised when a candidate extractor fails the static AST gate."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code='EXTRACTOR_REJECTED')


def _banned_module(name: str | None) -> bool:
    if not name:
        return False
    return name in BANNED_MODULES or any(
        name == banned or name.startswith(banned + '.')
        for banned in BANNED_MODULES if '.' not in banned)


def inspect_extractor_ast(script_bytes: bytes) -> None:
    """Fail fast on a candidate extractor that references forbidden machinery.

    Requires a top-level ``def extract_frame`` and rejects imports or calls of
    ``subprocess``, ``ctypes``, ``_ctypes``, ``multiprocessing``, ``pty``,
    ``webbrowser``, ``socketserver``, ``eval``, ``exec``, ``compile``,
    ``__import__`` and ``os.system``/``os.popen``/``os.exec*``/``os.spawn*``.

    This is a fail-fast gate and review aid, NOT a sandbox: aliases, dynamic
    attribute construction, importlib indirection and C extensions are not
    visible to static analysis. In other words the gate is fail-fast — it is
    not a sandbox and makes no containment claim. Runtime defense-in-depth
    lives in the isolated worker's audit hooks. Raises
    :class:`ExtractorRejectedError`.
    """
    try:
        tree = ast.parse(script_bytes.decode('utf-8'))
    except (SyntaxError, UnicodeDecodeError) as exc:
        raise ExtractorRejectedError(f'Extractor is not valid UTF-8 Python source: {exc}') from exc

    if not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name == 'extract_frame' for node in tree.body):
        raise ExtractorRejectedError(
            "Extractor must define a top-level 'def extract_frame(...)' entry point.")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _banned_module(alias.name):
                    raise ExtractorRejectedError(
                        f"Extractor imports '{alias.name}', which is not allowed for reviewed "
                        'data extractors (fail-fast gate; not a sandbox).')
        elif isinstance(node, ast.ImportFrom):
            if _banned_module(node.module):
                raise ExtractorRejectedError(
                    f"Extractor imports from '{node.module}', which is not allowed for "
                    'reviewed data extractors (fail-fast gate; not a sandbox).')
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in BANNED_CALL_NAMES:
                raise ExtractorRejectedError(
                    f"Extractor calls '{func.id}()', which is not allowed for reviewed data "
                    'extractors (fail-fast gate; not a sandbox).')
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) \
                    and func.value.id == 'os':
                banned = func.attr in BANNED_OS_ATTRS_EXACT or \
                    any(func.attr.startswith(prefix) for prefix in BANNED_OS_ATTRS_PREFIX)
                if banned:
                    raise ExtractorRejectedError(
                        f"Extractor calls 'os.{func.attr}()', which is not allowed for "
                        'reviewed data extractors (fail-fast gate; not a sandbox).')


def _sandboxed_env(workspace: Path) -> dict[str, str]:
    """Child environment: credentials stripped, home/tmp pointed at the sandbox."""
    child = {name: value for name, value in os.environ.items()
             if not _CREDENTIAL_PATTERN.search(name)}
    home = str(workspace / 'home')
    tmp = str(workspace / 'tmp')
    for name in _SANDBOX_HOME_VARS:
        child[name] = home
    for name in _SANDBOX_TMP_VARS:
        child[name] = tmp
    child.update(_THREAD_LIMIT_VARS)
    return child


def _chmod_quiet(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass  # POSIX-style permissions are not applicable on every filesystem


def _drain(stream, sink: bytearray, cap: int) -> None:
    """Read a child pipe until EOF, keeping at most ``cap`` bytes."""
    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            room = cap - len(sink)
            if room > 0:
                sink.extend(chunk[:room])
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _kill_process(proc: subprocess.Popen) -> None:
    """Kill the child (and its process group on POSIX) best-effort."""
    if proc.poll() is not None:
        return
    try:
        if os.name == 'posix':
            import signal
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                return
            except (ProcessLookupError, PermissionError, OSError):
                pass
        proc.kill()
    except (OSError, AttributeError, ValueError):
        pass


class SubprocessExtractorRunner:
    """Runs already-verified extractor bytes inside one disposable workspace.

    Contract: the caller passes in-memory script bytes it has ALREADY verified
    (checksum + :func:`inspect_extractor_ast`). The runner stages those exact
    bytes into a ``0o700`` temp workspace, scrubs the child environment, and
    spawns ``[sys.executable, '-I', worker, job.json]`` with ``shell=False``,
    ``stdin=DEVNULL`` and ``close_fds=True``. The whole job (interpreter start
    included) is bounded by ``request.deadline_sec`` wall-clock; the child is
    killed on timeout. stdout/stderr capture is capped at 64 KiB each; a flood
    is drained and discarded, never buffered unboundedly.

    This is process isolation for accident containment and credential
    reduction, NOT a kernel sandbox; see the module docstring.
    """

    def __init__(self, worker_path: str | os.PathLike | None = None,
                 deadline_sec: float = 120.0,
                 stdout_cap_bytes: int = STDOUT_CAP_BYTES,
                 stderr_cap_bytes: int = STDERR_CAP_BYTES) -> None:
        default_worker = Path(__file__).resolve().parent.parent / 'isolated_worker.py'
        self.worker_path = Path(worker_path) if worker_path else default_worker
        self.default_deadline_sec = float(deadline_sec)
        self.stdout_cap_bytes = int(stdout_cap_bytes)
        self.stderr_cap_bytes = int(stderr_cap_bytes)
        #: Captures of the most recent run (bytes, already capped).
        self.last_stdout: bytes = b''
        self.last_stderr: bytes = b''
        #: Payload bytes of the most recent run (read before workspace removal).
        self.last_payload_csv: bytes = b''
        self.last_payload_sidecar: bytes = b''

    def extract(self, script_bytes: bytes, request: WorkerRequest):
        """Run the extractor and decode its frame.

        Returns ``(WorkerResult, DataFrame | None)``; the frame is present only
        for successful runs. Payload decode failures surface as coded
        ``DataFetchError`` (``IPC_PAYLOAD_CORRUPT`` / ``IPC_SCHEMA_INVALID``).
        """
        result = self.run(script_bytes, request)
        if result.error_code is not None or not result.payload_csv:
            return result, None
        return result, decode_frame(self.last_payload_csv, self.last_payload_sidecar)

    def run(self, script_bytes: bytes, request: WorkerRequest) -> WorkerResult:
        """Execute ``script_bytes`` for ``request`` and return the worker result."""
        # Strict round-trip: a malformed request must fail here, not in the child.
        request = WorkerRequest.from_json(request.to_json())
        if request.script_sha256 != sha256_bytes(script_bytes):
            raise DataFetchError('Runner request does not match the staged extractor bytes.',
                                 code='INTERNAL')
        self.last_payload_csv = b''
        self.last_payload_sidecar = b''
        workspace = Path(tempfile.mkdtemp(prefix='dfp-worker-'))
        _chmod_quiet(workspace, 0o700)
        try:
            self._stage(workspace, script_bytes, request)
            result = self._execute(workspace, request)
            self._absorb_payload(workspace, result)
            return result
        except OSError as exc:
            return WorkerResult(error_code='WORKER_SETUP_FAILED',
                                error_summary=f'Worker workspace could not be prepared: {exc}')
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def _absorb_payload(self, workspace: Path, result: WorkerResult) -> None:
        """Read the worker's payload into memory before the workspace vanishes."""
        if result.error_code is not None or not result.payload_csv:
            return
        try:
            self.last_payload_csv = (workspace / result.payload_csv).read_bytes()
            self.last_payload_sidecar = (workspace / result.payload_sidecar).read_bytes()
        except OSError:
            self.last_payload_csv = b''
            self.last_payload_sidecar = b''

    def _stage(self, workspace: Path, script_bytes: bytes, request: WorkerRequest) -> None:
        (workspace / 'tmp').mkdir(exist_ok=True)
        (workspace / 'home').mkdir(exist_ok=True)
        script_path = workspace / SCRIPT_NAME
        with open(script_path, 'wb') as stream:  # noqa: SIM115 - staged file, not user data
            stream.write(script_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        _chmod_quiet(script_path, 0o600)
        job = workspace / JOB_NAME
        with open(job, 'w', encoding='utf-8') as stream:
            json.dump(request.to_json(), stream)
        _chmod_quiet(job, 0o600)

    def _execute(self, workspace: Path, request: WorkerRequest) -> WorkerResult:
        argv = [sys.executable, '-I', str(self.worker_path), str(workspace / JOB_NAME)]
        stdout: bytearray = bytearray()
        stderr: bytearray = bytearray()
        try:
            popen_kwargs: dict = {
                'stdin': subprocess.DEVNULL,
                'stdout': subprocess.PIPE,
                'stderr': subprocess.PIPE,
                'shell': False,
                'close_fds': True,
                'cwd': str(workspace),
                'env': _sandboxed_env(workspace),
            }
            if os.name == 'posix':
                # New session lets the runner kill the whole child tree.
                popen_kwargs['start_new_session'] = True
            proc = subprocess.Popen(argv, **popen_kwargs)
        except OSError as exc:
            return WorkerResult(error_code='WORKER_SETUP_FAILED',
                                error_summary=f'Worker process could not start: {exc}')
        threads = [
            threading.Thread(target=_drain, args=(proc.stdout, stdout, self.stdout_cap_bytes),
                             daemon=True),
            threading.Thread(target=_drain, args=(proc.stderr, stderr, self.stderr_cap_bytes),
                             daemon=True),
        ]
        for thread in threads:
            thread.start()
        deadline = float(request.deadline_sec)
        timed_out = False
        try:
            proc.wait(timeout=deadline)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_process(proc)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - kill escalation failed
                pass
        for thread in threads:
            thread.join(timeout=10)
        self.last_stdout = bytes(stdout)
        self.last_stderr = bytes(stderr)
        return self._collect(workspace, timed_out, deadline)

    def _collect(self, workspace: Path, timed_out: bool, deadline: float) -> WorkerResult:
        result_path = workspace / RESULT_NAME
        if timed_out and not result_path.exists():
            return WorkerResult(error_code='WORKER_TIMEOUT', error_summary=(
                f'Extractor exceeded its {deadline:g}s wall-clock budget and was killed.'))
        if not result_path.exists():
            excerpt = self.last_stderr.decode('utf-8', errors='replace')[-2000:]
            return WorkerResult(error_code='WORKER_CRASHED', error_summary=(
                'Worker produced no result (interpreter or import failure). '
                f'Stderr excerpt: {excerpt or "(empty)"}'))
        try:
            result = WorkerResult.from_json(json.loads(result_path.read_text(encoding='utf-8')))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError, DataFetchError) as exc:
            return WorkerResult(error_code='WORKER_RESULT_INVALID', error_summary=(
                f'Worker result failed strict validation: {exc}'))
        if timed_out and result.error_code is None:
            result = WorkerResult(error_code='WORKER_TIMEOUT', error_summary=(
                f'Extractor exceeded its {deadline:g}s wall-clock budget and was killed.'),
                warnings=result.warnings)
        return result
