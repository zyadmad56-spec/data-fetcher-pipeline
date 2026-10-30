"""Network egress policy tests: global-only IP verdicts, pinned transport,
trust_env independence, bounded robots fetch, analysis deadline, and parity
between the pipeline net_guard and the embedded standalone template policy."""
import inspect
import io
import ipaddress
import json
import runpy
import socket
import subprocess
import sys
import threading
import time
import zipfile
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

import scripts.net_guard as net_guard
from scripts.errors import DataFetchError
from scripts.http_utils import public_session, request_with_retry
from scripts.web_analyzer import AnalysisReport, WebAnalyzer

PUBLIC_DNS = [(2, 1, 6, "", ("93.184.216.34", 0))]


# ---------------------------------------------------------------------------
# is_public_ip — the shared IP verdict
# ---------------------------------------------------------------------------

VERDICTS = {
    # globally routable
    "8.8.8.8": True, "1.1.1.1": True, "93.184.216.34": True,
    "2606:4700::1111": True, "2001:4860:4860::8888": True,
    # RFC 6598 shared space — the verified historical gap
    "100.64.0.1": False, "100.100.100.200": False,
    # classic non-public ranges
    "0.0.0.0": False, "0.1.2.3": False, "10.0.0.5": False, "172.16.0.9": False,
    "172.31.255.255": False, "192.168.1.1": False, "169.254.169.254": False,
    "127.0.0.1": False, "192.0.2.1": False, "198.51.100.7": False,
    "203.0.113.9": False, "198.18.0.5": False, "224.0.0.1": False,
    "240.0.0.1": False, "255.255.255.255": False,
    # IPv6
    "::": False, "::1": False, "fe80::1": False, "fd00::1": False,
    "ff02::1": False, "2001:db8::1": False,
    # tunneled forms of non-public endpoints
    "::ffff:127.0.0.1": False, "::ffff:169.254.169.254": False,
    "::ffff:10.0.0.1": False, "2002:0a00:0001::": False,
    "2001:0:4136:e378:8000:63bf:3fff:fdd2": False,
    # tunneled public endpoint (mapped unwrap reaches a public address)
    "::ffff:8.8.8.8": True,
}


def test_is_public_ip_verdicts():
    for text, expected in VERDICTS.items():
        assert net_guard.is_public_ip(text) is expected, text


def test_is_public_ip_rejects_garbage_text():
    assert net_guard.is_public_ip("not-an-address") is False
    assert net_guard.is_public_ip("") is False


@pytest.mark.parametrize("url", [
    "https://100.100.100.200/latest/meta-data/",
    "http://[::ffff:169.254.169.254]/latest/meta-data/",
    "http://[::ffff:127.0.0.1]/admin",
    "http://169.254.169.254/latest/meta-data/",
    "http://127.0.0.1/admin",
    "http://10.0.0.5/internal",
    "http://localhost/data",
    "http://sub.localhost/data",
    "ftp://example.com/file.csv",
])
def test_validate_public_url_blocks_non_public_targets(url):
    with pytest.raises(ValueError):
        net_guard.validate_public_url(url)


def test_validate_public_url_rejects_userinfo_and_control_characters():
    for url in [
        "http://user:pass@example.com/data",
        "http://@example.com/data",
        "https://api.example.com/v1?key=1\x00",
        "http://exa\x1fmple.com/data",
        "http://example.com/data\n",
        "http://example.com/\x7f",
    ]:
        with pytest.raises(ValueError):
            net_guard.validate_public_url(url)


def test_validate_public_url_accepts_public_targets_with_patched_dns():
    with patch("socket.getaddrinfo", return_value=PUBLIC_DNS):
        net_guard.validate_public_url("https://example.com/data.csv")


def test_validate_public_url_blocks_mixed_public_private_dns_answers():
    mixed = PUBLIC_DNS + [(2, 1, 6, "", ("10.0.0.5", 0))]
    with patch("socket.getaddrinfo", return_value=mixed):
        with pytest.raises(ValueError):
            net_guard.validate_public_url("https://mixed-dns.example.com/data")


