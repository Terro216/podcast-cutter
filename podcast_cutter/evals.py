"""Measuring how often the search is wrong, and in which of several ways.

Every search defect so far was found by a person noticing something odd — that
«нейросети» found nothing in an episode about neural networks, that the clip
opened twenty seconds early, that the three answers quoted text none of them
contained. Each was real, each was fixed, and none was found by a method.
`ROADMAP.md` §5 exists because "someone might notice" does not answer the
question that matters: *how often* is a search wrong, and in which of several
possible ways.

The ways are different failures with different fixes, so they are counted
separately:

* **Missed it.** The moment is in the episode and the answer does not contain
  it. Either the recogniser never wrote the words or the index could not match
  what it wrote — and only the second is a search problem.
* **Found it, but in the wrong place.** The right moment, a start too far off
  to be usable. A tolerance counts this as a miss on purpose: a clip opening
  forty seconds early is not a hit that needs rounding.
* **Made it up.** A phrase never spoken, answered with a confident best guess.
  This is the failure that costs trust, and counting it over the negative
  queries is the only defence — `false_hit_rate`.

**Two runs of one basket.** A basket is measured twice: over a reference
transcript, and over what the shipped model actually produced from the same
audio. Neither number means much alone. The *gap* is the price of the model,
and without it there is no telling whether to change the recogniser or the
retriever — weeks of work in different directions.

Both runs are offline. The expensive part is *producing* a transcript, not
searching one, so both are committed as fixtures and a run is seconds of pure
CPU with no model and no network. That is what lets the comparison live in CI
rather than in a script nobody remembers to run.
"""

from __future__ import annotations

import gzip
import json
import logging
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .audio import parse_timestamp
from .errors import ConfigError
from .indexer import RESULTS as ANSWERS
from .transcripts import CHUNKER_VERSION, Utterance, Word, build

logger = logging.getLogger(__name__)

#: How far a returned start may be from the reference and still be the moment
#: that was asked for. The figure in `ROADMAP.md` §5, and chosen from what the
#: clip editor does rather than from statistics: the nudge buttons move in
#: 15-second steps, so a hit inside this is one tap from perfect.
TOLERANCE_SECONDS = 15.0

#: A positive query asks for a moment that exists; a negative asks for one that
#: does not, where the only right answer is nothing at all.
KINDS = ("quote", "meaning", "mention", "negative")

#: How far a metric may move before it counts as a regression. Rates are
#: fractions and the error is seconds, so one number for both would be either
#: meaningless on one or useless on the other. The rate slack is a little over
#: one query in thirty, which is the wobble a basket this size has by
#: construction.
SLACK: Mapping[str, float] = {
    "hit@1": 0.02,
    f"hit@{ANSWERS}": 0.02,
    "false_hit_rate": 0.02,
    "median_error_s": 1.0,
}

#: Metrics where a bigger number is a worse result. Getting this backwards
#: would make the regression check silently pass on exactly the two failures it
#: exists to catch.
LOWER_IS_BETTER = frozenset({"false_hit_rate", "median_error_s"})


class BasketError(ConfigError):
    """A basket file says something this runner cannot act on."""

    code = "basket_invalid"


# ----------------------------------------------------------------------
# What a basket is
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Query:
    """One question put to the search, and what counts as answering it."""

    id: str
    episode: str
    text: str
    kind: str
    #: Every place in the episode that legitimately answers this query. A list
    #: rather than one number because the `mention` class exists precisely for
    #: phrases said several times: with a single reference, returning the
    #: second and third real occurrences would score as two misses.
    at: tuple[float, ...] = ()
    #: Free text kept beside the query, usually recording what the recogniser
    #: was heard to do with it. This is where «base слышит "голки в 100
    #: гисены"» lives, and it is the difference between a red number and a
    #: diagnosis.
    note: str = ""

    @property
    def is_negative(self) -> bool:
        return self.kind == "negative"


