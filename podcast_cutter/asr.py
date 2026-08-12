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
from collections.abc import Callable
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
        self,
        path: Path,
        language: str | None = None,
        on_segment: Callable[[float], None] | None = None,
    ) -> tuple[list[Utterance], str | None]:
        """Return the utterances and the language actually detected.

        ``on_segment`` is called with how many seconds of audio have been
        recognised so far. It may be called from a worker thread, so it must
        not touch an event loop — record a number and let the loop read it.
        """
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

    def _transcribe(self, path: Path, language: str | None, on_segment=None):
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

        # `segments` is a generator: nothing is decoded until it is consumed,
        # and each item carries the point in the audio it ends at. Iterating
        # rather than list()-ing it is what turns "please wait" into a real
        # measure of how far along the work is.
        utterances = []
        for segment in segments:
            utterances.append(
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
            )
            if on_segment is not None:
                on_segment(segment.end)

        return utterances, getattr(info, "language", None)

    async def transcribe(
        self,
        path: Path,
        language: str | None = None,
        on_segment: Callable[[float], None] | None = None,
    ) -> tuple[list[Utterance], str | None]:
        async with self._lock:
            # to_thread because this pins a core for minutes, and the bot has
            # to keep answering buttons while it does.
            return await asyncio.to_thread(
                self._transcribe, path, language, on_segment
            )


