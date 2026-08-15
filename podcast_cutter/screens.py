"""Screen rendering.

Each function turns session state into a :class:`View` — the message text and
its inline keyboard. They perform no I/O and touch no Telegram objects beyond
building markup, which makes the entire interface unit-testable without a bot.

Text comes from :mod:`~podcast_cutter.i18n` in the session's language. The
session is the source of truth for that language, which is why even screens
with no other state (:func:`ask_podcast`) take it.
"""

from __future__ import annotations

from dataclasses import dataclass

from telegram import InlineKeyboardMarkup

from . import keyboards as kb
from .api import Episode
from .config import Settings
from .i18n import plural, t
from .states import FORMAT_NOTE, FORMAT_VIDEO, Screen, Session
from .text import esc, format_duration, human_bytes, truncate
from .transcripts import excerpt
from .video import MAX_VIDEO_SECONDS, VIDEO_NOTE_SECONDS

#: Separator used in the breadcrumb line.
CRUMB = " › "


@dataclass(frozen=True)
class View:
    """A rendered screen."""

    text: str
    keyboard: InlineKeyboardMarkup | None = None


def breadcrumb(session: Session) -> str:
    """A dim trail showing where the user is, e.g. ``Search › Radiolab``."""
    lang = session.language
    screen = session.current.screen if session.current else Screen.MENU
    parts: list[str] = []

    if screen in (Screen.ASK_PODCAST, Screen.FEEDS, Screen.EPISODES):
        parts.append(t(lang, "crumb_search"))
        if screen is not Screen.ASK_PODCAST and session.query:
            parts.append(f"“{esc(truncate(session.query, 24))}”")
        if screen is Screen.EPISODES and session.feed:
            parts[-1] = esc(truncate(session.feed.title, 28))
    elif screen in (Screen.ASK_PERSON, Screen.GLOBAL):
        parts.append(t(lang, "crumb_person"))
        if screen is Screen.GLOBAL and session.query:
            parts.append(f"“{esc(truncate(session.query, 24))}”")
    elif screen is Screen.TRENDING:
        parts.append(t(lang, "crumb_trending"))
    elif screen is Screen.RECENT:
        parts.append(t(lang, "crumb_recent"))
    elif screen in (Screen.INTERVAL, Screen.RESULT, Screen.ASK_PHRASE,
                    Screen.MOMENTS):
        # Show where the episode came from, so the trail stays meaningful
        # after a deep link too.
        if session.feed:
            parts.append(esc(truncate(session.feed.title, 28)))
        elif session.episode:
            parts.append(esc(truncate(session.episode.feed_title, 28)))
        # And, when the clip came out of a search, what was searched for — the
        # trail is where "how did I get here" is answered.
        if session.phrase and (
            screen is Screen.MOMENTS or came_from_search(session)
        ):
            parts.append(f"🔎 “{esc(truncate(session.phrase, 20))}”")

    if not parts:
        return ""
    return f"<i>{CRUMB.join(parts)}</i>\n\n"


def came_from_search(session: Session) -> bool:
    """Whether the moments list is one ``‹ Back`` away.

    Asked of the history rather than of ``session.phrase``, which outlives the
    screen that set it: a user who searches, leaves, and opens another episode
    by hand still has a phrase, and would be told to go back to a list that is
    no longer behind them.
    """
    return any(nav.screen is Screen.MOMENTS for nav in session.history)


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
        t(session.language, "menu_screen"),
        kb.menu_keyboard(
            has_recent=bool(session.recents), lang=session.language
        ),
    )


def ask_podcast(session: Session) -> View:
    return View(
        t(session.language, "ask_podcast"),
        kb.cancel_keyboard(session.language),
    )


def ask_person(session: Session) -> View:
    return View(
        t(session.language, "ask_person"),
        kb.cancel_keyboard(session.language),
    )


def language(session: Session) -> View:
    """The language chooser — the one screen that speaks both at once."""
    return View(
        t(session.language, "language_screen"),
        kb.language_keyboard(session.language),
    )