@dataclass(frozen=True, slots=True)
class EpisodeRef:
    """An episode a basket asks questions about.

    ``audio_url`` is carried so a fixture can be regenerated from the same
    bytes rather than from whatever the feed serves next month, and
    ``condition`` so a bad number can be attributed: studio, remote and field
    recordings fail differently, and one average over the three hides which.
    """

    slug: str
    title: str
    podcast: str
    audio_url: str
    condition: str
    #: Variant name -> fixture filename. ``reference`` and ``asr`` are the two
    #: the comparison stands on; more may exist while a model is being tried.
    transcripts: Mapping[str, str] = field(default_factory=dict)
    #: The Podcast Index id, so an episode in a basket can be found in the
    #: production journal — and so a fixture can be regenerated years later
    #: from the directory rather than from a URL that has since expired.
    episode_id: str = ""
    #: What the feed advertises. Only used to say how long a fixture run will
    #: take before it starts, which at eight hours is worth saying.
    duration_s: int = 0


@dataclass(frozen=True, slots=True)
class Basket:
    """One language's queries, the episodes they ask about, and the numbers
    this basket produced when it was last committed."""

    language: str
    episodes: Mapping[str, EpisodeRef]
    queries: tuple[Query, ...]
    #: Variant -> metric -> value, as last measured. The runner does not assert
    #: absolute quality against these; it asserts a change did not make things
    #: worse. See :meth:`Report.regressions`.
    baseline: Mapping[str, Mapping[str, float]] = field(default_factory=dict)

    def for_episode(self, slug: str) -> EpisodeRef:
        try:
            return self.episodes[slug]
        except KeyError:
            known = ", ".join(sorted(self.episodes)) or "none"
            raise BasketError(
                f"A query refers to episode {slug!r}, which this basket does "
                f"not describe. Known: {known}."
            ) from None


def _timestamps(raw, query_id: str) -> tuple[float, ...]:
    """Parse the ``at:`` list, written the way a person reads a clock.

    Reuses the bot's own parser rather than a second one: a basket saying
    ``12:34`` and a user typing ``12:34`` must mean the same instant, and two
    implementations of that eventually disagree.
    """
    if raw is None:
        return ()
    if isinstance(raw, (str, int, float)):
        raw = [raw]
    stamps = []
    for item in raw:
        try:
            stamps.append(float(parse_timestamp(str(item))))
        except Exception as exc:
            raise BasketError(
                f"Query {query_id!r} has an unreadable timestamp {item!r}: {exc}"
            ) from None
    return tuple(sorted(stamps))


def _read_query(item: Mapping, source: str, seen: set[str]) -> Query:
    identifier = str(item.get("id") or "")
    if not identifier:
        raise BasketError(f"A query in {source} has no id.")
    if identifier in seen:
        raise BasketError(f"Duplicate query id {identifier!r} in {source}.")

    kind = item.get("kind", "quote")
    if kind not in KINDS:
        raise BasketError(
            f"Query {identifier!r} has kind {kind!r}; expected one of "
            f"{', '.join(KINDS)}."
        )

    stamps = _timestamps(item.get("at"), identifier)
    # A positive query with no reference cannot be scored, and scoring it as a
    # miss would quietly depress every number in the report. Refuse it while
    # the basket is being written, which is when it can still be fixed.
    if kind != "negative" and not stamps:
        raise BasketError(
            f"Query {identifier!r} is a {kind} query with no `at:` timestamps, "
            f"so nothing can say whether an answer is right."
        )
    if kind == "negative" and stamps:
        raise BasketError(
            f"Query {identifier!r} is negative but names timestamps. A negative "
            f"asks for something that was never said."
        )

    return Query(
        id=identifier,
        episode=str(item.get("episode", "")),
        text=str(item.get("query", "")),
        kind=kind,
        at=stamps,
        note=str(item.get("note", "")),
    )


