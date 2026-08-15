"""Small pure helpers for turning API strings into things Telegram accepts.

Kept free of I/O so they are cheap to unit-test.
"""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Callable

from .i18n import t, t_seq

#: Characters that are illegal or troublesome in filenames on common platforms.
_UNSAFE_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_WHITESPACE = re.compile(r"\s+")


def one_line(value: str | None, fallback: str = "") -> str:
    """Collapse whitespace and strip control characters.

    Titles from RSS feeds routinely contain newlines and tabs, which break
    inline-button labels and message formatting.
    """
    if not value:
        return fallback
    # Substitute rather than delete: newlines and tabs separate words, so
    # dropping them outright would glue "Episode 42\nPart two" into one word.
    cleaned = "".join(
        ch if ch == " " or unicodedata.category(ch)[0] != "C" else " " for ch in value
    )
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    return cleaned or fallback


def truncate(value: str, limit: int) -> str:
    """Shorten to ``limit`` characters, marking the cut with an ellipsis."""
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    if limit == 1:
        return "…"
    return value[: limit - 1].rstrip() + "…"


def button_label(*parts: str, limit: int = 60, fallback: str = "Untitled") -> str:
    """Build a single-line inline-button label from title fragments."""
    joined = " · ".join(one_line(p) for p in parts if one_line(p))
    return truncate(joined, limit) or fallback


def safe_filename(*parts: str, ext: str, limit: int = 100) -> str:
    """Build a filename Telegram and every filesystem will accept.

    Titles arrive from arbitrary RSS feeds, so they may contain slashes, null
    bytes, newlines or be thousands of characters long.
    """
    cleaned_parts = []
    for part in parts:
        part = one_line(part)
        part = _UNSAFE_FILENAME_CHARS.sub("", part)
        part = part.replace(" ", "_").strip("._")
        if part:
            cleaned_parts.append(part)

    stem = truncate("-".join(cleaned_parts), limit).rstrip("…").strip("._-")
    if not stem:
        stem = "podcast_cut"
    return f"{stem}.{ext.lstrip('.')}"


def esc(value: str | None, fallback: str = "") -> str:
    """Single-line and HTML-escaped, ready to drop into a formatted message.

    Feed titles routinely contain ``&`` and angle brackets; unescaped they make
    Telegram reject the whole message with a parse error.
    """
    return html.escape(one_line(value, fallback), quote=False)


def human_bytes(count: float, lang: str = "en") -> str:
    """Byte count as a short human-readable string."""
    units = t_seq(lang, "byte_units")
    if count < 1024:
        return f"{int(count)} {units[0]}"
    for unit in units[1:]:
        count /= 1024
        if count < 1024 or unit == units[-1]:
            return f"{count:.1f} {unit}".replace(".0 ", " ")
    return f"{count:.1f} {units[-1]}"


def progress_bar(
    done: int,
    total: int | None,
    width: int = 10,
    label: Callable[[int], str] | None = None,
    lang: str = "en",
) -> str:
    """A textual progress bar, degrading gracefully when the size is unknown.

    ``label`` renders the quantities, because the bar outlived its first
    caller: downloads count bytes, transcription counts seconds of audio, and
    a bar that renders forty minutes as «2.4 KB» reads as a bug, not as
    progress.
    """
    if label is None:
        label = lambda count: human_bytes(count, lang)  # noqa: E731

    if not total or total <= 0:
        return t(lang, "so_far", amount=label(done))

    fraction = min(1.0, max(0.0, done / total))
    filled = round(fraction * width)
    bar = "▰" * filled + "▱" * (width - filled)
    return (
        f"{bar}  {fraction * 100:.0f}%"
        f"  ·  {label(done)} / {label(total)}"
    )


def format_duration(seconds: int) -> str:
    """Render a second count as ``H:MM:SS`` or ``M:SS``."""
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
