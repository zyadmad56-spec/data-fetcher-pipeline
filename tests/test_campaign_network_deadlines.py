"""Independent regressions for the 2026-11-02 network/deadline incidents."""
from __future__ import annotations

import ctypes
import importlib
import io
import ipaddress
import os
import select
import signal
import socket
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import requests

from scripts import http_utils, net_guard, web_analyzer
from scripts.errors import DataFetchError


def _streamed_response(body: bytes, status: int = 200) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.raw = io.BytesIO(body)
    response._content = False
    response._content_consumed = False
    return response


@pytest.mark.parametrize(
    ("body", "limit", "expected"),
    [(b"", 1024, ""), (b"abcde", 3, "abc"), (b"\xffa", 3, "\ufffda")],
)
def test_streamed_metadata_respects_empty_and_byte_boundary(body, limit, expected):
    # NET-001: an exhausted empty real Response cannot be read through .text.
    response = _streamed_response(body)
    try:
        assert net_guard.read_bounded_body(response, limit) == expected
    finally:
        response.close()


def test_empty_successful_robots_allows_scouting_without_consumed_body_error(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        ],
    )
    monkeypatch.setattr(
        web_analyzer,
        "request_with_retry",
        lambda *args, **kwargs: _streamed_response(b""),
    )
    analyzer = web_analyzer.WebAnalyzer("https://source.example/data.csv", str(tmp_path))
    assert analyzer._check_robots_txt() is True


@pytest.mark.parametrize(
    ("family", "reverse", "expected_families"),
    [
        (socket.AF_INET, False, {socket.AF_INET}),
        (socket.AF_INET, True, {socket.AF_INET}),
        (socket.AF_INET6, False, {socket.AF_INET6}),
        (socket.AF_INET6, True, {socket.AF_INET6}),
        (socket.AF_UNSPEC, False, {socket.AF_INET, socket.AF_INET6}),
    ],
)
def test_pinned_dual_stack_preserves_usable_requested_address_family(
    family, reverse, expected_families
):
    # DNS-001: incompatible AAAA/A answers must not discard compatible answers.
    candidates = [ipaddress.ip_address("8.8.8.8"),
                  ipaddress.ip_address("2606:4700:4700::1111")]
    if reverse:
        candidates.reverse()
    resolve = net_guard._pinned_getaddrinfo_factory(
        "source.example", candidates, socket.getaddrinfo
    )
    answers = resolve(
        "source.example", 443, family, socket.SOCK_STREAM, 0, socket.AI_NUMERICHOST
    )
    assert {answer[0] for answer in answers} == expected_families
    assert all(answer[4][1] == 443 for answer in answers)
    assert all(net_guard.is_public_ip(answer[4][0]) for answer in answers)


@pytest.mark.parametrize("unsafe", ["127.0.0.1", "10.0.0.1", "100.100.100.200", "::1"])
def test_mixed_public_and_unsafe_dns_answers_fail_closed(unsafe):
    def resolve(*args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (unsafe, 443)),
        ]

    with pytest.raises(requests.ConnectionError):
        net_guard._validated_dns_answers("source.example", resolver=resolve)


def test_successful_http_response_after_budget_is_timeout(monkeypatch):
    # TIME-001: the former implementation accepted a successful late response.
    clock = [0.0]
    response = requests.Response()
    response.status_code = 200
    response._content = b"id\n1\n"
    response.raw = io.BytesIO(response._content)

    def delayed_fetch(self, request):
        clock[0] += 2.0
        return response

    monkeypatch.setattr(http_utils.time, "monotonic", lambda: clock[0])
    # The fixed-function HTTP transport owns network I/O now; parent Session
    # mocks do not cross its process boundary. Keep the response itself real.
    monkeypatch.setattr(
        "scripts.adapters.http_transport.SupervisedHTTPTransport.fetch",
        delayed_fetch,
    )
    try:
        with pytest.raises(DataFetchError) as caught:
            http_utils.request_with_retry(
                "https://source.example/data.csv", deadline_sec=0.1, max_retries=1
            )
        assert caught.value.code == "TIMEOUT"
        assert response.raw.closed, "A late response must release its body resource"
    finally:
        response.close()


def _process_is_running(pid: int) -> bool:
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


def test_real_blocking_dns_child_is_timed_out_and_reaped(tmp_path, monkeypatch):
    """Public supervisor contract: blocking work stays inside the owned child."""
    transport_module = importlib.import_module("scripts.adapters.http_transport")
    pid_file = tmp_path / "blocking-worker.pid"
    worker = tmp_path / "blocking_dns_worker.py"
    worker.write_text(
        "import os, socket, sys, time\n"
        "from pathlib import Path\n"
        f"Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding='ascii')\n"
        "socket.getaddrinfo = lambda *args, **kwargs: time.sleep(30)\n"
        "socket.getaddrinfo('source.example', 443)\n",
        encoding="utf-8",
    )

    def forbidden_parent_dns(*args, **kwargs):
        raise AssertionError("DNS must run inside the supervised child")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden_parent_dns)
    request = transport_module.HTTPRequest(
        url="https://source.example/data.csv", deadline_sec=1.5,
        public_only=True, max_retries=1,
    )
    transport = transport_module.SupervisedHTTPTransport(worker_path=worker)
    try:
        started = time.monotonic()
        with pytest.raises(DataFetchError) as caught:
            transport.fetch(request)
        elapsed = time.monotonic() - started

        assert caught.value.code == "TIMEOUT"
        assert elapsed < 3.0, "The owned blocking child outlived the deadline tolerance"
        assert pid_file.exists(), "The fixture child must have started to prove supervision"
        pid = int(pid_file.read_text(encoding="ascii"))
        assert not _process_is_running(pid), "Timed-out worker must be killed and reaped"
    finally:
        if pid_file.exists():
            pid = int(pid_file.read_text(encoding="ascii"))
            if _process_is_running(pid):
                os.kill(pid, signal.SIGTERM)


