"""The first-listen queue, where the point is what survives.

Two properties are worth holding down here, because both were broken before
the queue moved into SQLite and neither is visible from the outside until it
costs somebody half an hour: a line that is of *episodes* rather than of
people, and a line that a redeploy does not throw away.
"""

from __future__ import annotations

import asyncio

import pytest

from conftest import FakeRecognizer, make_episode
from podcast_cutter.errors import AudioError
from podcast_cutter.listening import MAX_ATTEMPTS, QUEUED, TranscriptionQueue

pytestmark = pytest.mark.asyncio


class GatedRecognizer(FakeRecognizer):
    """Holds a transcription open until the test lets it finish."""

    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def transcribe(self, path, language=None, on_segment=None):
        self.started.set()
        await self.release.wait()
        return await super().transcribe(path, language, on_segment)


class Notes:
    """Collects what the queue said to people who were no longer waiting."""

    def __init__(self):
        self.sent: list[tuple[int, str, int | None]] = []

    async def __call__(self, job, transcript_id):
        self.sent.append((job.id, job.episode_id, transcript_id))

    @property
    def episodes(self) -> list[str]:
        return [episode for _, episode, _ in self.sent]


@pytest.fixture
def gate(indexer) -> GatedRecognizer:
    recognizer = GatedRecognizer()
    indexer.recognizer = recognizer
    return recognizer


@pytest.fixture
def notes() -> Notes:
    return Notes()


@pytest.fixture
async def queue(settings, store, indexer, notes):
    instance = TranscriptionQueue(settings, store, indexer, notify=notes)
    yield instance
    await instance.stop()


async def settle(times: int = 6) -> None:
    """Let the worker run without pinning the test to a wall-clock delay."""
    for _ in range(times):
        await asyncio.sleep(0)


class TestTheLineIsOfEpisodes:
    async def test_two_people_one_episode_is_one_transcription(
        self, queue, gate, store
    ):
        episode = make_episode("10")
        first = await queue.submit(episode, user_id=1, chat_id=11)
        await gate.started.wait()
        second = await queue.submit(episode, user_id=2, chat_id=22)

        gate.release.set()
        assert await first.done == await second.done
        assert gate.calls == 1

    async def test_the_second_asker_watches_the_bar_not_a_queue_position(
        self, queue, gate
    ):
        episode = make_episode("10")
        await queue.submit(episode, user_id=1, chat_id=11)
        await gate.started.wait()

        seen: list[str] = []

        async def watch(stage):
            seen.append(stage.stage)

        # Joining an episode already being listened to is position 1, not 2:
        # there is one episode in the line, however many people want it.
        assert await queue.position(episode.id) == 1
        await queue.submit(episode, user_id=2, chat_id=22, on_progress=watch)
        gate.release.set()
        await settle()

        assert QUEUED not in seen

    async def test_a_different_episode_is_told_its_place(self, queue, gate):
        await queue.submit(make_episode("10"), user_id=1, chat_id=11)
        await gate.started.wait()

        places: list[int] = []

        async def watch(stage):
            if stage.stage == QUEUED:
                places.append(int(stage.done))

        await queue.submit(
            make_episode("11"), user_id=2, chat_id=22, on_progress=watch
        )
        assert places == [2]

        gate.release.set()
        await settle(20)

    async def test_depth_counts_episodes(self, queue, gate):
        await queue.submit(make_episode("10"), user_id=1, chat_id=11)
        await queue.submit(make_episode("10"), user_id=2, chat_id=22)
        await queue.submit(make_episode("11"), user_id=3, chat_id=33)
        assert await queue.depth() == 2

        gate.release.set()
        await settle(30)


class TestARestart:
    async def test_a_job_interrupted_by_a_restart_is_finished_and_announced(
        self, settings, store, indexer, notes
    ):
        # The process that took this job died holding it: the row is left
        # `running`, which is a state nothing but a crash can leave behind.
        await store.enqueue_asr_job(make_episode("10"), user_id=1, chat_id=11)
        claimed = await store.claim_asr_batch()
        assert claimed is not None

        # A fresh queue, as the next process would build one.
        revived = TranscriptionQueue(settings, store, indexer, notify=notes)
        assert await revived.resume() == 1
        try:
            for _ in range(200):
                if notes.sent:
                    break
                await asyncio.sleep(0.01)
        finally:
            await revived.stop()

        assert notes.episodes == ["10"]
        # The expensive part is what was recovered: the transcript exists, so
        # the search that follows is instant.
        assert notes.sent[0][2] == await store.transcript_for_episode("10")

    async def test_an_episode_that_keeps_killing_the_bot_is_given_up_on(
        self, settings, store, indexer, notes
    ):
        await store.enqueue_asr_job(make_episode("10"), user_id=1, chat_id=11)
        # One crash per attempt, until the allowance is gone.
        for _ in range(MAX_ATTEMPTS):
            assert await store.claim_asr_batch() is not None
            await store.requeue_running_asr_jobs()

        revived = TranscriptionQueue(settings, store, indexer, notify=notes)
        try:
            assert await revived.resume() == 0
        finally:
            await revived.stop()

        # Told once that it failed, rather than retried forever in silence.
        assert notes.sent == [(1, "10", None)]

    async def test_a_finished_job_is_not_run_again(self, queue, gate, store):
        ticket = await queue.submit(make_episode("10"), user_id=1, chat_id=11)
        gate.release.set()
        await ticket.done

        assert await store.claim_asr_batch() is None


