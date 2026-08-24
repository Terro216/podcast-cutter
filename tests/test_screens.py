"""Rendering: what each screen actually says.

Screens are pure functions of session state, so the whole interface can be
checked without a bot — including the things that silently break a message,
like an unescaped ``&`` in a podcast title.
"""

from __future__ import annotations

import pytest

from conftest import make_episode, make_feed
from podcast_cutter import keyboards as kb
from podcast_cutter import screens
from podcast_cutter.states import Screen, Session


def payloads(view) -> list[str]:
    if view.keyboard is None:
        return []
    return [b.callback_data for row in view.keyboard.inline_keyboard for b in row]


def session_on(screen: Screen, page: int = 1) -> Session:
    session = Session()
    session.go(screen, page)
    return session


ALL_SCREENS = [
    Screen.MENU,
    Screen.ASK_PODCAST,
    Screen.FEEDS,
    Screen.EPISODES,
    Screen.ASK_PERSON,
    Screen.GLOBAL,
    Screen.TRENDING,
    Screen.RECENT,
    Screen.INTERVAL,
    Screen.RESULT,
]


class TestEveryScreenRenders:
    @pytest.mark.parametrize("screen", ALL_SCREENS, ids=lambda s: s.name)
    def test_never_returns_an_empty_message(self, bot, screen):
        # Telegram rejects an empty message outright.
        session = session_on(screen)
        session.select_episode(make_episode("1"), 60)
        session.remember_feeds([make_feed("1")])
        session.set_episodes([make_episode("1")])
        session.go(screen)

        view = bot.view_for(session)
        assert view.text.strip()

    @pytest.mark.parametrize("screen", ALL_SCREENS, ids=lambda s: s.name)
    def test_always_offers_somewhere_to_go(self, bot, screen):
        session = session_on(screen)
        session.select_episode(make_episode("1"), 60)
        session.set_episodes([make_episode("1")])
        session.go(screen)

        view = bot.view_for(session)
        assert view.keyboard is not None, f"{screen.name} is a dead end"


class TestEscaping:
    def test_podcast_titles_with_markup_characters_are_escaped(self, bot, settings):
        # "Rock & Roll <Live>" would otherwise make Telegram reject the message.
        session = Session()
        session.set_episodes([make_episode("1", "Rock & Roll <Live>")])
        session.go(Screen.EPISODES)
        session.episode_filter = "Rock & Roll <Live>"

        view = screens.episodes(session, settings)

        assert "&amp;" in view.text
        assert "<Live>" not in view.text

    def test_the_breadcrumb_escapes_the_query(self, settings):
        session = Session()
        session.query = "a & b"
        session.go(Screen.FEEDS)
        assert "&amp;" in screens.breadcrumb(session)

    def test_episode_headings_are_escaped(self, settings):
        session = Session()
        session.select_episode(make_episode("1", "Tom & Jerry <2>"), 60)
        session.go(Screen.INTERVAL)

        view = screens.interval(session, settings)
        assert "&amp;" in view.text and "<2>" not in view.text


class TestBreadcrumb:
    def test_shows_the_path_through_a_search(self, settings):
        session = Session()
        session.query = "radiolab"
        session.select_feed(make_feed("1", "Radiolab"))
        session.go(Screen.EPISODES)

        crumb = screens.breadcrumb(session)
        assert "Search" in crumb and "Radiolab" in crumb

    def test_is_absent_on_the_menu(self):
        assert screens.breadcrumb(session_on(Screen.MENU)) == ""

    def test_survives_a_deep_link_with_no_search_behind_it(self, settings):
        # Opened from a shared link: there is no feed, only the episode.
        session = Session()
        session.select_episode(make_episode("1"), 60)
        session.go(Screen.INTERVAL)
        assert "Some Show" in screens.breadcrumb(session)