class _OwnedHTTPFixture:
    """Observe real socket lifecycle without making requests outside loopback."""

    def __init__(self):
        self.stop = threading.Event()
        self.started = threading.Event()
        self.connections = []
        self.lock = threading.Lock()

    def client_closed(self, timeout: float = 1.0) -> bool:
        with self.lock:
            connections = list(self.connections)
        if not connections:
            return False
        deadline = time.monotonic() + timeout
        for connection in connections:
            try:
                if connection.fileno() < 0:
                    continue
                readable, _, _ = select.select(
                    [connection], [], [], max(0.0, deadline - time.monotonic())
                )
                if not readable or connection.recv(1, socket.MSG_PEEK):
                    return False
            except (ConnectionResetError, OSError, ValueError):
                continue
        return True


@pytest.fixture
def owned_http_server():
    fixture = _OwnedHTTPFixture()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args):
            return

        def do_GET(self):
            with fixture.lock:
                fixture.connections.append(self.connection)
            fixture.started.set()
            try:
                if self.path == "/delayed-headers":
                    if fixture.stop.wait(5.0):
                        return
                    self._fixed_body(b"id,value\n1,2\n")
                elif self.path == "/slow-trickle":
                    self.send_response(200)
                    self.send_header("Content-Length", "32")
                    self.end_headers()
                    for _ in range(32):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        if fixture.stop.wait(0.25):
                            return
                elif self.path in ("/oversize-chunked", "/oversize-final-chunk"):
                    self.send_response(200)
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    chunk_sizes = (33,) if self.path == "/oversize-final-chunk" else (64,) * 8
                    for size in chunk_sizes:
                        self.wfile.write(f"{size:x}\r\n".encode() + b"x" * size + b"\r\n")
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                elif self.path == "/oversize-declared":
                    self._fixed_body(b"x" * 4096)
                else:
                    self._fixed_body(b"id,value\n1,2\n")
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def _fixed_body(self, body):
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    fixture.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.02), daemon=True
    )
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
        thread.join(timeout=1.0)


@pytest.fixture
def owned_spool_root(tmp_path, monkeypatch):
    spool_root = tmp_path / "owned-http-spools"
    spool_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(spool_root))
    for variable in ("TMP", "TEMP", "TMPDIR"):
        monkeypatch.setenv(variable, str(spool_root))
    return spool_root


@pytest.mark.parametrize(
    ("route", "stream", "read_timeout"),
    [
        ("/delayed-headers", False, 10.0),
        ("/slow-trickle", False, 1.0),
        ("/slow-trickle", True, 1.0),
    ],
)
def test_supervisor_bounds_real_headers_and_slow_body(
    route, stream, read_timeout, owned_http_server, owned_spool_root
):
    transport_module = importlib.import_module("scripts.adapters.http_transport")
    request = transport_module.HTTPRequest(
        url=owned_http_server.url + route,
        public_only=False,
        deadline_sec=2.0,
        timeout=read_timeout,
        stream=stream,
        max_retries=1,
        max_body_bytes=1024,
    )
    started = time.monotonic()
    with pytest.raises(DataFetchError) as caught:
        transport_module.SupervisedHTTPTransport().fetch(request)
    elapsed = time.monotonic() - started

    assert caught.value.code == "TIMEOUT"
    assert elapsed < 3.5, "Headers/body work exceeded the total deadline tolerance"
    assert owned_http_server.started.is_set(), "The real HTTP boundary must be reached"
    assert owned_http_server.client_closed(), "Timed-out HTTP child kept its socket open"
    assert not list(owned_spool_root.iterdir()), "Timeout left an owned spool behind"


@pytest.mark.parametrize(
    "route", ["/oversize-declared", "/oversize-chunked", "/oversize-final-chunk"]
)
def test_supervisor_rejects_large_body_and_removes_owned_spool(
    route, owned_http_server, owned_spool_root
):
    transport_module = importlib.import_module("scripts.adapters.http_transport")
    request = transport_module.HTTPRequest(
        url=owned_http_server.url + route,
        public_only=False,
        deadline_sec=5.0,
        timeout=2.0,
        max_retries=1,
        max_body_bytes=32,
    )
    with pytest.raises(DataFetchError) as caught:
        transport_module.SupervisedHTTPTransport().fetch(request)

    assert caught.value.code == "SIZE_LIMIT"
    assert owned_http_server.started.is_set()
    assert owned_http_server.client_closed(), "Rejected body kept the client socket open"
    assert not list(owned_spool_root.iterdir()), "SIZE_LIMIT left an owned spool behind"


def test_supervisor_returns_real_small_body_and_close_releases_spool(
    owned_http_server, owned_spool_root
):
    transport_module = importlib.import_module("scripts.adapters.http_transport")
    request = transport_module.HTTPRequest(
        url=owned_http_server.url + "/small",
        public_only=False,
        deadline_sec=5.0,
        timeout=2.0,
        stream=True,
        max_retries=1,
        max_body_bytes=1024,
    )
    response = transport_module.SupervisedHTTPTransport().fetch(request)
    try:
        assert response.status_code == 200
        assert response.content == b"id,value\n1,2\n"
    finally:
        response.close()
    response.close()

    assert owned_http_server.started.is_set()
    assert owned_http_server.client_closed()
    assert not list(owned_spool_root.iterdir()), "Closing a response leaked its owned spool"
