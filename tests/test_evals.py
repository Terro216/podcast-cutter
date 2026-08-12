"""Tests for the measuring apparatus itself.

A basket exists to be trusted about how good the search is, which makes its own
correctness load-bearing in an unusual way: a scorer that is quietly wrong does
not fail, it reports. So the scoring rules are tested directly, and the runner
is tested end to end against a synthetic episode where every right answer is
known by construction.
"""

from __future__ import annotations

import gzip
import json

import pytest
import yaml

from podcast_cutter.evals import (
    ANSWERS,
    BasketError,
    Query,
    Report,
    by_kind,
    dump_utterances,
    load_basket,
    load_meta,
    load_utterances,
    render,
    run_variant,
    score,
    summarize,
)
from podcast_cutter.transcripts import Utterance, Word


def make_query(kind: str = "quote", at=(100.0,), text: str = "нейросети") -> Query:
    return Query(id="q1", episode="ep", text=text, kind=kind, at=tuple(at))


# ----------------------------------------------------------------------
# Scoring
# ----------------------------------------------------------------------


def test_a_start_inside_the_tolerance_is_a_hit_at_its_rank():
    answer = score(make_query(), [400.0, 108.0])
    assert answer.rank == 2
    assert answer.error == pytest.approx(8.0)


def test_a_start_outside_the_tolerance_is_a_miss_not_a_rounded_hit():
    # Sixteen seconds is a clip that opens on the wrong sentence. Counting it
    # as "nearly right" is exactly how a search feature ships broken.
    answer = score(make_query(), [116.0])
    assert answer.rank is None
    assert answer.error is None


def test_error_is_measured_against_the_nearest_reference():
    answer = score(make_query(kind="mention", at=(100.0, 500.0)), [497.0])
    assert answer.rank == 1
    assert answer.error == pytest.approx(3.0)


def test_a_mention_answered_at_several_real_occurrences_counts_them_distinctly():
    """Three answers on three real occurrences is the point of clustering."""
    query = make_query(kind="mention", at=(100.0, 500.0, 900.0))
    answer = score(query, [101.0, 502.0, 898.0])
    assert answer.distinct == 3


def test_three_answers_crowded_onto_one_occurrence_are_one_distinct_hit():
    """The failure clustering exists to prevent: one moment listed three
    times, which scores as a perfect hit@1 while looking broken to a user."""
    query = make_query(kind="mention", at=(100.0, 500.0, 900.0))
    answer = score(query, [98.0, 101.0, 104.0])
    assert answer.rank == 1
    assert answer.distinct == 1


def test_a_negative_answered_with_nothing_is_not_a_false_hit():
    assert not score(make_query(kind="negative", at=()), []).is_false_hit


def test_a_negative_answered_with_anything_is_a_false_hit():
    answer = score(make_query(kind="negative", at=()), [12.0])
    assert answer.is_false_hit
    assert answer.rank is None


# ----------------------------------------------------------------------
# Rolling up
# ----------------------------------------------------------------------


def test_summarize_separates_the_positive_and_negative_populations():
    answers = [
        score(Query("a", "ep", "x", "quote", (10.0,)), [11.0]),
        score(Query("b", "ep", "x", "quote", (10.0,)), [900.0]),
        score(Query("c", "ep", "x", "negative", ()), []),
        score(Query("d", "ep", "x", "negative", ()), [5.0]),
    ]
    report = summarize(answers, "run")

    assert (report.positives, report.negatives) == (2, 2)
    assert report.hit_at_1 == pytest.approx(0.5)
    assert report.false_hit_rate == pytest.approx(0.5)
    # A miss contributes no error, or one wrong answer forty minutes out would
    # decide the average on its own.
    assert report.errors == (pytest.approx(1.0),)


def test_a_hit_below_the_answer_limit_counts_for_hit_at_k_but_not_hit_at_1():
    answers = [score(Query("a", "ep", "x", "quote", (10.0,)), [900.0, 800.0, 11.0])]
    report = summarize(answers, "run")
    assert report.hit_at_1 == 0.0
    assert report.hit_at_k == 1.0


