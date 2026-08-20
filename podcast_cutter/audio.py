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
from .errors import (
    AudioError,
    BlockedError,
    IntervalError,
    ProcessingTimeout,
    TooLargeError,
    UnreachableError,
    UnreadableError,
)
from .i18n import t
from .proxy import DIRECT, MediaProxy, is_blocked_status, is_routing_failure
from .text import format_duration
from .urls import ensure_safe_source, redirect_guard

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

#: What an ffmpeg input is allowed to open. ``tcp``, ``tls`` and ``crypto``
#: are what ``http`` and ``https`` are built out of, so omitting them would
#: break ordinary fetches.
_REMOTE_PROTOCOLS = "http,https,tcp,tls,crypto"
#: Our own downloaded files and intermediate cuts. Nothing nested, so nothing
#: else needs to be reachable.
_LOCAL_PROTOCOLS = "file"

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
        raise IntervalError("err_ts_missing")

    if text.isdigit():
        return int(text)

    if ":" in text:
        parts = text.split(":")
        if len(parts) > 3 or not all(part.isdigit() for part in parts):
            raise IntervalError("err_ts_invalid", raw=raw.strip())
        values = [int(part) for part in parts]
        # Only the leading field may exceed 59; 12:75 is a typo, not 13:15.
        if any(value > 59 for value in values[1:]):
            raise IntervalError("err_ts_over59", raw=raw.strip())
        total = 0
        for value in values:
            total = total * 60 + value
        return total

    match = _COMPOUND_TIME.fullmatch(text)
    if match and any(match.groups()):
        hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
        return hours * 3600 + minutes * 60 + seconds

    raise IntervalError("err_ts_unparsed", raw=raw.strip())