def feeds(session: Session, settings: Settings) -> View:
    lang = session.language
    page = session.current.page if session.current else 1
    # The upstream search has no total count, so pages are only known one at a
    # time: show the current page plus whether another exists.
    pages = page + 1 if session.feeds_has_next else page

    return View(
        breadcrumb(session) + t(lang, "feeds_found", n=len(session.feeds)),
        kb.choice_keyboard(
            session.feeds,
            kb.FEED_PREFIX,
            id_of=lambda f: f.id,
            label_of=lambda f: f"{f.title} — {f.author}",
            lang=lang,
            page=page,
            pages=pages,
        ),
    )


def episodes(session: Session, settings: Settings) -> View:
    lang = session.language
    page = session.current.page if session.current else 1
    visible = session.visible_episodes
    window, page, pages = session.page_of(
        visible, page, settings.episodes_per_page
    )

    if session.episode_filter:
        query = esc(truncate(session.episode_filter, 30))
        if visible:
            heading = t(
                lang, "filter_match",
                n=len(visible), total=len(session.episodes), query=query,
            )
        else:
            heading = t(lang, "filter_none", query=query)
    else:
        count = len(session.episodes)
        heading = t(
            lang, "episodes_heading",
            n=count, episodes=plural(lang, "episodes", count),
        )

    return View(
        breadcrumb(session) + heading,
        kb.choice_keyboard(
            window,
            kb.EPISODE_PREFIX,
            id_of=lambda e: e.id,
            label_of=episode_label,
            lang=lang,
            page=page,
            pages=pages,
            extra_rows=(
                [[kb.clear_filter_button(lang)]]
                if session.episode_filter
                else None
            ),
        ),
    )


def _filtered_heading(session: Session, visible: list, total: int) -> str:
    """What a narrowed cross-feed list says about itself."""
    lang = session.language
    query = esc(truncate(session.episode_filter, 30))
    if visible:
        return t(lang, "filter_match", n=len(visible), total=total, query=query)
    return t(lang, "filter_none", query=query)


def global_episodes(session: Session, settings: Settings) -> View:
    lang = session.language
    page = session.current.page if session.current else 1
    visible = session.filter_episodes(session.episodes)
    window, page, pages = session.page_of(
        visible, page, settings.episodes_per_page
    )

    if session.episode_filter:
        heading = _filtered_heading(session, visible, len(session.episodes))
    else:
        count = len(session.episodes)
        heading = t(
            lang, "global_heading",
            n=count, episodes=plural(lang, "episodes_in", count),
        )

    return View(
        breadcrumb(session) + heading,
        kb.choice_keyboard(
            window,
            kb.EPISODE_PREFIX,
            id_of=lambda e: e.id,
            label_of=lambda e: f"{e.feed_title}: {e.title}",
            lang=lang,
            page=page,
            pages=pages,
            extra_rows=(
                [[kb.clear_filter_button(lang)]]
                if session.episode_filter
                else None
            ),
        ),
    )


def trending(session: Session, settings: Settings) -> View:
    lang = session.language
    page = session.current.page if session.current else 1
    window, page, pages = session.page_of(
        session.feeds, page, settings.podcasts_per_page
    )

    return View(
        breadcrumb(session) + t(lang, "trending_heading"),
        kb.choice_keyboard(
            window,
            kb.FEED_PREFIX,
            id_of=lambda f: f.id,
            label_of=lambda f: f"{f.title} — {f.author}",
            lang=lang,
            page=page,
            pages=pages,
        ),
    )


def recent(session: Session, settings: Settings) -> View:
    lang = session.language
    if not session.recents:
        return View(
            breadcrumb(session) + t(lang, "recent_empty"),
            InlineKeyboardMarkup([kb.footer_row(lang)]),
        )

    page = session.current.page if session.current else 1
    visible = session.filter_episodes(session.recents)
    window, page, pages = session.page_of(
        visible, page, settings.episodes_per_page
    )

    heading = (
        _filtered_heading(session, visible, len(session.recents))
        if session.episode_filter
        else t(lang, "recent_heading")
    )

    return View(
        breadcrumb(session) + heading,
        kb.choice_keyboard(
            window,
            kb.EPISODE_PREFIX,
            id_of=lambda e: e.id,
            label_of=lambda e: f"{e.feed_title}: {e.title}",
            lang=lang,
            page=page,
            pages=pages,
            extra_rows=(
                [[kb.clear_filter_button(lang)]]
                if session.episode_filter
                else None
            ),
        ),
    )


