"""SSRF guard for the browser tools.

``browser_navigate`` / ``browser_download`` call :func:`check_navigable` before
hitting the network, so the headless browser can reach the public web without
becoming an internal-network probe. Pure stdlib — no Playwright dependency.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

#: Schemes the browser may navigate to. ``file://`` is a local-file-read
#: primitive; everything non-web is blocked.
_ALLOWED_SCHEMES = frozenset({"http", "https"})


def check_navigable(url: str) -> None:
    """Reject URLs that aren't public web pages (the SSRF guard).

    Blocks non-http(s) schemes (notably ``file://``) and any host that
    resolves to a loopback/private/link-local/reserved address — so the
    browser reaches the public web without becoming an internal-network
    probe. Raises ``ValueError`` with a plain message on block.
    """
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(
            f"blocked: only http/https URLs allowed, got {parsed.scheme or 'none'!r}"
        )
    host = parsed.hostname
    if not host:
        raise ValueError("blocked: URL has no host")
    for ip in _resolve_ips(host):
        if not ip.is_global:
            raise ValueError(f"blocked: {host} resolves to non-public address {ip}")


def _resolve_ips(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve ``host`` to every IP it maps to (literal, or via DNS).

    A failed lookup raises ``ValueError`` so navigation fails loudly rather
    than silently skipping the guard.
    """
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass  # not a literal IP — resolve the name
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ValueError(f"blocked: cannot resolve {host}: {exc}") from exc
    return [ipaddress.ip_address(info[4][0]) for info in infos]
