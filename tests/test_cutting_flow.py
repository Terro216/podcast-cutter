"""The cut job as the user experiences it: progress, delivery, cleanup.

The cutting itself is covered by ``test_cut_integration.py`` against real
ffmpeg. Here ``cut_episode`` is stubbed so the surrounding behaviour — what is
sent, what the user is told, and what is left behind — can be checked in
isolation.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from conftest import FakeInlineUpdate, FakeUpdate, make_episode
from podcast_cutter import handlers as handlers_mod
from podcast_cutter import keyboards as kb
from podcast_cutter import video as video_mod
from podcast_cutter.audio import CutResult
from podcast_cutter.errors import AudioError, TooLargeError
from podcast_cutter.proxy import DIRECT, PROXY
from podcast_cutter.states import (
    FORMAT_NOTE,
    FORMAT_VIDEO,
    Awaiting,
    Screen,
    get_session,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def ready(bot, context):
    """A session sitting on the clip editor, ready to cut."""
    session = get_session(context.user_data)
    session.select_episode(make_episode("10", duration=3600), 60)
    session.set_clip(600, 60)
    session.awaiting = Awaiting.INTERVAL
    session.go(Screen.INTERVAL)
    return session


def stub_cut(
    monkeypatch, *, suffix=".mp3", size=1234, raises=None, record=None, route=DIRECT
):
    async def fake_cut(url, interval, workdir, settings, **kwargs):
        if record is not None:
            record.update(kwargs)
            record["workdir"] = workdir
            record["interval"] = interval
        if raises is not None:
            raise raises
        workdir.mkdir(parents=True, exist_ok=True)
        path = workdir / f"cut{suffix}"
        path.write_bytes(b"x" * size)
        return CutResult(path=path, size=size, transcoded=False, route=route)

    monkeypatch.setattr(handlers_mod, "cut_episode", fake_cut)


def stub_render(monkeypatch, *, size=2048, record=None):
    async def fake_render(audio, workdir, **kwargs):
        if record is not None:
            record.update(kwargs)
            record["audio"] = audio
        path = workdir / "note.mp4"
        path.write_bytes(b"v" * size)
        return path

    monkeypatch.setattr(video_mod, "render_clip", fake_render)


class TestDelivery:
    async def test_sends_an_audio_file_by_default(
        self, bot, context, ready, monkeypatch
    ):
        stub_cut(monkeypatch)
        update = FakeUpdate(callback=kb.ACTION_CUT)

        await bot.on_callback(update, context)

        sent = update.effective_message.sent_audio
        assert sent is not None
        assert sent["duration"] == 60
        assert sent["title"] == "Episode 10"
        assert sent["performer"] == "Some Show"
        assert sent["filename"].endswith(".mp3")

    async def test_sends_a_voice_note_when_asked(
        self, bot, context, ready, monkeypatch
    ):
        ready.as_voice = True
        stub_cut(monkeypatch, suffix=".ogg")
        update = FakeUpdate(callback=kb.ACTION_CUT)

        await bot.on_callback(update, context)

        assert update.effective_message.sent_voice is not None
        assert update.effective_message.sent_audio is None

    async def test_asks_ffmpeg_for_opus_when_sending_a_voice_note(
        self, bot, context, ready, monkeypatch
    ):
        ready.as_voice = True
        record: dict = {}
        stub_cut(monkeypatch, suffix=".ogg", record=record)

        await bot.on_callback(FakeUpdate(callback=kb.ACTION_CUT), context)

        assert record["voice"] is True

    async def test_sends_a_video_note_when_asked(
        self, bot, context, ready, monkeypatch
    ):
        ready.send_as = FORMAT_NOTE
        stub_cut(monkeypatch)
        stub_render(monkeypatch)
        update = FakeUpdate(callback=kb.ACTION_CUT)

        await bot.on_callback(update, context)

        sent = update.effective_message.sent_video_note
        assert sent is not None
        assert sent["duration"] == 60
        assert sent["length"] == video_mod.NOTE_SIZE
        assert update.effective_message.sent_audio is None

    async def test_the_circle_is_rendered_round_and_the_video_square(
        self, bot, context, ready, monkeypatch
    ):
        # The two formats share the renderer but not the layout: a note is
        # cropped to a circle by every client, a video file is not.
        for send_as, round_frame in ((FORMAT_NOTE, True), (FORMAT_VIDEO, False)):
            ready.send_as = send_as
            record: dict = {}
            stub_cut(monkeypatch)
            stub_render(monkeypatch, record=record)

            await bot.on_callback(FakeUpdate(callback=kb.ACTION_CUT), context)

            assert record["round_frame"] is round_frame

    async def test_sends_a_square_video_file_when_asked(
        self, bot, context, ready, monkeypatch
    ):
        ready.send_as = FORMAT_VIDEO
        ready.set_clip(600, 90)
        stub_cut(monkeypatch)
        stub_render(monkeypatch)
        update = FakeUpdate(callback=kb.ACTION_CUT)

        await bot.on_callback(update, context)

        sent = update.effective_message.sent_video
        assert sent is not None
        assert sent["width"] == sent["height"] == video_mod.NOTE_SIZE
        assert "Episode 10" in sent["caption"]
        assert sent["filename"].endswith(".mp4")
        assert update.effective_message.sent_video_note is None

    async def test_a_circle_over_a_minute_is_refused_not_silently_squared(
        self, bot, context, ready, monkeypatch
    ):
        # Which format arrives is the user's choice; the refusal names the
        # way out instead of switching formats behind their back.
        ready.send_as = FORMAT_NOTE
        ready.set_clip(600, 90)
        record: dict = {}
        stub_cut(monkeypatch, record=record)
        update = FakeUpdate(callback=kb.ACTION_CUT)

        await bot.on_callback(update, context)

        assert "one minute" in update.shown
        assert "Video" in update.shown
        assert record == {}

    async def test_a_video_past_the_cap_is_refused_before_any_work(
        self, bot, context, ready, monkeypatch
    ):
        ready.send_as = FORMAT_VIDEO
        ready.set_clip(600, video_mod.MAX_VIDEO_SECONDS + 1)
        record: dict = {}
        stub_cut(monkeypatch, record=record)
        update = FakeUpdate(callback=kb.ACTION_CUT)

        await bot.on_callback(update, context)

        assert "capped" in update.shown
        assert record == {}

    async def test_the_journal_records_the_note_and_its_skin(
        self, bot, context, ready, monkeypatch, store
    ):
        ready.send_as = FORMAT_NOTE
        ready.skin = "scope"
        stub_cut(monkeypatch)
        stub_render(monkeypatch)

        await bot.on_callback(FakeUpdate(callback=kb.ACTION_CUT), context)

        row = store._execute(
            "SELECT outcome, detail, as_voice FROM events WHERE action = 'cut'"
        )[0]
        assert (row["outcome"], row["detail"]) == ("ok", "note:scope")
        assert not row["as_voice"]

    async def test_no_transcript_means_no_subtitles_not_a_transcription(
        self, bot, context, ready, monkeypatch
    ):
        ready.send_as = FORMAT_NOTE
        record: dict = {}
        stub_cut(monkeypatch)
        stub_render(monkeypatch, record=record)

        await bot.on_callback(FakeUpdate(callback=kb.ACTION_CUT), context)

        assert record["subtitles"] is None

    async def test_a_warmed_transcript_becomes_subtitles(
        self, bot, context, ready, monkeypatch, store
    ):
        from podcast_cutter.store import TranscriptKey
        from podcast_cutter.transcripts import Utterance, build

        await store.save_transcript(
            TranscriptKey(
                episode_id="10",
                source_sha256="abc",
                asr_backend="fake",
                asr_model="test",
                chunker_version=2,
            ),
            {"source_url": "u"},
            build([Utterance(start=610, end=615, text="про фолдинг белков")]),
        )
        ready.send_as = FORMAT_NOTE
        record: dict = {}
        stub_cut(monkeypatch)
        stub_render(monkeypatch, record=record)

        await bot.on_callback(FakeUpdate(callback=kb.ACTION_CUT), context)

        lines = record["subtitles"]
        assert lines and lines[0].text == "про фолдинг белков"
        assert lines[0].start == pytest.approx(10.0)

    async def test_a_render_failure_is_a_failed_cut_not_a_crash(
        self, bot, context, ready, monkeypatch, store
    ):
        ready.send_as = FORMAT_NOTE
        stub_cut(monkeypatch)

        async def broken_render(audio, workdir, **kwargs):
            raise AudioError("Could not render the video for this clip.")

        monkeypatch.setattr(video_mod, "render_clip", broken_render)
        update = FakeUpdate(callback=kb.ACTION_CUT)

        await bot.on_callback(update, context)

        assert "render" in update.shown
        row = store._execute(
            "SELECT outcome FROM events WHERE action = 'cut'"
        )[0]
        assert row["outcome"] == "audio_failed"

    async def test_tags_the_clip_with_its_own_metadata(
        self, bot, context, ready, monkeypatch
    ):
        record: dict = {}
        stub_cut(monkeypatch, record=record)

        await bot.on_callback(FakeUpdate(callback=kb.ACTION_CUT), context)

        tags = record["metadata"]
        assert tags["artist"] == "Some Show"
        assert "10:00" in tags["title"]

    async def test_shows_a_typing_indicator_while_uploading(
        self, bot, context, ready, monkeypatch
    ):
        stub_cut(monkeypatch)
        update = FakeUpdate(callback=kb.ACTION_CUT)

        await bot.on_callback(update, context)

        assert update.effective_chat.actions

    async def test_lands_on_the_result_screen(
        self, bot, context, ready, monkeypatch
    ):
        stub_cut(monkeypatch)
        update = FakeUpdate(callback=kb.ACTION_CUT)

        await bot.on_callback(update, context)

        assert ready.current.screen is Screen.RESULT
        assert kb.ACTION_NEW_CLIP in [
            b.callback_data
            for row in update.markup.inline_keyboard
            for b in row
        ]


class TestNudging:
    async def test_the_journal_says_when_the_detour_earned_a_cut(
        self, bot, context, ready, monkeypatch, store
    ):
        """Otherwise there is no way to tell what the proxy is worth."""
        stub_cut(monkeypatch, route=PROXY)

        await bot.on_callback(FakeUpdate(callback=kb.ACTION_CUT), context)

        row = store._execute(
            "SELECT outcome, detail FROM events WHERE action = 'cut'"
        )[0]
        assert (row["outcome"], row["detail"]) == ("ok", "route=proxy")

    async def test_a_direct_cut_journals_no_route(
        self, bot, context, ready, monkeypatch, store
    ):
        stub_cut(monkeypatch)

        await bot.on_callback(FakeUpdate(callback=kb.ACTION_CUT), context)

        row = store._execute(
            "SELECT outcome, detail FROM events WHERE action = 'cut'"
        )[0]
        assert (row["outcome"], row["detail"]) == ("ok", None)

    async def test_shifting_moves_the_clip_and_recuts(
        self, bot, context, ready, monkeypatch
    ):
        record: dict = {}
        stub_cut(monkeypatch, record=record)

        await bot.on_callback(FakeUpdate(callback=f"{kb.SHIFT_PREFIX}:-15"), context)

        assert ready.clip_start == 585
        assert record["interval"].start == 585

    async def test_another_clip_returns_to_the_editor(
        self, bot, context, ready, monkeypatch
    ):
        stub_cut(monkeypatch)
        await bot.on_callback(FakeUpdate(callback=kb.ACTION_CUT), context)

        await bot.on_callback(FakeUpdate(callback=kb.ACTION_NEW_CLIP), context)

        assert ready.current.screen is Screen.INTERVAL
        assert ready.awaiting is Awaiting.INTERVAL


class TestFailures:
    async def test_a_cut_failure_explains_itself_and_offers_a_retry(
        self, bot, context, ready, monkeypatch
    ):
        stub_cut(monkeypatch, raises=AudioError("The host refused the download."))
        update = FakeUpdate(callback=kb.ACTION_CUT)

        await bot.on_callback(update, context)

        assert "refused" in update.shown
        assert kb.ACTION_RETRY in [
            b.callback_data
            for row in update.markup.inline_keyboard
            for b in row
        ]

    async def test_an_oversized_clip_suggests_a_shorter_one(
        self, bot, context, ready, monkeypatch
    ):
        stub_cut(monkeypatch, raises=TooLargeError("The cut is 60 MB."))
        update = FakeUpdate(callback=kb.ACTION_CUT)

        await bot.on_callback(update, context)

        assert "60 MB" in update.shown

    async def test_an_unexpected_crash_does_not_escape(
        self, bot, context, ready, monkeypatch
    ):
        stub_cut(monkeypatch, raises=RuntimeError("kaboom"))
        update = FakeUpdate(callback=kb.ACTION_CUT)

        await bot.on_callback(update, context)

        assert "kaboom" not in update.shown
        assert "⚠️" in update.shown

    async def test_retry_after_a_failure_works(
        self, bot, context, ready, monkeypatch
    ):
        stub_cut(monkeypatch, raises=AudioError("temporary"))
        await bot.on_callback(FakeUpdate(callback=kb.ACTION_CUT), context)

        stub_cut(monkeypatch)
        update = FakeUpdate(callback=kb.ACTION_RETRY)
        await bot.on_callback(update, context)

        assert update.effective_message.sent_audio is not None


class TestCleanup:
    async def test_the_job_directory_is_removed(
        self, bot, context, ready, monkeypatch
    ):
        record: dict = {}
        stub_cut(monkeypatch, record=record)

        await bot.on_callback(FakeUpdate(callback=kb.ACTION_CUT), context)

        assert not Path(record["workdir"]).exists()

    async def test_the_directory_is_removed_after_a_failure_too(
        self, bot, context, ready, monkeypatch
    ):
        record: dict = {}

        async def failing(url, interval, workdir, settings, **kwargs):
            record["workdir"] = workdir
            workdir.mkdir(parents=True, exist_ok=True)
            (workdir / "half-written.mp3").write_bytes(b"x")
            raise AudioError("nope")

        monkeypatch.setattr(handlers_mod, "cut_episode", failing)
        await bot.on_callback(FakeUpdate(callback=kb.ACTION_CUT), context)

        assert not Path(record["workdir"]).exists()


class TestConcurrency:
    async def test_one_cut_per_user_at_a_time(
        self, bot, context, ready, monkeypatch
    ):
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow(url, interval, workdir, settings, **kwargs):
            started.set()
            await release.wait()
            workdir.mkdir(parents=True, exist_ok=True)
            path = workdir / "cut.mp3"
            path.write_bytes(b"x")
            return CutResult(path=path, size=1, transcoded=False)

        monkeypatch.setattr(handlers_mod, "cut_episode", slow)

        first = asyncio.create_task(
            bot.on_callback(FakeUpdate(callback=kb.ACTION_CUT), context)
        )
        await started.wait()

        second = FakeUpdate(callback=kb.ACTION_CUT)
        await bot.on_callback(second, context)
        assert "one at a time" in second.shown

        release.set()
        await first


class TestProgressThrottling:
    async def test_identical_text_is_not_re_sent(self, bot, context):
        # Telegram errors on an edit that changes nothing.
        from conftest import FakeMessage

        editor = handlers_mod.StatusEditor(FakeMessage(), min_interval=0)
        await editor.set("same")
        await editor.set("same")

        assert len(editor.message.edits) == 1

    async def test_updates_are_rate_limited(self, bot, context):
        from conftest import FakeMessage

        editor = handlers_mod.StatusEditor(FakeMessage(), min_interval=100)
        await editor.set("first")
        await editor.set("second")

        assert [text for text, _ in editor.message.edits] == ["first"]

    async def test_important_updates_bypass_the_limit(self, bot, context):
        from conftest import FakeMessage

        editor = handlers_mod.StatusEditor(FakeMessage(), min_interval=100)
        await editor.set("first")
        await editor.set("second", force=True)

        assert len(editor.message.edits) == 2


class TestInlineMode:
    async def test_returns_episodes_for_a_query(self, bot, context):
        update = FakeInlineUpdate("lex fridman")
        await bot.on_inline_query(update, context)

        results = update.inline_query.results
        assert results
        assert results[0].title

    async def test_each_result_carries_a_deep_link_back(self, bot, context):
        update = FakeInlineUpdate("lex fridman")
        await bot.on_inline_query(update, context)

        content = update.inline_query.results[0].input_message_content
        assert "?start=ep_" in content.message_text

    async def test_a_short_query_just_offers_the_bot(self, bot, context, client):
        update = FakeInlineUpdate("a")
        await bot.on_inline_query(update, context)

        assert update.inline_query.results == []
        assert update.inline_query.options["button"] is not None
        assert not client.calls

    async def test_an_api_failure_answers_empty_rather_than_erroring(
        self, bot, context, client
    ):
        from podcast_cutter.errors import ApiError

        client.fail_with = ApiError("down")
        update = FakeInlineUpdate("something")

        await bot.on_inline_query(update, context)

        assert update.inline_query.answers
        assert update.inline_query.results == []

    async def test_falls_back_to_a_podcast_search_when_no_person_matches(
        self, bot, context, client
    ):
        from podcast_cutter.errors import NotFoundError

        client.person_fail = NotFoundError("nobody")
        update = FakeInlineUpdate("Radiolab")

        await bot.on_inline_query(update, context)

        assert update.inline_query.results
        assert any(call.startswith("search_feeds:") for call in client.calls)
        assert any(call.startswith("list_episodes:") for call in client.calls)

    async def test_a_query_nothing_matches_answers_empty(
        self, bot, context, client
    ):
        from podcast_cutter.errors import NotFoundError

        client.fail_with = NotFoundError("nothing at all")
        update = FakeInlineUpdate("zzzzz")

        await bot.on_inline_query(update, context)

        assert update.inline_query.results == []
        assert update.inline_query.options["button"] is not None

    async def test_an_empty_answer_is_not_cached_for_long(
        self, bot, context, client
    ):
        from podcast_cutter.errors import NotFoundError

        client.fail_with = NotFoundError("nothing at all")
        update = FakeInlineUpdate("Radio")

        await bot.on_inline_query(update, context)

        # A five-minute cache on "nothing" would outlive the word being typed.
        assert update.inline_query.options["cache_time"] <= 10

    async def test_results_are_capped(self, bot, context, client):
        # Telegram accepts at most 50 inline results.
        client.episodes = [make_episode(str(i)) for i in range(200)]
        update = FakeInlineUpdate("many")

        await bot.on_inline_query(update, context)

        assert 0 < len(update.inline_query.results) <= 50


class TestAttribution:
    """A clip is a citation, and a citation names its source (ROADMAP §13.4)."""

    async def test_the_caption_links_the_full_episode(
        self, bot, context, ready, monkeypatch
    ):
        stub_cut(monkeypatch)
        update = FakeUpdate(callback=kb.ACTION_CUT)
        await bot.on_callback(update, context)

        caption = update.effective_message.sent_audio["caption"]
        assert ready.episode.enclosure_url in caption
        assert "Full episode" in caption

    async def test_the_result_screen_offers_the_full_episode(
        self, bot, context, ready, monkeypatch
    ):
        # The video note has no caption at all, so the result screen's URL
        # button is the attribution every format shares.
        stub_cut(monkeypatch)
        update = FakeUpdate(callback=kb.ACTION_CUT)
        await bot.on_callback(update, context)

        buttons = [
            b for row in update.markup.inline_keyboard for b in row if b.url
        ]
        assert any(b.url == ready.episode.enclosure_url for b in buttons)


class TestCancel:
    async def test_cancel_stops_the_cut_in_flight(
        self, bot, context, ready, monkeypatch
    ):
        """/cancel during a cut cancels the job rather than letting an
        unwanted clip finish and land in the chat anyway."""
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def hanging_cut(url, interval, workdir, settings, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        monkeypatch.setattr(handlers_mod, "cut_episode", hanging_cut)

        cutting = FakeUpdate(callback=kb.ACTION_CUT)
        job = asyncio.create_task(bot.on_callback(cutting, context))
        await asyncio.wait_for(started.wait(), timeout=2)

        await bot.cmd_cancel(FakeUpdate(text="/cancel"), context)
        await asyncio.wait_for(job, timeout=2)

        assert cancelled.is_set()
        assert not bot._busy_users and not bot._cut_tasks
        assert "Cancelled" in cutting.shown

    async def test_a_cancelled_cut_frees_the_user_for_the_next_one(
        self, bot, context, ready, monkeypatch
    ):
        started = asyncio.Event()

        async def hanging_cut(url, interval, workdir, settings, **kwargs):
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(handlers_mod, "cut_episode", hanging_cut)
        job = asyncio.create_task(
            bot.on_callback(FakeUpdate(callback=kb.ACTION_CUT), context)
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        await bot.cmd_cancel(FakeUpdate(text="/cancel"), context)
        await asyncio.wait_for(job, timeout=2)

        # The next cut must not be refused as "still working".
        stub_cut(monkeypatch)
        session = get_session(context.user_data)
        session.select_episode(make_episode("10", duration=3600), 60)
        session.set_clip(600, 60)
        session.go(Screen.INTERVAL)
        update = FakeUpdate(callback=kb.ACTION_CUT)
        await bot.on_callback(update, context)
        assert update.effective_message.sent_audio is not None

    async def test_the_journal_says_cancelled_not_crashed(
        self, bot, context, ready, monkeypatch, store
    ):
        started = asyncio.Event()

        async def hanging_cut(url, interval, workdir, settings, **kwargs):
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(handlers_mod, "cut_episode", hanging_cut)
        job = asyncio.create_task(
            bot.on_callback(FakeUpdate(callback=kb.ACTION_CUT), context)
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        await bot.cmd_cancel(FakeUpdate(text="/cancel"), context)
        await asyncio.wait_for(job, timeout=2)

        failures = dict((await store.stats(24)).failures)
        assert failures.get("cancelled") == 1
