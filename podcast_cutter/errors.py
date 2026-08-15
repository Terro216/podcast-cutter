"""Exception hierarchy.

Every error that can reach a user carries a message that is safe and useful to
show verbatim in Telegram. Anything else is an unexpected bug and is reported as
a generic message by the global error handler.

Messages are carried as :mod:`~podcast_cutter.i18n` keys plus parameters and
rendered in the *reader's* language only at the moment of display — an error
raised while an English speaker and a Russian speaker wait on the same episode
has two correct renderings, so the exception must not commit to either. A
plain string that is not a key passes through untranslated; that is the path
for operator-facing errors (configuration, missing ffmpeg) whose value is
their literal detail.

Each class also carries a stable ``code``. That is what lands in the journal, so
grouping failures in SQL keeps working even if a class is renamed, and the
``/stats`` panel can distinguish "the host refused us" from "the file was
unreadable" — two problems with completely different remedies.
"""

from .i18n import DEFAULT_LANGUAGE, resolve_message


class PodcastCutterError(Exception):
    """Base class for expected, user-presentable failures."""

    #: i18n key shown when the exception is raised without one of its own.
    message_key = "err_generic"
    #: Stable identifier recorded in the journal.
    code = "error"

    def __init__(self, message: str = "", /, **params) -> None:
        #: An i18n key, or literal text for operator-facing errors.
        self.message = message or self.message_key
        self.params = params
        # str(exc) and the logs stay English regardless of who triggered it.
        super().__init__(resolve_message(DEFAULT_LANGUAGE, self.message, params))

    def user_message(self, lang: str = DEFAULT_LANGUAGE) -> str:
        return resolve_message(lang, self.message, self.params)


class ConfigError(PodcastCutterError):
    """Invalid or missing configuration. Raised at startup, never at runtime."""

    message_key = "err_misconfigured"
    code = "misconfigured"


class ApiError(PodcastCutterError):
    """The Podcast Index API failed or answered with something unusable."""

    message_key = "err_directory"
    code = "directory_unavailable"


class NotFoundError(ApiError):
    """The API answered fine but had no results."""

    message_key = "err_not_found"
    code = "not_found"


class AudioError(PodcastCutterError):
    """Downloading, probing or cutting the audio failed."""

    message_key = "err_audio"
    code = "audio_failed"


class BlockedError(AudioError):
    """The episode's host refuses requests from this server's IP address.

    Distinct from a generic audio failure because nothing about the request can
    fix it — Spotify-hosted feeds (Anchor) commonly reject datacenter
    addresses outright — and because it is worth counting separately.
    """

    message_key = "err_blocked"
    code = "host_blocked"


class UnsafeSourceError(AudioError):
    """The episode's audio URL is not something we are willing to open.

    Separate from a generic audio failure because it is not a failure at all:
    nothing was attempted. Enclosure URLs come from feeds anyone can submit, so
    a link with an unexpected scheme, or one resolving into this server's own
    network, is refused before ffmpeg or httpx sees it. Counted on its own so
    the journal can show whether this ever fires in the wild.
    """

    message_key = "err_unsafe"
    code = "unsafe_source"


class UnreachableError(AudioError):
    """The episode's audio could not be fetched at all."""

    message_key = "err_unreachable"
    code = "unreachable"


class UnreadableError(AudioError):
    """The audio was fetched but ffmpeg could not make sense of it."""

    message_key = "err_unreadable"
    code = "unreadable"


class ProcessingTimeout(AudioError):
    """Processing ran past its deadline and was stopped."""

    message_key = "err_timeout"
    code = "timeout"


class IntervalError(PodcastCutterError):
    """The user typed an interval we cannot make sense of."""

    message_key = "err_bad_interval"
    code = "bad_interval"


class TooLargeError(AudioError):
    """The resulting cut is bigger than Telegram accepts."""

    message_key = "err_too_large"
    code = "too_large"
