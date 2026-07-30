"""Telegram handlers: one text router, one callback router, one inline handler.

There is deliberately no ``ConversationHandler``. A screen stack in
:class:`~podcast_cutter.states.Session` already says where the user is, and an
explicit ``awaiting`` field says what typing means. With a flat router every
update has exactly one destination, so a message can never land in a state that
has no handler — which is what used to strand users.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from telegram import (
    InlineQueryResultArticle,
    InlineQueryResultsButton,
    InputTextMessageContent,
    Message,
    Update,
)
from telegram.constants import ChatAction
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from . import keyboards as kb
from . import screens
from .api import Episode, PodcastIndexClient
from .audio import Interval, cut_episode, parse_moment_or_range
from .config import Settings
from .errors import PodcastCutterError
from .screens import View
from .states import (
    MAX_RECENTS,
    Awaiting,
    Screen,
    Session,
    get_session,
    reset_session,
)
from .store import Event, Store
from .text import (
    esc,
    format_duration,
    human_bytes,
    one_line,
    progress_bar,
    safe_filename,
    truncate,
)

logger = logging.getLogger(__name__)

#: Prefix used by deep links, e.g. ``t.me/bot?start=ep_12345``.
DEEP_LINK_EPISODE = "ep_"

#: Minimum gap between progress edits. Telegram throttles aggressive editing,
#: and a bar that moves more often than this is noise, not information.
PROGRESS_INTERVAL = 3.0

GENERIC_ERROR = "Something went wrong on my side. Please try again."

HELP_TEXT = (
    "🎙 <b>Podcast Cutter</b>\n\n"
    "Find an episode, tell me a moment, get just that part back.\n\n"
    "<b>Finding something</b>\n"
    "/search — a podcast by name\n"
    "/person — episodes mentioning someone\n"
    "/trending — what's popular\n"
    "/surprise — a random episode\n"
    "/recent — episodes you looked at\n"
    "/cancel — back to the main menu\n"
    "/help — this message\n\n"
    "<b>Picking the moment</b>\n"
    "Send <code>12:30</code> for a clip starting there, or "
    "<code>12:30-14:00</code> for an exact range.\n"
    "Then nudge it with the ◀ ▶ buttons until it's right.\n\n"
    "<b>Anywhere else</b>\n"
    "Type <code>@{username}</code> in any chat to share an episode "
    "without leaving the conversation."
)


class StatusEditor:
    """Owns the progress message and keeps edits sane.

    Telegram rejects an edit that would not change anything and rate-limits
    frequent ones, so identical text is dropped and updates are throttled.
    """

    def __init__(self, message: Message, min_interval: float = PROGRESS_INTERVAL):
        self._message = message
        self._min_interval = min_interval
        self._last_text: str | None = None
        self._last_edit = 0.0

    @property
    def message(self) -> Message:
        return self._message

    async def set(self, text: str, *, force: bool = False) -> None:
        if text == self._last_text:
            return
        now = time.monotonic()
        if not force and now - self._last_edit < self._min_interval:
            return

        self._last_text = text
        self._last_edit = now
        with contextlib.suppress(TelegramError):
            await self._message.edit_text(text, parse_mode="HTML")

    async def show(self, view: View) -> None:
        self._last_text = view.text
        self._last_edit = time.monotonic()
        with contextlib.suppress(TelegramError):
            await self._message.edit_text(
                view.text, parse_mode="HTML", reply_markup=view.keyboard
            )


class PodcastCutterBot:
    """Handler collection, holding shared clients and limits."""

    def __init__(
        self,
        settings: Settings,
        client: PodcastIndexClient,
        store: Store | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.store = store
        self.bot_username = ""
        self._job_slots = asyncio.Semaphore(settings.max_concurrent_jobs)
        #: User ids with a cut in flight — one heavy job per person.
        self._busy_users: set[int] = set()

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    async def show(self, update: Update, view: View) -> Message | None:
        """Render a view, editing the message in place where possible.

        Editing keeps the chat from filling with dead menus as the user
        navigates; a fresh message is sent when editing is impossible.
        """
        query = update.callback_query
        if query is not None and query.message is not None:
            try:
                return await query.edit_message_text(
                    view.text, parse_mode="HTML", reply_markup=view.keyboard
                )
            except BadRequest as exc:
                if "not modified" in str(exc).lower():
                    return query.message
                logger.debug("Could not edit, sending a new message: %s", exc)

        message = update.effective_message
        if message is None:
            logger.warning("Update %s has nowhere to reply", update.update_id)
            return None
        return await message.reply_text(
            view.text, parse_mode="HTML", reply_markup=view.keyboard
        )

    def view_for(self, session: Session) -> View:
        """Render whatever screen the session currently points at."""
        nav = session.current
        screen = nav.screen if nav else Screen.MENU

        if screen is Screen.ASK_PODCAST:
            return screens.ask_podcast()
        if screen is Screen.ASK_PERSON:
            return screens.ask_person()
        if screen is Screen.FEEDS:
            return screens.feeds(session, self.settings)
        if screen is Screen.EPISODES:
            return screens.episodes(session, self.settings)
        if screen is Screen.GLOBAL:
            return screens.global_episodes(session, self.settings)
        if screen is Screen.TRENDING:
            return screens.trending(session, self.settings)
        if screen is Screen.RECENT:
            return screens.recent(session, self.settings)
        if screen is Screen.INTERVAL:
            return screens.interval(session, self.settings)
        if screen is Screen.RESULT:
            return screens.result(session, self.bot_username)
        return screens.menu(session)

    async def render(self, update: Update, session: Session) -> Message | None:
        return await self.show(update, self.view_for(session))

    async def _to_menu(self, update: Update, session: Session) -> None:
        session.reset_navigation()
        session.go(Screen.MENU)
        await self.render(update, session)

    # ------------------------------------------------------------------
    # Session plumbing
    # ------------------------------------------------------------------

    async def session_for(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> Session:
        """Fetch the session, starting over if it has gone stale.

        Expiring on next use rather than on a timer means the user is never
        interrupted by an unprompted "your session ended" message.
        """
        session = get_session(context.user_data)
        if session.is_stale(self.settings.conversation_timeout):
            session = reset_session(context.user_data)
        session.touch()

        # The recent list is the one thing worth surviving a restart, so it
        # lives in the database and is pulled in once per session.
        if not session.recents_loaded:
            session.recents_loaded = True
            user_id = self._user_id(update)
            # Only ever fill an empty list: the database is the cold-start
            # source, and anything already in memory is newer than it.
            if self.store is not None and user_id is not None and not session.recents:
                session.recents = await self.store.recent_episodes(
                    user_id, MAX_RECENTS
                )
        return session

    @staticmethod
    def _user_id(update: Update) -> int | None:
        user = update.effective_user
        return user.id if user is not None else None

    async def _log(self, update: Update, action: str, **fields) -> None:
        """Append to the journal, if one is configured."""
        if self.store is None:
            return
        await self.store.record(
            Event(action=action, user_id=self._user_id(update), **fields)
        )

    async def _select_episode(
        self, update: Update, session: Session, episode: Episode
    ) -> None:
        """Open an episode and remember it for the Recent list."""
        session.select_episode(episode, self.settings.default_clip_seconds)
        user_id = self._user_id(update)
        if self.store is not None and user_id is not None:
            await self.store.remember_recent(user_id, episode)
            await self.store.trim_recents(user_id, MAX_RECENTS)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def cmd_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        session = reset_session(context.user_data)

        payload = context.args[0] if context.args else ""
        if payload.startswith(DEEP_LINK_EPISODE):
            await self._open_shared_episode(
                update, session, payload[len(DEEP_LINK_EPISODE) :]
            )
            return

        await update.effective_message.reply_text(
            "👋 Welcome!", reply_markup=kb.main_menu()
        )
        session.go(Screen.MENU)
        await self.render(update, session)
        await self._log(update, "start")

    async def _open_shared_episode(
        self, update: Update, session: Session, episode_id: str
    ) -> None:
        """Follow a ``t.me/bot?start=ep_…`` link straight to the clip editor."""
        try:
            episode = await self.client.get_episode(episode_id)
        except PodcastCutterError as exc:
            await update.effective_message.reply_text(
                f"⚠️ {esc(exc.user_message)}", parse_mode="HTML",
                reply_markup=kb.main_menu(),
            )
            session.go(Screen.MENU)
            await self.render(update, session)
            return

        await self._select_episode(update, session, episode)
        session.awaiting = Awaiting.INTERVAL
        session.go(Screen.INTERVAL)
        await update.effective_message.reply_text(
            "🔗 Opened from a shared link.", reply_markup=kb.main_menu()
        )
        await self.render(update, session)
        await self._log(update, "deep_link", episode_id=episode.id)

    def command(self, action: MenuAction):
        """Adapt a session action into a plain command handler.

        Errors surface as a message with a retry, never as a silent no-op.
        """

        async def handler(
            update: Update, context: ContextTypes.DEFAULT_TYPE
        ) -> None:
            session = await self.session_for(update, context)
            try:
                await action(update, session)
            except PodcastCutterError as exc:
                await self._show_failure(update, session, exc.user_message)

        handler.__name__ = getattr(action, "__name__", "command")
        return handler

    async def cmd_help(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE = None
    ) -> None:
        await update.effective_message.reply_text(
            HELP_TEXT.format(username=self.bot_username or "podcast_cutter_bot"),
            parse_mode="HTML",
            reply_markup=kb.main_menu(),
            link_preview_options={"is_disabled": True},
        )

    async def cmd_stats(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """The operator's panel. Invisible to everyone else.

        Unauthorised callers get the ordinary unknown-command reply rather
        than "you are not an admin", which would confirm the command exists.
        The caller's id is logged so the owner can discover their own.
        """
        user_id = self._user_id(update)
        if not self.settings.is_admin(user_id):
            if user_id is not None:
                logger.info(
                    "Ignoring /stats from user %s. Set ADMIN_IDS=%s to allow it.",
                    user_id,
                    user_id,
                )
            await self.cmd_unknown(update, context)
            return

        if self.store is None:
            await update.effective_message.reply_text("No journal configured.")
            return

        view = screens.stats(
            await self.store.stats(24),
            await self.store.stats(24 * 7),
            await self.store.size_on_disk(),
        )
        await update.effective_message.reply_text(view.text, parse_mode="HTML")

    async def cmd_unknown(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await update.effective_message.reply_text(
            "I don't know that command — /help lists the ones I do.",
            reply_markup=kb.main_menu(),
        )

    async def cmd_cancel(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        session = reset_session(context.user_data)
        session.go(Screen.MENU)
        await self.render(update, session)

    # ------------------------------------------------------------------
    # Actions reachable from both the menu and commands
    # ------------------------------------------------------------------

    async def act_ask_podcast(self, update: Update, session: Session) -> None:
        session.awaiting = Awaiting.PODCAST_NAME
        session.go(Screen.ASK_PODCAST)
        await self.render(update, session)

    async def act_ask_person(self, update: Update, session: Session) -> None:
        session.awaiting = Awaiting.PERSON
        session.go(Screen.ASK_PERSON)
        await self.render(update, session)

    async def act_trending(self, update: Update, session: Session) -> None:
        await self.show(update, screens.working("🔥 Fetching trending…"))
        feeds = await self.client.trending_feeds(20)
        session.remember_feeds(feeds)
        session.awaiting = Awaiting.NOTHING
        session.go(Screen.TRENDING)
        await self.render(update, session)
        await self._log(update, "trending")

    async def act_surprise(self, update: Update, session: Session) -> None:
        await self.show(update, screens.working("🎲 Picking an episode…"))
        episode = await self.client.random_episode()
        await self._select_episode(update, session, episode)
        session.awaiting = Awaiting.INTERVAL
        session.go(Screen.INTERVAL)
        await self.render(update, session)
        await self._log(update, "surprise", episode_id=episode.id)

    async def act_recent(self, update: Update, session: Session) -> None:
        session.awaiting = Awaiting.NOTHING
        session.go(Screen.RECENT)
        await self.render(update, session)

    async def act_menu(self, update: Update, session: Session) -> None:
        session.awaiting = Awaiting.NOTHING
        await self._to_menu(update, session)

    # ------------------------------------------------------------------
    # Searching
    # ------------------------------------------------------------------

    async def _search_feeds(
        self, update: Update, session: Session, query: str, page: int = 1
    ) -> None:
        session.query = query
        await self.show(update, screens.working(f"🔍 Searching “{esc(query)}”…"))

        feeds, has_next = await self.client.search_feeds(query, page)
        session.remember_feeds(feeds, has_next)
        session.awaiting = Awaiting.NOTHING

        if len(feeds) == 1 and page == 1:
            # A single hit needs no disambiguation step.
            session.select_feed(feeds[0])
            await self._load_episodes(update, session)
            return

        if session.current and session.current.screen is Screen.FEEDS:
            session.replace(Screen.FEEDS, page)
        else:
            session.go(Screen.FEEDS, page)
        await self.render(update, session)
        if page == 1:
            await self._log(update, "search", detail=query)

    async def _search_people(
        self, update: Update, session: Session, query: str
    ) -> None:
        session.query = query
        await self.show(update, screens.working(f"🔎 Searching “{esc(query)}”…"))

        session.set_episodes(await self.client.search_episodes_by_person(query))
        session.awaiting = Awaiting.NOTHING
        session.go(Screen.GLOBAL)
        await self.render(update, session)
        await self._log(update, "person", detail=query)

    async def _load_episodes(self, update: Update, session: Session) -> None:
        if session.feed is None:
            await self._to_menu(update, session)
            return

        if not session.episodes:
            await self.show(update, screens.working("🎧 Loading episodes…"))
            session.set_episodes(await self.client.list_episodes(session.feed.id))

        session.awaiting = Awaiting.NOTHING
        session.go(Screen.EPISODES)
        await self.render(update, session)

    async def _open_episode(
        self, update: Update, session: Session, episode: Episode
    ) -> None:
        await self._select_episode(update, session, episode)
        session.awaiting = Awaiting.INTERVAL
        session.go(Screen.INTERVAL)
        await self.render(update, session)

    # ------------------------------------------------------------------
    # Text router
    # ------------------------------------------------------------------

    async def on_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        session = await self.session_for(update, context)
        text = one_line(update.effective_message.text)

        if not text:
            return

        try:
            if text in kb.MENU_BUTTONS:
                await self._handle_menu_button(update, session, text)
                return

            if session.awaiting is Awaiting.INTERVAL:
                await self._handle_interval_text(update, session, text)
                return

            if session.awaiting is Awaiting.PERSON:
                await self._search_people(update, session, text)
                return

            screen = session.current.screen if session.current else Screen.MENU
            if screen in (Screen.EPISODES, Screen.GLOBAL, Screen.RECENT):
                # On a list, typing filters it rather than starting over.
                session.episode_filter = text
                session.replace(screen, 1)
                await self.render(update, session)
                return

            # Everything else is a podcast search — including a bare message to
            # a bot the user has never configured, which is the common case.
            await self._search_feeds(update, session, text)
        except PodcastCutterError as exc:
            await self._show_failure(update, session, exc.user_message)

    async def _handle_menu_button(
        self, update: Update, session: Session, label: str
    ) -> None:
        if label == kb.BTN_SEARCH_PODCAST:
            await self.act_ask_podcast(update, session)
        elif label == kb.BTN_SEARCH_PERSON:
            await self.act_ask_person(update, session)
        elif label == kb.BTN_TRENDING:
            await self.act_trending(update, session)
        elif label == kb.BTN_SURPRISE:
            await self.act_surprise(update, session)
        elif label == kb.BTN_RECENT:
            await self.act_recent(update, session)
        elif label == kb.BTN_HELP:
            await self.cmd_help(update, None)

    async def _handle_interval_text(
        self, update: Update, session: Session, text: str
    ) -> None:
        interval = parse_moment_or_range(
            text, self.settings.max_cut_seconds, session.clip_length
        )
        session.set_clip(interval.start, interval.duration)
        session.clamp()
        session.replace(Screen.INTERVAL)
        await self.render(update, session)

    # ------------------------------------------------------------------
    # Callback router
    # ------------------------------------------------------------------

    async def on_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        data = query.data or ""

        # Acknowledge immediately; the client shows a spinner until we do.
        with contextlib.suppress(TelegramError):
            await query.answer()

        if data == kb.NAV_NOOP:
            return

        session = await self.session_for(update, context)
        prefix, value = kb.parse_callback(data)

        try:
            await self._route_callback(update, session, data, prefix, value)
        except PodcastCutterError as exc:
            await self._show_failure(update, session, exc.user_message)

    async def _route_callback(
        self, update: Update, session: Session, data: str, prefix: str, value: str
    ) -> None:
        # --- navigation ---------------------------------------------------
        if data in (kb.NAV_MENU, kb.NAV_CANCEL):
            await self.act_menu(update, session)
            return

        if data == kb.NAV_BACK:
            if session.back() is None:
                await self._to_menu(update, session)
            else:
                session.awaiting = (
                    Awaiting.INTERVAL
                    if session.current and session.current.screen is Screen.INTERVAL
                    else Awaiting.NOTHING
                )
                await self.render(update, session)
            return

        if prefix == "menu":
            await self._route_menu(update, session, value)
            return

        if prefix == kb.PAGE_PREFIX and session.current:
            with contextlib.suppress(ValueError):
                # Paging replaces rather than pushes, so Back leaves the list
                # instead of walking back through every page.
                session.replace(session.current.screen, int(value))
            await self.render(update, session)
            return

        # --- selection ----------------------------------------------------
        if prefix == kb.FEED_PREFIX:
            feed = session.find_feed(value)
            if feed is None:
                await self._stale(update, session)
                return
            session.select_feed(feed)
            await self._load_episodes(update, session)
            return

        if prefix == kb.EPISODE_PREFIX:
            episode = session.find_episode(value)
            if episode is None:
                await self._stale(update, session)
                return
            await self._open_episode(update, session, episode)
            return

        if data == kb.ACTION_CLEAR_FILTER:
            session.episode_filter = ""
            if session.current:
                session.replace(session.current.screen, 1)
            await self.render(update, session)
            return

        # --- clip editing -------------------------------------------------
        if prefix == kb.LENGTH_PREFIX:
            with contextlib.suppress(ValueError):
                session.set_length(int(value), self.settings.max_cut_seconds)
            await self.render(update, session)
            return

        if prefix == kb.MOVE_PREFIX:
            with contextlib.suppress(ValueError):
                session.move_clip(int(value))
            await self.render(update, session)
            return

        if data == kb.ACTION_TOGGLE_VOICE:
            session.as_voice = not session.as_voice
            await self.render(update, session)
            return

        # --- cutting ------------------------------------------------------
        if data in (kb.ACTION_CUT, kb.ACTION_RETRY):
            await self._start_cut(update, session)
            return

        if prefix == kb.SHIFT_PREFIX:
            with contextlib.suppress(ValueError):
                session.move_clip(int(value))
            await self._start_cut(update, session)
            return

        if data == kb.ACTION_NEW_CLIP:
            if session.episode is None:
                await self._stale(update, session)
                return
            session.awaiting = Awaiting.INTERVAL
            session.go(Screen.INTERVAL)
            await self.render(update, session)
            return

        await self._stale(update, session)

    async def _route_menu(
        self, update: Update, session: Session, action: str
    ) -> None:
        actions = {
            "search": self.act_ask_podcast,
            "person": self.act_ask_person,
            "trending": self.act_trending,
            "surprise": self.act_surprise,
            "recent": self.act_recent,
        }
        handler = actions.get(action)
        if handler is not None:
            await handler(update, session)
        elif action == "help":
            await self.cmd_help(update, None)
        else:
            await self._to_menu(update, session)

    async def _stale(self, update: Update, session: Session) -> None:
        """A button whose message no longer matches the session."""
        session.reset_navigation()
        session.go(Screen.MENU)
        view = self.view_for(session)
        await self.show(
            update,
            View("⌛ That menu is out of date — here's a fresh start.\n\n" + view.text,
                 view.keyboard),
        )

    async def _show_failure(
        self, update: Update, session: Session, message: str
    ) -> None:
        await self.show(update, screens.failure(message))

    # ------------------------------------------------------------------
    # Cutting
    # ------------------------------------------------------------------

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

    async def _start_cut(self, update: Update, session: Session) -> None:
        episode = session.episode
        if episode is None:
            await self._stale(update, session)
            return

        user = update.effective_user
        user_id = user.id if user else 0
        if user_id in self._busy_users:
            await self.show(
                update,
                View(
                    "⏳ Still working on your previous clip — one at a time!",
                    self.view_for(session).keyboard,
                ),
            )
            return

        session.clamp()
        interval = Interval(start=session.clip_start, end=session.clip_end)

        queued = self._job_slots.locked()
        status_message = await self.show(
            update,
            screens.working(
                "⏳ Queued — waiting for a free slot…"
                if queued
                else "⏳ Working on it…"
            ),
        )
        if status_message is None:
            return

        self._busy_users.add(user_id)
        try:
            await self._perform_cut(
                update, session, episode, interval, StatusEditor(status_message)
            )
        finally:
            self._busy_users.discard(user_id)

    async def _perform_cut(
        self,
        update: Update,
        session: Session,
        episode: Episode,
        interval: Interval,
        status: StatusEditor,
    ) -> None:
        message = update.effective_message
        chat = update.effective_chat

        async def on_status(text: str) -> None:
            await status.set(text, force=True)

        async def on_progress(done: int, total: int | None) -> None:
            await status.set(
                f"⬇️ Downloading the episode…\n\n{progress_bar(done, total)}"
            )

        started = time.monotonic()
        outcome = "failed"
        size_bytes: int | None = None
        detail: str | None = None

        async with self._job_slots:
            workdir = Path(
                tempfile.mkdtemp(prefix="cut-", dir=self._ensure_work_dir())
            )
            try:
                result = await cut_episode(
                    episode.enclosure_url,
                    interval,
                    workdir,
                    self.settings,
                    on_status=on_status,
                    on_progress=on_progress,
                    metadata=self._id3_tags(episode, interval),
                    voice=session.as_voice,
                )

                await status.set(
                    f"📤 Uploading {human_bytes(result.size)}…", force=True
                )
                if chat is not None:
                    with contextlib.suppress(TelegramError):
                        await chat.send_action(
                            ChatAction.UPLOAD_VOICE
                            if session.as_voice
                            else ChatAction.UPLOAD_DOCUMENT
                        )

                await self._deliver(
                    message, session, episode, interval, result
                )

                session.replace(Screen.RESULT)
                await status.show(screens.result(session, self.bot_username))
                outcome, size_bytes = "ok", result.size

            except PodcastCutterError as exc:
                # The stable code, not the class name: grouping failures in SQL
                # keeps working across refactors.
                outcome = exc.code
                detail = exc.user_message
                await status.show(screens.failure(exc.user_message))
            except TelegramError as exc:
                logger.exception("Failed to deliver the cut: %s", exc)
                outcome, detail = "upload_rejected", str(exc)
                await status.show(
                    screens.failure(
                        "I cut the audio but Telegram refused the upload. "
                        "Try a shorter clip."
                    )
                )
            except Exception as exc:
                logger.exception("Unexpected failure while cutting %s", episode.id)
                outcome, detail = "crash", f"{type(exc).__name__}: {exc}"
                await status.show(screens.failure(GENERIC_ERROR))
            finally:
                # One directory per job, so nothing can survive a crash here.
                shutil.rmtree(workdir, ignore_errors=True)
                await self._log(
                    update,
                    "cut",
                    outcome=outcome,
                    episode_id=episode.id,
                    feed_title=episode.feed_title,
                    episode_title=episode.title,
                    start_s=interval.start,
                    length_s=interval.duration,
                    as_voice=session.as_voice,
                    size_bytes=size_bytes,
                    ms=int((time.monotonic() - started) * 1000),
                    detail=detail,
                )

    def _ensure_work_dir(self) -> Path:
        self.settings.work_dir.mkdir(parents=True, exist_ok=True)
        return self.settings.work_dir

    async def _deliver(
        self,
        message: Message,
        session: Session,
        episode: Episode,
        interval: Interval,
        result,
    ) -> None:
        caption = (
            f"<b>{esc(truncate(episode.title, 90))}</b>\n"
            f"{esc(truncate(episode.feed_title, 60))} · "
            f"{format_duration(interval.start)}–{format_duration(interval.end)}"
        )
        timeouts = {
            "read_timeout": self.settings.upload_timeout,
            "write_timeout": self.settings.upload_timeout,
            "connect_timeout": 60,
        }

        with result.path.open("rb") as handle:
            if session.as_voice:
                await message.reply_voice(
                    voice=handle,
                    duration=interval.duration,
                    caption=caption,
                    parse_mode="HTML",
                    **timeouts,
                )
            else:
                await message.reply_audio(
                    audio=handle,
                    filename=safe_filename(
                        episode.feed_title,
                        episode.title,
                        f"{format_duration(interval.start)}"
                        f"-{format_duration(interval.end)}".replace(":", "."),
                        ext=result.path.suffix,
                    ),
                    title=truncate(one_line(episode.title), 64),
                    performer=truncate(one_line(episode.feed_title), 64),
                    duration=interval.duration,
                    caption=caption,
                    parse_mode="HTML",
                    **timeouts,
                )

    # ------------------------------------------------------------------
    # Inline mode
    # ------------------------------------------------------------------

    async def on_inline_query(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Answer ``@bot some words`` typed in any chat.

        Each result posts the episode plus a deep link, so the recipient can
        open the clip editor without searching for it themselves.
        """
        inline = update.inline_query
        query = one_line(inline.query)

        open_button = InlineQueryResultsButton(
            text="Open Podcast Cutter", start_parameter="menu"
        )

        if len(query) < 2:
            await inline.answer(
                [], cache_time=300, is_personal=False, button=open_button
            )
            return

        try:
            episodes = await self.client.search_episodes_by_person(query)
        except PodcastCutterError:
            await inline.answer([], cache_time=30, button=open_button)
            return

        results = [
            self._inline_result(episode) for episode in episodes[:25]
        ]
        with contextlib.suppress(TelegramError):
            await inline.answer(results, cache_time=300, button=open_button)
        await self._log(update, "inline", detail=query, size_bytes=len(results))

    def _inline_result(self, episode: Episode) -> InlineQueryResultArticle:
        link = self.episode_link(episode.id)
        length = (
            f" · {format_duration(episode.duration)}" if episode.duration else ""
        )
        return InlineQueryResultArticle(
            id=episode.id,
            title=truncate(episode.title, 60),
            description=f"{truncate(episode.feed_title, 40)}{length}",
            input_message_content=InputTextMessageContent(
                f"🎧 <b>{esc(truncate(episode.title, 120))}</b>\n"
                f"{esc(truncate(episode.feed_title, 60))}{length}\n\n"
                f'<a href="{link}">✂️ Cut a clip from this</a>',
                parse_mode="HTML",
                link_preview_options={"is_disabled": True},
            ),
        )

    def episode_link(self, episode_id: str) -> str:
        username = self.bot_username or "podcast_cutter_bot"
        return f"https://t.me/{username}?start={DEEP_LINK_EPISODE}{episode_id}"

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
            context.error.user_message
            if isinstance(context.error, PodcastCutterError)
            else GENERIC_ERROR
        )
        with contextlib.suppress(TelegramError):
            await message.reply_text(
                f"⚠️ {esc(text)}", parse_mode="HTML", reply_markup=kb.main_menu()
            )


#: An action that operates on the current session, e.g. :meth:`act_trending`.
MenuAction = Callable[[Update, Session], Awaitable[None]]
