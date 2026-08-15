"""Navigation model and the per-user session.

The bot is a stack of screens. ``Session`` holds where the user is, how they
got there (so ``‹ Back`` works), and the data the current screen renders from.

Two deliberate choices:

* Paging *replaces* the current screen instead of pushing, so Back leaves a
  list in one step rather than walking backwards through every page visited.
* What the bot is waiting for is an explicit field rather than an implicit
  conversation state. Every text message is routed by it, so there is no
  situation in which typing does nothing.
"""

from __future__ import annotations

import time
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

from .api import Episode, Feed

#: Key under which the session lives in ``context.user_data``.
_SESSION_KEY = "session"

#: How many screens of history to keep. Deep enough for any real path.
_MAX_HISTORY = 12

#: How many episodes the "Recent" list remembers.
MAX_RECENTS = 10

#: What a cut is delivered as. Strings rather than an enum because they ride
#: in callback payloads (``fmt:note``) and the journal, where an enum's name
#: would be serialised back to exactly this anyway.
FORMAT_AUDIO = "audio"
FORMAT_VOICE = "voice"
#: The round video note, cropped to a circle by every client and capped at a
#: minute by the API.
FORMAT_NOTE = "note"
#: An ordinary square video file — the same render, full-frame layout, sent
#: with a caption. Its own format rather than a fallback of the note: which
#: one arrives is the user's choice, not a side effect of the clip's length.
FORMAT_VIDEO = "video"
FORMATS = (FORMAT_AUDIO, FORMAT_VOICE, FORMAT_NOTE, FORMAT_VIDEO)


class Screen(Enum):
    """Where the user is."""

    MENU = auto()
    ASK_PODCAST = auto()
    FEEDS = auto()
    EPISODES = auto()
    ASK_PERSON = auto()
    GLOBAL = auto()
    TRENDING = auto()
    RECENT = auto()
    INTERVAL = auto()
    RESULT = auto()
    ASK_PHRASE = auto()
    MOMENTS = auto()
    LANGUAGE = auto()


class Awaiting(Enum):
    """What a plain text message means right now."""

    NOTHING = auto()
    PODCAST_NAME = auto()
    PERSON = auto()
    INTERVAL = auto()
    PHRASE = auto()


@dataclass(frozen=True)
class Nav:
    """A point in the navigation stack."""

    screen: Screen
    page: int = 1


