"""The pipeline that makes an episode searchable, with the recogniser faked.

A fake recogniser is not a compromise here: the questions worth asking are
about identity, sharing and refusal, none of which involve a model. What the
real backend does with real audio is a different kind of check and belongs to
the evaluation baskets, not to a unit test.
"""

from __future__ import annotations

import asyncio

import pytest

from podcast_cutter import indexer as indexer_mod
from podcast_cutter.config import Settings
from podcast_cutter.errors import AudioError
from podcast_cutter.indexer import Indexer, TranscriptionDisabled
from podcast_cutter.store import Store
from podcast_cutter.transcripts import Utterance, Word

pytestmark = pytest.mark.asyncio

SPEECH = [
    Utterance(
        start=0,
        end=10,
        text="сегодня обсуждаем как нейросети ищут новые лекарства",
        words=(Word(start=3.0, end=3.9, text="нейросети"),),
        avg_logprob=-0.3,
        no_speech_prob=0.01,
        compression_ratio=1.3,
    ),
    Utterance(
        start=300,
        end=310,
        text="а венчурные инвесторы смотрят прежде всего на команду",
        words=(Word(start=302.0, end=303.1, text="венчурные"),),
        avg_logprob=-0.35,
        no_speech_prob=0.02,
        compression_ratio=1.4,
    ),
]


class FakeRecognizer:
    backend = "fake"
    model = "test"

    def __init__(self, utterances=None, delay=0.0):
        self.utterances = utterances if utterances is not None else SPEECH
        self.calls = 0
        self.delay = delay

    async def transcribe(self, path, language=None, on_segment=None):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        # Reported the way the real one does — as each span is decoded — so the
        # progress path is exercised rather than merely tolerated.
        for utterance in self.utterances:
            if on_segment is not None:
                on_segment(utterance.end)
        return list(self.utterances), "ru"


def settings(**overrides) -> Settings:
    # setdefault, not a literal: one test deliberately turns the guard back on.
    overrides.setdefault("allow_private_sources", True)
    return Settings(bot_token="t", api_key="k", api_secret="s", **overrides)


@pytest.fixture
async def store(tmp_path):
    store = Store(tmp_path / "t.db")
    store.connect()
    yield store
    await store.aclose()


@pytest.fixture
def stub_fetch(monkeypatch, tmp_path):
    """Replace the network and ffmpeg, leaving the pipeline's own logic."""
    state = {"downloads": 0, "bytes": b"audio-one"}

    async def fake_resolve(url, timeout, proxy, allow_private=False):
        return url, "direct"

    async def fake_download(url, destination, settings, proxy, route, on_progress=None):
        state["downloads"] += 1
        destination.write_bytes(state["bytes"])
        return route

    async def fake_probe(source, timeout, env=None):
        return _SourceInfo(600)

    async def fake_decode(source, output, timeout):
        output.write_bytes(b"wav")

    monkeypatch.setattr(indexer_mod, "_resolve_url", fake_resolve)
    monkeypatch.setattr(indexer_mod, "_download_with_fallback", fake_download)
    monkeypatch.setattr(indexer_mod, "probe", fake_probe)
    monkeypatch.setattr(indexer_mod, "_decode_for_asr", fake_decode)
    return state


class _SourceInfo:
    def __init__(self, duration):
        self.duration = duration
        self.codec = "mp3"


async def index(indexer, tmp_path, episode="e1", url="https://cdn.example.com/a.mp3"):
    return await indexer.transcript_id(episode, url, tmp_path / "job")


