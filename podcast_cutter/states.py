"""Conversation states and the per-user session object.

The old code kept a dozen loose keys in ``context.user_data`` (``podcast_id``,
``all_found_episodes``, ``podcast_episode_page``, …) and cleared them only on
some entry points. Stale keys leaking between flows — a page number from a
global search reused for a podcast's episode list, for instance — were a steady
source of "it shows me the wrong thing" bugs. One typed object with an explicit
reset removes that whole class of problem.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import dataclass, field
from enum import IntEnum, auto
from typing import Any

from .api import Episode, Feed

#: Key under which the session lives in ``context.user_data``.
_SESSION_KEY = "session"


class State(IntEnum):
    """Conversation states.

    Values start at 1, so they cannot collide with ``ConversationHandler``'s
    own sentinels (END is -1, TIMEOUT -2, WAITING -3).
    """

    ASK_PODCAST_NAME = auto()
    CHOOSE_PODCAST = auto()
    CHOOSE_EPISODE = auto()
    ASK_PERSON_QUERY = auto()
    CHOOSE_GLOBAL_EPISODE = auto()
    ASK_INTERVAL = auto()


@dataclass
class Session:
    """Everything the bot remembers about one user's in-flight request."""

    #: The search term the user last typed.
    query: str = ""

    # -- podcast search ----------------------------------------------------
    feed_page: int = 1
    feeds: list[Feed] = field(default_factory=list)
    #: Every feed seen across pages, so buttons on scrolled-past messages still
    #: resolve instead of erroring out.
    feeds_by_id: dict[str, Feed] = field(default_factory=dict)
    feed: Feed | None = None

    # -- episodes ----------------------------------------------------------
    episode_page: int = 1
    episodes: list[Episode] = field(default_factory=list)
    episode: Episode | None = None

    def remember_feeds(self, feeds: list[Feed]) -> None:
        self.feeds = feeds
        self.feeds_by_id.update({feed.id: feed for feed in feeds})

    def find_feed(self, feed_id: str) -> Feed | None:
        return self.feeds_by_id.get(feed_id)

    def select_feed(self, feed: Feed) -> None:
        """Choose a podcast, discarding episodes cached for a different one.

        Forgetting this step is how the bot used to show one podcast's episode
        list under another podcast's name.
        """
        if self.feed is None or self.feed.id != feed.id:
            self.set_episodes([])
        self.feed = feed

    def set_episodes(self, episodes: list[Episode]) -> None:
        """Install a new episode list and rewind paging.

        Always resetting the page here is what keeps a leftover page number
        from a previous search out of the next one.
        """
        self.episodes = episodes
        self.episode_page = 1
        self.episode = None

    def find_episode(self, episode_id: str) -> Episode | None:
        return next((ep for ep in self.episodes if ep.id == episode_id), None)

    def page_of(self, items: list, page: int, per_page: int) -> tuple[list, bool, bool]:
        """Return ``(window, has_prev, has_next)`` for a client-side page."""
        page = max(1, page)
        start = (page - 1) * per_page
        window = items[start : start + per_page]
        return window, page > 1, start + per_page < len(items)


def get_session(user_data: MutableMapping[str, Any]) -> Session:
    """Fetch the session, creating it if this is the user's first message."""
    session = user_data.get(_SESSION_KEY)
    if not isinstance(session, Session):
        session = Session()
        user_data[_SESSION_KEY] = session
    return session


def reset_session(user_data: MutableMapping[str, Any]) -> Session:
    """Start a fresh session, dropping any cached episode lists."""
    user_data.clear()
    session = Session()
    user_data[_SESSION_KEY] = session
    return session