def test_validate_public_url_defers_unresolvable_hosts():
    with patch("socket.getaddrinfo", side_effect=socket.gaierror(11001, "lookup failed")):
        with pytest.raises(net_guard.UnresolvableHostError) as caught:
            net_guard.validate_public_url("https://offline.example.org/data")
    assert isinstance(caught.value, ValueError)


# ---------------------------------------------------------------------------
# PinnedPublicHTTPAdapter — connect-pinned transport
# ---------------------------------------------------------------------------

def _prepared_get(url: str):
    return requests.Request("GET", url).prepare()


def test_pinned_adapter_rejects_mixed_dns_answers_before_connecting():
    mixed = PUBLIC_DNS + [(2, 1, 6, "", ("172.16.0.9", 0))]
    with patch("socket.getaddrinfo", return_value=mixed):
        adapter = net_guard.PinnedPublicHTTPAdapter()
        with pytest.raises(requests.ConnectionError):
            adapter.send(_prepared_get("https://mixed-dns.example.com/data"))


def test_pinned_adapter_rejects_private_literal_and_hostile_urls_without_connecting():
    # Duck-typed requests (not requests.Request.prepare(), which silently moves
    # userinfo into .auth) so the adapter's own guard is what gets exercised.
    with patch("requests.adapters.HTTPAdapter.send") as parent_send:
        adapter = net_guard.PinnedPublicHTTPAdapter()
        for url in ["http://100.100.100.200/x", "http://user:pass@example.com/x",
                    "http://example.com/\x00"]:
            with pytest.raises(requests.ConnectionError):
                adapter.send(SimpleNamespace(url=url))
    parent_send.assert_not_called()  # rejection happens before any transport work


def test_pinned_adapter_serves_only_validated_answers_after_ttl_zero_flip():
    flipped = [(2, 1, 6, "", ("10.0.0.5", 0))]
    calls = {"count": 0}

    def rebinding_resolver(host, *args, **kwargs):
        calls["count"] += 1
        return PUBLIC_DNS if calls["count"] == 1 else flipped

    candidates = net_guard._validated_dns_answers("rebind.example", rebinding_resolver)
    assert [str(c) for c in candidates] == ["93.184.216.34"]

    def fake_real_getaddrinfo(host, port=None, family=0, type=0, proto=0, flags=0):
        return [(2, 1, 6, "", (host, port))]

    shim = net_guard._pinned_getaddrinfo_factory("rebind.example", candidates, fake_real_getaddrinfo)
    # Simulate every later lookup the connect phase would perform: the private
    # answer must never be served, and the port must be carried into the result.
    for _ in range(3):
        answers = shim("rebind.example", 443, 0, socket.SOCK_STREAM)
        assert answers, "pinned shim returned no answers"
        for entry in answers:
            assert entry[4][0] == "93.184.216.34"
            assert entry[4][1] == 443
    # Unrelated hosts still go through the wrapped (real) resolver.
    assert shim("other.example", 80, 0, socket.SOCK_STREAM) == \
        fake_real_getaddrinfo("other.example", 80, 0, socket.SOCK_STREAM)


def test_pinned_adapter_preserves_original_url_and_restores_resolver():
    sentinel = object()
    real_getaddrinfo = socket.getaddrinfo
    with patch("socket.getaddrinfo", return_value=PUBLIC_DNS):
        with patch("requests.adapters.HTTPAdapter.send", return_value=sentinel) as parent_send:
            adapter = net_guard.PinnedPublicHTTPAdapter()
            request = _prepared_get("https://example.com/data.csv")
            result = adapter.send(request, stream=True, timeout=7)
    assert result is sentinel
    # The prepared request is passed through untouched: the Host header, TLS
    # SNI and certificate hostname verification still name the real hostname.
    passed_request = parent_send.call_args.args[0]
    assert passed_request is request
    assert passed_request.url == "https://example.com/data.csv"
    assert parent_send.call_args.kwargs["timeout"] == 7
    assert parent_send.call_args.kwargs["stream"] is True
    # The temporary resolver swap is always restored.
    assert socket.getaddrinfo is real_getaddrinfo


