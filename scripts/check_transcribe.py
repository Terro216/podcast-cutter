"""End-to-end check of transcription and search against a real episode.

Not a unit test: it needs the network, ffmpeg, and a recognition model, and it
takes minutes. It exists because everything else about this pipeline is tested
with a fake recogniser, and a fake cannot answer the only question that
matters in the end — whether a person's phrasing finds the moment somebody
actually said.

    python scripts/check_transcribe.py <episode-url> "фраза" "другая фраза"

With no arguments it uses a Russian episode and a couple of phrases from it.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from podcast_cutter.asr import LocalWhisper  # noqa: E402
from podcast_cutter.config import Settings  # noqa: E402
from podcast_cutter.indexer import Indexer  # noqa: E402
from podcast_cutter.store import Store  # noqa: E402
from podcast_cutter.text import format_duration  # noqa: E402

DEFAULT_URL = "https://media.transistor.fm/18d189ee/9dfda6e5.mp3"
DEFAULT_QUERIES = [
    "нейросети",
    "белки",
    "квантовая телепортация",  # deliberately absent: the answer must be empty
]


async def main(url: str, queries: list[str]) -> int:
    # A persistent WORK_DIR makes this cheap to re-run: the transcript is keyed
    # on the audio's hash, so a second run against the same episode skips
    # recognition entirely and only re-asks the questions.
    workdir = Path(os.environ.get("WORK_DIR") or tempfile.mkdtemp(
        prefix="transcribe-check-"
    ))
    workdir.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        bot_token="x", api_key="x", api_secret="x", data_dir=workdir
    )
    store = Store(workdir / "check.db")
    store.connect()

    recognizer = LocalWhisper(
        model=settings.asr_model,
        download_root=settings.asr_model_dir,
        cpu_threads=settings.asr_threads,
    )
    indexer = Indexer(settings, store, recognizer)

    async def progress(update):
        print(f"   … {update.stage}", flush=True)

    print(f"— indexing {url}")
    started = time.monotonic()
    transcript = await indexer.transcript_id(
        "check-1", url, workdir / "job", progress
    )
    elapsed = time.monotonic() - started

    rows = store._execute(
        "SELECT duration_s, language, quarantined FROM transcripts WHERE id = ?",
        (transcript,),
    )[0]
    windows = store._execute(
        "SELECT count(*) FROM windows WHERE transcript_id = ?", (transcript,)
    )[0][0]
    duration = rows["duration_s"] or 0

    if duration:
        print(
            f"\n   {format_duration(duration)} of audio in {elapsed:.0f}s "
            f"(RTF {elapsed / duration:.2f}x)"
        )
    else:
        print(f"\n   done in {elapsed:.0f}s")
    print(
        f"   language={rows['language']} windows={windows} "
        f"quarantined={rows['quarantined']}"
    )

    for query in queries:
        print(f"\n— «{query}»")

        # What the recogniser actually wrote, before asking whether the index
        # can find it. A miss has two very different causes — the words were
        # never recognised, or they were recognised in a form the index cannot
        # match — and only one of them is a search problem.
        stem = query.split()[0][:6].lower()
        heard = store._execute(
            "SELECT start_ms, text FROM utterances "
            "WHERE lower(text) LIKE ? AND transcript_id = ? LIMIT 4",
            (f"%{stem}%", transcript),
        )
        for row in heard:
            print(
                f"   heard at {format_duration(row['start_ms'] // 1000)}: "
                f"…{row['text'][:90]}…"
            )
        if not heard:
            print(f"   the recogniser never wrote anything containing {stem!r}")

        moments = await indexer.search(transcript, query)
        if not moments:
            print("   -> nothing found")
            continue
        for moment in moments:
            # Show the text around the match, not the start of a 30-second
            # window: the window opens wherever the clock said, which is
            # usually nowhere near the words that matched.
            body = " ".join(moment.text.split())
            position = body.lower().find(stem)
            start = max(0, position - 40) if position >= 0 else 0
            print(
                f"   -> {format_duration(int(moment.clip_start))}  "
                f"…{body[start:start + 110]}…"
            )

    await store.aclose()
    print(f"\nWork kept in {workdir}")
    return 0


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    asked = sys.argv[2:] or DEFAULT_QUERIES
    raise SystemExit(asyncio.run(main(target, asked)))
