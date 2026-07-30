"""Fakes that let the routers be exercised without a network or a bot token.

The router is where the interface's correctness lives — which screen a tap
leads to, what typing means where — so it is worth testing directly rather
than through the Telegram API.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from podcast_cutter.api import Episode, Feed
from podcast_cutter.config import Settings
from podcast_cutter.errors import NotFoundError
from podcast_cutter.handlers import PodcastCutterBot
from podcast_cutter.store import Store


def make_feed(feed_id: str = "1", title: str | None = None) -> Feed:
    return Feed(id=feed_id, title=title or f"Feed {feed_id}", author="Author")


def make_episode(
    episode_id: str = "10",
    title: str | None = None,
    duration: int | None = 3600,
) -> Episode:
    return Episode(
        id=episode_id,
        title=title or f"Episode {episode_id}",
        feed_title="Some Show",
        enclosure_url=f"https://cdn.example.com/{episode_id}.mp3",
        duration=duration,
    )


class FakeMessage:
    """Records what the bot said instead of sending it."""

    def __init__(self, text: str = ""):
        self.text = text
        self.replies: list[tuple[str, dict]] = []
        self.edits: list[tuple[str, dict]] = []
        self.sent_audio: dict | None = None
        self.sent_voice: dict | None = None

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))
        return FakeMessage(text)

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))
        self.text = text
        return self

    async def reply_audio(self, **kwargs):
        self.sent_audio = kwargs
        return FakeMessage()

    async def reply_voice(self, **kwargs):
        self.sent_voice = kwargs
        return FakeMessage()

    @property
    def last(self) -> str:
        """The most recent thing shown here, however it was shown."""
        if self.edits:
            return self.edits[-1][0]
        if self.replies:
            return self.replies[-1][0]
        return self.text

    @property
    def last_markup(self):
        for source in (self.edits, self.replies):
            if source:
                return source[-1][1].get("reply_markup")
        return None


class FakeQuery:
    def __init__(self, data: str, message: FakeMessage):
        self.data = data
        self.message = message
        self.answered = False

    async def answer(self, *args, **kwargs):
        self.answered = True

    async def edit_message_text(self, text, **kwargs):
        return await self.message.edit_text(text, **kwargs)


class FakeChat:
    def __init__(self):
        self.actions: list[str] = []

    async def send_action(self, action):
        self.actions.append(str(action))


class FakeUpdate:
    def __init__(self, *, text=None, callback=None, user_id=1):
        self.update_id = 1
        self.message = FakeMessage(text) if text is not None else None
        self._callback_message = FakeMessage()
        self.callback_query = (
            FakeQuery(callback, self._callback_message) if callback else None
        )
        self.effective_user = SimpleNamespace(id=user_id, first_name="Tester")
        self.effective_chat = FakeChat()

    @property
    def effective_message(self) -> FakeMessage | None:
        if self.message is not None:
            return self.message
        return self.callback_query.message if self.callback_query else None

    @property
    def shown(self) -> str:
        message = self.effective_message
        return message.last if message else ""

    @property
    def markup(self):
        message = self.effective_message
        return message.last_markup if message else None


class FakeInlineQuery:
    def __init__(self, query: str):
        self.query = query
        self.answers: list[tuple[list, dict]] = []

    async def answer(self, results, **kwargs):
        self.answers.append((list(results), kwargs))

    @property
    def results(self) -> list:
        return self.answers[-1][0] if self.answers else []

    @property
    def options(self) -> dict:
        return self.answers[-1][1] if self.answers else {}


class FakeInlineUpdate:
    def __init__(self, query: str):
        self.update_id = 1
        self.inline_query = FakeInlineQuery(query)
        self.message = None
        self.callback_query = None
        self.effective_message = None
        self.effective_user = SimpleNamespace(id=1, first_name="Tester")


class FakeContext:
    def __init__(self, args=None):
        self.user_data: dict = {}
        self.args = args or []
        self.error = None


class FakeClient:
    """A Podcast Index stand-in with canned, overridable answers."""

    def __init__(self):
        self.feeds = [make_feed("1"), make_feed("2")]
        self.has_next = False
        self.episodes = [make_episode("10"), make_episode("11")]
        self.trending = [make_feed("7")]
        self.random = make_episode("99")
        self.by_id: dict[str, Episode] = {}
        self.fail_with: Exception | None = None
        #: Raised by :meth:`search_episodes_by_person` alone, so a caller that
        #: falls back to a feed search can be exercised.
        self.person_fail: Exception | None = None
        self.calls: list[str] = []

    def _check(self, name: str) -> None:
        self.calls.append(name)
        if self.fail_with is not None:
            raise self.fail_with

    async def search_feeds(self, query, page=1):
        self._check(f"search_feeds:{query}:{page}")
        return list(self.feeds), self.has_next

    async def list_episodes(self, feed_id):
        self._check(f"list_episodes:{feed_id}")
        return list(self.episodes)

    async def search_episodes_by_person(self, query):
        self._check(f"search_episodes_by_person:{query}")
        if self.person_fail is not None:
            raise self.person_fail
        return list(self.episodes)

    async def trending_feeds(self, limit=10):
        self._check("trending_feeds")
        return list(self.trending)

    async def random_episode(self):
        self._check("random_episode")
        return self.random

    async def get_episode(self, episode_id):
        self._check(f"get_episode:{episode_id}")
        episode = self.by_id.get(episode_id)
        if episode is None:
            raise NotFoundError("That episode is no longer available.")
        return episode

    async def aclose(self):
        pass


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        bot_token="123456:AAH",
        api_key="k",
        api_secret="s",
        episodes_per_page=5,
        podcasts_per_page=5,
        work_dir=tmp_path / "work",
    )


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def store(tmp_path) -> Store:
    instance = Store(tmp_path / "test.db")
    instance.connect()
    yield instance
    instance.close()


@pytest.fixture
def bot(settings, client, store) -> PodcastCutterBot:
    instance = PodcastCutterBot(settings, client, store)
    instance.bot_username = "podcast_cutter_bot"
    return instance


@pytest.fixture
def context() -> FakeContext:
    return FakeContext()
