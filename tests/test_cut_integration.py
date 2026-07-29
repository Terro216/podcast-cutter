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
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from podcast_cutter import audio as audio_mod
from podcast_cutter.audio import Interval, cut_episode, probe
from podcast_cutter.config import Settings
from podcast_cutter.errors import AudioError

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)

SOURCE_SECONDS = 30


def _settings(**overrides) -> Settings:
    return Settings(
        bot_token="t", api_key="k", api_secret="s", ffmpeg_timeout=120, **overrides
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

        if path.startswith("redirect/"):
            self.send_response(302)
            self.send_header("Location", "/" + path[len("redirect/") :])
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


@pytest.fixture(scope="module")
def audio_server(tmp_path_factory):
    """A local HTTP server exposing one episode per codec."""
    directory = tmp_path_factory.mktemp("sources")
    _make_source(directory, "a.mp3", ["-c:a", "libmp3lame", "-b:a", "64k"])
    _make_source(directory, "a.m4a", ["-c:a", "aac", "-b:a", "64k"])
    _make_source(directory, "a.wav", ["-c:a", "pcm_s16le"])

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


def cut(url: str, start: int, end: int, workdir: Path, **settings_overrides):
    return asyncio.run(
        cut_episode(
            url,
            Interval(start=start, end=end),
            workdir,
            _settings(**settings_overrides),
        )
    )


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
        with pytest.raises(AudioError, match="no usable audio link"):
            cut("file:///etc/passwd", 0, 5, tmp_path / "local")

    def test_messages_carry_no_raw_tracebacks(self, audio_server, tmp_path):
        with pytest.raises(AudioError) as excinfo:
            cut(f"{audio_server}/nope.mp3", 0, 5, tmp_path / "missing2")
        message = str(excinfo.value)
        assert "Traceback" not in message
        assert len(message) < 200


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
