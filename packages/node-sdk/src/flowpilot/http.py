"""Controlled HTTP client with SSRF, private-network, domain-allowlist, and size protections."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from flowpilot.errors import RetryableNodeError, SecurityPolicyError

ALLOWED_SCHEMES = frozenset({"http", "https"})
MAX_REDIRECTS = 5
DEFAULT_MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # 5 MiB


def _is_forbidden_ip(ip_str: str) -> bool:
    """Return True if the IP is private, loopback, link-local, metadata, or reserved."""
    ip = ipaddress.ip_address(ip_str)
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_outbound_url(
    url: str,
    *,
    allowed_domains: Iterable[str] = (),
    allow_private_networks: bool = False,
    resolve_dns: bool = True,
) -> None:
    """Validate URL scheme, domain allowlist, and SSRF / private IP rules."""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise SecurityPolicyError(
            f"URL scheme '{parsed.scheme}' is not permitted; only http/https are allowed.",
            code="SSRF_SCHEME_BLOCKED",
        )

    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        raise SecurityPolicyError("URL must include a valid hostname.", code="SSRF_INVALID_HOST")

    allowed = [d.strip().lower() for d in allowed_domains if d.strip()]
    if allowed and not any(
        hostname == domain or hostname.endswith(f".{domain}") for domain in allowed
    ):
        raise SecurityPolicyError(
            f"Host '{hostname}' is not in the allowed_domains policy ({allowed}).",
            code="DOMAIN_NOT_ALLOWED",
        )

    if allow_private_networks:
        return

    if hostname in {"localhost", "localhost.localdomain", "metadata.google.internal"}:
        raise SecurityPolicyError(
            f"Outbound requests to '{hostname}' are blocked by SSRF policy.",
            code="SSRF_HOST_BLOCKED",
        )

    # Check literal IP address first
    try:
        if _is_forbidden_ip(hostname):
            raise SecurityPolicyError(
                f"Outbound requests to private/reserved IP '{hostname}' are blocked.",
                code="SSRF_IP_BLOCKED",
            )
        return
    except ValueError:
        pass

    # Resolve DNS to guard against DNS rebinding to private/metadata IP addresses
    if resolve_dns:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        try:
            addr_infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise RetryableNodeError(
                f"DNS resolution failed for host '{hostname}': {exc}",
                code="DNS_RESOLUTION_FAILED",
            ) from exc

        for info in addr_infos:
            ip_str = str(info[4][0])
            if _is_forbidden_ip(ip_str):
                raise SecurityPolicyError(
                    f"Host '{hostname}' resolved to blocked private/reserved IP '{ip_str}'.",
                    code="SSRF_DNS_REBINDING_BLOCKED",
                )


@dataclass(frozen=True)
class HttpResponseData:
    """Structured HTTP response returned by ControlledHttpClient."""

    status_code: int
    headers: dict[str, str]
    body_text: str
    body_json: Any | None
    url: str


class ControlledHttpClient:
    """Policy-enforcing async HTTP client injected into ExecutionContext."""

    def __init__(
        self,
        *,
        allowed_domains: Iterable[str] = (),
        allow_private_networks: bool = False,
        resolve_dns: bool = True,
        default_timeout_seconds: float = 15.0,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._allowed_domains = tuple(allowed_domains)
        self._allow_private_networks = allow_private_networks
        self._resolve_dns = resolve_dns if transport is None else False
        self._default_timeout = default_timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._transport = transport

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        body: str | bytes | None = None,
        timeout_seconds: float | None = None,
        follow_redirects: bool = True,
    ) -> HttpResponseData:
        """Perform an SSRF-checked HTTP request with hop-by-hop redirect validation."""
        current_url = url
        redirects_remaining = MAX_REDIRECTS if follow_redirects else 0
        timeout = httpx.Timeout(timeout_seconds or self._default_timeout)

        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            transport=self._transport,
        ) as client:
            while True:
                validate_outbound_url(
                    current_url,
                    allowed_domains=self._allowed_domains,
                    allow_private_networks=self._allow_private_networks,
                    resolve_dns=self._resolve_dns,
                )

                try:
                    response = await client.request(
                        method=method.upper(),
                        url=current_url,
                        headers=headers,
                        params=params,
                        json=json_body,
                        content=body,
                    )
                except httpx.TimeoutException as exc:
                    raise RetryableNodeError(
                        f"HTTP request to '{current_url}' timed out.",
                        code="HTTP_TIMEOUT",
                    ) from exc
                except httpx.TransportError as exc:
                    raise RetryableNodeError(
                        f"HTTP transport error for '{current_url}': {exc}",
                        code="HTTP_TRANSPORT_ERROR",
                    ) from exc

                if response.is_redirect and "location" in response.headers:
                    if redirects_remaining <= 0:
                        raise SecurityPolicyError(
                            f"Exceeded maximum redirects ({MAX_REDIRECTS}).",
                            code="HTTP_MAX_REDIRECTS",
                        )
                    redirects_remaining -= 1
                    current_url = urljoin(current_url, response.headers["location"])
                    continue

                raw = response.content
                if len(raw) > self._max_response_bytes:
                    raise SecurityPolicyError(
                        f"HTTP response size ({len(raw)} bytes) exceeds limit "
                        f"({self._max_response_bytes} bytes).",
                        code="HTTP_RESPONSE_TOO_LARGE",
                    )

                body_text = response.text
                body_json: Any | None = None
                content_type = response.headers.get("content-type", "")
                if "json" in content_type.lower() and body_text.strip():
                    try:
                        body_json = response.json()
                    except ValueError:
                        body_json = None

                return HttpResponseData(
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    body_text=body_text,
                    body_json=body_json,
                    url=str(response.url),
                )
