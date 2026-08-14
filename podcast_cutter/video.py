"""Rendering a cut into a video note — the shareable circle.

A Telegram video note is a square MPEG-4 of at most one minute that can only
be uploaded, never sent by URL, so rendering happens here on the server. The
picture is an ffmpeg audio visualiser under a skin: a frame, a title, a
progress bar and — when the episode has already been listened to — subtitles
burned in from the same transcript the search runs on.

Everything textual is passed to ffmpeg through files (``textfile=`` and an
``.ass`` script) rather than inline in the filter graph: episode titles
contain colons, quotes and every other character that is an operator to the
graph parser, and escaping them in place is exactly the kind of code that
works until the first Кофлан.

Measured in the production image before this module was shaped (60 s at
384×384, eight pinned cores): bars 2.5 s / 3.3 MB, spectrum 1.5 s / 0.9 MB,
scope 1.9 s / 0.8 MB, cover 2.7 s / 0.9 MB. A render is seconds — the cost
class of a cut, not of a transcription — which is why it runs inside the same
job slot as the cut that feeds it instead of in the durable listening queue.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from .audio import _protocol_args, _run, probe
from .config import Settings
from .errors import AudioError
from .transcripts import Utterance, is_indexable, quarantine_signals
from .urls import ensure_safe_source, redirect_guard

logger = logging.getLogger(__name__)

#: Telegram's hard ceiling for a video note. Not configuration — the API
#: rejects longer notes outright.
VIDEO_NOTE_SECONDS = 60

#: Longest clip rendered as an ordinary square video when it is too long to be
#: a note. Bounds both the encode time and the upload: the busiest skin
#: measured ~3.3 MB per minute, so five minutes stays well under the limit.
MAX_VIDEO_SECONDS = 300

#: The square's side. 384 is what Telegram clients typically record at, and
#: it is the size every render was measured at.
NOTE_SIZE = 384

SKIN_BARS = "bars"
SKIN_SPECTRUM = "spectrum"
SKIN_SCOPE = "scope"
SKIN_COVER = "cover"

#: Render behaviour lives here; the matching button labels live in
#: :mod:`keyboards`, which must not import the ffmpeg half of the world. A
#: test holds the two key sets equal.
SKINS = (SKIN_BARS, SKIN_SPECTRUM, SKIN_SCOPE, SKIN_COVER)

#: Refuse cover images beyond this. Artwork is decoration; a feed offering a
#: 100 MB "image" is not a feed to indulge.
MAX_COVER_BYTES = 10 * 1024 * 1024

_DEJAVU = Path("/usr/share/fonts/truetype/dejavu")
_ENCODE_ARGS = [
    "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
    "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-b:a", "96k",
    "-movflags", "+faststart",
    "-shortest",
]


@dataclass(frozen=True, slots=True)
class SubtitleLine:
    """One burned-in caption, timed relative to the clip's start."""

    start: float
    end: float
    text: str


def subtitle_lines(
    utterances: list[Utterance], clip_start: float, clip_end: float
) -> list[SubtitleLine]:
    """Captions for the stretch of episode the clip covers.

    Quarantine applies here too: a decoder loop that is kept out of the search
    index has no business being burned into a video either — and unlike a
    search answer, a video cannot be corrected after the fact.
    """
    lines: list[SubtitleLine] = []
    duration = clip_end - clip_start
    for utterance in utterances:
        if utterance.end <= clip_start or utterance.start >= clip_end:
            continue
        if not is_indexable(quarantine_signals(utterance)):
            continue
        start = max(0.0, utterance.start - clip_start)
        end = min(duration, utterance.end - clip_start)
        text = " ".join(utterance.text.split())
        if end - start < 0.3 or not text:
            continue
        lines.append(SubtitleLine(start=start, end=end, text=text))
    return lines


def _ass_time(seconds: float) -> str:
    centis = int(round(max(0.0, seconds) * 100))
    hours, rest = divmod(centis, 360000)
    minutes, rest = divmod(rest, 6000)
    return f"{hours}:{minutes:02d}:{rest // 100:02d}.{rest % 100:02d}"


def ass_document(lines: list[SubtitleLine], size: int = NOTE_SIZE) -> str:
    """An ASS script libass renders bottom-centred with an outline.

    ``{`` and ``}`` open override tags in ASS, so they are defused; a
    recogniser has produced stranger things than braces.
    """
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {size}\n"
        f"PlayResY: {size}\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, "
        "BackColour, Bold, Outline, Shadow, Alignment, MarginL, MarginR, "
        "MarginV\n"
        f"Style: Default,DejaVu Sans,{max(14, size // 19)},&H00FFFFFF,"
        "&H00000000,&H80000000,0,2,0,2,12,12,44\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Text\n"
    )
    events = "".join(
        "Dialogue: 0,"
        f"{_ass_time(line.start)},{_ass_time(line.end)},Default,"
        f"{line.text.replace('{', '(').replace('}', ')')}\n"
        for line in lines
    )
    return header + events


