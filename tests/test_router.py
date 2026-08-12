"""Router behaviour — which tap leads where, and what typing means.

These replace the old ConversationHandler wiring tests. The bot no longer has
states that can lack a handler; instead every update reaches one of two
routers, and what matters is that they always leave the user somewhere usable.
"""

from __future__ import annotations

import time

import pytest

from conftest import FakeUpdate, make_episode, make_feed
from podcast_cutter import keyboards as kb
from podcast_cutter.errors import ApiError, NotFoundError
from podcast_cutter.states import Awaiting, Screen, get_session

pytestmark = pytest.mark.asyncio


async def text(bot, context, message: str) -> FakeUpdate:
    update = FakeUpdate(text=message)
    await bot.on_text(update, context)
    return update


async def tap(bot, context, data: str) -> FakeUpdate:
    update = FakeUpdate(callback=data)
    await bot.on_callback(update, context)
    return update


def session_of(context):
    return get_session(context.user_data)


def payloads(markup) -> list[str]:
    if markup is None:
        return []
    return [b.callback_data for row in markup.inline_keyboard for b in row]


# --------------------------------------------------------------------------


class TestTextRouting:
    async def test_a_bare_message_searches_podcasts(self, bot, context, client):
        # The commonest first interaction: someone just types a name.
        await text(bot, context, "radiolab")
        assert "search_feeds:radiolab:1" in client.calls
        assert session_of(context).current.screen is Screen.FEEDS

    async def test_a_single_hit_skips_the_disambiguation_step(
        self, bot, context, client
    ):
        client.feeds = [make_feed("1")]
        await text(bot, context, "radiolab")
        assert session_of(context).current.screen is Screen.EPISODES

    async def test_menu_buttons_work_from_any_screen(self, bot, context):
        await text(bot, context, "radiolab")
        await text(bot, context, kb.BTN_TRENDING)
        assert session_of(context).current.screen is Screen.TRENDING

    async def test_menu_buttons_are_never_read_as_a_search_term(
        self, bot, context, client
    ):
        await text(bot, context, kb.BTN_SURPRISE)
        assert not any(call.startswith("search_feeds") for call in client.calls)

    async def test_typing_on_a_list_filters_it(self, bot, context, client):
        client.episodes = [
            make_episode("1", "Roman Empire"),
            make_episode("2", "Physics"),
        ]
        client.feeds = [make_feed("1")]
        await text(bot, context, "show")

        await text(bot, context, "roman")

        session = session_of(context)
        assert session.episode_filter == "roman"
        assert [ep.id for ep in session.visible_episodes] == ["1"]

    async def test_a_filter_that_matches_nothing_says_so_and_stays_put(
        self, bot, context, client
    ):
        client.feeds = [make_feed("1")]
        await text(bot, context, "show")
        update = await text(bot, context, "zzzzz")

        assert "Nothing matches" in update.shown
        # Still on the list, with a way to clear the filter.
        assert session_of(context).current.screen is Screen.EPISODES
        assert kb.ACTION_CLEAR_FILTER in payloads(update.markup)

    async def test_person_prompt_routes_the_next_message(self, bot, context, client):
        await tap(bot, context, "menu:person")
        await text(bot, context, "lex fridman")
        assert "search_episodes_by_person:lex fridman" in client.calls
        assert session_of(context).current.screen is Screen.GLOBAL

    async def test_an_empty_message_is_ignored(self, bot, context, client):
        await text(bot, context, "   ")
        assert client.calls == []


