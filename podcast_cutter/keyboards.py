"""Keyboards and the callback-data vocabulary.

Two conventions hold everywhere:

* Callback payloads are namespaced (``ep:123``, ``nav:back``, ``mv:-15``), so a
  podcast id can never be mistaken for a control word and an unknown payload is
  recognisable as such rather than silently doing the wrong thing.
* Every screen offers a way out. Lists carry ``‹ Back``, and destructive or
  terminal actions are coloured with Bot API 9.4's button styles so the primary
  action is obvious at a glance.

Labels come from :mod:`~podcast_cutter.i18n`, so every builder takes the
language it renders in. Callback data never does: a payload is a name, not a
sentence, and a button pressed after its owner switched languages must still
route the same.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import TypeVar

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

from .i18n import LANGUAGE_NAMES, LANGUAGES, t
from .states import FORMAT_AUDIO, FORMAT_NOTE, FORMAT_VIDEO, FORMAT_VOICE
from .text import button_label, format_duration

T = TypeVar("T")

# Button colours. Older Telegram clients (before February 2026) ignore these
# and fall back to the default styling, so they are decoration, never meaning.
STYLE_PRIMARY = "primary"
STYLE_SUCCESS = "success"
STYLE_DANGER = "danger"

# -- main menu -------------------------------------------------------------

#: i18n label key → what the button does. The reply keyboard sends its label
#: back as plain text, so the router recognises a press by looking the text up
#: across *every* language: the labels on a client's screen change only when a
#: new keyboard is sent, and a Russian button must keep working after the user
#: switches the bot to English.
_MENU_LABEL_ACTIONS = (
    ("btn_search_podcast", "search"),
    ("btn_search_person", "person"),
    ("btn_trending", "trending"),
    ("btn_surprise", "surprise"),
    ("btn_recent", "recent"),
    ("btn_help", "help"),
    ("btn_language", "language"),
)

_MENU_ACTIONS: dict[str, str] = {
    t(lang, key): action
    for lang in LANGUAGES
    for key, action in _MENU_LABEL_ACTIONS
}

#: Labels that shipped on persistent reply keyboards and were later renamed.
#: A client keeps showing its old keyboard until the bot sends a new one, so
#: a retired label must keep routing to the action it always meant.
_MENU_ACTIONS.update(
    {
        "🧑 Search by person": "person",
        "🧑 Поиск по людям": "person",
    }
)


def menu_action(text: str) -> str | None:
    """The action a reply-keyboard label triggers, in any language."""
    return _MENU_ACTIONS.get(text)


def main_menu(lang: str = "en") -> ReplyKeyboardMarkup:
    """The persistent shortcut bar, always one tap away."""
    return ReplyKeyboardMarkup(
        [
            [t(lang, "btn_search_podcast"), t(lang, "btn_search_person")],
            [t(lang, "btn_trending"), t(lang, "btn_surprise")],
            [t(lang, "btn_recent"), t(lang, "btn_help")],
            [t(lang, "btn_language")],
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
#: The language chooser.
LANG_PREFIX = "lang"
LEGAL_PREFIX = "legal"
#: Pick another skin from the result screen, then reopen the full editor.
#: Its own prefix keeps buttons on already-sent result cards routable.
RESKIN_PREFIX = "reskin"

NAV_BACK = f"{NAV_PREFIX}:back"
NAV_MENU = f"{NAV_PREFIX}:menu"
NAV_CANCEL = f"{NAV_PREFIX}:cancel"
#: Attached to labels that exist only to display something.
NAV_NOOP = f"{NAV_PREFIX}:noop"

ACTION_CUT = "act:cut"
ACTION_RETRY = "act:retry"
#: Render a short sample of the current clip in every skin, so nobody has
#: to spend ten cuts to find out what the buttons mean.
ACTION_DEMO = "act:demo"
#: The pre-video-note format toggle. No keyboard offers it any more, but
#: buttons on scrolled-past messages outlive keyboards, so the router still
#: answers it.
ACTION_TOGGLE_VOICE = "act:voice"
ACTION_NEW_CLIP = "act:new"
ACTION_FIND = "act:find"
ACTION_SUBTITLES = "act:subtitles"
LEGAL_ACCEPT = f"{LEGAL_PREFIX}:accept"
LEGAL_DECLINE = f"{LEGAL_PREFIX}:decline"

#: Delivery format and video-note skin choices on the clip editor.
FORMAT_PREFIX = "fmt"
SKIN_PREFIX = "skin"

_FORMAT_LABEL_KEYS = (
    (FORMAT_AUDIO, "fmt_audio"),
    (FORMAT_VOICE, "fmt_voice"),
    (FORMAT_NOTE, "fmt_note"),
    (FORMAT_VIDEO, "fmt_video"),
)

#: Skin key → i18n label key. The keys must equal ``video.SKINS`` — a test
#: holds them together — but the labels live in the i18n tables because this
#: module is the UI vocabulary and must not import the ffmpeg half of the world.
SKIN_LABELS = {
    "cover": "skin_cover",
    "vinyl": "skin_vinyl",
    "aurora": "skin_aurora",
    "party": "skin_party",
    "lava": "skin_lava",
    "matrix": "skin_matrix",
    "fractal": "skin_fractal",
    "dvd": "skin_dvd",
    "roblox": "skin_roblox",
    "gta": "skin_gta",
    "asmr": "skin_asmr",
    "subway": "skin_subway",
}

#: The skin rows split so twelve choices do not become twelve slivers: the
#: artwork family first, then the glowing visualisers, then the memes,
#: with the four curated loop looks closing the block in two rows.
_SKIN_ROWS = (
    ("cover", "vinyl", "dvd"),
    ("aurora", "party", "lava"),
    ("matrix", "fractal"),
    ("roblox", "gta"),
    ("asmr", "subway"),
)

_ARTWORK_REQUIRED_SKINS = frozenset({"cover", "vinyl"})


def _visible_skin_rows(
    has_artwork: bool,
    available_skins: Iterable[str] | None = None,
) -> tuple[tuple[str, ...], ...]:
    """Keep looks out when their required artwork or loop is unavailable."""
    available = (
        set(available_skins) if available_skins is not None else set(SKIN_LABELS)
    )
    if not has_artwork:
        available -= _ARTWORK_REQUIRED_SKINS
    return tuple(
        tuple(key for key in row if key in available)
        for row in _SKIN_ROWS
        if any(key in available for key in row)
    )

#: A found moment, carried as its start in seconds rather than an index into a
#: list: the list lives in a session, and a button on a scrolled-past message
#: outlives it.
MOMENT_PREFIX = "at"

#: What the Share button puts into the inline query: the episode's *id*, not
#: its title. A title pushed through fuzzy directory search used to answer
#: with somebody else's podcasts entirely; an id answers with the episode.
INLINE_EPISODE_PREFIX = "ep:"


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
    lang: str = "en", *, back: bool = True, menu: bool = True
) -> list[InlineKeyboardButton]:
    row = []
    if back:
        row.append(_button(t(lang, "btn_back"), NAV_BACK))
    if menu:
        row.append(_button(t(lang, "btn_menu"), NAV_MENU))
    return row


# -- screens ---------------------------------------------------------------


ACTION_CLEAR_FILTER = "act:unfilter"


def clear_filter_button(lang: str = "en") -> InlineKeyboardButton:
    return _button(t(lang, "btn_clear_filter"), ACTION_CLEAR_FILTER)


def choice_keyboard(
    items: Sequence[T],
    prefix: str,
    id_of: Callable[[T], str],
    label_of: Callable[[T], str],
    *,
    lang: str = "en",
    page: int = 1,
    pages: int = 1,
    back: bool = True,
    extra_rows: Sequence[Sequence[InlineKeyboardButton]] | None = None,
) -> InlineKeyboardMarkup:
    """A page of choices with pagination and a way back."""
    rows = list(_choice_rows(items, prefix, id_of, label_of, lang))

    nav = pagination_row(page, pages)
    if nav:
        rows.append(nav)

    for row in extra_rows or ():
        rows.append(list(row))

    footer = footer_row(lang, back=back)
    if footer:
        rows.append(footer)

    return InlineKeyboardMarkup(rows)


def _choice_rows(
    items: Iterable[T],
    prefix: str,
    id_of: Callable[[T], str],
    label_of: Callable[[T], str],
    lang: str,
) -> Iterable[list[InlineKeyboardButton]]:
    for item in items:
        payload = f"{prefix}:{id_of(item)}"
        # Telegram caps callback_data at 64 bytes. Truncating would select the
        # wrong item, so skip the entry instead.
        if len(payload.encode()) > 64:
            continue
        label = button_label(label_of(item), fallback=t(lang, "untitled"))
        yield [_button(label, payload)]


#: Clip lengths offered as one-tap presets, in seconds.
LENGTH_PRESETS = (30, 60, 180, 300)

#: How far the ◀ ▶ buttons move the clip, in seconds.
MOVE_STEPS = (-60, -15, 15, 60)


def _move_label(delta: int, lang: str) -> str:
    minutes, seconds = t(lang, "unit_minutes"), t(lang, "unit_seconds")
    if delta <= -60:
        return f"◀◀ {delta // 60}{minutes}"
    if delta < 0:
        return f"◀ {delta}{seconds}"
    if delta >= 60:
        return f"+{delta // 60}{minutes} ▶▶"
    return f"+{delta}{seconds} ▶"


def interval_keyboard(
    length: int,
    *,
    max_length: int,
    send_as: str = FORMAT_AUDIO,
    skin: str = "bars",
    can_search: bool = False,
    can_subtitle: bool = False,
    subtitles: bool = False,
    transcript_ready: bool = False,
    has_artwork: bool = True,
    available_skins: Iterable[str] | None = None,
    lang: str = "en",
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
        [
            _button(_move_label(step, lang), f"{MOVE_PREFIX}:{step}")
            for step in MOVE_STEPS
        ]
    )
    rows.append(
        [
            _button(
                f"● {t(lang, key)}" if fmt == send_as else t(lang, key),
                f"{FORMAT_PREFIX}:{fmt}",
                STYLE_SUCCESS if fmt == send_as else None,
            )
            for fmt, key in _FORMAT_LABEL_KEYS
        ]
    )
    if send_as in (FORMAT_NOTE, FORMAT_VIDEO):
        # The skins only matter once a video format is chosen; a permanent
        # block of decoration would bury the cut button under choices that
        # mean nothing for audio.
        for group in _visible_skin_rows(has_artwork, available_skins):
            rows.append(
                [
                    _button(
                        f"● {t(lang, SKIN_LABELS[key])}" if key == skin
                        else t(lang, SKIN_LABELS[key]),
                        f"{SKIN_PREFIX}:{key}",
                        STYLE_SUCCESS if key == skin else None,
                    )
                    for key in group
                ]
            )
        rows.append([_button(t(lang, "btn_demo"), ACTION_DEMO)])
    rows.append([_button(t(lang, "btn_cut"), ACTION_CUT, STYLE_PRIMARY)])
    if can_search:
        rows.append([_button(t(lang, "btn_find"), ACTION_FIND)])
    if send_as in (FORMAT_NOTE, FORMAT_VIDEO) and can_subtitle:
        subtitle_key = (
            "btn_subtitles_on"
            if subtitles
            else "btn_subtitles_ready" if transcript_ready
            else "btn_subtitles_slow"
        )
        rows.append(
            [
                _button(
                    t(lang, subtitle_key),
                    ACTION_SUBTITLES,
                    STYLE_SUCCESS if subtitles else None,
                )
            ]
        )
    rows.append(footer_row(lang))
    return InlineKeyboardMarkup(rows)


def moments_keyboard(
    moments: Sequence, phrase: str, lang: str = "en"
) -> InlineKeyboardMarkup:
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
    rows.append([_button(t(lang, "btn_search_again"), ACTION_FIND)])
    rows.append(footer_row(lang))
    return InlineKeyboardMarkup(rows)


def result_keyboard(
    share_query: str,
    lang: str = "en",
    episode_url: str | None = None,
    send_as: str | None = None,
    skin: str = "bars",
    has_artwork: bool = True,
    available_skins: Iterable[str] | None = None,
) -> InlineKeyboardMarkup:
    """Offered after a successful cut: adjust, re-skin, repeat, share — and
    the way back to the whole episode, which is the attribution a clip owes
    its source (see ROADMAP §13.4).

    The skin rows appear only when a visual format was just sent. A tap merely
    selects the next look and opens the editor; only its blue Cut button sends
    another file."""
    rows = [
        [
            _button(t(lang, "btn_earlier"), f"{SHIFT_PREFIX}:-15"),
            _button(t(lang, "btn_later"), f"{SHIFT_PREFIX}:15"),
        ],
    ]
    if send_as in (FORMAT_NOTE, FORMAT_VIDEO):
        for group in _visible_skin_rows(has_artwork, available_skins):
            rows.append(
                [
                    _button(
                        f"● {t(lang, SKIN_LABELS[key])}" if key == skin
                        else t(lang, SKIN_LABELS[key]),
                        f"{RESKIN_PREFIX}:{key}",
                        STYLE_SUCCESS if key == skin else None,
                    )
                    for key in group
                ]
            )
    rows += [
        [_button(t(lang, "btn_another_clip"), ACTION_NEW_CLIP)],
        [
            InlineKeyboardButton(
                t(lang, "btn_share"),
                switch_inline_query=share_query,
            )
        ],
    ]
    if episode_url:
        rows.append(
            [InlineKeyboardButton(t(lang, "btn_full_episode"), url=episode_url)]
        )
    rows.append([_button(t(lang, "btn_menu"), NAV_MENU)])
    return InlineKeyboardMarkup(rows)


def open_episode(link: str, lang: str = "en") -> InlineKeyboardMarkup:
    """One button, on a message the bot sent unprompted.

    A deep link rather than callback data on purpose: the message it sits under
    may outlive the session, the process, or both, and a `?start=ep_…` link is
    the one route back into an episode that needs neither.
    """
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t(lang, "btn_open"), url=link)]]
    )


def error_keyboard(lang: str = "en") -> InlineKeyboardMarkup:
    """Shown when a cut fails: retrying is usually worth one tap."""
    return InlineKeyboardMarkup(
        [
            [_button(t(lang, "btn_retry"), ACTION_RETRY, STYLE_PRIMARY)],
            footer_row(lang),
        ]
    )


def menu_keyboard(has_recent: bool, lang: str = "en") -> InlineKeyboardMarkup:
    """The inline twin of the reply keyboard, for tap-driven navigation."""
    rows = [
        [
            _button(t(lang, "btn_menu_podcast"), "menu:search", STYLE_PRIMARY),
            _button(t(lang, "btn_menu_person"), "menu:person"),
        ],
        [
            _button(t(lang, "btn_menu_trending"), "menu:trending"),
            _button(t(lang, "btn_menu_surprise"), "menu:surprise"),
        ],
    ]
    if has_recent:
        rows.append([_button(t(lang, "btn_menu_recent"), "menu:recent")])
    rows.append(
        [
            _button(t(lang, "btn_menu_help"), "menu:help"),
            _button(t(lang, "btn_menu_language"), "menu:language"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def language_keyboard(current: str) -> InlineKeyboardMarkup:
    """One button per language the bot speaks.

    The names are never translated — someone stuck in a language they cannot
    read must still be able to find their own — and the current choice is
    marked rather than hidden, so the screen answers "which is it now?" too.
    """
    rows = [
        [
            _button(
                f"● {LANGUAGE_NAMES[lang]}" if lang == current
                else LANGUAGE_NAMES[lang],
                f"{LANG_PREFIX}:{lang}",
                STYLE_SUCCESS if lang == current else None,
            )
        ]
        for lang in LANGUAGES
    ]
    rows.append(footer_row(current))
    return InlineKeyboardMarkup(rows)


def terms_keyboard(lang: str = "en") -> InlineKeyboardMarkup:
    """Explicit, versioned acceptance before the bot processes content."""
    return InlineKeyboardMarkup(
        [
            [_button(t(lang, "btn_accept_terms"), LEGAL_ACCEPT, STYLE_SUCCESS)],
            [_button(t(lang, "btn_decline_terms"), LEGAL_DECLINE, STYLE_DANGER)],
        ]
    )


def cancel_keyboard(lang: str = "en") -> InlineKeyboardMarkup:
    """Attached to prompts that wait for typed input."""
    return InlineKeyboardMarkup(
        [[_button(t(lang, "btn_cancel"), NAV_CANCEL, STYLE_DANGER)]]
    )