def test_an_answer_past_the_answer_limit_does_not_count():
    starts = [900.0] * ANSWERS + [10.0]
    report = summarize([score(Query("a", "ep", "x", "quote", (10.0,)), starts)], "run")
    assert report.hit_at_k == 0.0


def test_by_kind_splits_the_classes_because_they_test_different_things():
    answers = [
        score(Query("a", "ep", "x", "quote", (10.0,)), [11.0]),
        score(Query("b", "ep", "x", "meaning", (10.0,)), [900.0]),
        score(Query("c", "ep", "x", "negative", ()), []),
    ]
    reports = by_kind(answers, "ru")
    assert reports["quote"].hit_at_k == 1.0
    assert reports["meaning"].hit_at_k == 0.0
    assert reports["negative"].false_hit_rate == 0.0


def test_metrics_of_an_empty_population_are_absent_rather_than_zero():
    """Nothing measured is not the same as measured and bad, and a report that
    conflates them puts a confident 0% in a table."""
    report = summarize([], "run")
    assert report.hit_at_k is None
    assert report.false_hit_rate is None
    assert "hit@1" not in report.as_dict()


# ----------------------------------------------------------------------
# Regression against a committed baseline
# ----------------------------------------------------------------------


def make_report(**kwargs) -> Report:
    base = {
        "label": "ru/reference",
        "positives": 30,
        "hits_at_1": 24,
        "hits_at_k": 27,
        "negatives": 10,
        "false_hits": 1,
        "errors": (2.0, 3.0, 4.0),
    }
    return Report(**{**base, **kwargs})


def test_a_rate_that_falls_beyond_the_slack_is_a_regression():
    complaints = make_report(hits_at_k=20).regressions({f"hit@{ANSWERS}": 0.9})
    assert len(complaints) == 1
    assert "fell" in complaints[0]


def test_a_rate_that_wobbles_by_one_query_is_not():
    """A thirty-query basket moves 3.3 points when a single query flips, and a
    check that fires on that is a check people learn to ignore. The baseline
    here is one a 30-query run can actually produce — 27/30 — because the slack
    has to clear the real step size, 1/30, not a rounder number."""
    assert make_report(hits_at_k=26).regressions({f"hit@{ANSWERS}": 27 / 30}) == []
    assert make_report(hits_at_k=25).regressions({f"hit@{ANSWERS}": 27 / 30}) != []


def test_a_rate_that_improves_is_never_a_regression():
    assert make_report(hits_at_k=30).regressions({f"hit@{ANSWERS}": 0.9}) == []


def test_a_rising_false_hit_rate_is_a_regression_even_though_it_rose():
    complaints = make_report(false_hits=5).regressions({"false_hit_rate": 0.1})
    assert len(complaints) == 1
    assert "rose" in complaints[0]


def test_a_falling_false_hit_rate_is_not():
    assert make_report(false_hits=0).regressions({"false_hit_rate": 0.1}) == []


def test_a_growing_start_error_is_measured_in_seconds_not_in_points():
    """The bug this guards: one slack figure for rates and for seconds makes
    the error check either hair-trigger or inert. Two seconds of drift is the
    clip opening on the previous sentence."""
    assert make_report(errors=(3.5,)).regressions({"median_error_s": 3.0}) == []
    assert make_report(errors=(6.0,)).regressions({"median_error_s": 3.0}) != []


def test_a_baseline_naming_a_metric_this_run_did_not_produce_complains():
    """The quiet alternative is a disabled check: mistype a key, or change
    RESULTS so the run measures hit@5 against a baseline that still says
    hit@3, and every regression on that metric would pass unnoticed."""
    complaints = summarize([], "run").regressions({f"hit@{ANSWERS}": 0.9})
    assert len(complaints) == 1
    assert "did not measure" in complaints[0]


