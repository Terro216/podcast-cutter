"""The evaluation baskets, run as tests.

This is the `[EVALS]` half of the workflow `ROADMAP.md` §2 commits to, and it
is a test rather than a script for one reason: a measurement nobody is made to
look at stops being taken. Every push runs both baskets over both transcripts
and fails if a number went backwards.

What this can and cannot catch is worth being precise about. Full transcript
runs use the operator's gitignored `private/eval-fixtures/`, so ordinary public
CI checks the baskets' shape and skips rows whose private inputs are absent.
With local fixtures present this catches every regression on the path from
recognised words to an answer: the lemma index, windowing, clustering and
placement. All three defects found in production so far lived there.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path

import pytest

from podcast_cutter.evals import (
    ANSWERS,
    SLACK,
    by_kind,
    load_basket,
    render,
    run_variant,
    summarize,
)

EVALS = Path(__file__).resolve().parent.parent / "evals"
FIXTURES = Path(
    os.environ.get(
        "EVAL_FIXTURES_DIR",
        Path(__file__).resolve().parent.parent / "private" / "eval-fixtures",
    )
)
BASKETS = sorted(EVALS.glob("baskets/*.yaml"))
#: reference and asr are the pair the two-run design stands on; small and
#: speechkit are the candidate recognisers, guarded so the comparison table
#: stays a fact rather than a one-off measurement.
VARIANTS = ("reference", "asr", "small", "speechkit")
#: The retrieval axis: plain lexical, and lexical+dense when a converted
#: embedding model is available. CI without the model still guards the
#: lexical baselines; the hybrid rows run wherever EMBED_MODEL_DIR points at
#: real weights — see HANDOFF §6 for the command.
RETRIEVALS = ("lexical", "hybrid")


@functools.cache
def _embedder():
    path = os.environ.get("EMBED_MODEL_DIR", "")
    if not path or not (Path(path) / "model.bin").exists():
        return None
    from podcast_cutter.embeddings import Embedder

    return Embedder(Path(path))


def _embedder_or_skip(retrieval: str):
    if retrieval == "lexical":
        return None
    embedder = _embedder()
    if embedder is None:
        pytest.skip("EMBED_MODEL_DIR does not point at a converted model")
    return embedder


def basket_ids() -> list[str]:
    return [path.stem for path in BASKETS]


@pytest.fixture(scope="module", params=BASKETS, ids=basket_ids())
def basket(request):
    loaded = load_basket(request.param)
    if not loaded.queries:
        pytest.skip(
            f"{request.param.name} has no queries yet — the fixtures have to "
            f"exist before the answer key can be written from them."
        )
    return loaded


def _fixtures_present(basket, variant: str) -> bool:
    # An episode naming no fixture counts as absent, not present. Joining the
    # empty string onto the directory yields the directory, which exists — and
    # the run would then fail deep inside the loader instead of skipping here.
    names = [episode.transcripts.get(variant) for episode in basket.episodes.values()]
    return all(name and (FIXTURES / name).exists() for name in names)


def test_every_basket_file_is_readable():
    """Runs even when a basket is still empty, because a YAML typo should fail
    here rather than eight hours into a fixture run."""
    assert BASKETS, "no baskets found under evals/baskets/"
    for path in BASKETS:
        loaded = load_basket(path)
        assert loaded.language, f"{path.name} does not say what language it is"


def test_the_basket_is_big_enough_to_mean_something(basket):
    """A twelve-query basket reporting 100% is worse than no basket.

    `ROADMAP.md` §13.5 scaled the research report's 200 queries down to 30
    positive and 10 negative per language, which is the point where one flipped
    query is three points rather than ten. Below that the number is noise
    wearing a percent sign, and it will still get quoted. So the floor is a
    failing test and not a note — a basket half-written is a basket that has
    not been written.
    """
    positive = [query for query in basket.queries if not query.is_negative]
    negative = [query for query in basket.queries if query.is_negative]
    kinds = {query.kind for query in positive}

    assert len(positive) >= 30, (
        f"{basket.language}: {len(positive)} positive queries, need 30"
    )
    assert len(negative) >= 10, (
        f"{basket.language}: {len(negative)} negative queries, need 10"
    )
    # All three positive classes, because they test different components and a
    # basket missing one is silent about whichever it left out.
    missing = {"quote", "meaning", "mention"} - kinds
    assert not missing, f"{basket.language}: missing query classes {missing}"


@pytest.mark.parametrize("retrieval", RETRIEVALS)
@pytest.mark.parametrize("variant", VARIANTS)
async def test_the_basket_has_not_regressed(
    basket, variant, retrieval, tmp_path, capsys
):
    if not _fixtures_present(basket, variant):
        pytest.skip(f"{variant} fixtures are not available locally")
    embedder = _embedder_or_skip(retrieval)

    key = variant if retrieval == "lexical" else f"{variant}+e5"
    answers = await run_variant(
        basket, FIXTURES, variant, tmp_path / "basket.db",
        embedder=embedder,
    )
    label = f"{basket.language}/{key}"
    report = summarize(answers, label)

    # Printed unconditionally: the number is the point of the exercise, and a
    # test that only speaks up when it fails hides the trend that matters.
    with capsys.disabled():
        print("\n" + render([report, *by_kind(answers, label).values()]))
        for answer in answers:
            if answer.query.is_negative and not answer.is_false_hit:
                continue
            if answer.rank == 1:
                continue
            print(
                f"  {answer.query.id:<10} {answer.query.kind:<8} "
                f"rank={answer.rank} «{answer.query.text}»"
                + (f" — {answer.query.note}" if answer.query.note else "")
            )

    complaints = report.regressions(basket.baseline.get(key, {}))
    assert not complaints, "\n".join(complaints)


async def test_the_reference_is_at_least_as_good_as_the_shipped_model(
    basket, tmp_path
):
    """The gap that justifies the whole two-run design, asserted in the one
    direction it can only go.

    A reference transcript that scored *meaningfully worse* than `base` would
    not be a surprising result, it would be a broken fixture — the reference
    mislabelled, or the two variants collapsed into one stored transcript.
    Either makes every other number in the report meaningless, so it is worth
    one assertion.

    The tolerance is there because the strict inequality is not guaranteed:
    a garbled word occasionally matches a query the correct one does not, and
    a basket this size moves three points on one query. Slack absorbs that
    without absorbing a reference that is wholesale wrong.
    """
    if not all(_fixtures_present(basket, variant) for variant in VARIANTS):
        pytest.skip("both variants have to be committed to compare them")

    scores = {}
    for variant in VARIANTS:
        answers = await run_variant(
            basket, FIXTURES, variant, tmp_path / f"{variant}.db"
        )
        scores[variant] = summarize(answers, variant).hit_at_k

    for variant in VARIANTS:
        assert scores["reference"] >= scores[variant] - SLACK[f"hit@{ANSWERS}"], (
            f"the reference transcript scores {scores['reference']:.3f} "
            f"against {variant}'s {scores[variant]:.3f} — suspect the "
            f"fixtures, not the retriever"
        )
