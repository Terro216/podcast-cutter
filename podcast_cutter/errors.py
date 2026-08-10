"""Exception hierarchy.

Every error that can reach a user carries a message that is safe and useful to
show verbatim in Telegram. Anything else is an unexpected bug and is reported as
a generic message by the global error handler.

Each class also carries a stable ``code``. That is what lands in the journal, so
grouping failures in SQL keeps working even if a class is renamed, and the
``/stats`` panel can distinguish "the host refused us" from "the file was
unreadable" — two problems with completely different remedies.
"""


class PodcastCutterError(Exception):
    """Base class for expected, user-presentable failures."""

    #: Shown to the user when the exception has no message of its own.
    default_message = "Something went wrong. Please try again."
    #: Stable identifier recorded in the journal.
    code = "error"

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.default_message)

    @property
    def user_message(self) -> str:
        return str(self)


class ConfigError(PodcastCutterError):
    """Invalid or missing configuration. Raised at startup, never at runtime."""

    default_message = "The bot is misconfigured."
    code = "misconfigured"


class ApiError(PodcastCutterError):
    """The Podcast Index API failed or answered with something unusable."""

    default_message = "The podcast directory is unavailable right now."
    code = "directory_unavailable"


class NotFoundError(ApiError):
    """The API answered fine but had no results."""

    default_message = "Nothing found."
    code = "not_found"


class AudioError(PodcastCutterError):
    """Downloading, probing or cutting the audio failed."""

    default_message = "Could not cut this episode."
    code = "audio_failed"


class BlockedError(AudioError):
    """The episode's host refuses requests from this server's IP address.

    Distinct from a generic audio failure because nothing about the request can
    fix it — Spotify-hosted feeds (Anchor) commonly reject datacenter
    addresses outright — and because it is worth counting separately.
    """

    default_message = (
        "The host of this episode refuses downloads from this server. "
        "This usually means Spotify-hosted feeds; try a different episode."
    )
    code = "host_blocked"


class UnsafeSourceError(AudioError):
    """The episode's audio URL is not something we are willing to open.

    Separate from a generic audio failure because it is not a failure at all:
    nothing was attempted. Enclosure URLs come from feeds anyone can submit, so
    a link with an unexpected scheme, or one resolving into this server's own
    network, is refused before ffmpeg or httpx sees it. Counted on its own so
    the journal can show whether this ever fires in the wild.
    """

    default_message = (
        "This episode's audio link was refused for security reasons."
    )
    code = "unsafe_source"


class UnreachableError(AudioError):
    """The episode's audio could not be fetched at all."""

    default_message = "The episode's audio file could not be reached."
    code = "unreachable"


class UnreadableError(AudioError):
    """The audio was fetched but ffmpeg could not make sense of it."""

    default_message = (
        "Could not cut this episode — the audio file appears to be unreadable."
    )
    code = "unreadable"


class ProcessingTimeout(AudioError):
    """Processing ran past its deadline and was stopped."""

    default_message = (
        "Audio processing took too long and was stopped. "
        "Try a shorter interval or a different episode."
    )
    code = "timeout"


class IntervalError(PodcastCutterError):
    """The user typed an interval we cannot make sense of."""

    default_message = "That interval does not look right."
    code = "bad_interval"


class TooLargeError(AudioError):
    """The resulting cut is bigger than Telegram accepts."""

    default_message = "The cut is too large to send."
    code = "too_large"