class TestIndexing:
    async def test_transcribes_once_and_reuses(self, store, stub_fetch, tmp_path):
        recognizer = FakeRecognizer()
        indexer = Indexer(settings(), store, recognizer)

        first = await index(indexer, tmp_path)
        second = await index(indexer, tmp_path)

        assert first == second
        assert recognizer.calls == 1, "the second ask must not recognise again"

    async def test_concurrent_askers_share_one_job(self, store, stub_fetch, tmp_path):
        """A crowd from one chat must not become a crowd of ffmpeg jobs."""
        recognizer = FakeRecognizer(delay=0.05)
        indexer = Indexer(settings(), store, recognizer)

        results = await asyncio.gather(
            *(index(indexer, tmp_path) for _ in range(5))
        )

        assert len(set(results)) == 1
        assert recognizer.calls == 1

    async def test_identical_bytes_under_another_id_are_not_redone(
        self, store, stub_fetch, tmp_path
    ):
        recognizer = FakeRecognizer()
        indexer = Indexer(settings(), store, recognizer)

        first = await index(indexer, tmp_path, episode="e1")
        second = await index(indexer, tmp_path, episode="e2")

        assert first == second
        assert recognizer.calls == 1

    async def test_changed_audio_is_transcribed_again(
        self, store, stub_fetch, tmp_path
    ):
        """Dynamic ad insertion: same episode, different bytes. Reusing the old
        transcript would place every timestamp against audio that is gone."""
        recognizer = FakeRecognizer()
        indexer = Indexer(settings(), store, recognizer)
        await index(indexer, tmp_path, episode="e1")

        # A different episode id, so the by-episode shortcut does not apply,
        # carrying audio that has since changed.
        stub_fetch["bytes"] = b"audio-two-with-a-different-advert"
        await index(indexer, tmp_path, episode="e2")

        assert recognizer.calls == 2

    async def test_the_kill_switch_refuses_politely(self, store, stub_fetch, tmp_path):
        indexer = Indexer(settings(asr_enabled=False), store, FakeRecognizer())
        with pytest.raises(TranscriptionDisabled) as excinfo:
            await index(indexer, tmp_path)
        assert "timestamp" in excinfo.value.user_message

    async def test_an_over_long_episode_is_refused(
        self, store, stub_fetch, tmp_path, monkeypatch
    ):
        async def long_probe(source, timeout, env=None):
            return _SourceInfo(50_000)

        monkeypatch.setattr(indexer_mod, "probe", long_probe)
        indexer = Indexer(settings(), store, FakeRecognizer())

        with pytest.raises(AudioError, match="too long"):
            await index(indexer, tmp_path)

    async def test_a_refused_url_never_reaches_the_recogniser(
        self, store, stub_fetch, tmp_path
    ):
        recognizer = FakeRecognizer()
        indexer = Indexer(settings(allow_private_sources=False), store, recognizer)

        with pytest.raises(AudioError):
            await indexer.transcript_id(
                "e1", "file:///etc/passwd", tmp_path / "job"
            )
        assert recognizer.calls == 0

    async def test_progress_is_reported_in_order(self, store, stub_fetch, tmp_path):
        stages = []

        async def on_progress(progress):
            stages.append(progress.stage)

        indexer = Indexer(settings(), store, FakeRecognizer())
        await indexer.transcript_id(
            "e1", "https://cdn.example.com/a.mp3", tmp_path / "job", on_progress
        )

        # Deduplicated: download reports repeatedly as bytes arrive.
        assert [s for i, s in enumerate(stages) if i == 0 or stages[i - 1] != s] == [
            "download",
            "decode",
            "transcribe",
        ]

    async def test_progress_carries_a_measurable_fraction(
        self, store, stub_fetch, tmp_path
    ):
        """The point of the whole change: a bar that measures work, rather
        than a line that sits still while a 30-minute episode decodes."""
        seen = []

        async def on_progress(progress):
            if progress.stage == "transcribe":
                seen.append(progress)

        indexer = Indexer(settings(), store, FakeRecognizer())
        await indexer.transcript_id(
            "e1", "https://cdn.example.com/a.mp3", tmp_path / "job", on_progress
        )

        assert seen, "the transcribing stage must report at least once"
        assert seen[0].total == 600, "episode length, from the probe"
        assert seen[0].fraction is not None

    async def test_an_unknown_episode_length_reports_no_fraction(
        self, store, stub_fetch, tmp_path, monkeypatch
    ):
        """A feed reporting no duration must yield no bar, not a fake one."""

        async def no_duration(source, timeout, env=None):
            return _SourceInfo(None)

        monkeypatch.setattr(indexer_mod, "probe", no_duration)
        seen = []

        async def on_progress(progress):
            if progress.stage == "transcribe":
                seen.append(progress)

        indexer = Indexer(settings(), store, FakeRecognizer())
        await indexer.transcript_id(
            "e1", "https://cdn.example.com/a.mp3", tmp_path / "job", on_progress
        )

        assert seen and all(p.fraction is None for p in seen)

    async def test_a_failure_does_not_wedge_the_episode(
        self, store, stub_fetch, tmp_path, monkeypatch
    ):
        """A job that raised must leave nothing behind that stops a retry."""
        calls = {"n": 0}

        async def flaky_decode(source, output, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                raise AudioError("first attempt fails")
            output.write_bytes(b"wav")

        monkeypatch.setattr(indexer_mod, "_decode_for_asr", flaky_decode)
        indexer = Indexer(settings(), store, FakeRecognizer())

        with pytest.raises(AudioError):
            await index(indexer, tmp_path)

        assert await index(indexer, tmp_path) > 0


class TestSearching:
    async def _indexed(self, store, tmp_path):
        indexer = Indexer(settings(), store, FakeRecognizer())
        return indexer, await index(indexer, tmp_path)

    async def test_finds_the_moment(self, store, stub_fetch, tmp_path):
        indexer, transcript = await self._indexed(store, tmp_path)
        found = await indexer.search(transcript, "венчурные")
        assert found
        assert 290 <= found[0].start <= 320

    async def test_the_clip_starts_on_the_word_not_the_window(
        self, store, stub_fetch, tmp_path
    ):
        indexer, transcript = await self._indexed(store, tmp_path)
        found = await indexer.search(transcript, "венчурные")
        # The word starts at 302.0, padded back by the lead-in.
        assert found[0].clip_start == pytest.approx(300.0)

    async def test_an_absent_phrase_returns_nothing(self, store, stub_fetch, tmp_path):
        """The answer the whole design turns on: no result beats a wrong one."""
        indexer, transcript = await self._indexed(store, tmp_path)
        assert await indexer.search(transcript, "квантовая криптография") == []

    async def test_answers_are_distinct_moments(self, store, stub_fetch, tmp_path):
        indexer, transcript = await self._indexed(store, tmp_path)
        found = await indexer.search(transcript, "нейросети")
        starts = [moment.start for moment in found]
        assert len(starts) == len(set(starts))

    async def test_never_returns_more_than_three(self, store, stub_fetch, tmp_path):
        speech = [
            Utterance(start=i * 120, end=i * 120 + 10, text=f"повтор темы {i} фолдинг")
            for i in range(10)
        ]
        indexer = Indexer(settings(), store, FakeRecognizer(speech))
        transcript = await index(indexer, tmp_path)
        assert len(await indexer.search(transcript, "фолдинг")) <= 3
