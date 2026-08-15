"""The i18n tables and the rules that keep them honest.

English is the reference: every other language must cover exactly the same
keys with exactly the same placeholders, or a translation drifts silently —
either an English sentence in a Russian screen (missing key) or a KeyError at
render time (missing placeholder value only one language asks for).
"""

from __future__ import annotations

import re

import pytest

from podcast_cutter import i18n
from podcast_cutter.i18n import (
    DEFAULT_LANGUAGE,
    LANGUAGES,
    ordinal,
    plural,
    resolve_language,
    t,
    t_seq,
)

OTHER_LANGUAGES = [lang for lang in LANGUAGES if lang != DEFAULT_LANGUAGE]

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def placeholders(value) -> set[str]:
    if isinstance(value, tuple):
        return {name for part in value for name in _PLACEHOLDER.findall(part)}
    return set(_PLACEHOLDER.findall(value))


class TestTables:
    @pytest.mark.parametrize("lang", OTHER_LANGUAGES)
    def test_every_language_covers_every_key(self, lang):
        english = set(i18n._STRINGS[DEFAULT_LANGUAGE])
        other = set(i18n._STRINGS[lang])
        assert other == english, (
            f"missing: {sorted(english - other)}, extra: {sorted(other - english)}"
        )

    @pytest.mark.parametrize("lang", OTHER_LANGUAGES)
    def test_placeholders_match_the_english_original(self, lang):
        # A translation may reorder placeholders but never add or drop one.
        for key, original in i18n._STRINGS[DEFAULT_LANGUAGE].items():
            translated = i18n._STRINGS[lang][key]
            assert placeholders(translated) == placeholders(original), key

    @pytest.mark.parametrize("lang", OTHER_LANGUAGES)
    def test_tuple_entries_keep_their_shape(self, lang):
        for key, original in i18n._STRINGS[DEFAULT_LANGUAGE].items():
            if isinstance(original, tuple):
                translated = i18n._STRINGS[lang][key]
                assert isinstance(translated, tuple)
                assert len(translated) == len(original), key

    @pytest.mark.parametrize("lang", LANGUAGES)
    def test_html_tags_are_balanced(self, lang):
        # An unclosed <b> makes Telegram reject the whole message, which
        # would surface as a missing reply, not as a visible typo.
        for key, value in i18n._STRINGS[lang].items():
            parts = value if isinstance(value, tuple) else (value,)
            for part in parts:
                for tag in ("b", "i", "code"):
                    assert part.count(f"<{tag}>") == part.count(f"</{tag}>"), (
                        f"{lang}:{key}"
                    )

    @pytest.mark.parametrize("lang", LANGUAGES)
    def test_plural_tables_cover_the_same_nouns(self, lang):
        assert set(i18n._PLURALS[lang]) == set(i18n._PLURALS[DEFAULT_LANGUAGE])


class TestLookup:
    def test_a_key_renders_with_its_parameters(self):
        assert "3:00" in t("en", "video_cap", limit="3:00")

    def test_russian_is_actually_russian(self):
        assert t("ru", "ask_podcast") != t("en", "ask_podcast")

    def test_an_unknown_language_falls_back_to_english(self):
        assert t("de", "ask_podcast") == t("en", "ask_podcast")

    def test_sequences_come_back_whole(self):
        notes = t_seq("ru", "waiting_notes")
        assert isinstance(notes, tuple) and len(notes) == 6

    def test_a_literal_passes_resolve_message_untouched(self):
        assert i18n.resolve_message("ru", "ffmpeg is missing", {}) == (
            "ffmpeg is missing"
        )


class TestResolution:
    def test_a_stored_choice_wins_over_the_client(self):
        assert resolve_language("en", "ru") == "en"

    def test_the_client_language_is_used_when_nothing_is_stored(self):
        assert resolve_language(None, "ru") == "ru"

    def test_regional_variants_match_their_language(self):
        assert resolve_language(None, "ru-RU") == "ru"
        assert resolve_language(None, "en-GB") == "en"

    def test_unknown_languages_fall_back_to_english(self):
        assert resolve_language(None, "de") == "en"
        assert resolve_language(None, None) == "en"
        assert resolve_language("de", None) == "en"


class TestPlurals:
    @pytest.mark.parametrize(
        ("n", "form"),
        [(1, "эпизод"), (2, "эпизода"), (4, "эпизода"), (5, "эпизодов"),
         (11, "эпизодов"), (14, "эпизодов"), (21, "эпизод"), (22, "эпизода"),
         (111, "эпизодов")],
    )
    def test_russian_declines_by_the_full_rule(self, n, form):
        assert plural("ru", "episodes", n) == form

    @pytest.mark.parametrize(("n", "form"), [(1, "episode"), (2, "episodes")])
    def test_english_has_two_forms(self, n, form):
        assert plural("en", "episodes", n) == form

    def test_the_moments_dative_declines(self):
        assert plural("ru", "moments_to", 1) == "моменту"
        assert plural("ru", "moments_to", 3) == "моментам"


class TestOrdinals:
    @pytest.mark.parametrize(
        ("n", "text"),
        [(1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"), (11, "11th"),
         (12, "12th"), (13, "13th"), (21, "21st"), (102, "102nd")],
    )
    def test_english(self, n, text):
        assert ordinal("en", n) == text

    def test_russian(self):
        assert ordinal("ru", 1) == "1-й"
        assert ordinal("ru", 5) == "5-й"
