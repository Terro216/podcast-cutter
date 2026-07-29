"""Interval parsing and audio extraction.

The cutting strategy, in order of preference:

1. Stream-copy straight from the episode URL. ffmpeg issues HTTP range
   requests, so this touches only the bytes around the requested interval and
   finishes in seconds.
2. If that fails (host blocks range requests, redirects oddly, or serves a
   container ffmpeg cannot seek remotely), download the episode once and
   stream-copy from the local file.
3. If stream-copying fails even locally — most often because the source codec
   cannot live in the chosen container — re-encode to MP3.

Step 3 is what the previous implementation was missing: it always wrote to
``.mp3`` with ``-c copy``, so every AAC/M4A podcast (a large share of the
directory) failed outright, and the "download the whole file" fallback reran
the exact same doomed command.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import Settings
from .errors import AudioError, IntervalError, TooLargeError
from .text import format_duration

logger = logging.getLogger(__name__)

StatusCallback = Callable[[str], Awaitable[None]]
#: Called with (bytes so far, total bytes or None) while downloading.
ProgressCallback = Callable[[int, "int | None"], Awaitable[None]]

#: Encoder presets. ``None`` means stream-copy — no re-encoding at all.
#: Voice notes must be Opus in an Ogg container; Telegram rejects anything else
#: from sendVoice.
_ENCODERS: dict[str, tuple[list[str], str]] = {
    "mp3": (["-c:a", "libmp3lame", "-b:a", "96k"], "mp3"),
    "opus": (["-c:a", "libopus", "-b:a", "48k", "-ac", "1"], "ogg"),
}

#: Codec -> container that can hold it without re-encoding.
#:
#: Only the lossy codecs podcasts actually ship in. Lossless sources (FLAC,
#: ALAC, raw PCM) are deliberately absent: they are vanishingly rare in feeds,
#: a stream copy of them blows past Telegram's size limit within a couple of
#: minutes, and copying FLAC leaves the original total-sample count in the
#: header, so the cut reports the *source* duration to players.
_COPY_CONTAINERS: dict[str, str] = {
    "mp3": "mp3",
    "aac": "m4a",
    "opus": "opus",
    "vorbis": "ogg",
}

#: Used when re-encoding. 96 kbps keeps a 15-minute cut near 10 MB.
_TRANSCODE_BITRATE = "96k"

_BROWSERISH_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_INTERVAL_SEPARATOR = re.compile(r"\s*(?:--|[-–—]|\.\.|\bto\b)\s*")
_COMPOUND_TIME = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")


# --------------------------------------------------------------------------
# Interval parsing (pure)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Interval:
    start: int
    end: int

    @property
    def duration(self) -> int:
        return self.end - self.start


def parse_timestamp(raw: str) -> int:
    """Parse a single timestamp into seconds.

    Accepts ``SS``, ``MM:SS``, ``HH:MM:SS`` and compound forms like ``1h30m``.
    """
    text = raw.strip().lower().replace(" ", "")
    if not text:
        raise IntervalError("A timestamp is missing.")

    if text.isdigit():
        return int(text)

    if ":" in text:
        parts = text.split(":")
        if len(parts) > 3 or not all(part.isdigit() for part in parts):
            raise IntervalError(
                f"“{raw.strip()}” is not a valid timestamp. Use MM:SS or HH:MM:SS."
            )
        values = [int(part) for part in parts]
        # Only the leading field may exceed 59; 12:75 is a typo, not 13:15.
        if any(value > 59 for value in values[1:]):
            raise IntervalError(
                f"“{raw.strip()}” has a minute or second value above 59."
            )
        total = 0
        for value in values:
            total = total * 60 + value
        return total

    match = _COMPOUND_TIME.fullmatch(text)
    if match and any(match.groups()):
        hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
        return hours * 3600 + minutes * 60 + seconds

    raise IntervalError(
        f"“{raw.strip()}” is not a valid timestamp. Try 01:20, 1:05:00 or 90s."
    )


def parse_interval(raw: str, max_duration: int) -> Interval:
    """Parse ``01:20-02:00`` (and friends) into a validated interval."""
    text = (raw or "").strip()
    parts = _INTERVAL_SEPARATOR.split(text, maxsplit=1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise IntervalError(
            "Send a start and an end separated by a hyphen, e.g. 01:20-02:00."
        )

    start = parse_timestamp(parts[0])
    end = parse_timestamp(parts[1])

    if end <= start:
        raise IntervalError("The end time must come after the start time.")

    duration = end - start
    if duration > max_duration:
        raise IntervalError(
            f"That is {format_duration(duration)} long. "
            f"The maximum is {format_duration(max_duration)}."
        )

    return Interval(start=start, end=end)


def has_range(raw: str) -> bool:
    """Whether the text names two endpoints rather than a single moment."""
    parts = _INTERVAL_SEPARATOR.split((raw or "").strip(), maxsplit=1)
    return len(parts) == 2 and bool(parts[0]) and bool(parts[1])


def parse_moment_or_range(
    raw: str, max_duration: int, default_length: int = 60
) -> Interval:
    """Parse either ``12:30`` or ``12:30-14:00``.

    Accepting a bare timestamp removes the most common friction: people paste a
    moment from show notes or a comment, and having to compute an end time to
    get a clip is needless arithmetic.
    """
    if has_range(raw):
        return parse_interval(raw, max_duration)

    start = parse_timestamp(raw)
    length = max(1, min(default_length, max_duration))
    return Interval(start=start, end=start + length)


# --------------------------------------------------------------------------
# ffmpeg / ffprobe
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceInfo:
    codec: str | None
    duration: int | None


@dataclass(frozen=True, slots=True)
class CutResult:
    path: Path
    size: int
    transcoded: bool


def ensure_ffmpeg_available() -> None:
    """Fail at startup rather than on a user's first cut."""
    missing = [tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None]
    if missing:
        raise AudioError(
            f"Required tool(s) not found on PATH: {', '.join(missing)}. "
            "Install ffmpeg."
        )


