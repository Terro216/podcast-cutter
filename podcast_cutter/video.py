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

Measured in the production image when the second-generation skins landed
(30 s of real speech at 384×384, eight pinned cores): brainrot 2.5 s /
1.4 MB, cover 3.4 s / 0.5 MB, aurora 4.2 s / 0.6 MB, vinyl 4.4 s / 0.6 MB,
dvd 7.0 s / 1.0 MB, fractal 11 s / 4.1 MB, lava 11.6 s / 1.1 MB, party
12.5 s / 1.8 MB, matrix 24 s / 4.0 MB. A render is seconds — the cost class
of a cut, not of a transcription — which is why it runs inside the same job
slot as the cut that feeds it instead of in the durable listening queue.
"""

from __future__ import annotations

import contextlib
import logging
import random
from dataclasses import dataclass
from pathlib import Path

import httpx

from .audio import _protocol_args, _run, probe
from .config import Settings
from .errors import AudioError
from .text import truncate
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

SKIN_COVER = "cover"
SKIN_VINYL = "vinyl"
SKIN_AURORA = "aurora"
SKIN_PARTY = "party"
SKIN_LAVA = "lava"
SKIN_MATRIX = "matrix"
SKIN_FRACTAL = "fractal"
SKIN_DVD = "dvd"
#: The loop-backed pair: ``random`` plays any file the operator dropped on
#: the volume, ``subway`` only what lives in the ``subway/`` subdirectory.
SKIN_RANDOM = "random"
SKIN_SUBWAY = "subway"

#: Render behaviour lives here; the matching button labels live in
#: :mod:`keyboards`, which must not import the ffmpeg half of the world. A
#: test holds the two key sets equal.
SKINS = (
    SKIN_COVER,
    SKIN_VINYL,
    SKIN_AURORA,
    SKIN_PARTY,
    SKIN_LAVA,
    SKIN_MATRIX,
    SKIN_FRACTAL,
    SKIN_DVD,
    SKIN_RANDOM,
    SKIN_SUBWAY,
)

#: The loop-backed skins, and where each looks for its footage relative to
#: ``settings.brainrot_dir``: ``random`` sweeps the whole tree, a themed
#: skin only its own subdirectory.
LOOP_SKINS: dict[str, str | None] = {
    SKIN_RANDOM: None,
    SKIN_SUBWAY: "subway",
}

#: Retired skins, mapped to their closest living relative. Buttons on
#: scrolled-past messages outlive keyboards, and a session that chose a look
#: before a redeploy should get *a* video, not a ValueError.
LEGACY_SKINS = {
    "bars": SKIN_PARTY,
    "spectrum": SKIN_AURORA,
    "scope": SKIN_MATRIX,
    "vhs": SKIN_VINYL,
    "brainrot": SKIN_RANDOM,
}

#: Skins whose second input is the episode artwork; the caller only fetches
#: a cover when the skin will actually put it on screen.
COVER_SKINS = frozenset({SKIN_COVER, SKIN_VINYL, SKIN_DVD})

#: Container suffixes accepted as brainrot background loops.
BACKGROUND_SUFFIXES = frozenset({".mp4", ".mov", ".mkv", ".webm", ".m4v"})

#: Refuse cover images beyond this. Artwork is decoration; a feed offering a
#: 100 MB "image" is not a feed to indulge.
MAX_COVER_BYTES = 10 * 1024 * 1024

_DEJAVU = Path("/usr/share/fonts/truetype/dejavu")
_ENCODE_ARGS = [
    "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
    # The VBV cap is for the noisy skins: an uncapped Mandelbrot dive
    # measured ~2 Mbit/s, which over a five-minute square video overshoots
    # the upload ceiling. 1 Mbit/s tops out near 40 MB with audio and looks
    # the same at 384 px.
    "-maxrate", "1M", "-bufsize", "2M",
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


def ass_document(
    lines: list[SubtitleLine],
    size: int = NOTE_SIZE,
    round_frame: bool = False,
    centered: bool = False,
) -> str:
    """An ASS script libass renders bottom-centred with an outline.

    ``{`` and ``}`` open override tags in ASS, so they are defused; a
    recogniser has produced stranger things than braces.

    ``round_frame`` widens the side margins and lifts the block: a video
    note is shown cropped to the circle inscribed in the square, so text
    near the frame's edge is text the viewer never sees.

    ``centered`` is the brainrot layout: captions in the middle of the
    frame, bigger and bold, because over gameplay footage the words *are*
    the content.
    """
    side = size // 6 if round_frame else size // 32
    bottom = int(size * 0.31) if round_frame else int(size * 0.115)
    if centered:
        fontsize, bold, alignment = size // 15, 1, 5
    else:
        fontsize, bold, alignment = max(14, size // 21), 0, 2
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
        f"Style: Default,DejaVu Sans,{fontsize},&H00FFFFFF,"
        f"&H00000000,&H80000000,{bold},2,0,{alignment},{side},{side},{bottom}\n"
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
    textfile: Path | None = None,
    *,
    text: str | None = None,
    x: str = "(w-text_w)/2",
    y: int,
    fontsize: int,
    color: str = "white",
    bold: bool = False,
    box: bool = False,
) -> str:
    """One drawtext filter. User-controlled strings go through ``textfile``;
    inline ``text`` is reserved for strings this module wrote itself (the
    running clock), which must never contain filter-graph operators."""
    source = (
        f"textfile='{textfile}'" if textfile is not None else f"text='{text}'"
    )
    parts = [
        _font(bold),
        source,
        f"fontcolor={color}",
        f"fontsize={fontsize}",
        f"x={x}",
        f"y={y}",
    ]
    if box:
        parts += ["box=1", "boxcolor=black@0.45", "boxborderw=6"]
    return "drawtext=" + ":".join(parts)


#: The running clock next to the progress bar: minutes and zero-padded
#: seconds of the *clip*, ticking as it plays. ``eif`` needs its colons
#: escaped one level deeper than the option quoting, hence the backslashes.
_CLOCK_TEXT = r"%{eif\:trunc(t/60)\:d}\:%{eif\:mod(trunc(t)\,60)\:d\:2}"


@dataclass(frozen=True, slots=True)
class _Canvas:
    """One skin's picture, as a graph fragment ending in ``[canvas]``."""

    graph: str
    bar_color: str
    title_color: str
    #: Busy, unpredictable background: put dark boxes behind every piece of
    #: text and under the progress bar, or a bright cover eats them alive.
    boxed: bool = False
    #: The canvas never ends on its own (looped stills, endless generators);
    #: the progress-bar overlay then carries ``shortest=1`` to end the video.
    endless: bool = False


