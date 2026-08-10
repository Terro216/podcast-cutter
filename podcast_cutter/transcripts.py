"""Turning recognised speech into something searchable.

Three pure steps, kept away from the recogniser so they can be tested without
a model and reused whichever backend produced the words:

1. **Quarantine.** Whisper invents text on silence and music, usually as
   repetition. Those inventions are indistinguishable from real speech to a
   search index, so they become confident wrong answers. Nothing is deleted —
   every metric stays on the row — but a segment with two independent signals
   against it stops being searchable.

2. **Windowing.** Recognised utterances are a few seconds long, so a phrase
   spanning two of them matches neither. Search runs over fixed windows of
   about thirty seconds at a fifteen-second stride, which puts any moment
   within 7.5 s of some window's centre and lets a thought that starts at the
   end of one window finish inside the next.

3. **Clustering.** At 50% overlap, neighbouring windows say almost the same
   thing, so the top three hits would otherwise be one moment shown three
   times. Overlapping and adjacent hits collapse into one before the answer is
   cut down to three.

Normalisation for the lexical index is versioned rather than applied in place:
changing what it does changes what the index means, so the column is rebuilt
rather than quietly diverging from the text it came from.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache

logger = logging.getLogger(__name__)

#: Bumped whenever windowing changes shape. Stored per transcript so a rebuilt
#: index is distinguishable from one made by an older rule.
CHUNKER_VERSION = 1
#: Bumped whenever :func:`normalize` or :func:`lemmatize` changes what it
#: produces, because that changes what the stored index *means*.
NORMALIZER_VERSION = 2

WINDOW_SECONDS = 30.0
STRIDE_SECONDS = 15.0

#: Hits closer than this are the same moment described twice.
CLUSTER_GAP_SECONDS = 15.0

# --- quarantine thresholds -------------------------------------------------
#
# These mirror faster-whisper's own fallback triggers. Reaching one there means
# "decode again at a higher temperature", and the final answer is kept even if
# every attempt failed — so the same numbers are re-checked here, where the
# question is whether to let the text be searched at all.
MAX_COMPRESSION_RATIO = 2.4
MIN_AVG_LOGPROB = -1.0
MAX_NO_SPEECH_PROB = 0.6
#: A phrase repeated this many times in one utterance is a decoding loop.
MAX_PHRASE_REPEATS = 4

_WORD_RE = re.compile(r"\w+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class Word:
    """One recognised word, with the timing that lets a clip start on it."""

    start: float
    end: float
    text: str
    probability: float | None = None


@dataclass(frozen=True, slots=True)
class Utterance:
    """One recogniser output span, with the metrics used to judge it.

    The metrics are backend-specific and optional: a cloud recogniser that
    reports none simply never trips the signals that depend on them.
    """

    start: float
    end: float
    text: str
    words: tuple[Word, ...] = ()
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    compression_ratio: float | None = None

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass(frozen=True, slots=True)
class Window:
    """A searchable span of text covering a fixed stretch of the episode."""

    start: float
    end: float
    text: str


@dataclass(frozen=True, slots=True)
class Moment:
    """A place in the episode that answers a query."""

    start: float
    end: float
    text: str
    score: float
    #: Where the clip should actually begin, snapped to a word when the
    #: recogniser gave timings and padded so the first syllable is not clipped.
    clip_start: float = 0.0


def normalize(text: str) -> str:
    """Fold a string to what the lexical index should match on.

    Deliberately minimal. ``ё`` and ``е`` are folded together because feeds and
    speakers disagree about the former, and a search that distinguishes them
    fails in a way nobody can see. Morphology is not handled here: Russian
    needs lemmatisation, which is a dictionary and therefore a decision with a
    version of its own — see :data:`NORMALIZER_VERSION`.
    """
    return " ".join(_WORD_RE.findall(text.lower().replace("ё", "е")))


@lru_cache(maxsize=1)
def _morph():
    """The Russian analyser, built once and only if it is installed.

    Optional on purpose. The bot must start, and the tests must run, on a
    machine without it; what is lost then is inflection matching, not search.
    """
    try:
        import pymorphy3
    except ImportError:  # pragma: no cover - depends on the environment
        logger.warning(
            "pymorphy3 is not installed: Russian searches will only match the "
            "exact word form that was spoken."
        )
        return None
    return pymorphy3.MorphAnalyzer()


@lru_cache(maxsize=100_000)
def lemma(word: str) -> str:
    """The dictionary form of one word, or the word itself.

    Cached because episodes repeat themselves: a 50-minute transcript is some
    thousands of tokens over a few hundred distinct words, and the windows
    overlap, so every word is looked up at least twice.

    Words the dictionary does not know — names, brands, English, and whatever
    the recogniser garbled — come back unchanged, which is the behaviour that
    matters most. The dictionaries date to 2022, so "unknown" covers rather
    more than it sounds like it should.
    """
    morph = _morph()
    if morph is None:
        return word
    try:
        parsed = morph.parse(word)
    except (ValueError, IndexError):  # pragma: no cover - defensive
        return word
    return parsed[0].normal_form if parsed else word


def lemmatize(text: str) -> str:
    """Normalise, then fold every word to its dictionary form.

    This is what the lexical index is built on and what queries are matched
    against, so «нейросети» finds «нейросетей». Measured need, not a
    precaution: on a real episode about neural networks, the recogniser wrote
    «нейросетей» four times and «нейросети» never once, and the search for the
    episode's own subject came back empty.
    """
    return " ".join(lemma(word) for word in normalize(text).split())


def repeated_phrase_count(text: str, size: int = 4) -> int:
    """How often the most repeated ``size``-word phrase occurs.

    A decoding loop is the failure mode that most looks like speech: fluent,
    well-formed, and the same thing over and over. Counting the commonest
    n-gram catches it without a list of known bad phrases, which would only
    ever describe the loops we happened to have seen.
    """
    words = normalize(text).split()
    if len(words) < size:
        return 1
    grams = Counter(
        tuple(words[index : index + size]) for index in range(len(words) - size + 1)
    )
    return max(grams.values())


def quarantine_signals(utterance: Utterance) -> list[str]:
    """Everything suggesting this text was not spoken.

    Returned rather than acted upon: the caller decides what a single signal
    is worth, and the reasons are stored so a bad decision can be reviewed
    instead of guessed at.
    """
    signals: list[str] = []

    if not normalize(utterance.text):
        signals.append("empty")

    if (
        utterance.compression_ratio is not None
        and utterance.compression_ratio > MAX_COMPRESSION_RATIO
    ):
        signals.append("repetitive")

    if repeated_phrase_count(utterance.text) > MAX_PHRASE_REPEATS:
        signals.append("looping")

    # Neither of these alone means silence: faster-whisper requires both before
    # it will call a span empty, and so do we.
    if (
        utterance.no_speech_prob is not None
        and utterance.no_speech_prob > MAX_NO_SPEECH_PROB
        and utterance.avg_logprob is not None
        and utterance.avg_logprob < MIN_AVG_LOGPROB
    ):
        signals.append("silence")

    if utterance.avg_logprob is not None and utterance.avg_logprob < MIN_AVG_LOGPROB:
        signals.append("unsure")

    # Speech has a rate. Far more words than the span can hold is a decoder
    # that ran away rather than a fast talker.
    spoken = len(normalize(utterance.text).split())
    if utterance.duration > 0 and spoken / utterance.duration > 8:
        signals.append("too_dense")

    return signals


def is_indexable(signals: list[str]) -> bool:
    """Whether a segment carrying ``signals`` may be searched.

    One signal demotes, two independent ones exclude. Real speech trips a
    single noisy metric often enough that excluding on one would quietly lose
    parts of ordinary episodes — which is a worse failure than the occasional
    invented line, because nobody can see it happen.
    """
    if "empty" in signals:
        return False
    return len(signals) < 2


def build_windows(
    utterances: list[Utterance],
    window_seconds: float = WINDOW_SECONDS,
    stride_seconds: float = STRIDE_SECONDS,
) -> list[Window]:
    """Lay overlapping search windows over the episode.

    Boundaries follow the clock rather than the text, and each window takes
    every utterance that overlaps it at all. An utterance therefore appears in
    two windows, which is the point: a sentence split across a boundary is
    still wholly inside one of them.
    """
    usable = [u for u in utterances if normalize(u.text)]
    if not usable:
        return []

    end_of_episode = max(u.end for u in usable)
    windows: list[Window] = []
    start = min(u.start for u in usable)

    while start < end_of_episode:
        end = start + window_seconds
        inside = [u for u in usable if u.end > start and u.start < end]
        if inside:
            text = " ".join(u.text.strip() for u in inside)
            windows.append(
                Window(
                    start=min(u.start for u in inside),
                    end=max(u.end for u in inside),
                    text=text,
                )
            )
        start += stride_seconds

    return windows


def cluster(moments: list[Moment], gap: float = CLUSTER_GAP_SECONDS) -> list[Moment]:
    """Collapse hits describing the same moment, keeping the best of each.

    Without this, 50%-overlapping windows make the answer look broken while
    the retriever is working perfectly: three results, one moment.
    """
    if not moments:
        return []

    by_time = sorted(moments, key=lambda m: m.start)
    clusters: list[list[Moment]] = [[by_time[0]]]

    for moment in by_time[1:]:
        current = clusters[-1]
        latest_end = max(m.end for m in current)
        if moment.start - latest_end <= gap:
            current.append(moment)
        else:
            clusters.append([moment])

    best = [max(group, key=lambda m: m.score) for group in clusters]
    return sorted(best, key=lambda m: m.score, reverse=True)


def locate_phrase(
    utterances: list[Utterance],
    query: str,
    within: tuple[float, float],
    padding: float = 2.0,
) -> float | None:
    """Where inside ``within`` the query's words actually start.

    Word timings are not editing-grade — the boundary of a consonant moves —
    so the answer is padded backwards. Returns ``None`` when the recogniser
    gave no timings, leaving the caller to fall back on the window start.
    """
    wanted = set(normalize(query).split())
    if not wanted:
        return None

    start, end = within
    for utterance in utterances:
        if utterance.end < start or utterance.start > end:
            continue
        for word in utterance.words:
            if normalize(word.text) in wanted:
                return max(0.0, word.start - padding)

    return None


@dataclass
class TranscriptBuild:
    """The result of turning one recognised episode into stored rows."""

    utterances: list[Utterance] = field(default_factory=list)
    windows: list[Window] = field(default_factory=list)
    #: Parallel to ``utterances``: the signals found against each.
    signals: list[list[str]] = field(default_factory=list)

    @property
    def indexable_utterances(self) -> list[Utterance]:
        return [
            utterance
            for utterance, signals in zip(self.utterances, self.signals, strict=True)
            if is_indexable(signals)
        ]

    @property
    def quarantined(self) -> int:
        return sum(1 for signals in self.signals if not is_indexable(signals))


def build(utterances: list[Utterance]) -> TranscriptBuild:
    """Judge every utterance, then window whatever survived."""
    signals = [quarantine_signals(utterance) for utterance in utterances]
    result = TranscriptBuild(utterances=utterances, signals=signals)
    result.windows = build_windows(result.indexable_utterances)
    return result
