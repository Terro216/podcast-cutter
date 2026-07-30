"""Route selection, the circuit breaker, and what ffmpeg is told.

The guarantee these tests exist to hold: with ``MEDIA_PROXY`` unset nothing
changes, and with it set but broken nothing *breaks*. A detour that can take
the working majority of the directory down with it would be worse than the 22%
it was added to recover.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from podcast_cutter import audio as audio_mod
from podcast_cutter.audio import Interval, cut_episode
from podcast_cutter.config import Settings
from podcast_cutter.errors import ConfigError
from podcast_cutter.proxy import (
    DIRECT,
    PROXY,
    MediaProxy,
    is_blocked_status,
    is_routing_failure,
)

PROXY_URL = "http://proxy.internal:3128"


def _settings(**overrides) -> Settings:
    return Settings(bot_token="t", api_key="k", api_secret="s", **overrides)


class TestConfiguration:
    def test_absent_by_default(self):
        proxy = MediaProxy(_settings())
        assert not proxy.configured
        assert proxy.url is None
        assert proxy.routes() == (DIRECT,)

    def test_configured_url_enables_the_feature(self):
        proxy = MediaProxy(_settings(media_proxy=PROXY_URL))
        assert proxy.configured and proxy.available
        assert proxy.routes() == (DIRECT, PROXY)

    def test_off_mode_keeps_the_url_but_stops_using_it(self):
        """The kill switch: rollback without losing the configuration."""
        proxy = MediaProxy(_settings(media_proxy=PROXY_URL, media_proxy_mode="off"))
        assert not proxy.configured
        assert proxy.routes() == (DIRECT,)

    def test_always_mode_puts_the_proxy_first(self):
        proxy = MediaProxy(
            _settings(media_proxy=PROXY_URL, media_proxy_mode="always")
        )
        assert proxy.routes() == (PROXY, DIRECT)

    def test_unknown_mode_is_refused_at_startup(self):
        with pytest.raises(ConfigError, match="MEDIA_PROXY_MODE"):
            _settings(media_proxy_mode="sometimes")

    def test_non_http_proxy_is_refused_at_startup(self):
        # ffmpeg only understands a plain HTTP proxy, and a socks:// URL that
        # httpx accepts would work for downloads and silently not for cuts.
        with pytest.raises(ConfigError, match="http://"):
            _settings(media_proxy="socks5://proxy.internal:1080")


class TestBreaker:
    def test_failure_takes_the_proxy_out_of_rotation(self):
        proxy = MediaProxy(_settings(media_proxy=PROXY_URL))
        proxy.mark_down("ConnectError: refused")

        assert not proxy.available
        assert proxy.routes() == (DIRECT,)
        assert "refused" in proxy.down_reason

    def test_cooldown_expiry_lets_it_back_in(self):
        proxy = MediaProxy(
            _settings(media_proxy=PROXY_URL, media_proxy_cooldown=0.0)
        )
        proxy.mark_down("ConnectError: refused")
        assert proxy.available, "a zero cooldown should be immediately retried"

    def test_recovery_clears_the_reason(self):
        proxy = MediaProxy(_settings(media_proxy=PROXY_URL))
        proxy.mark_down("boom")
        proxy.mark_up()
        assert proxy.available and proxy.down_reason == ""

    def test_marking_an_unconfigured_proxy_is_harmless(self):
        proxy = MediaProxy(_settings())
        proxy.mark_down("boom")
        assert proxy.routes() == (DIRECT,)


class TestApplyingARoute:
    def test_ffmpeg_is_given_http_proxy_and_not_https_proxy(self):
        """ffmpeg reads http_proxy for https:// sources and ignores the other.

        Both halves were verified against a logging proxy. Setting
        ``https_proxy`` instead is indistinguishable from the feature being off.
        """
        proxy = MediaProxy(_settings(media_proxy=PROXY_URL))
        env = proxy.subprocess_env(PROXY)

        assert env["http_proxy"] == PROXY_URL
        assert "https_proxy" not in env
        assert "HTTPS_PROXY" not in env

    def test_direct_route_strips_inherited_proxy_variables(self, monkeypatch):
        monkeypatch.setenv("http_proxy", "http://somewhere-else:8080")
        monkeypatch.setenv("HTTPS_PROXY", "http://somewhere-else:8080")
        proxy = MediaProxy(_settings(media_proxy=PROXY_URL))

        env = proxy.subprocess_env(DIRECT)
        assert "http_proxy" not in env and "HTTPS_PROXY" not in env

    def test_environment_is_inherited_when_the_feature_is_off(self):
        # None, not a copy: an unconfigured bot must not even change how
        # subprocesses are spawned.
        assert MediaProxy(_settings()).subprocess_env(DIRECT) is None

    def test_httpx_proxy_only_for_the_proxy_route(self):
        proxy = MediaProxy(_settings(media_proxy=PROXY_URL))
        assert proxy.httpx_proxy(PROXY) == PROXY_URL
        assert proxy.httpx_proxy(DIRECT) is None

    def test_an_attempt_with_a_fallback_waits_less(self):
        """The failure being routed around costs ~9s of silent SYN retries."""
        proxy = MediaProxy(
            _settings(media_proxy=PROXY_URL, media_proxy_connect_timeout=8.0)
        )
        assert proxy.connect_timeout(DIRECT, 15.0) == 8.0
        # Nothing left to fall back to, so a slow host is worth the full wait.
        assert proxy.connect_timeout(PROXY, 15.0) == 15.0

    def test_without_a_proxy_the_normal_timeout_applies(self):
        assert MediaProxy(_settings()).connect_timeout(DIRECT, 15.0) == 15.0


class TestFailureClassification:
    @pytest.mark.parametrize("status", [401, 403, 451])
    def test_refusals_are_about_who_we_are(self, status):
        assert is_blocked_status(status)

    @pytest.mark.parametrize("status", [200, 206, 404, 500])
    def test_other_statuses_are_not(self, status):
        assert not is_blocked_status(status)

    @pytest.mark.parametrize(
        "exc",
        [
            httpx.ConnectTimeout("timed out"),
            httpx.ConnectError("refused"),
            httpx.ReadTimeout("stalled"),
            httpx.ProxyError("bad gateway"),
        ],
    )
    def test_transport_failures_are_routing_shaped(self, exc):
        assert is_routing_failure(exc)

    def test_a_readable_response_is_not_a_routing_failure(self):
        response = httpx.Response(500, request=httpx.Request("GET", "http://x/"))
        assert not is_routing_failure(
            httpx.HTTPStatusError("500", request=response.request, response=response)
        )


class TestResolverChoosesARoute:
    """``_resolve_url`` is where the fallback decision is made."""

    @staticmethod
    def _resolve(url, proxy):
        return asyncio.run(audio_mod._resolve_url(url, 5.0, proxy))

    def test_direct_success_never_touches_the_proxy(self, monkeypatch):
        used: list[str | None] = []

        monkeypatch.setattr(
            audio_mod.httpx, "AsyncClient", _fake_client(used, {None: 206})
        )
        proxy = MediaProxy(_settings(media_proxy=PROXY_URL))
        resolved, route = self._resolve("https://host/ep.mp3", proxy)

        assert route == DIRECT
        assert used == [None], "the working 78% must keep its existing route"
        assert resolved == "https://host/ep.mp3"

    def test_a_403_falls_back_to_the_proxy(self, monkeypatch):
        used: list[str | None] = []
        monkeypatch.setattr(
            audio_mod.httpx,
            "AsyncClient",
            _fake_client(used, {None: 403, PROXY_URL: 206}),
        )
        proxy = MediaProxy(_settings(media_proxy=PROXY_URL))

        assert self._resolve("https://anchor.fm/ep.mp3", proxy)[1] == PROXY
        assert used == [None, PROXY_URL]

    def test_a_connect_timeout_falls_back_to_the_proxy(self, monkeypatch):
        used: list[str | None] = []
        monkeypatch.setattr(
            audio_mod.httpx,
            "AsyncClient",
            _fake_client(used, {None: httpx.ConnectTimeout("dropped"), PROXY_URL: 206}),
        )
        proxy = MediaProxy(_settings(media_proxy=PROXY_URL))

        assert self._resolve("https://traffic.megaphone.fm/ep.mp3", proxy)[1] == PROXY

    def test_a_dead_proxy_leaves_the_direct_result_alone(self, monkeypatch):
        """The nothing-breaks case: a broken detour costs one extra attempt."""
        used: list[str | None] = []
        monkeypatch.setattr(
            audio_mod.httpx,
            "AsyncClient",
            _fake_client(
                used, {None: 403, PROXY_URL: httpx.ConnectError("no route")}
            ),
        )
        proxy = MediaProxy(_settings(media_proxy=PROXY_URL))

        resolved, route = self._resolve("https://anchor.fm/ep.mp3", proxy)
        assert route == DIRECT
        assert resolved == "https://anchor.fm/ep.mp3"
        assert not proxy.available, "a dead proxy must be taken out of rotation"

    def test_the_dead_proxy_is_skipped_for_the_next_episode(self, monkeypatch):
        used: list[str | None] = []
        monkeypatch.setattr(
            audio_mod.httpx,
            "AsyncClient",
            _fake_client(
                used, {None: 403, PROXY_URL: httpx.ConnectError("no route")}
            ),
        )
        proxy = MediaProxy(_settings(media_proxy=PROXY_URL))

        self._resolve("https://anchor.fm/one.mp3", proxy)
        used.clear()
        self._resolve("https://anchor.fm/two.mp3", proxy)

        assert used == [None], "every later cut should skip the known-dead proxy"

    def test_always_mode_starts_at_the_proxy(self, monkeypatch):
        used: list[str | None] = []
        monkeypatch.setattr(
            audio_mod.httpx, "AsyncClient", _fake_client(used, {PROXY_URL: 206})
        )
        proxy = MediaProxy(
            _settings(media_proxy=PROXY_URL, media_proxy_mode="always")
        )

        assert self._resolve("https://host/ep.mp3", proxy)[1] == PROXY
        assert used == [PROXY_URL]

    def test_without_a_proxy_a_403_resolves_as_it_always_did(self, monkeypatch):
        used: list[str | None] = []
        monkeypatch.setattr(
            audio_mod.httpx, "AsyncClient", _fake_client(used, {None: 403})
        )

        resolved, route = self._resolve(
            "https://anchor.fm/ep.mp3", MediaProxy(_settings())
        )
        assert (resolved, route) == ("https://anchor.fm/ep.mp3", DIRECT)
        assert used == [None]


class TestDownloadFallback:
    def test_a_refused_download_is_retried_on_the_other_route(self, monkeypatch):
        attempts: list[str | None] = []

        async def fake_download(url, destination, settings, on_progress=None,
                                proxy_url=None):
            attempts.append(proxy_url)
            if proxy_url is None:
                raise audio_mod.BlockedError
            destination.write_bytes(b"audio")

        monkeypatch.setattr(audio_mod, "_download", fake_download)
        proxy = MediaProxy(_settings(media_proxy=PROXY_URL))

        route = asyncio.run(
            audio_mod._download_with_fallback(
                "https://host/ep.mp3", _tmp(), _settings(), proxy, DIRECT
            )
        )
        assert route == PROXY
        assert attempts == [None, PROXY_URL]

    def test_a_size_refusal_is_not_a_routing_problem(self, monkeypatch):
        attempts: list[str | None] = []

        async def fake_download(url, destination, settings, on_progress=None,
                                proxy_url=None):
            attempts.append(proxy_url)
            raise audio_mod.AudioError("This episode file is unusually large")

        monkeypatch.setattr(audio_mod, "_download", fake_download)
        proxy = MediaProxy(_settings(media_proxy=PROXY_URL))

        with pytest.raises(audio_mod.AudioError, match="unusually large"):
            asyncio.run(
                audio_mod._download_with_fallback(
                    "https://host/ep.mp3", _tmp(), _settings(), proxy, DIRECT
                )
            )
        assert attempts == [None], "retrying elsewhere cannot make a file smaller"

    def test_both_routes_failing_reports_the_original_taxonomy(self, monkeypatch):
        async def fake_download(url, destination, settings, on_progress=None,
                                proxy_url=None):
            raise audio_mod.BlockedError

        monkeypatch.setattr(audio_mod, "_download", fake_download)
        proxy = MediaProxy(_settings(media_proxy=PROXY_URL))

        with pytest.raises(audio_mod.BlockedError):
            asyncio.run(
                audio_mod._download_with_fallback(
                    "https://host/ep.mp3", _tmp(), _settings(), proxy, DIRECT
                )
            )


class TestStartupCheck:
    def test_any_answer_through_the_proxy_counts_as_working(self, monkeypatch):
        # The question is whether the proxy forwards traffic at all; what the
        # far end thinks of a probe request is not our business.
        monkeypatch.setattr(
            "podcast_cutter.proxy.httpx.AsyncClient",
            _fake_client([], {PROXY_URL: 404}),
        )
        proxy = MediaProxy(_settings(media_proxy=PROXY_URL))

        assert asyncio.run(proxy.check()) is True

    def test_a_failing_check_marks_it_down_without_raising(self, monkeypatch):
        monkeypatch.setattr(
            "podcast_cutter.proxy.httpx.AsyncClient",
            _fake_client([], {PROXY_URL: httpx.ConnectError("refused")}),
        )
        proxy = MediaProxy(_settings(media_proxy=PROXY_URL))

        assert asyncio.run(proxy.check()) is False
        assert not proxy.available

    def test_an_unconfigured_proxy_checks_as_false(self):
        assert asyncio.run(MediaProxy(_settings()).check()) is False


class TestCutEpisodeIsInertWithoutAProxy:
    def test_no_proxy_means_no_environment_and_no_extra_requests(self, monkeypatch):
        """The parity test: unset MEDIA_PROXY must change nothing at all."""
        seen: dict[str, object] = {}

        async def fake_resolve(url, timeout, proxy):
            seen["routes"] = proxy.routes()
            return url, DIRECT

        async def fake_probe(source, timeout, env=None):
            seen["env"] = env
            return audio_mod.SourceInfo(codec="mp3", duration=600)

        async def fake_try_cut(source, output, interval, **kwargs):
            seen["cut_env"] = kwargs.get("env")
            output.write_bytes(b"x" * 32)
            return None

        monkeypatch.setattr(audio_mod, "_resolve_url", fake_resolve)
        monkeypatch.setattr(audio_mod, "probe", fake_probe)
        monkeypatch.setattr(audio_mod, "_try_cut", fake_try_cut)

        workdir = _tmp().parent / "job"
        result = asyncio.run(
            cut_episode(
                "https://host/ep.mp3",
                Interval(start=10, end=20),
                workdir,
                _settings(),
            )
        )

        assert seen["routes"] == (DIRECT,)
        assert seen["env"] is None and seen["cut_env"] is None
        assert result.route == DIRECT


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _tmp():
    import tempfile
    from pathlib import Path

    return Path(tempfile.mkdtemp()) / "source.bin"


def _fake_client(used: list, answers: dict):
    """An ``httpx.AsyncClient`` stand-in keyed by the proxy it was given.

    ``answers`` maps a proxy URL (``None`` for direct) to either a status code
    to answer with or an exception to raise, which is enough to drive every
    routing decision without a network.
    """

    class FakeResponse:
        def __init__(self, url: str, status_code: int):
            self.url = url
            self.status_code = status_code

    class FakeStream:
        def __init__(self, outcome, url):
            self._outcome = outcome
            self._url = url

        async def __aenter__(self):
            if isinstance(self._outcome, BaseException):
                raise self._outcome
            return FakeResponse(self._url, self._outcome)

        async def __aexit__(self, *exc_info):
            return False

    class FakeClient:
        def __init__(self, proxy=None, **kwargs):
            self._proxy = proxy
            used.append(proxy)
            self._outcome = answers.get(proxy, httpx.ConnectError("unconfigured"))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        def stream(self, method, url, **kwargs):
            return FakeStream(self._outcome, url)

        async def get(self, url, **kwargs):
            if isinstance(self._outcome, BaseException):
                raise self._outcome
            return FakeResponse(url, self._outcome)

    return FakeClient
