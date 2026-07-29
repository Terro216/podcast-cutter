"""Wiring checks.

These catch the class of bug that made the old bot feel broken: a state with no
handlers, a menu button the conversation swallows, or a documented command that
does not exist.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from telegram import Chat, Message, Update, User
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
)

from podcast_cutter import keyboards as kb
from podcast_cutter.app import BOT_COMMANDS, build_application
from podcast_cutter.config import Settings
from podcast_cutter.handlers import HELP_TEXT
from podcast_cutter.states import State

TOKEN = "123456:AAHfakefakefakefakefakefakefakefake"


@pytest.fixture(scope="module")
def application():
    return build_application(
        Settings(bot_token=TOKEN, api_key="k", api_secret="s")
    )


@pytest.fixture(scope="module")
def conversation(application) -> ConversationHandler:
    for handlers in application.handlers.values():
        for handler in handlers:
            if isinstance(handler, ConversationHandler):
                return handler
    raise AssertionError("no ConversationHandler registered")


def _text_update(text: str) -> Update:
    """A plain text message, as a menu button press arrives."""
    return Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=Chat(id=1, type=Chat.PRIVATE),
            from_user=User(id=1, first_name="Tester", is_bot=False),
            text=text,
        ),
    )


def _command_names(handlers) -> set[str]:
    names = set()
    for handler in handlers:
        if isinstance(handler, CommandHandler):
            names.update(handler.commands)
    return names


class TestConversationStates:
    @pytest.mark.parametrize("state", list(State), ids=lambda s: s.name)
    def test_every_state_has_at_least_one_handler(self, conversation, state):
        # ENTER_EPISODE_NAME used to be an empty list, so any error routed
        # there left the user permanently stuck.
        assert conversation.states.get(state), f"{state.name} is a dead end"

    def test_every_state_accepts_text_or_a_button(self, conversation):
        for state in State:
            handlers = conversation.states[state]
            assert any(
                isinstance(h, (MessageHandler, CallbackQueryHandler)) for h in handlers
            ), f"{state.name} accepts no user input"

    def test_timeout_is_handled(self, conversation):
        assert conversation.states.get(ConversationHandler.TIMEOUT)
        assert conversation.conversation_timeout

    def test_reentry_is_allowed(self, conversation):
        # Otherwise main-menu buttons do nothing mid-conversation.
        assert conversation.allow_reentry is True


class TestEscapeHatches:
    def test_cancel_and_start_are_always_available(self, conversation):
        assert {"cancel", "start", "help"} <= _command_names(conversation.fallbacks)

    def test_the_cancel_button_works_from_any_state(self, conversation):
        assert any(
            isinstance(h, CallbackQueryHandler) for h in conversation.fallbacks
        )


class TestMenuButtons:
    """Menu presses are ordinary text, so routing them takes explicit care."""

    @pytest.mark.parametrize(
        "label", [b for b in kb.MENU_BUTTONS if b != kb.BTN_HELP], ids=str
    )
    def test_each_button_starts_its_flow(self, conversation, label):
        # allow_reentry means entry points are consulted even mid-conversation,
        # so this is what makes the menu work from anywhere.
        assert any(
            h.check_update(_text_update(label)) for h in conversation.entry_points
        ), f"{label} starts nothing"

    def test_help_is_reachable_as_a_fallback(self, conversation):
        assert any(
            h.check_update(_text_update(kb.BTN_HELP))
            for h in conversation.fallbacks
            if isinstance(h, MessageHandler)
        )

    @pytest.mark.parametrize("state", list(State), ids=lambda s: s.name)
    def test_buttons_are_never_read_as_typed_input(self, conversation, state):
        # Otherwise pressing "🎲 Surprise me" while a search is open searches
        # for the literal string "🎲 Surprise me".
        text_handlers = [
            h for h in conversation.states[state] if isinstance(h, MessageHandler)
        ]
        for label in kb.MENU_BUTTONS:
            assert not any(
                h.check_update(_text_update(label)) for h in text_handlers
            ), f"{state.name} would treat {label} as user input"

    def test_ordinary_text_still_reaches_the_state(self, conversation):
        text_handlers = [
            h
            for h in conversation.states[State.ASK_PODCAST_NAME]
            if isinstance(h, MessageHandler)
        ]
        assert any(h.check_update(_text_update("radiolab")) for h in text_handlers)


class TestHandlerOrder:
    def test_the_conversation_is_registered_first(self, application):
        # Standalone /help and menu handlers must not shadow the conversation
        # while a user is inside it.
        first = application.handlers[0][0]
        assert isinstance(first, ConversationHandler)


class TestPublishedCommands:
    def test_every_published_command_exists(self, application, conversation):
        registered = _command_names(conversation.entry_points)
        registered |= _command_names(conversation.fallbacks)
        for handlers in application.handlers.values():
            registered |= _command_names(handlers)

        for name, _ in BOT_COMMANDS:
            assert name in registered, f"/{name} is advertised but not handled"

    def test_every_published_command_is_documented(self):
        for name, _ in BOT_COMMANDS:
            assert f"/{name}" in HELP_TEXT, f"/{name} is missing from /help"

    def test_descriptions_fit_telegrams_limits(self):
        for name, description in BOT_COMMANDS:
            assert 1 <= len(name) <= 32
            assert 3 <= len(description) <= 256
            assert name.islower()
