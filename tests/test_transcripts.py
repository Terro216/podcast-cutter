"""Judging, windowing and clustering — everything that happens to recognised
speech before it becomes an answer.

None of this needs a model, which is the point of keeping it separate: the
behaviour that decides whether a search is any good is testable in
milliseconds.
"""

from __future__ import annotations

import pytest

from podcast_cutter.transcripts import (
    MAX_PHRASE_REPEATS,
    Moment,
    Utterance,
    Word,
    build,
    build_windows,
    cluster,
    excerpt,
    is_indexable,
    lemma,
    lemmatize,
    locate_phrase,
    normalize,
    quarantine_signals,
    repeated_phrase_count,
)


def utterance(start, end, text, **metrics) -> Utterance:
    return Utterance(start=start, end=end, text=text, **metrics)


class TestNormalize:
    def test_folds_case(self):
        assert normalize("Привет МИР") == "привет мир"

    def test_folds_yo_to_ye(self):
        """Feeds and speakers disagree; a search that distinguishes them fails
        invisibly."""
        assert normalize("ещё") == normalize("еще")

    def test_drops_punctuation(self):
        assert normalize("а, вот — это: «да»!") == "а вот это да"

    def test_keeps_latin_and_digits(self):
        assert normalize("useLayoutEffect в React 19") == "uselayouteffect в react 19"

    def test_empty_stays_empty(self):
        assert normalize("  —  ") == ""


class TestLemmatize:
    """The measured gap: a literal-token index cannot find its own subject.

    On a real episode about neural networks the recogniser wrote «нейросетей»
    four times and «нейросети» not once, so searching the episode's own topic
    came back empty.
    """

    def test_folds_a_russian_inflection_to_the_form_people_type(self):
        pytest.importorskip("pymorphy3")
        assert lemmatize("нейросетей") == lemmatize("нейросети")

    def test_the_case_that_failed_in_production(self):
        pytest.importorskip("pymorphy3")
        heard = "потенциал нейросетей то как они находят"
        assert lemma("нейросети") in lemmatize(heard).split()

    def test_plurals_fold_to_the_singular(self):
        pytest.importorskip("pymorphy3")
        assert lemmatize("лекарства") == lemmatize("лекарство")

    def test_latin_words_are_left_alone(self):
        assert lemma("uselayouteffect") == "uselayouteffect"

    def test_an_unknown_word_is_guessed_at_but_guessed_consistently(self):
        """pymorphy3 predicts a lemma for words it does not know — «нейросеиц»
        becomes «нейросеица» — so a garbled word is *not* passed through
        untouched. It still finds itself, because the same guess is applied to
        the text and to the query. The surface index behind it is what covers
        the case where the two guesses differ.
        """
        pytest.importorskip("pymorphy3")
        garbled = "нейросеиц"
        assert lemma(garbled) != garbled, "the dictionary guesses rather than defers"
        assert lemmatize(f"управляя через {garbled}").split()[-1] == lemma(garbled)

    def test_works_without_the_dictionary_installed(self, monkeypatch):
        """Losing pymorphy3 costs inflection matching, not search."""
        import podcast_cutter.transcripts as module

        lemma.cache_clear()
        monkeypatch.setattr(module, "_morph", lambda: None)
        try:
            assert lemmatize("нейросетей") == "нейросетей"
        finally:
            lemma.cache_clear()

    def test_normalisation_happens_first(self):
        assert lemmatize("Ещё, БЕЛКИ!") == lemmatize("еще белки")


class TestRepetition:
    def test_ordinary_speech_is_not_a_loop(self):
        text = "мы обсуждали фолдинг белков и как это меняет разработку лекарств"
        assert repeated_phrase_count(text) == 1

    def test_a_decoding_loop_is_caught(self):
        text = " ".join(["продолжение следует нас ждёт"] * 8)
        assert repeated_phrase_count(text) > MAX_PHRASE_REPEATS

    def test_short_text_cannot_loop(self):
        assert repeated_phrase_count("да") == 1


