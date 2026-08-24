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

#: The square's side. 512 keeps the small clock and subtitles crisp through
#: Telegram's second encode while staying below sendVideoNote's 640 px limit.
#: The original 384 px canvas made thin glyphs visibly fray after upload.
NOTE_SIZE = 512

SKIN_COVER = "cover"
SKIN_VINYL = "vinyl"
SKIN_AURORA = "aurora"
SKIN_PARTY = "party"
SKIN_LAVA = "lava"
SKIN_MATRIX = "matrix"
SKIN_FRACTAL = "fractal"
SKIN_DVD = "dvd"
#: Each loop look names one curated operator-owned file. The file is fixed;
#: the start offset is random for every render, so two users do not get the
#: same stretch of gameplay behind their clips.
SKIN_ROBLOX = "roblox"
SKIN_GTA = "gta"
SKIN_ASMR = "asmr"
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
    SKIN_ROBLOX,
    SKIN_GTA,
    SKIN_ASMR,
    SKIN_SUBWAY,
)

#: The loop-backed skins and their exact file relative to
#: ``settings.brainrot_dir``. Exact names make a button's promise stable: a
#: Roblox tap can never randomly serve GTA just because both share a folder.
LOOP_SKINS: dict[str, str] = {
    SKIN_ROBLOX: "01-roblox-parkour.mp4",
    SKIN_GTA: "02-gta-5-mega-ramp.mp4",
    SKIN_ASMR: "03-asmr-cutting.mp4",
    SKIN_SUBWAY: "04-subway-surfers.mp4",
}

#: Retired skins, mapped to their closest living relative. Buttons on
#: scrolled-past messages outlive keyboards, and a session that chose a look
#: before a redeploy should get *a* video, not a ValueError.
LEGACY_SKINS = {
    "bars": SKIN_PARTY,
    "spectrum": SKIN_AURORA,
    "scope": SKIN_MATRIX,
    "vhs": SKIN_VINYL,
    "brainrot": SKIN_ROBLOX,
    "random": SKIN_ROBLOX,
}

#: Skins whose second input is the episode artwork; the caller only fetches
#: a cover when the skin will actually put it on screen.
COVER_SKINS = frozenset({SKIN_COVER, SKIN_VINYL, SKIN_DVD})

#: These two looks have no identity without artwork. DVD has a deliberate
#: bouncing-note fallback, so it remains useful when a feed has no image.
ARTWORK_REQUIRED_SKINS = frozenset({SKIN_COVER, SKIN_VINYL})
DEFAULT_NO_ARTWORK_SKIN = SKIN_AURORA

#: Container suffixes accepted as brainrot background loops.
BACKGROUND_SUFFIXES = frozenset({".mp4", ".mov", ".mkv", ".webm", ".m4v"})

#: Refuse cover images beyond this. Artwork is decoration; a feed offering a
#: 100 MB "image" is not a feed to indulge.
MAX_COVER_BYTES = 10 * 1024 * 1024

_DEJAVU = Path("/usr/share/fonts/truetype/dejavu")
_ENCODE_ARGS = [
    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
    # The VBV cap is for the noisy skins: an uncapped Mandelbrot dive
    # measured ~2 Mbit/s, which over a five-minute square video overshoots
    # the upload ceiling. 1 Mbit/s tops out near 40 MB with audio and looks
    # the same at this display size.
    "-maxrate", "1M", "-bufsize", "2M",
    "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-b:a", "96k",
    "-movflags", "+faststart",
    "-shortest",
]


def available_skins(
    has_artwork: bool, *, settings: Settings | None = None
) -> tuple[str, ...]:
    """Return only looks that can produce their promised picture.

    A missing cover used to leave both Cover and Vinyl as identical dark
    cards in the menu and demo. They are choices only when
    the episode actually advertises artwork. Loop-backed looks likewise exist
    only when the operator has supplied footage; every offered skin must be
    able to produce the picture its label promises.
    """
    available = set(SKINS)
    if not has_artwork:
        available -= ARTWORK_REQUIRED_SKINS
    if settings is not None:
        available -= {
            skin
            for skin, filename in LOOP_SKINS.items()
            if loop_file(settings, filename) is None
        }
    return tuple(skin for skin in SKINS if skin in available)


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


_MATRIX_GLYPHS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ#$%&*+-<=>?"