def _dark_card(size: int, dur: float) -> str:
    # No artwork came with the episode; an honest dark card keeps the title
    # and subtitles rather than pretending another skin was asked for.
    return f"color=c=0x1a1a2e:s={size}x{size}:d={dur}[canvas];"


def _canvas(
    skin: str,
    *,
    size: int,
    dur: float,
    with_media: bool,
    round_frame: bool = False,
) -> _Canvas:
    """The skin's picture. Every skin paints the whole frame: the inset-box
    geometry of the first generation is what made the visualisers read as
    «слишком технически», so it did not survive the redesign.

    ``with_media`` says whether a second input exists — episode artwork for
    the cover-family skins, a background loop for the loop skins.
    ``round_frame`` is for the one skin whose *shape* depends on the crop:
    dvd bounces off the circle in a note and off the frame in a video.
    """
    if skin == SKIN_COVER:
        if not with_media:
            return _Canvas(_dark_card(size, dur), "white@0.85", "white", boxed=True)
        # Ken Burns instead of a frozen poster: the still is scaled with
        # headroom, then zoompan creeps in ~12% over the clip. ``pzoom``
        # (not ``zoom``) is what accumulates across a looped still's frames.
        big = size * 13 // 10
        graph = (
            f"[1:v]scale={big}:{big}:force_original_aspect_ratio=increase,"
            f"crop={big}:{big},"
            f"zoompan=z='min(pzoom+{0.12 / (25 * dur):.7f},1.5)'"
            ":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d=1:s={size}x{size}:fps=25,"
            "eq=brightness=-0.1[canvas];"
        )
        return _Canvas(graph, "white@0.9", "white", boxed=True, endless=True)

    if skin == SKIN_VINYL:
        if not with_media:
            return _Canvas(_dark_card(size, dur), "white@0.85", "white", boxed=True)
        # The artwork spins like the record it decorates. Scaled to the
        # square's diagonal so the rotating square always covers the frame,
        # and vignetted so the corners fall off into black — in the round
        # note the circle crop turns this into a literal spinning disc.
        diag = int(size * 1.42)
        graph = (
            f"[1:v]scale={diag}:{diag}:force_original_aspect_ratio=increase,"
            f"crop={diag}:{diag},"
            f"rotate=2*PI*t/6:ow={size}:oh={size}:c=black,"
            "vignette=PI/3.5,"
            f"drawbox=x={size // 2 - 7}:y={size // 2 - 7}:w=14:h=14"
            ":color=0x111111@0.9:t=fill[canvas];"
        )
        return _Canvas(graph, "white@0.9", "white", boxed=True, endless=True)

    if skin in LOOP_SKINS:
        if not with_media:
            return _Canvas(_dark_card(size, dur), "white@0.85", "white", boxed=True)
        # The operator's own background loop (see docs/video-skins.md),
        # cropped to the square and barely touched: the whole point of the
        # genre is that the footage is loud.
        graph = (
            f"[1:v]scale={size}:{size}:force_original_aspect_ratio=increase,"
            f"crop={size}:{size},eq=brightness=-0.05[canvas];"
        )
        return _Canvas(graph, "white@0.9", "white", boxed=True, endless=True)

    if skin == SKIN_AURORA:
        # Bars, but melted: a long tmix and a heavy blur turn the averaged
        # spectrum into a glowing ridge that breathes with the voice, and a
        # slow hue drift plays the northern lights. The ridge lives on the
        # lower half of a black sky — rendered full-height it filled the
        # frame with a solid wall of colour and stopped reading as lights.
        # The gamma lift matters — blurring spreads the energy thin, and
        # without it the glow reads as a dim smudge.
        ridge = int(size * 0.55)
        graph = (
            f"color=c=black:s={size}x{size}:d={dur}[sky];"
            f"[0:a]showfreqs=s={size}x{ridge}:mode=bar:ascale=log:fscale=log"
            ":averaging=8:colors=0x40e0d0|0xff69b4[ridge];"
            f"[sky][ridge]overlay=x=0:y={size - ridge}:shortest=1[comp];"
            "[comp]tmix=frames=7,gblur=sigma=14,"
            "eq=gamma=0.6:saturation=1.9,hue=H=2*PI*t/16[canvas];"
        )
        return _Canvas(graph, "0xb066ff@0.8", "white")

    if skin == SKIN_PARTY:
        # A crisp equalizer standing on the centre line with its reflection
        # hanging below, spun through the hue wheel. The falling peaks are
        # ``lagfun``: each pixel keeps its maximum and decays a few percent
        # per frame, so every syllable leaves a ghost that sinks back down —
        # the peak-hold of a hi-fi deck. (The earlier tmix+gblur cut was
        # dismissed as «мыльно»; this one stays sharp on purpose.)
        half = size // 2
        graph = (
            f"[0:a]showfreqs=s={size}x{half}:mode=bar:ascale=cbrt:fscale=log"
            ":averaging=4:colors=0xff4dd2|0x4dd2ff[fr];"
            "[fr]split=2[up][dn];"
            "[dn]vflip,eq=brightness=-0.18:saturation=0.8[refl];"
            "[up][refl]vstack[eq];"
            "[eq]lagfun=decay=0.93,"
            "eq=saturation=1.7,hue=H=2*PI*t/8[canvas];"
        )
        return _Canvas(graph, "0xff4dd2@0.9", "white")

    if skin == SKIN_LAVA:
        # An actual lava lamp: slow warm gradient blobs underneath, and the
        # aurora treatment of the spectrum screened on top so the glow still
        # swells with the voice.
        graph = (
            f"gradients=s={size}x{size}:c0=0x1a0005:c1=0x5c0a00:c2=0xd93800"
            f":c3=0xff9a00:n=4:speed=0.02:d={dur}[grad];"
            f"[0:a]showfreqs=s={size}x{size}:mode=bar:ascale=log:fscale=log"
            ":averaging=8:colors=0xff5a00|0xffc040,"
            "tmix=frames=9,gblur=sigma=16,eq=gamma=0.65[glow];"
            "[grad][glow]blend=all_mode=screen,eq=saturation=1.4[canvas];"
        )
        return _Canvas(graph, "0xffa030@0.9", "0xffb347")

    if skin == SKIN_MATRIX:
        # The spectrogram falls down the frame — ``orientation=horizontal``
        # turns the scroll vertical — and a column grid slices it into the
        # falling green streams of the meme. ``fscale=log`` keeps speech off
        # the frame's edge; ``drange=48`` keeps the noise floor black.
        # Rendered at half size and upscaled with nearest-neighbour: the
        # doubled cells read as chunky glyph streams instead of a fine-grain
        # spectrogram, and the frame fills twice as fast from a cold start.
        half = size // 2
        graph = (
            f"[0:a]showspectrum=s={half}x{half}:mode=combined"
            ":color=intensity:scale=sqrt:fscale=log:slide=scroll"
            ":orientation=horizontal:drange=48[sp];"
            # vflip: the scroll grows upward on its own, and rain that rises
            # is not rain — the streams must fall.
            f"[sp]scale={size}:{size}:flags=neighbor,vflip,"
            "colorchannelmixer=rr=0:bb=0,"
            f"drawgrid=w=12:h={size}:t=3:c=black@0.85,"
            "eq=contrast=1.3[canvas];"
        )
        # Boxed: once the streams fill the frame, green-on-green text needs
        # the black pad to stay legible.
        return _Canvas(graph, "0x33ff66@0.9", "0x33ff66", boxed=True)

    if skin == SKIN_FRACTAL:
        # An endless Mandelbrot dive that the voice lights up: a blurred
        # spectrum glow is soft-lit onto the fractal, so loud passages
        # flush it with colour and silence lets it cool back down. The
        # generator never ends on its own, so the bar overlay trims it.
        graph = (
            f"mandelbrot=s={size}x{size}:rate=25:end_scale=0.00001"
            ":end_pts=1200[mb];"
            "[mb]hue=H=t/9:s=1.3[dive];"
            f"[0:a]showfreqs=s={size}x{size}:mode=bar:ascale=log:fscale=log"
            ":averaging=8:colors=0xff40c0|0x40c0ff,"
            "tmix=frames=5,gblur=sigma=20,eq=gamma=0.7[pulse];"
            "[dive][pulse]blend=all_mode=softlight[canvas];"
        )
        return _Canvas(graph, "white@0.85", "white", boxed=True, endless=True)

    if skin == SKIN_DVD:
        # The bouncing-logo meme. The episode artwork (or a music note when
        # the feed has none) drifts and ricochets off the edges; everyone
        # waits for the corner hit. Speeds are deliberately not multiples of
        # each other, so the path takes ages to repeat.
        #
        # In the round note the frame's edges are invisible — Telegram crops
        # to the inscribed circle — so the logo bounces inside the circle's
        # inscribed *square* instead: a logo whose corner grazes that
        # square's corner sits exactly on the circle, which reads as
        # bouncing off the round border rather than off nothing.
        item = size * 3 // 10
        margin = round(size * (1 - 0.7071) / 2) if round_frame else 0
        span_w = size - 2 * margin - item
        graph = f"color=c=0x11101c:s={size}x{size}:d={dur}[bg];"
        if with_media:
            graph += (
                f"[1:v]scale={item}:{item}:force_original_aspect_ratio="
                f"increase,crop={item}:{item}[item];"
                "[bg][item]overlay"
                f"=x='{margin}+abs(mod(43*t,{2 * span_w})-{span_w})'"
                f":y='{margin}+abs(mod(31*t,{2 * span_w})-{span_w})'[canvas];"
            )
        else:
            pad = 2 * margin + 28
            graph += (
                "[bg]drawtext=" + _font(bold=True)
                + ":text='♪':fontcolor=white:fontsize=72"
                + ":box=1:boxcolor=0x7a2ea8@0.9:boxborderw=14"
                + f":x='{margin}+abs(mod(43*t,2*(w-{pad}-text_w))"
                + f"-(w-{pad}-text_w))'"
                + f":y='{margin}+abs(mod(31*t,2*(h-{pad}-text_h))"
                + f"-(h-{pad}-text_h))',"
                + "hue=H=2*PI*t/12[canvas];"
            )
        return _Canvas(graph, "0xc084ff@0.9", "white")

    raise ValueError(f"Unknown skin {skin!r}")


