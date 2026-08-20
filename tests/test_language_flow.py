"""The bilingual behaviour, end to end through the real routers.

Three rules under test, because each is a distinct promise:

* Detection: a Russian Telegram client gets Russian without asking for it.
* Choice: an explicit /language pick is stored, survives a fresh session, and
  beats whatever the client claims from then on.
* Routing: the reply keyboard's labels arrive as plain text, so every
  language's labels must keep working — including after a switch, when the
  labels on screen are still the old language's.
"""

from __future__ import annotations

import pytest

from conftest import FakeUpdate
from podcast_cutter import keyboards as kb
from podcast_cutter.errors import NotFoundError
from podcast_cutter.i18n import t
from podcast_cutter.states import Screen, get_session

pytestmark = pytest.mark.asyncio


def russian_update(**kwargs) -> FakeUpdate:
    update = FakeUpdate(**kwargs)
    update.effective_user.language_code = "ru"
    return update


async def text(bot, context, message: str, lang_code: str | None = None):
    update = FakeUpdate(text=message)
    if lang_code:
        update.effective_user.language_code = lang_code
    await bot.on_text(update, context)
    return update


async def tap(bot, context, data: str, lang_code: str | None = None):
    update = FakeUpdate(callback=data)
    if lang_code:
        update.effective_user.language_code = lang_code
    await bot.on_callback(update, context)
    return update


class TestDetection:
    async def test_a_russian_client_is_answered_in_russian(self, bot, context):
        update = russian_update(text="/start")
        await bot.cmd_start(update, context)
        greeting = update.effective_message.replies[0][0]
        assert "Привет" in greeting

    async def test_everyone_else_gets_english(self, bot, context):
        update = FakeUpdate(text="/start")
        update.effective_user.language_code = "de"
        await bot.cmd_start(update, context)
        greeting = update.effective_message.replies[0][0]
        assert "Welcome" in greeting

    async def test_screens_follow_the_detected_language(self, bot, context):
        await text(bot, context, t("ru", "btn_search_podcast"), lang_code="ru")
        session = get_session(context.user_data)
        assert session.language == "ru"
        assert session.current.screen is Screen.ASK_PODCAST


class TestChoice:
    async def test_the_choice_is_stored_and_survives_a_new_session(
        self, bot, context, store
    ):
        # A Russian client explicitly picks English…
        await tap(bot, context, "menu:language", lang_code="ru")
        update = russian_update(callback=f"{kb.LANG_PREFIX}:en")
        await bot.on_callback(update, context)

        assert await store.user_language(1) == "en"

        # …and a brand-new session on a Russian client still answers English.
        context.user_data.clear()
        await text(bot, context, t("en", "btn_trending"), lang_code="ru")
        assert get_session(context.user_data).language == "en"

    async def test_switching_confirms_in_the_new_language(self, bot, context):
        # An English-detected user picks Russian — an actual change.
        update = FakeUpdate(callback=f"{kb.LANG_PREFIX}:ru")
        await bot.on_callback(update, context)
        # The confirmation is a fresh message carrying the re-labelled reply
        # keyboard; the chooser itself is edited in place.
        confirmation, kwargs = update.effective_message.replies[0]
        assert confirmation == t("ru", "language_set")
        assert kwargs["reply_markup"] is not None

    async def test_a_forged_language_payload_is_a_stale_button(
        self, bot, context, store
    ):
        await tap(bot, context, f"{kb.LANG_PREFIX}:xx")
        assert not await store.user_language(1)
        assert get_session(context.user_data).language == "en"


class TestRouting:
    async def test_russian_menu_labels_route_like_english_ones(
        self, bot, context, client
    ):
        await text(bot, context, t("ru", "btn_trending"), lang_code="ru")
        assert get_session(context.user_data).current.screen is Screen.TRENDING
        assert "trending_feeds" in client.calls

    async def test_old_labels_keep_working_after_a_switch(self, bot, context):
        """The reply keyboard on the user's screen is re-labelled only when a
        new one is sent, so the previous language's buttons must not degrade
        into podcast searches."""
        await tap(bot, context, f"{kb.LANG_PREFIX}:ru")
        await text(bot, context, t("en", "btn_surprise"))
        assert get_session(context.user_data).current.screen is Screen.INTERVAL

    async def test_the_menu_offers_the_language_screen(self, bot, context):
        update = await tap(bot, context, "menu:language")
        session = get_session(context.user_data)
        assert session.current.screen is Screen.LANGUAGE
        data = [
            b.callback_data
            for row in update.markup.inline_keyboard
            for b in row
        ]
        assert f"{kb.LANG_PREFIX}:en" in data and f"{kb.LANG_PREFIX}:ru" in data


class TestLocalizedFailures:
    async def test_errors_arrive_in_the_users_language(
        self, bot, context, client
    ):
        client.fail_with = NotFoundError("err_no_podcasts", query="нечто")
        update = await text(bot, context, "нечто", lang_code="ru")
        assert "подкастов не нашлось" in update.shown

    async def test_the_journal_still_reads_english(self, client):
        # Failure details land in SQL; grouping must not fork by reader.
        error = NotFoundError("err_no_podcasts", query="x")
        assert "No podcasts found" in error.user_message()