def _matrix_font(bold: bool) -> str:
    """The image's known monospaced face: aligned columns sell the rain."""
    suffix = "-Bold" if bold else ""
    path = _DEJAVU / f"DejaVuSansMono{suffix}.ttf"
    if path.exists():
        return f"fontfile='{path}'"
    return "font='monospace'"


def _write_matrix_columns(workdir: Path, size: int) -> tuple[Path, ...]:
    """Write deterministic vertical glyph streams for the Matrix skin.

    Files keep non-ASCII text and newlines out of the filter expression. A
    local RNG makes identical clips visually stable without touching the
    module RNG used to pick background footage and offsets.
    """
    rng = random.Random(0x4D4154524958 + size)
    count = max(14, size // 27)
    paths = []
    for index in range(count):
        length = rng.randint(10, 22)
        glyphs = [rng.choice(_MATRIX_GLYPHS) for _ in range(length)]
        path = workdir / f"matrix-{index:02d}.txt"
        path.write_text("\n".join(glyphs), encoding="utf-8")
        path.with_suffix(".head.txt").write_text(glyphs[-1], encoding="utf-8")
        paths.append(path)
    return tuple(paths)


def _matrix_canvas(size: int, dur: float, columns: tuple[Path, ...]) -> str:
    """Build independently falling glyph columns with crisp heads and trails."""
    graph = f"color=c=0x010703:s={size}x{size}:d={dur}[rain0];"
    count = len(columns)
    font_size = max(16, size // 28)
    line_height = font_size + max(2, size // 170)
    usable = size - font_size
    for index, path in enumerate(columns):
        x = round(index * usable / max(1, count - 1))
        # Co-prime-ish speeds and staggered starts stop the rain moving as a
        # single wallpaper. text_h is evaluated by drawtext for each stream.
        speed = round(size * (0.20 + (index * 7 % 11) * 0.018))
        offset = round(size * ((index * 13) % count) / count)
        alpha = 0.48 + (index % 4) * 0.08
        source = f"rain{2 * index}"
        tailed, target = f"rain{2 * index + 1}", f"rain{2 * index + 2}"
        y = f"mod(t*{speed}+{offset}\\,h+text_h)-text_h"
        graph += (
            f"[{source}]drawtext={_matrix_font(bold=False)}:textfile='{path}'"
            f":fontcolor=0x17e84f@{alpha:.2f}:fontsize={font_size}"
            f":line_spacing={line_height - font_size}:x={x}:y='{y}'"
            ":shadowcolor=0x00ff55@0.35:shadowx=0:shadowy=0"
            f"[{tailed}];"
        )
        glyph_count = len(path.read_text(encoding="utf-8").splitlines())
        block_height = glyph_count * line_height - (line_height - font_size)
        head_y = (
            f"mod(t*{speed}+{offset}\\,h+{block_height})"
            f"-{line_height}"
        )
        graph += (
            f"[{tailed}]drawtext={_matrix_font(bold=True)}"
            f":textfile='{path.with_suffix('.head.txt')}'"
            f":fontcolor=0xd8ffe2:fontsize={font_size}:x={x}:y='{head_y}'"
            ":shadowcolor=0x5cff86@0.85:shadowx=0:shadowy=0"
            f"[{target}];"
        )

    last = f"rain{2 * count}"
    # The unblurred branch preserves recognisable symbols. The short temporal
    # mix leaves a downward phosphor trail; only its duplicate is blurred and
    # screened back on for glow, so the text itself never turns mushy.
    graph += (
        f"[{last}]tmix=frames=4:weights='1 0.55 0.28 0.12',"
        "eq=contrast=1.18:saturation=1.2[sharp];"
        "[sharp]split=2[base][glowin];"
        "[glowin]gblur=sigma=3[glow];"
        "[base][glow]blend=all_mode=screen:all_opacity=0.32[canvas];"
    )
    return graph


def _dvd_position(size: int, item: int, *, round_frame: bool) -> tuple[str, str]:
    """Return frame-evaluated top-left coordinates for the DVD logo.

    A square video is the familiar independent-axis ping-pong and has no
    cosmetic inset: zero and ``size - item`` are real collisions. A video
    note is a circular billiard. Successive points lie on the circle and the
    logo travels along the chord between them; the radius at each point is
    adjusted for the square logo's support, so its farthest corner — not an
    imaginary circumscribed disc — touches the crop exactly.
    """
    span = size - item
    if not round_frame:
        return (
            f"abs(mod(97*t,{2 * span})-{span})",
            f"abs(mod(73*t,{2 * span})-{span})",
        )

    # A golden-angle step avoids a short repeating polygon. Every 2.3 s is a
    # visible ricochet; between impacts the interpolation is a straight chord.
    step = "2.3999632297"
    phase = "0.7853981634"
    beat = "2.3"
    half = item / 2
    radius = size / 2
    centre = (size - item) / 2

    def point(axis: str, offset: int) -> str:
        angle = f"({phase}+(floor(t/{beat})+{offset})*{step})"
        cosine = f"cos({angle})"
        sine = f"sin({angle})"
        # For centre distance d and the corner in the travel direction:
        # |d*u + corner|^2 = R^2. Solving the quadratic gives this d.
        support = f"({half:.1f}*(abs({cosine})+abs({sine})))"
        distance = (
            f"(-{support}+sqrt({support}*{support}"
            f"+{radius:.1f}*{radius:.1f}-2*{half:.1f}*{half:.1f}))"
        )
        component = cosine if axis == "x" else sine
        return f"({distance}*{component})"

    fraction = f"mod(t/{beat},1)"
    x0, x1 = point("x", 0), point("x", 1)
    y0, y1 = point("y", 0), point("y", 1)
    return (
        f"{centre:.1f}+(1-{fraction})*{x0}+{fraction}*{x1}",
        f"{centre:.1f}+(1-{fraction})*{y0}+{fraction}*{y1}",
    )


def _canvas(
    skin: str,
    *,
    size: int,
    dur: float,
    with_media: bool,
    round_frame: bool = False,
    matrix_columns: tuple[Path, ...] = (),
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
        # Wax, not a rotating gradient. Five soft metaball-like fields rise at
        # different speeds, wander sideways and merge as their Gaussian edges
        # overlap. A very faint audio glow changes their heat without changing
        # the motion into yet another equaliser.
        centres = (
            (
                "W*(0.20+0.08*sin(N/53))",
                "mod(H*1.35-N*1.15\\,H*1.65)-H*0.25",
                "0.016",
            ),
            (
                "W*(0.42+0.11*sin(N/71+1.4))",
                "mod(H*1.60-N*0.82\\,H*1.75)-H*0.30",
                "0.024",
            ),
            (
                "W*(0.68+0.09*sin(N/61+2.5))",
                "mod(H*1.25-N*1.42\\,H*1.70)-H*0.22",
                "0.018",
            ),
            (
                "W*(0.82+0.06*sin(N/89+0.7))",
                "mod(H*1.75-N*0.67\\,H*1.85)-H*0.35",
                "0.030",
            ),
            (
                "W*(0.51+0.14*sin(N/97+3.1))",
                "mod(H*1.10-N*1.73\\,H*1.60)-H*0.20",
                "0.012",
            ),
        )
        fields = []
        for x, y, radius in centres:
            dx = f"(X-{x})"
            dy = f"(Y-({y}))"
            fields.append(
                f"exp(-(({dx}*{dx}+{dy}*{dy})/(W*W*{radius})))"
            )
        wax = "+".join(fields)
        graph = (
            f"nullsrc=s={size}x{size}:r=25:d={dur},"
            f"geq=lum='255*min(1\\,{wax})':cb=128:cr=128,format=gray,"
            "gblur=sigma=8,lutrgb=r='18+val*1.10':g='2+val*0.30'"
            ":b='12+val*0.08'[wax];"
            f"[0:a]showfreqs=s={size}x{size}:mode=bar:ascale=log:fscale=log"
            ":averaging=8:colors=0xff5a00|0xffc040,"
            "tmix=frames=9,gblur=sigma=24,eq=gamma=0.55[glow];"
            "[wax][glow]blend=all_mode=screen:all_opacity=0.22,"
            "vignette=PI/5,eq=saturation=1.35:contrast=1.08[canvas];"
        )
        return _Canvas(graph, "0xffa030@0.9", "0xffb347")

    if skin == SKIN_MATRIX:
        if not matrix_columns:
            raise ValueError("Matrix skin needs its glyph columns")
        graph = _matrix_canvas(size, dur, matrix_columns)
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
        # each other, and both axes now collide during a five-second demo.
        #
        # Square videos use the literal frame edges. In a round note, moving
        # inside an inscribed square leaves an obvious gap at almost every
        # collision. Instead the logo follows straight chords whose endpoints
        # put its outermost corner exactly on Telegram's circular crop.
        item = size * 3 // 10
        dvd_x, dvd_y = _dvd_position(size, item, round_frame=round_frame)
        graph = f"color=c=0x11101c:s={size}x{size}:d={dur}[bg];"
        if with_media:
            graph += (
                f"[1:v]scale={item}:{item}:force_original_aspect_ratio="
                f"increase,crop={item}:{item}[item];"
                "[bg][item]overlay"
                f"=x='{dvd_x}':y='{dvd_y}'"
                ":eval=frame:shortest=1[canvas];"
            )
        else:
            # Give the no-artwork fallback a known-size logo as well. The old
            # direct drawtext path had runtime-dependent text/box dimensions,
            # so its alleged collision coordinate was not the box's edge.
            note_size = item * 47 // 100
            graph += (
                f"color=c=0x7a2ea8:s={item}x{item}:d={dur},"
                "drawtext=" + _font(bold=True)
                + f":text='♪':fontcolor=white:fontsize={note_size}"
                + ":x=(w-text_w)/2:y=(h-text_h)/2,"
                + "hue=H=2*PI*t/12[item];"
                + f"[bg][item]overlay=x='{dvd_x}':y='{dvd_y}'"
                + ":eval=frame:shortest=1[canvas];"
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
    matrix_columns: tuple[Path, ...] = (),
    watermark: bool = False,
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
        title_y, title_size, line_gap = int(size * 0.099), size // 27, size // 21
        span_y, span_size = int(size * 0.8125), size // 32
        bar_w, bar_h = int(size * 0.39), max(5, size // 77)
        bar_y = int(size * 0.875)
        clock_size = size // 35
    else:
        title_y, title_size, line_gap = size // 32, size // 26, size // 19
        span_y, span_size = int(size * 0.911), size // 30
        bar_w, bar_h = size, max(6, size // 64)
        bar_y = int(size * 0.964)
        clock_size = size // 30
    bar_x = (size - bar_w) // 2

    canvas = _canvas(
        skin, size=size, dur=dur, with_media=with_media,
        round_frame=round_frame, matrix_columns=matrix_columns,
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
    if watermark:
        chain += "," + _drawtext(
            text="@podcast_cutter_bot",
            y=int(size * (0.205 if round_frame else 0.145)),
            fontsize=max(12, size // 38),
            color="white@0.82",
            bold=True,
            box=True,
        )
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
        if not written:
            return None
        if not await _cover_usable(destination):
            logger.info("Downloaded cover from %s is not a decodable image", url)
            return None
        return destination
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


def loop_file(settings: Settings, filename: str) -> Path | None:
    """Return one curated loop only when its exact expected file exists."""
    path = settings.brainrot_dir / filename
    try:
        usable = path.suffix.lower() in BACKGROUND_SUFFIXES and path.is_file()
    except OSError:
        return None
    return path if usable else None


async def _pick_background(
    settings: Settings, need: float, filename: str
) -> tuple[Path | None, list[str]]:
    """One curated background at a random offset, plus args that loop it.

    A random start offset keeps two renders of the same skin from serving the
    same stretch; the file loops if it runs out. Duration comes from the MP4
    container even though the curated files deliberately have no audio.
    """
    choice = loop_file(settings, filename)
    if choice is None:
        logger.info(
            "No background file for the loop skin at %s; falling back.",
            settings.brainrot_dir / filename,
        )
        return None, []
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
    watermark: bool = False,
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

    matrix_columns = (
        _write_matrix_columns(workdir, NOTE_SIZE)
        if skin == SKIN_MATRIX
        else ()
    )

    media = cover if skin in COVER_SKINS else None
    media_args = ["-loop", "1"]
    if skin in LOOP_SKINS:
        media, media_args = await _pick_background(
            settings, duration, LOOP_SKINS[skin]
        )
        if media is None:
            # A loop may disappear after its keyboard was rendered (or an old
            # button may be pressed). Never turn that race into the old empty
            # title card: Aurora is a complete audio-driven fallback.
            skin = DEFAULT_NO_ARTWORK_SKIN

    if media is not None and not await _cover_usable(media):
        logger.info("Second input %s is not decodable; dropping it.", media)
        media = None
        if skin in LOOP_SKINS:
            skin = DEFAULT_NO_ARTWORK_SKIN

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
            matrix_columns=matrix_columns,
            watermark=watermark,
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
    matrix_columns: tuple[Path, ...],
    watermark: bool,
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
        matrix_columns=matrix_columns,
        watermark=watermark,
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