class TestIntervalInput:
    async def _open_episode(self, bot, context, duration=3600):
        session = session_of(context)
        session.select_episode(make_episode("10", duration=duration), 60)
        session.awaiting = Awaiting.INTERVAL
        session.go(Screen.INTERVAL)
        return session

    async def test_a_bare_timestamp_makes_a_default_length_clip(self, bot, context):
        session = await self._open_episode(bot, context)
        await text(bot, context, "12:30")

        assert session.clip_start == 750
        assert session.clip_length == 60

    async def test_a_range_is_taken_literally(self, bot, context):
        session = await self._open_episode(bot, context)
        await text(bot, context, "12:30-14:00")

        assert (session.clip_start, session.clip_end) == (750, 840)

    async def test_the_bare_timestamp_keeps_the_chosen_length(self, bot, context):
        session = await self._open_episode(bot, context)
        session.set_length(180, 900)
        await text(bot, context, "20:00")

        assert session.clip_length == 180

    async def test_nonsense_explains_itself_and_keeps_the_screen(self, bot, context):
        await self._open_episode(bot, context)
        update = await text(bot, context, "sometime after lunch")

        assert "⚠️" in update.shown
        # And offers a retry rather than dumping the user out.
        assert kb.ACTION_RETRY in payloads(update.markup)

    async def test_a_timestamp_past_the_end_is_clamped(self, bot, context):
        session = await self._open_episode(bot, context, duration=600)
        await text(bot, context, "50:00")
        assert session.clip_start < 600


class TestNavigationCallbacks:
    async def test_back_returns_to_the_previous_screen(self, bot, context, client):
        client.feeds = [make_feed("1"), make_feed("2")]
        await text(bot, context, "show")
        await tap(bot, context, f"{kb.FEED_PREFIX}:1")
        assert session_of(context).current.screen is Screen.EPISODES

        await tap(bot, context, kb.NAV_BACK)
        assert session_of(context).current.screen is Screen.FEEDS

    async def test_back_from_the_first_screen_lands_on_the_menu(self, bot, context):
        await tap(bot, context, kb.NAV_BACK)
        assert session_of(context).current.screen is Screen.MENU

    async def test_back_into_the_clip_editor_restores_typing(self, bot, context):
        session = session_of(context)
        session.select_episode(make_episode("10"), 60)
        session.go(Screen.INTERVAL)
        session.awaiting = Awaiting.INTERVAL
        session.go(Screen.RESULT)

        await tap(bot, context, kb.NAV_BACK)

        # Otherwise typing a timestamp would silently start a podcast search.
        assert session.awaiting is Awaiting.INTERVAL

    async def test_paging_does_not_deepen_the_history(self, bot, context, client):
        client.episodes = [make_episode(str(i)) for i in range(30)]
        client.feeds = [make_feed("1")]
        await text(bot, context, "show")

        await tap(bot, context, f"{kb.PAGE_PREFIX}:2")
        await tap(bot, context, f"{kb.PAGE_PREFIX}:3")
        assert session_of(context).current.page == 3

        await tap(bot, context, kb.NAV_BACK)
        assert session_of(context).current.screen is not Screen.EPISODES

    async def test_feeds_paging_asks_the_directory_for_that_page(
        self, bot, context, client
    ):
        """The bug this pins: «›» on podcast search results re-rendered page 1
        with a bigger counter, because FEEDS is the one paged screen without a
        cached full list — its pages live in the directory, and flipping one
        has to fetch it."""
        client.feeds = [make_feed(str(i)) for i in range(5)]
        client.has_next = True
        await text(bot, context, "history podcasts")
        assert session_of(context).current.screen is Screen.FEEDS

        client.feeds = [make_feed(str(i)) for i in range(5, 10)]
        await tap(bot, context, f"{kb.PAGE_PREFIX}:2")

        assert "search_feeds:history podcasts:2" in client.calls
        session = session_of(context)
        assert session.current.page == 2
        assert [feed.id for feed in session.feeds] == [str(i) for i in range(5, 10)]

    async def test_feeds_paging_back_to_an_earlier_page_refetches_it(
        self, bot, context, client
    ):
        client.feeds = [make_feed(str(i)) for i in range(5)]
        client.has_next = True
        await text(bot, context, "history podcasts")
        await tap(bot, context, f"{kb.PAGE_PREFIX}:2")

        await tap(bot, context, f"{kb.PAGE_PREFIX}:1")

        assert "search_feeds:history podcasts:1" in client.calls[-1:]
        assert session_of(context).current.page == 1

    async def test_an_expired_session_says_so_before_carrying_on(
        self, bot, context, client
    ):
        """The field report this pins: a user answered the «what was said?»
        prompt after their session had expired, and «winter or tree» silently
        became a podcast search. The search still runs — the text is all we
        have — but the user is told the context is gone first."""
        session = session_of(context)
        session.awaiting = Awaiting.PHRASE
        session.last_active = time.time() - 10_000

        update = await text(bot, context, "winter or tree")

        notices = [reply for reply, _ in update.effective_message.replies]
        assert any("started fresh" in notice for notice in notices)
        assert "search_feeds:winter or tree:1" in client.calls
        assert not session_of(context).was_reset

    async def test_the_page_counter_does_nothing(self, bot, context):
        update = await tap(bot, context, kb.NAV_NOOP)
        assert update.callback_query.answered
        assert update.shown == ""

    async def test_menu_resets_the_flow(self, bot, context, client):
        await text(bot, context, "show")
        await tap(bot, context, kb.NAV_MENU)

        session = session_of(context)
        assert session.current.screen is Screen.MENU
        assert session.history == []

    async def test_every_callback_is_acknowledged(self, bot, context):
        # An unanswered callback leaves a spinner on the user's button.
        for data in (kb.NAV_MENU, kb.NAV_BACK, kb.ACTION_CUT, "garbage"):
            update = await tap(bot, context, data)
            assert update.callback_query.answered, data


