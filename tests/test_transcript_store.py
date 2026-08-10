"""Transcripts in SQLite: identity, atomicity, and lexical search.

Run against a real database file rather than a fake, because the things worth
pinning here are SQLite's — FTS5 staying in step with its content table, a
foreign key actually cascading, and a transaction rolling back as a unit.
"""

from __future__ import annotations

import pytest

from podcast_cutter.store import SCHEMA_VERSION, Store, TranscriptKey
from podcast_cutter.transcripts import (
    CHUNKER_VERSION,
    Utterance,
    Word,
    build,
    cluster,
)

pytestmark = pytest.mark.asyncio


def key(sha="abc123", model="base", backend="local", episode="e1") -> TranscriptKey:
    return TranscriptKey(
        episode_id=episode,
        source_sha256=sha,
        asr_backend=backend,
        asr_model=model,
        chunker_version=CHUNKER_VERSION,
    )


SPEECH = [
    Utterance(
        start=0,
        end=10,
        text="сегодня обсуждаем как нейросети ищут новые лекарства",
        words=(Word(start=2.0, end=2.6, text="нейросети"),),
        avg_logprob=-0.3,
        no_speech_prob=0.01,
        compression_ratio=1.3,
    ),
    Utterance(
        start=10,
        end=20,
        text="фолдинг белков это предсказание структуры",
        words=(Word(start=10.2, end=11.0, text="фолдинг"),),
        avg_logprob=-0.25,
        no_speech_prob=0.01,
        compression_ratio=1.4,
    ),
    Utterance(
        start=600,
        end=610,
        text="а венчурные инвесторы смотрят на команду",
        avg_logprob=-0.4,
        no_speech_prob=0.02,
        compression_ratio=1.5,
    ),
]


@pytest.fixture
async def store(tmp_path):
    store = Store(tmp_path / "t.db")
    store.connect()
    yield store
    await store.aclose()


async def save(store, speech=None, **key_kwargs) -> int:
    return await store.save_transcript(
        key(**key_kwargs),
        {"source_url": "https://cdn/ep.mp3", "duration_s": 700, "language": "ru"},
        build(speech if speech is not None else SPEECH),
    )


class TestSchema:
    async def test_version_is_recorded(self, store):
        assert store._execute("PRAGMA user_version")[0][0] == SCHEMA_VERSION

    async def test_a_version_one_journal_gains_the_new_tables(self, tmp_path):
        """Upgrading must not require anyone to do anything."""
        path = tmp_path / "old.db"
        old = Store(path)
        old.connect()
        old._execute("PRAGMA user_version = 1")
        old.close()

        upgraded = Store(path)
        upgraded.connect()
        assert upgraded._execute("PRAGMA user_version")[0][0] == SCHEMA_VERSION
        assert upgraded._execute("SELECT count(*) FROM windows")[0][0] == 0
        upgraded.close()

    async def test_an_older_index_is_rebuilt_rather_than_half_converted(
        self, tmp_path
    ):
        """Transcripts are derived data: re-fetching beats a partial index,
        which would answer questions wrongly instead of not at all."""
        path = tmp_path / "v2.db"
        store = Store(path)
        store.connect()
        await save(store)
        assert store._execute("SELECT count(*) FROM windows")[0][0] > 0
        store._execute("PRAGMA user_version = 2")
        store.close()

        upgraded = Store(path)
        upgraded.connect()
        assert upgraded._execute("PRAGMA user_version")[0][0] == SCHEMA_VERSION
        assert upgraded._execute("SELECT count(*) FROM transcripts")[0][0] == 0
        # The journal is not derived, and must survive untouched.
        assert upgraded._execute("SELECT count(*) FROM events")[0][0] == 0
        upgraded.close()

    async def test_the_journal_is_never_rebuilt(self, tmp_path):
        path = tmp_path / "keep.db"
        store = Store(path)
        store.connect()
        store._execute(
            "INSERT INTO events (at, action) VALUES (?, ?)", (1.0, "cut")
        )
        store._execute("PRAGMA user_version = 2")
        store.close()

        upgraded = Store(path)
        upgraded.connect()
        assert upgraded._execute("SELECT count(*) FROM events")[0][0] == 1
        upgraded.close()


class TestIdentity:
    async def test_the_same_bytes_and_rules_are_a_hit(self, store):
        saved = await save(store)
        assert await store.find_transcript(key()) == saved

    async def test_different_audio_is_not_a_hit(self, store):
        """Dynamic ad insertion: same episode id, different bytes, and the old
        timestamps would cut an advert."""
        await save(store)
        assert await store.find_transcript(key(sha="different")) is None

    async def test_a_different_model_is_not_a_hit(self, store):
        await save(store)
        assert await store.find_transcript(key(model="small")) is None

    async def test_lookup_by_episode_ignores_which_model_made_it(self, store):
        saved = await save(store)
        assert await store.transcript_for_episode("e1") == saved

    async def test_unknown_episode_has_no_transcript(self, store):
        assert await store.transcript_for_episode("nope") is None


