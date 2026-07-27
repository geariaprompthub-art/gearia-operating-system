"""Security regression tests for the Scout RSS transport boundary."""

import socket

import httpx
import pytest

from app.core.config import Settings
from app.services.safe_rss_fetcher import SafeRSSFetchError, SafeRSSFetcher


def _public_dns(_: str, __: int) -> list[tuple[object, ...]]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]


def test_fetcher_rejects_private_local_and_credentialed_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    """SSRF-sensitive targets are rejected before any HTTP client is constructed."""

    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    fetcher = SafeRSSFetcher(Settings())

    for url in (
        "file:///etc/passwd",
        "ftp://example.com/feed.xml",
        "http://user:password@example.com/feed.xml",
        "http://localhost/feed.xml",
        "http://127.0.0.1/feed.xml",
        "http://[::1]/feed.xml",
        "http://169.254.169.254/latest/meta-data",
    ):
        with pytest.raises(SafeRSSFetchError):
            fetcher._validate_url(url)


def test_fetcher_revalidates_redirects_and_reads_a_bounded_public_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A public redirect is revalidated before the bounded response is consumed."""

    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/feed.xml"}, request=request)
        return httpx.Response(
            200,
            headers={"content-type": "application/rss+xml"},
            content=b"<rss></rss>",
            request=request,
        )

    def client_factory(**kwargs: object) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    payload = SafeRSSFetcher(Settings(), client_factory=client_factory).fetch("https://example.com/start")

    assert payload == b"<rss></rss>"
    assert requests == ["https://example.com/start", "https://example.com/feed.xml"]


def test_fetcher_rejects_private_redirect_and_oversized_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirects and streamed payloads cannot bypass the same SSRF/size boundary."""

    calls = 0

    def resolving_dns(host: str, _: int) -> list[tuple[object, ...]]:
        nonlocal calls
        calls += 1
        address = "93.184.216.34" if host == "example.com" else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 80))]

    monkeypatch.setattr(socket, "getaddrinfo", resolving_dns)

    def redirect_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://internal.test/feed"}, request=request)

    def redirect_client(**kwargs: object) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(redirect_handler), **kwargs)

    with pytest.raises(SafeRSSFetchError):
        SafeRSSFetcher(Settings(), client_factory=redirect_client).fetch("https://example.com/start")

    oversized = b"x" * 1_025

    def large_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-length": str(len(oversized))},
            content=oversized,
            request=request,
        )

    def large_client(**kwargs: object) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(large_handler), **kwargs)

    with pytest.raises(SafeRSSFetchError):
        SafeRSSFetcher(
            Settings(scout_max_response_bytes=1_024), client_factory=large_client
        ).fetch("https://example.com/feed")

    assert calls >= 2
