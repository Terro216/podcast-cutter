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
import html
import logging
import re
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path

from telegram import (
    Bot,
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
from . import screens, video
from .api import Episode, PodcastIndexClient
from .audio import Interval, cut_episode, parse_moment_or_range
from .config import Settings
from .errors import NotFoundError, PodcastCutterError, TooLargeError
from .i18n import (
    DEFAULT_LANGUAGE,
    LANGUAGES,
    ordinal,
    plural,
    resolve_language,
    t,
    t_seq,
)
from .indexer import Indexer, TranscriptionDisabled
from .limits import Budget
from .listening import QUEUED, TranscriptionQueue
from .proxy import PROXY, MediaProxy
from .screens import View
from .states import (
    FORMAT_NOTE,
    FORMAT_VIDEO,
    FORMATS,
    MAX_RECENTS,
    Awaiting,
    Screen,
    Session,
    get_session,
    peek_session,
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

#: Prefix that tags where a newcomer came from, e.g.
#: ``t.me/bot?start=src_reddit``. Recorded against the ``start`` event, so
#: ``/stats`` can say which channel actually brought people.
DEEP_LINK_SOURCE = "src_"

#: Campaign tags are truncated to this; the journal is not a landfill.
MAX_SOURCE_TAG = 32

#: Telegram accepts at most 50 inline results; fewer keeps the list scannable.
INLINE_RESULT_LIMIT = 25

#: How long Telegram may reuse an inline answer that found something.
INLINE_CACHE_SECONDS = 300

#: …and one that found nothing. Kept short on purpose: an inline query is
#: usually a prefix of a word still being typed, and caching "nothing" for five
#: minutes keeps answering "nothing" long after the user finished typing it.
INLINE_EMPTY_CACHE_SECONDS = 5

#: Minimum gap between progress edits. Telegram throttles aggressive editing,
#: and a bar that moves more often than this is noise, not information.
PROGRESS_INTERVAL = 3.0

#: Progress-stage → i18n key. The bar and the estimate carry the detail;
#: the waiting notes live in the i18n tables next to every other sentence.
_STAGE_KEYS = {
    "download": "stage_download",
    "decode": "stage_decode",
    "transcribe": "stage_transcribe",
    "index": "stage_index",
}

#: How long a first transcription takes per second of audio, before this
#: deployment has measured its own. Measured on the production host with
#: `base` on eight cores; the store replaces it with the median of real runs
#: as soon as there is one.
DEFAULT_RTF = 0.09

#: How many *episodes* may be in the listening queue at once — one being
#: listened to plus three in line. Beyond that the wait is tens of minutes, and
#: an honest refusal beats a queue position that means "tonight". Counted in
#: episodes rather than in people because that is what the wait is made of:
#: the tenth person to want an episode already queued adds no work at all.
MAX_ASR_QUEUE = 4

#: How long each waiting note stays on screen.
NOTE_SECONDS = 20

#: What typing means on each screen ``‹ Back`` can restore. Restoring the
#: screen without restoring this is how a phrase typed into a restored
#: "what was said?" prompt used to run off as a podcast-name search.
_SCREEN_AWAITING = {
    Screen.INTERVAL: Awaiting.INTERVAL,
    Screen.ASK_PODCAST: Awaiting.PODCAST_NAME,
    Screen.ASK_PERSON: Awaiting.PERSON,
    Screen.ASK_PHRASE: Awaiting.PHRASE,
}


def campaign_source(payload: str) -> str | None:
    """The tag in a ``t.me/bot?start=src_…`` link, or ``None``.

    Telegram already restricts a start payload to ``A-Za-z0-9_-``, but the
    value ends up in the journal and in ``/stats``, so it is normalised and
    bounded here rather than trusted to arrive clean.
    """
    if not payload.startswith(DEEP_LINK_SOURCE):
        return None
    tag = re.sub(r"[^a-z0-9_-]", "", payload[len(DEEP_LINK_SOURCE) :].lower())
    return tag[:MAX_SOURCE_TAG] or None


class StatusEditor:
    """Owns the progress message and keeps edits sane.

    Telegram rejects an edit that would not change anything and rate-limits
    frequent ones, so identical text is dropped and updates are throttled.
    """

    def __init__(self, message: Message, min_interval: float = PROGRESS_INTERVAL):
        self._message = message
        self._min_interval = min_interval
        self._last_text: str | None = None
        # -inf, not 0.0: monotonic() counts from boot, so on a freshly
        # started host 0.0 is "recently" and the first edit gets throttled.
        self._last_edit = float("-inf")

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


def _listening_text(
    stage, started: float, estimate: int, lang: str = DEFAULT_LANGUAGE
) -> str:
    """What the waiting screen says right now.

    Three things, in the order they answer "has this hung?": which stage, how
    far through it, and how much longer. The remaining time is derived from the
    work actually done so far rather than from the opening estimate, so it
    stops being a promise the moment reality disagrees with it.
    """
    if stage.stage == QUEUED:
        place = int(stage.done)
        ahead = place - 1
        return t(
            lang, "queue_position",
            place=ordinal(lang, place),
            episodes=f"{ahead} {plural(lang, 'episodes', ahead)}",
        )

    stage_key = _STAGE_KEYS.get(stage.stage, "stage_working")
    lines = [t(lang, stage_key)]

    fraction = stage.fraction
    if fraction is not None:
        lines.append("")
        # Each stage counts a different thing: bytes down, seconds of audio
        # heard, windows embedded. The bar has to say which, or forty minutes
        # of episode reads as «2.4 KB».
        label = {
            "transcribe": format_duration,
            "index": lambda count: str(int(count)),
        }.get(stage.stage, lambda count: human_bytes(count, lang))
        lines.append(
            progress_bar(int(stage.done), int(stage.total), label=label, lang=lang)
        )

    if stage.stage == "transcribe":
        elapsed = time.monotonic() - started
        remaining = None
        # Only once enough is done for the rate to mean anything: extrapolating
        # from the first few seconds produces a number that swings wildly and
        # is worse than saying nothing.
        if fraction and fraction > 0.05:
            remaining = int(elapsed / fraction - elapsed)
        elif estimate >= 30:
            remaining = max(0, estimate - int(elapsed))

        if remaining and remaining >= 10:
            lines.append(
                f"<i>{t(lang, 'about_left', duration=format_duration(remaining))}</i>"
            )

        # Rotated on the clock, not on how often this happens to be called:
        # edits are throttled, so counting calls would change the line without
        # anyone seeing it.
        notes = t_seq(lang, "waiting_notes")
        note = notes[int(elapsed // NOTE_SECONDS) % len(notes)]
        lines.append("")
        lines.append(f"<i>{note}</i>")

    return "\n".join(lines)


class PodcastCutterBot:
    """Handler collection, holding shared clients and limits."""

    def __init__(
        self,
        settings: Settings,
        client: PodcastIndexClient,
        store: Store | None = None,
        indexer: Indexer | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.store = store
        #: Absent when transcription is switched off or unavailable, in which
        #: case the bot behaves exactly as it did before searching existed.
        self.indexer = indexer
        self.bot_username = ""
        #: Set at startup. The queue needs a way to reach a chat whose session
        #: is gone — a job that outlived the request has nothing else to
        #: answer through.
        self.telegram: Bot | None = None
        #: Shared by every cut: the breaker state is the point, so one cut
        #: discovering a dead proxy spares the rest the same wait.
        self.media_proxy = MediaProxy(settings)
        self._job_slots = asyncio.Semaphore(settings.max_concurrent_jobs)
        #: Transcription has its own line, apart from the cut pool: sharing one
        #: semaphore let a single first-search occupy a cut slot for minutes,
        #: and the recogniser serialises on its own lock anyway, so more than
        #: one at a time would only hide the queue. It is in SQLite rather than
        #: in memory because a redeploy used to throw away everything waiting.
        self.listening: TranscriptionQueue | None = (
            TranscriptionQueue(settings, store, indexer, notify=self.notify_listened)
            if store is not None and indexer is not None
            else None
        )
        #: User ids with a cut in flight — one heavy job per person. Checked
        #: and set with no await in between, or two quick taps both pass.
        self._busy_users: set[int] = set()
        #: The task actually performing each user's cut, so /cancel can
        #: reach in and stop it — cancelling kills the ffmpeg underneath
        #: (see ``audio._run``) instead of letting an unwanted clip finish.
        self._cut_tasks: dict[int, asyncio.Task] = {}
        self._input_budget = Budget(settings.rate_input_per_minute, 60)
        self._cut_budget = Budget(settings.rate_cuts_per_hour, 3600)
        self._asr_budget = Budget(settings.rate_asr_per_day, 86400)
        #: Refusals are throttled separately: a flood of over-budget messages
        #: must not become a flood of scolding replies.
        self._limit_notice = Budget(1, 30)

    def _budget_allows(self, budget: Budget, update: Update) -> bool:
        """Charge one event, unless the user is an admin — admins are the
        people debugging the bot, and a debugging session looks exactly like
        abuse."""
        user = update.effective_user
        user_id = user.id if user else 0
        if self.settings.is_admin(user_id):
            return True
        return budget.allow(user_id)

    async def _refuse_rate(self, update: Update, kind: str, text: str) -> None:
        await self._log(update, "limit", outcome=kind)
        user = update.effective_user
        if self._limit_notice.allow(user.id if user else 0):
            with contextlib.suppress(TelegramError):
                await update.effective_message.reply_text(text)

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
            return screens.ask_podcast(session)
        if screen is Screen.ASK_PERSON:
            return screens.ask_person(session)
        if screen is Screen.LANGUAGE:
            return screens.language(session)
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
        if screen is Screen.ASK_PHRASE:
            return screens.ask_phrase(session, session.episode_transcribed)
        if screen is Screen.MOMENTS:
            return screens.moments(session)
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
            session.was_reset = True
        session.touch()
        await self._ensure_language(update, session)

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

    async def _language_for_user(self, user) -> str:
        """The language for a user outside any session — inline mode, resumed
        jobs. The stored choice wins, then the client's ``language_code``."""
        stored = (
            await self.store.user_language(user.id)
            if self.store is not None and user is not None
            else None
        )
        return resolve_language(
            stored, getattr(user, "language_code", None) if user else None
        )

    async def _ensure_language(self, update: Update, session: Session) -> None:
        """Resolve the session's language once, on its first update."""
        if session.language_loaded:
            return
        session.language_loaded = True
        session.language = await self._language_for_user(update.effective_user)

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
        await self._ensure_language(update, session)

        payload = context.args[0] if context.args else ""
        if payload.startswith(DEEP_LINK_EPISODE):
            await self._open_shared_episode(
                update, session, payload[len(DEEP_LINK_EPISODE) :]
            )
            return

        await update.effective_message.reply_text(
            t(
                session.language, "welcome",
                username=self.bot_username or "podcast_cutter_bot",
            ),
            parse_mode="HTML",
            reply_markup=kb.main_menu(session.language),
        )
        session.go(Screen.MENU)
        await self.render(update, session)
        await self._log(update, "start", detail=campaign_source(payload))

    async def _open_shared_episode(
        self, update: Update, session: Session, episode_id: str
    ) -> None:
        """Follow a ``t.me/bot?start=ep_…`` link straight to the clip editor."""
        try:
            episode = await self.client.get_episode(episode_id)
        except PodcastCutterError as exc:
            await update.effective_message.reply_text(
                f"⚠️ {esc(exc.user_message(session.language))}",
                parse_mode="HTML",
                reply_markup=kb.main_menu(session.language),
            )
            session.go(Screen.MENU)
            await self.render(update, session)
            return

        await self._select_episode(update, session, episode)
        session.awaiting = Awaiting.INTERVAL
        session.go(Screen.INTERVAL)
        await update.effective_message.reply_text(
            t(session.language, "opened_from_link"),
            reply_markup=kb.main_menu(session.language),
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
                await self._show_failure(
                    update, session, exc.user_message(session.language)
                )

        handler.__name__ = getattr(action, "__name__", "command")
        return handler

    async def act_help(self, update: Update, session: Session) -> None:
        await update.effective_message.reply_text(
            t(
                session.language, "help",
                username=self.bot_username or "podcast_cutter_bot",
            ),
            parse_mode="HTML",
            reply_markup=kb.main_menu(session.language),
            link_preview_options={"is_disabled": True},
        )

    async def act_language(self, update: Update, session: Session) -> None:
        session.awaiting = Awaiting.NOTHING
        session.go(Screen.LANGUAGE)
        await self.render(update, session)

    async def cmd_stats(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """The operator's panel. Invisible to everyone else.

        Unauthorised callers get the ordinary unknown-command reply rather
        than "you are not an admin", which would confirm the command exists.
        The caller's id is logged so the owner can discover their own.
        """
        session = await self.session_for(update, context)
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
            await update.effective_message.reply_text(
                t(session.language, "no_journal")
            )
            return

        view = screens.stats(
            await self.store.stats(24),
            await self.store.stats(24 * 7),
            await self.store.size_on_disk(),
            lang=session.language,
        )
        await update.effective_message.reply_text(view.text, parse_mode="HTML")

    async def cmd_unknown(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        session = await self.session_for(update, context)
        await update.effective_message.reply_text(
            t(session.language, "unknown_command"),
            reply_markup=kb.main_menu(session.language),
        )

    async def cmd_cancel(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        # A cut in flight is the one thing "cancel" can actually abort —
        # everything else it merely navigates away from.
        user_id = self._user_id(update)
        task = self._cut_tasks.get(user_id) if user_id is not None else None
        if task is not None:
            task.cancel()

        session = reset_session(context.user_data)
        await self._ensure_language(update, session)
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
        await self.show(
            update, screens.working(t(session.language, "working_trending"))
        )
        feeds = await self.client.trending_feeds(20)
        session.remember_feeds(feeds)
        session.awaiting = Awaiting.NOTHING
        session.go(Screen.TRENDING)
        await self.render(update, session)
        await self._log(update, "trending")

    async def act_surprise(self, update: Update, session: Session) -> None:
        await self.show(
            update, screens.working(t(session.language, "working_surprise"))
        )
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
        await self.show(
            update,
            screens.working(
                t(session.language, "working_search", query=esc(query))
            ),
        )

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
        await self.show(
            update,
            screens.working(
                t(session.language, "working_person", query=esc(query))
            ),
        )

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
            await self.show(
                update, screens.working(t(session.language, "working_episodes"))
            )
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
    # Searching inside an episode
    # ------------------------------------------------------------------

    async def act_ask_phrase(self, update: Update, session: Session) -> None:
        """Open the search screen, having found out what it may promise."""
        if self.indexer is None:
            raise TranscriptionDisabled
        if session.episode is None:
            await self._stale(update, session)
            return

        transcript = await self.indexer.store.transcript_for_episode(
            session.episode.id
        )
        session.episode_transcribed = transcript is not None
        session.awaiting = Awaiting.PHRASE
        session.go(Screen.ASK_PHRASE)
        await self.render(update, session)

    async def _search_phrase(
        self, update: Update, session: Session, phrase: str
    ) -> None:
        """Transcribe if necessary, then answer.

        The first search on an episode is minutes of work, so it runs under the
        same one-heavy-job-per-user rule as cutting, reports progress, and — if
        someone else is already listening to this episode — joins their job
        rather than starting a second one.
        """
        if self.indexer is None or session.episode is None:
            await self._stale(update, session)
            return

        session.phrase = phrase
        user_id = update.effective_user.id if update.effective_user else 0
        episode = session.episode

        if user_id in self._busy_users:
            await self._show_failure(
                update, session, t(session.language, "busy_running")
            )
            return

        # Whether this search is milliseconds or minutes decides everything
        # below: only a first-time transcription is charged against the daily
        # budget, queues for the single listening slot, or can be refused
        # because that queue is full.
        indexed = (
            await self.store.transcript_for_episode(episode.id)
            if self.store
            else None
        )
        if indexed is None:
            if not self._budget_allows(self._asr_budget, update):
                await self._log(update, "limit", outcome="asr")
                await self._show_failure(
                    update, session, t(session.language, "asr_budget_spent")
                )
                return
            # Joining a line that already holds this episode is free: it is one
            # transcription either way, and refusing the tenth person to ask
            # about a popular episode would be refusing them nothing.
            if (
                self.listening is not None
                and not await self.listening.position(episode.id)
                and await self.listening.depth() >= MAX_ASR_QUEUE
            ):
                await self._log(update, "limit", outcome="asr_queue")
                await self._show_failure(
                    update, session, t(session.language, "asr_queue_full")
                )
                return

        # Claimed before the first await, or two quick messages both pass the
        # busy check above and start two jobs.
        self._busy_users.add(user_id)
        try:
            await self._listen_and_search(update, session, episode, phrase, indexed)
        finally:
            self._busy_users.discard(user_id)

    async def _listen_and_search(
        self,
        update: Update,
        session: Session,
        episode: Episode,
        phrase: str,
        indexed: int | None,
    ) -> None:
        """The body of a phrase search, with the busy flag already held."""
        lang = session.language
        rtf = await self.store.measured_rtf() if self.store else None
        estimate = int((episode.duration or 0) * (rtf or DEFAULT_RTF))
        opening = t(lang, "getting_ready")
        if estimate >= 30:
            opening += (
                "\n\n<i>"
                + t(lang, "estimate_note", duration=format_duration(estimate))
                + "</i>"
            )
        progress = StatusEditor(
            await update.effective_message.reply_text(opening, parse_mode="HTML")
        )
        started = time.monotonic()
        shown_stage = {"name": ""}

        async def on_progress(stage) -> None:
            # Throttled within a stage — a bar redrawn faster than this is
            # noise — but a change of stage is information, and must not be
            # swallowed by the same limiter.
            changed = stage.stage != shown_stage["name"]
            shown_stage["name"] = stage.stage
            await progress.set(
                _listening_text(stage, started, estimate, lang), force=changed
            )


        # "failed" until proven otherwise: an exception this handler did not
        # anticipate still reaches the journal as a failure, not as a search
        # that worked. `_perform_cut` learned this first.
        outcome = "failed"
        try:
            if indexed is not None or self.listening is None:
                # Already searchable: milliseconds, and it must not stand in
                # line behind somebody else's half-hour first listen. (The
                # queue is also absent when there is no store to hold it, in
                # which case this is the only path there is.)
                transcript = await self.indexer.transcript_id(
                    episode.id,
                    episode.enclosure_url,
                    self.settings.work_dir / f"asr-{episode.id}",
                    on_progress,
                    meta={
                        "episode_title": episode.title,
                        "feed_title": episode.feed_title,
                    },
                )
            else:
                transcript = await self._wait_in_line(update, episode, on_progress)
            session.moments = await self.indexer.search(transcript, phrase)
            outcome = "ok" if session.moments else "empty"
        except PodcastCutterError as exc:
            outcome = getattr(exc, "code", "error")
            with contextlib.suppress(TelegramError):
                await progress.message.delete()
            await self._show_failure(update, session, exc.user_message(lang))
            return
        finally:
            await self._log(
                update,
                "search_audio",
                outcome=outcome,
                episode_id=episode.id,
                feed_title=episode.feed_title,
                episode_title=episode.title,
                detail=phrase[:100],
                ms=int((time.monotonic() - started) * 1000),
            )

        session.awaiting = Awaiting.NOTHING
        session.go(Screen.MOMENTS)
        await progress.show(self.view_for(session))

    async def _wait_in_line(self, update: Update, episode: Episode, on_progress) -> int:
        """Queue this episode for a first listen and wait for the transcript.

        The wait is given up on release, not the work: the job belongs to the
        queue now, so a caller that goes away — this handler cancelled, the
        process restarted — leaves an episode that still gets listened to and
        an answer that arrives as a message instead.
        """
        user = update.effective_user
        chat = update.effective_message.chat_id if update.effective_message else 0
        ticket = await self.listening.submit(
            episode, user.id if user else 0, chat, on_progress
        )
        try:
            return await ticket.done
        finally:
            self.listening.release(ticket)

    async def notify_listened(self, job, transcript_id: int | None) -> None:
        """Tell someone their episode is ready, when nothing else can.

        Reached only when the asker is no longer waiting — in practice, when
        the bot restarted while their episode was in line. Their session is
        gone with the process (sessions are a two-minute working set and are
        deliberately not persisted), so this cannot put the moments back on
        screen. It can say the expensive part is done and hand over a link
        that reopens the episode, where the search is now instant.
        """
        if self.telegram is None:
            return
        stored = (
            await self.store.user_language(job.user_id)
            if self.store is not None
            else None
        )
        lang = resolve_language(stored, None)
        title = truncate(job.episode_title or t(lang, "that_episode"), 80)
        if transcript_id is None:
            text = t(lang, "notify_failed", title=esc(title))
            markup = None
        else:
            text = t(lang, "notify_done", title=esc(title))
            markup = kb.open_episode(self.episode_link(job.episode_id), lang)
        try:
            await self.telegram.send_message(
                job.chat_id, text, parse_mode="HTML", reply_markup=markup
            )
        except TelegramError as exc:
            # Logged rather than swallowed: this is the only message the bot
            # ever sends unprompted, so a chat that blocks it is the one case
            # where a finished transcript reaches nobody, and silence here
            # would make that indistinguishable from a queue that never ran.
            logger.warning(
                "Could not tell %s that %s is ready: %s", job.chat_id,
                job.episode_id, exc,
            )
        if self.store is not None:
            await self.store.record(
                Event(
                    action="listened",
                    user_id=job.user_id,
                    outcome="ok" if transcript_id is not None else "failed",
                    episode_id=job.episode_id,
                    episode_title=job.episode_title,
                    feed_title=job.feed_title,
                    detail="resumed",
                )
            )

    async def _open_moment(
        self, update: Update, session: Session, start: int
    ) -> None:
        """A found moment becomes an ordinary clip, in the ordinary editor."""
        if session.episode is None:
            await self._stale(update, session)
            return
        session.set_clip(start, session.clip_length)
        session.clamp()
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

        if not self._budget_allows(self._input_budget, update):
            await self._refuse_rate(
                update, "input", t(session.language, "rate_input")
            )
            return

        menu_action = kb.menu_action(text)

        if session.was_reset:
            session.was_reset = False
            if menu_action is None:
                # Their message may answer a prompt that expired with the old
                # session; whatever happens next, they deserve to know the
                # context is gone before it does.
                with contextlib.suppress(TelegramError):
                    await update.effective_message.reply_text(
                        t(session.language, "started_fresh")
                    )

        try:
            if menu_action is not None:
                await self._route_menu(update, session, menu_action)
                return

            if session.awaiting is Awaiting.INTERVAL:
                await self._handle_interval_text(update, session, text)
                return

            if session.awaiting is Awaiting.PERSON:
                await self._search_people(update, session, text)
                return

            if session.awaiting is Awaiting.PHRASE:
                await self._search_phrase(update, session, text)
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
            await self._show_failure(
                update, session, exc.user_message(session.language)
            )

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

        if not self._budget_allows(self._input_budget, update):
            # The spinner is already answered; a refusal here is silence plus
            # a journal row, which is exactly what a button-masher deserves.
            await self._log(update, "limit", outcome="input")
            return

        session = await self.session_for(update, context)
        # A button press lands on a rendered screen, so no notice is owed —
        # but the flag must not survive to decorate some unrelated later text.
        session.was_reset = False
        prefix, value = kb.parse_callback(data)

        try:
            await self._route_callback(update, session, data, prefix, value)
        except PodcastCutterError as exc:
            await self._show_failure(
                update, session, exc.user_message(session.language)
            )

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
                screen = (
                    session.current.screen if session.current else Screen.MENU
                )
                session.awaiting = _SCREEN_AWAITING.get(
                    screen, Awaiting.NOTHING
                )
                await self.render(update, session)
            return

        if prefix == "menu":
            await self._route_menu(update, session, value)
            return

        if prefix == kb.LANG_PREFIX:
            await self._set_language(update, session, value)
            return

        if prefix == kb.PAGE_PREFIX and session.current:
            with contextlib.suppress(ValueError):
                page = int(value)
                # FEEDS is the one paged screen without a cached full list —
                # the directory is asked per page, so flipping a page is a
                # fetch, not a re-render. Re-rendering here showed page 1
                # again with a bigger number on it.
                if session.current.screen is Screen.FEEDS and session.query:
                    await self._search_feeds(update, session, session.query, page)
                    return
                # Paging replaces rather than pushes, so Back leaves the list
                # instead of walking back through every page.
                session.replace(session.current.screen, page)
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

        if prefix == kb.FORMAT_PREFIX:
            if value in FORMATS:
                session.send_as = value
            await self.render(update, session)
            return

        if prefix == kb.SKIN_PREFIX:
            if value in kb.SKIN_LABELS:
                session.skin = value
            await self.render(update, session)
            return

        if data == kb.ACTION_TOGGLE_VOICE:
            # Only reachable from a message rendered before the format row
            # existed; behaves as it always did.
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

        if prefix == kb.RESKIN_PREFIX:
            # Same clip, another look — a real cut, spent from the result
            # screen so nobody has to walk the editor again for it.
            if value not in kb.SKIN_LABELS:
                await self._stale(update, session)
                return
            if value == session.skin:
                # The marked current skin: nothing to re-render.
                return
            session.skin = value
            await self._start_cut(update, session)
            return

        # --- searching inside the episode ---------------------------------
        if data == kb.ACTION_FIND:
            await self.act_ask_phrase(update, session)
            return

        if prefix == kb.MOMENT_PREFIX:
            with contextlib.suppress(ValueError):
                await self._open_moment(update, session, int(value))
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
            "help": self.act_help,
            "language": self.act_language,
        }
        handler = actions.get(action)
        if handler is not None:
            await handler(update, session)
        else:
            await self._to_menu(update, session)

    async def _set_language(
        self, update: Update, session: Session, value: str
    ) -> None:
        """An explicit language choice — the one that is written down."""
        if value not in LANGUAGES:
            await self._stale(update, session)
            return
        changed = value != session.language
        session.language = value
        user_id = self._user_id(update)
        if self.store is not None and user_id is not None:
            await self.store.set_user_language(user_id, value)
        if changed:
            # The reply keyboard's labels live on the client until a new
            # keyboard arrives, so the confirmation carries one. Only on a
            # real change: repeating the current choice should not spam.
            with contextlib.suppress(TelegramError):
                await update.effective_message.reply_text(
                    t(value, "language_set"),
                    reply_markup=kb.main_menu(value),
                )
        session.replace(Screen.LANGUAGE)
        await self.render(update, session)
        await self._log(update, "language", detail=value)

    async def _stale(self, update: Update, session: Session) -> None:
        """A button whose message no longer matches the session."""
        session.reset_navigation()
        session.go(Screen.MENU)
        view = self.view_for(session)
        await self.show(
            update,
            View(
                t(session.language, "stale_menu") + "\n\n" + view.text,
                view.keyboard,
            ),
        )

    async def _show_failure(
        self, update: Update, session: Session, message: str
    ) -> None:
        await self.show(update, screens.failure(message, session.language))

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
                    t(session.language, "busy_previous_clip"),
                    self.view_for(session).keyboard,
                ),
            )
            return

        # Refused before any work or budget is charged: the editor screen
        # already warned, but a button on a scrolled-past message may carry a
        # length the warning never saw. A circle that cannot fit is a refusal
        # with a way out, never a silent switch to another format.
        refusal = None
        if (
            session.send_as == FORMAT_NOTE
            and session.clip_length > video.VIDEO_NOTE_SECONDS
        ):
            refusal = t(session.language, "circle_rule")
        if (
            session.send_as == FORMAT_VIDEO
            and session.clip_length > video.MAX_VIDEO_SECONDS
        ):
            refusal = t(
                session.language, "video_cap",
                limit=format_duration(video.MAX_VIDEO_SECONDS),
            )
        if refusal is not None:
            await self.show(
                update, View(refusal, self.view_for(session).keyboard)
            )
            return

        if not self._budget_allows(self._cut_budget, update):
            await self.show(
                update,
                View(
                    t(session.language, "rate_cuts"),
                    self.view_for(session).keyboard,
                ),
            )
            await self._log(update, "limit", outcome="cut")
            return

        # Claimed before the first await, or two quick taps both pass the
        # check above and cut twice.
        self._busy_users.add(user_id)
        try:
            session.clamp()
            interval = Interval(start=session.clip_start, end=session.clip_end)

            queued = self._job_slots.locked()
            status_message = await self.show(
                update,
                screens.working(
                    t(
                        session.language,
                        "queued_slot" if queued else "working_on_it",
                    )
                ),
            )
            if status_message is None:
                return

            cut = asyncio.create_task(
                self._perform_cut(
                    update, session, episode, interval,
                    StatusEditor(status_message),
                )
            )
            self._cut_tasks[user_id] = cut
            try:
                await cut
            except asyncio.CancelledError:
                # The task being cancelled is /cancel doing its job — it has
                # already cleaned up and journalled. Anything else means *this*
                # handler is being cancelled, and the cut must go down with it.
                if not cut.cancelled():
                    cut.cancel()
                    raise
        finally:
            self._cut_tasks.pop(user_id, None)
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
        lang = session.language

        async def on_status(text: str) -> None:
            await status.set(text, force=True)

        async def on_progress(done: int, total: int | None) -> None:
            await status.set(
                t(lang, "downloading_episode")
                + f"\n\n{progress_bar(done, total, lang=lang)}"
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
                    proxy=self.media_proxy,
                    lang=lang,
                )

                if session.send_as in (FORMAT_NOTE, FORMAT_VIDEO):
                    await status.set(t(lang, "painting"), force=True)
                    result = await self._render_video(
                        session, episode, interval, result, workdir
                    )

                await status.set(
                    t(lang, "uploading", size=human_bytes(result.size, lang)),
                    force=True,
                )
                if chat is not None:
                    with contextlib.suppress(TelegramError):
                        await chat.send_action(self._upload_action(session, interval))

                await self._deliver(
                    message, session, episode, interval, result
                )

                session.replace(Screen.RESULT)
                await status.show(screens.result(session, self.bot_username))
                outcome, size_bytes = "ok", result.size
                details = []
                if session.send_as in (FORMAT_NOTE, FORMAT_VIDEO):
                    # Which format and which skin — the first thing to ask
                    # the journal once real people press this.
                    details.append(f"{session.send_as}:{session.skin}")
                if result.route == PROXY:
                    # Recorded on the cut row itself rather than as its own
                    # event, so "how many episodes is the detour earning?" is
                    # one query and the action vocabulary stays as it was.
                    details.append("route=proxy")
                detail = " ".join(details) or None

            except asyncio.CancelledError:
                # /cancel while cutting. audio._run has already killed the
                # subprocess; say so on the status message the menu will sit
                # under, journal it, and let the cancellation continue out.
                outcome = "cancelled"
                await status.show(screens.working(t(lang, "cut_cancelled")))
                raise
            except PodcastCutterError as exc:
                # The stable code, not the class name: grouping failures in SQL
                # keeps working across refactors. The journal reads English;
                # the user reads their own language.
                outcome = exc.code
                detail = exc.user_message()
                await status.show(
                    screens.failure(exc.user_message(lang), lang)
                )
            except TelegramError as exc:
                logger.exception("Failed to deliver the cut: %s", exc)
                outcome, detail = "upload_rejected", str(exc)
                await status.show(
                    screens.failure(t(lang, "upload_rejected"), lang)
                )
            except Exception as exc:
                logger.exception("Unexpected failure while cutting %s", episode.id)
                outcome, detail = "crash", f"{type(exc).__name__}: {exc}"
                await status.show(
                    screens.failure(t(lang, "generic_error"), lang)
                )
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

    def _upload_action(self, session: Session, interval: Interval) -> ChatAction:
        if session.send_as == FORMAT_NOTE:
            return ChatAction.UPLOAD_VIDEO_NOTE
        if session.send_as == FORMAT_VIDEO:
            return ChatAction.UPLOAD_VIDEO
        return (
            ChatAction.UPLOAD_VOICE
            if session.as_voice
            else ChatAction.UPLOAD_DOCUMENT
        )

    async def _render_video(
        self,
        session: Session,
        episode: Episode,
        interval: Interval,
        result,
        workdir: Path,
    ):
        """The video half of a note job, run inside the same cut slot.

        Deliberately *not* a queued task of its own: a render measured
        seconds on this host — the cost class of the cut that feeds it, not
        of a transcription — and it cannot start before that cut exists
        anyway. The durable queue earns its complexity protecting minutes of
        CPU across a redeploy; a lost render costs one more tap.
        """
        subtitles = None
        if self.store is not None:
            # The same transcript the search runs on. Nothing is transcribed
            # for a video: an episode nobody has searched simply gets no
            # subtitles, because minutes of CPU for a caption is the wrong
            # trade until somebody asks for the words.
            transcript = await self.store.transcript_for_episode(episode.id)
            if transcript is not None:
                utterances = await self.store.utterances_for(transcript)
                subtitles = (
                    video.subtitle_lines(
                        utterances, interval.start, interval.end
                    )
                    or None
                )

        cover = None
        if session.skin in video.COVER_SKINS and episode.image:
            cover = await video.fetch_cover(
                episode.image, workdir, self.settings
            )

        path = await video.render_clip(
            result.path,
            workdir,
            skin=session.skin,
            duration=float(interval.duration),
            # The renderer fits this to the layout — how many characters
            # survive is a property of the circle, not of the episode.
            title=one_line(f"{episode.feed_title} — {episode.title}"),
            span=(
                f"{format_duration(interval.start)}–"
                f"{format_duration(interval.end)}"
            ),
            subtitles=subtitles,
            cover=cover,
            settings=self.settings,
            round_frame=session.send_as == FORMAT_NOTE,
        )
        size = path.stat().st_size
        if size > self.settings.max_upload_bytes:
            raise TooLargeError(
                "err_video_too_large",
                size=size // (1024 * 1024),
                limit=self.settings.max_upload_bytes // (1024 * 1024),
            )
        return replace(result, path=path, size=size, transcoded=True)

    async def _deliver(
        self,
        message: Message,
        session: Session,
        episode: Episode,
        interval: Interval,
        result,
    ) -> None:
        # The last line is the attribution: a clip is a citation, and a
        # citation names its source in the same message (ROADMAP §13.4).
        source = html.escape(episode.enclosure_url, quote=True)
        caption = (
            f"<b>{esc(truncate(episode.title, 90))}</b>\n"
            f"{esc(truncate(episode.feed_title, 60))} · "
            f"{format_duration(interval.start)}–{format_duration(interval.end)}\n"
            f'<a href="{source}">{t(session.language, "full_episode_link")}</a>'
        )
        timeouts = {
            "read_timeout": self.settings.upload_timeout,
            "write_timeout": self.settings.upload_timeout,
            "connect_timeout": 60,
        }

        with result.path.open("rb") as handle:
            if session.send_as == FORMAT_NOTE:
                # sendVideoNote has no caption parameter at all, so the
                # attribution lives on the result screen that follows.
                await message.reply_video_note(
                    video_note=handle,
                    duration=interval.duration,
                    length=video.NOTE_SIZE,
                    **timeouts,
                )
            elif session.send_as == FORMAT_VIDEO:
                await message.reply_video(
                    video=handle,
                    duration=interval.duration,
                    width=video.NOTE_SIZE,
                    height=video.NOTE_SIZE,
                    caption=caption,
                    parse_mode="HTML",
                    filename=safe_filename(
                        episode.feed_title,
                        episode.title,
                        f"{format_duration(interval.start)}"
                        f"-{format_duration(interval.end)}".replace(":", "."),
                        ext=".mp4",
                    ),
                    **timeouts,
                )
            elif session.as_voice:
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
        lang = await self._language_for_user(update.effective_user)

        open_button = InlineQueryResultsButton(
            text=t(lang, "inline_open_bot"), start_parameter="menu"
        )

        if len(query) < 2:
            await inline.answer(
                [],
                cache_time=INLINE_CACHE_SECONDS,
                is_personal=False,
                button=open_button,
            )
            return

        # Inline needs no /start and would otherwise be the way around every
        # limit above it. Same input budget, empty answer when it runs out —
        # the button survives, so the polite path stays open.
        if not self._budget_allows(self._input_budget, update):
            await self._log(update, "limit", outcome="inline")
            with contextlib.suppress(TelegramError):
                await inline.answer(
                    [],
                    cache_time=INLINE_CACHE_SECONDS,
                    is_personal=True,
                    button=open_button,
                )
            return

        try:
            if query.startswith(kb.INLINE_EPISODE_PREFIX):
                # The Share button asks for one exact episode by id; nothing
                # here should depend on how the directory feels about titles.
                episodes = [
                    await self.client.get_episode(
                        query[len(kb.INLINE_EPISODE_PREFIX) :].strip()
                    )
                ]
            else:
                episodes = await self._inline_episodes(query)
        except PodcastCutterError as exc:
            # Nothing to show is not an error the user can act on here — the
            # button stays, so they can still open the bot and search properly.
            with contextlib.suppress(TelegramError):
                await inline.answer(
                    [], cache_time=INLINE_EMPTY_CACHE_SECONDS, button=open_button
                )
            await self._log(update, "inline", detail=query, outcome=exc.code)
            return

        results = [
            self._inline_result(episode, lang)
            for episode in episodes[:INLINE_RESULT_LIMIT]
        ]
        with contextlib.suppress(TelegramError):
            await inline.answer(
                results, cache_time=INLINE_CACHE_SECONDS, button=open_button
            )
        await self._log(
            update, "inline", detail=query, outcome="ok", size_bytes=len(results)
        )

    async def _inline_episodes(self, query: str) -> list[Episode]:
        """Episodes to offer for a free-text inline query.

        The podcast search goes first: what people type inline is mostly a
        show's name, and ``/search/byperson`` — despite its name — matches
        free text loosely across the whole directory, so asking it first
        answered a show title with somebody else's podcasts (observed live:
        a shared episode title came back as a page of Chinese feeds). A
        person's name that matches no podcast still falls through to it.
        """
        try:
            feeds, _ = await self.client.search_feeds(query)
            if feeds:
                return await self.client.list_episodes(feeds[0].id)
        except NotFoundError:
            pass

        return await self.client.search_episodes_by_person(query)

    def _inline_result(
        self, episode: Episode, lang: str = DEFAULT_LANGUAGE
    ) -> InlineQueryResultArticle:
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
                f'<a href="{link}">{t(lang, "inline_cut_link")}</a>',
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

        # Without a routed session, the best guess is whatever session already
        # exists, then the client's language — never a crash on top of a crash.
        session = peek_session(getattr(context, "user_data", None))
        if session is not None:
            lang = session.language
        else:
            user = update.effective_user
            lang = resolve_language(
                None, user.language_code if user else None
            )

        text = (
            context.error.user_message(lang)
            if isinstance(context.error, PodcastCutterError)
            else t(lang, "generic_error")
        )
        with contextlib.suppress(TelegramError):
            await message.reply_text(
                f"⚠️ {esc(text)}", parse_mode="HTML",
                reply_markup=kb.main_menu(lang),
            )


#: An action that operates on the current session, e.g. :meth:`act_trending`.
MenuAction = Callable[[Update, Session], Awaitable[None]]
