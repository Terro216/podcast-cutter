"""Searching inside an episode, driven through the routers.

The engine is tested elsewhere; what matters here is that a person can reach
it, is told the truth while they wait, and lands somewhere useful afterwards —
including when the answer is "nothing".
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import pytest

from conftest import FakeContext, FakeUpdate, make_episode
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
        assert rows[0]["detail"] is None

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


class TestWaitingInLine:
    """What a second person sees. «Queued» was the whole message before, and a
    wait with no number is indistinguishable from a hang — at which point
    people press the button again and make the line longer."""

    @staticmethod
    def _asking(bot, episode_id: str, phrase: str, user_id: int):
        """One person, mid-search, on their own session."""
        own = FakeContext()
        session = get_session(own.user_data)
        session.select_episode(make_episode(episode_id))
        session.go(Screen.INTERVAL)
        session.awaiting = Awaiting.PHRASE
        update = FakeUpdate(text=phrase, user_id=user_id)
        return update, asyncio.create_task(bot.on_text(update, own))

    @staticmethod
    async def _until(predicate, tries: int = 300) -> bool:
        for _ in range(tries):
            if predicate():
                return True
            await asyncio.sleep(0.01)
        return False

    async def test_the_place_in_line_is_a_number(self, bot, store):
        await store.accept_terms(2, "en", bot.settings.terms_version)
        held = asyncio.Event()

        async def slow(path, language=None, on_segment=None):
            await held.wait()
            return [], "ru"

        bot.indexer.recognizer.transcribe = slow
        _, first = self._asking(bot, "10", "фолдинг", user_id=1)
        assert await self._until(lambda: bot.listening._running == "10")

        second, waiting = self._asking(bot, "11", "белков", user_id=2)

        def said() -> list[str]:
            children = second.effective_message.children
            return [t for t, _ in children[-1].edits] if children else []

        shown = await self._until(lambda: any("in line" in t for t in said()))
        # Read before the answer overwrites it: the progress message is edited
        # in place, so the waiting screen only exists while the wait does.
        waited = said()

        held.set()
        await asyncio.gather(first, waiting)
        await bot.listening.stop()
        assert shown
        assert any("2nd in line" in text for text in waited)

    async def test_the_line_is_full_of_episodes_not_of_people(
        self, bot, context, store
    ):
        """Ten people wanting one episode is one job. Refusing the tenth
        would be refusing them nothing."""
        episode = make_episode("10")
        for user in range(1, 9):
            await bot.listening.submit(episode, user_id=user, chat_id=user)

        assert await bot.listening.depth() == 1
        await bot.listening.stop()

    async def test_a_full_queue_refuses_a_new_episode(self, bot, context):
        from podcast_cutter.handlers import MAX_ASR_QUEUE

        for index in range(MAX_ASR_QUEUE):
            await bot.listening.submit(
                make_episode(f"9{index}"), user_id=index, chat_id=index
            )
        await bot.listening.stop()

        session, _ = await at_the_editor(bot, context)
        await tap(bot, context, kb.ACTION_FIND)
        update = await text(bot, context, "фолдинг")

        assert "queue is full" in update.shown
        assert session.current.screen is not Screen.MOMENTS


class TestHowAnswersRead:
    """Reported from real use: three buttons cut mid-sentence, none of which
    contained the phrase that was searched for."""

    def _view(self, phrase="фолдинг"):
        from podcast_cutter.transcripts import Moment

        session = Session()
        session.episode = make_episode()
        session.phrase = phrase
        session.go(Screen.MOMENTS)
        session.moments = [
            Moment(
                start=880,
                end=910,
                text="начало окна где-то далеко отсюда и только потом "
                "мы говорим про фолдинг белков и что это значит",
                score=5.0,
                clip_start=903,
            )
        ]
        return screens.moments(session)

    async def test_the_quotation_contains_the_phrase(self):
        assert "фолдинг" in self._view().text

    async def test_the_phrase_is_emphasised(self):
        assert "<b>фолдинг</b>" in self._view().text

    async def test_the_buttons_only_number_and_stamp(self):
        """A button is one short line; a sentence squeezed into one is cut
        mid-word, so the text belongs in the message."""
        view = self._view()
        labels = [
            b.text
            for row in view.keyboard.inline_keyboard
            for b in row
            if b.callback_data.startswith(kb.MOMENT_PREFIX)
        ]
        assert labels == ["1 · 15:03"]

    async def test_answers_share_one_row(self):
        from podcast_cutter.transcripts import Moment

        session = Session()
        session.episode = make_episode()
        session.phrase = "фолдинг"
        session.go(Screen.MOMENTS)
        session.moments = [
            Moment(start=t, end=t + 30, text="фолдинг белков", score=1.0, clip_start=t)
            for t in (100, 500, 900)
        ]
        rows = screens.moments(session).keyboard.inline_keyboard
        picks = [r for r in rows if r[0].callback_data.startswith(kb.MOMENT_PREFIX)]
        assert len(picks) == 1 and len(picks[0]) == 3

    async def test_words_are_not_glued_to_the_emphasised_match(self):
        """Reported from real use: "ту жевышкуна прикладной".

        The escaping helper collapses and strips whitespace, so a separator
        carried inside a fragment is eaten before it is ever rendered.
        """
        rendered = self._view().text
        assert "провышленный" not in rendered
        assert " <b>фолдинг</b> " in rendered

    async def test_markup_in_the_transcript_cannot_break_the_message(self):
        """Recognised text is arbitrary, and it is rendered as HTML."""
        view = self._view(phrase="<b>")
        assert "<b>&lt;" in view.text or "&lt;b&gt;" in view.text


class TestGettingBackToTheOtherMoments:
    """Asked in real use: "why does the list disappear, what if I want the
    second one?" Opening a moment edits the same message, so the list is
    replaced — recoverable with Back, but only if you know that."""

    async def _searched(self, bot, context):
        session, _ = await at_the_editor(bot, context)
        await tap(bot, context, kb.ACTION_FIND)
        await text(bot, context, "фолдинг")
        return session

    async def test_back_returns_to_the_moments(self, bot, context):
        session = await self._searched(bot, context)
        await tap(bot, context, f"{kb.MOMENT_PREFIX}:122")
        assert session.current.screen is Screen.INTERVAL

        update = await tap(bot, context, kb.NAV_BACK)

        assert session.current.screen is Screen.MOMENTS
        assert "фолдинг" in update.shown

    async def test_the_moments_survive_the_trip(self, bot, context):
        """The list is rebuilt from the session, so it must still be there."""
        session = await self._searched(bot, context)
        found = len(session.moments)
        await tap(bot, context, f"{kb.MOMENT_PREFIX}:122")
        await tap(bot, context, kb.NAV_BACK)
        assert len(session.moments) == found

    async def test_the_clip_editor_says_where_back_leads(self, bot, context):
        session = await self._searched(bot, context)
        session.moments = session.moments * 2  # more than one to return to
        update = await tap(bot, context, f"{kb.MOMENT_PREFIX}:122")
        assert "Back returns to" in update.shown

    async def test_it_says_nothing_when_there_is_nothing_to_return_to(
        self, bot, context
    ):
        """A phrase outlives the screen that set it, so this is asked of the
        history: opening an episode by hand after an earlier search must not
        promise a list that is not behind you."""
        session, _ = await at_the_editor(bot, context)
        session.phrase = "фолдинг"
        view = screens.interval(session, bot.settings)
        assert "Back returns to" not in view.text

    async def test_the_trail_shows_what_was_searched_for(self, bot, context):
        await self._searched(bot, context)
        update = await tap(bot, context, f"{kb.MOMENT_PREFIX}:122")
        assert "🔎" in update.shown


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
        await store.accept_terms(1, "en", settings.terms_version)
        bot = PodcastCutterBot(settings, client, store, None)
        bot.bot_username = "podcast_cutter_bot"
        session = session_of(context)
        session.episode = make_episode()
        session.go(Screen.INTERVAL)

        update = await tap(bot, context, kb.ACTION_FIND)

        assert "off" in update.shown.lower()
        # And the bot is still usable afterwards.
        assert payloads(update.markup)