#: Title budgets, in characters per line, verified against rendered frames:
#: bold DejaVu at these sizes fills the round title chord at ~26 characters
#: and the square frame at ~40. The second round line sits on a wider chord.
TITLE_BUDGETS_ROUND = (24, 28)
TITLE_BUDGETS_SQUARE = (40, 40)


def wrap_title(title: str, budgets: tuple[int, int]) -> list[str]:
    """Fit a title into at most two centred lines.

    Greedy word wrap against per-line budgets — they differ in the round
    layout, where each line lives on its own chord of the circle. Whatever
    does not fit the second line is ellipsised away.
    """
    first, second = budgets
    words = title.split()
    line1 = ""
    index = 0
    while index < len(words):
        candidate = f"{line1} {words[index]}".strip()
        if len(candidate) > first:
            break
        line1 = candidate
        index += 1
    if not line1:
        # A single word longer than the whole line; cut it rather than wrap.
        return [truncate(title, first)]
    rest = " ".join(words[index:])
    if not rest:
        return [line1]
    return [line1, truncate(rest, second)]


def build_graph(
    skin: str,
    *,
    duration: float,
    title_file: Path,
    span_file: Path,
    subs_file: Path | None,
    with_media: bool,
    title2_file: Path | None = None,
    size: int = NOTE_SIZE,
    round_frame: bool = False,
) -> str:
    """The whole filter graph for one skin, ending in ``[out]``.

    Two layouts share the code. The square one uses the full frame: up to two
    title lines at the top edge; along the bottom a frame-wide progress bar
    flanked by a running clock on the left and the clip length on the right,
    with the episode time span centred between them. ``round_frame`` is for a
    video note, which Telegram crops to the circle inscribed in the square —
    at 384 px, a centred line at the very top has barely 200 px of visible
    chord — so everything textual moves inside the circle and the progress
    bar becomes a short centred track with the clock and length beside it.

    The progress fill is a strip slid across by ``overlay``'s ``t``
    expression *inside* a track-sized composition, which clips it: overlay's
    output takes the first input's size, so the strip cannot poke out of the
    track while it slides. (``crop`` evaluates ``w`` once at configuration,
    so a bar that grows by ``t`` is not available that way; drawbox can
    animate but overlay is the documented, boring path.)
    """
    skin = LEGACY_SKINS.get(skin, skin)
    if skin not in SKINS:
        raise ValueError(f"Unknown skin {skin!r}")

    dur = max(0.1, duration)
    if round_frame:
        title_y, title_size, line_gap = 38, 14, 18
        span_y, span_size = int(size * 0.8125), 12
        bar_w, bar_h = int(size * 0.39), 5
        bar_y = int(size * 0.875)
        clock_size = 11
    else:
        title_y, title_size, line_gap = 12, 15, 20
        span_y, span_size = size - 34, 13
        bar_w, bar_h = size, 6
        bar_y = size - 14
        clock_size = 13
    bar_x = (size - bar_w) // 2

    canvas = _canvas(
        skin, size=size, dur=dur, with_media=with_media,
        round_frame=round_frame,
    )
    total = f"{int(dur) // 60}\\:{int(dur) % 60:02d}"

    chain = canvas.graph
    base_label = "canvas"
    if canvas.boxed:
        # A dark pad under the progress bar, sized with the same margins the
        # drawtext boxes get, so the bar reads on top of bright artwork.
        chain += (
            f"[canvas]drawbox=x={bar_x - 6 if bar_x else 0}:y={bar_y - 5}"
            f":w={bar_w + 12 if bar_x else bar_w}:h={bar_h + 10}"
            ":color=black@0.45:t=fill[padded];"
        )
        base_label = "padded"
    chain += (
        f"color=c=white@0.18:s={bar_w}x{bar_h}:d={dur}[track];"
        + f"color=c={canvas.bar_color}:s={bar_w}x{bar_h}:d={dur}[fill];"
        + f"[track][fill]overlay=x='-{bar_w}+{bar_w}*t/{dur}':y=0"
        + ":shortest=1[bar];"
        + f"[{base_label}][bar]overlay=x={bar_x}:y={bar_y}"
        + (":shortest=1" if canvas.endless else "")
        + "[timed];"
        + "[timed]"
        + _drawtext(
            title_file, y=title_y, fontsize=title_size,
            color=canvas.title_color, bold=True, box=canvas.boxed,
        )
    )
    if title2_file is not None:
        chain += "," + _drawtext(
            title2_file, y=title_y + line_gap, fontsize=title_size,
            color=canvas.title_color, bold=True, box=canvas.boxed,
        )
    if round_frame:
        clock_x = f"{bar_x - 8}-text_w"
        total_x = str(bar_x + bar_w + 8)
        clock_y = bar_y + (bar_h - clock_size) // 2
    else:
        clock_x, total_x = "10", "w-text_w-10"
        clock_y = span_y
    chain += (
        ","
        + _drawtext(
            span_file, y=span_y, fontsize=span_size, color="0xcccccc",
            box=canvas.boxed,
        )
        + ","
        + _drawtext(
            text=_CLOCK_TEXT, x=clock_x, y=clock_y, fontsize=clock_size,
            color="0xdddddd", box=canvas.boxed,
        )
        + ","
        + _drawtext(
            text=total, x=total_x, y=clock_y, fontsize=clock_size,
            color="0xdddddd", box=canvas.boxed,
        )
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


def background_files(settings: Settings, subdir: str | None) -> list[Path]:
    """The operator's loops for one loop skin.

    ``random`` (``subdir=None``) sweeps the whole tree, so a file filed
    under ``subway/`` still counts; a themed skin sees only its own folder.
    """
    directory = settings.brainrot_dir
    if subdir is not None:
        directory = directory / subdir
    try:
        entries = sorted(directory.rglob("*"))
    except OSError:
        return []
    return [
        path for path in entries
        if path.suffix.lower() in BACKGROUND_SUFFIXES and path.is_file()
    ]


async def _pick_background(
    settings: Settings, need: float, subdir: str | None
) -> tuple[Path | None, list[str]]:
    """A random background loop and the input args that loop it.

    A random start offset keeps two renders of the same clip from serving
    the same thirty seconds of gameplay; the file loops if it runs out. The
    offset needs the file's duration, which the audio probe answers only for
    loops that ship sound — a silent file just starts from the top.
    """
    files = background_files(settings, subdir)
    if not files:
        logger.info(
            "No background loops for the loop skin under %s; "
            "rendering the plain card.",
            settings.brainrot_dir / (subdir or ""),
        )
        return None, []
    choice = random.choice(files)
    offset = 0.0
    info = await probe(choice, timeout=30.0)
    if info.duration and info.duration > need + 2:
        offset = random.uniform(0.0, info.duration - need - 1)
    return choice, ["-stream_loop", "-1", "-ss", f"{offset:.2f}"]


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
    round_frame: bool = False,
) -> Path:
    """Render the cut audio into a square video, verified before it is sent.

    The second input — artwork or a brainrot loop — is test-decoded first
    and dropped if unreadable; see :func:`_cover_usable` for why that cannot
    wait for the render to find out. A render that then still fails
    *quickly* with media is retried once without it, losing the picture
    rather than the clip; a render that times out is not retried, because
    its failure already cost minutes.
    """
    skin = LEGACY_SKINS.get(skin, skin)
    # Fitted here, not by the caller, because how many characters survive is
    # a property of the layout: drawtext neither wraps nor ellipsises, and a
    # centred line wider than the frame — or, in a note, wider than the
    # circle's chord at title height — is simply cropped at both ends.
    lines = wrap_title(
        title, TITLE_BUDGETS_ROUND if round_frame else TITLE_BUDGETS_SQUARE
    )
    title_file = workdir / "title.txt"
    title_file.write_text(lines[0], encoding="utf-8")
    title2_file: Path | None = None
    if len(lines) > 1:
        title2_file = workdir / "title2.txt"
        title2_file.write_text(lines[1], encoding="utf-8")
    span_file = workdir / "span.txt"
    span_file.write_text(span, encoding="utf-8")

    subs_file: Path | None = None
    if subtitles:
        subs_file = workdir / "subs.ass"
        subs_file.write_text(
            ass_document(
                subtitles,
                round_frame=round_frame,
                centered=skin in LOOP_SKINS,
            ),
            encoding="utf-8",
        )

    media = cover if skin in COVER_SKINS else None
    media_args = ["-loop", "1"]
    if skin in LOOP_SKINS:
        media, media_args = await _pick_background(
            settings, duration, LOOP_SKINS[skin]
        )

    if media is not None and not await _cover_usable(media):
        logger.info("Second input %s is not decodable; dropping it.", media)
        media = None

    output = workdir / "note.mp4"
    attempts = [media] if media is None else [media, None]
    reason = ""
    for attempt_media in attempts:
        reason = await _render_once(
            audio, output,
            skin=skin, duration=duration, title_file=title_file,
            title2_file=title2_file, span_file=span_file,
            subs_file=subs_file, media=attempt_media,
            media_args=media_args, round_frame=round_frame,
            timeout=settings.ffmpeg_timeout,
        )
        if reason is None:
            return output
        if attempt_media is not None:
            logger.info(
                "Render with media failed (%s); retrying without it.",
                reason[:300],
            )
    logger.error("Video render failed: %s", reason[:500])
    raise AudioError("err_render_failed")


async def _render_once(
    audio: Path,
    output: Path,
    *,
    skin: str,
    duration: float,
    title_file: Path,
    title2_file: Path | None,
    span_file: Path,
    subs_file: Path | None,
    media: Path | None,
    media_args: list[str],
    round_frame: bool,
    timeout: float,
) -> str | None:
    """One render attempt. ``None`` on success, else the reason it failed."""
    with contextlib.suppress(FileNotFoundError):
        output.unlink()

    graph = build_graph(
        skin,
        duration=duration,
        title_file=title_file,
        title2_file=title2_file,
        span_file=span_file,
        subs_file=subs_file,
        with_media=media is not None,
        round_frame=round_frame,
    )
    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        *_protocol_args(audio), "-i", str(audio),
    ]
    if media is not None:
        cmd += [*media_args, *_protocol_args(media), "-i", str(media)]
    cmd += [
        "-filter_complex", graph, "-map", "[out]", "-map", "0:a",
        "-map_metadata", "0",
    ]
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
