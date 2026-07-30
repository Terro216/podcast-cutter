"""The /stats panel and who may see it."""

from __future__ import annotations

import dataclasses

import pytest

from conftest import FakeContext, FakeUpdate, make_episode
from podcast_cutter import handlers as handlers_mod
from podcast_cutter import keyboards as kb
from podcast_cutter import screens
from podcast_cutter.audio import CutResult
from podcast_cutter.errors import AudioError
from podcast_cutter.handlers import PodcastCutterBot
from podcast_cutter.states import Screen, get_session
from podcast_cutter.store import Event, Stats

pytestmark = pytest.mark.asyncio

ADMIN = 4242


@pytest.fixture
def admin_bot(settings, client, store) -> PodcastCutterBot:
    admin_settings = dataclasses.replace(settings, admin_ids=frozenset({ADMIN}))
    instance = PodcastCutterBot(admin_settings, client, store)
    instance.bot_username = "podcast_cutter_bot"
    return instance


class TestAccess:
    async def test_an_admin_gets_the_panel(self, admin_bot, store):
        await store.record(Event(action="cut", user_id=ADMIN, outcome="ok", ms=1200))
        update = FakeUpdate(text="/stats", user_id=ADMIN)

        await admin_bot.cmd_stats(update, FakeContext())

        assert "Podcast Cutter" in update.shown
        assert "Last 24h" in update.shown

    async def test_a_stranger_is_told_nothing_about_it(self, admin_bot):
        # Saying "you are not an admin" would confirm the command exists.
        update = FakeUpdate(text="/stats", user_id=9999)

        await admin_bot.cmd_stats(update, FakeContext())

        assert "stats" not in update.shown.lower()
        assert "don't know that command" in update.shown

    async def test_nobody_qualifies_when_admin_ids_is_unset(self, bot):
        update = FakeUpdate(text="/stats", user_id=ADMIN)
        await bot.cmd_stats(update, FakeContext())
        assert "don't know that command" in update.shown

    async def test_the_callers_id_is_logged_so_the_owner_can_find_it(
        self, bot, caplog
    ):
        with caplog.at_level("INFO"):
            await bot.cmd_stats(FakeUpdate(text="/stats", user_id=777), FakeContext())
        assert "777" in caplog.text
        assert "ADMIN_IDS" in caplog.text


class TestPanel:
    def _stats(self, **kwargs) -> Stats:
        base = {
            "window_hours": 24,
            "cuts_ok": 10,
            "cuts_failed": 2,
            "unique_users": 4,
            "voice_share": 0.25,
            "median_ms": 3400,
            "slowest_ms": 12000,
            "total_bytes": 5 * 1024 * 1024,
        }
        return Stats(**{**base, **kwargs})

    async def test_reports_the_success_rate(self):
        view = screens.stats(self._stats(), self._stats(), 1024)
        assert "10 ok" in view.text and "2 failed" in view.text
        assert "83%" in view.text

    async def test_reports_timing_in_seconds(self):
        view = screens.stats(self._stats(), self._stats(), 1024)
        assert "3.4s" in view.text

    async def test_lists_failures_and_top_podcasts(self):
        week = self._stats(
            failures=[("AudioError", 3)], top_podcasts=[("Radiolab", 7)]
        )
        view = screens.stats(self._stats(), week, 1024)
        assert "AudioError × 3" in view.text
        assert "Radiolab × 7" in view.text

    async def test_lists_where_people_came_from(self):
        week = self._stats(sources=[("reddit", 12)])
        view = screens.stats(self._stats(), week, 1024)
        assert "reddit × 12" in view.text

    async def test_says_nothing_about_sources_when_there_are_none(self):
        view = screens.stats(self._stats(), self._stats(), 1024)
        assert "came from" not in view.text

    async def test_an_empty_journal_renders_without_dividing_by_zero(self):
        empty = Stats(window_hours=24)
        view = screens.stats(empty, empty, 0)
        assert "0 ok" in view.text
        assert "—" in view.text

    async def test_escapes_podcast_titles(self):
        # A show called "Tom & Jerry <Live>" must not break the message.
        week = self._stats(top_podcasts=[("Tom & Jerry <Live>", 1)])
        view = screens.stats(self._stats(), week, 0)
        assert "&amp;" in view.text and "<Live>" not in view.text

    async def test_shows_how_big_the_journal_has_grown(self):
        view = screens.stats(self._stats(), self._stats(), 3 * 1024 * 1024)
        assert "3.0 MB" in view.text or "3 MB" in view.text


class TestJournalWiring:
    async def test_a_cut_is_recorded_with_its_outcome(
        self, bot, context, store, monkeypatch
    ):
        async def fake_cut(url, interval, workdir, settings, **kwargs):
            workdir.mkdir(parents=True, exist_ok=True)
            path = workdir / "cut.mp3"
            path.write_bytes(b"x" * 4096)
            return CutResult(path=path, size=4096, transcoded=False)

        monkeypatch.setattr(handlers_mod, "cut_episode", fake_cut)

        session = get_session(context.user_data)
        session.select_episode(make_episode("10", duration=3600), 60)
        session.go(Screen.INTERVAL)

        await bot.on_callback(FakeUpdate(callback=kb.ACTION_CUT), context)

        stats = await store.stats(24)
        assert stats.cuts_ok == 1
        assert stats.total_bytes == 4096

    async def test_a_failed_cut_is_recorded_too(
        self, bot, context, store, monkeypatch
    ):
        async def failing(url, interval, workdir, settings, **kwargs):
            raise AudioError("nope")

        monkeypatch.setattr(handlers_mod, "cut_episode", failing)

        session = get_session(context.user_data)
        session.select_episode(make_episode("10", duration=3600), 60)
        session.go(Screen.INTERVAL)

        await bot.on_callback(FakeUpdate(callback=kb.ACTION_CUT), context)

        stats = await store.stats(24)
        assert stats.cuts_failed == 1
        assert ("audio_failed", 1) in stats.failures

    async def test_searches_are_journalled(self, bot, context, store):
        await bot.on_text(FakeUpdate(text="radiolab"), context)
        actions = dict((await store.stats(24)).actions)
        assert actions.get("search") == 1

    async def test_a_bot_without_a_journal_still_works(self, settings, client):
        # store=None must degrade to "no statistics", not to a crash.
        plain = PodcastCutterBot(settings, client)
        update = FakeUpdate(text="radiolab")
        await plain.on_text(update, FakeContext())
        assert update.shown
