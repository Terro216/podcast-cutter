"""The journal and the recent list.

Storage is deliberately best-effort: a lost statistics row must never cost a
user their clip. Several tests here pin exactly that.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from conftest import make_episode
from podcast_cutter.store import SCHEMA_VERSION, Event, Store

pytestmark = pytest.mark.asyncio


async def add_cut(store, *, outcome="ok", user=1, ms=1000, voice=False,
                  feed="Some Show", size=1000, at=None):
    await store.record(
        Event(
            action="cut",
            user_id=user,
            outcome=outcome,
            episode_id="e1",
            feed_title=feed,
            episode_title="Ep",
            start_s=10,
            length_s=60,
            as_voice=voice,
            size_bytes=size,
            ms=ms,
        )
    )
    if at is not None:
        # Backdate the row we just wrote so retention can be exercised.
        store._execute(
            "UPDATE events SET at = ? WHERE id = (SELECT max(id) FROM events)",
            (at,),
        )


class TestSchema:
    async def test_records_its_version(self, store):
        version = store._execute("PRAGMA user_version")[0][0]
        assert version == SCHEMA_VERSION

    async def test_uses_wal(self, store):
        mode = store._execute("PRAGMA journal_mode")[0][0]
        assert mode.lower() == "wal"

    async def test_reopening_an_existing_file_is_safe(self, tmp_path):
        path = tmp_path / "reopen.db"
        first = Store(path)
        first.connect()
        await first.record(Event(action="start", user_id=1))
        first.close()

        second = Store(path)
        second.connect()
        try:
            assert (await second.stats(24)).unique_users == 1
        finally:
            second.close()

    async def test_creates_missing_directories(self, tmp_path):
        store = Store(tmp_path / "nested" / "deep" / "x.db")
        store.connect()
        store.close()
        assert (tmp_path / "nested" / "deep" / "x.db").exists()


class TestJournal:
    async def test_records_a_cut(self, store):
        await add_cut(store)
        stats = await store.stats(24)
        assert stats.cuts_ok == 1 and stats.cuts_failed == 0

    async def test_counts_failures_separately(self, store):
        await add_cut(store, outcome="ok")
        await add_cut(store, outcome="AudioError")
        stats = await store.stats(24)

        assert (stats.cuts_ok, stats.cuts_failed) == (1, 1)
        assert stats.success_rate == 0.5
        assert ("AudioError", 1) in stats.failures

    async def test_a_write_failure_never_propagates(self, store):
        # A statistics row is not worth failing a user's request over.
        store.close()
        await store.record(Event(action="cut", user_id=1))

    async def test_long_text_is_truncated_rather_than_rejected(self, store):
        await store.record(
            Event(action="cut", user_id=1, outcome="ok", feed_title="x" * 5000)
        )
        row = store._execute("SELECT feed_title FROM events")[0]
        assert len(row["feed_title"]) <= 200

    async def test_titles_with_quotes_are_stored_verbatim(self, store):
        # Parameter binding, not string building.
        await store.record(
            Event(action="cut", user_id=1, feed_title="O'Brien; DROP TABLE events--")
        )
        rows = store._execute("SELECT feed_title FROM events")
        assert rows[0]["feed_title"].startswith("O'Brien")


class TestStats:
    async def test_window_excludes_older_rows(self, store):
        await add_cut(store, at=time.time() - 40 * 3600)
        await add_cut(store)

        assert (await store.stats(24)).cuts_ok == 1
        assert (await store.stats(24 * 7)).cuts_ok == 2

    async def test_counts_unique_people(self, store):
        await add_cut(store, user=1)
        await add_cut(store, user=1)
        await add_cut(store, user=2)
        assert (await store.stats(24)).unique_users == 2

    async def test_reports_timing(self, store):
        for ms in (1000, 2000, 9000):
            await add_cut(store, ms=ms)
        stats = await store.stats(24)
        assert stats.median_ms == 2000
        assert stats.slowest_ms == 9000

    async def test_reports_the_voice_share(self, store):
        await add_cut(store, voice=True)
        await add_cut(store, voice=False)
        assert (await store.stats(24)).voice_share == 0.5

    async def test_ranks_podcasts_by_successful_cuts(self, store):
        await add_cut(store, feed="A")
        await add_cut(store, feed="A")
        await add_cut(store, feed="B")
        # A failure should not make a podcast look popular.
        await add_cut(store, feed="C", outcome="AudioError")

        top = (await store.stats(24)).top_podcasts
        assert top[0] == ("A", 2)
        assert "C" not in dict(top)

    async def test_empty_journal_reports_nothing_rather_than_dividing_by_zero(
        self, store
    ):
        stats = await store.stats(24)
        assert stats.cuts_total == 0
        assert stats.success_rate is None
        assert stats.median_ms is None

    async def test_summarises_every_action(self, store):
        await store.record(Event(action="search", user_id=1))
        await store.record(Event(action="search", user_id=1))
        await store.record(Event(action="inline", user_id=1))

        actions = dict((await store.stats(24)).actions)
        assert actions["search"] == 2 and actions["inline"] == 1

    async def test_counts_campaign_sources_by_person_not_by_visit(self, store):
        # One enthusiast opening the same link twice is not two arrivals.
        await store.record(Event(action="start", user_id=1, detail="reddit"))
        await store.record(Event(action="start", user_id=1, detail="reddit"))
        await store.record(Event(action="start", user_id=2, detail="reddit"))
        await store.record(Event(action="start", user_id=3, detail="hn"))
        await store.record(Event(action="start", user_id=4))

        assert (await store.stats(24)).sources == [("reddit", 2), ("hn", 1)]


class TestRetention:
    async def test_purges_rows_past_the_window(self, store):
        await add_cut(store, at=time.time() - 100 * 86400)
        await add_cut(store)

        removed = await store.purge(90)

        assert removed == 1
        assert (await store.stats(24 * 365)).cuts_ok == 1

    async def test_zero_disables_purging(self, store):
        await add_cut(store, at=time.time() - 1000 * 86400)
        assert await store.purge(0) == 0
        assert (await store.stats(24 * 365 * 10)).cuts_ok == 1

    async def test_purging_an_empty_journal_is_fine(self, store):
        assert await store.purge(1) == 0


class TestRecents:
    async def test_round_trip(self, store):
        episode = make_episode("42", "The Big One", duration=1234)
        await store.remember_recent(7, episode)

        recents = await store.recent_episodes(7, 10)

        assert len(recents) == 1
        assert recents[0].id == "42"
        assert recents[0].title == "The Big One"
        assert recents[0].duration == 1234
        assert recents[0].enclosure_url == episode.enclosure_url

    async def test_newest_first(self, store):
        for index in range(3):
            await store.remember_recent(7, make_episode(str(index)))
        assert [ep.id for ep in await store.recent_episodes(7, 10)] == ["2", "1", "0"]

    async def test_reopening_the_same_episode_moves_it_to_the_top(self, store):
        await store.remember_recent(7, make_episode("a"))
        await store.remember_recent(7, make_episode("b"))
        await store.remember_recent(7, make_episode("a"))

        assert [ep.id for ep in await store.recent_episodes(7, 10)] == ["a", "b"]

    async def test_users_do_not_see_each_others_history(self, store):
        await store.remember_recent(1, make_episode("mine"))
        await store.remember_recent(2, make_episode("theirs"))

        assert [ep.id for ep in await store.recent_episodes(1, 10)] == ["mine"]

    async def test_trimming_keeps_the_newest(self, store):
        for index in range(6):
            await store.remember_recent(7, make_episode(str(index)))

        await store.trim_recents(7, 3)

        assert [ep.id for ep in await store.recent_episodes(7, 10)] == ["5", "4", "3"]

    async def test_trimming_does_not_touch_other_users(self, store):
        await store.remember_recent(1, make_episode("keep"))
        for index in range(5):
            await store.remember_recent(2, make_episode(str(index)))

        await store.trim_recents(2, 1)

        assert len(await store.recent_episodes(1, 10)) == 1

    async def test_a_read_failure_returns_empty_rather_than_raising(self, store):
        store.close()
        assert await store.recent_episodes(1, 10) == []

    async def test_a_write_failure_never_propagates(self, store):
        store.close()
        await store.remember_recent(1, make_episode("x"))


class TestDisconnected:
    async def test_queries_before_connect_raise_clearly(self, tmp_path):
        store = Store(tmp_path / "unopened.db")
        with pytest.raises(RuntimeError, match="not connected"):
            store._execute("SELECT 1")

    async def test_size_of_a_missing_file_is_zero(self, tmp_path):
        assert await Store(tmp_path / "missing.db").size_on_disk() == 0


class TestConcurrency:
    async def test_parallel_writes_all_land(self, store):
        import asyncio

        await asyncio.gather(
            *(store.record(Event(action="cut", user_id=i, outcome="ok"))
              for i in range(25))
        )
        assert (await store.stats(24)).cuts_ok == 25

    async def test_the_connection_is_usable_from_worker_threads(self, store):
        # check_same_thread=False plus a lock; without it SQLite would refuse.
        try:
            await store.record(Event(action="start", user_id=1))
        except sqlite3.ProgrammingError as exc:  # pragma: no cover
            pytest.fail(f"threading misconfigured: {exc}")