def _protocol_args(source: str | Path) -> list[str]:
    """HTTP-only ffmpeg options, omitted for local paths.

    ``-user_agent`` belongs to ffmpeg's http protocol. Passing it alongside a
    local file makes ffmpeg abort with "Option user_agent not found" — which
    silently broke the entire download-then-cut fallback.
    """
    if str(source).startswith(("http://", "https://")):
        return ["-user_agent", _BROWSERISH_USER_AGENT]
    return []


def container_for_codec(codec: str | None) -> str | None:
    """Container extension that can hold ``codec`` verbatim, if any."""
    if not codec:
        return None
    return _COPY_CONTAINERS.get(codec.lower())


async def _run(cmd: list[str], timeout: float) -> tuple[int, str]:
    """Run a subprocess, returning ``(returncode, stderr)``.

    A hung ffmpeg used to be able to block the bot forever; here it is killed
    once ``timeout`` elapses.
    """
    logger.info("Running: %s", " ".join(cmd))
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        with contextlib.suppress(ProcessLookupError):
            await process.wait()
        raise AudioError(
            "Audio processing took too long and was stopped. "
            "Try a shorter interval or a different episode."
        ) from None

    return process.returncode or 0, stderr.decode("utf-8", "replace").strip()


async def probe(source: str | Path, timeout: float) -> SourceInfo:
    """Read codec and duration of the first audio stream.

    Never raises: an unprobeable source is not fatal, it just means we fall
    back to guessing the container and skip the "start is past the end" check.
    """
    cmd = [
        "ffprobe",
        "-v",
        "error",
        *_protocol_args(source),
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_name:format=duration",
        "-of",
        "json",
        str(source),
    ]
    logger.info("Probing: %s", source)
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        process.kill()
        with contextlib.suppress(ProcessLookupError):
            await process.wait()
        logger.warning("ffprobe timed out for %s", source)
        return SourceInfo(codec=None, duration=None)

    if process.returncode != 0:
        logger.warning(
            "ffprobe failed for %s: %s", source, stderr.decode("utf-8", "replace")
        )
        return SourceInfo(codec=None, duration=None)

    try:
        payload = json.loads(stdout or b"{}")
    except ValueError:
        return SourceInfo(codec=None, duration=None)

    streams = payload.get("streams") or []
    codec = streams[0].get("codec_name") if streams else None

    duration: int | None = None
    raw_duration = (payload.get("format") or {}).get("duration")
    with contextlib.suppress(TypeError, ValueError):
        value = float(raw_duration)
        if value > 0:
            duration = int(value)

    return SourceInfo(codec=codec, duration=duration)


def _cut_command(
    source: str | Path,
    output: Path,
    interval: Interval,
    *,
    encode: str | None,
    metadata: dict[str, str] | None = None,
) -> list[str]:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        *_protocol_args(source),
        # -ss before -i seeks without decoding everything up to that point.
        "-ss",
        str(interval.start),
        "-i",
        str(source),
        "-t",
        str(interval.duration),
        # Take exactly one audio stream; cover art would otherwise be picked up
        # as a video stream and break the copy.
        "-map",
        "0:a:0",
        "-vn",
        # Drop the source's metadata. Feeds routinely carry enormous ID3 tags —
        # one popular show ships an 18 MB tag — and ffmpeg copies the whole
        # thing into the cut, so a 30-second clip came out at 20 MB.
        "-map_metadata",
        "-1",
    ]
    for key, value in (metadata or {}).items():
        if value:
            cmd += ["-metadata", f"{key}={value}"]
    cmd += _ENCODERS[encode][0] if encode else ["-c:a", "copy"]
    cmd.append(str(output))
    return cmd