def test_render_puts_the_runs_in_one_table():
    table = render([make_report(label="ru/reference"), make_report(label="ru/asr")])
    assert "ru/reference" in table and "ru/asr" in table
    assert f"hit@{ANSWERS}" in table


# ----------------------------------------------------------------------
# Reading a basket
# ----------------------------------------------------------------------

MINIMAL_EPISODES = {
    "ep": {
        "title": "An episode",
        "podcast": "A show",
        "audio_url": "https://cdn.example.com/ep.mp3",
        "condition": "studio",
        "transcripts": {"reference": "ep.reference.json", "asr": "ep.asr.json"},
    }
}


def write_basket(tmp_path, queries, episodes=None, baseline=None):
    path = tmp_path / "ru.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "language": "ru",
                "episodes": episodes if episodes is not None else MINIMAL_EPISODES,
                "queries": queries,
                **({"baseline": baseline} if baseline else {}),
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return path


def test_a_basket_reads_clock_timestamps_the_way_the_bot_does(tmp_path):
    path = write_basket(
        tmp_path,
        [
            {
                "id": "q1",
                "episode": "ep",
                "query": "нейросети",
                "at": ["12:34", "1:02:03"],
            }
        ],
    )
    basket = load_basket(path)
    assert basket.queries[0].at == (754.0, 3723.0)


def test_a_single_timestamp_need_not_be_written_as_a_list(tmp_path):
    path = write_basket(
        tmp_path, [{"id": "q1", "episode": "ep", "query": "x", "at": "02:00"}]
    )
    assert load_basket(path).queries[0].at == (120.0,)


def test_a_positive_query_with_no_reference_is_refused(tmp_path):
    """Scored silently it would be a permanent miss, quietly dragging every
    number in the report down for a reason nobody could see."""
    path = write_basket(tmp_path, [{"id": "q1", "episode": "ep", "query": "x"}])
    with pytest.raises(BasketError, match="no `at:` timestamps"):
        load_basket(path)


def test_a_negative_query_naming_a_timestamp_is_refused(tmp_path):
    path = write_basket(
        tmp_path,
        [{"id": "q1", "episode": "ep", "query": "x", "kind": "negative", "at": "1:00"}],
    )
    with pytest.raises(BasketError, match="never said"):
        load_basket(path)


def test_an_unknown_query_class_is_refused(tmp_path):
    path = write_basket(
        tmp_path,
        [{"id": "q1", "episode": "ep", "query": "x", "kind": "vibes", "at": "1:00"}],
    )
    with pytest.raises(BasketError, match="expected one of"):
        load_basket(path)


def test_duplicate_query_ids_are_refused(tmp_path):
    query = {"id": "q1", "episode": "ep", "query": "x", "at": "1:00"}
    with pytest.raises(BasketError, match="Duplicate"):
        load_basket(write_basket(tmp_path, [query, dict(query)]))


def test_a_query_pointing_at_an_unknown_episode_fails_the_whole_basket(tmp_path):
    """Not at the twentieth measurement in it: a typo in a slug is a broken
    basket, and finding out mid-run means the run's numbers are already wrong.
    """
    path = write_basket(
        tmp_path, [{"id": "q1", "episode": "typo", "query": "x", "at": "1:00"}]
    )
    with pytest.raises(BasketError, match="does not describe"):
        load_basket(path)


def test_an_unreadable_timestamp_names_the_query_it_came_from(tmp_path):
    path = write_basket(
        tmp_path, [{"id": "q1", "episode": "ep", "query": "x", "at": "half past"}]
    )
    with pytest.raises(BasketError, match="q1"):
        load_basket(path)


def test_a_baseline_is_carried_per_variant(tmp_path):
    path = write_basket(
        tmp_path,
        [{"id": "q1", "episode": "ep", "query": "x", "at": "1:00"}],
        baseline={"reference": {f"hit@{ANSWERS}": 0.9}, "asr": {f"hit@{ANSWERS}": 0.7}},
    )
    basket = load_basket(path)
    assert basket.baseline["asr"][f"hit@{ANSWERS}"] == 0.7