def test_pinned_adapter_restores_resolver_after_transport_error():
    real_getaddrinfo = socket.getaddrinfo
    with patch("socket.getaddrinfo", return_value=PUBLIC_DNS):
        with patch("requests.adapters.HTTPAdapter.send", side_effect=requests.ConnectionError("boom")):
            adapter = net_guard.PinnedPublicHTTPAdapter()
            with pytest.raises(requests.ConnectionError):
                adapter.send(_prepared_get("https://example.com/data.csv"))
    assert socket.getaddrinfo is real_getaddrinfo


def test_public_session_is_trust_env_false_with_pinned_transport():
    assert public_session.trust_env is False
    adapters = [a for a in public_session.adapters.values()
                if isinstance(a, net_guard.PinnedPublicHTTPAdapter)]
    assert adapters, "public_session must mount PinnedPublicHTTPAdapter"


# ---------------------------------------------------------------------------
# request_with_retry — opt-in public-only transport, caps and deadline
# ---------------------------------------------------------------------------

def test_public_only_rejects_private_target_before_any_dial():
    with patch("scripts.http_utils.session") as default_session, \
            patch("scripts.http_utils.public_session") as hardened:
        with pytest.raises(ValueError):
            request_with_retry("http://100.100.100.200/latest/meta-data/", public_only=True, max_retries=1)
    default_session.get.assert_not_called()
    hardened.get.assert_not_called()


def test_public_only_uses_the_hardened_session():
    public_dns = PUBLIC_DNS
    with patch("socket.getaddrinfo", return_value=public_dns):
        with patch("scripts.http_utils.session") as default_session, \
                patch("scripts.http_utils.public_session") as hardened:
            hardened.get.return_value = MagicMock(status_code=200, content=b"ok", headers={})
            response = request_with_retry("https://example.com/data.csv", public_only=True)
    assert response.status_code == 200
    hardened.get.assert_called_once()
    default_session.get.assert_not_called()


def _local_http_server(seen_headers: list):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen_headers.append(dict(self.headers))
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}/fixture"


def test_netrc_credentials_are_ignored_by_trust_env_false_sessions(tmp_path, monkeypatch):
    netrc_file = tmp_path / "netrc"
    netrc_file.write_text("machine 127.0.0.1 login canary-user password canary-secret\n", encoding="utf-8")
    monkeypatch.setenv("NETRC", str(netrc_file))
    seen = []
    server, url = _local_http_server(seen)
    try:
        # Control: a trust_env=True session really would apply the netrc pair.
        control = requests.Session()
        control.trust_env = True
        control.get(url, timeout=5)
        assert seen[-1].get("Authorization", "").startswith("Basic ")

        # Hardened: trust_env=False never reads the credential file.
        hardened = requests.Session()
        hardened.trust_env = False
        hardened.get(url, timeout=5)
        assert "Authorization" not in seen[-1]
    finally:
        server.shutdown()
        server.server_close()


def test_ambient_proxy_environment_is_ignored_by_trust_env_false_sessions(tmp_path, monkeypatch):
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")  # dead proxy: any use would fail
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    seen = []
    server, url = _local_http_server(seen)
    try:
        hardened = requests.Session()
        hardened.trust_env = False
        response = hardened.get(url, timeout=5)  # direct connection despite proxy vars
        assert response.status_code == 200

        # Control: a trust_env=True session really would route via the proxy.
        control = requests.Session()
        control.trust_env = True
        with pytest.raises(requests.RequestException):
            control.get(url, timeout=2)
    finally:
        server.shutdown()
        server.server_close()


def test_max_body_bytes_aborts_buffered_responses():
    oversized = MagicMock(status_code=200, headers={}, content=b"x" * (512 * 1024 + 1))
    with patch("scripts.http_utils.session") as default_session:
        default_session.get.return_value = oversized
        with pytest.raises(DataFetchError) as caught:
            request_with_retry("https://example.com/big.bin", max_body_bytes=512 * 1024, max_retries=1)
    assert caught.value.code == "SIZE_LIMIT"