def interval(session: Session, settings: Settings) -> View:
    """The clip editor — the screen users spend the most time on."""
    lang = session.language
    episode = session.episode
    if episode is None:  # pragma: no cover - guarded by the router
        return menu(session)

    start, end = session.clip_start, session.clip_end
    body = (
        f"{_episode_heading(episode)}\n\n"
        + t(
            lang, "interval_editor",
            start=format_duration(start),
            end=format_duration(end),
            length=format_duration(session.clip_length),
        )
    )

    # Telegram's limits change what is honest to promise per format, so the
    # screen says where the current length stands before the cut button
    # finds out. Nothing is switched behind the user's back: a circle that
    # cannot fit stays a refusal with a way out, not a silent square video.
    if session.send_as == FORMAT_NOTE and session.clip_length > VIDEO_NOTE_SECONDS:
        body += f"\n\n<i>{t(lang, 'circle_rule')}</i>"
    if session.send_as == FORMAT_VIDEO and session.clip_length > MAX_VIDEO_SECONDS:
        body += (
            "\n\n<i>"
            + t(lang, "video_cap", limit=format_duration(MAX_VIDEO_SECONDS))
            + "</i>"
        )

    # Arriving from a search replaces the list of moments with this screen, so
    # say where the others went. `‹ Back` already returns to them; without this
    # line the only way to know is to try it, and a person who found three
    # moments and wants the second one should not have to guess.
    if came_from_search(session) and len(session.moments) > 1:
        count = len(session.moments)
        body += (
            "\n\n<i>"
            + t(
                lang, "back_to_moments",
                n=count,
                moments=plural(lang, "moments_to", count),
                phrase=esc(truncate(session.phrase, 40)),
            )
            + "</i>"
        )

    return View(
        breadcrumb(session) + body,
        kb.interval_keyboard(
            session.clip_length,
            max_length=settings.max_cut_seconds,
            send_as=session.send_as,
            skin=session.skin,
            can_search=settings.asr_enabled,
            lang=lang,
        ),
    )


def ask_phrase(session: Session, transcribed: bool) -> View:
    """Asking what to look for inside this episode.

    Whether the episode has been listened to already changes what is honest to
    promise, so it changes the text: the first search on an episode costs
    minutes, and a user who is not told that will assume the bot has hung.
    """
    lang = session.language
    episode = session.episode
    heading = f"{_episode_heading(episode)}\n\n" if episode else ""

    promise = t(lang, "promise_instant" if transcribed else "promise_first")

    return View(
        breadcrumb(session) + heading + t(lang, "ask_phrase") + promise,
        kb.cancel_keyboard(lang),
    )


def moments(session: Session) -> View:
    """What a search found, or a clear statement that it found nothing."""
    lang = session.language
    episode = session.episode
    heading = f"{_episode_heading(episode)}\n\n" if episode else ""
    asked = esc(truncate(session.phrase, 60))

    if not session.moments:
        # Said plainly and without a suggestion to try synonyms, because the
        # usual cause is that the words really were not spoken — and the second
        # most usual is that they were spoken and misheard, which no rephrasing
        # of the same query will fix either.
        return View(
            breadcrumb(session) + heading + t(lang, "moments_none", query=asked),
            kb.moments_keyboard([], session.phrase, lang),
        )

    # The context goes in the message, not on the buttons. A button is one
    # short line, so three of them truncated mid-sentence read as three
    # unrelated fragments — which is exactly how this looked before.
    lines = []
    for index, moment in enumerate(session.moments, start=1):
        before, match, after = excerpt(moment.text, session.phrase)
        # Joined here, with the separators outside the escaped parts: `esc`
        # collapses and strips whitespace, so a space tucked inside a fragment
        # never arrives.
        parts = [esc(before), f"<b>{esc(match)}</b>" if match else "", esc(after)]
        quoted = " ".join(part for part in parts if part)
        lines.append(
            f"<b>{index}.</b> <code>{format_duration(int(moment.clip_start))}</code>"
            f"\n{quoted}"
        )

    count = len(session.moments)
    return View(
        breadcrumb(session)
        + heading
        + t(
            lang, "moments_header",
            query=asked, n=count, moments=plural(lang, "moments", count),
        )
        + "\n\n"
        + "\n\n".join(lines)
        + "\n\n"
        + t(lang, "moments_tap"),
        kb.moments_keyboard(session.moments, session.phrase, lang),
    )