class TestQuarantine:
    def test_clean_speech_has_no_signals(self):
        assert quarantine_signals(
            utterance(
                0,
                5,
                "они скажут что их всё это мало заботит",
                avg_logprob=-0.3,
                no_speech_prob=0.01,
                compression_ratio=1.4,
            )
        ) == []

    def test_empty_text_is_never_indexable(self):
        signals = quarantine_signals(utterance(0, 5, "   "))
        assert "empty" in signals
        assert not is_indexable(signals)

    def test_silence_needs_both_metrics(self):
        """faster-whisper requires both before calling a span empty, and a high
        no-speech probability on confident text is common enough that acting on
        it alone would drop real speech."""
        signals = quarantine_signals(
            utterance(0, 5, "нормальная речь", no_speech_prob=0.9, avg_logprob=-0.2)
        )
        assert "silence" not in signals

    def test_silence_is_flagged_when_both_agree(self):
        signals = quarantine_signals(
            utterance(0, 5, "субтитры сделал DimaTorzok",
                      no_speech_prob=0.9, avg_logprob=-1.8)
        )
        assert "silence" in signals

    def test_a_loop_over_silence_is_excluded(self):
        """Two independent signals, which is the bar for dropping a segment."""
        signals = quarantine_signals(
            utterance(
                0,
                30,
                " ".join(["продолжение следует"] * 12),
                no_speech_prob=0.95,
                avg_logprob=-1.5,
                compression_ratio=6.0,
            )
        )
        assert len(signals) >= 2
        assert not is_indexable(signals)

    def test_one_signal_only_demotes(self):
        signals = quarantine_signals(
            utterance(0, 5, "неразборчиво но это речь", avg_logprob=-1.4)
        )
        assert signals == ["unsure"]
        assert is_indexable(signals)

    def test_more_words_than_the_span_can_hold(self):
        signals = quarantine_signals(utterance(0, 1, " ".join(["слово"] * 20)))
        assert "too_dense" in signals

    def test_a_single_token_decoder_loop_is_excluded_on_ratio_alone(self):
        """The real case from obrecheny-startup at 54:30: 448 characters of
        «генегенеген…» inside one token, compression_ratio 24.08, and the model
        confidently sure of itself (−0.06). Every other signal misses for a
        structural reason — the loop has fewer than four words, high
        confidence, one word per span — so an extreme ratio must convict on
        its own or the two-signal rule protects exactly this."""
        signals = quarantine_signals(
            utterance(
                0,
                12,
                "но не " + "гене" * 112,
                avg_logprob=-0.06,
                no_speech_prob=0.5,
                compression_ratio=24.08,
            )
        )
        assert "runaway" in signals
        assert not is_indexable(signals)

    def test_a_merely_repetitive_span_still_only_demotes(self):
        """The conclusive threshold must not swallow the ordinary case: real
        speech does trip the ratio alone occasionally, and one signal demoting
        rather than excluding is the whole point of the two-signal rule."""
        signals = quarantine_signals(
            utterance(0, 8, "и так далее и так далее ну вот", compression_ratio=3.0)
        )
        assert signals == ["repetitive"]
        assert is_indexable(signals)

    def test_missing_metrics_are_not_held_against_a_backend(self):
        """A cloud recogniser reporting none of these must not be quarantined
        for the silence of its metrics."""
        assert quarantine_signals(utterance(0, 5, "обычная фраза здесь")) == []


class TestWindows:
    def _utterances(self, count=12, each=5.0):
        return [
            utterance(index * each, (index + 1) * each, f"фраза номер {index}")
            for index in range(count)
        ]

    def test_covers_the_whole_episode(self):
        windows = build_windows(self._utterances())
        assert windows
        assert windows[0].start == pytest.approx(0.0)
        assert windows[-1].end == pytest.approx(60.0)

    def test_windows_overlap(self):
        windows = build_windows(self._utterances())
        assert windows[1].start < windows[0].end, "a stride shorter than the window"

    def test_a_phrase_split_across_a_boundary_lands_whole_in_some_window(self):
        speech = [
            utterance(0, 14, "начало разговора"),
            utterance(14, 16, "фолдинг"),
            utterance(16, 30, "белков меняет всё"),
        ]
        windows = build_windows(speech)
        assert any(
            "фолдинг" in window.text and "белков" in window.text
            for window in windows
        )

    def test_empty_input_yields_nothing(self):
        assert build_windows([]) == []

    def test_silence_only_input_yields_nothing(self):
        assert build_windows([utterance(0, 5, "   ")]) == []

    def test_quarantined_utterances_never_reach_a_window(self):
        speech = [
            utterance(0, 5, "настоящая речь про белки"),
            utterance(
                5,
                30,
                " ".join(["продолжение следует"] * 12),
                no_speech_prob=0.95,
                avg_logprob=-1.6,
                compression_ratio=7.0,
            ),
        ]
        result = build(speech)
        assert result.quarantined == 1
        assert all("продолжение" not in window.text for window in result.windows)


