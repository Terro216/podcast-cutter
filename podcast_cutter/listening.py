"""The first-listen queue: one episode at a time, and it survives a restart.

Cutting a clip is seconds; listening to a whole episode is minutes. That gap is
the whole reason this exists as its own thing rather than as another semaphore:

**The line is of episodes, not of people.** Ten people from one chat asking
about the same popular episode are one transcription and ten waiters. A queue
that counted them as ten would report a ten-deep line for one episode's worth
of work, and would be lying to nine of them.

**The line lives in SQLite.** It used to live in a semaphore and a counter,
which meant a redeploy silently threw away every waiting job — the most
expensive thing this bot does, lost in the one operation that happens most
often. Now a restart picks the line up where it stopped.

**A resumed job finishes the transcript, not the screen.** Sessions are
deliberately not persisted, so nobody's moments list can be restored across a
restart. What can be restored is the expensive artifact: the episode gets
listened to, the person is told it is ready, and their next search on it is
instant. That is the part worth minutes of CPU.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .api import Episode
from .config import Settings
from .errors import PodcastCutterError
from .indexer import Indexer, Progress
from .store import AsrBatch, AsrJob, Store

logger = logging.getLogger(__name__)

#: How many times an episode may be taken out of the queue before it is given
#: up on. A job is charged an attempt when it starts, so a transcription that
#: kills the process gets exactly one more chance on the next boot and then
#: stops — a crash loop that re-downloads an episode every start is a worse
#: failure than one lost job.
MAX_ATTEMPTS = 2

#: The stage name a waiting-in-line update carries. Not one of the indexer's
#: stages, because nothing is happening to this episode yet; ``done`` is the
#: position in line rather than a fraction of anything.
QUEUED = "queued"

ProgressCallback = Callable[[Progress], Awaitable[None]]

#: Told about a job whose asker is no longer waiting — the usual reason being
#: that the bot restarted under them. Takes the job and the transcript id, or
#: ``None`` if the episode could not be listened to after all.
Notifier = Callable[[AsrJob, int | None], Awaitable[None]]


@dataclass
class Ticket:
    """A live caller's claim on a queued job.

    Exists only while somebody is actually waiting: the handler drops it in a
    ``finally``, and a job whose ticket is gone by the time it finishes is
    delivered by :data:`Notifier` instead.
    """

    job_id: int
    episode_id: str
    on_progress: ProgressCallback | None = None
    done: asyncio.Future = field(default_factory=asyncio.Future)


class TranscriptionQueue:
    """Serves first listens one episode at a time, from a durable line."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        indexer: Indexer,
        notify: Notifier | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.indexer = indexer
        self.notify = notify
        #: Job id -> the caller waiting for it right now.
        self._tickets: dict[int, Ticket] = {}
        #: The episode being listened to right now. Progress is fanned out by
        #: episode rather than to the claimed rows, so somebody who asks about
        #: it a minute later watches the same bar instead of being told they
        #: are second in a line of one.
        self._running: str | None = None
        self._worker: asyncio.Task | None = None
        #: Set when there might be something to do. The worker clears it before
        #: it looks, so work arriving mid-scan is not missed.
        self._wake = asyncio.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._wake.set()
            self._worker = asyncio.create_task(self._serve(), name="asr-queue")

    async def stop(self) -> None:
        worker, self._worker = self._worker, None
        if worker is None:
            return
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker

    async def resume(self) -> int:
        """Take over the line left by the previous process.

        Anything still marked running was interrupted by whatever stopped that
        process — nothing else can leave that state behind — so it goes back to
        the front of the queue. Jobs out of attempts are dropped here rather
        than at claim time, so their askers are told once instead of never.
        """
        recovered = await self.store.requeue_running_asr_jobs()
        for job in await self.store.abandon_exhausted_asr_jobs(MAX_ATTEMPTS):
            logger.warning(
                "Giving up on %s after %d attempts", job.episode_id, job.attempts
            )
            await self._deliver(job, None)
        waiting = len(await self.store.asr_queue_episodes())
        if waiting:
            logger.info(
                "Resuming the listening queue: %d episode%s waiting (%d "
                "interrupted by the restart)",
                waiting,
                "" if waiting == 1 else "s",
                recovered,
            )
        self.start()
        return waiting

    # ------------------------------------------------------------------
    # What a handler uses
    # ------------------------------------------------------------------

    async def depth(self) -> int:
        """Distinct episodes queued or running."""
        return len(await self.store.asr_queue_episodes())

    async def position(self, episode_id: str) -> int:
        """Where this episode stands, 1 being the one in progress. 0 = absent."""
        episodes = await self.store.asr_queue_episodes()
        if episode_id not in episodes:
            return 0
        return episodes.index(episode_id) + 1

    async def submit(
        self,
        episode: Episode,
        user_id: int,
        chat_id: int,
        on_progress: ProgressCallback | None = None,
    ) -> Ticket:
        """Join the line for this episode and get something to await."""
        job_id = await self.store.enqueue_asr_job(episode, user_id, chat_id)
        ticket = Ticket(
            job_id=job_id, episode_id=episode.id, on_progress=on_progress
        )
        self._tickets[job_id] = ticket
        self.start()
        self._wake.set()
        # Everyone behind this one keeps their number; the newcomer needs to be
        # told theirs before anything else happens.
        await self._say_position(ticket)
        return ticket

    def release(self, ticket: Ticket) -> None:
        """Stop waiting. The job itself carries on — it is worth finishing for
        whoever asks next, and its answer is delivered by message instead."""
        self._tickets.pop(ticket.job_id, None)

    # ------------------------------------------------------------------
    # The worker
    # ------------------------------------------------------------------

    async def _serve(self) -> None:
        # Nothing but cancellation may end this loop. There is exactly one
        # worker, so a single unhandled exception would leave the queue with
        # nobody serving it for the rest of the process's life — and it would
        # look like a hang rather than like a crash, which is worse.
        while True:
            try:
                batch = await self.store.claim_asr_batch(MAX_ATTEMPTS)
                if batch is None:
                    self._wake.clear()
                    await self._wake.wait()
                    continue
                await self._run_batch(batch)
                await self._announce_positions()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("The listening queue hit an error; carrying on")
                await asyncio.sleep(5)

    async def _run_batch(self, batch: AsrBatch) -> None:
        job = batch.head
        job_dir = self.settings.work_dir / f"asr-{batch.episode_id}"

        async def on_progress(stage: Progress) -> None:
            await self._fan_out(batch.episode_id, stage)

        self._running = batch.episode_id
        try:
            transcript = await self.indexer.transcript_id(
                job.episode_id,
                job.audio_url,
                job_dir,
                on_progress,
                meta={
                    "episode_title": job.episode_title,
                    "feed_title": job.feed_title,
                },
            )
        except asyncio.CancelledError:
            # The process is going down. The rows stay `running`, which is
            # exactly what `resume` looks for on the way back up.
            raise
        except PodcastCutterError as exc:
            await self._fail_batch(batch, exc, getattr(exc, "code", "error"))
            return
        except Exception as exc:  # noqa: BLE001 - the worker must not die
            logger.exception("Transcription of %s failed", job.episode_id)
            await self._fail_batch(batch, exc, "error")
            return
        finally:
            self._running = None
            shutil.rmtree(job_dir, ignore_errors=True)

        await self.store.finish_asr_jobs(batch.ids, "done", "ok")
        for waiter in batch.jobs:
            ticket = self._tickets.pop(waiter.id, None)
            if ticket is not None and not ticket.done.done():
                ticket.done.set_result(transcript)
            else:
                await self._deliver(waiter, transcript)

    async def _fail_batch(
        self, batch: AsrBatch, error: Exception, outcome: str
    ) -> None:
        """A job that failed is failed — it is not retried.

        The attempt counter is about restarts, not about errors: a download
        that 403s will 403 again a second later, and retrying it here would
        hold the line for everyone behind it while telling the person who asked
        nothing. They get the reason and can ask again, which is what the bot
        did before there was a queue at all.
        """
        await self.store.finish_asr_jobs(batch.ids, "failed", outcome)
        for waiter in batch.jobs:
            ticket = self._tickets.pop(waiter.id, None)
            if ticket is not None and not ticket.done.done():
                ticket.done.set_exception(error)
            else:
                await self._deliver(waiter, None)

    async def _deliver(self, job: AsrJob, transcript: int | None) -> None:
        if self.notify is None:
            return
        with contextlib.suppress(Exception):
            await self.notify(job, transcript)

    async def _fan_out(self, episode_id: str, stage: Progress) -> None:
        for ticket in list(self._tickets.values()):
            if ticket.episode_id == episode_id and ticket.on_progress is not None:
                with contextlib.suppress(Exception):
                    await ticket.on_progress(stage)

    async def _announce_positions(self) -> None:
        """Everyone still in line hears their new number."""
        for ticket in list(self._tickets.values()):
            await self._say_position(ticket)

    async def _say_position(self, ticket: Ticket) -> None:
        if ticket.on_progress is None or ticket.done.done():
            return
        place = await self.position(ticket.episode_id)
        if place <= 1:
            # Either it is being listened to now, or it left the queue: both
            # are the indexer's story to tell, and overwriting a progress bar
            # with "1st in line" would be a step backwards on screen.
            return
        with contextlib.suppress(Exception):
            await ticket.on_progress(Progress(stage=QUEUED, done=place))


__all__ = ["MAX_ATTEMPTS", "QUEUED", "Ticket", "TranscriptionQueue"]
