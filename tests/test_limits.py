"""The per-user input budgets — the thing between a `src_` link in a busy
chat and a bot that stops answering everyone."""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from conftest import FakeUpdate, make_episode
from podcast_cutter import keyboards as kb
from podcast_cutter.limits import Budget
from podcast_cutter.states import Awaiting, Screen, get_session


class TestBudget:
    def test_allows_up_to_the_count(self):
        budget = Budget(3, 60)
        assert [budget.allow(1, now=t) for t in (0, 1, 2)] == [True, True, True]

    def test_refuses_the_one_over(self):
        budget = Budget(3, 60)
        for t in (0, 1, 2):
            budget.allow(1, now=t)
        assert budget.allow(1, now=3) is False

    def test_the_window_slides(self):
        budget = Budget(2, 60)
        budget.allow(1, now=0)
        budget.allow(1, now=30)
        assert budget.allow(1, now=59) is False
        # The event at t=0 has left the window; room again.
        assert budget.allow(1, now=61) is True

    def test_users_do_not_share_a_budget(self):
        """The point of per-user limits: one pushy afternoon must not spend
        anybody else's."""
        budget = Budget(1, 60)
        assert budget.allow(1, now=0) is True
        assert budget.allow(2, now=0) is True
        assert budget.allow(1, now=1) is False

    def test_a_refusal_is_not_charged(self):
        """Refused attempts must not extend the lockout, or a user who keeps
        trying never gets back in."""
        budget = Budget(1, 60)
        budget.allow(1, now=0)
        for t in range(1, 59):
            budget.allow(1, now=t)
        assert budget.allow(1, now=61) is True

    def test_zero_means_off(self):
        budget = Budget(0, 60)
        assert all(budget.allow(1, now=t) for t in range(100))


@pytest.fixture
def ready(bot, context):
    """A session on the clip editor, the same shape test_cutting_flow uses."""
    session = get_session(context.user_data)
    session.select_episode(make_episode("10", duration=3600), 60)
    session.set_clip(600, 60)
    session.awaiting = Awaiting.INTERVAL
    session.go(Screen.INTERVAL)
    return session


class TestWiring:
    pytestmark = pytest.mark.asyncio

    async def test_the_input_budget_answers_once_and_goes_quiet(self, bot, context):
        bot._input_budget = Budget(1, 60)
        await bot.on_text(FakeUpdate(text="podcasts about bees"), context)

        second = FakeUpdate(text="podcasts about wasps")
        await bot.on_text(second, context)
        assert "🐢" in second.effective_message.last

        # The refusal itself is throttled: a flood of over-budget messages
        # must not become a flood of scolding replies.
        third = FakeUpdate(text="podcasts about hornets")
        await bot.on_text(third, context)
        assert third.effective_message.replies == []

    async def test_admins_are_exempt(self, bot, context):
        bot.settings = dataclasses.replace(
            bot.settings, admin_ids=frozenset({1})
        )
        bot._input_budget = Budget(1, 60)
        for text in ("one", "two", "three"):
            update = FakeUpdate(text=text)
            await bot.on_text(update, context)
            assert "🐢" not in (update.effective_message.last or "")

    async def test_the_hourly_cut_budget_refuses_politely(
        self, bot, context, ready, monkeypatch
    ):
        async def fake_perform(update, session, episode, interval, editor):
            update.effective_message.sent_audio = {"stub": True}

        monkeypatch.setattr(bot, "_perform_cut", fake_perform)
        bot._cut_budget = Budget(1, 3600)

        first = FakeUpdate(callback=kb.ACTION_CUT)
        await bot.on_callback(first, context)
        assert first.effective_message.sent_audio is not None

        second = FakeUpdate(callback=kb.ACTION_CUT)
        await bot.on_callback(second, context)
        assert second.effective_message.sent_audio is None
        assert "🐢" in second.effective_message.last

    async def test_two_quick_taps_cut_once(self, bot, context, ready, monkeypatch):
        """The race `concurrent_updates` exposes: the busy flag is claimed
        before the first await, so the second of two simultaneous taps is
        refused instead of starting a second job."""
        calls = []
        hold = asyncio.Event()

        async def slow_perform(update, session, episode, interval, editor):
            calls.append(1)
            await hold.wait()

        monkeypatch.setattr(bot, "_perform_cut", slow_perform)

        one = asyncio.create_task(
            bot.on_callback(FakeUpdate(callback=kb.ACTION_CUT), context)
        )
        refused = FakeUpdate(callback=kb.ACTION_CUT)
        two = asyncio.create_task(bot.on_callback(refused, context))
        await asyncio.sleep(0.01)
        hold.set()
        await asyncio.gather(one, two)

        assert calls == [1]
        assert "Still working" in refused.effective_message.last