class TestIntervalScreen:
    def _session(self, duration=3600):
        session = Session()
        session.select_episode(make_episode("1", duration=duration), 60)
        session.go(Screen.INTERVAL)
        return session

    def test_shows_the_current_range_and_length(self, settings):
        session = self._session()
        session.set_clip(750, 90)

        view = screens.interval(session, settings)

        assert "12:30" in view.text
        assert "14:00" in view.text
        assert "1:30" in view.text

    def test_shows_the_episode_length(self, settings):
        view = screens.interval(self._session(duration=6440), settings)
        assert "1:47:20" in view.text

    def test_explains_both_input_forms(self, settings):
        view = screens.interval(self._session(), settings)
        assert "12:30" in view.text and "12:30-14:00" in view.text

    def test_explains_format_confirmation_and_word_search(self, settings):
        view = screens.interval(self._session(), settings)
        assert "delivery format" in view.text
        assert "blue" in view.text and "Cut" in view.text
        assert "keyword or phrase" in view.text

    def test_offers_the_cut_button(self, settings):
        assert kb.ACTION_CUT in payloads(screens.interval(self._session(), settings))


class TestResultScreen:
    def test_has_one_plain_instruction_without_editor_jargon(self, settings):
        session = Session(language="ru")
        session.select_episode(make_episode("1"), 60)
        session.go(Screen.RESULT)

        text = screens.result(session, "podcast_cutter_bot", settings).text

        assert text.count("Ничего не отправится") == 1
        assert "редактор" not in text.lower()
        assert "Хотите другую версию" not in text


class TestLists:
    def test_pagination_appears_only_when_needed(self, settings):
        session = Session()
        session.set_episodes([make_episode(str(i)) for i in range(3)])
        session.go(Screen.EPISODES)

        assert f"{kb.PAGE_PREFIX}:2" not in payloads(
            screens.episodes(session, settings)
        )

        session.set_episodes([make_episode(str(i)) for i in range(30)])
        assert f"{kb.PAGE_PREFIX}:2" in payloads(screens.episodes(session, settings))

    def test_the_count_is_reported(self, settings):
        session = Session()
        session.set_episodes([make_episode(str(i)) for i in range(17)])
        session.go(Screen.EPISODES)
        assert "17" in screens.episodes(session, settings).text

    def test_a_filter_reports_how_much_it_narrowed(self, settings):
        session = Session()
        session.set_episodes(
            [make_episode("1", "Roman"), make_episode("2", "Physics")]
        )
        session.go(Screen.EPISODES)
        session.episode_filter = "roman"

        text = screens.episodes(session, settings).text
        assert "1" in text and "2" in text

    def test_the_clear_filter_button_appears_only_when_filtering(self, settings):
        session = Session()
        session.set_episodes([make_episode("1")])
        session.go(Screen.EPISODES)

        assert kb.ACTION_CLEAR_FILTER not in payloads(
            screens.episodes(session, settings)
        )
        session.episode_filter = "x"
        assert kb.ACTION_CLEAR_FILTER in payloads(screens.episodes(session, settings))

    def test_full_episode_titles_live_in_text_and_buttons_drop_feed_prefix(
        self, settings
    ):
        session = Session()
        title = (
            "Some Show - Episode 3.83.3. - The important title that used to "
            "disappear from the Telegram button"
        )
        session.set_episodes([make_episode("1", title)])
        session.go(Screen.EPISODES)

        view = screens.episodes(session, settings)
        button = view.keyboard.inline_keyboard[0][0]
        assert "The important title" in view.text
        assert button.text.startswith("#1 · Episode 3.83.3.")
        assert not button.text.startswith("Some Show")


class TestMenu:
    def test_hides_recent_until_there_is_something_in_it(self):
        assert "menu:recent" not in payloads(screens.menu(Session()))

        session = Session()
        session.remember_recent(make_episode("1"))
        assert "menu:recent" in payloads(screens.menu(session))


class TestFailure:
    def test_shows_the_message_and_a_retry(self):
        view = screens.failure("The host refused the download.")
        assert "refused" in view.text
        assert kb.ACTION_RETRY in payloads(view)

    def test_escapes_the_message(self):
        assert "&amp;" in screens.failure("a & b").text
