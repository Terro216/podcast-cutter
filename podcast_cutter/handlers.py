"""Telegram handlers.

Two rules shape this module:

* **No dead ends.** Every error path returns a state that actually has
  handlers registered, and the main-menu buttons re-enter the conversation from
  anywhere (``allow_reentry``). Previously an API hiccup could return the user
  to ``ENTER_EPISODE_NAME``, a state with an empty handler list, leaving them
  unable to do anything but ``/cancel``.
* **No unbounded work.** Cutting is capped by a semaphore and every temporary
  file lives in a per-job directory that is removed in a ``finally``.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardMarkup, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes, ConversationHandler

from . import keyboards as kb
from .api import Episode, Feed, PodcastIndexClient
from .audio import Interval, cut_episode, parse_interval
from .config import Settings
from .errors import PodcastCutterError
from .states import State, get_session, reset_session
from .text import button_label, format_duration, one_line, safe_filename, truncate

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "🎙 *Podcast Cutter*\n\n"
    "Find an episode, tell me a time range, and I send back just that part.\n\n"
    "*Commands*\n"
    "/start — main menu\n"
    "/search — find a podcast by name\n"
    "/person — find episodes mentioning someone\n"
    "/trending — what is popular right now\n"
    "/surprise — a random episode\n"
    "/cancel — stop what you are doing\n"
    "/help — this message\n\n"
    "*Time ranges*\n"
    "`01:20-02:00` · `1:05:00-1:07:30` · `90-150` · `1h2m-1h5m`"
)

GENERIC_ERROR = "⚠️ Something went wrong on my side. Please try again."


def guard(
    fallback_state: int, hint: str = ""
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Turn expected failures into a message plus a usable next state.

    Without this, an exception escaping a handler leaves the conversation in
    whatever state it was in with no feedback to the user at all.
    """

    def decorator(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(func)
        async def wrapper(
            self: PodcastCutterBot, update: Update, context: Any
        ) -> Any:
            try:
                return await func(self, update, context)
            except PodcastCutterError as exc:
                text = f"⚠️ {exc.user_message}"
                if hint:
                    text = f"{text}\n\n{hint}"
                await self.respond(update, text)
                return fallback_state

        return wrapper

    return decorator


class PodcastCutterBot:
    """Handler collection, holding the shared API client and job limiter."""

    def __init__(self, settings: Settings, client: PodcastIndexClient) -> None:
        self.settings = settings
        self.client = client
        self._job_slots = asyncio.Semaphore(settings.max_concurrent_jobs)
        #: User ids with a cut in flight — one heavy job per person.
        self._busy_users: set[int] = set()

    # ------------------------------------------------------------------
    # Reply plumbing
    # ------------------------------------------------------------------

    async def respond(
        self,
        update: Update,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ):
        """Show ``text`` to the user, editing in place where that makes sense.

        Callback queries arrive attached to the message holding the button, so
        editing it keeps the chat tidy. Falls back to a fresh message when the
        edit is impossible (message too old, content unchanged, or the update
        was a plain message to begin with).
        """
        query = update.callback_query
        if query is not None and query.message is not None:
            try:
                return await query.edit_message_text(text, reply_markup=reply_markup)
            except BadRequest as exc:
                logger.debug("Could not edit message, sending a new one: %s", exc)

        message = update.effective_message
        if message is None:
            logger.warning("Update %s has no message to reply to", update.update_id)
            return None
        return await message.reply_text(text, reply_markup=reply_markup)

    @staticmethod
    async def _ack(update: Update) -> str:
        """Acknowledge a callback query and return its payload."""
        query = update.callback_query
        if query is None:
            return ""
        with contextlib.suppress(TelegramError):
            await query.answer()
        return query.data or ""

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        reset_session(context.user_data)
        await update.effective_message.reply_text(
            "👋 Hi! Pick something below, or send /help to see everything I can do.",
            reply_markup=kb.main_menu(),
        )
        return ConversationHandler.END

    async def help_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await update.effective_message.reply_text(
            HELP_TEXT, parse_mode="Markdown", reply_markup=kb.main_menu()
        )

    async def ask_podcast_name(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        reset_session(context.user_data)
        await update.effective_message.reply_text(
            "🔍 What podcast are you looking for?", reply_markup=kb.main_menu()
        )
        return State.ASK_PODCAST_NAME

    async def ask_person_query(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        reset_session(context.user_data)
        await update.effective_message.reply_text(
            "🧑 Who or what should I look for across all podcasts?",
            reply_markup=kb.main_menu(),
        )
        return State.ASK_PERSON_QUERY

    async def trending(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        session = reset_session(context.user_data)
        status = await update.effective_message.reply_text("🔥 Fetching trending…")

        try:
            feeds = await self.client.trending_feeds()
        except PodcastCutterError as exc:
            # Edit the placeholder rather than leaving "Fetching…" hanging.
            await status.edit_text(f"⚠️ {exc.user_message}")
            return ConversationHandler.END

        session.remember_feeds(feeds)

        # Trending is a single fixed list, so no pagination controls.
        await status.edit_text(
            "🔥 Trending right now — pick one:",
            reply_markup=kb.choice_keyboard(
                feeds,
                kb.FEED_PREFIX,
                id_of=lambda f: f.id,
                label_of=lambda f: f"{f.title} — {f.author}",
            ),
        )
        return State.CHOOSE_PODCAST

    async def surprise(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        session = reset_session(context.user_data)
        status = await update.effective_message.reply_text("🎲 Picking an episode…")

        try:
            episode = await self.client.random_episode()
        except PodcastCutterError as exc:
            await status.edit_text(f"⚠️ {exc.user_message}")
            return ConversationHandler.END

        session.episode = episode

        await status.edit_text(
            f"🎲 Here you go:\n\n"
            f"{one_line(episode.feed_title)}\n"
            f"{truncate(one_line(episode.title), 120)}\n\n"
            f"{self._interval_prompt(episode)}"
        )
        return State.ASK_INTERVAL

    # ------------------------------------------------------------------
    # Podcast search
    # ------------------------------------------------------------------

    @guard(State.ASK_PODCAST_NAME, "Try another name:")
    async def handle_podcast_name(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        session = get_session(context.user_data)
        session.query = one_line(update.effective_message.text)
        session.feed_page = 1
        if not session.query:
            await self.respond(update, "Please send me a podcast name.")
            return State.ASK_PODCAST_NAME
        return await self._show_feeds(update, context)

    async def _show_feeds(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        session = get_session(context.user_data)
        feeds, has_next = await self.client.search_feeds(
            session.query, session.feed_page
        )
        session.remember_feeds(feeds)

        if len(feeds) == 1 and session.feed_page == 1:
            session.select_feed(feeds[0])
            await self.respond(update, f"📻 {truncate(feeds[0].title, 80)}")
            return await self._show_episodes(update, context, State.CHOOSE_EPISODE)

        await self.respond(
            update,
            f"📻 Results for “{truncate(session.query, 60)}” "
            f"(page {session.feed_page}) — pick one, or send another name:",
            reply_markup=kb.choice_keyboard(
                feeds,
                kb.FEED_PREFIX,
                id_of=lambda f: f.id,
                label_of=lambda f: f"{f.title} — {f.author}",
                has_prev=session.feed_page > 1,
                has_next=has_next,
            ),
        )
        return State.CHOOSE_PODCAST

    @guard(State.CHOOSE_PODCAST)
    async def handle_podcast_choice(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        data = await self._ack(update)
        session = get_session(context.user_data)
        prefix, value = kb.parse_callback(data)

        if data == kb.NAV_CANCEL:
            return await self.cancel(update, context)

        if data in (kb.NAV_NEXT, kb.NAV_PREV):
            if not session.query:
                # Trending has no pages; nothing sensible to turn to.
                await self.respond(update, "That list has only one page.")
                return State.CHOOSE_PODCAST
            session.feed_page = max(
                1, session.feed_page + (1 if data == kb.NAV_NEXT else -1)
            )
            return await self._show_feeds(update, context)

        if prefix != kb.FEED_PREFIX:
            return await self._stale_button(update)

        feed: Feed | None = session.find_feed(value)
        if feed is None:
            return await self._stale_button(update)

        session.select_feed(feed)
        await self.respond(
            update, f"📻 {truncate(feed.title, 80)}\n\nLoading episodes…"
        )
        return await self._show_episodes(update, context, State.CHOOSE_EPISODE)

    # ------------------------------------------------------------------
    # Episodes
    # ------------------------------------------------------------------

    async def _show_episodes(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, state: State
    ) -> int:
        session = get_session(context.user_data)

        if not session.episodes:
            if session.feed is None:
                await self.respond(update, "Pick a podcast first.")
                return State.ASK_PODCAST_NAME
            session.set_episodes(await self.client.list_episodes(session.feed.id))

        return await self._render_episode_page(update, context, state)

    async def _render_episode_page(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, state: State
    ) -> int:
        session = get_session(context.user_data)
        window, has_prev, has_next = session.page_of(
            session.episodes, session.episode_page, self.settings.episodes_per_page
        )

        if not window:
            # Only reachable if the list shrank underneath us; rewind rather
            # than showing an empty keyboard.
            session.episode_page = 1
            window, has_prev, has_next = session.page_of(
                session.episodes, 1, self.settings.episodes_per_page
            )

        await self.respond(
            update,
            f"🎧 {len(session.episodes)} episodes (page {session.episode_page}) — "
            "pick one, or type part of a title:",
            reply_markup=kb.choice_keyboard(
                window,
                kb.EPISODE_PREFIX,
                id_of=lambda e: e.id,
                label_of=self._episode_label,
                has_prev=has_prev,
                has_next=has_next,
            ),
        )
        return state

    @staticmethod
    def _episode_label(episode: Episode) -> str:
        if episode.duration:
            return f"{episode.title} ({format_duration(episode.duration)})"
        return episode.title

    async def _handle_episode_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, state: State
    ) -> int:
        data = await self._ack(update)
        session = get_session(context.user_data)
        prefix, value = kb.parse_callback(data)

        if data == kb.NAV_CANCEL:
            return await self.cancel(update, context)

        if data in (kb.NAV_NEXT, kb.NAV_PREV):
            session.episode_page = max(
                1, session.episode_page + (1 if data == kb.NAV_NEXT else -1)
            )
            return await self._render_episode_page(update, context, state)

        if prefix != kb.EPISODE_PREFIX:
            return await self._stale_button(update)

        episode = session.find_episode(value)
        if episode is None:
            return await self._stale_button(update)

        session.episode = episode
        await self.respond(update, self._selected_text(episode))
        return State.ASK_INTERVAL

    async def _handle_episode_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, state: State
    ) -> int:
        session = get_session(context.user_data)
        needle = one_line(update.effective_message.text).lower()

        if not session.episodes:
            await self.respond(
                update, "I have no episode list to search. Send /start to begin again."
            )
            return ConversationHandler.END

        matches = [ep for ep in session.episodes if needle in ep.title.lower()]

        if not matches:
            await self.respond(
                update, "No episode title contains that. Try different words:"
            )
            return state

        if len(matches) == 1:
            session.episode = matches[0]
            await self.respond(update, self._selected_text(matches[0]))
            return State.ASK_INTERVAL

        shown = matches[: self.settings.episodes_per_page]
        note = (
            f"🔎 {len(matches)} matches"
            + (f", showing the first {len(shown)}" if len(matches) > len(shown) else "")
            + ":"
        )
        await self.respond(
            update,
            note,
            reply_markup=kb.choice_keyboard(
                shown,
                kb.EPISODE_PREFIX,
                id_of=lambda e: e.id,
                label_of=self._episode_label,
            ),
        )
        return state

    def _selected_text(self, episode: Episode) -> str:
        return (
            f"🎧 {truncate(one_line(episode.title), 120)}\n\n"
            f"{self._interval_prompt(episode)}"
        )

    def _interval_prompt(self, episode: Episode) -> str:
        length = (
            f" This episode is {format_duration(episode.duration)} long."
            if episode.duration
            else ""
        )
        # Sent without parse_mode: episode titles are interpolated nearby and
        # a stray * or _ in a feed's title would break Markdown parsing.
        return (
            f"⏱ Which part should I cut?{length}\n"
            f"Send a range like 01:20-02:00 "
            f"(max {format_duration(self.settings.max_cut_seconds)})."
        )

    # Thin wrappers so each state gets a handler that returns its own state.

    @guard(State.CHOOSE_EPISODE)
    async def handle_episode_choice(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        return await self._handle_episode_callback(
            update, context, State.CHOOSE_EPISODE
        )

    @guard(State.CHOOSE_EPISODE)
    async def handle_episode_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        return await self._handle_episode_text(update, context, State.CHOOSE_EPISODE)

    @guard(State.CHOOSE_GLOBAL_EPISODE)
    async def handle_global_episode_choice(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        return await self._handle_episode_callback(
            update, context, State.CHOOSE_GLOBAL_EPISODE
        )

    @guard(State.ASK_PERSON_QUERY, "Try another search term:")
    async def handle_person_query(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        session = get_session(context.user_data)
        session.query = one_line(update.effective_message.text)
        if not session.query:
            await self.respond(update, "Please send a name or a keyword.")
            return State.ASK_PERSON_QUERY

        session.set_episodes(
            await self.client.search_episodes_by_person(session.query)
        )
        return await self._render_global_page(update, context)

    async def _render_global_page(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        session = get_session(context.user_data)
        window, has_prev, has_next = session.page_of(
            session.episodes, session.episode_page, self.settings.episodes_per_page
        )

        await self.respond(
            update,
            f"🔎 {len(session.episodes)} episodes for “{truncate(session.query, 50)}” "
            f"(page {session.episode_page}):",
            reply_markup=kb.choice_keyboard(
                window,
                kb.EPISODE_PREFIX,
                id_of=lambda e: e.id,
                label_of=lambda e: button_label(e.feed_title, e.title),
                has_prev=has_prev,
                has_next=has_next,
            ),
        )
        return State.CHOOSE_GLOBAL_EPISODE

    # ------------------------------------------------------------------
    # Cutting
    # ------------------------------------------------------------------

    @guard(State.ASK_INTERVAL)
    async def handle_interval(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        session = get_session(context.user_data)
        episode = session.episode

        if episode is None:
            await self.respond(
                update, "I lost track of which episode that was. Let's start over."
            )
            return await self.start(update, context)

        raw = update.effective_message.text or ""
        # Raises IntervalError, which @guard turns into a message + retry.
        interval = parse_interval(raw, self.settings.max_cut_seconds)

        if episode.duration and interval.start >= episode.duration:
            await self.respond(
                update,
                f"⚠️ This episode is only {format_duration(episode.duration)} long. "
                "Pick an earlier start time:",
            )
            return State.ASK_INTERVAL

        user = update.effective_user
        user_id = user.id if user else 0
        if user_id in self._busy_users:
            await self.respond(
                update, "⏳ I'm still working on your previous cut — one at a time!"
            )
            return State.ASK_INTERVAL

        self._busy_users.add(user_id)
        try:
            await self._perform_cut(update, episode, interval, raw)
        finally:
            self._busy_users.discard(user_id)

        return ConversationHandler.END

    @staticmethod
    def _id3_tags(episode: Episode, interval: Interval) -> dict[str, str]:
        """Tags written onto the cut, replacing the source's.

        The source tags are dropped wholesale because feeds ship huge embedded
        artwork, so the clip needs its own or it arrives untitled.
        """
        span = f"{format_duration(interval.start)}–{format_duration(interval.end)}"
        return {
            "title": truncate(one_line(episode.title), 120) + f" [{span}]",
            "artist": truncate(one_line(episode.feed_title), 120),
            "album": truncate(one_line(episode.feed_title), 120),
            "comment": "Cut with @podcast_cutter_bot",
        }

    async def _perform_cut(
        self, update: Update, episode: Episode, interval: Interval, raw_interval: str
    ) -> None:
        message = update.effective_message
        queued = self._job_slots.locked()
        status = await message.reply_text(
            "⏳ Queued — waiting for a free slot…"
            if queued
            else "⏳ Working on it…"
        )

        async def set_status(text: str) -> None:
            with contextlib.suppress(TelegramError):
                await status.edit_text(text)

        async with self._job_slots:
            self.settings.work_dir.mkdir(parents=True, exist_ok=True)
            workdir = Path(
                tempfile.mkdtemp(prefix="cut-", dir=self.settings.work_dir)
            )
            try:
                result = await cut_episode(
                    episode.enclosure_url,
                    interval,
                    workdir,
                    self.settings,
                    on_status=set_status,
                    metadata=self._id3_tags(episode, interval),
                )

                await set_status("📤 Uploading…")
                filename = safe_filename(
                    episode.feed_title,
                    episode.title,
                    raw_interval.replace(":", "."),
                    ext=result.path.suffix,
                )

                with result.path.open("rb") as handle:
                    await message.reply_audio(
                        audio=handle,
                        filename=filename,
                        title=truncate(one_line(episode.title), 64),
                        performer=truncate(one_line(episode.feed_title), 64),
                        duration=interval.duration,
                        caption=(
                            f"✂️ {format_duration(interval.start)}"
                            f"–{format_duration(interval.end)}"
                        ),
                        read_timeout=self.settings.upload_timeout,
                        write_timeout=self.settings.upload_timeout,
                        connect_timeout=60,
                    )

                with contextlib.suppress(TelegramError):
                    await status.delete()

            except PodcastCutterError as exc:
                await set_status(f"❌ {exc.user_message}")
            except TelegramError as exc:
                logger.exception("Failed to deliver the cut: %s", exc)
                await set_status(
                    "❌ I cut the audio but Telegram refused the upload. "
                    "Try a shorter interval."
                )
            except Exception:
                logger.exception("Unexpected failure while cutting %s", episode.id)
                await set_status(GENERIC_ERROR)
            finally:
                # One directory per job, so nothing can survive a crash here.
                shutil.rmtree(workdir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Exits
    # ------------------------------------------------------------------

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        await self._ack(update)
        reset_session(context.user_data)

        if update.callback_query is not None:
            # Retire the inline keyboard, then re-offer the menu separately:
            # a reply keyboard cannot be attached to an edited message.
            await self.respond(update, "Okay, cancelled.")
            message = update.effective_message
            if message is not None:
                await message.reply_text("What next?", reply_markup=kb.main_menu())
        else:
            await update.effective_message.reply_text(
                "Okay, cancelled. What next?", reply_markup=kb.main_menu()
            )
        return ConversationHandler.END

    async def on_timeout(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        reset_session(context.user_data)
        message = update.effective_message
        if message is not None:
            with contextlib.suppress(TelegramError):
                await message.reply_text(
                    "⌛ That took a while, so I closed the session. "
                    "Pick something to start again.",
                    reply_markup=kb.main_menu(),
                )
        return ConversationHandler.END

    async def _stale_button(self, update: Update) -> int:
        """A button from a message that no longer matches the session."""
        await self.respond(
            update,
            "That button is out of date — send /start to begin again.",
        )
        return ConversationHandler.END

    async def unknown_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await update.effective_message.reply_text(
            "I don't know that command. Try /help.", reply_markup=kb.main_menu()
        )

    # ------------------------------------------------------------------
    # Global error handler
    # ------------------------------------------------------------------

    async def on_error(
        self, update: object, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        logger.exception(
            "Unhandled exception while processing an update", exc_info=context.error
        )

        if not isinstance(update, Update):
            return
        message = update.effective_message
        if message is None:
            return

        text = (
            f"⚠️ {context.error.user_message}"
            if isinstance(context.error, PodcastCutterError)
            else GENERIC_ERROR
        )
        with contextlib.suppress(TelegramError):
            await message.reply_text(text, reply_markup=kb.main_menu())