class TestWriting:
    async def test_utterances_and_windows_both_land(self, store):
        transcript = await save(store)
        utterances = store._execute(
            "SELECT count(*) FROM utterances WHERE transcript_id = ?", (transcript,)
        )[0][0]
        windows = store._execute(
            "SELECT count(*) FROM windows WHERE transcript_id = ?", (transcript,)
        )[0][0]
        assert utterances == len(SPEECH)
        assert windows > 0

    async def test_word_timings_survive_the_round_trip(self, store):
        transcript = await save(store)
        restored = await store.utterances_for(transcript)
        assert restored[1].words[0].text == "фолдинг"
        assert restored[1].words[0].start == pytest.approx(10.2)

    async def test_quality_metrics_are_kept_for_review(self, store):
        """Nothing is deleted: a quarantine decision has to be reviewable."""
        transcript = await save(store)
        restored = await store.utterances_for(transcript)
        assert restored[0].avg_logprob == pytest.approx(-0.3)

    async def test_deleting_a_transcript_takes_its_rows_with_it(self, store):
        transcript = await save(store)
        store._execute("DELETE FROM transcripts WHERE id = ?", (transcript,))
        assert store._execute("SELECT count(*) FROM utterances")[0][0] == 0
        assert store._execute("SELECT count(*) FROM windows")[0][0] == 0

    async def test_the_fts_index_follows_a_delete(self, store):
        """External-content FTS5 keeps no copy, so without the triggers a
        search would return rows that no longer exist."""
        transcript = await save(store)
        assert await store.search_windows(transcript, "фолдинг")
        store._execute("DELETE FROM transcripts WHERE id = ?", (transcript,))
        assert store._execute(
            "SELECT count(*) FROM windows_fts WHERE windows_fts MATCH ?",
            ('text_normalized : ("фолдинг")',),
        )[0][0] == 0

    async def test_a_failed_write_leaves_nothing_behind(self, store, monkeypatch):
        """A transcript row with no windows is an episode that looks searchable
        and answers nothing."""
        import podcast_cutter.store as store_mod

        original = store_mod.normalize

        def explode(text):
            if "венчурные" in text:
                raise RuntimeError("boom")
            return original(text)

        monkeypatch.setattr(store_mod, "normalize", explode)

        with pytest.raises(RuntimeError):
            await save(store)

        assert store._execute("SELECT count(*) FROM transcripts")[0][0] == 0
        assert store._execute("SELECT count(*) FROM utterances")[0][0] == 0


class TestSearch:
    async def test_finds_a_word(self, store):
        transcript = await save(store)
        found = await store.search_windows(transcript, "фолдинг")
        assert found and "фолдинг" in found[0].text

    async def test_is_case_and_yo_insensitive(self, store):
        transcript = await save(
            store, speech=[Utterance(start=0, end=5, text="ещё раз про нейросети")]
        )
        assert await store.search_windows(transcript, "ЕЩЕ")

    async def test_returns_nothing_for_an_absent_phrase(self, store):
        """The negative case: the bot must be able to say it found nothing
        rather than return its best bad guess."""
        transcript = await save(store)
        assert await store.search_windows(transcript, "квантовая телепортация") == []

    async def test_punctuation_a_user_types_is_not_an_operator(self, store):
        """FTS5 would treat these as syntax; a person means them as text."""
        transcript = await save(store)
        for query in ('фолдинг "', "белков OR", "* AND", "'", "NEAR("):
            assert isinstance(
                await store.search_windows(transcript, query), list
            ), query

    async def test_an_empty_query_finds_nothing(self, store):
        transcript = await save(store)
        assert await store.search_windows(transcript, "   ") == []

    async def test_hits_carry_usable_timestamps(self, store):
        transcript = await save(store)
        found = await store.search_windows(transcript, "венчурные")
        assert found[0].start >= 590

    async def test_search_is_confined_to_one_transcript(self, store):
        mine = await save(store)
        await save(store, sha="other", episode="e2")
        found = await store.search_windows(mine, "фолдинг")
        assert all(moment.start < 700 for moment in found)

    async def test_higher_score_is_better_after_clustering(self, store):
        """bm25 is lower-is-better; the store negates it so callers do not each
        have to remember which way round it is."""
        transcript = await save(store)
        found = cluster(await store.search_windows(transcript, "фолдинг белков"))
        assert found
        assert found[0].score == max(moment.score for moment in found)

    async def test_finds_a_word_spoken_in_another_form(self, store):
        """The regression this whole column exists for."""
        pytest.importorskip("pymorphy3")
        transcript = await save(
            store,
            speech=[
                Utterance(start=0, end=8, text="потенциал нейросетей в биологии")
            ],
        )
        assert await store.search_windows(transcript, "нейросети")

    async def test_an_exact_form_still_wins_when_the_dictionary_mangles_it(
        self, store
    ):
        """Garbled recognition and jargon are unknown words; they must still
        find themselves through the surface index."""
        transcript = await save(
            store,
            speech=[Utterance(start=0, end=8, text="управляя через нейросеиц")],
        )
        assert await store.search_windows(transcript, "нейросеиц")

    async def test_overlapping_windows_collapse_into_one_answer(self, store):
        transcript = await save(store)
        raw = await store.search_windows(transcript, "фолдинг")
        assert len(raw) > 1, "overlap means several windows hold the same phrase"
        assert len(cluster(raw)) == 1