async def _try_cut(
    source: str | Path,
    output: Path,
    interval: Interval,
    *,
    encode: str | None,
    timeout: float,
    verify_timeout: float = 30.0,
    metadata: dict[str, str] | None = None,
) -> str | None:
    """Attempt one cut. Returns ``None`` on success, else a reason string."""
    with contextlib.suppress(FileNotFoundError):
        output.unlink()

    code, stderr = await _run(
        _cut_command(source, output, interval, encode=encode, metadata=metadata),
        timeout,
    )

    if code != 0:
        return stderr or f"ffmpeg exited with {code}"

    # ffmpeg happily exits 0 while writing nothing — for instance when the
    # start time is past the end of the episode. Treat that as a failure.
    if not output.exists() or output.stat().st_size == 0:
        return "ffmpeg produced an empty file"

    # A zero exit and a non-empty file still are not proof of success: reading
    # a seek-hostile source over HTTP can yield a small unplayable fragment.
    # Only a file ffprobe can decode counts, so a bad result falls through to
    # the next strategy instead of being sent to the user.
    info = await probe(output, timeout=verify_timeout)
    if info.codec is None:
        return f"output is not decodable ({output.stat().st_size} bytes)"

    return None


async def _download(
    url: str,
    destination: Path,
    settings: Settings,
    on_progress: ProgressCallback | None = None,
) -> None:
    headers = {
        "User-Agent": _BROWSERISH_USER_AGENT,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Referer": "https://podcastindex.org/",
    }
    if not str(url).startswith(("http://", "https://")):
        # Episode URLs are validated when parsed, so reaching here means a bug
        # rather than bad user input; still, say something intelligible.
        raise AudioError("This episode has no usable audio link.")

    written = 0
    try:
        client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(settings.download_timeout, connect=30.0),
        )
        async with client, client.stream("GET", url, headers=headers) as response:
            if response.status_code == 403:
                raise AudioError(
                    "The host of this episode refuses downloads from this "
                    "server. Try a different episode."
                )
            if response.status_code >= 400:
                raise AudioError(f"The episode host returned {response.status_code}.")

            total: int | None = None
            # Absent on chunked responses, so a missing header is normal.
            with contextlib.suppress(TypeError, ValueError):
                total = int(response.headers.get("content-length"))

            with destination.open("wb") as handle:
                async for chunk in response.aiter_bytes(64 * 1024):
                    written += len(chunk)
                    if written > settings.max_source_bytes:
                        raise AudioError(
                            "This episode file is unusually large; refusing "
                            "to download it."
                        )
                    handle.write(chunk)
                    if on_progress is not None:
                        # The callback decides how often to actually surface
                        # this; downloads emit thousands of chunks.
                        with contextlib.suppress(Exception):
                            await on_progress(written, total)
    except httpx.HTTPError as exc:
        raise AudioError(f"Could not download the episode: {exc}") from exc

    if written == 0:
        raise AudioError("The episode host returned an empty file.")


async def _resolve_url(url: str, timeout: float) -> str:
    """Follow redirects in Python; some CDNs confuse ffmpeg's redirect handling."""
    headers = {"User-Agent": _BROWSERISH_USER_AGENT, "Accept": "*/*"}
    try:
        client = httpx.AsyncClient(
            follow_redirects=True, timeout=httpx.Timeout(timeout, connect=15.0)
        )
        # A ranged GET is cheap and works on hosts that reject HEAD.
        ranged = client.stream("GET", url, headers={**headers, "Range": "bytes=0-1"})
        async with client, ranged as response:
            return str(response.url)
    except httpx.HTTPError as exc:
        logger.warning("Could not resolve redirects for %s: %s", url, exc)
        return url


