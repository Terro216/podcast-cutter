from __future__ import annotations

from dataclasses import replace

import pytest

from conftest import FakeContext, FakeUpdate, make_episode
from podcast_cutter.handlers import PodcastCutterBot
from podcast_cutter.keyboards import LEGAL_ACCEPT, LEGAL_DECLINE
from podcast_cutter.states import Screen, get_session

pytestmark = pytest.mark.asyncio


class TestAcceptance:
    async def test_first_start_requires_explicit_acceptance(self, bot, store):
        context = FakeContext()
        update = FakeUpdate(text="/start", user_id=2)

        await bot.cmd_start(update, context)

        assert "Terms of Use" in update.shown
        assert not await store.terms_accepted(2, bot.settings.terms_version)

    async def test_acceptance_is_versioned_and_opens_the_menu(self, bot, store):
        context = FakeContext()
        await bot.cmd_start(FakeUpdate(text="/start", user_id=2), context)

        update = FakeUpdate(callback=LEGAL_ACCEPT, user_id=2)
        await bot.on_callback(update, context)

        assert await store.terms_accepted(2, bot.settings.terms_version)
        assert get_session(context.user_data).current.screen is Screen.MENU

    async def test_declining_does_not_enable_processing(self, bot, store):
        context = FakeContext()
        update = FakeUpdate(callback=LEGAL_DECLINE, user_id=2)

        await bot.on_callback(update, context)

        assert "will not search" in update.shown
        assert not await store.terms_accepted(2, bot.settings.terms_version)


class TestUserDataCommands:
    async def test_mydata_reports_the_persisted_categories(
        self, bot, store
    ):
        await store.remember_recent(1, make_episode("42"))
        update = FakeUpdate(text="/mydata")

        await bot.cmd_mydata(update, FakeContext())

        assert "Recent episodes: 1" in update.shown
        assert bot.settings.terms_version in update.shown

    async def test_delete_me_requires_confirmation(self, bot, store):
        update = FakeUpdate(text="/delete_me")
        await bot.cmd_delete_me(update, FakeContext())

        assert "/delete_me confirm" in update.shown
        assert await store.terms_accepted(1, bot.settings.terms_version)

    async def test_confirmed_deletion_removes_all_user_rows(self, bot, store):
        await store.remember_recent(1, make_episode("42"))
        context = FakeContext(args=["confirm"])

        await bot.cmd_delete_me(FakeUpdate(text="/delete_me confirm"), context)

        data = await store.user_data(1)
        assert data["profile"] is None
        assert data["recents"] == []
        assert data["events"] == 0
        assert data["asr_jobs"] == 0


class TestBlocklist:
    async def test_blocked_feed_cannot_be_opened(self, bot, context):
        bot.settings = replace(
            bot.settings, podcast_blocklist=frozenset({"1"})
        )
        session = get_session(context.user_data)
        session.set_episodes([make_episode("10")])
        session.go(Screen.GLOBAL)

        update = FakeUpdate(callback="ep:10")
        await bot.on_callback(update, context)

        assert "unavailable for processing" in update.shown
        assert session.episode is None

    async def test_blocklist_is_rechecked_by_the_durable_queue(
        self, settings, client, store, indexer
    ):
        blocked = replace(settings, podcast_blocklist=frozenset({"1"}))
        bot = PodcastCutterBot(blocked, client, store, indexer)
        ticket = await bot.listening.submit(
            make_episode("10"), user_id=7, chat_id=7
        )

        with pytest.raises(Exception, match="unavailable for processing"):
            await ticket.done
        await bot.listening.stop()
