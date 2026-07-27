"""Bounded and SSRF-resistant transport for Scout RSS sources."""

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urljoin, urlsplit

import httpx

from app.core.config import Settings

_ALLOWED_CONTENT_TYPES = frozenset(
    {
        "application/atom+xml",
        "application/rss+xml",
        "application/xml",
        "text/xml",
    }
)


class SafeRSSFetchError(RuntimeError):
    """A sanitized, expected failure while retrieving one untrusted RSS URL."""


class SafeRSSFetcher:
    """Fetch public RSS payloads while validating every URL hop before use."""

    def __init__(
        self,
        settings: Settings,
        client_factory: Callable[..., httpx.Client] = httpx.Client,
    ) -> None:
        self._settings = settings
        self._client_factory = client_factory

    def fetch(self, url: str) -> bytes:
        """Return a bounded RSS payload after validating redirects and DNS results."""

        current_url = url
        timeout = httpx.Timeout(
            connect=self._settings.scout_connect_timeout_seconds,
            read=self._settings.scout_read_timeout_seconds,
            write=self._settings.scout_read_timeout_seconds,
            pool=self._settings.scout_connect_timeout_seconds,
        )
        with self._client_factory(
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": "GearIA-Scout/1.0 (+https://gearia.com.br)"},
        ) as client:
            for redirect_count in range(self._settings.scout_max_redirects + 1):
                self._validate_url(current_url)
                try:
                    with client.stream("GET", current_url) as response:
                        if response.is_redirect:
                            location = response.headers.get("location")
                            if not location:
                                raise SafeRSSFetchError("RSS redirect response is missing a location")
                            if redirect_count == self._settings.scout_max_redirects:
                                raise SafeRSSFetchError("RSS redirect limit exceeded")
                            current_url = urljoin(current_url, location)
                            continue
                        response.raise_for_status()
                        self._validate_content_type(response.headers.get("content-type"))
                        return self._read_bounded(response)
                except SafeRSSFetchError:
                    raise
                except httpx.TimeoutException as error:
                    raise SafeRSSFetchError("RSS request timed out") from error
                except httpx.HTTPError as error:
                    raise SafeRSSFetchError("RSS request failed") from error
        raise SafeRSSFetchError("RSS redirect limit exceeded")

    def _validate_url(self, url: str) -> None:
        """Reject private, local, credentialed, and unsupported request targets."""

        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"}:
            raise SafeRSSFetchError("RSS URL must use HTTP or HTTPS")
        if parsed.username is not None or parsed.password is not None:
            raise SafeRSSFetchError("RSS URL must not contain credentials")
        hostname = parsed.hostname
        if not hostname:
            raise SafeRSSFetchError("RSS URL must include a hostname")
        if hostname.lower().rstrip(".") in {"localhost", "localhost.localdomain"} or hostname.lower().endswith(".local"):
            raise SafeRSSFetchError("RSS URL resolves to a local hostname")
        try:
            literal_address = ipaddress.ip_address(hostname)
        except ValueError:
            literal_address = None
        if literal_address is not None:
            self._validate_ip(str(literal_address))
            return
        try:
            addresses = socket.getaddrinfo(hostname, parsed.port or 443 if parsed.scheme == "https" else 80)
        except socket.gaierror as error:
            raise SafeRSSFetchError("RSS hostname could not be resolved") from error
        if not addresses:
            raise SafeRSSFetchError("RSS hostname could not be resolved")
        for address in addresses:
            self._validate_ip(address[4][0])

    @staticmethod
    def _validate_ip(address: str) -> None:
        """Reject all IP address classes that must not be reachable from Scout."""

        try:
            value = ipaddress.ip_address(address)
        except ValueError as error:
            raise SafeRSSFetchError("RSS hostname resolved to an invalid address") from error
        if (
            value.is_loopback
            or value.is_private
            or value.is_link_local
            or value.is_multicast
            or value.is_reserved
            or value.is_unspecified
            or (isinstance(value, ipaddress.IPv6Address) and value.ipv4_mapped is not None)
        ):
            raise SafeRSSFetchError("RSS URL resolves to a prohibited network address")

    def _validate_content_type(self, content_type: str | None) -> None:
        """Reject declared non-feed content while allowing servers that omit the header."""

        if content_type is None:
            return
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type not in _ALLOWED_CONTENT_TYPES:
            raise SafeRSSFetchError("RSS response has an unsupported content type")

    def _read_bounded(self, response: httpx.Response) -> bytes:
        """Stream no more than the configured payload budget into memory."""

        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > self._settings.scout_max_response_bytes:
                    raise SafeRSSFetchError("RSS response exceeds the configured size limit")
            except ValueError as error:
                raise SafeRSSFetchError("RSS response has an invalid content length") from error
        payload = bytearray()
        for chunk in response.iter_bytes():
            payload.extend(chunk)
            if len(payload) > self._settings.scout_max_response_bytes:
                raise SafeRSSFetchError("RSS response exceeds the configured size limit")
        return bytes(payload)
