"""Help write a basket's answer key, and then check it.

Drafting sixty queries by scrubbing six hours of audio is the part of §5 that
sounds cheap and is not. Both modes here exist to turn it into reading.

    # what is worth asking about, and what is safe to ask about and absent
    python scripts/draft_queries.py evals/baskets/ru.yaml --draft

    # once the queries are written: the ±30 s to listen to, and what the
    # search does with each of them today
    python scripts/draft_queries.py evals/baskets/ru.yaml --verify

**Drafting.** Candidates are picked by how a word is distributed across the
basket, which needs no stopword list and no dictionary — both of which would be
another thing to be wrong in another language. A word the basket says only in
this episode is distinctive; one it says several times here is a `mention`
candidate; and — the useful one — a word said in *other* episodes of the basket
but never in this one is a **negative** that is plausible for the subject matter
rather than invented. «квантовая телепортация» is a fine negative but it is one
somebody thought of; "this show talks about it, this episode does not" is the
kind a user would actually type and be wrong about.

**Verifying.** Nothing here decides whether a timestamp is right — a person
listening decides that. What it does is put the reference text around each
claimed answer in front of them, and show what both variants return today, so a
wrong `at:` shows up as text that does not contain the phrase.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from podcast_cutter.evals import (  # noqa: E402
    load_basket,
    load_utterances,
    run_variant,
)
from podcast_cutter.text import format_duration  # noqa: E402
from podcast_cutter.transcripts import build, lemma, normalize  # noqa: E402

#: A word has to be at least this long to be a candidate. Short tokens are
#: overwhelmingly function words in both languages, and filtering by length
#: costs nothing where a stopword list costs a list per language.
MIN_WORD = 5

#: And no longer than this. Nothing in either language is 30 characters, so a
#: token that long is a decoder loop collapsed into one word — «генегенегене…»
#: at 448 characters is real, from `base` on obrecheny-startup. Proposing one
#: as a query would be drafting an answer key out of a hallucination.
MAX_WORD = 30

#: How much reference text to show around a claimed timestamp. Wider than the
#: ±15 s tolerance on purpose: the question being answered by ear is "is this
#: the moment", and that needs the sentence before it.
CONTEXT = 30.0


def _usable(token: str) -> bool:
    return MIN_WORD <= len(token) <= MAX_WORD


def _fixture_path(fixtures: Path, episode, variant: str) -> Path | None:
    """The fixture for this variant, or ``None`` if there is not one.

    An episode naming no fixture has to be caught on the empty name: joining
    the empty string onto the directory yields the directory, and directories
    exist.
    """
    name = episode.transcripts.get(variant)
    if not name:
        return None
    path = fixtures / name
    return path if path.exists() else None


def _words_at(utterances) -> dict[str, list[float]]:
    """Every lemma the index can actually match, and when it was said.

    Fed from ``build(...).indexable_utterances`` rather than the raw
    transcript, because a quarantined span is not searchable — proposing a
    query from one would write an answer key whose answer is guaranteed to be
    a miss, and the miss would look like a retrieval failure.
    """
    when: dict[str, list[float]] = defaultdict(list)
    for utterance in utterances:
        # Word timings where the recogniser gave them, the utterance start
        # otherwise — a drafting aid does not need editing-grade placement,
        # and `locate_phrase` will do that properly at search time anyway.
        if utterance.words:
            for word in utterance.words:
                for token in normalize(word.text).split():
                    if _usable(token):
                        when[lemma(token)].append(word.start)
        else:
            for token in normalize(utterance.text).split():
                if _usable(token):
                    when[lemma(token)].append(utterance.start)
    return when


def draft(basket, fixtures: Path, variant: str) -> None:
    episodes = {}
    spoken: dict[str, set[str]] = {}
    for slug, episode in basket.episodes.items():
        path = _fixture_path(fixtures, episode, variant)
        if path is None:
            print(f"!! {slug}: no {variant} fixture")
            continue
        utterances = load_utterances(path)
        built = build(utterances)
        if built.quarantined:
            print(f"   {slug}: {built.quarantined} utterances quarantined, skipped")
        episodes[slug] = _words_at(built.indexable_utterances)
        # For negatives the bar is "never *spoken*", not "never searchable":
        # a word confined to a quarantined span is still a word the episode
        # said, and proposing it as a negative writes a required refusal that
        # turns into a false hit the day the quarantine heuristics improve.
        spoken[slug] = set(_words_at(utterances))

    if not episodes:
        return

    for slug, when in episodes.items():
        elsewhere = set().union(
            *(other.keys() for name, other in episodes.items() if name != slug)
        )
        counts = Counter({word: len(times) for word, times in when.items()})

        print(f"\n{'=' * 72}\n{slug} — {basket.episodes[slug].title[:60]}")

        print("\n  quote candidates (said once here, never in the other episodes)")
        once = [w for w, n in counts.items() if n == 1 and w not in elsewhere]
        for word in sorted(once, key=len, reverse=True)[:20]:
            print(f"    {format_duration(int(when[word][0])):>8}  {word}")

        print("\n  mention candidates (said several times here, not elsewhere)")
        many = [w for w, n in counts.items() if n >= 3 and w not in elsewhere]
        for word in sorted(many, key=lambda w: -counts[w])[:12]:
            stamps = ", ".join(
                format_duration(int(t)) for t in sorted(when[word])[:5]
            )
            print(f"    {counts[word]:>3}×  {word:<24} {stamps}")

        print("\n  negative candidates (this basket says them, this episode does not)")
        # Ranked by how hard *one other* episode leans on the word, not by how
        # often the basket says it overall. A word every episode uses is a
        # common word and its absence here is an accident; a word one other
        # episode is *about* is a subject a listener could plausibly expect to
        # find here and be wrong about, which is the negative worth testing.
        loudest: Counter = Counter()
        spread: Counter = Counter()
        for name, other in episodes.items():
            if name == slug:
                continue
            for word, times in other.items():
                loudest[word] = max(loudest[word], len(times))
                spread[word] += 1
        absent = [
            word
            for word in elsewhere
            if word not in spoken[slug] and spread[word] <= 2 and loudest[word] >= 3
        ]
        for word in sorted(absent, key=lambda w: -loudest[w])[:15]:
            print(f"    {loudest[word]:>4}×  {word}")


def _text_around(utterances, at: float, width: float = CONTEXT) -> str:
    inside = [
        utterance.text.strip()
        for utterance in utterances
        if utterance.end > at - width and utterance.start < at + width
    ]
    return " ".join(inside) or "(nothing recognised here)"


async def verify(basket, fixtures: Path, variants: tuple[str, ...]) -> None:
    if not basket.queries:
        print("This basket has no queries yet — run with --draft first.")
        return

    # A throwaway directory rather than a fixed path: WAL is on, so a database
    # is three files, and reusing a name leaves the other two behind to be
    # picked up by the next run.
    scratch = Path(tempfile.mkdtemp(prefix="basket-verify-"))

    answers: dict[str, dict[str, object]] = {}
    for variant in variants:
        # `fixtures / ""` is the fixtures directory, and that exists — so an
        # episode naming no fixture at all has to be caught by the name being
        # empty, not by the path being absent. Getting this wrong here meant
        # the run went ahead and died inside the loader.
        missing = [
            slug
            for slug, episode in basket.episodes.items()
            if not _fixture_path(fixtures, episode, variant)
        ]
        if missing:
            print(f"!! skipping the {variant} run: no fixture for {missing}")
            continue
        db = scratch / f"{basket.language}-{variant}.db"
        answers[variant] = {
            answer.query.id: answer
            for answer in await run_variant(basket, fixtures, variant, db)
        }

    # Prefer the reference, but fall back to whatever exists and say which.
    # Early in the annotation pass the reference does not exist yet, and
    # printing empty context blocks is less useful than printing the shipped
    # model's words with a label warning they are the ones being judged.
    context: dict[str, tuple[str, list]] = {}
    for slug, episode in basket.episodes.items():
        for variant in ("reference", *variants):
            path = _fixture_path(fixtures, episode, variant)
            if path is not None:
                context[slug] = (variant, load_utterances(path))
                break

    for query in basket.queries:
        print(f"\n{'=' * 72}")
        print(f"{query.id}  [{query.kind}]  «{query.text}»")
        if query.note:
            print(f"  note: {query.note}")

        source, utterances = context.get(query.episode, (None, None))
        for at in query.at:
            print(
                f"\n  claimed {format_duration(int(at))} — listen from "
                f"{format_duration(int(max(0, at - 10)))}"
                + (f", {source} says:" if source else " (no transcript):")
            )
            if utterances:
                print(f"    …{_text_around(utterances, at)[:400]}…")

        for variant, found in answers.items():
            answer = found.get(query.id)
            if answer is None:
                continue
            where = ", ".join(
                format_duration(int(start)) for start in answer.starts
            ) or "nothing"
            verdict = (
                "false hit" if answer.is_false_hit
                else "ok" if query.is_negative
                else f"rank {answer.rank}" if answer.rank
                else "MISS"
            )
            # For a phrase said several times, landing on one of them three
            # times is the defect clustering exists to prevent — and it scores
            # a perfect hit@1 while looking broken to whoever taps the buttons.
            if query.kind == "mention" and answer.starts:
                verdict += f", {answer.distinct}/{len(query.at)} occurrences"
            print(f"  {variant:<10} -> {where:<30} {verdict}")


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("basket", type=Path)
    parser.add_argument("--draft", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument(
        "--from-variant",
        default="reference",
        help="Which transcript to draft candidates from (default: reference).",
    )
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "evals" / "fixtures",
    )
    args = parser.parse_args(argv)

    basket = load_basket(args.basket)
    if args.draft:
        draft(basket, args.fixtures, args.from_variant)
    if args.verify:
        await verify(basket, args.fixtures, ("reference", "asr"))
    if not (args.draft or args.verify):
        parser.error("choose --draft or --verify")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