class TestClipEditingCallbacks:
    async def _open(self, bot, context):
        session = session_of(context)
        session.select_episode(make_episode("10", duration=3600), 60)
        session.awaiting = Awaiting.INTERVAL
        session.go(Screen.INTERVAL)
        return session

    async def test_length_presets(self, bot, context):
        session = await self._open(bot, context)
        await tap(bot, context, f"{kb.LENGTH_PREFIX}:180")
        assert session.clip_length == 180

    async def test_moving_keeps_the_length(self, bot, context):
        session = await self._open(bot, context)
        session.set_clip(600, 60)
        await tap(bot, context, f"{kb.MOVE_PREFIX}:-15")
        assert (session.clip_start, session.clip_length) == (585, 60)

    async def test_the_format_toggle_flips(self, bot, context):
        session = await self._open(bot, context)
        assert session.as_voice is False
        await tap(bot, context, kb.ACTION_TOGGLE_VOICE)
        assert session.as_voice is True

    async def test_a_malformed_number_is_survived(self, bot, context):
        session = await self._open(bot, context)
        await tap(bot, context, f"{kb.MOVE_PREFIX}:abc")
        assert session.current.screen is Screen.INTERVAL


class TestStaleAndUnknown:
    async def test_an_unknown_payload_offers_a_fresh_start(self, bot, context):
        update = await tap(bot, context, "totally:unknown")
        assert "out of date" in update.shown
        assert session_of(context).current.screen is Screen.MENU

    async def test_a_button_for_a_forgotten_episode_does_not_crash(
        self, bot, context
    ):
        update = await tap(bot, context, f"{kb.EPISODE_PREFIX}:does-not-exist")
        assert "out of date" in update.shown

    async def test_a_stale_session_starts_over_silently(self, bot, context, client):
        await text(bot, context, "show")
        session_of(context).last_active = time.time() - 10_000

        await text(bot, context, "another show")

        session = session_of(context)
        assert session.query == "another show"
        assert session.history == []


class TestErrors:
    async def test_a_directory_outage_is_explained_with_a_retry(
        self, bot, context, client
    ):
        client.fail_with = ApiError("The podcast directory is not responding.")
        update = await text(bot, context, "show")

        assert "not responding" in update.shown
        assert kb.ACTION_RETRY in payloads(update.markup)

    async def test_no_results_is_not_an_error_page_dead_end(
        self, bot, context, client
    ):
        client.fail_with = NotFoundError("No podcasts found.")
        update = await text(bot, context, "asdfgh")

        assert "No podcasts found." in update.shown
        assert kb.NAV_MENU in payloads(update.markup)

    async def test_a_failing_menu_action_still_answers(self, bot, context, client):
        client.fail_with = ApiError("boom")
        update = await tap(bot, context, "menu:trending")
        assert update.callback_query.answered
        assert "boom" in update.shown


