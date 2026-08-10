"""Screen rendering.

Each function turns session state into a :class:`View` — the message text and
its inline keyboard. They perform no I/O and touch no Telegram objects beyond
building markup, which makes the entire interface unit-testable without a bot.
"""

from __future__ import annotations

from dataclasses import dataclass

from telegram import InlineKeyboardMarkup

from . import keyboards as kb
from .api import Episode
from .config import Settings
from .states import Screen, Session
from .text import esc, format_duration, human_bytes, truncate

#: Separator used in the breadcrumb line.
CRUMB = " › "


@dataclass(frozen=True)
class View:
    """A rendered screen."""

    text: str
    keyboard: InlineKeyboardMarkup | None = None


def breadcrumb(session: Session) -> str:
    """A dim trail showing where the user is, e.g. ``Search › Radiolab``."""
    screen = session.current.screen if session.current else Screen.MENU
    parts: list[str] = []

    if screen in (Screen.ASK_PODCAST, Screen.FEEDS, Screen.EPISODES):
        parts.append("🔍 Search")
        if screen is not Screen.ASK_PODCAST and session.query:
            parts.append(f"“{esc(truncate(session.query, 24))}”")
        if screen is Screen.EPISODES and session.feed:
            parts[-1] = esc(truncate(session.feed.title, 28))
    elif screen in (Screen.ASK_PERSON, Screen.GLOBAL):
        parts.append("🧑 Person")
        if screen is Screen.GLOBAL and session.query:
            parts.append(f"“{esc(truncate(session.query, 24))}”")
    elif screen is Screen.TRENDING:
        parts.append("🔥 Trending")
    elif screen is Screen.RECENT:
        parts.append("🕘 Recent")
    elif screen in (Screen.INTERVAL, Screen.RESULT, Screen.ASK_PHRASE,
                    Screen.MOMENTS):
        # Show where the episode came from, so the trail stays meaningful
        # after a deep link too.
        if session.feed:
            parts.append(esc(truncate(session.feed.title, 28)))
        elif session.episode:
            parts.append(esc(truncate(session.episode.feed_title, 28)))

    if not parts:
        return ""
    return f"<i>{CRUMB.join(parts)}</i>\n\n"


def episode_label(episode: Episode) -> str:
    if episode.duration:
        return f"{episode.title}  ·  {format_duration(episode.duration)}"
    return episode.title


def _episode_heading(episode: Episode) -> str:
    length = (
        f"  ·  {format_duration(episode.duration)}" if episode.duration else ""
    )
    return (
        f"🎧 <b>{esc(truncate(episode.title, 140))}</b>\n"
        f"{esc(truncate(episode.feed_title, 60))}{length}"
    )


# --------------------------------------------------------------------------
# Screens
# --------------------------------------------------------------------------


def menu(session: Session) -> View:
    return View(
        "🎙 <b>Podcast Cutter</b>\n"
        "Find an episode, pick a moment, get just that part.\n\n"
        "Tap below, or just send me a podcast name.",
        kb.menu_keyboard(has_recent=bool(session.recents)),
    )


def ask_podcast() -> View:
    return View(
        "🔍 <b>Which podcast?</b>\n\nSend me its name.",
        kb.cancel_keyboard(),
    )


def ask_person() -> View:
    return View(
        "🧑 <b>Who or what?</b>\n\n"
        "I'll look across every podcast in the directory — a guest's name, a "
        "topic, anything.",
        kb.cancel_keyboard(),
    )


def feeds(session: Session, settings: Settings) -> View:
    page = session.current.page if session.current else 1
    # The upstream search has no total count, so pages are only known one at a
    # time: show the current page plus whether another exists.
    pages = page + 1 if session.feeds_has_next else page

    return View(
        breadcrumb(session)
        + f"Found <b>{len(session.feeds)}</b> on this page. Pick one, "
        "or send a different name.",
        kb.choice_keyboard(
            session.feeds,
            kb.FEED_PREFIX,
            id_of=lambda f: f.id,
            label_of=lambda f: f"{f.title} — {f.author}",
            page=page,
            pages=pages,
        ),
    )