def load_basket(path: Path) -> Basket:
    """Read a basket file.

    YAML rather than JSON because a basket is hand-written and hand-read, and
    the comments are half its value: a query without the note saying what the
    recogniser did with it is a number nobody can act on.
    """
    # Imported here, not at module scope. The bot never loads a basket, so the
    # production image does not ship PyYAML — the same reason `pymorphy3` and
    # `faster_whisper` are imported where they are used rather than at the top.
    import yaml

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    episodes = {
        slug: EpisodeRef(
            slug=slug,
            title=item.get("title", slug),
            podcast=item.get("podcast", ""),
            audio_url=item.get("audio_url", ""),
            condition=item.get("condition", "unknown"),
            transcripts=dict(item.get("transcripts") or {}),
            episode_id=str(item.get("episode_id", "")),
            duration_s=int(item.get("duration_s") or 0),
        )
        for slug, item in (raw.get("episodes") or {}).items()
    }

    queries: list[Query] = []
    seen: set[str] = set()
    for item in raw.get("queries") or []:
        query = _read_query(item, path.name, seen)
        seen.add(query.id)
        queries.append(query)

    basket = Basket(
        language=str(raw.get("language", "")),
        episodes=episodes,
        queries=tuple(queries),
        baseline={
            variant: dict(values)
            for variant, values in (raw.get("baseline") or {}).items()
        },
    )
    # Checked once, here, rather than when a run reaches the offending query:
    # a typo in an episode slug should fail the basket, not the twentieth
    # measurement in it.
    for query in basket.queries:
        basket.for_episode(query.episode)
    return basket


# ----------------------------------------------------------------------
# Transcript fixtures
# ----------------------------------------------------------------------


def load_utterances(path: Path) -> list[Utterance]:
    """Read a committed transcript.

    Gzipped, because a 50-minute episode with word timings is most of a
    megabyte of JSON and there are two per episode. The suffix decides, so an
    uncompressed file stays readable by hand while it is being corrected.
    """
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)

    return [
        Utterance(
            start=float(item["start"]),
            end=float(item["end"]),
            text=str(item.get("text", "")),
            words=tuple(
                Word(
                    start=float(word["start"]),
                    end=float(word["end"]),
                    text=str(word.get("text", "")),
                    probability=word.get("probability"),
                )
                for word in item.get("words") or ()
            ),
            avg_logprob=item.get("avg_logprob"),
            no_speech_prob=item.get("no_speech_prob"),
            compression_ratio=item.get("compression_ratio"),
        )
        for item in payload.get("utterances") or ()
    ]


def load_meta(path: Path) -> dict:
    """What produced a fixture, without caring what it says.

    Separate from :func:`load_utterances` because the interesting question is
    usually about provenance — which model wrote this, and from which audio —
    and answering it should not require caring about ten thousand words.
    """
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        return json.load(handle).get("meta") or {}


def _word_payload(word: Word) -> dict:
    payload = {
        "start": round(word.start, 3),
        "end": round(word.end, 3),
        "text": word.text,
    }
    if word.probability is not None:
        payload["probability"] = round(word.probability, 4)
    return payload


def _utterance_payload(utterance: Utterance) -> dict:
    payload = {
        "start": round(utterance.start, 3),
        "end": round(utterance.end, 3),
        "text": utterance.text,
        "words": [_word_payload(word) for word in utterance.words],
    }
    for name, value in (
        ("avg_logprob", utterance.avg_logprob),
        ("no_speech_prob", utterance.no_speech_prob),
        ("compression_ratio", utterance.compression_ratio),
    ):
        if value is not None:
            payload[name] = round(value, 4)
    return payload


def dump_utterances(utterances: Sequence[Utterance], path: Path, meta: dict) -> None:
    """Write a transcript fixture, with what produced it recorded alongside.

    The metadata is not decoration: a basket compares a reference against one
    specific model's output, and a fixture that does not say which model wrote
    it becomes unattributable the first time the model changes.
    """
    payload = {
        "meta": {**meta, "chunker_version": CHUNKER_VERSION},
        "utterances": [_utterance_payload(item) for item in utterances],
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "wt", encoding="utf-8") as handle:
        # Indented: a fixture corrected by one word should show a one-line
        # diff, not a rewritten file.
        json.dump(payload, handle, ensure_ascii=False, indent=1)