def result(session: Session, bot_username: str) -> View:
    lang = session.language
    episode = session.episode
    heading = _episode_heading(episode) if episode else ""
    # The id, not the title: the title went through the directory's fuzzy
    # search and shared somebody else's podcasts (observed live, 2026-08-15).
    share = f"{kb.INLINE_EPISODE_PREFIX}{episode.id}" if episode else ""

    body = (
        breadcrumb(session)
        + f"{heading}\n\n"
        + t(
            lang, "result_sent",
            start=format_duration(session.clip_start),
            end=format_duration(session.clip_end),
        )
    )
    if episode and session.send_as in (FORMAT_NOTE, FORMAT_VIDEO):
        body += f"\n\n<i>{t(lang, 'reskin_hint')}</i>"

    return View(
        body,
        kb.result_keyboard(
            share,
            lang,
            # Attribution: the way back to the whole episode rides on the
            # result screen, which is also what a captionless video note has.
            episode_url=episode.enclosure_url if episode else None,
            send_as=session.send_as,
            skin=session.skin,
        ),
    )


def stats(day, week, db_bytes: int, lang: str = "en") -> View:
    """The operator's panel: is the bot actually working, and for whom."""
    lines = ["📊 <b>Podcast Cutter</b>", ""]

    for label_key, window in (("stats_24h", day), ("stats_7d", week)):
        rate = window.success_rate
        rate_text = f"{rate * 100:.0f}%" if rate is not None else "—"
        lines.append(f"<b>{t(lang, label_key)}</b>")
        lines.append(
            t(
                lang, "stats_clips",
                ok=window.cuts_ok, failed=window.cuts_failed, rate=rate_text,
            )
        )
        lines.append(t(lang, "stats_people", n=window.unique_users))
        if window.median_ms is not None:
            lines.append(
                t(
                    lang, "stats_time",
                    median=f"{window.median_ms / 1000:.1f}",
                    worst=f"{(window.slowest_ms or 0) / 1000:.1f}",
                )
            )
        if window.cuts_ok:
            lines.append(
                t(
                    lang, "stats_voice",
                    share=f"{window.voice_share * 100:.0f}",
                    size=human_bytes(window.total_bytes, lang),
                )
            )
        lines.append("")

    if week.failures:
        lines.append(f"<b>{t(lang, 'stats_failures')}</b>")
        lines += [f"  {esc(name)} × {count}" for name, count in week.failures]
        lines.append("")

    if week.top_podcasts:
        lines.append(f"<b>{t(lang, 'stats_top')}</b>")
        lines += [
            f"  {esc(truncate(name, 40))} × {count}"
            for name, count in week.top_podcasts
        ]
        lines.append("")

    if week.sources:
        lines.append(f"<b>{t(lang, 'stats_sources')}</b>")
        lines += [
            f"  {esc(truncate(name, 32))} × {count}" for name, count in week.sources
        ]
        lines.append("")

    if week.actions:
        summary = " · ".join(f"{esc(name)} {count}" for name, count in week.actions)
        lines.append(f"<b>{t(lang, 'stats_activity')}</b>\n  {summary}")
        lines.append("")

    lines.append(
        f"<i>{t(lang, 'stats_journal', size=human_bytes(db_bytes, lang))}</i>"
    )
    return View("\n".join(lines).strip())


def working(message: str) -> View:
    return View(message, None)


def failure(message: str, lang: str = "en") -> View:
    return View(f"⚠️ {esc(message)}", kb.error_keyboard(lang))