# ----------------------------------------------------------------------
# Transcript fixtures
# ----------------------------------------------------------------------


def sample_utterances() -> list[Utterance]:
    return [
        Utterance(
            start=30.0,
            end=38.0,
            text="сегодня поговорим про нейросетей и что они умеют",
            words=(
                Word(start=30.5, end=31.0, text="сегодня"),
                Word(start=33.0, end=33.9, text="нейросетей", probability=0.91),
            ),
            avg_logprob=-0.2,
            no_speech_prob=0.01,
            compression_ratio=1.4,
        ),
        Utterance(
            start=100.0,
            end=108.0,
            text="а вот белки сворачиваются очень быстро",
            words=(Word(start=101.0, end=101.6, text="белки"),),
            avg_logprob=-0.3,
        ),
        Utterance(
            start=200.0,
            end=208.0,
            text="и снова про нейросетей в медицине",
            words=(Word(start=203.0, end=203.8, text="нейросетей"),),
            avg_logprob=-0.25,
        ),
    ]


@pytest.mark.parametrize("suffix", [".json", ".json.gz"])
def test_a_transcript_fixture_survives_a_round_trip(tmp_path, suffix):
    path = tmp_path / f"ep.reference{suffix}"
    dump_utterances(sample_utterances(), path, {"model": "large-v3"})

    restored = load_utterances(path)
    assert [u.text for u in restored] == [u.text for u in sample_utterances()]
    assert restored[0].words[1].text == "нейросетей"
    assert restored[0].words[1].probability == pytest.approx(0.91)
    assert restored[0].no_speech_prob == pytest.approx(0.01)


def test_a_fixture_records_what_produced_it(tmp_path):
    """A fixture that does not name its model becomes unattributable the first
    time the model changes, and the comparison it belongs to becomes a guess."""
    path = tmp_path / "ep.asr.json.gz"
    dump_utterances(
        sample_utterances(),
        path,
        {"model": "base", "backend": "local", "source_sha256": "abc123"},
    )

    meta = load_meta(path)
    assert meta["model"] == "base"
    assert meta["source_sha256"] == "abc123"
    assert "chunker_version" in meta

    # And the file really is gzipped JSON of the documented shape, not just
    # whatever the loader happens to accept back from itself.
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        assert json.load(handle)["meta"]["model"] == "base"


def test_reading_provenance_does_not_require_reading_the_transcript(tmp_path):
    """`load_meta` answers "which model, which audio" on its own, which is what
    the fixture builder needs before deciding whether to transcribe at all."""
    path = tmp_path / "ep.reference.json"
    dump_utterances([], path, {"model": "large-v3"})
    assert load_meta(path) == {"model": "large-v3", "chunker_version": 1}


# ----------------------------------------------------------------------
# The whole runner, over a synthetic episode
# ----------------------------------------------------------------------


def garbled_utterances() -> list[Utterance]:
    """The same episode as `base` was actually heard to render it.

    «нейросеиц» is not invented for this test: it is what the shipped model
    wrote on a real episode, recorded in `ROADMAP.md` §15. A basket exists to
    turn that anecdote into a rate, and this fixture is the smallest honest
    version of it.
    """
    spoken = sample_utterances()
    return [
        Utterance(
            start=item.start,
            end=item.end,
            text=item.text.replace("нейросетей", "нейросеиц"),
            words=tuple(
                Word(
                    start=word.start,
                    end=word.end,
                    text=word.text.replace("нейросетей", "нейросеиц"),
                    probability=word.probability,
                )
                for word in item.words
            ),
            avg_logprob=item.avg_logprob,
            no_speech_prob=item.no_speech_prob,
            compression_ratio=item.compression_ratio,
        )
        for item in spoken
    ]


