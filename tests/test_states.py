from podcast_cutter.api import Episode, Feed
from podcast_cutter.states import Session, State, get_session, reset_session


def feed(feed_id: str) -> Feed:
    return Feed(id=feed_id, title=f"Feed {feed_id}", author="Author")


def episode(episode_id: str, title: str = "Episode") -> Episode:
    return Episode(
        id=episode_id,
        title=title,
        feed_title="Feed",
        enclosure_url="https://example.com/a.mp3",
        duration=None,
    )


class TestStateValues:
    def test_do_not_collide_with_conversation_sentinels(self):
        # ConversationHandler uses -1 (END), -2 (TIMEOUT) and -3 (WAITING);
        # a state sharing one of those values silently breaks the flow.
        assert all(state > 0 for state in State)

    def test_are_distinct(self):
        assert len({int(state) for state in State}) == len(list(State))


class TestPaging:
    def test_splits_into_pages(self):
        session = Session()
        items = list(range(12))

        window, has_prev, has_next = session.page_of(items, 1, 5)
        assert (window, has_prev, has_next) == ([0, 1, 2, 3, 4], False, True)

        window, has_prev, has_next = session.page_of(items, 2, 5)
        assert (window, has_prev, has_next) == ([5, 6, 7, 8, 9], True, True)

        window, has_prev, has_next = session.page_of(items, 3, 5)
        assert (window, has_prev, has_next) == ([10, 11], True, False)

    def test_first_item_is_never_dropped(self):
        # The old feed pagination asked for `per_page * page + 1` results and
        # then took the *last* `per_page` of them, so the top search hit never
        # appeared on page one.
        window, _, _ = Session().page_of(["top", "b", "c"], 1, 2)
        assert window[0] == "top"

    def test_page_past_the_end_is_empty_not_an_error(self):
        window, has_prev, has_next = Session().page_of([1, 2], 9, 5)
        assert window == [] and has_next is False

    def test_zero_and_negative_pages_clamp_to_the_first(self):
        for page in (0, -3):
            window, has_prev, _ = Session().page_of([1, 2, 3], page, 2)
            assert window == [1, 2] and has_prev is False


class TestSessionLookups:
    def test_remembers_feeds_across_pages(self):
        session = Session()
        session.remember_feeds([feed("1"), feed("2")])
        session.remember_feeds([feed("3")])

        # A button on the first page's message must still resolve after the
        # user has paged forward.
        assert session.find_feed("1") is not None
        assert session.find_feed("3") is not None
        assert session.find_feed("99") is None

    def test_find_episode(self):
        session = Session()
        session.set_episodes([episode("10"), episode("11")])
        assert session.find_episode("11").id == "11"
        assert session.find_episode("nope") is None


class TestFeedSwitching:
    def test_choosing_a_different_podcast_drops_cached_episodes(self):
        session = Session()
        session.select_feed(feed("1"))
        session.set_episodes([episode("100")])
        session.episode_page = 3

        session.select_feed(feed("2"))

        assert session.episodes == []
        assert session.episode_page == 1

    def test_reselecting_the_same_podcast_keeps_the_cache(self):
        session = Session()
        session.select_feed(feed("1"))
        session.set_episodes([episode("100")])

        session.select_feed(feed("1"))

        assert len(session.episodes) == 1

    def test_setting_episodes_rewinds_paging(self):
        session = Session()
        session.episode_page = 4
        session.episode = episode("1")

        session.set_episodes([episode("2")])

        assert session.episode_page == 1
        assert session.episode is None


class TestSessionStorage:
    def test_get_creates_on_first_use(self):
        user_data = {}
        assert isinstance(get_session(user_data), Session)

    def test_get_is_stable(self):
        user_data = {}
        assert get_session(user_data) is get_session(user_data)

    def test_reset_discards_everything(self):
        user_data = {"stale": "value"}
        session = get_session(user_data)
        session.query = "old"

        fresh = reset_session(user_data)

        assert fresh is not session
        assert fresh.query == ""
        assert "stale" not in user_data

    def test_recovers_from_a_corrupted_slot(self):
        # e.g. a session pickled by an older version of the code.
        user_data = {"session": "not a session"}
        assert isinstance(get_session(user_data), Session)
