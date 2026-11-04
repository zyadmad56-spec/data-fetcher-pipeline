"""Source suitability and safe assessment metadata."""

from __future__ import annotations
from types import SimpleNamespace
import io
import json
import pytest
import requests
import socket
from scripts import cli
from scripts import web_analyzer
from scripts.errors import DataFetchError


def _response(body: bytes, content_type: str = "text/plain", status: int = 200):
    response = requests.Response()
    response.status_code = status
    response.headers = {"Content-Type": content_type, "Content-Length": str(len(body))}
    response.raw = io.BytesIO(body)
    response._content = False
    response._content_consumed = False
    response.encoding = "utf-8"
    return response


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        ],
    )


@pytest.mark.parametrize(
    ("suffix", "body", "content_type", "expected"),
    [
        ("data.csv", b"id,value\n1,2\n", "text/csv", "easy"),
        ("data.csv", b"<html><form><input type='password'></form></html>",
         "text/html", "not_easy"),
        ("data.csv?download=1", b"<html><div class='hcaptcha'>captcha</div></html>",
         "text/html", "blocked"),
        ("data", b"<html><table><tr><td>1</td></tr></table></html>",
         "text/html", "medium"),
    ],
)
def test_assessment_uses_actual_content_not_filename(
    suffix, body, content_type, expected, tmp_path, public_dns, monkeypatch
):
    def get(url, **kwargs):
        if url.endswith("/robots.txt"):
            return _response(b"User-agent: *\nAllow: /\n")
        return _response(body, content_type)

    monkeypatch.setattr(web_analyzer, "request_with_retry", get)
    report = web_analyzer.WebAnalyzer(
        "https://source.example/" + suffix, str(tmp_path)
    ).analyze()
    if expected == "not_easy":
        assert report.difficulty in {"medium", "hard", "blocked"}
    else:
        assert report.difficulty == expected
    assert report.content_type == content_type


def test_robots_denial_prevents_target_request(tmp_path, public_dns, monkeypatch):
    def get(url, **kwargs):
        if url.endswith("/robots.txt"):
            return _response(b"User-agent: *\nDisallow: /\n")
        raise AssertionError("The denied target must not be requested")

    monkeypatch.setattr(web_analyzer, "request_with_retry", get)
    report = web_analyzer.WebAnalyzer(
        "https://source.example/data.csv", str(tmp_path)
    ).analyze()
    assert report.robots_allowed is False
    assert report.difficulty == "blocked"


def test_redirect_to_private_network_is_rejected_before_target_fetch(
    tmp_path, public_dns, monkeypatch
):
    from scripts import net_guard
    fetched = []

    class FixtureHTTPTransport:
        def fetch(self, request):
            assert request.public_only is True
            net_guard.validate_public_url(request.url)
            fetched.append(request.url)
            if request.url.endswith("/robots.txt"):
                return _response(b"User-agent: *\nAllow: /\n")
            if request.url == "https://source.example/data.csv":
                response = _response(b"", status=302)
                response.headers["Location"] = "http://127.0.0.1/private.csv"
                return response
            raise AssertionError("A private redirect destination must never be fetched")

    monkeypatch.setattr('scripts.adapters.http_transport.SupervisedHTTPTransport', FixtureHTTPTransport)
    report = web_analyzer.WebAnalyzer(
        "https://source.example/data.csv", str(tmp_path)
    ).analyze()
    assert report.difficulty == "blocked" and report.robots_allowed is False
    assert fetched == ['https://source.example/robots.txt',
                       'https://source.example/data.csv']


# Assessment metadata isolation


@pytest.mark.parametrize('channel', ['redirect-path', 'rate-header-name', 'rate-header-value'])
def test_assessment_external_metadata_cannot_enter_automatic_envelope(tmp_path, monkeypatch, channel):
    marker = 'UNTRUSTED_ASSESSMENT_CANARY_5842'
    origin = 'https://source.example'
    initial = origin + '/data.csv'
    redirected = origin + '/download/' + marker + '.csv'
    observed = []

    class FixtureHTTPTransport:
        def fetch(self, request):
            assert request.public_only is True
            observed.append(request.url)
            response = requests.Response()
            response.status_code = 200
            response.url = request.url
            response.encoding = 'utf-8'
            response.headers['Content-Type'] = 'text/csv'
            body = b'id,value\n1,2\n'
            if request.url.endswith('/robots.txt'):
                body = b'User-agent: *\nAllow: /\n'
            elif channel == 'redirect-path' and request.url == initial:
                response.status_code = 302
                response.headers['Location'] = redirected
                body = b''
            elif channel == 'rate-header-name':
                response.headers['X-RateLimit-' + marker] = '10'
            elif channel == 'rate-header-value':
                response.headers['X-RateLimit-Remaining'] = marker
            response.headers['Content-Length'] = str(len(body))
            response.raw = io.BytesIO(body)
            response.body_complete = True
            response.body_bytes = len(body)
            return response

    monkeypatch.setattr('scripts.adapters.http_transport.SupervisedHTTPTransport', FixtureHTTPTransport)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: pytest.fail('Assessment resolved outside supervised boundary'))
    monkeypatch.setattr('requests.sessions.Session.request', lambda *a, **kw: pytest.fail('Synthetic assessment attempted real HTTP'))
    envelope = cli._analyze_source(SimpleNamespace(query=initial, outdir=str(tmp_path)))
    assert envelope['status'] == 'success' and envelope['mode'] == 'analysis'
    assert envelope['difficulty'] == 'easy' and envelope['status_code'] == 200
    assert observed, 'Actual assessment composition was not exercised'
    assert marker not in json.dumps(envelope), 'Server-controlled metadata leaked into automatic JSON'
