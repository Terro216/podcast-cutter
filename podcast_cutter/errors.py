"""Exception hierarchy.

Every error that can reach a user carries a message that is safe and useful to
show verbatim in Telegram. Anything else is an unexpected bug and is reported as
a generic message by the global error handler.
"""


class PodcastCutterError(Exception):
    """Base class for expected, user-presentable failures."""

    #: Shown to the user when the exception has no message of its own.
    default_message = "Something went wrong. Please try again."

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.default_message)

    @property
    def user_message(self) -> str:
        return str(self)


class ConfigError(PodcastCutterError):
    """Invalid or missing configuration. Raised at startup, never at runtime."""

    default_message = "The bot is misconfigured."


class ApiError(PodcastCutterError):
    """The Podcast Index API failed or answered with something unusable."""

    default_message = "The podcast directory is unavailable right now."


class NotFoundError(ApiError):
    """The API answered fine but had no results."""

    default_message = "Nothing found."


class AudioError(PodcastCutterError):
    """Downloading, probing or cutting the audio failed."""

    default_message = "Could not cut this episode."


class IntervalError(PodcastCutterError):
    """The user typed an interval we cannot make sense of."""

    default_message = "That interval does not look right."


class TooLargeError(AudioError):
    """The resulting cut is bigger than Telegram accepts."""

    default_message = "The cut is too large to send."