class TestWhenNobodyIsWaiting:
    async def test_a_released_ticket_still_gets_its_episode_listened_to(
        self, queue, gate, notes, store
    ):
        ticket = await queue.submit(make_episode("10"), user_id=1, chat_id=11)
        await gate.started.wait()
        # The handler gave up — cancelled, or the person walked away.
        queue.release(ticket)

        gate.release.set()
        for _ in range(200):
            if notes.sent:
                break
            await asyncio.sleep(0.01)

        assert notes.episodes == ["10"]
        assert await store.transcript_for_episode("10") is not None


class TestTellingSomebodyAfterARestart:
    """The delivery path itself, not a stand-in for it. Everything here — the
    bot's own username, the deep link, the markup — is only assembled when a
    job outlives its request, which is to say on the one path nobody exercises
    by hand."""

    class FakeTelegram:
        def __init__(self):
            self.sent: list[tuple[int, str, dict]] = []

        async def send_message(self, chat_id, text, **kwargs):
            self.sent.append((chat_id, text, kwargs))

    async def test_the_message_carries_a_link_back_into_the_episode(
        self, bot, store
    ):
        telegram = self.FakeTelegram()
        bot.telegram = telegram
        await store.enqueue_asr_job(make_episode("10"), user_id=7, chat_id=99)
        batch = await store.claim_asr_batch()

        await bot.notify_listened(batch.head, transcript_id=1)

        chat_id, text, options = telegram.sent[0]
        assert chat_id == 99
        assert "Episode 10" in text
        button = options["reply_markup"].inline_keyboard[0][0]
        # A deep link, not callback data: the session it would have needed is
        # what the restart destroyed.
        assert button.url == "https://t.me/podcast_cutter_bot?start=ep_10"

    async def test_a_failure_says_so_and_offers_no_link(self, bot, store):
        telegram = self.FakeTelegram()
        bot.telegram = telegram
        await store.enqueue_asr_job(make_episode("10"), user_id=7, chat_id=99)
        batch = await store.claim_asr_batch()

        await bot.notify_listened(batch.head, transcript_id=None)

        _, text, options = telegram.sent[0]
        assert "could not" in text
        assert options["reply_markup"] is None

    async def test_it_is_journalled(self, bot, store):
        bot.telegram = self.FakeTelegram()
        await store.enqueue_asr_job(make_episode("10"), user_id=7, chat_id=99)
        batch = await store.claim_asr_batch()

        await bot.notify_listened(batch.head, transcript_id=1)

        rows = store._execute(
            "SELECT user_id, outcome, detail FROM events WHERE action = 'listened'"
        )
        assert rows[0]["user_id"] == 7
        assert rows[0]["outcome"] == "ok"
        assert rows[0]["detail"] == "resumed"

    async def test_no_bot_to_talk_through_is_survivable(self, bot, store):
        """Reached if a job finishes before startup finished wiring things up.
        Losing the message is a shame; crashing the worker is worse."""
        bot.telegram = None
        await store.enqueue_asr_job(make_episode("10"), user_id=7, chat_id=99)
        batch = await store.claim_asr_batch()

        await bot.notify_listened(batch.head, transcript_id=1)


class TestFailure:
    async def test_the_reason_reaches_the_person_who_asked(
        self, settings, store, indexer, notes
    ):
        async def explode(*args, **kwargs):
            raise AudioError("This episode could not be fetched.")

        indexer.transcript_id = explode
        queue = TranscriptionQueue(settings, store, indexer, notify=notes)
        try:
            ticket = await queue.submit(make_episode("10"), user_id=1, chat_id=11)
            with pytest.raises(AudioError):
                await ticket.done
        finally:
            await queue.stop()

        # Failed, not requeued: a fetch that 403s will 403 again a second
        # later, and holding the line for it tells nobody anything.
        assert await store.claim_asr_batch() is None
        # The person waiting was told by the exception, so no message is sent.
        assert notes.sent == []
