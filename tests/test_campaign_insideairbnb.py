"""Inside Airbnb selection must use verified exports rather than index scraping."""
from __future__ import annotations

import socket

import pytest
import requests

from scripts.errors import DataFetchError
from scripts.fetchers import airbnb


ALBANY_SUMMARY = (
    "https://data.insideairbnb.com/united-states/ny/albany/2026-09-14/"
    "visualisations/listings.csv"
)
AMSTERDAM_DETAIL = (
    "https://data.insideairbnb.com/the-netherlands/north-holland/amsterdam/"
    "2026-09-16/data/listings.csv.gz"
)


@pytest.fixture
def export_network(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        ],
    )

    def get(url, **kwargs):
        assert url != airbnb.INDEX_URL, "Automatic HTML index scraping is prohibited"
        assert url.startswith("https://data.insideairbnb.com/")
        response = requests.Response()
        response.status_code = 200
        response.headers = {"Content-Type": "text/csv", "Content-Length": "8"}
        response._content = b"id\n1\n"
        return response

    monkeypatch.setattr(airbnb, "request_with_retry", get)


def test_verified_city_preserves_detailed_listings_without_index_scrape(
    tmp_path, export_network
):
    fetcher = airbnb.AirbnbFetcher("amsterdam", str(tmp_path), {})
    metadata = fetcher.scout()
    assert fetcher.download_url == AMSTERDAM_DETAIL
    assert metadata["url"] == AMSTERDAM_DETAIL
    assert fetcher._payload_compression() == "gzip"


def test_explicit_official_summary_url_is_not_forced_to_gzip(tmp_path, export_network):
    fetcher = airbnb.AirbnbFetcher(ALBANY_SUMMARY, str(tmp_path), {})
    metadata = fetcher.scout()
    assert fetcher.download_url == ALBANY_SUMMARY
    assert metadata["url"] == ALBANY_SUMMARY
    assert fetcher._payload_compression() is None


def test_unknown_city_requires_source_selection_without_index_scrape(
    tmp_path, export_network
):
    fetcher = airbnb.AirbnbFetcher("unverified-city-2026", str(tmp_path), {})
    with pytest.raises(DataFetchError) as caught:
        fetcher.scout()
    assert caught.value.code == "SOURCE_SELECTION_REQUIRED"


@pytest.mark.parametrize(
    "url",
    [
        "http://data.insideairbnb.com/city/listings.csv",
        "https://data.insideairbnb.com.evil.example/city/listings.csv",
        "https://insideairbnb.com@evil.example/city/listings.csv",
        "https://user:token@data.insideairbnb.com/city/listings.csv",
        "file:///tmp/listings.csv",
    ],
)
def test_unsafe_explicit_export_is_rejected_without_any_network(
    url, tmp_path, monkeypatch
):
    def forbid_network(*args, **kwargs):
        raise AssertionError("Unsafe explicit exports must be rejected before networking")

    monkeypatch.setattr(airbnb, "request_with_retry", forbid_network)
    fetcher = airbnb.AirbnbFetcher(url, str(tmp_path), {})
    with pytest.raises(DataFetchError) as caught:
        fetcher.scout()
    assert caught.value.code in {"BLOCKED_URL", "ARG_INVALID", "SOURCE_SELECTION_REQUIRED"}
