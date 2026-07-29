import pytest

from podcast_cutter.audio import (
    Interval,
    _cut_command,
    container_for_codec,
    parse_interval,
    parse_timestamp,
)
from podcast_cutter.errors import IntervalError

MAX = 15 * 60


class TestParseTimestamp:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("90", 90),
            ("0", 0),
            ("01:20", 80),
            ("1:20", 80),
            ("00:00", 0),
            ("1:05:00", 3900),
            ("01:05:30", 3930),
            ("1h", 3600),
            ("30m", 1800),
            ("45s", 45),
            ("1h30m", 5400),
            ("1h2m3s", 3723),
            ("  01:20  ", 80),
            ("1H30M", 5400),
        ],
    )
    def test_accepts(self, raw, expected):
        assert parse_timestamp(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "abc",
            "5x",
            "1:2:3:4",
            "aa:bb",
            "1:70",  # seconds above 59 is a typo, not 2:10
            "1:60:00",
            "-30",
            "1.5",
        ],
    )
    def test_rejects(self, raw):
        with pytest.raises(IntervalError):
            parse_timestamp(raw)

    def test_rejects_bare_junk_that_the_old_regex_silently_accepted(self):
        # The previous implementation used re.match with an all-optional
        # pattern, so it matched the empty string at position 0 and could
        # return 0 seconds for inputs it should have rejected.
        with pytest.raises(IntervalError):
            parse_timestamp("banana")


class TestParseInterval:
    @pytest.mark.parametrize(
        ("raw", "start", "end"),
        [
            ("01:20-02:00", 80, 120),
            ("01:20 - 02:00", 80, 120),
            ("1:20–2:00", 80, 120),  # en dash
            ("1:20—2:00", 80, 120),  # em dash
            ("1:20..2:00", 80, 120),
            ("1:20 to 2:00", 80, 120),
            ("90-150", 90, 150),
            ("1h2m-1h5m", 3720, 3900),
            ("0-10", 0, 10),
        ],
    )
    def test_accepts(self, raw, start, end):
        interval = parse_interval(raw, MAX)
        assert (interval.start, interval.end) == (start, end)
        assert interval.duration == end - start

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "01:20",  # no separator
            "-02:00",  # no start
            "01:20-",  # no end
            "02:00-01:20",  # backwards
            "01:20-01:20",  # empty range
        ],
    )
    def test_rejects(self, raw):
        with pytest.raises(IntervalError):
            parse_interval(raw, MAX)

    def test_enforces_max_duration(self):
        parse_interval("0-900", MAX)
        with pytest.raises(IntervalError, match="maximum"):
            parse_interval("0-901", MAX)

    def test_none_is_rejected_not_crashed(self):
        with pytest.raises(IntervalError):
            parse_interval(None, MAX)


class TestInterval:
    def test_duration(self):
        assert Interval(start=10, end=70).duration == 60


class TestContainerForCodec:
    @pytest.mark.parametrize(
        ("codec", "ext"),
        [
            ("mp3", "mp3"),
            ("MP3", "mp3"),
            ("aac", "m4a"),
            ("opus", "opus"),
            ("vorbis", "ogg"),
        ],
    )
    def test_known_codecs_copy_into_a_matching_container(self, codec, ext):
        assert container_for_codec(codec) == ext

    @pytest.mark.parametrize("codec", ["flac", "alac", "pcm_s16le"])
    def test_lossless_codecs_are_re_encoded_rather_than_copied(self, codec):
        # Deliberate: lossless cuts overshoot Telegram's size limit within
        # minutes, and a copied FLAC keeps the source's total-sample count in
        # its header, so players report the wrong length.
        assert container_for_codec(codec) is None

    @pytest.mark.parametrize("codec", [None, "", "wmav2", "unknown_codec"])
    def test_unknown_codecs_have_no_copy_container(self, codec):
        # A None here is what tells cut_episode to go straight to re-encoding
        # instead of attempting an impossible stream copy.
        assert container_for_codec(codec) is None

    def test_aac_does_not_copy_into_mp3(self):
        # The original bug: every AAC episode was written to .mp3 with
        # `-c copy`, which ffmpeg always rejects.
        assert container_for_codec("aac") != "mp3"


class TestCutCommand:
    def _cmd(self, **kwargs):
        from pathlib import Path

        return _cut_command(
            "https://example.com/a.mp3",
            Path("/tmp/out.mp3"),
            Interval(start=10, end=40),
            **{"encode": None, **kwargs},
        )

    def test_drops_the_source_metadata(self):
        # Feeds ship multi-megabyte ID3 tags; copying one into a 30-second cut
        # made the clip hundreds of times larger than its audio.
        cmd = self._cmd()
        assert "-map_metadata" in cmd
        assert cmd[cmd.index("-map_metadata") + 1] == "-1"

    def test_seeks_before_the_input(self):
        # -ss after -i would decode everything up to the start point.
        cmd = self._cmd()
        assert cmd.index("-ss") < cmd.index("-i")

    def test_takes_one_audio_stream_only(self):
        cmd = self._cmd()
        assert cmd[cmd.index("-map") + 1] == "0:a:0"
        assert "-vn" in cmd

    def test_passes_the_interval(self):
        cmd = self._cmd()
        assert cmd[cmd.index("-ss") + 1] == "10"
        assert cmd[cmd.index("-t") + 1] == "30"

    def test_copies_when_no_encoder_is_named(self):
        assert "copy" in self._cmd(encode=None)

    def test_encodes_mp3_when_asked(self):
        assert "libmp3lame" in self._cmd(encode="mp3")

    def test_voice_notes_use_opus_in_mono(self):
        # sendVoice only accepts Opus in Ogg; anything else is rejected.
        cmd = self._cmd(encode="opus")
        assert "libopus" in cmd
        assert cmd[cmd.index("-ac") + 1] == "1"

    def test_writes_supplied_tags(self):
        cmd = self._cmd(metadata={"title": "Ep 1", "artist": "Show"})
        assert "title=Ep 1" in cmd
        assert "artist=Show" in cmd

    def test_skips_empty_tags(self):
        cmd = self._cmd(metadata={"title": "", "artist": "Show"})
        assert "title=" not in cmd

    def test_http_options_are_omitted_for_local_files(self):
        from pathlib import Path

        cmd = _cut_command(
            Path("/tmp/source.mp3"),
            Path("/tmp/out.mp3"),
            Interval(start=0, end=5),
            encode=None,
        )
        # ffmpeg aborts with "Option user_agent not found" on a local input.
        assert "-user_agent" not in cmd

    def test_http_options_are_present_for_urls(self):
        assert "-user_agent" in self._cmd()