class TestExcerpt:
    """What the answer quotes back.

    Previously it showed the opening of a thirty-second window, which starts
    wherever the clock said — so three results read as three unrelated
    fragments, none of which contained the phrase asked for.
    """

    def test_centres_on_the_match(self):
        text = " ".join(["слово"] * 40 + ["фолдинг"] + ["другое"] * 40)
        before, match, after = excerpt(text, "фолдинг")
        assert match == "фолдинг"
        assert "слово" in before and "другое" in after

    def test_marks_that_it_was_cut(self):
        text = " ".join(["слово"] * 40 + ["фолдинг"] + ["другое"] * 40)
        before, _, after = excerpt(text, "фолдинг")
        assert before.startswith("…") and after.rstrip().endswith("…")

    def test_no_ellipsis_when_nothing_was_cut(self):
        before, match, after = excerpt("вот и фолдинг тут", "фолдинг")
        assert "…" not in before and "…" not in after
        assert match == "фолдинг"

    def test_finds_an_inflected_form(self):
        pytest.importorskip("pymorphy3")
        _, match, _ = excerpt("и я поступил в Вышку в тот год", "Вышка")
        assert match == "Вышку"

    def test_never_splits_a_word(self):
        text = " ".join(["длинноесловоцелое"] * 30 + ["фолдинг"])
        before, match, _ = excerpt(text, "фолдинг", width=40)
        assert "длинноесловоцел " not in before
        assert match == "фолдинг"

    def test_falls_back_to_the_opening_when_nothing_matches(self):
        """The window matched on something; if the exact word cannot be found
        the fragment is still better than nothing."""
        before, match, after = excerpt("совершенно другой текст", "фолдинг")
        assert before and not match and not after

    def test_empty_text_is_survivable(self):
        assert excerpt("", "фолдинг") == ("", "", "")


class TestCluster:
    def test_overlapping_hits_become_one_moment(self):
        """The failure this exists to prevent: three results, one moment."""
        moments = [
            Moment(start=100, end=130, text="a", score=5.0),
            Moment(start=115, end=145, text="b", score=7.0),
            Moment(start=130, end=160, text="c", score=6.0),
        ]
        assert len(cluster(moments)) == 1

    def test_the_best_of_a_cluster_survives(self):
        moments = [
            Moment(start=100, end=130, text="a", score=5.0),
            Moment(start=115, end=145, text="best", score=9.0),
        ]
        assert cluster(moments)[0].text == "best"

    def test_distant_hits_stay_separate(self):
        moments = [
            Moment(start=100, end=130, text="a", score=5.0),
            Moment(start=900, end=930, text="b", score=6.0),
        ]
        assert len(cluster(moments)) == 2

    def test_results_come_back_best_first(self):
        moments = [
            Moment(start=100, end=130, text="weak", score=1.0),
            Moment(start=900, end=930, text="strong", score=9.0),
        ]
        assert [m.text for m in cluster(moments)] == ["strong", "weak"]

    def test_nothing_in_nothing_out(self):
        assert cluster([]) == []


class TestLocatePhrase:
    def test_finds_the_word_and_pads_backwards(self):
        speech = [
            Utterance(
                start=100,
                end=110,
                text="говорим про фолдинг белков",
                words=(
                    Word(start=100.0, end=100.5, text="говорим"),
                    Word(start=100.5, end=101.0, text="про"),
                    Word(start=101.0, end=101.8, text="фолдинг"),
                ),
            )
        ]
        found = locate_phrase(speech, "фолдинг", within=(95, 130))
        # Padded back so the first syllable is not clipped.
        assert found == pytest.approx(99.0)

    def test_never_returns_a_negative_start(self):
        speech = [
            Utterance(
                start=0,
                end=3,
                text="фолдинг",
                words=(Word(start=0.5, end=1.0, text="фолдинг"),),
            )
        ]
        assert locate_phrase(speech, "фолдинг", within=(0, 30)) == 0.0

    def test_returns_none_without_word_timings(self):
        speech = [Utterance(start=100, end=110, text="говорим про фолдинг")]
        assert locate_phrase(speech, "фолдинг", within=(95, 130)) is None

    def test_finds_a_word_spoken_in_another_form(self):
        """Found in production: a search for «Вышка» landed on a window where
        «Вышку» was said, then placed the clip at the top of the window twenty
        seconds early, because the window was found on lemmas and the word was
        matched on its surface form."""
        pytest.importorskip("pymorphy3")
        speech = [
            Utterance(
                start=885,
                end=905,
                text="и я поступил в Вышку в тот год",
                words=(
                    Word(start=890.0, end=890.4, text="и"),
                    Word(start=905.0, end=905.6, text="Вышку"),
                ),
            )
        ]
        found = locate_phrase(speech, "Вышка", within=(885, 915))
        assert found == pytest.approx(903.0)

    def test_ignores_matches_outside_the_window(self):
        speech = [
            Utterance(
                start=10,
                end=12,
                text="фолдинг",
                words=(Word(start=10.0, end=10.5, text="фолдинг"),),
            )
        ]
        assert locate_phrase(speech, "фолдинг", within=(500, 530)) is None