async def cut_episode(
    audio_url: str,
    interval: Interval,
    workdir: Path,
    settings: Settings,
    on_status: StatusCallback | None = None,
    on_progress: ProgressCallback | None = None,
    metadata: dict[str, str] | None = None,
    voice: bool = False,
) -> CutResult:
    """Extract ``interval`` from ``audio_url`` into ``workdir``.

    ``workdir`` is expected to be a per-job directory that the caller removes
    afterwards, so no temporary file can outlive the request. ``metadata``
    replaces the source's tags on the cut (see ``_cut_command``). With
    ``voice`` the result is Opus in Ogg, the only format sendVoice accepts.
    """

    async def status(message: str) -> None:
        if on_status is not None:
            with contextlib.suppress(Exception):
                await on_status(message)

    def plan(codec: str | None) -> list[tuple[Path, str | None]]:
        """Output candidates, best first.

        A voice note has exactly one option; otherwise prefer a stream copy
        into whatever container fits the source codec, and keep MP3 as the
        universal fallback.
        """
        if voice:
            return [(workdir / "cut.ogg", "opus")]

        candidates: list[tuple[Path, str | None]] = []
        copy_ext = container_for_codec(codec)
        if copy_ext:
            candidates.append((workdir / f"cut.{copy_ext}", None))
        candidates.append((workdir / "cut.mp3", "mp3"))
        return candidates

    shrink_with = "opus" if voice else "mp3"
    workdir.mkdir(parents=True, exist_ok=True)

    resolved_url = await _resolve_url(audio_url, settings.probe_timeout)
    info = await probe(resolved_url, settings.probe_timeout)

    if info.duration is not None and interval.start >= info.duration:
        raise AudioError(
            f"This episode is only {format_duration(info.duration)} long, "
            f"so {format_duration(interval.start)} is past the end."
        )

    # --- attempt 1: work directly off the URL -----------------------------
    await status("✂️ Cutting the segment…")
    for output, encode in plan(info.codec):
        reason = await _try_cut(
            resolved_url,
            output,
            interval,
            encode=encode,
            timeout=settings.ffmpeg_timeout,
            verify_timeout=settings.probe_timeout,
            metadata=metadata,
        )
        if reason is None:
            return await _finalize(
                output, encode, interval, workdir, settings, status,
                metadata, shrink_with,
            )
        logger.info("Streaming cut failed (encode=%s): %s", encode, reason[:500])

    # --- attempt 2: download the episode, then cut locally ----------------
    await status(
        "⬇️ This host does not allow partial reads — downloading "
        "the full episode first. This can take a couple of minutes…"
    )
    local_source = workdir / "source.bin"
    await _download(resolved_url, local_source, settings, on_progress)

    local_info = await probe(local_source, settings.probe_timeout)
    if local_info.duration is not None and interval.start >= local_info.duration:
        raise AudioError(
            f"This episode is only {format_duration(local_info.duration)} long, "
            f"so {format_duration(interval.start)} is past the end."
        )

    await status("✂️ Cutting the segment…")
    failures: list[str] = []
    for output, encode in plan(local_info.codec or info.codec):
        reason = await _try_cut(
            local_source,
            output,
            interval,
            encode=encode,
            timeout=settings.ffmpeg_timeout,
            verify_timeout=settings.probe_timeout,
            metadata=metadata,
        )
        if reason is None:
            local_source.unlink(missing_ok=True)
            return await _finalize(
                output, encode, interval, workdir, settings, status,
                metadata, shrink_with,
            )
        failures.append(reason)
        logger.info("Local cut failed (encode=%s): %s", encode, reason[:500])

    local_source.unlink(missing_ok=True)
    detail = failures[-1] if failures else "unknown error"
    logger.error("All cut attempts failed for %s: %s", audio_url, detail[:500])
    raise AudioError(
        "Could not cut this episode — the audio file appears to be unreadable."
    )


async def _finalize(
    output: Path,
    encode: str | None,
    interval: Interval,
    workdir: Path,
    settings: Settings,
    status: StatusCallback,
    metadata: dict[str, str] | None = None,
    shrink_with: str = "mp3",
) -> CutResult:
    """Enforce the upload size limit, re-encoding once if that might help."""
    size = output.stat().st_size

    if size > settings.max_upload_bytes and encode is None:
        logger.info(
            "Cut is %d bytes, above the %d limit; re-encoding.",
            size,
            settings.max_upload_bytes,
        )
        await status("🗜 The segment is large — compressing it…")
        # Deliberately not reusing the plain "cut.<ext>" name: it may be
        # `output` itself, and `_try_cut` deletes its destination first.
        compressed = workdir / f"compressed.{_ENCODERS[shrink_with][1]}"
        reason = await _try_cut(
            output,
            compressed,
            Interval(start=0, end=interval.duration),
            encode=shrink_with,
            timeout=settings.ffmpeg_timeout,
            verify_timeout=settings.probe_timeout,
            metadata=metadata,
        )
        if reason is None:
            output.unlink(missing_ok=True)
            output = compressed
            encode = shrink_with
            size = compressed.stat().st_size

    if size > settings.max_upload_bytes:
        raise TooLargeError(
            f"The cut is {size // (1024 * 1024)} MB, above the "
            f"{settings.max_upload_bytes // (1024 * 1024)} MB Telegram limit. "
            "Please pick a shorter interval."
        )

    return CutResult(path=output, size=size, transcoded=encode is not None)
