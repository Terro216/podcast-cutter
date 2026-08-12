"""Making one episode searchable, and searching it.

The pipeline is: fetch the audio through the same guarded path a cut uses,
hash the bytes that actually arrived, decode to what the recogniser wants,
recognise, judge, window, store. Then a query is lexical hits over those
windows, collapsed into distinct moments and placed on the word that matched.

Two properties are worth stating because they are what the design is for.

**The index belongs to the bytes, not to the episode.** Podcasts insert
advertisements dynamically, so an episode can serve different audio next month
under the same id. A transcript keyed on the id would then place its
timestamps against audio that no longer exists, and the bot would confidently
cut an advert. Keying on the SHA-256 of what was fetched makes that a miss
rather than a wrong answer.

**One transcription at a time, shared by everyone waiting.** Ten people from
one chat asking about the same popular episode must produce one job and ten
waiters, or a crowd is indistinguishable from an attack.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from .asr import SAMPLE_RATE, Recognizer
from .audio import _download_with_fallback, _resolve_url, _run, probe
from .config import Settings
from .errors import AudioError, PodcastCutterError
from .proxy import MediaProxy
from .store import Store, TranscriptKey
from .transcripts import (
    CHUNKER_VERSION,
    CLUSTER_GAP_SECONDS,
    Moment,
    build,
    cluster,
    is_indexable,
    locate_phrase,
    quarantine_signals,
)
from .urls import ensure_safe_source

logger = logging.getLogger(__name__)

#: How many distinct moments a search answers with. Three fits a phone screen
#: and is enough for "one of these is it" without becoming a list to read.
RESULTS = 3

#: Padding added around a located phrase, so the clip does not open mid-word.
CLIP_LEAD_IN = 2.0

#: How often progress is recomputed while recognising. The renderer throttles
#: its own edits; this only decides how fresh the number it reads is.
PROGRESS_TICK = 2.0


class TranscriptionDisabled(PodcastCutterError):
    """The kill switch is on."""

    default_message = (
        "Searching inside episodes is switched off right now. "
        "You can still cut by timestamp."
    )
    code = "asr_disabled"


@dataclass
class Progress:
    """What a waiting user is told, in the order it happens.

    ``done`` and ``total`` are in seconds of audio for the transcribing stage
    and in bytes while downloading — the caller renders them, and in both cases
    they are measured rather than guessed. ``total`` is ``None`` when the size
    is genuinely unknown, which is a thing to say rather than to fake.
    """

    stage: str
    detail: str = ""
    done: float = 0.0
    total: float | None = None

    @property
    def fraction(self) -> float | None:
        if not self.total or self.total <= 0:
            return None
        return min(1.0, max(0.0, self.done / self.total))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


async def _decode_for_asr(source: Path, output: Path, timeout: float) -> None:
    """Mono PCM at the recogniser's rate.

    Done as its own pass rather than letting the recogniser open the original:
    the source is whatever a feed ships, and giving ffmpeg one job with one
    known-good output is easier to reason about than trusting every backend's
    idea of decoding.
    """
    code, stderr = await _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-protocol_whitelist",
            "file",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            str(output),
        ],
        timeout,
    )
    if code != 0 or not output.exists() or output.stat().st_size == 0:
        raise AudioError(
            "Could not decode this episode's audio for transcription."
        )


class Indexer:
    """Transcribes episodes, once each, and answers questions about them."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        recognizer: Recognizer,
        proxy: MediaProxy | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.recognizer = recognizer
        self.proxy = proxy if proxy is not None else MediaProxy(settings)
        #: Episode id -> the task transcribing it. Everyone asking for the same
        #: episode awaits the same task rather than starting another.
        self._in_flight: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------

    async def transcript_id(
        self,
        episode_id: str,
        audio_url: str,
        workdir: Path,
        on_progress=None,
        meta: dict | None = None,
    ) -> int:
        """The transcript for this episode, making it if it does not exist.

        Concurrent callers for the same episode share one job. The task is
        removed from the map only once it has finished, so a second caller
        arriving mid-transcription joins rather than queues.
        """
        if not self.settings.asr_enabled:
            raise TranscriptionDisabled

        existing = await self.store.transcript_for_episode(episode_id)
        if existing is not None:
            return existing

        async with self._lock:
            task = self._in_flight.get(episode_id)
            if task is None:
                task = asyncio.create_task(
                    self._transcribe(episode_id, audio_url, workdir, on_progress, meta)
                )
                self._in_flight[episode_id] = task
                task.add_done_callback(
                    lambda _, key=episode_id: self._in_flight.pop(key, None)
                )
            else:
                logger.info("Joining the transcription running for %s", episode_id)

        # Awaited outside the lock, or nobody else could ever join.
        return await asyncio.shield(task)

    async def _transcribe(
        self,
        episode_id: str,
        audio_url: str,
        workdir: Path,
        on_progress=None,
        meta: dict | None = None,
    ) -> int:
        async def say(
            stage: str, detail: str = "", done: float = 0.0, total: float | None = None
        ) -> None:
            if on_progress is not None:
                with contextlib.suppress(Exception):
                    await on_progress(
                        Progress(stage=stage, detail=detail, done=done, total=total)
                    )

        started = time.monotonic()
        workdir.mkdir(parents=True, exist_ok=True)
        source = workdir / "episode.bin"
        decoded = workdir / "episode.wav"

        await ensure_safe_source(
            audio_url, allow_private=self.settings.allow_private_sources
        )
        resolved, route = await _resolve_url(
            audio_url,
            self.settings.probe_timeout,
            self.proxy,
            allow_private=self.settings.allow_private_sources,
        )
        await ensure_safe_source(
            resolved, allow_private=self.settings.allow_private_sources
        )

        await say("download", "Fetching the episode")

        async def downloaded(done: int, total: int | None) -> None:
            await say("download", done=done, total=total)

        await _download_with_fallback(
            resolved, source, self.settings, self.proxy, route, downloaded
        )

        digest = await asyncio.to_thread(_sha256, source)
        key = TranscriptKey(
            episode_id=episode_id,
            source_sha256=digest,
            asr_backend=self.recognizer.backend,
            asr_model=self.recognizer.model,
            chunker_version=CHUNKER_VERSION,
        )

        # Another episode id can point at identical bytes — the same show
        # re-published, or a feed listing an episode twice — and there is no
        # reason to recognise those twice.
        already = await self.store.find_transcript(key)
        if already is not None:
            logger.info("These exact bytes are already transcribed (%s)", digest[:12])
            source.unlink(missing_ok=True)
            return already

        info = await probe(source, self.settings.probe_timeout)
        if (
            info.duration is not None
            and info.duration > self.settings.max_source_seconds
        ):
            source.unlink(missing_ok=True)
            raise AudioError("This episode is too long to transcribe.")

        await say("decode", "Preparing the audio")
        await _decode_for_asr(source, decoded, self.settings.ffmpeg_timeout)
        source.unlink(missing_ok=True)

        # Recognition runs in a worker thread, so its callback cannot await
        # anything. It records a number; a ticker on the event loop reads it
        # and reports. The GIL makes a bare float assignment safe, and a lost
        # update would only mean one stale tick.
        heard = {"seconds": 0.0}
        total_seconds = float(info.duration or 0)

        async def tick() -> None:
            while True:
                await asyncio.sleep(PROGRESS_TICK)
                await say(
                    "transcribe", done=heard["seconds"], total=total_seconds or None
                )

        await say("transcribe", done=0.0, total=total_seconds or None)
        ticker = asyncio.create_task(tick())
        try:
            utterances, language = await asyncio.wait_for(
                self.recognizer.transcribe(
                    decoded, on_segment=lambda end: heard.__setitem__("seconds", end)
                ),
                timeout=self.settings.transcribe_timeout,
            )
        finally:
            ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ticker
        decoded.unlink(missing_ok=True)

        result = build(utterances)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "Transcribed %s in %d ms: %d utterances, %d quarantined, %d windows",
            episode_id,
            elapsed_ms,
            len(result.utterances),
            result.quarantined,
            len(result.windows),
        )

        return await self.store.save_transcript(
            key,
            {
                **(meta or {}),
                "source_url": resolved,
                "duration_s": info.duration,
                "language": language,
                "ms": elapsed_ms,
            },
            result,
        )

    # ------------------------------------------------------------------
    # Searching
    # ------------------------------------------------------------------

    async def search(
        self, transcript_id: int, query: str, limit: int = RESULTS
    ) -> list[Moment]:
        """Distinct moments answering ``query``, best first.

        Empty is a real answer, and the one most worth getting right: a search
        that always returns its best guess turns a recognition error into a
        confident lie.
        """
        hits = await self.store.search_windows(transcript_id, query)
        if not hits:
            return []

        # Quarantined utterances stay out of placement the same way they stay
        # out of the index: a clip must not open on a timestamp the index
        # itself refused to trust.
        utterances = [
            u
            for u in await self.store.utterances_for(transcript_id)
            if is_indexable(quarantine_signals(u))
        ]

        # Placement happens before the answer is cut to `limit`, and moments
        # whose clips open at the same instant collapse to the best-scored
        # one. Clustering catches windows that overlap; this catches the same
        # spoken moment reached through windows too far apart to cluster —
        # without it, two of the three answers can be one moment twice.
        placed: list[Moment] = []
        for moment in cluster(hits):
            found = locate_phrase(
                utterances, query, within=(moment.start, moment.end),
                padding=CLIP_LEAD_IN,
            )
            # Falling back to the window start is deliberate: without word
            # timings the honest answer is "somewhere in here", and the nudge
            # buttons exist for exactly that.
            clip = found if found is not None else moment.start
            if any(abs(clip - other.clip_start) <= CLUSTER_GAP_SECONDS
                   for other in placed):
                continue
            placed.append(
                Moment(
                    start=moment.start,
                    end=moment.end,
                    text=moment.text,
                    score=moment.score,
                    clip_start=clip,
                )
            )
            if len(placed) == limit:
                break
        return placed
