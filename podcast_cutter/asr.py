"""Speech recognition backends.

One narrow interface, so the pipeline that downloads, indexes and searches
never learns which engine produced the words. That matters twice: a second
backend is planned (SpeechKit, once the cloud path earns its keep), and the
evaluation baskets are only a comparison if both engines can be run over the
same audio by swapping one object.

The local backend is faster-whisper. Measured on the production host — two
Xeon E5-2650 v2, no AVX2, no GPU — ``base`` runs at RTF 0.07, about 3.6 minutes
for a 50-minute episode, while ``small`` costs 0.23 for a difference that
mostly does not change which moment a search lands on. The transcript exists to
locate a moment, not to be read: the clip that comes back is the audio itself.

Two decoding choices are deliberate and worth not undoing:

* **Sequential, never batched.** faster-whisper's batched pipeline documents
  ``compression_ratio_threshold``, ``log_prob_threshold``,
  ``no_speech_threshold``, ``condition_on_previous_text`` and
  ``hallucination_silence_threshold`` as unused or overridden — which is
  precisely the set this project relies on to tell speech from invention.
* **``temperature=0.0``.** The fallback ladder decodes more freely after a
  failed attempt, which on silence produces fluent text that was never said.
  Losing a hard passage is better than indexing a plausible fiction, because a
  gap is visible and an invention is not.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Protocol

from .transcripts import Utterance, Word

logger = logging.getLogger(__name__)

#: Recognised audio must be mono PCM at this rate; every backend here expects
#: it, and ffmpeg produces it in one pass while downloading is happening anyway.
SAMPLE_RATE = 16_000


class Recognizer(Protocol):
    """Whatever turns an audio file into timed utterances."""

    #: Recorded on the transcript, and part of its identity.
    backend: str
    model: str

    async def transcribe(
        self, path: Path, language: str | None = None
    ) -> tuple[list[Utterance], str | None]:
        """Return the utterances and the language actually detected."""
        ...


class LocalWhisper:
    """faster-whisper on the CPU of whatever host this runs on.

    The model is loaded once, lazily, and kept. Loading ``base`` costs about a
    second when the files are already on disk, which is trivial per process and
    absurd per episode.
    """

    backend = "local"

    def __init__(
        self,
        model: str = "base",
        *,
        download_root: Path | None = None,
        compute_type: str = "int8",
        cpu_threads: int = 8,
    ) -> None:
        self.model = model
        self._download_root = download_root
        self._compute_type = compute_type
        self._cpu_threads = cpu_threads
        self._loaded = None
        #: One recognition at a time per process. The model is not safe to call
        #: concurrently, and the host has better uses for its cores than two
        #: transcriptions fighting over them.
        self._lock = asyncio.Lock()

    def _load(self):
        # Imported here rather than at module scope: the bot must start, and
        # the test suite must run, on a machine with no ASR installed at all.
        from faster_whisper import WhisperModel

        logger.info(
            "Loading Whisper %s (%s, %d threads)",
            self.model,
            self._compute_type,
            self._cpu_threads,
        )
        return WhisperModel(
            self.model,
            device="cpu",
            compute_type=self._compute_type,
            cpu_threads=self._cpu_threads,
            download_root=str(self._download_root) if self._download_root else None,
        )

    def _transcribe(self, path: Path, language: str | None):
        if self._loaded is None:
            self._loaded = self._load()

        segments, info = self._loaded.transcribe(
            str(path),
            language=language,
            beam_size=1,
            # Silence is where inventions come from; removing it up front is
            # the cheapest of the defences, and the only one that also saves
            # decoding time.
            vad_filter=True,
            word_timestamps=True,
            # A wrong line must not become the prompt for the next window and
            # start a loop. Costs some continuity of phrasing between windows,
            # which a search index does not care about.
            condition_on_previous_text=False,
            temperature=0.0,
        )

        utterances = [
            Utterance(
                start=segment.start,
                end=segment.end,
                text=segment.text.strip(),
                words=tuple(
                    Word(
                        start=word.start,
                        end=word.end,
                        text=word.word.strip(),
                        probability=getattr(word, "probability", None),
                    )
                    for word in (segment.words or ())
                ),
                avg_logprob=segment.avg_logprob,
                no_speech_prob=segment.no_speech_prob,
                compression_ratio=segment.compression_ratio,
            )
            for segment in segments
        ]
        return utterances, getattr(info, "language", None)

    async def transcribe(
        self, path: Path, language: str | None = None
    ) -> tuple[list[Utterance], str | None]:
        async with self._lock:
            # to_thread because this pins a core for minutes, and the bot has
            # to keep answering buttons while it does.
            return await asyncio.to_thread(self._transcribe, path, language)


def build_recognizer(settings) -> Recognizer:
    """The recogniser named by configuration.

    Only one backend exists so far; the indirection is here because the shape
    of the second one is already known, and because a bot started with a
    misspelled backend should say so at startup rather than on someone's first
    search.
    """
    from .errors import ConfigError

    if settings.asr_backend == "local":
        return LocalWhisper(
            model=settings.asr_model,
            download_root=settings.asr_model_dir,
            cpu_threads=settings.asr_threads,
        )

    raise ConfigError(
        f"ASR_BACKEND must be 'local' for now, got {settings.asr_backend!r}."
    )