def episodes(session: Session, settings: Settings) -> View:
    page = session.current.page if session.current else 1
    visible = session.visible_episodes
    window, page, pages = session.page_of(
        visible, page, settings.episodes_per_page
    )

    if session.episode_filter:
        heading = (
            f"🔎 <b>{len(visible)}</b> of {len(session.episodes)} match "
            f"“{esc(truncate(session.episode_filter, 30))}”."
        )
        if not visible:
            heading = (
                f"🔎 Nothing matches “{esc(truncate(session.episode_filter, 30))}”. "
                "Try other words."
            )
    else:
        heading = (
            f"🎧 <b>{len(session.episodes)}</b> episodes. "
            "Pick one, or type part of a title to filter."
        )

    return View(
        breadcrumb(session) + heading,
        kb.choice_keyboard(
            window,
            kb.EPISODE_PREFIX,
            id_of=lambda e: e.id,
            label_of=episode_label,
            page=page,
            pages=pages,
            extra_rows=(
                [[kb.clear_filter_button()]] if session.episode_filter else None
            ),
        ),
    )


def global_episodes(session: Session, settings: Settings) -> View:
    page = session.current.page if session.current else 1
    window, page, pages = session.page_of(
        session.episodes, page, settings.episodes_per_page
    )

    return View(
        breadcrumb(session)
        + f"🔎 <b>{len(session.episodes)}</b> episodes mention that.",
        kb.choice_keyboard(
            window,
            kb.EPISODE_PREFIX,
            id_of=lambda e: e.id,
            label_of=lambda e: f"{e.feed_title}: {e.title}",
            page=page,
            pages=pages,
        ),
    )


def trending(session: Session, settings: Settings) -> View:
    page = session.current.page if session.current else 1
    window, page, pages = session.page_of(
        session.feeds, page, settings.podcasts_per_page
    )

    return View(
        breadcrumb(session) + "🔥 <b>Popular right now.</b>",
        kb.choice_keyboard(
            window,
            kb.FEED_PREFIX,
            id_of=lambda f: f.id,
            label_of=lambda f: f"{f.title} — {f.author}",
            page=page,
            pages=pages,
        ),
    )


def recent(session: Session, settings: Settings) -> View:
    if not session.recents:
        return View(
            breadcrumb(session)
            + "🕘 Nothing here yet — episodes you cut show up in this list.",
            InlineKeyboardMarkup([kb.footer_row()]),
        )

    page = session.current.page if session.current else 1
    window, page, pages = session.page_of(
        session.recents, page, settings.episodes_per_page
    )

    return View(
        breadcrumb(session) + "🕘 <b>Episodes you looked at recently.</b>",
        kb.choice_keyboard(
            window,
            kb.EPISODE_PREFIX,
            id_of=lambda e: e.id,
            label_of=lambda e: f"{e.feed_title}: {e.title}",
            page=page,
            pages=pages,
        ),
    )


def interval(session: Session, settings: Settings) -> View:
    """The clip editor — the screen users spend the most time on."""
    episode = session.episode
    if episode is None:  # pragma: no cover - guarded by the router
        return menu(session)

    start, end = session.clip_start, session.clip_end
    body = (
        f"{_episode_heading(episode)}\n\n"
        f"✂️ <b>{format_duration(start)} → {format_duration(end)}</b>"
        f"   <i>({format_duration(session.clip_length)})</i>\n\n"
        f"Send a timestamp to jump there — <code>12:30</code> for a "
        f"{format_duration(session.clip_length)} clip, or "
        f"<code>12:30-14:00</code> for an exact range."
    )

    return View(
        breadcrumb(session) + body,
        kb.interval_keyboard(
            session.clip_length,
            max_length=settings.max_cut_seconds,
            as_voice=session.as_voice,
            can_search=settings.asr_enabled,
        ),
    )


