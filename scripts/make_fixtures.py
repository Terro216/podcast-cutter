"""Produce the transcript fixtures a basket is measured over.

A basket is run twice — once against a reference transcript and once against
what production's model actually wrote — and the gap between them is the price
of that model. Both runs have to be cheap and deterministic or they will not
live in CI, so the expensive half happens here, once, offline, and its output
is committed.

    # what production ships, ~30 minutes for both baskets
    python scripts/make_fixtures.py evals/baskets/*.yaml --variant asr --model base

    # the reference, ~8 hours for both baskets on this host
    python scripts/make_fixtures.py evals/baskets/*.yaml \\
        --variant reference --model large-v3

Measured on big-one, 180 s sample, int8, 8 physical cores of one socket:
``base`` runs at RTF 0.079, ``medium`` at 0.665 and ``large-v3`` at 1.252. The
reference pass is therefore an overnight job, and it belongs on the socket the
bot is *not* pinned to — production has ``cpuset: "0-7"``, so:

    docker run --rm --user root --cpuset-cpus 8-15 --cpuset-mems 1 \\
      -v podcast-asr-bench:/bench -e HF_HOME=/bench/hf \\
      -v /home/me/server/projects/podcast-cutter:/app -w /app \\
      --entrypoint python podcast-cutter-podcast-cutter:latest \\
      scripts/make_fixtures.py evals/baskets/ru.yaml --variant reference \\
      --model large-v3 --work /bench/work

Resumable, because eight hours is long enough for something to go wrong: an
episode whose fixture already exists is skipped, and the decoded audio is kept
so the second variant does not re-download anything.

**Off the production host** — the reference pass is much faster on an Apple
Silicon laptop, and it needs nothing from big-one. `ffmpeg` on `PATH`, then:

    poetry install
    poetry run python scripts/make_fixtures.py evals/baskets/ru.yaml \\
        evals/baskets/en.yaml --variant reference --model large-v3 --threads 10

It downloads the episodes itself and writes straight into `evals/fixtures/`,
so the result is a `git add` away. Keep `--compute int8`: the reference is
going to be corrected by hand regardless, and float32 buys hours rather than
accuracy where it matters.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from podcast_cutter.asr import LocalWhisper  # noqa: E402
from podcast_cutter.config import Settings  # noqa: E402
from podcast_cutter.evals import (  # noqa: E402
    EpisodeRef,
    dump_utterances,
    load_basket,
    load_meta,
)
from podcast_cutter.indexer import (  # noqa: E402
    _decode_for_asr,
    _download_with_fallback,
    _resolve_url,
)
from podcast_cutter.proxy import MediaProxy  # noqa: E402
from podcast_cutter.text import format_duration  # noqa: E402
from podcast_cutter.urls import ensure_safe_source  # noqa: E402


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _audio_of_other_variants(
    fixtures: Path, episode: EpisodeRef, variant: str
) -> tuple[str, str] | None:
    """``(variant, source_sha256)`` of some already-written sibling fixture."""
    for other, name in episode.transcripts.items():
        if other == variant or not name:
            continue
        path = fixtures / name
        if path.exists():
            digest = load_meta(path).get("source_sha256")
            if digest:
                return other, digest
    return None


async def _audio_for(
    episode: EpisodeRef, settings: Settings, proxy: MediaProxy, work: Path
) -> tuple[Path, str]:
    """The episode as 16 kHz mono PCM, plus the hash of what was downloaded.

    The decoded audio is kept rather than cleaned up: the two variants of one
    episode must be transcribed from *the same bytes*, or the comparison
    quietly includes whatever advertisement the feed inserted between the two
    runs — the failure `TranscriptKey` exists to make impossible in production.

    **The hash is of the download, not of the decode**, which is the same
    choice `indexer._transcribe` makes and for a reason learned the hard way
    here: decoded PCM depends on the ffmpeg that produced it. The reference
    pass runs on a laptop and the shipped-model pass runs in a Linux container,
    and those two decode one identical mp3 into two different byte streams. A
    guard on the decoded hash therefore fires on every cross-machine run and
    says "the feed changed", which is both wrong and alarming. The downloaded
    bytes are the thing that is actually supposed to be identical.

    The hash is cached beside the audio, so resuming a run does not have to
    re-download an episode just to remember what it was.
    """
    decoded = work / f"{episode.slug}.wav"
    fingerprint = work / f"{episode.slug}.sha256"
    if decoded.exists() and fingerprint.exists():
        return decoded, fingerprint.read_text().strip()

    source = work / f"{episode.slug}.bin"
    await ensure_safe_source(episode.audio_url)
    resolved, route = await _resolve_url(
        episode.audio_url, settings.probe_timeout, proxy
    )
    await ensure_safe_source(resolved)
    print(f"   fetching via {route}", flush=True)
    await _download_with_fallback(resolved, source, settings, proxy, route)

    digest = await asyncio.to_thread(_sha256, source)
    await _decode_for_asr(source, decoded, settings.ffmpeg_timeout)
    source.unlink(missing_ok=True)
    fingerprint.write_text(digest, encoding="utf-8")
    return decoded, digest


def _reporter(started: float, total: float):
    """A callback that says how far into the audio the recogniser has got.

    An hour of silence looks identical to a hang, and this script had exactly
    that shape: nothing between "fetching" and a finished episode. The bot
    learned this lesson already — §15, where a 30-minute episode looked hung
    behind one unchanging line — and `Recognizer.transcribe` has carried the
    hook for it since. Not using it here was an oversight, not a decision.

    The remaining time is derived from work actually done rather than from an
    estimate made before starting, which is the same choice the bot makes and
    the only one that survives a machine being slower than expected.

    Called from a worker thread, so it only formats and prints.
    """
    state = {"last": 0.0}

    def report(end: float) -> None:
        # Per recognised segment is far too often for a terminal; once per
        # thirty seconds of audio is about one line every few seconds.
        if end - state["last"] < 30.0:
            return
        state["last"] = end
        spent = time.monotonic() - started
        rtf = spent / end if end else 0.0
        line = f"   {format_duration(int(end))} recognised, RTF {rtf:.2f}"
        if total:
            remaining = max(0.0, total - end) * rtf
            line += f", {end / total:.0%}, ~{remaining / 60:.0f} min left"
        print(f"{line:<78}", end="\r", flush=True)

    return report


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baskets", nargs="+", type=Path)
    parser.add_argument("--variant", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument(
        "--compute",
        default="int8",
        help=(
            "CTranslate2 compute type. int8 by default, which is what "
            "production runs and what the RTF figures above were measured "
            "with. float32 costs several times the time for a reference "
            "transcript that is going to be hand-corrected anyway."
        ),
    )
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "evals" / "fixtures",
    )
    parser.add_argument("--work", type=Path, default=Path("/tmp/basket-fixtures"))
    args = parser.parse_args(argv)

    args.work.mkdir(parents=True, exist_ok=True)
    # No MEDIA_PROXY: this runs in a bench container outside the compose
    # network, where the proxy's hostname does not resolve. Every episode in
    # the baskets was checked to answer directly before it was chosen, so the
    # detour has nothing to add and a dead one would only add latency.
    settings = Settings(bot_token="x", api_key="x", api_secret="x", data_dir=args.work)
    proxy = MediaProxy(settings)
    recognizer = LocalWhisper(
        model=args.model,
        download_root=settings.asr_model_dir,
        compute_type=args.compute,
        cpu_threads=args.threads,
    )

    todo: list[tuple[EpisodeRef, Path]] = []
    for path in args.baskets:
        basket = load_basket(path)
        for episode in basket.episodes.values():
            name = episode.transcripts.get(args.variant)
            if name is None:
                print(f"!! {episode.slug}: no {args.variant!r} fixture named")
                continue
            todo.append((episode, args.fixtures / name))

    # Said up front, because the difference between "twenty minutes" and "all
    # night" is the whole reason the RTF above was measured rather than guessed.
    hours = sum(episode.duration_s or 0 for episode, _ in todo) / 3600
    print(
        f"{len(todo)} episodes, {hours:.1f} h of audio, "
        f"variant={args.variant} model={args.model}\n"
    )

    for index, (episode, target) in enumerate(todo, start=1):
        head = f"[{index}/{len(todo)}] {episode.slug}"
        if target.exists():
            print(f"{head}: already done, skipping")
            continue

        print(f"{head}: {episode.title[:60]}")
        decoded, digest = await _audio_for(episode, settings, proxy, args.work)

        # The two variants have to come from the same download or the gap
        # between them is partly a gap between two different recordings. Feeds
        # insert advertisements dynamically — the reason production keys a
        # transcript on `source_sha256` at all — and a reference pass run on
        # another machine, days later, is exactly when that bites.
        sibling = _audio_of_other_variants(args.fixtures, episode, args.variant)
        if sibling is not None and sibling[1] != digest:
            other, expected = sibling
            raise SystemExit(
                f"\n{episode.slug}: the feed is serving different bytes than "
                f"the {other!r} fixture was built from.\n"
                f"  {other}: {expected[:16]}…\n  now:  {digest[:16]}…\n"
                f"Timestamps taken against one would not fit the other. "
                f"Delete the {other!r} fixture and rebuild both variants from "
                f"this download."
            )

        started = time.monotonic()
        utterances, language = await recognizer.transcribe(
            decoded, on_segment=_reporter(started, episode.duration_s)
        )
        elapsed = time.monotonic() - started
        # Leave the progress line behind rather than on top of the result.
        print(" " * 78, end="\r")
        spoken = max((u.end for u in utterances), default=0.0)

        dump_utterances(
            utterances,
            target,
            {
                "episode_id": episode.slug,
                "audio_url": episode.audio_url,
                # Of the download, matching what production stores. Hashing
                # the *decoded* audio instead is the obvious-looking choice
                # and it is wrong: two ffmpeg builds turn one identical mp3
                # into two different byte streams, so the check would fail on
                # every cross-machine run and blame the feed for it.
                "source_sha256": digest,
                "backend": recognizer.backend,
                "model": args.model,
                "language": language,
                "seconds": round(spoken, 1),
                "recognised_in_s": round(elapsed, 1),
            },
        )
        rtf = elapsed / spoken if spoken else 0.0
        print(
            f"   {format_duration(int(spoken))} in {elapsed / 60:.1f} min "
            f"(RTF {rtf:.2f}), {len(utterances)} utterances, lang={language}",
            flush=True,
        )

    print(f"\nFixtures in {args.fixtures}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
