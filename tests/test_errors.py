"""The error taxonomy.

Failures are grouped by a stable ``code`` rather than a class name, because
that code lands in the journal: `/stats` and any SQL over `events` must keep
working across refactors. The distinctions matter operationally — "the host
refuses this server" needs a different egress, "unreadable" needs a different
episode, and conflating them hides which one you actually have.
"""

from __future__ import annotations

import inspect

import pytest

from podcast_cutter import errors as errors_mod
from podcast_cutter.errors import (
    ApiError,
    AudioError,
    BlockedError,
    ConfigError,
    IntervalError,
    NotFoundError,
    PodcastCutterError,
    ProcessingTimeout,
    TooLargeError,
    UnreachableError,
    UnreadableError,
)

ALL_ERRORS = [
    obj
    for _, obj in inspect.getmembers(errors_mod, inspect.isclass)
    if issubclass(obj, PodcastCutterError)
]


class TestContract:
    @pytest.mark.parametrize("cls", ALL_ERRORS, ids=lambda c: c.__name__)
    def test_every_error_has_a_user_message(self, cls):
        # Anything reaching a user must read as a sentence, not a stack frame.
        message = cls().user_message
        assert message and message[0].isupper() and message.endswith((".", "!"))

    @pytest.mark.parametrize("cls", ALL_ERRORS, ids=lambda c: c.__name__)
    def test_every_error_has_a_code(self, cls):
        assert cls.code
        assert cls.code.islower()
        assert " " not in cls.code

    def test_codes_are_unique_per_meaning(self):
        # Two different failures sharing a code would merge in the panel.
        codes = [cls.code for cls in ALL_ERRORS if cls is not PodcastCutterError]
        assert len(codes) == len(set(codes)), sorted(codes)

    @pytest.mark.parametrize("cls", ALL_ERRORS, ids=lambda c: c.__name__)
    def test_an_explicit_message_wins_over_the_default(self, cls):
        assert cls("something specific").user_message == "something specific"

    @pytest.mark.parametrize("cls", ALL_ERRORS, ids=lambda c: c.__name__)
    def test_messages_are_short_enough_to_read(self, cls):
        assert len(cls().user_message) < 200


class TestHierarchy:
    @pytest.mark.parametrize(
        "cls",
        [BlockedError, UnreachableError, UnreadableError, ProcessingTimeout,
         TooLargeError],
    )
    def test_audio_failures_are_catchable_as_one(self, cls):
        # Callers that only care "the cut failed" should not enumerate subclasses.
        assert issubclass(cls, AudioError)

    def test_not_found_is_an_api_failure(self):
        assert issubclass(NotFoundError, ApiError)

    @pytest.mark.parametrize(
        "cls", [ConfigError, ApiError, AudioError, IntervalError]
    )
    def test_everything_is_catchable_as_one(self, cls):
        assert issubclass(cls, PodcastCutterError)


class TestBlockedIsDistinct:
    def test_it_is_not_just_a_generic_audio_failure(self):
        # The whole point: a refused IP is not the same problem as a broken file.
        assert BlockedError.code != AudioError.code
        assert BlockedError.code != UnreadableError.code

    def test_it_names_the_likely_cause(self):
        # Operators need to know this is not their bug to fix in code.
        message = BlockedError().user_message.lower()
        assert "refuses" in message or "refuse" in message
        assert "spotify" in message

    def test_the_code_reads_as_what_it_is(self):
        assert BlockedError.code == "host_blocked"


class TestUnreachableIsDistinct:
    def test_separates_a_broken_host_from_a_hostile_one(self):
        assert UnreachableError.code != BlockedError.code

    def test_carries_the_status_when_there_is_one(self):
        assert "404" in UnreachableError("The episode host returned 404.").user_message