# ----------------------------------------------------------------------
# Scoring
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Answer:
    """What the search returned for one query, and how it scored."""

    query: Query
    #: Where each returned moment would open the clip, in the order shown.
    starts: tuple[float, ...]
    #: 1-based position of the first answer close enough to be the moment, or
    #: ``None`` if none were.
    rank: int | None
    #: How far that answer's start was from its nearest reference. ``None``
    #: on a miss — averaging error over a miss is meaningless, because a miss
    #: can be forty minutes out and would swamp the number.
    error: float | None
    #: How many *different* reference points the answers landed on. Interesting
    #: for `mention` queries, where three answers on three real occurrences is
    #: the behaviour clustering exists to produce.
    distinct: int

    @property
    def is_false_hit(self) -> bool:
        """A negative query answered with anything at all."""
        return self.query.is_negative and bool(self.starts)


def score(
    query: Query, starts: Sequence[float], tolerance: float = TOLERANCE_SECONDS
) -> Answer:
    """Judge one set of returned starts against a query's references."""
    rank: int | None = None
    error: float | None = None
    matched: set[float] = set()

    for position, start in enumerate(starts, start=1):
        if not query.at:
            break
        nearest = min(query.at, key=lambda reference: abs(start - reference))
        distance = abs(start - nearest)
        if distance <= tolerance:
            matched.add(nearest)
            if rank is None:
                rank, error = position, distance

    return Answer(
        query=query,
        starts=tuple(starts),
        rank=rank,
        error=error,
        distinct=len(matched),
    )


@dataclass(frozen=True, slots=True)
class Report:
    """What a basket run says, in the shape the roadmap asks for."""

    label: str
    positives: int
    hits_at_1: int
    hits_at_k: int
    negatives: int
    false_hits: int
    errors: tuple[float, ...]

    @property
    def hit_at_1(self) -> float | None:
        return self.hits_at_1 / self.positives if self.positives else None

    @property
    def hit_at_k(self) -> float | None:
        return self.hits_at_k / self.positives if self.positives else None

    @property
    def false_hit_rate(self) -> float | None:
        return self.false_hits / self.negatives if self.negatives else None

    @property
    def mean_error(self) -> float | None:
        return statistics.fmean(self.errors) if self.errors else None

    @property
    def median_error(self) -> float | None:
        """Reported instead of the mean, because they disagree usefully here.

        Placement lands either on the matched word or — when `locate_phrase`
        cannot find it — on the window start, which is a bimodal distribution.
        A mean over those two cases describes neither.

        **Read the floor as ~2 s, not 0.** A reference timestamp names when the
        word was said and `CLIP_LEAD_IN` opens the clip deliberately earlier,
        so a placement that is exactly right measures as two seconds of error.
        A number near 2 s is the good case; it is drift above that which means
        the clip is opening on the wrong sentence.
        """
        return statistics.median(self.errors) if self.errors else None

    def as_dict(self) -> dict[str, float]:
        """The metrics as plain numbers, for comparing against a baseline."""
        values = {
            "hit@1": self.hit_at_1,
            f"hit@{ANSWERS}": self.hit_at_k,
            "false_hit_rate": self.false_hit_rate,
            "median_error_s": self.median_error,
        }
        return {name: value for name, value in values.items() if value is not None}

    def regressions(self, baseline: Mapping[str, float]) -> list[str]:
        """Where this run is worse than the numbers last committed.

        A basket cannot assert absolute quality: nobody knows in advance what
        `hit@3` *should* be on a given set of episodes, and a threshold invented
        before the first measurement is a guess wearing a requirement's clothes.
        What a basket can assert is that a change did not make things worse, and
        that a number which moves is noticed and re-committed deliberately.
        """
        measured = self.as_dict()
        complaints = []
        for name, expected in baseline.items():
            actual = measured.get(name)
            if actual is None:
                continue
            slack = SLACK.get(name, 0.0)
            if name in LOWER_IS_BETTER:
                worse, direction = actual > expected + slack, "rose"
            else:
                worse, direction = actual < expected - slack, "fell"
            if worse:
                complaints.append(
                    f"{self.label}: {name} {direction} from {expected:.3f} "
                    f"to {actual:.3f}"
                )
        return complaints