def test_max_body_bytes_aborts_streaming_on_content_length():
    huge = MagicMock(status_code=200, headers={"Content-Length": str(64 * 1024 * 1024)})
    with patch("scripts.http_utils.session") as default_session:
        default_session.get.return_value = huge
        with pytest.raises(DataFetchError) as caught:
            request_with_retry("https://example.com/big.bin", stream=True,
                               max_body_bytes=512 * 1024, max_retries=1)
    assert caught.value.code == "SIZE_LIMIT"


def test_request_deadline_aborts_with_coded_timeout():
    def dead_get(*args, **kwargs):
        raise requests.Timeout("no route")

    with patch("scripts.http_utils.session") as default_session:
        default_session.get.side_effect = dead_get
        started = time.monotonic()
        with pytest.raises(DataFetchError) as caught:
            request_with_retry("https://example.com/stall", max_retries=5, deadline_sec=0.2)
    assert caught.value.code == "TIMEOUT"
    assert time.monotonic() - started < 2.0


def test_request_deadline_clamps_hostile_retry_after_sleeps():
    limited = MagicMock(status_code=429, headers={"Retry-After": "3600"}, content=b"")
    with patch("scripts.http_utils.session") as default_session:
        default_session.get.return_value = limited
        # The fake sleep records the clamped duration AND advances the clock.
        with patch("scripts.http_utils.time.sleep", side_effect=time.sleep) as fake_sleep:
            with pytest.raises(DataFetchError) as caught:
                request_with_retry("https://example.com/limited", max_retries=3, deadline_sec=0.3)
    assert caught.value.code == "TIMEOUT"
    assert fake_sleep.call_count == 1
    assert 0 < fake_sleep.call_args.args[0] <= 0.3  # the hostile 3600s was clamped


def test_read_bounded_body_caps_streamed_metadata():
    class FakeResponse:
        def __init__(self):
            self.iter_calls = 0

        def iter_content(self, chunk_size):
            self.iter_calls += 1
            for _ in range(20):  # far more than the cap would allow
                yield b"#" * chunk_size

        text = ""

    response = FakeResponse()
    body = net_guard.read_bounded_body(response, 512 * 1024)
    assert len(body) == 512 * 1024
    assert response.iter_calls == 1  # reading stopped before the stream was exhausted


# ---------------------------------------------------------------------------
# WebAnalyzer — bounded robots fetch and whole-job deadline
# ---------------------------------------------------------------------------

def test_oversized_robots_txt_is_aborted_at_512_kib(tmp_path: Path):
    analyzer = WebAnalyzer("https://example.com/data.csv", str(tmp_path))
    served = {"chunks": 0}

    def endless_robots(*args, **kwargs):
        for _ in range(400):  # 400 x 64 KiB = 25 MB offered to the reader
            served["chunks"] += 1
            yield b"#" * 65536
        yield b"User-agent: *\nDisallow: /\n"

    robots_response = MagicMock(status_code=200, headers={}, text="")
    robots_response.iter_content.side_effect = endless_robots
    main_response = MagicMock(status_code=200, headers={"Content-Type": "text/csv", "Content-Length": "12"})

    def fake_request(url, **kwargs):
        return robots_response if url.endswith("/robots.txt") else main_response

    with patch("socket.getaddrinfo", return_value=PUBLIC_DNS):
        with patch("scripts.web_analyzer.request_with_retry", side_effect=fake_request) as request:
            report = analyzer.analyze()

    assert served["chunks"] == 9  # 8 chunks fill the cap, the 9th triggers the abort
    assert request.call_count == 2  # robots + main target (policy allowed: cap hid no rule)
    assert report.difficulty == "easy"


