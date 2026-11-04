"""Independent production-entry HTTP, preview and installed-resource regressions.

All network fixtures are owned loopback sockets. Artificial blocking workers use
the exported transport constructor, and run in disposable outer probes so a
broken supervisor cannot hang pytest or leave an owned child running.
"""
from __future__ import annotations

import ctypes
import gzip
import importlib.util
import json
import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
BUDGET = 1.5
PREFIX_BUDGET = 5.0


def _running(pid):
    if os.name == "nt":
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class _HTTPFixture:
    def __init__(self):
        self.stop = threading.Event()
        self.started = threading.Event()
        self.connections = []
        self.requests = 0
        self.lock = threading.Lock()

    def closed(self):
        with self.lock:
            connections = list(self.connections)
        if not connections:
            return False
        for connection in connections:
            try:
                if connection.fileno() < 0:
                    continue
                readable, _, _ = select.select([connection], [], [], 0.5)
                if not readable or connection.recv(1, socket.MSG_PEEK):
                    return False
            except (OSError, ValueError):
                pass
        return True


@pytest.fixture
def local_http():
    fixture = _HTTPFixture()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            with fixture.lock:
                fixture.connections.append(self.connection)
                fixture.requests += 1
                request_number = fixture.requests
            fixture.started.set()
            try:
                if self.path == "/headers":
                    if fixture.stop.wait(4.0):
                        return
                    self._headers(13)
                    self.wfile.write(b"id,value\n1,2\n")
                elif self.path == "/drip":
                    self._headers(20)
                    for _ in range(20):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        if fixture.stop.wait(0.2):
                            return
                elif self.path == "/prefix":
                    # A usable prefix arrives now; the large remaining dataset
                    # does not. Prefix mode must not await or reject that tail.
                    self._headers(1024 * 1024)
                    self.wfile.write(b"id,value\n" + b"1,2\n" * 4096)
                    self.wfile.flush()
                    fixture.stop.wait()
                elif self.path == "/prefix-stalled-tail":
                    self._headers(1024 * 1024)
                    self.wfile.write(b"p" * 128)
                    self.wfile.flush()
                    fixture.stop.wait()
                elif self.path == "/latin1-gzip":
                    body = gzip.compress(b"name\ncaf\xe9\n")
                    self.send_response(200)
                    self.send_header("content-type", "text/csv; charset=iso-8859-1")
                    self.send_header("Content-Encoding", "gzip")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    self.wfile.flush()
                elif self.path == "/exact-prefix":
                    self._headers(128)
                    self.wfile.write(b"a" * 128)
                    self.wfile.flush()
                elif self.path == "/retry" and request_number == 1:
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif self.path in ("/always429", "/always503"):
                    status = 429 if self.path == "/always429" else 503
                    self.send_response(status)
                    self.send_header("Content-Length", "3")
                    self.send_header("Retry-After", "0.01")
                    self.end_headers()
                    self.wfile.write(b"err")
                    self.wfile.flush()
                elif self.path == "/drop-once" and request_number == 1:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                else:
                    self._headers(13)
                    self.wfile.write(b"id,value\n1,2\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        def _headers(self, length):
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Length", str(length))
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    fixture.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=lambda: server.serve_forever(0.02), daemon=True)
    thread.start()
    try:
        yield fixture
    finally:
        fixture.stop.set()
        server.shutdown()
        with fixture.lock:
            connections = list(fixture.connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        server.server_close()
        thread.join(timeout=1)


_PROBE = r'''
import json, os, socket, subprocess, sys, tempfile, time
from pathlib import Path

job = json.loads(sys.stdin.read())
sys.path.insert(0, job['repo'])
tempfile.tempdir = job['spools']
from scripts import http_utils
from scripts.adapters import payload
from scripts.core.models import FetchState, PayloadSpec
from scripts.adapters.http_transport import SupervisedHTTPTransport
from scripts.adapters.http_transport import HTTPRequest

# Disable ambient proxy routing only on this fixture's trusted loopback client.
# The public-only production session and all IP checks are left intact.
http_utils.session.trust_env = False
payload.DOWNLOAD_DEADLINE_SECONDS = job['budget']
operation = job['operation']
kwargs = dict(deadline_sec=job['budget'], timeout=10, max_retries=1)
result = {}
response = None
Path(job['ready_file']).write_text('ready', encoding='ascii')
started = time.monotonic()
http_utils.reset_metrics()
try:
    if operation == 'pacing':
        kwargs['transport'] = SupervisedHTTPTransport(worker_path=Path(job['worker']))
        kwargs['public_only'] = True
        def parent_dns_forbidden(*args, **kwargs):
            raise AssertionError('Pacing fixture must not access an external resolver')
        socket.getaddrinfo = parent_dns_forbidden
        first = http_utils.request_with_retry(job['url'], **kwargs)
        try:
            assert first.status_code == 200 and first.content == b'id,value\n1,2\n'
        finally:
            first.close()
        started = time.monotonic()
        response = http_utils.request_with_retry(job['url'], **kwargs)
    elif operation in ('dns', 'stdin', 'cancel'):
        kwargs['transport'] = SupervisedHTTPTransport(worker_path=Path(job['worker']))
        kwargs['public_only'] = True
        def parent_dns_forbidden(*args, **kwargs):
            raise AssertionError('Parent DNS ran before supervised transport')
        socket.getaddrinfo = parent_dns_forbidden
        if operation == 'stdin':
            kwargs['headers'] = {'X-Owned-Fixture': 'x' * (60 * 1024)}
        if operation == 'cancel':
            real_popen = subprocess.Popen
            class InterruptingPopen(real_popen):
                def wait(self, timeout=None):
                    if not getattr(self, 'fixture_interrupted', False):
                        until = time.monotonic() + 1
                        while not Path(job['pid_file']).exists() and time.monotonic() < until:
                            time.sleep(0.01)
                        self.fixture_interrupted = True
                        raise KeyboardInterrupt('Owned fixture cancellation at OS wait boundary')
                    return super().wait(timeout=timeout)
            subprocess.Popen = InterruptingPopen
        response = http_utils.request_with_retry(job['url'], **kwargs)
    elif operation == 'transport-content':
        response = SupervisedHTTPTransport().fetch(HTTPRequest(
            url=job['url'], deadline_sec=job['budget'], public_only=False,
            max_retries=1, max_body_bytes=1024))
    elif operation.startswith('request'):
        kwargs['public_only'] = False
        kwargs['stream'] = job.get('stream', False)
        if operation == 'request-prefix':
            kwargs['prefix_bytes'] = 128
            kwargs['max_body_bytes'] = 128
        if operation == 'request-retry':
            kwargs['max_retries'] = 2
            kwargs['backoff_base'] = 0.01
        response = http_utils.request_with_retry(job['url'], **kwargs)
    else:
        state = FetchState(query='owned-records', outdir=job['payloads'])
        spec = PayloadSpec(job['url'], {}, ',', None, 128, 1024)
        def trusted_loopback_request(url, **options):
            assert url.startswith(job['base_url'] + '/')
            # PayloadReader's HTTP system boundary is genuinely exercised;
            # only the fixture-owned endpoint opts out of public IP policy.
            options['public_only'] = False
            options['timeout'] = 10
            options['max_retries'] = 1
            if operation == 'download-metrics':
                options['max_retries'] = 2
                options['backoff_base'] = 0.01
            return http_utils.request_with_retry(url, **options)
        reader = payload.PayloadReader(state, spec, trusted_loopback_request)
        if operation == 'preview':
            frame = reader._bounded_csv_preview(job['url'])
            result['rows'] = frame.to_dict(orient='records')
            result['cached'] = state._temp_payload is not None
        else:
            target = reader._download_to_tempfile(job['url'])
            result['body'] = target.read_bytes().decode('ascii')
            target.unlink()
        result['status'] = 'ok'
    if response is not None:
        result.update(status='ok', status_code=response.status_code,
                      body=response.text, body_hex=response.content.hex(),
                      body_complete=getattr(response, 'body_complete', None))
except KeyboardInterrupt:
    result.update(status='cancelled')
except BaseException as exc:
    result.update(status='error', error_type=type(exc).__name__,
                  code=getattr(exc, 'code', None), message=str(exc))
finally:
    if response is not None:
        response.close()
        response.close()
    result['elapsed'] = time.monotonic() - started
    result['metrics'] = http_utils.get_metrics()
print(json.dumps(result))
'''


def _probe(tmp_path, operation, url, *, stream=False, base_url="", worker=None, budget=BUDGET):
    spools = tmp_path / "spools"
    payloads = tmp_path / "payloads"
    spools.mkdir()
    payloads.mkdir()
    pid_file = tmp_path / "worker.pid"
    ready_file = tmp_path / "probe.ready"
    job = dict(repo=str(REPO), operation=operation, url=url, stream=stream,
               budget=budget, spools=str(spools), payloads=str(payloads),
               base_url=base_url, worker=str(worker or ""), pid_file=str(pid_file),
               ready_file=str(ready_file))
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
           "TMP": str(spools), "TEMP": str(spools), "TMPDIR": str(spools)}
    proc = subprocess.Popen([sys.executable, "-B", "-c", _PROBE], cwd=tmp_path,
                            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        # Fixture interpreter/data-stack startup is outside the product call's
        # clock. Its readiness marker prevents a cold pandas import from being
        # mistaken for a production deadline failure. The supervised worker's
        # own startup remains inside request_with_retry's absolute budget.
        proc.stdin.write(json.dumps(job))
        proc.stdin.close()
        proc.stdin = None
        startup_deadline = time.monotonic() + 15
        while not ready_file.exists() and proc.poll() is None and time.monotonic() < startup_deadline:
            time.sleep(0.01)
        try:
            stdout, stderr = proc.communicate(timeout=budget + 2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate(timeout=2)
            pytest.fail("Production entry hung beyond its absolute deadline; outer probe killed")
        assert proc.returncode == 0, f"Probe crashed: {stderr} {stdout}"
        result = json.loads(stdout)
        if pid_file.exists():
            pid = int(pid_file.read_text(encoding="ascii"))
            result['worker_started'] = True
            result['worker_alive'] = _running(pid)
        else:
            result['worker_started'] = False
        result['spools'] = [entry.name for entry in spools.iterdir()]
        result['payloads'] = [entry.name for entry in payloads.iterdir()]
        return result
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)
        if pid_file.exists():
            pid = int(pid_file.read_text(encoding="ascii"))
            if _running(pid):
                os.kill(pid, signal.SIGTERM)


@pytest.mark.parametrize(("route", "stream"), [("/headers", False), ("/drip", False), ("/drip", True)])
def test_production_request_bounds_headers_and_entire_stream(tmp_path, local_http, route, stream):
    # 2026-11-03: standalone supervisor tests passed while this entry bypassed it.
    result = _probe(tmp_path, "request", local_http.url + route, stream=stream)
    assert result.get("code") == "TIMEOUT", result
    assert result["elapsed"] < BUDGET + 1.5, result
    assert local_http.started.is_set()
    assert local_http.closed(), "Deadline left an active HTTP socket"
    assert result["spools"] == []


def test_payload_download_bounds_blocking_stream_and_cleans_partial_file(tmp_path, local_http):
    result = _probe(tmp_path, "download", local_http.url + "/drip", base_url=local_http.url)
    assert result.get("code") == "TIMEOUT", result
    assert result["elapsed"] < BUDGET + 1.5, result
    assert local_http.closed()
    assert result["spools"] == [] and result["payloads"] == [], result


@pytest.mark.parametrize("operation", ["request-prefix", "preview"])
def test_preview_prefix_returns_before_large_body_without_full_size_rejection(tmp_path, local_http, operation):
    # Allow for a cold worker on slow runners. The server holds the tail
    # until teardown, so success still proves the request did not wait for it.
    result = _probe(tmp_path, operation, local_http.url + "/prefix",
                    base_url=local_http.url, budget=PREFIX_BUDGET)
    assert result["status"] == "ok", result
    assert result["elapsed"] < PREFIX_BUDGET + 1.0, result
    if operation == "request-prefix":
        assert result["body"].encode("ascii") == (b"id,value\n" + b"1,2\n" * 4096)[:128]
        assert result["body_complete"] is False, result
    else:
        assert result["rows"] == [{"id": 1, "value": 2}] * 5
        assert result["cached"] is False, "A prefix was incorrectly marked as the complete cached dataset"
    assert local_http.closed()
    assert result["spools"] == [] and result["payloads"] == [], result


def test_production_small_body_close_releases_owned_spool(tmp_path, local_http):
    result = _probe(tmp_path, "request", local_http.url + "/small", stream=True)
    assert result["status"] == "ok" and result["status_code"] == 200, result
    assert result["body"] == "id,value\n1,2\n"
    assert result["body_complete"] is True, result
    assert result["spools"] == []
    assert local_http.closed()


@pytest.mark.parametrize("deadline", [float("inf"), float("nan"), 0.0], ids=["infinite", "nan", "zero"])
def test_invalid_deadline_is_rejected_before_worker_start(deadline, tmp_path, monkeypatch):
    from scripts.adapters.http_transport import HTTPRequest, SupervisedHTTPTransport
    from scripts.errors import DataFetchError

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    def forbidden_spawn(*args, **kwargs):
        raise AssertionError("Invalid deadline reached the OS process-spawn boundary")

    monkeypatch.setattr(subprocess, "Popen", forbidden_spawn)
    with pytest.raises(DataFetchError) as caught:
        SupervisedHTTPTransport().fetch(HTTPRequest(
            url="https://owned.invalid/data.csv", deadline_sec=deadline))
    assert caught.value.code == "TIMEOUT"
    assert not list(tmp_path.iterdir()), "Invalid request created an owned spool"


def test_supervised_gzip_body_preserves_lowercase_declared_latin1(tmp_path, local_http):
    result = _probe(tmp_path, "transport-content", local_http.url + "/latin1-gzip")
    assert result["status"] == "ok", result
    assert result["body_hex"] == b"name\ncaf\xe9\n".hex(), result
    assert result["body"] == "name\ncaf\u00e9\n", result
    assert local_http.closed()
    assert result["spools"] == []


def test_real_payload_retry_reports_actual_requests_and_counts_transfer_once(tmp_path, local_http):
    # Two real cold Windows interpreters need startup room. This scenario
    # checks accounting; hard-deadline scenarios retain the 1.5-second budget.
    result = _probe(tmp_path, "download-metrics", local_http.url + "/retry", base_url=local_http.url, budget=5.0)
    assert result["status"] == "ok" and result["body"] == "id,value\n1,2\n", result
    assert local_http.requests == 2, "Fixture did not observe the actual retry"
    assert result["metrics"] == {"requests": 2, "retries": 1, "bytes": 13}, result
    assert local_http.closed()
    assert result["spools"] == [] and result["payloads"] == [], result


def test_exact_prefix_matching_declared_body_is_complete_without_extra_read(tmp_path, local_http):
    result = _probe(tmp_path, "request-prefix", local_http.url + "/exact-prefix")
    assert result["status"] == "ok" and result["body"] == "a" * 128, result
    assert result["body_complete"] is True, "Matching full Content-Length proves this prefix is the complete body"
    assert local_http.closed()
    assert result["spools"] == []


@pytest.mark.parametrize(("route", "code"), [("/always429", "RATE_LIMITED"), ("/always503", "PROVIDER_ERROR")])
def test_exhausted_http_status_retries_raise_coded_error_and_release_all_bodies(tmp_path, local_http, route, code):
    result = _probe(tmp_path, "request-retry", local_http.url + route, budget=5.0)
    assert result.get("code") == code, result
    assert local_http.requests == 2, "Configured attempts did not reach the HTTP boundary"
    assert result["metrics"] == {"requests": 2, "retries": 1, "bytes": 6}, result
    assert local_http.closed()
    assert result["spools"] == [], result


def test_transient_connection_drop_retries_at_real_http_boundary(tmp_path, local_http):
    result = _probe(tmp_path, "request-retry", local_http.url + "/drop-once", budget=5.0)
    assert result["status"] == "ok" and result["body"] == "id,value\n1,2\n", result
    assert local_http.requests == 2, "Transient NETWORK failure bypassed the retry policy"
    assert local_http.closed()
    assert result["spools"] == [], result


def test_prefix_ready_at_cap_returns_without_reading_stalled_tail(tmp_path, local_http):
    result = _probe(tmp_path, "request-prefix", local_http.url + "/prefix-stalled-tail",
                    budget=PREFIX_BUDGET)
    assert result["status"] == "ok" and result["body"] == "p" * 128, result
    assert result["body_complete"] is False, "The unseen tail must not become a complete dataset"
    assert result["elapsed"] < PREFIX_BUDGET + 1.0, result
    assert local_http.closed()
    assert result["spools"] == [], result


def test_existing_host_pacing_cannot_outlive_next_request_deadline(tmp_path):
    pid_file = tmp_path / "worker.pid"
    worker = tmp_path / "owned_pacing_http_worker.py"
    worker.write_text(
        "import json, os, sys\nfrom pathlib import Path\n"
        f"Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding='ascii')\n"
        "job=json.loads(sys.stdin.read())\n"
        "body=b'id,value\\n1,2\\n'\n"
        "Path(job['spool_path']).write_bytes(body)\n"
        "print(json.dumps({'status':'ok','status_code':200,'headers':{'Content-Length':str(len(body))},"
        "'url':job['url'],'body_complete':True,'bytes':len(body)}))\n",
        encoding="utf-8",
    )
    # The real provider's existing six-second policy is exercised in the
    # parent, while the exported fixed-worker seam prevents any external DNS
    # or HTTP access. The first real worker naturally seeds pacing state.
    result = _probe(tmp_path, "pacing", "https://api.coingecko.com/owned-fixture", worker=worker)
    assert result.get("code") == "TIMEOUT", result
    assert result["elapsed"] < BUDGET + 1.5, result
    assert result["worker_started"] and not result["worker_alive"], result
    assert result["spools"] == [], result


@pytest.mark.parametrize("operation", ["dns", "stdin", "cancel"])
def test_production_entry_reaps_blocked_or_cancelled_fixed_worker(tmp_path, operation):
    pid_file = tmp_path / "worker.pid"
    worker = tmp_path / "owned_blocking_worker.py"
    worker.write_text(
        "import os, socket, time\nfrom pathlib import Path\n"
        f"pid_file = Path({str(pid_file)!r})\n"
        "pending = pid_file.with_suffix('.tmp')\n"
        "pending.write_text(str(os.getpid()), encoding='ascii')\n"
        "pending.replace(pid_file)\n"
        + ("socket.getaddrinfo = lambda *a, **k: time.sleep(30)\n"
           "socket.getaddrinfo('owned.invalid', 443)\n" if operation == "dns" else "time.sleep(30)\n"),
        encoding="utf-8",
    )
    result = _probe(tmp_path, operation, "https://owned.invalid/data.csv", worker=worker)
    if operation == "cancel":
        assert result["status"] == "cancelled", result
    else:
        assert result.get("code") == "TIMEOUT", result
    assert result["elapsed"] < BUDGET + 1.5, result
    assert result["worker_started"], "Real fixture child never started"
    assert not result["worker_alive"], "Production entry leaked its owned worker"
    assert result["spools"] == [], result


def test_clean_wheel_and_installed_package_include_verified_airbnb_registry(tmp_path):
    """A checkout-only JSON file is not sufficient for installed city selection."""
    installed = importlib.util.find_spec("setuptools")
    backend_root = Path(installed.origin).parent.parent if installed else None
    if backend_root is None and os.environ.get("CAMPAIGN_BUILD_BACKEND_PATH"):
        backend_root = Path(os.environ["CAMPAIGN_BUILD_BACKEND_PATH"])
    if backend_root is None:
        pytest.skip("Offline wheel capability missing: install declared build tools or set CAMPAIGN_BUILD_BACKEND_PATH")

    clean = tmp_path / "clean-source"
    clean.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(REPO / name, clean / name)
    shutil.copytree(REPO / "scripts", clean / "scripts",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    build_code = (
        "import importlib.metadata,sys\nfrom pathlib import Path\n"
        "sys.path.insert(0,sys.argv[1])\n"
        "try:\n import tomllib\n"
        "except ModuleNotFoundError:\n from setuptools._vendor import tomli as tomllib\n"
        "from packaging.requirements import Requirement\n"
        "requirements=tomllib.loads(Path('pyproject.toml').read_text(encoding='utf-8'))['build-system']['requires']\n"
        "for raw in requirements:\n"
        " requirement=Requirement(raw)\n"
        " if requirement.name == 'setuptools':\n"
        "  version=importlib.metadata.version('setuptools')\n"
        "  assert version in requirement.specifier, (raw,version)\n"
        "import setuptools.build_meta\n"
        "print(setuptools.build_meta.build_wheel(sys.argv[2]))\n"
    )
    build = subprocess.run([sys.executable, "-B", "-c", build_code,
                            str(backend_root), str(wheels)], cwd=clean,
                           capture_output=True, text=True, timeout=60)
    assert build.returncode == 0, f"Offline clean-wheel build failed: {build.stderr}\n{build.stdout}"
    wheel_files = list(wheels.glob("*.whl"))
    assert len(wheel_files) == 1, build.stdout
    target = tmp_path / "installed"
    target.mkdir()
    with zipfile.ZipFile(wheel_files[0]) as archive:
        resource = "scripts/resources/insideairbnb_exports.json"
        assert resource in archive.namelist(), "Built wheel omits the city export registry"
        registry = json.loads(archive.read(resource))
        assert any(row["region_id"] == "amsterdam" for row in registry["resources"])
        archive.extractall(target)
    install_probe = (
        "import json,sys\nfrom pathlib import Path\n"
        "from contextlib import redirect_stdout\n"
        "sys.path.insert(0,sys.argv[1])\n"
        "from scripts.fetchers.airbnb import AirbnbFetcher\n"
        "import scripts.fetchers.airbnb as module\n"
        "assert Path(module.__file__).is_relative_to(Path(sys.argv[1]))\n"
        "fetcher=AirbnbFetcher('amsterdam',sys.argv[2],{})\n"
        "with redirect_stdout(sys.stderr):\n fetcher.scout()\n"
        "print(json.dumps({'url': fetcher.download_url}))\n"
    )
    installed_probe = subprocess.run([sys.executable, "-B", "-c", install_probe,
                                      str(target), str(tmp_path / "installed-output")],
                                     cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert installed_probe.returncode == 0, installed_probe.stderr
    assert json.loads(installed_probe.stdout)["url"] == (
        "https://data.insideairbnb.com/the-netherlands/north-holland/"
        "amsterdam/2026-09-16/data/listings.csv.gz"
    )
