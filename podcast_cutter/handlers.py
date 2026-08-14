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
from .indexer import Indexer, TranscriptionDisabled
from .limits import Budget
from .listening import QUEUED, TranscriptionQueue
from .proxy import PROXY, MediaProxy
from .screens import View
from .states import (
    FORMAT_NOTE,
    FORMATS,
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

#: Headline per stage. The bar and the estimate carry the detail.
_LISTENING_STAGES = {
    "download": "⬇️ Fetching the episode…",
    "decode": "🔧 Preparing the audio…",
    "transcribe": "🎧 Listening to the episode…",
    "index": "🧭 Indexing what was said…",
}

#: What "you are Nth in line" reads as. Numbers, not "queued": a wait with no
#: number is indistinguishable from a hang, and the first thing someone does
#: about a hang is press the button again.
_ORDINALS = ("", "1st", "2nd", "3rd", "4th", "5th", "6th", "7th", "8th", "9th")


def _ordinal(place: int) -> str:
    return _ORDINALS[place] if place < len(_ORDINALS) else f"{place}th"

#: Rotated under the bar while recognition runs, one every few edits.
#:
#: Each says something true about what is happening or why it is worth the
#: wait. That is the whole test for adding one: filler that could appear over
#: any wait at all makes the screen less trustworthy, not friendlier — a person
#: reading the same cheerful nothing twice concludes it is a spinner, which is
#: the impression this exists to remove.
_WAITING_NOTES = (
    "This happens once per episode — every later search on it is instant.",
    "The whole episode gets listened to in one go, so any future question "
    "about it is already paid for.",
    "Timestamps come from the words themselves, so a clip opens where the "
    "phrase actually starts.",
    "Silence and music are skipped, which is why the bar sometimes jumps.",
    "Names and jargon are the hard part; common words come out fine.",
    "Once this is done you can search this episode as many times as you like.",
)

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

GENERIC_ERROR = "Something went wrong on my side. Please try again."

#: The first thing a newcomer reads. The menu that follows says what the
#: buttons do, so this says the two things the buttons cannot: how a moment is
#: written, and that the bot works from inside someone else's chat.
WELCOME_TEXT = (
    "👋 <b>Welcome!</b>\n\n"
    "I cut a short piece out of a podcast episode and send it back, so you "
    "can share the good bit instead of a two-hour link.\n\n"
    "Once you've picked an episode, tell me when it starts: "
    "<code>12:30</code> for a clip from there, or <code>12:30-14:00</code> "
    "for an exact range. The ◀ ▶ buttons nudge it until it's right.\n\n"
    "In any other chat, type <code>@{username}</code> and a name to hand "
    "someone an episode without leaving the conversation.\n\n"
    "Send me a podcast name to start."
)

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


def _listening_text(stage, started: float, estimate: int) -> str:
    """What the waiting screen says right now.

    Three things, in the order they answer "has this hung?": which stage, how
    far through it, and how much longer. The remaining time is derived from the
    work actually done so far rather than from the opening estimate, so it
    stops being a promise the moment reality disagrees with it.
    """
    if stage.stage == QUEUED:
        place = int(stage.done)
        ahead = place - 1
        return (
            f"⏳ <b>{_ordinal(place)} in line</b>\n\n"
            f"<i>{ahead} episode{'' if ahead == 1 else 's'} ahead of this one. "
            "Listening starts on its own — nothing to press.</i>"
        )

    lines = [_LISTENING_STAGES.get(stage.stage, "🎧 Working…")]

    fraction = stage.fraction
    if fraction is not None:
        lines.append("")
        # Each stage counts a different thing: bytes down, seconds of audio
        # heard, windows embedded. The bar has to say which, or forty minutes
        # of episode reads as «2.4 KB».
        label = {
            "transcribe": format_duration,
            "index": lambda count: str(int(count)),
        }.get(stage.stage, human_bytes)
        lines.append(progress_bar(int(stage.done), int(stage.total), label=label))

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
            lines.append(f"<i>about {format_duration(remaining)} left</i>")

        # Rotated on the clock, not on how often this happens to be called:
        # edits are throttled, so counting calls would change the line without
        # anyone seeing it.
        note = _WAITING_NOTES[int(elapsed // NOTE_SECONDS) % len(_WAITING_NOTES)]
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
            WELCOME_TEXT.format(
                username=self.bot_username or "podcast_cutter_bot"
            ),
            parse_mode="HTML",
            reply_markup=kb.main_menu(),
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
                update,
                session,
                "One job at a time, please — this one is still running.",
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
                    update,
                    session,
                    "🐢 That is the day's budget for first listens spent. "
                    "Episodes the bot already knows still search instantly.",
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
                    update,
                    session,
                    "The listening queue is full right now — "
                    "try again in a few minutes.",
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
        rtf = await self.store.measured_rtf() if self.store else None
        estimate = int((episode.duration or 0) * (rtf or DEFAULT_RTF))
        opening = "🎧 Getting ready to listen…"
        if estimate >= 30:
            opening += (
                f"\n\n<i>About {format_duration(estimate)} for this one — "
                "it only happens once per episode.</i>"
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
                _listening_text(stage, started, estimate), force=changed
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
            await self._show_failure(update, session, exc.user_message)
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
        title = truncate(job.episode_title or "that episode", 80)
        if transcript_id is None:
            text = (
                f"⚠️ I could not finish listening to <b>{esc(title)}</b>. "
                "Ask again and I will retry it."
            )
            markup = None
        else:
            text = (
                f"✅ I have finished listening to <b>{esc(title)}</b> — "
                "searching inside it is instant now.\n\n"
                "<i>Your place in the chat was lost when I restarted, so open "
                "it again and ask for the phrase.</i>"
            )
            markup = kb.open_episode(self.episode_link(job.episode_id))
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
                update,
                "input",
                "🐢 That is a lot of requests in one minute. "
                "Give it a moment and try again.",
            )
            return

        if session.was_reset:
            session.was_reset = False
            if text not in kb.MENU_BUTTONS:
                # Their message may answer a prompt that expired with the old
                # session; whatever happens next, they deserve to know the
                # context is gone before it does.
                with contextlib.suppress(TelegramError):
                    await update.effective_message.reply_text(
                        "⏱ It had been a while, so I started fresh — "
                        "if this was meant for an earlier screen, "
                        "just navigate there again."
                    )

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

        if (
            session.send_as == FORMAT_NOTE
            and session.clip_length > video.MAX_VIDEO_SECONDS
        ):
            # Refused before any work or budget is charged: the editor screen
            # already warned, but a button on a scrolled-past message may
            # carry a length the warning never saw.
            await self.show(
                update,
                View(
                    "⚠️ Video is capped at "
                    f"{format_duration(video.MAX_VIDEO_SECONDS)} — "
                    "shorten the clip or switch back to audio.",
                    self.view_for(session).keyboard,
                ),
            )
            return

        if not self._budget_allows(self._cut_budget, update):
            await self.show(
                update,
                View(
                    "🐢 That was a lot of clips for one hour. "
                    "The budget resets as the hour rolls on — try again soon.",
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
                    "⏳ Queued — waiting for a free slot…"
                    if queued
                    else "⏳ Working on it…"
                ),
            )
            if status_message is None:
                return

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
                    proxy=self.media_proxy,
                )

                if session.send_as == FORMAT_NOTE:
                    await status.set("🎨 Painting the sound…", force=True)
                    result = await self._render_video(
                        session, episode, interval, result, workdir
                    )

                await status.set(
                    f"📤 Uploading {human_bytes(result.size)}…", force=True
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
                if session.send_as == FORMAT_NOTE:
                    # `note` fits the circle; `video` means the clip outgrew a
                    # minute and went out square. The distinction is the first
                    # thing to ask the journal once real people press this.
                    kind = (
                        "note"
                        if interval.duration <= video.VIDEO_NOTE_SECONDS
                        else "video"
                    )
                    details.append(f"{kind}:{session.skin}")
                if result.route == PROXY:
                    # Recorded on the cut row itself rather than as its own
                    # event, so "how many episodes is the detour earning?" is
                    # one query and the action vocabulary stays as it was.
                    details.append("route=proxy")
                detail = " ".join(details) or None

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

    def _upload_action(self, session: Session, interval: Interval) -> ChatAction:
        if session.send_as == FORMAT_NOTE:
            return (
                ChatAction.UPLOAD_VIDEO_NOTE
                if interval.duration <= video.VIDEO_NOTE_SECONDS
                else ChatAction.UPLOAD_VIDEO
            )
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
        if session.skin == video.SKIN_COVER and episode.image:
            cover = await video.fetch_cover(
                episode.image, workdir, self.settings
            )

        path = await video.render_clip(
            result.path,
            workdir,
            skin=session.skin,
            duration=float(interval.duration),
            title=truncate(
                one_line(f"{episode.feed_title} — {episode.title}"), 44
            ),
            span=(
                f"{format_duration(interval.start)}–"
                f"{format_duration(interval.end)}"
            ),
            subtitles=subtitles,
            cover=cover,
            settings=self.settings,
        )
        size = path.stat().st_size
        if size > self.settings.max_upload_bytes:
            raise TooLargeError(
                f"The video is {size // (1024 * 1024)} MB, above the "
                f"{self.settings.max_upload_bytes // (1024 * 1024)} MB "
                "Telegram limit. Please pick a shorter interval."
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
            if session.send_as == FORMAT_NOTE:
                if interval.duration <= video.VIDEO_NOTE_SECONDS:
                    # sendVideoNote has no caption parameter at all, so the
                    # attribution lives on the result screen that follows.
                    await message.reply_video_note(
                        video_note=handle,
                        duration=interval.duration,
                        length=video.NOTE_SIZE,
                        **timeouts,
                    )
                else:
                    await message.reply_video(
                        video=handle,
                        duration=interval.duration,
                        width=video.NOTE_SIZE,
                        height=video.NOTE_SIZE,
                        caption=caption,
                        parse_mode="HTML",
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

        open_button = InlineQueryResultsButton(
            text="Open Podcast Cutter", start_parameter="menu"
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
            self._inline_result(episode)
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
        """Episodes to offer for an inline query.

        ``/search/byperson`` only matches people credited in a feed, so it
        answers nothing for a topic or a podcast's own name — which is what
        most people type. Fall back to searching podcasts by term and offering
        the top match's episodes, the same two-step the in-chat search does.
        """
        try:
            return await self.client.search_episodes_by_person(query)
        except NotFoundError:
            pass

        feeds, _ = await self.client.search_feeds(query)
        if not feeds:
            raise NotFoundError(f"No podcasts found for “{one_line(query)}”.")
        return await self.client.list_episodes(feeds[0].id)

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
