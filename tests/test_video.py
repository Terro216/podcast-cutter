"""The video-note renderer: the pure half, and one real render.

The graph builder and the subtitle machinery are pure functions, so most of
this runs without ffmpeg. The one test that encodes for real is the same
deal as ``test_cut_integration.py``: it skips where ffmpeg is absent and is
the only proof the graph text actually parses.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess

import pytest

from podcast_cutter import keyboards as kb
from podcast_cutter import video
from podcast_cutter.errors import AudioError
from podcast_cutter.transcripts import Utterance


def make_utterance(start, end, text, **metrics):
    return Utterance(start=start, end=end, text=text, **metrics)


class TestSubtitleLines:
    def test_keeps_only_what_overlaps_the_clip(self):
        utterances = [
            make_utterance(0, 5, "before"),
            make_utterance(9, 14, "inside"),
            make_utterance(40, 45, "after"),
        ]
        lines = video.subtitle_lines(utterances, 10.0, 30.0)
        assert [line.text for line in lines] == ["inside"]

    def test_times_are_relative_to_the_clip_and_clamped(self):
        lines = video.subtitle_lines(
            [make_utterance(9, 14, "spans the start")], 10.0, 30.0
        )
        assert lines[0].start == 0.0
        assert lines[0].end == pytest.approx(4.0)

    def test_quarantined_speech_is_not_burned_in(self):
        # A decoder loop kept out of the search index has no business in a
        # video either — and a video cannot be corrected after the fact.
        loop = make_utterance(
            12, 13, "генегене " * 60, compression_ratio=24.0
        )
        lines = video.subtitle_lines(
            [loop, make_utterance(15, 18, "real speech")], 10.0, 30.0
        )
        assert [line.text for line in lines] == ["real speech"]

    def test_whitespace_is_collapsed(self):
        lines = video.subtitle_lines(
            [make_utterance(11, 14, "  two\n words ")], 10.0, 30.0
        )
        assert lines[0].text == "two words"

    def test_a_sliver_of_a_line_is_dropped(self):
        lines = video.subtitle_lines(
            [make_utterance(9.0, 10.1, "barely there")], 10.0, 30.0
        )
        assert lines == []


class TestAssDocument:
    def test_times_are_ass_shaped(self):
        assert video._ass_time(0) == "0:00:00.00"
        assert video._ass_time(61.5) == "0:01:01.50"
        assert video._ass_time(3600.25) == "1:00:00.25"

    def test_braces_cannot_open_override_tags(self):
        # ``{\b1}`` in recognised text would style itself instead of showing.
        document = video.ass_document(
            [video.SubtitleLine(0, 2, "a {weird} span")]
        )
        assert "{weird}" not in document
        assert "(weird)" in document

    def test_the_canvas_size_is_declared(self):
        document = video.ass_document([video.SubtitleLine(0, 2, "x")], size=384)
        assert "PlayResX: 384" in document
        assert "PlayResY: 384" in document

    def test_the_round_layout_pulls_the_text_inside_the_circle(self):
        # A note is cropped to the inscribed circle; margins that fit the
        # square frame leave the first and last words invisible.
        lines = [video.SubtitleLine(0, 2, "x")]
        square = video.ass_document(lines)
        round_ = video.ass_document(lines, round_frame=True)
        assert square != round_

        def margins(document):
            style = next(
                line for line in document.splitlines()
                if line.startswith("Style:")
            )
            return [int(field) for field in style.split(",")[-3:]]

        square_l, _, square_v = margins(square)
        round_l, _, round_v = margins(round_)
        assert round_l > square_l
        assert round_v > square_v


class TestBuildGraph:
    def build(self, skin, tmp_path, **kwargs):
        options = {
            "duration": 60.0,
            "title_file": tmp_path / "t.txt",
            "span_file": tmp_path / "s.txt",
            "subs_file": None,
            "with_media": False,
        }
        options.update(kwargs)
        return video.build_graph(skin, **options)

    def test_every_skin_builds_and_ends_in_out(self, tmp_path):
        for skin in video.SKINS:
            for round_frame in (False, True):
                for with_media in (False, True):
                    graph = self.build(
                        skin, tmp_path,
                        round_frame=round_frame, with_media=with_media,
                    )
                    assert graph.endswith("[out]")

    def test_a_retired_skin_still_renders_as_its_heir(self, tmp_path):
        # Buttons on scrolled-past messages outlive keyboards: a callback
        # carrying a pre-redesign skin must map to a living one, not raise.
        for legacy, heir in video.LEGACY_SKINS.items():
            assert heir in video.SKINS
            graph = self.build(legacy, tmp_path)
            assert graph.endswith("[out]")

    def test_the_round_progress_bar_is_a_short_centred_track(self, tmp_path):
        # At the frame's bottom edge the circle leaves ~140 px of visible
        # chord, so the full-width square bar would show only its middle.
        square = self.build("aurora", tmp_path)
        round_ = self.build("aurora", tmp_path, round_frame=True)
        assert f"s={video.NOTE_SIZE}x6" in square
        assert f"s={video.NOTE_SIZE}x6" not in round_

    def test_an_unknown_skin_is_refused(self, tmp_path):
        with pytest.raises(ValueError):
            self.build("winamp2000", tmp_path)

    def test_subtitles_join_the_chain_only_when_given(self, tmp_path):
        without = self.build("aurora", tmp_path)
        with_subs = self.build(
            "aurora", tmp_path, subs_file=tmp_path / "subs.ass"
        )
        assert "subtitles=" not in without
        assert "subtitles=" in with_subs

    def test_cover_art_is_used_when_present_and_not_faked_when_absent(
        self, tmp_path
    ):
        with_art = self.build("cover", tmp_path, with_media=True)
        without = self.build("cover", tmp_path, with_media=False)
        assert "[1:v]" in with_art
        assert "[1:v]" not in without

    def test_the_running_clock_and_length_flank_every_bar(self, tmp_path):
        # The clip's own clock, ticking in minutes and seconds, plus the
        # static total — both are inline text this module wrote itself.
        graph = self.build("aurora", tmp_path, duration=95.0)
        assert "eif" in graph
        assert "1\\:35" in graph

    def test_a_second_title_line_joins_only_when_given(self, tmp_path):
        one = self.build("aurora", tmp_path)
        two = self.build(
            "aurora", tmp_path, title2_file=tmp_path / "t2.txt"
        )
        assert one.count("drawtext") + 1 == two.count("drawtext")


class TestWrapTitle:
    def test_a_short_title_stays_on_one_line(self):
        assert video.wrap_title("Short", (24, 28)) == ["Short"]

    def test_a_long_title_wraps_at_word_boundaries(self):
        lines = video.wrap_title(
            "Запуск Завтра — Как мы чинили прод в три часа ночи", (24, 28)
        )
        assert len(lines) == 2
        assert lines[0] == "Запуск Завтра — Как мы"
        assert lines[1].startswith("чинили прод")

    def test_the_budgets_are_respected(self):
        for first, second in ((24, 28), (40, 40)):
            lines = video.wrap_title("word " * 30, (first, second))
            assert len(lines[0]) <= first
            assert len(lines[1]) <= second

    def test_overflow_past_two_lines_is_ellipsised(self):
        lines = video.wrap_title("word " * 30, (24, 28))
        assert lines[1].endswith("…")

    def test_a_single_giant_word_is_cut_not_wrapped(self):
        lines = video.wrap_title("щ" * 60, (24, 28))
        assert len(lines) == 1
        assert len(lines[0]) <= 24

    def test_skin_keys_match_the_keyboard_labels(self):
        # The render behaviour lives in this module and the button labels in
        # keyboards, which must not import the ffmpeg half of the world; this
        # is the seam that keeps the two sets from drifting apart.
        assert set(video.SKINS) == set(kb.SKIN_LABELS)


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)
class TestRealRender:
    @pytest.mark.parametrize(
        ("skin", "round_frame"),
        [
            ("aurora", True),
            ("party", False),
            ("lava", True),
            ("matrix", False),
            ("fractal", True),
            # vinyl and dvd without artwork: the dark-card and bouncing-note
            # fallbacks; the artwork paths ride the cover test below.
            ("vinyl", True),
            ("dvd", False),
            # brainrot with an empty loops directory: the honest card.
            ("brainrot", True),
            # A retired skin arriving from an old button.
            ("vhs", True),
        ],
    )
    def test_renders_a_playable_square_video_with_subtitles(
        self, tmp_path, settings, skin, round_frame
    ):
        audio = tmp_path / "clip.m4a"
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                "-c:a", "aac", str(audio),
            ],
            check=True,
        )

        output = asyncio.run(
            video.render_clip(
                audio,
                tmp_path,
                skin=skin,
                duration=3.0,
                title="Show — Episode",
                span="00:10–00:13",
                subtitles=[video.SubtitleLine(0.5, 2.5, "hello there")],
                cover=None,
                settings=settings,
                round_frame=round_frame,
            )
        )

        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=codec_name,width,height",
                "-of", "csv=p=0", str(output),
            ],
            capture_output=True, text=True, check=True,
        )
        codec, width, height = probe.stdout.strip().split(",")
        assert codec == "h264"
        assert width == height == str(video.NOTE_SIZE)

    def test_a_corrupt_cover_loses_the_picture_not_the_clip(
        self, tmp_path, settings
    ):
        audio = tmp_path / "clip.m4a"
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                "-c:a", "aac", str(audio),
            ],
            check=True,
        )
        bad_cover = tmp_path / "cover.img"
        bad_cover.write_bytes(b"this is not an image")

        output = asyncio.run(
            video.render_clip(
                audio,
                tmp_path,
                skin="cover",
                duration=2.0,
                title="Show",
                span="00:00–00:02",
                subtitles=None,
                cover=bad_cover,
                settings=settings,
            )
        )
        assert output.exists() and output.stat().st_size > 0

    def test_unreadable_audio_raises_the_audio_error(
        self, tmp_path, settings
    ):
        broken = tmp_path / "broken.m4a"
        broken.write_bytes(b"nothing decodable")

        with pytest.raises(AudioError):
            asyncio.run(
                video.render_clip(
                    broken,
                    tmp_path,
                    skin="bars",
                    duration=2.0,
                    title="Show",
                    span="00:00–00:02",
                    subtitles=None,
                    cover=None,
                    settings=settings,
                )
            )
