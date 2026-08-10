"""Guards on the enclosure URL, which is third-party input.

Anyone can submit a feed to the podcast directory, so a URL coming back from
the API is no more trustworthy than one a stranger typed. These tests pin the
two things that follow: only ordinary web schemes are opened, and nothing is
fetched from this server's own network.
"""

from __future__ import annotations

import asyncio

import pytest

from podcast_cutter import urls as urls_mod
from podcast_cutter.errors import AudioError, UnsafeSourceError
from podcast_cutter.urls import (
    ensure_safe_source,
    is_public_address,
    redirect_guard,
)


def _check(url: str, *, allow_private: bool = False, resolves_to=None):
    """Run the check with DNS replaced, so no test touches the network."""
    if resolves_to is not None:
        async def fake_addresses(host):
            return list(resolves_to)

        original = urls_mod._addresses_for
        urls_mod._addresses_for = fake_addresses
        try:
            return asyncio.run(
                ensure_safe_source(url, allow_private=allow_private)
            )
        finally:
            urls_mod._addresses_for = original
    return asyncio.run(ensure_safe_source(url, allow_private=allow_private))


class TestAddressClassification:
    @pytest.mark.parametrize(
        "address",
        [
            "8.8.8.8",
            "93.184.216.34",
            "2606:2800:220:1:248:1893:25c8:1946",
        ],
    )
    def test_public(self, address):
        assert is_public_address(address)

    @pytest.mark.parametrize(
        "address",
        [
            "127.0.0.1",
            "10.0.0.5",
            "192.168.1.1",
            "172.16.0.1",
            # The one that matters on a cloud host: instance metadata.
            "169.254.169.254",
            "0.0.0.0",
            "::1",
            "fd00::1",
            "224.0.0.1",
            "not-an-address",
        ],
    )
    def test_not_public(self, address):
        assert not is_public_address(address)


class TestScheme:
    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "concat:/etc/passwd|/etc/shadow",
            "subfile,,start,0,end,100,:///etc/passwd",
            "data:audio/mp3;base64,AAAA",
            "ftp://example.com/ep.mp3",
            "/etc/passwd",
        ],
    )
    def test_refuses_anything_but_http(self, url):
        with pytest.raises(UnsafeSourceError):
            _check(url)

    def test_refuses_a_url_with_no_host(self):
        with pytest.raises(UnsafeSourceError):
            _check("http:///ep.mp3")

    def test_scheme_is_checked_before_dns(self):
        """A refused scheme must not become a name lookup."""
        called = []

        async def explode(host):
            called.append(host)
            raise AssertionError("resolution should not have been attempted")

        original = urls_mod._addresses_for
        urls_mod._addresses_for = explode
        try:
            with pytest.raises(UnsafeSourceError):
                asyncio.run(ensure_safe_source("file:///etc/passwd"))
        finally:
            urls_mod._addresses_for = original
        assert called == []


class TestAddress:
    def test_allows_a_public_host(self):
        _check("https://cdn.example.com/ep.mp3", resolves_to=["93.184.216.34"])

    def test_refuses_a_host_pointing_at_loopback(self):
        with pytest.raises(UnsafeSourceError):
            _check("https://evil.example.com/ep.mp3", resolves_to=["127.0.0.1"])

    def test_refuses_cloud_metadata(self):
        with pytest.raises(UnsafeSourceError):
            _check("https://metadata.example/ep.mp3", resolves_to=["169.254.169.254"])

    def test_one_private_address_among_several_is_enough_to_refuse(self):
        """A name answering with both is a real technique, not a curiosity."""
        with pytest.raises(UnsafeSourceError):
            _check(
                "https://both.example.com/ep.mp3",
                resolves_to=["93.184.216.34", "10.1.2.3"],
            )

    def test_allow_private_skips_the_check(self):
        """What lets the integration tests serve audio from 127.0.0.1."""
        _check(
            "http://127.0.0.1:8000/ep.mp3",
            allow_private=True,
            resolves_to=["127.0.0.1"],
        )

    def test_unresolvable_name_is_allowed_through(self):
        """Fail open: the fetch that follows produces the real error.

        Refusing here would turn a DNS hiccup into a permanent, unexplainable
        "unsafe" verdict for a legitimate episode.
        """
        async def fail(host):
            raise OSError("Name or service not known")

        original = urls_mod._addresses_for
        urls_mod._addresses_for = fail
        try:
            asyncio.run(ensure_safe_source("https://gone.example.com/ep.mp3"))
        finally:
            urls_mod._addresses_for = original

    def test_is_an_audio_error_so_the_journal_keeps_its_taxonomy(self):
        assert issubclass(UnsafeSourceError, AudioError)
        assert UnsafeSourceError().code == "unsafe_source"


class FakeResponse:
    def __init__(self, status_code: int, location: str | None, url: str):
        self.status_code = status_code
        self.headers = {"location": location} if location else {}
        self.request = type("Request", (), {"url": url})()

    @property
    def is_redirect(self) -> bool:
        return self.status_code in (301, 302, 303, 307, 308)


class TestRedirectGuard:
    """The interesting hop is rarely the first one."""

    @staticmethod
    def _fire(response, allow_private=False, resolves_to=("93.184.216.34",)):
        async def fake_addresses(host):
            return list(resolves_to)

        original = urls_mod._addresses_for
        urls_mod._addresses_for = fake_addresses
        try:
            asyncio.run(redirect_guard(allow_private)(response))
        finally:
            urls_mod._addresses_for = original

    def test_passes_a_redirect_to_a_public_host(self):
        self._fire(
            FakeResponse(302, "https://cdn2.example.com/ep.mp3", "https://a/ep.mp3")
        )

    def test_refuses_a_redirect_into_private_space(self):
        with pytest.raises(UnsafeSourceError):
            self._fire(
                FakeResponse(302, "http://10.0.0.1/ep.mp3", "https://a/ep.mp3"),
                resolves_to=["10.0.0.1"],
            )

    def test_refuses_a_redirect_that_changes_scheme(self):
        with pytest.raises(UnsafeSourceError):
            self._fire(FakeResponse(302, "file:///etc/passwd", "https://a/ep.mp3"))

    def test_resolves_a_relative_location_against_the_request(self):
        """A bare path must be judged as the absolute URL it becomes."""
        with pytest.raises(UnsafeSourceError):
            self._fire(
                FakeResponse(302, "/ep.mp3", "http://127.0.0.1:9/x"),
                resolves_to=["127.0.0.1"],
            )

    def test_ignores_a_final_response(self):
        self._fire(FakeResponse(200, None, "https://a/ep.mp3"))
