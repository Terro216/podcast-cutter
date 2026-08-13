"""End-to-end cutting tests against real files served over real HTTP.

These cover the failure that made the bot unusable for a large share of the
directory: every non-MP3 episode was written to a ``.mp3`` container with
``-c copy``, which ffmpeg always rejects, and the "download the whole file"
fallback reran the identical command.

The fixtures serve audio over a local HTTP server rather than handing ffmpeg a
file path, so the tests exercise the same code path production does — redirect
resolution, remote probing, and the download fallback included.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from podcast_cutter import audio as audio_mod
from podcast_cutter.audio import Interval, cut_episode, probe
from podcast_cutter.config import Settings
from podcast_cutter.errors import AudioError, UnsafeSourceError
from podcast_cutter.proxy import DIRECT, PROXY

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)

SOURCE_SECONDS = 30


def _settings(**overrides) -> Settings:
    # ``allow_private_sources`` because the whole point of these tests is that
    # audio travels over a real HTTP server, and that server is on 127.0.0.1.
    return Settings(
        bot_token="t",
        api_key="k",
        api_secret="s",
        ffmpeg_timeout=120,
        allow_private_sources=True,
        **overrides,
    )


def _make_source(directory: Path, name: str, codec_args: list[str]) -> Path:
    """Render a short sine tone so tests need no checked-in fixture files."""
    path = directory / name
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi",
            "-i", f"sine=frequency=440:duration={SOURCE_SECONDS}",
            *codec_args,
            str(path),
        ],
        check=True,
    )
    return path


#: Bloat per tag. A single argv entry is capped at 128 KB by the kernel, so the
#: fixture spreads the padding across several tags. Real feeds go far bigger —
#: one popular show ships an 18 MB ID3 tag — but the leak is proportional, so a
#: few hundred KB is enough to detect it.
BLOAT_PER_TAG = 100_000
BLOAT_TAGS = ("comment", "description", "synopsis")
BLOAT_BYTES = BLOAT_PER_TAG * len(BLOAT_TAGS)


def _make_bloated_source(directory: Path, name: str) -> Path:
    """An MP3 carrying a large ID3 tag, the way real podcast feeds do."""
    path = directory / name
    padding = []
    for tag in BLOAT_TAGS:
        padding += ["-metadata", f"{tag}={'x' * BLOAT_PER_TAG}"]

    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi",
            "-i", f"sine=frequency=440:duration={SOURCE_SECONDS}",
            "-c:a", "libmp3lame", "-b:a", "64k",
            *padding,
            str(path),
        ],
        check=True,
    )
    assert path.stat().st_size > BLOAT_BYTES, "the fixture is not actually bloated"
    return path


class _AudioHandler(BaseHTTPRequestHandler):
    """Serves the generated files, plus routes that misbehave on purpose.

    ``/403/<name>`` mimics the hosts that geofence server IPs; ``/redirect/``
    mimics the tracking-prefix redirects most podcast CDNs use.
    """

    def __init__(self, *args, directory: Path, **kwargs):
        self._directory = directory
        super().__init__(*args, **kwargs)

    def log_message(self, *args):  # keep pytest output readable
        pass

    def do_GET(self):  # noqa: N802 - name mandated by BaseHTTPRequestHandler
        path = self.path.lstrip("/")

        if path.startswith("403/"):
            self.send_error(403, "Forbidden")
            return

        # Serves the same URL differently depending on where the request came
        # from, which is the entire problem MEDIA_PROXY exists for. Standing in
        # for the source address: ``X-Egress``, which only the proxy fixture
        # below adds.
        if path.startswith("geofenced/"):
            if self.headers.get("X-Egress") is None:
                self.send_error(403, "Forbidden")
                return
            path = path[len("geofenced/") :]

        if path.startswith("redirect/"):
            self.send_response(302)
            self.send_header("Location", "/" + path[len("redirect/") :])
            self.end_headers()
            return

        # A host that answers httpx's resolution politely but 302s the fetch
        # that follows — the SSRF vector `-max_redirects 0` closes. The target
        # is an absolute off-limits URL so following it would be the bug.
        if path.startswith("evil-redirect/"):
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/")
            self.end_headers()
            return

        target = self._directory / path
        if not target.is_file():
            self.send_error(404, "Not Found")
            return

        payload = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _ProxyHandler(BaseHTTPRequestHandler):
    """A forward proxy of the kind ffmpeg and httpx expect for ``http://``.

    Both send an absolute-URI ``GET`` to the proxy rather than opening a
    CONNECT tunnel, which they reserve for ``https://``, so forwarding one
    request is all this has to do. It stamps ``X-Egress`` on what it forwards —
    that is how the geofenced route above tells the two egresses apart, the way
    a different source address does in production.

    Every forwarded request is recorded as ``(url, range header)``. The range
    is what distinguishes ffmpeg's ranged reads from the single ``bytes=0-1``
    of redirect resolution, and therefore what proves ffmpeg itself honoured
    ``http_proxy`` rather than quietly going direct.
    """

    def __init__(self, *args, seen: list, **kwargs):
        self._seen = seen
        super().__init__(*args, **kwargs)

    def log_message(self, *args):  # keep pytest output readable
        pass

    def do_GET(self):  # noqa: N802 - name mandated by BaseHTTPRequestHandler
        self._seen.append((self.path, self.headers.get("Range")))

        forwarded = {"X-Egress": "proxy"}
        for name in ("Range", "User-Agent", "Accept"):
            value = self.headers.get(name)
            if value is not None:
                forwarded[name] = value

        # An opener with an empty ProxyHandler, so an ambient http_proxy in the
        # environment cannot make this fixture forward to itself.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(
                urllib.request.Request(self.path, headers=forwarded), timeout=30
            ) as upstream:
                status, payload = upstream.status, upstream.read()
                content_type = upstream.headers.get("Content-Type")
        except urllib.error.HTTPError as exc:
            status, payload = exc.code, exc.read()
            content_type = exc.headers.get("Content-Type")

        self.send_response(status)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def media_proxy():
    """A live proxy plus the list of requests that went through it."""
    seen: list[tuple[str, str | None]] = []
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(_ProxyHandler, seen=seen)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield SimpleNamespace(url=f"http://{host}:{port}", seen=seen)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(scope="module")
def audio_server(tmp_path_factory):
    """A local HTTP server exposing one episode per codec."""
    directory = tmp_path_factory.mktemp("sources")
    _make_source(directory, "a.mp3", ["-c:a", "libmp3lame", "-b:a", "64k"])
    _make_source(directory, "a.m4a", ["-c:a", "aac", "-b:a", "64k"])
    _make_source(directory, "a.wav", ["-c:a", "pcm_s16le"])
    _make_bloated_source(directory, "bloated.mp3")

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(_AudioHandler, directory=directory)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def cut(url: str, start: int, end: int, workdir: Path, metadata=None, **overrides):
    return asyncio.run(
        cut_episode(
            url,
            Interval(start=start, end=end),
            workdir,
            _settings(**overrides),
            metadata=metadata,
        )
    )


def read_tags(path: Path) -> dict[str, str]:
    output = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format_tags",
            "-of", "default=nw=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    tags = {}
    for line in output.splitlines():
        key, _, value = line.partition("=")
        tags[key.removeprefix("TAG:").lower()] = value
    return tags


class TestProbe:
    @pytest.mark.parametrize(
        ("name", "codec"),
        [("a.mp3", "mp3"), ("a.m4a", "aac"), ("a.wav", "pcm_s16le")],
    )
    def test_reads_codec_and_duration_over_http(self, audio_server, name, codec):
        info = asyncio.run(probe(f"{audio_server}/{name}", timeout=60))
        assert info.codec == codec
        assert info.duration == pytest.approx(SOURCE_SECONDS, abs=1)

    def test_unreadable_source_is_not_fatal(self, tmp_path):
        junk = tmp_path / "not-audio.mp3"
        junk.write_bytes(b"this is not audio")
        info = asyncio.run(probe(junk, timeout=30))
        assert info.codec is None and info.duration is None


class TestCutting:
    @pytest.mark.parametrize(
        ("name", "expected_suffix", "transcoded"),
        [
            ("a.mp3", ".mp3", False),
            # Regression: AAC used to fail outright, in both the streaming and
            # the download path.
            ("a.m4a", ".m4a", False),
            # Lossless has no copy container by design, so it is re-encoded.
            ("a.wav", ".mp3", True),
        ],
    )
    def test_produces_a_playable_cut(
        self, audio_server, tmp_path, name, expected_suffix, transcoded
    ):
        result = cut(f"{audio_server}/{name}", 5, 15, tmp_path / name)

        assert result.path.suffix == expected_suffix
        assert result.transcoded is transcoded
        assert result.size > 0

        info = asyncio.run(probe(result.path, timeout=30))
        assert info.duration == pytest.approx(10, abs=1)

    def test_aac_is_never_written_into_an_mp3_container(self, audio_server, tmp_path):
        # The exact shape of the original bug.
        result = cut(f"{audio_server}/a.m4a", 0, 5, tmp_path / "aac")
        assert result.path.suffix != ".mp3"

    def test_follows_redirects(self, audio_server, tmp_path):
        # Podcast CDNs almost always front the audio with tracking redirects.
        result = cut(f"{audio_server}/redirect/a.mp3", 0, 5, tmp_path / "redir")
        assert result.size > 0

    def test_ffmpeg_itself_does_not_follow_a_redirect(self, audio_server):
        """The SSRF lock, at the layer it has to hold.

        httpx resolves the redirect chain and validates every hop, then hands
        ffmpeg the *final* URL. If ffmpeg followed redirects of its own — as it
        does by default — a host could 302 it to the metadata address after the
        address check had already passed. `probe` is called directly here,
        bypassing httpx resolution, so what it exercises is ffmpeg's own
        behaviour: with `-max_redirects 0` it must refuse rather than follow.
        """
        info = asyncio.run(probe(f"{audio_server}/evil-redirect/a.mp3", timeout=30))
        assert info.codec is None

    def test_falls_back_to_transcoding_when_no_container_fits(
        self, audio_server, tmp_path, monkeypatch
    ):
        # Simulate an exotic codec by removing every stream-copy option.
        monkeypatch.setattr(audio_mod, "_COPY_CONTAINERS", {})

        result = cut(f"{audio_server}/a.m4a", 0, 5, tmp_path / "transcode")

        assert result.path.suffix == ".mp3"
        assert result.transcoded is True
        assert asyncio.run(probe(result.path, timeout=30)).codec == "mp3"

    def test_start_past_the_end_reports_the_real_length(self, audio_server, tmp_path):
        with pytest.raises(AudioError, match="only"):
            cut(f"{audio_server}/a.mp3", 600, 700, tmp_path / "past-end")

    def test_interval_running_past_the_end_is_clamped(self, audio_server, tmp_path):
        # Starting inside the file but ending beyond it yields whatever audio
        # exists, rather than an error.
        result = cut(f"{audio_server}/a.mp3", 25, 120, tmp_path / "overrun")
        info = asyncio.run(probe(result.path, timeout=30))
        assert info.duration == pytest.approx(5, abs=1)

    def test_oversized_cut_is_recompressed_rather_than_refused(
        self, audio_server, tmp_path
    ):
        # 20s of WAV is ~3.5 MB, far above this artificially low ceiling; the
        # finalizer should re-encode instead of giving up.
        result = cut(
            f"{audio_server}/a.wav", 0, 20, tmp_path / "big", max_upload_bytes=400_000
        )
        assert result.transcoded is True
        assert result.size <= 400_000

    def test_temporary_files_stay_inside_the_job_directory(
        self, audio_server, tmp_path
    ):
        workdir = tmp_path / "job"
        cut(f"{audio_server}/a.mp3", 0, 5, workdir)

        # Deleting the caller's directory must be enough to clean up fully.
        assert workdir.exists()
        shutil.rmtree(workdir)
        assert not workdir.exists()


class TestMetadata:
    """Source tags are dropped; ours are written instead."""

    def test_a_giant_source_tag_does_not_end_up_in_the_cut(
        self, audio_server, tmp_path
    ):
        # Real regression: one popular feed ships an 18 MB ID3 tag, ffmpeg
        # copied it verbatim, and a 30-second clip came out at 20 MB.
        result = cut(f"{audio_server}/bloated.mp3", 5, 15, tmp_path / "bloated")

        # Ten seconds of 64 kbps audio is ~80 KB; a leak would add ~300 KB.
        assert result.size < BLOAT_BYTES / 2, (
            f"cut is {result.size} bytes; the source tag leaked into it"
        )

    def test_the_source_tags_do_not_survive_the_cut(self, audio_server, tmp_path):
        # Size is a proxy; this checks the tags themselves are gone.
        result = cut(f"{audio_server}/bloated.mp3", 5, 15, tmp_path / "tags-gone")
        tags = read_tags(result.path)
        for tag in BLOAT_TAGS:
            assert "x" * 100 not in tags.get(tag, ""), f"{tag} leaked from the source"

    def test_writes_our_own_tags(self, audio_server, tmp_path):
        result = cut(
            f"{audio_server}/a.mp3",
            5,
            15,
            tmp_path / "tagged",
            metadata={"title": "Ep 1 [0:05–0:15]", "artist": "Some Show"},
        )
        tags = read_tags(result.path)
        assert tags.get("title") == "Ep 1 [0:05–0:15]"
        assert tags.get("artist") == "Some Show"

    def test_survives_titles_with_awkward_characters(self, audio_server, tmp_path):
        # Tag values are passed as separate argv entries, so quotes and dashes
        # in a feed title cannot break the command.
        title = 'He said "hi" -- then left; rm -rf /'
        result = cut(
            f"{audio_server}/a.mp3",
            0,
            5,
            tmp_path / "awkward",
            metadata={"title": title},
        )
        assert read_tags(result.path).get("title") == title

    def test_empty_tag_values_are_skipped(self, audio_server, tmp_path):
        result = cut(
            f"{audio_server}/a.mp3",
            0,
            5,
            tmp_path / "empty-tags",
            metadata={"title": "", "artist": "Show"},
        )
        tags = read_tags(result.path)
        assert not tags.get("title")
        assert tags.get("artist") == "Show"


class TestFailureMessages:
    """Whatever goes wrong, the user must get something they can act on."""

    def test_missing_episode(self, audio_server, tmp_path):
        with pytest.raises(AudioError) as excinfo:
            cut(f"{audio_server}/nope.mp3", 0, 5, tmp_path / "missing")
        assert "404" in str(excinfo.value)

    def test_host_refusing_downloads(self, audio_server, tmp_path):
        with pytest.raises(AudioError, match="refuses downloads"):
            cut(f"{audio_server}/403/a.mp3", 0, 5, tmp_path / "forbidden")

    def test_non_http_source(self, tmp_path):
        """Refused before anything opens it, not after a fetch fails.

        This used to be caught inside the downloader, which meant a probe and
        a streaming cut had already been attempted against the path.
        """
        with pytest.raises(UnsafeSourceError, match="not an ordinary web download"):
            cut("file:///etc/passwd", 0, 5, tmp_path / "local")

    def test_a_source_longer_than_the_ceiling_is_refused(
        self, audio_server, tmp_path
    ):
        """The byte limit bounds the download; this bounds the work."""
        with pytest.raises(AudioError, match="past the"):
            cut(
                f"{audio_server}/a.mp3",
                0,
                5,
                tmp_path / "too-long",
                max_cut_seconds=5,
                max_source_seconds=10,
            )

    def test_a_source_inside_the_ceiling_still_cuts(self, audio_server, tmp_path):
        result = cut(
            f"{audio_server}/a.mp3",
            0,
            5,
            tmp_path / "short-enough",
            max_cut_seconds=5,
            max_source_seconds=60,
        )
        assert result.path.exists()

    def test_messages_carry_no_raw_tracebacks(self, audio_server, tmp_path):
        with pytest.raises(AudioError) as excinfo:
            cut(f"{audio_server}/nope.mp3", 0, 5, tmp_path / "missing2")
        message = str(excinfo.value)
        assert "Traceback" not in message
        assert len(message) < 200


class TestProxyRouting:
    """The audio detour, against a real proxy and real ffmpeg.

    Mocks cannot answer the two questions that matter here: whether ffmpeg
    actually honours ``http_proxy``, and whether a broken proxy costs the
    episodes that were working.
    """

    def test_working_hosts_stay_direct(self, audio_server, media_proxy, tmp_path):
        result = cut(
            f"{audio_server}/a.mp3",
            0,
            5,
            tmp_path / "direct",
            media_proxy=media_proxy.url,
        )
        assert result.route == DIRECT
        assert media_proxy.seen == [], (
            "fallback mode must not send the working majority through the proxy"
        )

    def test_a_refused_host_is_fetched_through_the_proxy(
        self, audio_server, media_proxy, tmp_path
    ):
        result = cut(
            f"{audio_server}/geofenced/a.mp3",
            0,
            5,
            tmp_path / "detour",
            media_proxy=media_proxy.url,
        )
        assert result.route == PROXY
        assert result.size > 0
        assert media_proxy.seen, "the detour should have carried this one"

    def test_ffmpeg_itself_honours_http_proxy(
        self, audio_server, media_proxy, tmp_path
    ):
        """The fact the whole design rests on.

        ffmpeg reads ``http_proxy`` (and ignores ``https_proxy``), so the cut
        itself — not just the Python-side redirect resolution — goes through the
        proxy. Ranged reads are ffmpeg's signature: redirect resolution asks for
        exactly ``bytes=0-1`` and the download fallback asks for no range at all.
        """
        cut(
            f"{audio_server}/geofenced/a.mp3",
            0,
            5,
            tmp_path / "ffmpeg-proxy",
            media_proxy=media_proxy.url,
        )
        ranged = [
            (url, header)
            for url, header in media_proxy.seen
            if header is not None and header != "bytes=0-1"
        ]
        assert ranged, (
            "no ranged request reached the proxy, so ffmpeg went direct: "
            f"{media_proxy.seen}"
        )

    def test_always_mode_routes_everything_through_the_proxy(
        self, audio_server, media_proxy, tmp_path
    ):
        result = cut(
            f"{audio_server}/a.mp3",
            0,
            5,
            tmp_path / "always",
            media_proxy=media_proxy.url,
            media_proxy_mode="always",
        )
        assert result.route == PROXY
        assert media_proxy.seen

    def test_a_dead_proxy_does_not_break_a_working_episode(
        self, audio_server, tmp_path
    ):
        """The nothing-breaks guarantee, end to end.

        ``always`` mode with a proxy that refuses connections is the worst case
        the design has to survive: every fetch tries the detour first, finds it
        dead, and falls back. The user still gets their clip.
        """
        result = cut(
            f"{audio_server}/a.mp3",
            0,
            5,
            tmp_path / "dead-proxy",
            # Port 1 is reserved and nothing listens there.
            media_proxy="http://127.0.0.1:1",
            media_proxy_mode="always",
        )
        assert result.route == DIRECT
        assert result.size > 0

    def test_off_mode_ignores_a_configured_proxy(
        self, audio_server, media_proxy, tmp_path
    ):
        result = cut(
            f"{audio_server}/a.mp3",
            0,
            5,
            tmp_path / "off",
            media_proxy=media_proxy.url,
            media_proxy_mode="off",
        )
        assert result.route == DIRECT
        assert media_proxy.seen == []

    def test_a_host_refusing_both_routes_still_reports_the_refusal(
        self, audio_server, media_proxy, tmp_path
    ):
        # The proxy adds X-Egress, but /403/ refuses regardless, so both routes
        # fail and the user must still get the message they got before.
        with pytest.raises(AudioError, match="refuses downloads"):
            cut(
                f"{audio_server}/403/a.mp3",
                0,
                5,
                tmp_path / "both-refused",
                media_proxy=media_proxy.url,
            )


class TestStatusUpdates:
    def test_reports_progress_to_the_caller(self, audio_server, tmp_path):
        seen: list[str] = []

        async def record(message: str) -> None:
            seen.append(message)

        asyncio.run(
            cut_episode(
                f"{audio_server}/a.mp3",
                Interval(start=0, end=5),
                tmp_path / "status",
                _settings(),
                on_status=record,
            )
        )
        assert seen, "the user should get at least one progress update"

    def test_a_failing_status_callback_does_not_break_the_cut(
        self, audio_server, tmp_path
    ):
        async def broken(message: str) -> None:
            raise RuntimeError("telegram is down")

        result = asyncio.run(
            cut_episode(
                f"{audio_server}/a.mp3",
                Interval(start=0, end=5),
                tmp_path / "broken-status",
                _settings(),
                on_status=broken,
            )
        )
        assert result.size > 0
