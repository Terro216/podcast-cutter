"""Searching inside an episode, driven through the routers.

The engine is tested elsewhere; what matters here is that a person can reach
it, is told the truth while they wait, and lands somewhere useful afterwards —
including when the answer is "nothing".
"""

from __future__ import annotations

import time
from dataclasses import replace

import pytest

from conftest import FakeUpdate, make_episode
from podcast_cutter import keyboards as kb
from podcast_cutter import screens
from podcast_cutter.handlers import PodcastCutterBot
from podcast_cutter.states import Awaiting, Screen, Session, get_session

pytestmark = pytest.mark.asyncio


async def text(bot, context, message: str) -> FakeUpdate:
    update = FakeUpdate(text=message)
    await bot.on_text(update, context)
    return update


async def tap(bot, context, data: str) -> FakeUpdate:
    update = FakeUpdate(callback=data)
    await bot.on_callback(update, context)
    return update


def session_of(context):
    return get_session(context.user_data)


def payloads(markup) -> list[str]:
    if markup is None:
        return []
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def outcome(update: FakeUpdate):
    """The message a search finally leaves the user looking at.

    A search replies with a progress message and then edits *that* in place, so
    the answer is not on the message the user typed into.
    """
    return update.effective_message.children[-1]


async def at_the_editor(bot, context, duration=3600):
    """Put the session in the clip editor, where searching starts."""
    session = session_of(context)
    episode = make_episode("10", duration=duration)
    session.select_episode(episode)
    session.go(Screen.INTERVAL)
    return session, episode


class TestReachingTheSearch:
    async def test_the_clip_editor_offers_it(self, settings):
        session = Session()
        session.episode = make_episode()
        session.go(Screen.INTERVAL)
        view = screens.interval(session, settings)

        assert kb.ACTION_FIND in payloads(view.keyboard)

    async def test_it_is_hidden_when_transcription_is_off(self, settings):
        session = Session()
        session.episode = make_episode()
        session.go(Screen.INTERVAL)
        view = screens.interval(session, replace(settings, asr_enabled=False))

        assert kb.ACTION_FIND not in payloads(view.keyboard)


class TestAskingForAPhrase:
    async def test_warns_that_the_first_search_is_slow(self, bot, context):
        """A user not told this assumes the bot has hung."""
        session, _ = await at_the_editor(bot, context)

        update = await tap(bot, context, kb.ACTION_FIND)

        assert session.awaiting is Awaiting.PHRASE
        assert session.current.screen is Screen.ASK_PHRASE
        assert "few minutes" in update.shown

    async def test_promises_speed_once_the_episode_is_known(
        self, bot, context, indexer, tmp_path
    ):
        _, episode = await at_the_editor(bot, context)
        await indexer.transcript_id(
            episode.id, episode.enclosure_url, tmp_path / "job"
        )

        update = await tap(bot, context, kb.ACTION_FIND)

        assert "instant" in update.shown