def _font(bold: bool) -> str:
    """A drawtext font argument that works in the image and degrades outside.

    The production image ships DejaVu at a known path; anywhere else,
    fontconfig picks a sans — the build enables it, and a slightly different
    face on a dev box is not worth failing a render over.
    """
    path = _DEJAVU / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")
    if path.exists():
        return f"fontfile='{path}'"
    return "font='sans'"


def _drawtext(
    textfile: Path,
    *,
    y: int,
    fontsize: int,
    color: str = "white",
    bold: bool = False,
    box: bool = False,
) -> str:
    parts = [
        _font(bold),
        f"textfile='{textfile}'",
        f"fontcolor={color}",
        f"fontsize={fontsize}",
        "x=(w-text_w)/2",
        f"y={y}",
    ]
    if box:
        parts += ["box=1", "boxcolor=black@0.45", "boxborderw=8"]
    return "drawtext=" + ":".join(parts)


def build_graph(
    skin: str,
    *,
    duration: float,
    title_file: Path,
    span_file: Path,
    subs_file: Path | None,
    with_cover: bool,
    size: int = NOTE_SIZE,
) -> str:
    """The whole filter graph for one skin, ending in ``[out]``.

    Layout is shared across skins so the presets read as variations of one
    design rather than four unrelated screens: title on top, visualiser in the
    middle, the time span above a progress bar along the bottom edge. The
    progress bar is a strip slid across by ``overlay``'s ``t`` — drawbox can
    animate too, but overlay's expressions are the documented, boring path.
    """
    if skin not in SKINS:
        raise ValueError(f"Unknown skin {skin!r}")

    pad = 8
    viz_w, viz_h = size - 2 * pad, size - 144
    viz_y = 72
    dur = max(0.1, duration)

    if skin == SKIN_COVER:
        if with_cover:
            base = (
                f"[1:v]scale={size}:{size}:force_original_aspect_ratio="
                f"increase,crop={size}:{size},eq=brightness=-0.15[canvas];"
            )
        else:
            # No artwork came with the episode; an honest dark card keeps the
            # title and subtitles rather than pretending another skin was
            # asked for.
            base = f"color=c=0x1a1a2e:s={size}x{size}:d={dur}[canvas];"
        bar_color = "white@0.85"
        title_box = True
    else:
        viz = {
            SKIN_BARS: (
                "0x0d0d1a",
                f"showfreqs=s={viz_w}x{viz_h}:mode=bar:ascale=log:fscale=log"
                ":colors=0x00e07c|0xffcc00",
            ),
            SKIN_SPECTRUM: (
                "0x0a0a14",
                f"showcqt=s={viz_w}x{viz_h}:count=2:bar_g=2:sono_g=4"
                ":sono_h=0:axis_h=0",
            ),
            SKIN_SCOPE: (
                "0x001100",
                f"avectorscope=s={viz_w}x{viz_h}:draw=line:zoom=1.5",
            ),
        }[skin]
        base = (
            f"color=c={viz[0]}:s={size}x{size}:d={dur}[bg];"
            f"[0:a]{viz[1]}[viz];"
            f"[bg][viz]overlay={pad}:{viz_y}:shortest=1[canvas];"
        )
        bar_color = "0xffcc00@0.9" if skin == SKIN_BARS else "white@0.7"
        title_box = False

    title_color = "0x33ff66" if skin == SKIN_SCOPE else "white"
    chain = (
        base
        + f"color=c={bar_color}:s={size}x6:d={dur}[bar];"
        + f"[canvas][bar]overlay=x='-W+W*t/{dur}':y={size - 14}"
        + (":shortest=1" if skin == SKIN_COVER and with_cover else "")
        + "[timed];"
        + "[timed]"
        + _drawtext(
            title_file, y=16, fontsize=15, color=title_color, bold=True,
            box=title_box,
        )
        + ","
        + _drawtext(span_file, y=size - 34, fontsize=13, color="0xaaaaaa")
    )
    if subs_file is not None:
        chain += f",subtitles=filename='{subs_file}'"
    return chain + "[out]"


