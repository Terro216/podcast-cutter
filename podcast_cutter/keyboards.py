"""Keyboards and the callback-data vocabulary.

Two conventions hold everywhere:

* Callback payloads are namespaced (``ep:123``, ``nav:back``, ``mv:-15``), so a
  podcast id can never be mistaken for a control word and an unknown payload is
  recognisable as such rather than silently doing the wrong thing.
* Every screen offers a way out. Lists carry ``‹ Back``, and destructive or
  terminal actions are coloured with Bot API 9.4's button styles so the primary
  action is obvious at a glance.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from typing import TypeVar

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

from .states import FORMAT_AUDIO, FORMAT_NOTE, FORMAT_VIDEO, FORMAT_VOICE
from .text import button_label, format_duration

T = TypeVar("T")

# Button colours. Older Telegram clients (before February 2026) ignore these
# and fall back to the default styling, so they are decoration, never meaning.
STYLE_PRIMARY = "primary"
STYLE_SUCCESS = "success"
STYLE_DANGER = "danger"

# -- main menu -------------------------------------------------------------

BTN_SEARCH_PODCAST = "🔍 Search a podcast"
BTN_SEARCH_PERSON = "🧑 Search by person"
BTN_TRENDING = "🔥 Trending"
BTN_SURPRISE = "🎲 Surprise me"
BTN_RECENT = "🕘 Recent"
BTN_HELP = "❓ Help"

MENU_BUTTONS = (
    BTN_SEARCH_PODCAST,
    BTN_SEARCH_PERSON,
    BTN_TRENDING,
    BTN_SURPRISE,
    BTN_RECENT,
    BTN_HELP,
)


def menu_regex(*labels: str) -> str:
    """Exact-match regex for reply-keyboard buttons, emoji included."""
    alternatives = "|".join(re.escape(label) for label in labels)
    return f"^(?:{alternatives})$"


def main_menu() -> ReplyKeyboardMarkup:
    """The persistent shortcut bar, always one tap away."""
    return ReplyKeyboardMarkup(
        [
            [BTN_SEARCH_PODCAST, BTN_SEARCH_PERSON],
            [BTN_TRENDING, BTN_SURPRISE],
            [BTN_RECENT, BTN_HELP],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


# -- callback vocabulary ---------------------------------------------------

FEED_PREFIX = "feed"
EPISODE_PREFIX = "ep"
PAGE_PREFIX = "page"
NAV_PREFIX = "nav"
#: Interval editing: clip length, window move, start/end nudge.
LENGTH_PREFIX = "len"
MOVE_PREFIX = "mv"
#: Post-cut actions.
SHIFT_PREFIX = "shift"

NAV_BACK = f"{NAV_PREFIX}:back"
NAV_MENU = f"{NAV_PREFIX}:menu"
NAV_CANCEL = f"{NAV_PREFIX}:cancel"
#: Attached to labels that exist only to display something.
NAV_NOOP = f"{NAV_PREFIX}:noop"

ACTION_CUT = "act:cut"
ACTION_RETRY = "act:retry"
#: The pre-video-note format toggle. No keyboard offers it any more, but
#: buttons on scrolled-past messages outlive keyboards, so the router still
#: answers it.
ACTION_TOGGLE_VOICE = "act:voice"
ACTION_NEW_CLIP = "act:new"
ACTION_FIND = "act:find"

#: Delivery format and video-note skin choices on the clip editor.
FORMAT_PREFIX = "fmt"
SKIN_PREFIX = "skin"

_FORMAT_LABELS = (
    (FORMAT_AUDIO, "🎵 Audio"),
    (FORMAT_VOICE, "🎤 Voice"),
    (FORMAT_NOTE, "⭕ Circle"),
    (FORMAT_VIDEO, "🎬 Video"),
)

#: Skin key → button label. The keys must equal ``video.SKINS`` — a test
#: holds them together — but the labels live here because this module is the
#: UI vocabulary and must not import the ffmpeg half of the world.
SKIN_LABELS = {
    "bars": "🎚 Bars",
    "spectrum": "🌈 Spectrum",
    "scope": "🟢 Scope",
    "cover": "🖼 Cover",
    "party": "🪩 Party",
    "vhs": "📼 VHS",
    "matrix": "💊 Matrix",
}

#: The skin rows split so seven choices do not become seven slivers.
_SKIN_ROWS = (("bars", "spectrum", "scope", "cover"), ("party", "vhs", "matrix"))

#: A found moment, carried as its start in seconds rather than an index into a
#: list: the list lives in a session, and a button on a scrolled-past message
#: outlives it.
MOMENT_PREFIX = "at"


def parse_callback(data: str | None) -> tuple[str, str]:
    """Split ``"ep:1234"`` into ``("ep", "1234")``.

    Unrecognised or empty payloads come back as ``("", "")`` so the router can
    treat them as a stale button rather than raising.
    """
    if not data or ":" not in data:
        return "", ""
    prefix, _, value = data.partition(":")
    return prefix, value


def _button(
    text: str, data: str, style: str | None = None
) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data, style=style)


# -- reusable rows ---------------------------------------------------------


def pagination_row(page: int, pages: int) -> list[InlineKeyboardButton]:
    """``‹  2/7  ›`` — the counter tells users how much is left.

    The counter itself is a button because Telegram has no inert label; it is
    wired to a no-op so tapping it does nothing visible.
    """
    if pages <= 1:
        return []

    row = []
    if page > 1:
        row.append(_button("‹", f"{PAGE_PREFIX}:{page - 1}"))
    row.append(_button(f"{page}/{pages}", NAV_NOOP))
    if page < pages:
        row.append(_button("›", f"{PAGE_PREFIX}:{page + 1}"))
    return row


def footer_row(
    *, back: bool = True, menu: bool = True
) -> list[InlineKeyboardButton]:
    row = []
    if back:
        row.append(_button("‹ Back", NAV_BACK))
    if menu:
        row.append(_button("☰ Menu", NAV_MENU))
    return row


# -- screens ---------------------------------------------------------------


ACTION_CLEAR_FILTER = "act:unfilter"


def clear_filter_button() -> InlineKeyboardButton:
    return _button("✕ Clear filter", ACTION_CLEAR_FILTER)


def choice_keyboard(
    items: Sequence[T],
    prefix: str,
    id_of: Callable[[T], str],
    label_of: Callable[[T], str],
    *,
    page: int = 1,
    pages: int = 1,
    back: bool = True,
    extra_rows: Sequence[Sequence[InlineKeyboardButton]] | None = None,
) -> InlineKeyboardMarkup:
    """A page of choices with pagination and a way back."""
    rows = list(_choice_rows(items, prefix, id_of, label_of))

    nav = pagination_row(page, pages)
    if nav:
        rows.append(nav)

    for row in extra_rows or ():
        rows.append(list(row))

    footer = footer_row(back=back)
    if footer:
        rows.append(footer)

    return InlineKeyboardMarkup(rows)


def _choice_rows(
    items: Iterable[T],
    prefix: str,
    id_of: Callable[[T], str],
    label_of: Callable[[T], str],
) -> Iterable[list[InlineKeyboardButton]]:
    for item in items:
        payload = f"{prefix}:{id_of(item)}"
        # Telegram caps callback_data at 64 bytes. Truncating would select the
        # wrong item, so skip the entry instead.
        if len(payload.encode()) > 64:
            continue
        yield [_button(button_label(label_of(item)), payload)]


#: Clip lengths offered as one-tap presets, in seconds.
LENGTH_PRESETS = (30, 60, 180, 300)

#: How far the ◀ ▶ buttons move the clip, in seconds.
MOVE_STEPS = (-60, -15, 15, 60)


def _move_label(delta: int) -> str:
    if delta <= -60:
        return f"◀◀ {delta // 60}m"
    if delta < 0:
        return f"◀ {delta}s"
    if delta >= 60:
        return f"+{delta // 60}m ▶▶"
    return f"+{delta}s ▶"


def interval_keyboard(
    length: int,
    *,
    max_length: int,
    send_as: str = FORMAT_AUDIO,
    skin: str = "bars",
    can_search: bool = False,
) -> InlineKeyboardMarkup:
    """The clip editor: pick a length, slide the window, then cut."""
    presets = [
        _button(
            f"● {format_duration(seconds)}"
            if seconds == length
            else format_duration(seconds),
            f"{LENGTH_PREFIX}:{seconds}",
            STYLE_SUCCESS if seconds == length else None,
        )
        for seconds in LENGTH_PRESETS
        if seconds <= max_length
    ]

    rows = []
    if presets:
        rows.append(presets)
    rows.append(
        [_button(_move_label(step), f"{MOVE_PREFIX}:{step}") for step in MOVE_STEPS]
    )
    rows.append(
        [
            _button(
                f"● {label}" if key == send_as else label,
                f"{FORMAT_PREFIX}:{key}",
                STYLE_SUCCESS if key == send_as else None,
            )
            for key, label in _FORMAT_LABELS
        ]
    )
    if send_as in (FORMAT_NOTE, FORMAT_VIDEO):
        # The skins only matter once a video format is chosen; a permanent
        # block of decoration would bury the cut button under choices that
        # mean nothing for audio.
        for group in _SKIN_ROWS:
            rows.append(
                [
                    _button(
                        f"● {SKIN_LABELS[key]}" if key == skin
                        else SKIN_LABELS[key],
                        f"{SKIN_PREFIX}:{key}",
                        STYLE_SUCCESS if key == skin else None,
                    )
                    for key in group
                ]
            )
    rows.append([_button("✂️ Cut it", ACTION_CUT, STYLE_PRIMARY)])
    if can_search:
        rows.append([_button("🔎 Find a moment by what was said", ACTION_FIND)])
    rows.append(footer_row())
    return InlineKeyboardMarkup(rows)


def moments_keyboard(moments: Sequence, phrase: str) -> InlineKeyboardMarkup:
    """The answers to a search, each opening the clip editor where it starts.

    Numbered and stamped, nothing more: a button is a single short line, and a
    sentence squeezed into one is cut mid-word, so the quotations live in the
    message above and these only have to say *which* one. Side by side, so
    three answers cost one row instead of three.
    """
    picks = [
        _button(
            f"{index} · {format_duration(int(moment.clip_start))}",
            f"{MOMENT_PREFIX}:{int(moment.clip_start)}",
        )
        for index, moment in enumerate(moments, start=1)
    ]

    rows = [picks] if picks else []
    rows.append([_button("🔎 Search again", ACTION_FIND)])
    rows.append(footer_row())
    return InlineKeyboardMarkup(rows)


def result_keyboard(share_query: str) -> InlineKeyboardMarkup:
    """Offered after a successful cut: adjust, repeat, or share."""
    return InlineKeyboardMarkup(
        [
            [
                _button("↺ 15s earlier", f"{SHIFT_PREFIX}:-15"),
                _button("15s later ↻", f"{SHIFT_PREFIX}:15"),
            ],
            [_button("✂️ Another clip from this episode", ACTION_NEW_CLIP)],
            [
                InlineKeyboardButton(
                    "📤 Share this episode",
                    switch_inline_query=share_query,
                )
            ],
            [_button("☰ Menu", NAV_MENU)],
        ]
    )


def open_episode(link: str) -> InlineKeyboardMarkup:
    """One button, on a message the bot sent unprompted.

    A deep link rather than callback data on purpose: the message it sits under
    may outlive the session, the process, or both, and a `?start=ep_…` link is
    the one route back into an episode that needs neither.
    """
    return InlineKeyboardMarkup([[InlineKeyboardButton("🎧 Open it", url=link)]])


def error_keyboard() -> InlineKeyboardMarkup:
    """Shown when a cut fails: retrying is usually worth one tap."""
    return InlineKeyboardMarkup(
        [
            [_button("↻ Try again", ACTION_RETRY, STYLE_PRIMARY)],
            [_button("‹ Back", NAV_BACK), _button("☰ Menu", NAV_MENU)],
        ]
    )


def menu_keyboard(has_recent: bool) -> InlineKeyboardMarkup:
    """The inline twin of the reply keyboard, for tap-driven navigation."""
    rows = [
        [
            _button("🔍 Podcast", "menu:search", STYLE_PRIMARY),
            _button("🧑 Person", "menu:person"),
        ],
        [
            _button("🔥 Trending", "menu:trending"),
            _button("🎲 Surprise", "menu:surprise"),
        ],
    ]
    if has_recent:
        rows.append([_button("🕘 Recent episodes", "menu:recent")])
    rows.append([_button("❓ How this works", "menu:help")])
    return InlineKeyboardMarkup(rows)


def cancel_keyboard() -> InlineKeyboardMarkup:
    """Attached to prompts that wait for typed input."""
    return InlineKeyboardMarkup([[_button("✕ Cancel", NAV_CANCEL, STYLE_DANGER)]])