class TestSearching:
    async def _ready(self, bot, context):
        session, _ = await at_the_editor(bot, context)
        await tap(bot, context, kb.ACTION_FIND)
        return session

    async def test_a_phrase_finds_a_moment(self, bot, context):
        session = await self._ready(bot, context)
        await text(bot, context, "фолдинг")

        assert session.current.screen is Screen.MOMENTS
        assert session.moments
        assert session.awaiting is Awaiting.NOTHING

    async def test_typing_here_is_a_phrase_not_a_podcast_search(
        self, bot, context, client
    ):
        """The failure this router shape exists to prevent: text meaning the
        wrong thing because a screen forgot what it was waiting for."""
        await self._ready(bot, context)
        client.calls.clear()

        await text(bot, context, "фолдинг")

        assert not any(call.startswith("search_feeds") for call in client.calls)

    async def test_an_absent_phrase_says_so_plainly(self, bot, context):
        session = await self._ready(bot, context)
        update = await text(bot, context, "квантовая телепортация")

        assert session.current.screen is Screen.MOMENTS
        assert session.moments == []
        assert "Nothing in this episode" in outcome(update).last

    async def test_the_empty_answer_still_offers_a_way_on(self, bot, context):
        """A dead end is the one thing no screen may be."""
        await self._ready(bot, context)
        update = await text(bot, context, "квантовая телепортация")

        buttons = payloads(outcome(update).last_markup)
        assert kb.ACTION_FIND in buttons
        assert kb.NAV_MENU in buttons

    async def test_the_answer_replaces_the_progress_message(self, bot, context):
        """Rather than leaving "still listening…" above the result forever."""
        await self._ready(bot, context)
        update = await text(bot, context, "фолдинг")

        assert "Listening" not in outcome(update).last
        assert payloads(outcome(update).last_markup)

    async def test_the_wait_shows_real_progress_not_a_spinner(self, bot, context):
        """A 30-minute episode sat on one unchanging line for minutes and read
        as a hang. faster-whisper yields segments as it goes, and each knows
        where in the audio it ends, so the bar measures work rather than
        decorating a wait."""
        from podcast_cutter.handlers import _listening_text
        from podcast_cutter.indexer import Progress

        text_now = _listening_text(
            Progress(stage="transcribe", done=450, total=1800),
            started=time.monotonic() - 40,
            estimate=160,
        )
        assert "25%" in text_now
        assert "▰" in text_now

    async def test_the_estimate_comes_from_work_done_not_the_opening_guess(self):
        """So it stops being a promise the moment reality disagrees."""
        from podcast_cutter.handlers import _listening_text
        from podcast_cutter.indexer import Progress

        # A quarter done after 100 s means ~300 s left, whatever was promised.
        shown = _listening_text(
            Progress(stage="transcribe", done=450, total=1800),
            started=time.monotonic() - 100,
            estimate=10,
        )
        assert "5 min" in shown or "300" in shown or "4:" in shown or "5:" in shown

    async def test_no_estimate_is_offered_before_it_would_mean_anything(self):
        """Extrapolating from the first seconds swings wildly; saying nothing
        beats saying something wrong."""
        from podcast_cutter.handlers import _listening_text
        from podcast_cutter.indexer import Progress

        shown = _listening_text(
            Progress(stage="transcribe", done=1, total=1800),
            started=time.monotonic() - 1,
            estimate=0,
        )
        assert "left" not in shown

    async def test_the_note_changes_as_the_wait_goes_on(self):
        """Reading the same cheerful line twice is how a screen becomes a
        spinner in the reader's mind."""
        from podcast_cutter.handlers import NOTE_SECONDS, _listening_text
        from podcast_cutter.indexer import Progress

        def note_at(seconds):
            return _listening_text(
                Progress(stage="transcribe", done=450, total=1800),
                started=time.monotonic() - seconds,
                estimate=160,
            ).splitlines()[-1]

        assert note_at(1) != note_at(NOTE_SECONDS + 1)

    async def test_an_unknown_length_still_renders(self):
        """A feed that reports no duration must not break the screen."""
        from podcast_cutter.handlers import _listening_text
        from podcast_cutter.indexer import Progress

        shown = _listening_text(
            Progress(stage="transcribe", done=0, total=None),
            started=time.monotonic(),
            estimate=0,
        )
        assert "Listening" in shown

    async def test_every_stage_is_shown_even_when_edits_are_throttled(
        self, bot, context
    ):
        """Redrawing a bar faster than the throttle is noise, but a change of
        stage is information — and must not be swallowed by the same limiter.
        Caught by this test: with fast fakes, "listening" never appeared."""
        await self._ready(bot, context)
        update = await text(bot, context, "фолдинг")

        said = " ".join(
            [t for t, _ in outcome(update).edits]
            + [t for t, _ in update.effective_message.replies]
        )
        assert "Fetching" in said
        assert "Listening" in said

    async def test_one_heavy_job_per_person(self, bot, context):
        session = await self._ready(bot, context)
        bot._busy_users.add(1)

        await text(bot, context, "фолдинг")

        assert session.current.screen is not Screen.MOMENTS

    async def test_the_search_is_journalled(self, bot, context, store):
        await self._ready(bot, context)
        await text(bot, context, "фолдинг")

        rows = store._execute(
            "SELECT outcome, detail FROM events WHERE action = 'search_audio'"
        )
        assert rows and rows[0]["outcome"] == "ok"
        assert rows[0]["detail"] == "фолдинг"

    async def test_an_empty_result_is_journalled_differently(
        self, bot, context, store
    ):
        """Otherwise the panel cannot tell a working search from a useless
        one — both look like a successful request."""
        await self._ready(bot, context)
        await text(bot, context, "нет такого текста")

        rows = store._execute(
            "SELECT outcome FROM events WHERE action = 'search_audio'"
        )
        assert rows[0]["outcome"] == "empty"

    async def test_the_episode_is_only_listened_to_once(
        self, bot, context, indexer
    ):
        await self._ready(bot, context)
        await text(bot, context, "фолдинг")
        await tap(bot, context, kb.ACTION_FIND)
        await text(bot, context, "белков")

        assert indexer.recognizer.calls == 1


class TestOpeningAMoment:
    async def test_a_moment_opens_the_ordinary_clip_editor(self, bot, context):
        session, _ = await at_the_editor(bot, context)
        session.go(Screen.MOMENTS)

        await tap(bot, context, f"{kb.MOMENT_PREFIX}:122")

        assert session.current.screen is Screen.INTERVAL
        assert session.clip_start == 122
        assert session.awaiting is Awaiting.INTERVAL

    async def test_a_moment_past_the_episode_is_clamped(self, bot, context):
        session, _ = await at_the_editor(bot, context, duration=100)
        session.go(Screen.MOMENTS)

        await tap(bot, context, f"{kb.MOMENT_PREFIX}:99999")

        assert session.clip_start < 100

    async def test_a_stale_moment_button_does_not_error(self, bot, context):
        """Buttons outlive sessions; a tap on an old message must land
        somewhere sane rather than raise."""
        session = session_of(context)
        session.episode = None
        session.go(Screen.MOMENTS)

        await tap(bot, context, f"{kb.MOMENT_PREFIX}:120")

        assert session.current.screen is Screen.MENU


class TestWithoutTranscription:
    async def test_losing_recognition_costs_the_search_not_the_bot(
        self, settings, client, store, context
    ):
        bot = PodcastCutterBot(settings, client, store, None)
        bot.bot_username = "podcast_cutter_bot"
        session = session_of(context)
        session.episode = make_episode()
        session.go(Screen.INTERVAL)

        update = await tap(bot, context, kb.ACTION_FIND)

        assert "off" in update.shown.lower()
        # And the bot is still usable afterwards.
        assert payloads(update.markup)