def parse_interval(raw: str, max_duration: int) -> Interval:
    """Parse ``01:20-02:00`` (and friends) into a validated interval."""
    text = (raw or "").strip()
    parts = _INTERVAL_SEPARATOR.split(text, maxsplit=1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise IntervalError("err_range_format")

    start = parse_timestamp(parts[0])
    end = parse_timestamp(parts[1])

    if end <= start:
        raise IntervalError("err_end_before_start")

    duration = end - start
    if duration > max_duration:
        raise IntervalError(
            "err_interval_too_long",
            duration=format_duration(duration),
            max=format_duration(max_duration),
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
    #: A deliberately small allowlist of provenance/rightsholder tags.  Raw
    #: podcast files sometimes carry multi-megabyte comments and descriptions;
    #: blindly copying all global metadata makes a tiny cut enormous and can
    #: also publish arbitrary source fields the bot never intended to expose.
    metadata: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class CutResult:
    path: Path
    size: int
    transcoded: bool
    #: Which egress route produced this cut. ``DIRECT`` unless the proxy was
    #: both configured and actually used, so the journal can say how many
    #: episodes the detour is earning.
    route: str = DIRECT


def ensure_ffmpeg_available() -> None:
    """Fail at startup rather than on a user's first cut."""
    missing = [tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None]
    if missing:
        raise AudioError(
            f"Required tool(s) not found on PATH: {', '.join(missing)}. "
            "Install ffmpeg."
        )


def _protocol_args(source: str | Path) -> list[str]:
    """Protocol options for one input, which differ for remote and local sources.

    ``-user_agent`` belongs to ffmpeg's http protocol. Passing it alongside a
    local file makes ffmpeg abort with "Option user_agent not found" — which
    silently broke the entire download-then-cut fallback.

    ``-protocol_whitelist`` bounds what an *input* may go on to open by itself.
    A URL that answers with a playlist rather than audio can name further URLs,
    and ffmpeg will follow them; current versions already refuse the obvious
    ``file:`` case, so this is a second lock on a door that is mostly shut
    rather than a fix for an open one. Placed before ``-i``, it constrains the
    input only: the output is still written through the file protocol, which
    was verified rather than assumed.

    ``-max_redirects 0`` is the SSRF lock that the address check alone cannot
    provide. :func:`ensure_safe_source` validates every hop of the chain that
    *httpx* follows and hands ffmpeg the final resolved URL — but ffmpeg would
    otherwise follow redirects of its own, which nothing validated. Measured on
    this host's ffmpeg 7.1.5: a source answering ``302 Location:
    169.254.169.254`` made a default ffprobe connect straight to the metadata
    address; with ``0`` it refuses the redirect instead. The resolved URL is
    already past every redirect httpx saw, so a well-behaved host serves it 200
    and this costs nothing; a host that still redirects fails attempt 1 and
    falls to the download path, where httpx re-validates the new hop.
    """
    if str(source).startswith(("http://", "https://")):
        return [
            "-user_agent",
            _BROWSERISH_USER_AGENT,
            "-protocol_whitelist",
            _REMOTE_PROTOCOLS,
            "-max_redirects",
            "0",
        ]
    return ["-protocol_whitelist", _LOCAL_PROTOCOLS]


def container_for_codec(codec: str | None) -> str | None:
    """Container extension that can hold ``codec`` verbatim, if any."""
    if not codec:
        return None
    return _COPY_CONTAINERS.get(codec.lower())


async def _run(
    cmd: list[str], timeout: float, env: dict[str, str] | None = None
) -> tuple[int, str]:
    """Run a subprocess, returning ``(returncode, stderr)``.

    A hung ffmpeg used to be able to block the bot forever; here it is killed
    once ``timeout`` elapses. ``env`` replaces the child's environment
    wholesale — see :meth:`~podcast_cutter.proxy.MediaProxy.subprocess_env`;
    ``None`` inherits ours.
    """
    logger.info("Running: %s", " ".join(cmd))
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        with contextlib.suppress(ProcessLookupError):
            await process.wait()
        raise ProcessingTimeout from None
    except asyncio.CancelledError:
        # A cancelled cut (the user pressed /cancel) must not orphan an
        # ffmpeg that keeps encoding a clip nobody wants.
        process.kill()
        with contextlib.suppress(ProcessLookupError):
            await process.wait()
        raise

    return process.returncode or 0, stderr.decode("utf-8", "replace").strip()


async def probe(
    source: str | Path, timeout: float, env: dict[str, str] | None = None
) -> SourceInfo:
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
        "stream=codec_name:format=duration:format_tags",
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
        env=env,
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

    raw_tags = (payload.get("format") or {}).get("tags") or {}
    # Keep only fields that identify the work/rightsholder or its canonical
    # source.  Values are bounded so a hostile or merely eccentric feed cannot
    # smuggle a giant tag into every generated clip.
    allowed = {
        "artist",
        "album_artist",
        "copyright",
        "organization",
        "publisher",
        "purl",
        "webpage_url",
    }
    safe_metadata: list[tuple[str, str]] = []
    for raw_key, raw_value in raw_tags.items():
        key = str(raw_key).strip().lower()
        value = str(raw_value).strip()
        if key in allowed and value:
            safe_metadata.append((key, value[:512]))

    return SourceInfo(
        codec=codec,
        duration=duration,
        metadata=tuple(safe_metadata),
    )


def _clip_metadata(
    source: SourceInfo, supplied: dict[str, str] | None
) -> dict[str, str]:
    """Merge bounded source attribution with the bot's canonical clip tags."""
    merged = dict(source.metadata)
    merged.update(supplied or {})
    return merged


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
        # Never copy source metadata wholesale: real feeds carry enormous or
        # arbitrary tags. ``probe`` extracts a bounded provenance allowlist,
        # which the caller passes back explicitly below.
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
    env: dict[str, str] | None = None,
) -> str | None:
    """Attempt one cut. Returns ``None`` on success, else a reason string."""
    with contextlib.suppress(FileNotFoundError):
        output.unlink()

    code, stderr = await _run(
        _cut_command(source, output, interval, encode=encode, metadata=metadata),
        timeout,
        env=env,
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
    proxy_url: str | None = None,
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
        raise AudioError("err_no_audio_link")

    written = 0
    try:
        client = httpx.AsyncClient(
            follow_redirects=True,
            proxy=proxy_url,
            timeout=httpx.Timeout(settings.download_timeout, connect=30.0),
            event_hooks={
                "response": [redirect_guard(settings.allow_private_sources)]
            },
        )
        async with client, client.stream("GET", url, headers=headers) as response:
            # 401 and 403 mean this server is unwelcome, which no amount of
            # retrying or reshaping the request will change. Worth counting
            # separately from a host that is merely broken or missing.
            if response.status_code in (401, 403):
                raise BlockedError
            if response.status_code >= 400:
                raise UnreachableError(
                    "err_host_status", status=response.status_code
                )

            total: int | None = None
            # Absent on chunked responses, so a missing header is normal.
            with contextlib.suppress(TypeError, ValueError):
                total = int(response.headers.get("content-length"))

            with destination.open("wb") as handle:
                async for chunk in response.aiter_bytes(64 * 1024):
                    written += len(chunk)
                    if written > settings.max_source_bytes:
                        raise AudioError("err_source_too_big")
                    handle.write(chunk)
                    if on_progress is not None:
                        # The callback decides how often to actually surface
                        # this; downloads emit thousands of chunks.
                        with contextlib.suppress(Exception):
                            await on_progress(written, total)
    except httpx.HTTPError as exc:
        raise UnreachableError("err_download_failed", reason=str(exc)) from exc

    if written == 0:
        raise UnreachableError("err_empty_file")


async def _resolve_url(
    url: str, timeout: float, proxy: MediaProxy, allow_private: bool = False
) -> tuple[str, str]:
    """Follow redirects in Python and pick the egress route for this episode.

    Some CDNs confuse ffmpeg's redirect handling, which is why the chain is
    walked here in the first place. That makes this the natural place to choose
    a route as well: the ranged GET ends at the host that will actually serve
    the audio, so getting bytes back from it is proof the route works, and
    getting a connect timeout or a 403 is the exact signal the proxy exists
    for. Nothing extra is spent finding out — this request already happened.

    Returns ``(resolved_url, route)``. The route is sticky for the rest of the
    job on purpose: CDN URLs are often signed for the client that resolved
    them, so resolving through one route and fetching through another is a way
    to turn a working episode into a 403.
    """
    headers = {"User-Agent": _BROWSERISH_USER_AGENT, "Accept": "*/*"}
    routes = proxy.routes()
    resolved = url

    for route in routes:
        try:
            client = httpx.AsyncClient(
                follow_redirects=True,
                proxy=proxy.httpx_proxy(route),
                timeout=httpx.Timeout(
                    timeout, connect=proxy.connect_timeout(route, 15.0)
                ),
                event_hooks={"response": [redirect_guard(allow_private)]},
            )
            # A ranged GET is cheap and works on hosts that reject HEAD.
            ranged = client.stream(
                "GET", url, headers={**headers, "Range": "bytes=0-1"}
            )
            async with client, ranged as response:
                resolved = str(response.url)
                if is_blocked_status(response.status_code) and not proxy.is_last_resort(
                    route
                ):
                    logger.info(
                        "%s refused the %s route with %d; trying another route.",
                        httpx.URL(resolved).host,
                        route,
                        response.status_code,
                    )
                    continue
                if route != DIRECT:
                    logger.info("Resolved %s through the proxy.", url)
                    proxy.mark_up()
                return resolved, route
        except httpx.HTTPError as exc:
            if route != DIRECT and is_routing_failure(exc):
                # The proxy itself, not the episode host: stop trying it for
                # everyone rather than making each cut discover it.
                proxy.mark_down(f"{type(exc).__name__}: {exc}")
            # The class name matters: a bare ConnectTimeout stringifies to an
            # empty message, and "could not resolve X:" says nothing.
            logger.warning(
                "Could not resolve %s over the %s route: %s",
                url,
                route,
                f"{type(exc).__name__}: {exc}".rstrip(": "),
            )

    # Every route failed. Hand back what we have and let the fetch that
    # follows produce a proper, attributable error — which is what happened
    # before any of this existed.
    return resolved, routes[0]


async def cut_episode(
    audio_url: str,
    interval: Interval,
    workdir: Path,
    settings: Settings,
    on_status: StatusCallback | None = None,
    on_progress: ProgressCallback | None = None,
    metadata: dict[str, str] | None = None,
    voice: bool = False,
    proxy: MediaProxy | None = None,
    lang: str = "en",
) -> CutResult:
    """Extract ``interval`` from ``audio_url`` into ``workdir``.

    ``workdir`` is expected to be a per-job directory that the caller removes
    afterwards, so no temporary file can outlive the request. ``metadata``
    replaces the source's tags on the cut (see ``_cut_command``). With
    ``voice`` the result is Opus in Ogg, the only format sendVoice accepts.

    ``proxy`` carries the shared breaker state and should be the bot's single
    instance. Omitted, one is built from ``settings`` — which is inert unless
    ``MEDIA_PROXY`` is configured, so a caller that knows nothing about proxies
    gets exactly the behaviour it got before they existed.
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

    def check_length(duration: int | None) -> None:
        """Refuse an episode too long to be worth the work it would cost.

        The byte ceiling bounds the download but not the processing: seeking
        and re-encoding scale with the source, and a feed can advertise a file
        of any length at all.
        """
        if duration is not None and duration > settings.max_source_seconds:
            raise AudioError(
                "err_episode_too_long",
                duration=format_duration(duration),
                max=format_duration(settings.max_source_seconds),
            )

    shrink_with = "opus" if voice else "mp3"
    workdir.mkdir(parents=True, exist_ok=True)
    proxy = proxy if proxy is not None else MediaProxy(settings)

    # Before anything opens it. The URL came from a feed, and feeds are not
    # ours; see :mod:`podcast_cutter.urls`.
    await ensure_safe_source(
        audio_url, allow_private=settings.allow_private_sources
    )

    resolved_url, route = await _resolve_url(
        audio_url,
        settings.probe_timeout,
        proxy,
        allow_private=settings.allow_private_sources,
    )
    # The resolver walks redirects, and the guard on each hop only fires while
    # a chain is being followed. Where it ended up is checked here, because a
    # route that failed altogether hands back whatever it last saw.
    await ensure_safe_source(
        resolved_url, allow_private=settings.allow_private_sources
    )

    # Every remote ffmpeg call in this job follows the route the resolver
    # settled on. ``None`` when the feature is off, i.e. inherit our env.
    remote_env = proxy.subprocess_env(route)
    info = await probe(resolved_url, settings.probe_timeout, env=remote_env)
    effective_metadata = _clip_metadata(info, metadata)

    check_length(info.duration)
    if info.duration is not None and interval.start >= info.duration:
        raise AudioError(
            "err_past_end",
            duration=format_duration(info.duration),
            start=format_duration(interval.start),
        )

    # --- attempt 1: work directly off the URL -----------------------------
    await status(t(lang, "status_cutting"))
    for output, encode in plan(info.codec):
        reason = await _try_cut(
            resolved_url,
            output,
            interval,
            encode=encode,
            timeout=settings.ffmpeg_timeout,
            verify_timeout=settings.probe_timeout,
            metadata=effective_metadata,
            env=remote_env,
        )
        if reason is None:
            return await _finalize(
                output, encode, interval, workdir, settings, status,
                effective_metadata, shrink_with, route, lang,
            )
        logger.info("Streaming cut failed (encode=%s): %s", encode, reason[:500])

    # --- attempt 2: download the episode, then cut locally ----------------
    await status(t(lang, "status_full_download"))
    local_source = workdir / "source.bin"
    route = await _download_with_fallback(
        resolved_url, local_source, settings, proxy, route, on_progress
    )

    local_info = await probe(local_source, settings.probe_timeout)
    effective_metadata = _clip_metadata(local_info, metadata)
    # The remote probe can come back with nothing at all — a host that refuses
    # ffprobe still downloads fine — so this is the first length we see for
    # some episodes, not a repeat of the check above.
    check_length(local_info.duration)
    if local_info.duration is not None and interval.start >= local_info.duration:
        raise AudioError(
            "err_past_end",
            duration=format_duration(local_info.duration),
            start=format_duration(interval.start),
        )

    await status(t(lang, "status_cutting"))
    failures: list[str] = []
    for output, encode in plan(local_info.codec or info.codec):
        reason = await _try_cut(
            local_source,
            output,
            interval,
            encode=encode,
            timeout=settings.ffmpeg_timeout,
            verify_timeout=settings.probe_timeout,
            metadata=effective_metadata,
        )
        if reason is None:
            local_source.unlink(missing_ok=True)
            return await _finalize(
                output, encode, interval, workdir, settings, status,
                effective_metadata, shrink_with, route, lang,
            )
        failures.append(reason)
        logger.info("Local cut failed (encode=%s): %s", encode, reason[:500])

    local_source.unlink(missing_ok=True)
    detail = failures[-1] if failures else "unknown error"
    logger.error("All cut attempts failed for %s: %s", audio_url, detail[:500])
    raise UnreadableError


async def _download_with_fallback(
    url: str,
    destination: Path,
    settings: Settings,
    proxy: MediaProxy,
    route: str,
    on_progress: ProgressCallback | None = None,
) -> str:
    """Download the episode, changing route if the host turns us away.

    The resolver already picked a route that answered, so this almost always
    downloads on the first try. It can still fail — a signed URL expiring, a
    host that serves two bytes to a range request and refuses the whole file —
    and when it does, the other route is worth one attempt. Only refusals and
    unreachability are retried: a file that is too large, or a timeout while
    processing, means nothing about where the request came from.

    Returns the route that succeeded, so the caller can report it.
    """
    last_error: AudioError | None = None
    for attempt in (route, *proxy.alternatives(route)):
        try:
            await _download(
                url,
                destination,
                settings,
                on_progress,
                proxy_url=proxy.httpx_proxy(attempt),
            )
        except (BlockedError, UnreachableError) as exc:
            last_error = exc
            logger.info(
                "Downloading %s over the %s route failed: %s", url, attempt, exc
            )
            continue
        if attempt != route:
            logger.info("Downloaded %s over the %s route instead.", url, attempt)
        return attempt

    # Both routes refused. Report the failure the last attempt produced, which
    # keeps the error taxonomy — and so the journal — meaningful.
    raise last_error if last_error is not None else UnreachableError


async def _finalize(
    output: Path,
    encode: str | None,
    interval: Interval,
    workdir: Path,
    settings: Settings,
    status: StatusCallback,
    metadata: dict[str, str] | None = None,
    shrink_with: str = "mp3",
    route: str = DIRECT,
    lang: str = "en",
) -> CutResult:
    """Enforce the upload size limit, re-encoding once if that might help."""
    size = output.stat().st_size

    if size > settings.max_upload_bytes and encode is None:
        logger.info(
            "Cut is %d bytes, above the %d limit; re-encoding.",
            size,
            settings.max_upload_bytes,
        )
        await status(t(lang, "status_compressing"))
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
            "err_cut_too_large",
            size=size // (1024 * 1024),
            limit=settings.max_upload_bytes // (1024 * 1024),
        )

    return CutResult(
        path=output, size=size, transcoded=encode is not None, route=route
    )