@pytest.fixture
def worked_basket(tmp_path):
    """A tiny episode whose every right answer is known by construction."""
    fixtures = tmp_path / "fixtures"
    dump_utterances(
        sample_utterances(), fixtures / "ep.reference.json.gz", {"model": "large-v3"}
    )
    dump_utterances(
        garbled_utterances(), fixtures / "ep.asr.json.gz", {"model": "base"}
    )

    episodes = {
        "ep": {
            "title": "An episode",
            "podcast": "A show",
            "audio_url": "https://cdn.example.com/ep.mp3",
            "condition": "studio",
            "transcripts": {
                "reference": "ep.reference.json.gz",
                "asr": "ep.asr.json.gz",
            },
        }
    }
    queries = [
        # Said as «нейросетей», asked in the nominative: the lemma index is
        # what makes this findable at all, and it is the defect that shipped.
        {"id": "ru-01", "episode": "ep", "query": "нейросети", "kind": "mention",
         "at": ["00:31", "03:21"]},
        {"id": "ru-02", "episode": "ep", "query": "белки", "kind": "quote",
         "at": ["01:39"]},
        {"id": "ru-03", "episode": "ep", "query": "квантовая телепортация",
         "kind": "negative"},
    ]
    path = write_basket(tmp_path, queries, episodes=episodes)
    return load_basket(path), fixtures


async def test_the_runner_finds_the_moments_a_person_would(worked_basket, tmp_path):
    basket, fixtures = worked_basket
    answers = await run_variant(basket, fixtures, "reference", tmp_path / "e.db")

    found = {answer.query.id: answer for answer in answers}
    assert found["ru-01"].rank == 1
    assert found["ru-02"].rank == 1
    assert not found["ru-03"].starts

    report = summarize(answers, "ru/reference")
    assert report.hit_at_k == 1.0
    assert report.false_hit_rate == 0.0


async def test_the_runner_places_the_clip_on_the_spoken_word(worked_basket, tmp_path):
    """Two seconds before the word, not the top of the thirty-second window —
    the difference is a clip that opens twenty seconds early, which shipped."""
    basket, fixtures = worked_basket
    answers = await run_variant(basket, fixtures, "reference", tmp_path / "e.db")

    protein = next(a for a in answers if a.query.id == "ru-02")
    assert protein.starts[0] == pytest.approx(99.0)


async def test_the_runner_answers_a_mention_at_each_place_it_was_said(
    worked_basket, tmp_path
):
    basket, fixtures = worked_basket
    answers = await run_variant(basket, fixtures, "reference", tmp_path / "e.db")

    mention = next(a for a in answers if a.query.id == "ru-01")
    assert mention.distinct == 2


async def test_the_two_variants_are_indexed_apart_and_the_gap_is_the_measurement(
    worked_basket, tmp_path
):
    """The whole point of running one basket twice.

    Same episode, same questions, two transcripts. The reference finds the
    moment; the shipped model garbled the word into something no lemma
    connects back, so it does not. If the two collided into one stored
    transcript this would silently be one run reported twice — which is a
    comparison that always shows a gap of zero.
    """
    basket, fixtures = worked_basket
    db = tmp_path / "e.db"
    reference = summarize(await run_variant(basket, fixtures, "reference", db), "ref")
    asr = summarize(await run_variant(basket, fixtures, "asr", db), "asr")

    assert reference.hit_at_k == 1.0
    assert asr.hit_at_k < reference.hit_at_k
    # And the failure is the recogniser's, not the retriever's: the negative
    # still comes back empty, so nothing started guessing to fill the gap.
    assert asr.false_hit_rate == 0.0


async def test_a_variant_missing_from_an_episode_is_refused(worked_basket, tmp_path):
    basket, fixtures = worked_basket
    with pytest.raises(BasketError, match="no 'small' transcript"):
        await run_variant(basket, fixtures, "small", tmp_path / "e.db")