class SpeechKit:
    """Yandex SpeechKit v3 async recognition, over plain REST.

    The measured trade against local Whisper (`ROADMAP.md` §4 guessed, the
    smoke run confirmed): a 180 s Russian sample came back in ten seconds,
    correctly, including the exact phrases `base` garbles — «первой линии
    защиты», «руководством факультета». What the cloud does not return is any
    of the quarantine metrics; the judge already treats missing metrics as a
    property of the backend, not as suspicion.

    Two response details drive the parsing:

    * Results arrive as newline-delimited JSON where each final carries
      word-level timings; `finalRefinement` repeats the same words with
      numbers and names normalised («100», not «сто»), which is what users
      type — so refinements win over their raw finals.
    * A "final" can span a minute of audio, and a minute-long utterance
      repeated into every 30-second window it overlaps would bloat the index.
      Words are therefore regrouped into utterances at speech pauses, the
      same granularity Whisper produces naturally.
    """

    backend = "speechkit"

    #: Split a final into utterances at silences this long, or when one grows
    #: past a window's length anyway.
    _SPLIT_PAUSE_S = 1.2
    _MAX_UTTERANCE_S = 20.0

    def __init__(self, api_key: str, folder_id: str, model: str = "general") -> None:
        self.model = model
        self._api_key = api_key
        self._folder_id = folder_id

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Api-Key {self._api_key}",
            "x-folder-id": self._folder_id,
        }

    async def transcribe(
        self,
        path: Path,
        language: str | None = None,
        on_segment: Callable[[float], None] | None = None,
    ) -> tuple[list[Utterance], str | None]:
        import base64

        import httpx

        from .errors import AudioError

        ogg = path.with_suffix(".spk.ogg")
        try:
            await self._encode_opus(path, ogg)
            content = base64.b64encode(
                await asyncio.to_thread(ogg.read_bytes)
            ).decode()
        finally:
            ogg.unlink(missing_ok=True)

        body = {
            "content": content,
            "recognitionModel": {
                "model": self.model,
                "audioFormat": {
                    "containerAudio": {"containerAudioType": "OGG_OPUS"}
                },
                "textNormalization": {
                    "textNormalization": "TEXT_NORMALIZATION_ENABLED"
                },
            },
        }
        if language:
            body["recognitionModel"]["languageRestriction"] = {
                "restrictionType": "WHITELIST",
                "languageCode": [self._language_code(language)],
            }

        async with httpx.AsyncClient(timeout=120) as client:
            submitted = await client.post(
                "https://stt.api.cloud.yandex.net/stt/v3/recognizeFileAsync",
                headers=self._headers,
                json=body,
            )
            if submitted.status_code != 200:
                raise AudioError(
                    f"SpeechKit refused the job: {submitted.status_code} "
                    f"{submitted.text[:200]}"
                )
            operation = submitted.json()["id"]

            while True:
                await asyncio.sleep(10)
                state = (
                    await client.get(
                        f"https://operation.api.cloud.yandex.net/operations/{operation}",
                        headers=self._headers,
                    )
                ).json()
                if state.get("done"):
                    if "error" in state:
                        raise AudioError(
                            f"SpeechKit failed: {state['error']!r}"[:300]
                        )
                    break

            results = await client.get(
                "https://stt.api.cloud.yandex.net/stt/v3/getRecognition",
                headers=self._headers,
                params={"operationId": operation},
            )
            results.raise_for_status()

        utterances = self._parse(results.text, on_segment)
        return utterances, language

    async def _encode_opus(self, source: Path, target: Path) -> None:
        """Re-encode for upload: a raw 16 kHz WAV is ~115 MB per hour, over
        the API's direct-content limit; Opus at 32 kbps is ~14 MB and the
        recogniser's own documentation prefers it."""
        from .errors import AudioError

        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(source),
            "-ac", "1", "-c:a", "libopus", "-b:a", "32k",
            str(target),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            raise AudioError(
                f"Could not prepare audio for SpeechKit: "
                f"{stderr.decode(errors='replace')[:200]}"
            )

    @staticmethod
    def _language_code(language: str) -> str:
        return {"ru": "ru-RU", "en": "en-US"}.get(language, language)

    def _parse(self, ndjson: str, on_segment) -> list[Utterance]:
        import json

        # One alternatives-list per finalIndex; refinements overwrite finals
        # because they carry the normalised text users actually type.
        by_index: dict[str, list] = {}
        for line in ndjson.splitlines():
            if not line.strip():
                continue
            payload = json.loads(line).get("result", {})
            cursor = payload.get("audioCursors") or {}
            index = str(cursor.get("finalIndex", len(by_index)))
            if "finalRefinement" in payload:
                by_index[index] = (
                    payload["finalRefinement"]["normalizedText"]["alternatives"]
                )
            elif "final" in payload and index not in by_index:
                by_index[index] = payload["final"]["alternatives"]

        words: list[Word] = []
        for index in sorted(by_index, key=float):
            alternative = (by_index[index] or [{}])[0]
            for item in alternative.get("words") or ():
                words.append(
                    Word(
                        start=int(item["startTimeMs"]) / 1000,
                        end=int(item["endTimeMs"]) / 1000,
                        text=item["text"],
                    )
                )

        utterances: list[Utterance] = []
        group: list[Word] = []

        def flush() -> None:
            if not group:
                return
            utterances.append(
                Utterance(
                    start=group[0].start,
                    end=group[-1].end,
                    text=" ".join(word.text for word in group),
                    words=tuple(group),
                )
            )
            if on_segment is not None:
                on_segment(group[-1].end)
            group.clear()

        for word in words:
            if group and (
                word.start - group[-1].end >= self._SPLIT_PAUSE_S
                or word.end - group[0].start >= self._MAX_UTTERANCE_S
            ):
                flush()
            group.append(word)
        flush()
        return utterances


def build_recognizer(settings) -> Recognizer:
    """The recogniser named by configuration.

    A bot started with a misspelled backend should say so at startup rather
    than on someone's first search.
    """
    from .errors import ConfigError

    if settings.asr_backend == "local":
        return LocalWhisper(
            model=settings.asr_model,
            download_root=settings.asr_model_dir,
            cpu_threads=settings.asr_threads,
        )

    if settings.asr_backend == "speechkit":
        if not (settings.speechkit_api_key and settings.speechkit_folder_id):
            raise ConfigError(
                "ASR_BACKEND=speechkit needs SPEECHKIT_API_KEY and "
                "SPEECHKIT_FOLDER_ID."
            )
        return SpeechKit(
            api_key=settings.speechkit_api_key,
            folder_id=settings.speechkit_folder_id,
        )

    raise ConfigError(
        f"ASR_BACKEND must be 'local' or 'speechkit', got "
        f"{settings.asr_backend!r}."
    )