async def fetch_cover(url: str, workdir: Path, settings: Settings) -> Path | None:
    """Download episode artwork, or quietly do without.

    The URL comes from the same third-party feed the enclosure does, so it
    passes the same address checks before anything opens it. Failure of any
    kind returns ``None`` rather than raising: artwork is decoration, and a
    missing picture must never cost anyone their clip.
    """
    if not url.startswith(("http://", "https://")):
        return None
    destination = workdir / "cover.img"
    try:
        await ensure_safe_source(
            url, allow_private=settings.allow_private_sources
        )
        client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(20.0, connect=10.0),
            event_hooks={
                "response": [redirect_guard(settings.allow_private_sources)]
            },
        )
        written = 0
        async with client, client.stream("GET", url) as response:
            if response.status_code >= 400:
                return None
            with destination.open("wb") as handle:
                async for chunk in response.aiter_bytes(64 * 1024):
                    written += len(chunk)
                    if written > MAX_COVER_BYTES:
                        return None
                    handle.write(chunk)
        return destination if written else None
    except Exception as exc:
        logger.info("No cover art from %s: %s", url, exc)
        return None


async def _cover_usable(cover: Path, timeout: float = 30.0) -> bool:
    """Whether ffmpeg can actually decode this file as a picture.

    Checked by decoding one frame rather than by trusting a probe, because
    the failure this guards against is not an error but a hang: ffmpeg 7.1.5
    given an undecodable second input reports ``Invalid data`` and then sits
    there — reproduced on this host — so by the time a render discovers the
    problem, it has already cost the whole ``ffmpeg_timeout``. ``-map 0:v:0``
    is what makes a video stream mandatory; without it an audio file posing
    as artwork would pass.
    """
    code, _ = await _run(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-v", "error",
            *_protocol_args(cover), "-i", str(cover),
            "-map", "0:v:0", "-frames:v", "1", "-f", "null", "-",
        ],
        timeout,
    )
    return code == 0


async def render_clip(
    audio: Path,
    workdir: Path,
    *,
    skin: str,
    duration: float,
    title: str,
    span: str,
    subtitles: list[SubtitleLine] | None,
    cover: Path | None,
    settings: Settings,
) -> Path:
    """Render the cut audio into a square video, verified before it is sent.

    The cover is test-decoded first and dropped if unreadable — see
    :func:`_cover_usable` for why that cannot wait for the render to find
    out. A render that then still fails *quickly* with artwork is retried
    once without it, losing the picture rather than the clip; a render that
    times out is not retried, because its failure already cost minutes.
    """
    title_file = workdir / "title.txt"
    span_file = workdir / "span.txt"
    title_file.write_text(title, encoding="utf-8")
    span_file.write_text(span, encoding="utf-8")

    subs_file: Path | None = None
    if subtitles:
        subs_file = workdir / "subs.ass"
        subs_file.write_text(ass_document(subtitles), encoding="utf-8")

    if cover is not None and not await _cover_usable(cover):
        logger.info("Cover art is not decodable; rendering without it.")
        cover = None

    output = workdir / "note.mp4"
    attempts = [cover] if cover is None else [cover, None]
    reason = ""
    for attempt_cover in attempts:
        reason = await _render_once(
            audio, output,
            skin=skin, duration=duration, title_file=title_file,
            span_file=span_file, subs_file=subs_file, cover=attempt_cover,
            timeout=settings.ffmpeg_timeout,
        )
        if reason is None:
            return output
        if attempt_cover is not None:
            logger.info(
                "Render with cover art failed (%s); retrying without it.",
                reason[:300],
            )
    logger.error("Video render failed: %s", reason[:500])
    raise AudioError("Could not render the video for this clip.")


async def _render_once(
    audio: Path,
    output: Path,
    *,
    skin: str,
    duration: float,
    title_file: Path,
    span_file: Path,
    subs_file: Path | None,
    cover: Path | None,
    timeout: float,
) -> str | None:
    """One render attempt. ``None`` on success, else the reason it failed."""
    with contextlib.suppress(FileNotFoundError):
        output.unlink()

    graph = build_graph(
        skin,
        duration=duration,
        title_file=title_file,
        span_file=span_file,
        subs_file=subs_file,
        with_cover=cover is not None,
    )
    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        *_protocol_args(audio), "-i", str(audio),
    ]
    if cover is not None:
        cmd += ["-loop", "1", *_protocol_args(cover), "-i", str(cover)]
    cmd += ["-filter_complex", graph, "-map", "[out]", "-map", "0:a"]
    cmd += [*_ENCODE_ARGS, str(output)]

    code, stderr = await _run(cmd, timeout)
    if code != 0:
        return stderr or f"ffmpeg exited with {code}"
    if not output.exists() or output.stat().st_size == 0:
        return "ffmpeg produced an empty file"
    # The same lesson as cutting: exit 0 is not proof. A file ffprobe cannot
    # decode is not something to upload.
    info = await probe(output, timeout=30.0)
    if info.codec is None:
        return f"output is not decodable ({output.stat().st_size} bytes)"
    return None