class TestDeepLinks:
    async def test_a_shared_link_opens_the_clip_editor(self, bot, context, client):
        client.by_id["555"] = make_episode("555")
        update = FakeUpdate(text="/start")
        context.args = ["ep_555"]

        await bot.cmd_start(update, context)

        session = session_of(context)
        assert session.episode.id == "555"
        assert session.current.screen is Screen.INTERVAL
        assert session.awaiting is Awaiting.INTERVAL

    async def test_a_dead_link_falls_back_to_the_menu(self, bot, context):
        update = FakeUpdate(text="/start")
        context.args = ["ep_nope"]

        await bot.cmd_start(update, context)

        assert session_of(context).current.screen is Screen.MENU

    async def test_plain_start_shows_the_menu(self, bot, context):
        await bot.cmd_start(FakeUpdate(text="/start"), context)
        assert session_of(context).current.screen is Screen.MENU

    async def test_the_welcome_teaches_the_interval_syntax(self, bot, context):
        update = FakeUpdate(text="/start")

        await bot.cmd_start(update, context)

        welcome = update.effective_message.replies[0][0]
        assert "12:30-14:00" in welcome
        assert "@podcast_cutter_bot" in welcome

    async def test_a_campaign_link_records_where_it_came_from(
        self, bot, context, store
    ):
        context.args = ["src_reddit"]

        await bot.cmd_start(FakeUpdate(text="/start"), context)

        assert (await store.stats(24)).sources == [("reddit", 1)]
        assert session_of(context).current.screen is Screen.MENU

    async def test_a_plain_start_has_no_source(self, bot, context, store):
        await bot.cmd_start(FakeUpdate(text="/start"), context)

        assert (await store.stats(24)).sources == []

    async def test_a_campaign_tag_is_normalised_before_it_is_stored(self):
        from podcast_cutter.handlers import campaign_source

        assert campaign_source("src_Reddit") == "reddit"
        assert campaign_source("src_" + "x" * 100) == "x" * 32
        assert campaign_source("src_") is None
        assert campaign_source("ep_555") is None
        assert campaign_source("") is None

    async def test_links_point_at_this_bot(self, bot):
        assert bot.episode_link("42") == (
            "https://t.me/podcast_cutter_bot?start=ep_42"
        )


class TestRecents:
    async def test_opening_an_episode_records_it(self, bot, context, client):
        client.feeds = [make_feed("1")]
        await text(bot, context, "show")
        await tap(bot, context, f"{kb.EPISODE_PREFIX}:10")

        assert [ep.id for ep in session_of(context).recents] == ["10"]

    async def test_the_recent_screen_lists_them(self, bot, context, client):
        client.feeds = [make_feed("1")]
        await text(bot, context, "show")
        await tap(bot, context, f"{kb.EPISODE_PREFIX}:10")

        update = await tap(bot, context, "menu:recent")

        assert session_of(context).current.screen is Screen.RECENT
        assert f"{kb.EPISODE_PREFIX}:10" in payloads(update.markup)

    async def test_they_survive_a_restart(self, bot, context, client, store):
        # The one thing worth persisting: a fresh process, same list.
        client.feeds = [make_feed("1")]
        await text(bot, context, "show")
        await tap(bot, context, f"{kb.EPISODE_PREFIX}:10")

        fresh = type(context)()  # a brand-new, empty user_data
        update = await tap(bot, fresh, "menu:recent")

        assert f"{kb.EPISODE_PREFIX}:10" in payloads(update.markup)

    async def test_an_in_memory_list_is_not_clobbered_by_the_database(
        self, bot, context
    ):
        session = session_of(context)
        session.remember_recent(make_episode("99"))

        await tap(bot, context, "menu:recent")

        assert [ep.id for ep in session.recents] == ["99"]

    async def test_an_empty_recent_screen_still_has_a_way_out(self, bot, context):
        update = await tap(bot, context, "menu:recent")
        assert "Nothing here yet" in update.shown
        assert kb.NAV_MENU in payloads(update.markup)