@dataclass
class Session:
    """Everything the bot remembers about one user."""

    # -- navigation --------------------------------------------------------
    current: Nav | None = None
    history: list[Nav] = field(default_factory=list)
    awaiting: Awaiting = Awaiting.NOTHING
    last_active: float = field(default_factory=time.time)
    #: True on the first use after an expired session was thrown away. The
    #: user's next message may have been typed against a screen that no
    #: longer exists — «winter or tree» meant for a phrase prompt becomes a
    #: baffling podcast search — so the router gets one chance to say what
    #: happened before quietly carrying on.
    was_reset: bool = False

    # -- language ----------------------------------------------------------
    #: What the bot answers in. Resolved once per session: the stored choice
    #: wins, then Telegram's ``language_code``, then English.
    language: str = "en"
    #: Whether the stored preference has been looked up yet this session.
    language_loaded: bool = False

    # -- search ------------------------------------------------------------
    query: str = ""
    feeds: list[Feed] = field(default_factory=list)
    #: Every feed seen across pages, so buttons on scrolled-past messages still
    #: resolve instead of erroring out.
    feeds_by_id: dict[str, Feed] = field(default_factory=dict)
    feed: Feed | None = None
    feeds_has_next: bool = False

    # -- episodes ----------------------------------------------------------
    episodes: list[Episode] = field(default_factory=list)
    episode: Episode | None = None
    recents: list[Episode] = field(default_factory=list)
    #: Whether the recent list has been pulled from the database yet.
    recents_loaded: bool = False
    #: Typed filter narrowing the episode list, without discarding it.
    episode_filter: str = ""

    # -- clip --------------------------------------------------------------
    clip_start: int = 0
    clip_length: int = 60
    #: One of :data:`FORMATS`. The old boolean grew a third value when video
    #: notes arrived; ``as_voice`` below keeps the boolean question answerable.
    send_as: str = FORMAT_AUDIO
    #: Video-note skin. The value is a key of ``keyboards.SKIN_LABELS`` and of
    #: ``video.SKINS`` — a test holds those two sets equal, so a bare string
    #: here cannot quietly drift from either.
    skin: str = "cover"

    # -- searching inside an episode ---------------------------------------
    #: The phrase last looked for, kept so the results screen can say what it
    #: answered and so a failed search can be retried without retyping.
    phrase: str = ""
    #: Moments from the last search. Not persisted: they are derived from a
    #: transcript that outlives the session, so a stale one costs a re-search
    #: rather than a re-transcription.
    moments: list = field(default_factory=list)
    #: Whether this episode has already been listened to. Read once, when the
    #: search screen opens, because the screen has to promise either "instant"
    #: or "a few minutes" and rendering is not the place to query a database.
    episode_transcribed: bool = False

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def go(self, screen: Screen, page: int = 1) -> Nav:
        """Move to a new screen, remembering the current one for Back."""
        if self.current is not None and self.current.screen is not screen:
            self.history.append(self.current)
            del self.history[:-_MAX_HISTORY]
        self.current = Nav(screen, page)
        return self.current

    def replace(self, screen: Screen, page: int = 1) -> Nav:
        """Swap the current screen without growing the history."""
        self.current = Nav(screen, page)
        return self.current

    def back(self) -> Nav | None:
        """Pop to the previous screen, or ``None`` if there is none left."""
        if not self.history:
            return None
        self.current = self.history.pop()
        return self.current

    def reset_navigation(self) -> None:
        self.current = None
        self.history.clear()
        self.awaiting = Awaiting.NOTHING

    def touch(self) -> None:
        self.last_active = time.time()

    def is_stale(self, timeout: float) -> bool:
        return time.time() - self.last_active > timeout

    # ------------------------------------------------------------------
    # Search results
    # ------------------------------------------------------------------

    def remember_feeds(self, feeds: list[Feed], has_next: bool = False) -> None:
        self.feeds = feeds
        self.feeds_has_next = has_next
        self.feeds_by_id.update({feed.id: feed for feed in feeds})

    def find_feed(self, feed_id: str) -> Feed | None:
        return self.feeds_by_id.get(feed_id)

    def select_feed(self, feed: Feed) -> None:
        """Choose a podcast, discarding episodes cached for a different one."""
        if self.feed is None or self.feed.id != feed.id:
            self.set_episodes([])
        self.feed = feed

    def set_episodes(self, episodes: list[Episode]) -> None:
        self.episodes = episodes
        self.episode = None
        self.episode_filter = ""

    @property
    def visible_episodes(self) -> list[Episode]:
        """The episode list as the user currently sees it."""
        if not self.episode_filter:
            return self.episodes
        needle = self.episode_filter.lower()
        return [ep for ep in self.episodes if needle in ep.title.lower()]

    def filter_episodes(self, episodes: list[Episode]) -> list[Episode]:
        """``episodes`` narrowed by the typed filter, for cross-feed lists.

        Matches the show title as well as the episode's, because on GLOBAL
        and RECENT the show is half of what the user sees on each button —
        unlike :attr:`visible_episodes`, where every row shares one show and
        matching its name would match everything.
        """
        if not self.episode_filter:
            return episodes
        needle = self.episode_filter.lower()
        return [
            ep
            for ep in episodes
            if needle in ep.title.lower() or needle in ep.feed_title.lower()
        ]

    def find_episode(self, episode_id: str) -> Episode | None:
        """Look an episode up anywhere we might still know about it."""
        for pool in (self.episodes, self.recents):
            for episode in pool:
                if episode.id == episode_id:
                    return episode
        if self.episode is not None and self.episode.id == episode_id:
            return self.episode
        return None

    def remember_recent(self, episode: Episode) -> None:
        """Push onto the recent list, most recent first, without duplicates."""
        self.recents = [ep for ep in self.recents if ep.id != episode.id]
        self.recents.insert(0, episode)
        del self.recents[MAX_RECENTS:]

    # ------------------------------------------------------------------
    # Clip editing
    # ------------------------------------------------------------------

    def select_episode(self, episode: Episode, default_length: int = 60) -> None:
        self.episode = episode
        self.clip_start = 0
        self.clip_length = default_length
        self.remember_recent(episode)

    def set_clip(self, start: int, length: int) -> None:
        self.clip_start = max(0, start)
        self.clip_length = max(1, length)

    def set_length(self, length: int, max_length: int) -> None:
        self.clip_length = max(1, min(length, max_length))
        self.clamp()

    def move_clip(self, delta: int) -> None:
        """Slide the whole window, keeping its length."""
        self.clip_start = max(0, self.clip_start + delta)
        self.clamp()

    def clamp(self) -> None:
        """Keep the clip inside the episode, when we know how long it is."""
        duration = self.episode.duration if self.episode else None
        if not duration:
            return
        # Leave at least a second of audio; a zero-length clip is not useful.
        self.clip_start = max(0, min(self.clip_start, max(0, duration - 1)))
        self.clip_length = max(1, min(self.clip_length, duration - self.clip_start))

    @property
    def clip_end(self) -> int:
        return self.clip_start + self.clip_length

    @property
    def as_voice(self) -> bool:
        """Whether the clip goes out as a voice note. Kept as a property so
        the journal's ``as_voice`` column and the delivery code did not have
        to learn three-valued logic when the note format arrived."""
        return self.send_as == FORMAT_VOICE

    @as_voice.setter
    def as_voice(self, value: bool) -> None:
        self.send_as = FORMAT_VOICE if value else FORMAT_AUDIO

    # ------------------------------------------------------------------
    # Paging helpers
    # ------------------------------------------------------------------

    @staticmethod
    def page_of(items: list, page: int, per_page: int) -> tuple[list, int, int]:
        """Return ``(window, page, pages)`` with the page clamped into range."""
        pages = max(1, -(-len(items) // per_page)) if items else 1
        page = max(1, min(page, pages))
        start = (page - 1) * per_page
        return items[start : start + per_page], page, pages


def get_session(user_data: MutableMapping[str, Any]) -> Session:
    """Fetch the session, creating it if this is the user's first message."""
    session = user_data.get(_SESSION_KEY)
    if not isinstance(session, Session):
        session = Session()
        user_data[_SESSION_KEY] = session
    return session


def reset_session(user_data: MutableMapping[str, Any]) -> Session:
    """Start a fresh session, keeping only the recent-episode list.

    Recents survive because they are the user's history, not part of whatever
    flow is being abandoned.
    """
    previous = user_data.get(_SESSION_KEY)
    known = isinstance(previous, Session)
    recents = list(previous.recents) if known else []

    user_data.clear()
    # Carrying `recents_loaded` over avoids re-reading the database on every
    # cancel, since the list itself is carried over too.
    session = Session(
        recents=recents, recents_loaded=previous.recents_loaded if known else False
    )
    if known:
        # The language survives every reset: it is who the user is, not part
        # of whatever flow is being abandoned.
        session.language = previous.language
        session.language_loaded = previous.language_loaded
    user_data[_SESSION_KEY] = session
    return session


def peek_session(user_data: MutableMapping[str, Any] | None) -> Session | None:
    """The session if one exists, without creating or resetting anything.

    For code that runs outside the normal routers — the global error handler —
    and needs the user's language without side effects.
    """
    if not user_data:
        return None
    session = user_data.get(_SESSION_KEY)
    return session if isinstance(session, Session) else None