def summarize(answers: Iterable[Answer], label: str) -> Report:
    """Roll a run's answers up into one line of a table."""
    answers = list(answers)
    positive = [answer for answer in answers if not answer.query.is_negative]
    negative = [answer for answer in answers if answer.query.is_negative]
    hits_at_k = sum(
        1
        for answer in positive
        if answer.rank is not None and answer.rank <= ANSWERS
    )

    return Report(
        label=label,
        positives=len(positive),
        hits_at_1=sum(1 for answer in positive if answer.rank == 1),
        hits_at_k=hits_at_k,
        negatives=len(negative),
        false_hits=sum(1 for answer in negative if answer.is_false_hit),
        errors=tuple(a.error for a in positive if a.error is not None),
    )


def by_kind(answers: Iterable[Answer], label: str) -> dict[str, Report]:
    """The same roll-up, split by query class.

    Kept out of :func:`summarize` rather than nested in its result: the classes
    test different parts of the system — `quote` the recogniser, `meaning` the
    retriever, `negative` the refusal — and an overall number that moves says
    nothing about which of them changed.
    """
    groups: dict[str, list[Answer]] = {}
    for answer in answers:
        groups.setdefault(answer.query.kind, []).append(answer)
    return {
        kind: summarize(group, f"{label}/{kind}")
        for kind, group in sorted(groups.items())
    }


def _percent(value: float | None) -> str:
    return "     —" if value is None else f"{value * 100:5.1f}%"


def _seconds(value: float | None) -> str:
    return "     —" if value is None else f"{value:5.1f}s"


def render(reports: Sequence[Report]) -> str:
    """One table, because the point of two runs is reading them side by side."""
    header = (
        f"{'run':<28} {'n':>4} {'hit@1':>6} {f'hit@{ANSWERS}':>6} "
        f"{'err':>6} {'false':>6}"
    )
    lines = [header, "-" * len(header)]
    for report in reports:
        lines.append(
            f"{report.label:<28} {report.positives + report.negatives:>4} "
            f"{_percent(report.hit_at_1)} {_percent(report.hit_at_k)} "
            f"{_seconds(report.median_error)} {_percent(report.false_hit_rate)}"
        )
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Running one
# ----------------------------------------------------------------------


async def run_variant(
    basket: Basket,
    fixtures: Path,
    variant: str,
    db: Path,
    tolerance: float = TOLERANCE_SECONDS,
) -> list[Answer]:
    """Index every episode's ``variant`` transcript, then ask every question.

    Deliberately goes through :meth:`Indexer.search` and the real store rather
    than calling the retrieval helpers directly. An eval that reimplements the
    path it measures measures the reimplementation — and both defects this
    project has had in placement and clustering lived *between* those helpers,
    where a shortcut would not have looked.
    """
    from .config import Settings
    from .indexer import Indexer
    from .store import Store, TranscriptKey

    store = Store(db)
    store.connect()
    settings = Settings(bot_token="x", api_key="x", api_secret="x", data_dir=db.parent)
    # No recogniser: a basket never transcribes. Everything expensive already
    # happened, offline, and is sitting in the fixtures.
    indexer = Indexer(settings, store, recognizer=None)

    try:
        transcript_ids = {}
        for slug, episode in basket.episodes.items():
            name = episode.transcripts.get(variant)
            if name is None:
                raise BasketError(
                    f"Episode {slug!r} has no {variant!r} transcript, so the "
                    f"{variant} run cannot cover it."
                )
            result = build(load_utterances(fixtures / name))
            transcript_ids[slug] = await store.save_transcript(
                TranscriptKey(
                    episode_id=f"{basket.language}:{slug}:{variant}",
                    # The fixture *is* the audio as far as a basket is
                    # concerned, so its name stands in for the hash. Keeping
                    # the field distinct matters: two variants of one episode
                    # must not collapse into a single stored transcript.
                    source_sha256=f"fixture:{variant}:{name}",
                    asr_backend="fixture",
                    asr_model=variant,
                    chunker_version=CHUNKER_VERSION,
                ),
                {"source_url": episode.audio_url, "language": basket.language},
                result,
            )

        answers = []
        for query in basket.queries:
            moments = await indexer.search(transcript_ids[query.episode], query.text)
            starts = [moment.clip_start for moment in moments]
            answers.append(score(query, starts, tolerance=tolerance))
        return answers
    finally:
        await store.aclose()