def test_analyze_deadline_stops_before_target_fetch(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("scripts.web_analyzer.ANALYSIS_DEADLINE_SECONDS", 0.05)
    analyzer = WebAnalyzer("https://example.com/data.csv", str(tmp_path))
    robots_response = MagicMock(status_code=200, headers={}, text="")

    def slow_robots(*args, **kwargs):
        time.sleep(0.4)
        return robots_response

    with patch("socket.getaddrinfo", return_value=PUBLIC_DNS):
        with patch("scripts.web_analyzer.request_with_retry", side_effect=slow_robots) as request:
            report = analyzer.analyze()
    assert request.call_count == 1
    assert report.difficulty == "blocked" and report.status_code == 0
    assert any("deadline" in warning.lower() for warning in report.warnings)


# ---------------------------------------------------------------------------
# Standalone template (Phase 5) — record-aware preview, zip guard, parity
# ---------------------------------------------------------------------------

def _template_module(tmp_path: Path, difficulty: str = "easy"):
    analyzer = WebAnalyzer("https://example.com/data.csv", str(tmp_path))
    path = analyzer.generate_script(AnalysisReport(url=analyzer.url, difficulty=difficulty))
    return runpy.run_path(str(path), run_name="dfp_template_probe")


def _streaming_response(payload: bytes, mime: str = "text/csv", chunk_size: int = 16384):
    counter = {"chunks": 0}

    def chunks(*args, **kwargs):
        for index in range(0, len(payload), chunk_size):
            counter["chunks"] += 1
            yield payload[index:index + chunk_size]

    response = MagicMock(status_code=200,
                         headers={"Content-Type": mime, "Content-Length": str(len(payload))},
                         url="https://example.com/data.csv")
    response.iter_content.side_effect = chunks
    response.chunk_counter = counter
    return response


def test_template_ip_policy_matches_net_guard(tmp_path: Path):
    """PARITY: the duplicated ~60 lines embedded in the generated template must
    reach the same verdict as scripts/net_guard.py on every hostile address."""
    module = _template_module(tmp_path)
    template_is_public_ip = module["_is_public_ip"]
    for text, expected in VERDICTS.items():
        assert net_guard.is_public_ip(text) is expected, f"net_guard verdict drifted: {text}"
        assert template_is_public_ip(text) is expected, f"template verdict drifted: {text}"
        try:
            address = ipaddress.ip_address(text)
        except ValueError:
            continue
        assert net_guard.is_public_ip(address) is template_is_public_ip(address), text


def test_template_extract_frame_signature_unchanged(tmp_path: Path):
    module = _template_module(tmp_path)
    parameters = inspect.signature(module["extract_frame"]).parameters
    assert list(parameters) == ["url", "columns", "table_index", "row_limit"]
    assert [p.default for p in parameters.values()] == [None, None, None, 0]


def test_template_stops_streaming_once_enough_records_are_buffered(tmp_path: Path):
    module = _template_module(tmp_path)
    payload = b"value,label\n" + b"".join(f"{index},real\n".encode() for index in range(100000))
    response = _streaming_response(payload)
    module["extract_frame"].__globals__["_fetch_with_retry"] = lambda *args, **kwargs: response

    frame = module["extract_frame"](row_limit=5)

    assert len(frame) == 5
    assert frame["value"].tolist() == [0, 1, 2, 3, 4]
    assert response.chunk_counter["chunks"] == 1  # stopped after the first 16 KiB batch
    response.close.assert_called_once()


def test_template_multiline_quoted_records_parse_completely(tmp_path: Path):
    module = _template_module(tmp_path)
    payload = (b'name,notes\n'
               b'alpha,"first line\nsecond line"\n'
               b'beta,"has ""quotes"" inside"\n'
               b'gamma,plain\n'
               b'delta,"multi\nline\nvalue"\n')
    response = _streaming_response(payload)
    module["extract_frame"].__globals__["_fetch_with_retry"] = lambda *args, **kwargs: response

    frame = module["extract_frame"](row_limit=4)

    assert len(frame) == 4
    assert frame["name"].tolist() == ["alpha", "beta", "gamma", "delta"]
    assert frame["notes"].iloc[0] == "first line\nsecond line"
    assert frame["notes"].iloc[1] == 'has "quotes" inside'
    assert frame["notes"].iloc[3] == "multi\nline\nvalue"
    assert response.chunk_counter["chunks"] == 1


def test_template_falls_back_to_full_read_with_fewer_records(tmp_path: Path):
    module = _template_module(tmp_path)
    payload = b"value,label\n1,real\n2,real\n"
    response = _streaming_response(payload)
    module["extract_frame"].__globals__["_fetch_with_retry"] = lambda *args, **kwargs: response

    frame = module["extract_frame"](row_limit=50)

    assert len(frame) == 2  # whole (small) source parsed; nothing invented
    response.close.assert_called_once()


def _xlsx_bytes(entry_count: int = 2, inflate_last: int = 0) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for index in range(entry_count):
            archive.writestr(f"xl/sheet{index}.xml", b"<x/>")
        if inflate_last:
            # Forge the central-directory uncompressed size (classic bomb lie).
            archive.filelist[-1].file_size = inflate_last
    return buffer.getvalue()


def test_template_rejects_xlsx_zip_bomb_entry_count(tmp_path: Path):
    module = _template_module(tmp_path)
    response = _streaming_response(
        _xlsx_bytes(entry_count=1001),
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    module["extract_frame"].__globals__["_fetch_with_retry"] = lambda *args, **kwargs: response

    with pytest.raises(ValueError, match="SIZE_LIMIT"):
        module["extract_frame"]()


def test_template_rejects_xlsx_zip_bomb_uncompressed_size(tmp_path: Path):
    module = _template_module(tmp_path)
    response = _streaming_response(
        _xlsx_bytes(entry_count=2, inflate_last=201 * 1024 * 1024),
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    module["extract_frame"].__globals__["_fetch_with_retry"] = lambda *args, **kwargs: response

    with pytest.raises(ValueError, match="SIZE_LIMIT"):
        module["extract_frame"]()


def test_template_accepts_small_legitimate_xlsx(tmp_path: Path):
    import pandas as pd
    module = _template_module(tmp_path)
    buffer = io.BytesIO()
    pd.DataFrame({"a": [1, 2]}).to_excel(buffer, index=False)
    response = _streaming_response(
        buffer.getvalue(),
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    module["extract_frame"].__globals__["_fetch_with_retry"] = lambda *args, **kwargs: response
    frame = module["extract_frame"]()
    assert frame["a"].tolist() == [1, 2]


def test_template_transfer_deadline_aborts(tmp_path: Path):
    module = _template_module(tmp_path)
    module["extract_frame"].__globals__["TRANSFER_DEADLINE_SECONDS"] = 0.05
    module["extract_frame"].__globals__["_robots_allowed"] = lambda url: True
    with patch("socket.getaddrinfo", return_value=PUBLIC_DNS), \
            patch("requests.get", side_effect=requests.Timeout("stalled")):
        with pytest.raises(RuntimeError, match="[Dd]eadline"):
            module["_fetch_with_retry"]("https://example.com/data.csv", {}, retries=3)


def test_generated_script_is_standalone(tmp_path: Path):
    analyzer = WebAnalyzer("https://example.com/data.csv", str(tmp_path))
    path = analyzer.generate_script(AnalysisReport(url=analyzer.url, difficulty="easy"))
    source = Path(path).read_text(encoding="utf-8")
    compile(source, path, "exec")
    assert "import scripts" not in source and "from scripts" not in source
    run = subprocess.run([sys.executable, "-I", str(path), "--help"],
                         capture_output=True, text=True, timeout=120)
    assert run.returncode == 0
    assert "extractor" in run.stdout.lower()


def test_generated_template_encodes_difficulty_and_url(tmp_path: Path):
    analyzer = WebAnalyzer("https://example.com/d?q=1", str(tmp_path))
    path = analyzer.generate_script(AnalysisReport(url=analyzer.url, difficulty="medium"))
    module = runpy.run_path(str(path), run_name="dfp_meta_probe")
    assert module["DIFFICULTY"] == "medium"
    assert module["URL"] == "https://example.com/d?q=1"
    source = Path(path).read_text(encoding="utf-8")
    assert json.dumps("https://example.com/d?q=1") in source