def ask_phrase(session: Session, transcribed: bool) -> View:
    """Asking what to look for inside this episode.

    Whether the episode has been listened to already changes what is honest to
    promise, so it changes the text: the first search on an episode costs
    minutes, and a user who is not told that will assume the bot has hung.
    """
    episode = session.episode
    heading = f"{_episode_heading(episode)}\n\n" if episode else ""

    if transcribed:
        promise = "This episode is already transcribed, so this is instant."
    else:
        promise = (
            "⏳ Nobody has searched this episode yet, so I'll listen to it "
            "first. That takes a few minutes — you'll see progress, and it "
            "only happens once per episode."
        )

    return View(
        breadcrumb(session)
        + heading
        + "🔎 <b>What was said?</b>\n\n"
        "Send a word or a phrase and I'll find where it comes up.\n\n"
        + promise,
        kb.cancel_keyboard(),
    )


def moments(session: Session) -> View:
    """What a search found, or a clear statement that it found nothing."""
    episode = session.episode
    heading = f"{_episode_heading(episode)}\n\n" if episode else ""
    asked = esc(truncate(session.phrase, 60))

    if not session.moments:
        # Said plainly and without a suggestion to try synonyms, because the
        # usual cause is that the words really were not spoken — and the second
        # most usual is that they were spoken and misheard, which no rephrasing
        # of the same query will fix either.
        return View(
            breadcrumb(session)
            + heading
            + f"🔎 <b>“{asked}”</b>\n\n"
            "Nothing in this episode matches that.\n\n"
            "It may not have been said — or it was said and misheard: "
            "transcription is imperfect on names and jargon.",
            kb.moments_keyboard([], session.phrase),
        )

    return View(
        breadcrumb(session)
        + heading
        + f"🔎 <b>“{asked}”</b> — {len(session.moments)} "
        f"{'moment' if len(session.moments) == 1 else 'moments'}\n\n"
        "Tap one to open the clip editor there.",
        kb.moments_keyboard(session.moments, session.phrase),
    )


def result(session: Session, bot_username: str) -> View:
    episode = session.episode
    heading = _episode_heading(episode) if episode else ""
    share = episode.title if episode else ""

    return View(
        breadcrumb(session)
        + f"{heading}\n\n"
        f"✅ Sent <b>{format_duration(session.clip_start)} → "
        f"{format_duration(session.clip_end)}</b>\n\n"
        "Not quite the right moment? Nudge it below.",
        kb.result_keyboard(truncate(share, 40)),
    )


def stats(day, week, db_bytes: int) -> View:
    """The operator's panel: is the bot actually working, and for whom."""
    lines = ["📊 <b>Podcast Cutter</b>", ""]

    for label, window in (("Last 24h", day), ("Last 7 days", week)):
        rate = window.success_rate
        rate_text = f"{rate * 100:.0f}%" if rate is not None else "—"
        lines.append(f"<b>{label}</b>")
        lines.append(
            f"  clips: {window.cuts_ok} ok · {window.cuts_failed} failed"
            f"  ({rate_text})"
        )
        lines.append(f"  people: {window.unique_users}")
        if window.median_ms is not None:
            lines.append(
                f"  time: {window.median_ms / 1000:.1f}s median · "
                f"{(window.slowest_ms or 0) / 1000:.1f}s worst"
            )
        if window.cuts_ok:
            lines.append(
                f"  voice notes: {window.voice_share * 100:.0f}%"
                f" · sent {human_bytes(window.total_bytes)}"
            )
        lines.append("")

    if week.failures:
        lines.append("<b>Failures this week</b>")
        lines += [f"  {esc(name)} × {count}" for name, count in week.failures]
        lines.append("")

    if week.top_podcasts:
        lines.append("<b>Most cut this week</b>")
        lines += [
            f"  {esc(truncate(name, 40))} × {count}"
            for name, count in week.top_podcasts
        ]
        lines.append("")

    if week.sources:
        lines.append("<b>Where people came from this week</b>")
        lines += [
            f"  {esc(truncate(name, 32))} × {count}" for name, count in week.sources
        ]
        lines.append("")

    if week.actions:
        summary = " · ".join(f"{esc(name)} {count}" for name, count in week.actions)
        lines.append(f"<b>Activity</b>\n  {summary}")
        lines.append("")

    lines.append(f"<i>journal: {human_bytes(db_bytes)}</i>")
    return View("\n".join(lines).strip())


def working(message: str) -> View:
    return View(message, None)


def failure(message: str) -> View:
    return View(f"⚠️ {esc(message)}", kb.error_keyboard())
