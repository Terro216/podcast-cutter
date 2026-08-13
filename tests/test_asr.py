"""The recogniser lock, at the one moment it is load-bearing.

`LocalWhisper` runs recognition in a worker thread that cannot be cancelled.
When a caller wraps `transcribe` in `wait_for` and it times out, the await is
cancelled but the thread keeps decoding on the model — which is not safe to
call twice at once. The lock must therefore stay held until the thread really
finishes, not be released as the cancelled await unwinds. This is the exact
retry-starts-a-second-transcription defect from HANDOFF §5.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from podcast_cutter.asr import LocalWhisper


def _whisper_with_blocking_worker(release: threading.Event, started: threading.Event):
    """A LocalWhisper whose `_transcribe` blocks on an event instead of loading
    a model, so the thread's lifetime is under the test's control."""
    whisper = LocalWhisper(model="base")

    def fake_transcribe(path, language, on_segment):
        started.set()
        release.wait(timeout=5)
        return [], "en"

    whisper._transcribe = fake_transcribe
    return whisper


async def test_the_lock_is_held_until_the_worker_thread_finishes():
    release = threading.Event()
    started = threading.Event()
    whisper = _whisper_with_blocking_worker(release, started)

    # A caller that gives up after a short wait, exactly as the indexer's
    # transcribe_timeout does.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(whisper.transcribe("x.wav"), timeout=0.1)

    await asyncio.to_thread(started.wait, 5)
    # The await was cancelled, but the worker is still running — so the lock
    # must not have been handed back yet. Releasing it here is what let a retry
    # start a second decode on the same model.
    assert whisper._lock.locked(), "lock released while the worker still runs"

    release.set()
    # Once the thread ends, the lock has to come back on its own, or the
    # episode is wedged forever.
    for _ in range(100):
        if not whisper._lock.locked():
            break
        await asyncio.sleep(0.02)
    assert not whisper._lock.locked(), "lock never released after the worker ended"


async def test_a_normal_transcription_releases_the_lock():
    release = threading.Event()
    started = threading.Event()
    release.set()  # do not block
    whisper = _whisper_with_blocking_worker(release, started)

    utterances, language = await whisper.transcribe("x.wav")

    assert language == "en"
    assert not whisper._lock.locked()
    # And the model is free for the next episode.
    await whisper.transcribe("y.wav")
    assert not whisper._lock.locked()
