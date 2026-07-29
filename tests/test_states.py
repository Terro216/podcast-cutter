import time

import pytest

from podcast_cutter.api import Episode, Feed
from podcast_cutter.states import (
    MAX_RECENTS,
    Awaiting,
    Screen,
    Session,
    get_session,
    reset_session,
)


def feed(feed_id: str) -> Feed:
    return Feed(id=feed_id, title=f"Feed {feed_id}", author="Author")


def episode(episode_id: str, title: str = "Episode", duration=None) -> Episode:
    return Episode(
        id=episode_id,
        title=title,
        feed_title="Feed",
        enclosure_url="https://example.com/a.mp3",
        duration=duration,
    )


class TestNavigation:
    def test_back_returns_to_the_previous_screen(self):
        session = Session()
        session.go(Screen.MENU)
        session.go(Screen.FEEDS)
        session.go(Screen.EPISODES)

        assert session.back().screen is Screen.FEEDS
        assert session.back().screen is Screen.MENU
        assert session.back() is None

    def test_paging_does_not_grow_the_history(self):
        # Otherwise Back walks the user backwards through every page they
        # scrolled, which feels broken.
        session = Session()
        session.go(Screen.MENU)
        session.go(Screen.EPISODES)
        for page in range(2, 8):
            session.replace(Screen.EPISODES, page)

        assert session.current.page == 7
        assert session.back().screen is Screen.MENU

    def test_revisiting_the_same_screen_does_not_stack_it(self):
        session = Session()
        session.go(Screen.MENU)
        session.go(Screen.FEEDS)
        session.go(Screen.FEEDS)

        assert session.back().screen is Screen.MENU

    def test_history_is_bounded(self):
        session = Session()
        for _ in range(200):
            session.go(Screen.FEEDS)
            session.go(Screen.EPISODES)
        assert len(session.history) <= 12

    def test_reset_clears_navigation_and_awaiting(self):
        session = Session()
        session.go(Screen.INTERVAL)
        session.awaiting = Awaiting.INTERVAL

        session.reset_navigation()

        assert session.current is None
        assert session.history == []
        assert session.awaiting is Awaiting.NOTHING


class TestStaleness:
    def test_a_fresh_session_is_not_stale(self):
        assert Session().is_stale(60) is False

    def test_an_old_session_is_stale(self):
        session = Session()
        session.last_active = time.time() - 120
        assert session.is_stale(60) is True

    def test_touch_revives_it(self):
        session = Session()
        session.last_active = time.time() - 120
        session.touch()
        assert session.is_stale(60) is False


class TestClipEditing:
    def test_moving_keeps_the_length(self):
        session = Session()
        session.set_clip(100, 60)
        session.move_clip(15)
        assert (session.clip_start, session.clip_length) == (115, 60)

    def test_cannot_move_before_the_start(self):
        session = Session()
        session.set_clip(10, 60)
        session.move_clip(-60)
        assert session.clip_start == 0

    def test_length_presets_respect_the_maximum(self):
        session = Session()
        session.set_length(9999, max_length=900)
        assert session.clip_length == 900

    def test_clip_is_clamped_to_the_episode(self):
        session = Session()
        session.select_episode(episode("1", duration=100))
        session.set_clip(90, 60)
        session.clamp()

        assert session.clip_start == 90
        assert session.clip_end <= 100

    def test_start_cannot_pass_the_end_of_the_episode(self):
        session = Session()
        session.select_episode(episode("1", duration=100))
        session.set_clip(0, 30)
        session.move_clip(9999)

        assert session.clip_start < 100
        assert session.clip_length >= 1

    def test_unknown_duration_is_left_alone(self):
        # Most feeds omit it; clamping to a guess would be worse than nothing.
        session = Session()
        session.select_episode(episode("1", duration=None))
        session.set_clip(50_000, 60)
        session.clamp()
        assert session.clip_start == 50_000

    def test_selecting_an_episode_resets_the_clip(self):
        session = Session()
        session.set_clip(500, 300)
        session.select_episode(episode("2"), default_length=60)
        assert (session.clip_start, session.clip_length) == (0, 60)


class TestRecents:
    def test_most_recent_first_without_duplicates(self):
        session = Session()
        session.remember_recent(episode("1"))
        session.remember_recent(episode("2"))
        session.remember_recent(episode("1"))

        assert [ep.id for ep in session.recents] == ["1", "2"]

    def test_bounded(self):
        session = Session()
        for index in range(MAX_RECENTS + 10):
            session.remember_recent(episode(str(index)))
        assert len(session.recents) == MAX_RECENTS

    def test_survive_a_reset(self):
        # They are the user's history, not part of the abandoned flow.
        user_data = {}
        session = get_session(user_data)
        session.remember_recent(episode("1"))

        fresh = reset_session(user_data)

        assert [ep.id for ep in fresh.recents] == ["1"]
        assert fresh.query == ""


class TestEpisodeFilter:
    def test_narrows_without_discarding(self):
        session = Session()
        session.set_episodes([episode("1", "Roman Empire"), episode("2", "Physics")])
        session.episode_filter = "roman"

        assert [ep.id for ep in session.visible_episodes] == ["1"]
        # The full list is still there, so clearing the filter restores it.
        assert len(session.episodes) == 2

    def test_is_case_insensitive(self):
        session = Session()
        session.set_episodes([episode("1", "Roman Empire")])
        session.episode_filter = "ROMAN"
        assert len(session.visible_episodes) == 1

    def test_new_episodes_clear_it(self):
        session = Session()
        session.set_episodes([episode("1", "A")])
        session.episode_filter = "zzz"
        session.set_episodes([episode("2", "B")])
        assert session.episode_filter == ""


class TestLookups:
    def test_finds_episodes_in_recents_too(self):
        # A button on an old message must still resolve after the list changed.
        session = Session()
        session.remember_recent(episode("99"))
        session.set_episodes([episode("1")])
        assert session.find_episode("99") is not None

    def test_remembers_feeds_across_pages(self):
        session = Session()
        session.remember_feeds([feed("1")])
        session.remember_feeds([feed("2")])
        assert session.find_feed("1") is not None
        assert session.find_feed("nope") is None

    def test_switching_podcasts_drops_cached_episodes(self):
        session = Session()
        session.select_feed(feed("1"))
        session.set_episodes([episode("100")])
        session.select_feed(feed("2"))
        assert session.episodes == []


class TestPaging:
    def test_reports_the_page_count(self):
        window, page, pages = Session.page_of(list(range(12)), 1, 5)
        assert (window, page, pages) == ([0, 1, 2, 3, 4], 1, 3)

    def test_last_page_is_partial(self):
        window, page, pages = Session.page_of(list(range(12)), 3, 5)
        assert (window, page, pages) == ([10, 11], 3, 3)

    @pytest.mark.parametrize("requested", [0, -5, 99])
    def test_out_of_range_pages_are_clamped(self, requested):
        window, page, pages = Session.page_of(list(range(12)), requested, 5)
        assert 1 <= page <= pages
        assert window

    def test_empty_list(self):
        assert Session.page_of([], 1, 5) == ([], 1, 1)


class TestSessionStorage:
    def test_stable_across_calls(self):
        user_data = {}
        assert get_session(user_data) is get_session(user_data)

    def test_recovers_from_a_corrupted_slot(self):
        assert isinstance(get_session({"session": "not a session"}), Session)
