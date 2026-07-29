"""Keyboards and the callback-data vocabulary.

Callback payloads are namespaced (``feed:123``, ``ep:456``, ``nav:next``).
Previously a raw id shared the same space as the control words ``next_page`` and
``prev_page``, so behaviour depended on ids never colliding with them, and a
button press in the wrong state was indistinguishable from a real selection.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from typing import TypeVar

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

from .text import button_label

T = TypeVar("T")

# -- main menu -------------------------------------------------------------

BTN_SEARCH_PODCAST = "🔍 Search a podcast"
BTN_SEARCH_PERSON = "🧑 Search by person"
BTN_TRENDING = "🔥 Trending"
BTN_SURPRISE = "🎲 Surprise me"
BTN_HELP = "❓ Help"

MENU_BUTTONS = (
    BTN_SEARCH_PODCAST,
    BTN_SEARCH_PERSON,
    BTN_TRENDING,
    BTN_SURPRISE,
    BTN_HELP,
)


def menu_regex(*labels: str) -> str:
    """Exact-match regex for reply-keyboard buttons, emoji included."""
    alternatives = "|".join(re.escape(label) for label in labels)
    return f"^(?:{alternatives})$"


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [BTN_SEARCH_PODCAST, BTN_SEARCH_PERSON],
            [BTN_TRENDING, BTN_SURPRISE],
            [BTN_HELP],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


# -- inline callback data --------------------------------------------------

FEED_PREFIX = "feed"
EPISODE_PREFIX = "ep"
NAV_PREFIX = "nav"

NAV_NEXT = f"{NAV_PREFIX}:next"
NAV_PREV = f"{NAV_PREFIX}:prev"
NAV_CANCEL = f"{NAV_PREFIX}:cancel"


def parse_callback(data: str | None) -> tuple[str, str]:
    """Split ``"ep:1234"`` into ``("ep", "1234")``.

    Unrecognised or empty payloads come back as ``("", "")`` so callers can
    respond to a stale button instead of raising.
    """
    if not data or ":" not in data:
        return "", ""
    prefix, _, value = data.partition(":")
    return prefix, value


def _choice_rows(
    items: Iterable[T],
    prefix: str,
    id_of: Callable[[T], str],
    label_of: Callable[[T], str],
) -> list[list[InlineKeyboardButton]]:
    rows = []
    for item in items:
        payload = f"{prefix}:{id_of(item)}"
        # Telegram caps callback_data at 64 bytes; ids this long are not real,
        # but truncating silently would select the wrong item, so skip instead.
        if len(payload.encode()) > 64:
            continue
        rows.append(
            [InlineKeyboardButton(button_label(label_of(item)), callback_data=payload)]
        )
    return rows


def _nav_row(has_prev: bool, has_next: bool) -> list[InlineKeyboardButton]:
    row = []
    if has_prev:
        row.append(InlineKeyboardButton("« Previous", callback_data=NAV_PREV))
    if has_next:
        row.append(InlineKeyboardButton("Next »", callback_data=NAV_NEXT))
    return row


def choice_keyboard(
    items: Sequence[T],
    prefix: str,
    id_of: Callable[[T], str],
    label_of: Callable[[T], str],
    *,
    has_prev: bool = False,
    has_next: bool = False,
) -> InlineKeyboardMarkup:
    """A page of choices, with pagination and a cancel button."""
    rows = _choice_rows(items, prefix, id_of, label_of)

    nav = _nav_row(has_prev, has_next)
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton("✖️ Cancel", callback_data=NAV_CANCEL)])
    return InlineKeyboardMarkup(rows)
