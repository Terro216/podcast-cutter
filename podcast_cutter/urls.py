"""Checks applied to an episode's audio URL before anything opens it.

Enclosure URLs are third-party input. They arrive from the podcast directory,
and anyone may submit a feed to it, so the fact that a URL came back from the
API says nothing about where it points. Two things follow:

* the scheme must be one we meant to support — ffmpeg speaks many protocols,
  and most of them have no business being reachable from a feed;
* the address behind the hostname must not be one of ours. A feed that points
  at ``169.254.169.254`` or ``127.0.0.1`` is asking this server to fetch from
  its own network and hand back the result.

The address check follows redirects because the interesting hop is rarely the
first one: a perfectly ordinary CDN hostname can redirect inward.

Failing open on an unresolvable name is deliberate. A name we cannot resolve is
a fetch that will fail on its own a moment later, with a message that says so;
refusing here would turn a DNS hiccup into a permanent "unsafe" verdict for a
legitimate episode, which is the kind of error nobody can diagnose from the
outside.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urljoin, urlsplit

from .errors import UnsafeSourceError

logger = logging.getLogger(__name__)

#: The only schemes an enclosure may use. ``file``, ``concat``, ``subfile`` and
#: friends are exactly what this list exists to keep out.
ALLOWED_SCHEMES = ("http", "https")


def is_public_address(address: str) -> bool:
    """Whether ``address`` is a routable public IP.

    ``is_global`` already excludes loopback, private ranges, link-local — and
    so the cloud metadata address — plus reserved and unspecified blocks, for
    both IPv4 and IPv6.
    """
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return parsed.is_global and not parsed.is_multicast


async def _addresses_for(host: str) -> list[str]:
    """Every address ``host`` resolves to, or an empty list if it resolves to none.

    A hostname with one public and one private address is a real technique, so
    the caller checks all of them rather than whichever one comes first.
    """
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [info[4][0] for info in infos]


async def ensure_safe_source(url: str, *, allow_private: bool = False) -> None:
    """Raise :class:`UnsafeSourceError` unless ``url`` is an ordinary public download.

    ``allow_private`` exists for the test suite, which serves audio from
    ``127.0.0.1`` over a real HTTP server on purpose — that is what makes those
    tests worth having. It is off in production.
    """
    parts = urlsplit(url)

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeSourceError("err_unsafe_scheme")

    host = parts.hostname
    if not host:
        raise UnsafeSourceError("err_unsafe_no_host")

    if allow_private:
        return

    try:
        addresses = await _addresses_for(host)
    except (OSError, UnicodeError) as exc:
        logger.info("Could not resolve %s to check its address: %s", host, exc)
        return

    private = [address for address in addresses if not is_public_address(address)]
    if private:
        logger.warning(
            "Refusing %s: %s resolves to %s", url, host, ", ".join(private)
        )
        raise UnsafeSourceError("err_unsafe_private")


def redirect_guard(allow_private: bool = False):
    """An httpx response hook that checks every hop of a redirect chain.

    httpx fires response hooks for intermediate redirects as well as the final
    response, so raising here stops the chain before the request to the next
    location is made — which is the only point at which stopping it matters.
    """

    async def hook(response) -> None:
        if not response.is_redirect:
            return
        location = response.headers.get("location")
        if not location:
            return
        await ensure_safe_source(
            urljoin(str(response.request.url), location), allow_private=allow_private
        )

    return hook
