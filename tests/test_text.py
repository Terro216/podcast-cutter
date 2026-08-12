import pytest

from podcast_cutter.text import (
    button_label,
    format_duration,
    one_line,
    progress_bar,
    safe_filename,
    truncate,
)


class TestProgressBar:
    def test_counts_what_it_is_told_to(self):
        """The bug this pins: transcription fed seconds of audio into a bar
        that only knew bytes, and forty minutes rendered as «2.4 KB»."""
        bar = progress_bar(1500, 3600, label=format_duration)
        assert "25:00" in bar and "1:00:00" in bar
        assert "KB" not in bar

    def test_bytes_stay_the_default(self):
        assert "KB" in progress_bar(1500, 3600)


class TestOneLine:
    def test_collapses_newlines_and_tabs(self):
        assert one_line("Episode\n  42\tpart\r\ntwo") == "Episode 42 part two"

    def test_replaces_control_characters_with_a_space(self):
        # Substituting rather than deleting keeps words apart.
        assert one_line("bad\x00title\x07") == "bad title"

    @pytest.mark.parametrize("value", [None, "", "   ", "\n\t"])
    def test_falls_back_when_empty(self, value):
        assert one_line(value, "Untitled") == "Untitled"

    def test_keeps_unicode(self):
        assert one_line("Подкаст — про всё") == "Подкаст — про всё"


class TestTruncate:
    def test_leaves_short_strings_alone(self):
        assert truncate("hello", 10) == "hello"

    def test_marks_the_cut(self):
        assert truncate("hello world", 8) == "hello w…"
        assert len(truncate("hello world", 8)) == 8

    def test_degenerate_limits(self):
        assert truncate("hello", 0) == ""
        assert truncate("hello", 1) == "…"


class TestButtonLabel:
    def test_joins_parts(self):
        assert button_label("Podcast", "Episode") == "Podcast · Episode"

    def test_skips_empty_parts(self):
        assert button_label("Podcast", "", None or "") == "Podcast"

    def test_respects_the_limit(self):
        # Telegram silently misrenders very long button labels.
        label = button_label("x" * 200, "y" * 200, limit=60)
        assert len(label) == 60

    def test_never_returns_empty(self):
        assert button_label("", "  ") == "Untitled"


class TestSafeFilename:
    def test_builds_a_readable_name(self):
        assert (
            safe_filename("My Podcast", "Episode 12", "01.20-02.00", ext="mp3")
            == "My_Podcast-Episode_12-01.20-02.00.mp3"
        )

    def test_strips_path_separators(self):
        # A feed title like "AC/DC" must not turn into a directory traversal.
        name = safe_filename("AC/DC", "S1/E1", ext="mp3")
        assert "/" not in name and "\\" not in name

    def test_strips_reserved_characters(self):
        name = safe_filename('a:b*c?d"e<f>g|h', ext="mp3")
        assert name == "abcdefgh.mp3"

    def test_flattens_newlines(self):
        assert safe_filename("two\nlines", ext="mp3") == "two_lines.mp3"

    def test_bounds_the_length(self):
        name = safe_filename("x" * 500, "y" * 500, ext="mp3")
        assert len(name) <= 110

    def test_strips_control_characters(self):
        assert safe_filename("a\x00b", ext="mp3") == "a_b.mp3"

    def test_falls_back_when_everything_is_stripped(self):
        assert safe_filename("///", "\x00", ext="mp3") == "podcast_cut.mp3"

    def test_accepts_a_dotted_extension(self):
        assert safe_filename("show", ext=".m4a").endswith(".m4a")


class TestFormatDuration:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0, "0:00"),
            (5, "0:05"),
            (65, "1:05"),
            (900, "15:00"),
            (3600, "1:00:00"),
            (3930, "1:05:30"),
            (-5, "0:00"),
        ],
    )
    def test_formats(self, seconds, expected):
        assert format_duration(seconds) == expected
