"""Configuration, loaded once at startup.

Everything tunable lives here so the rest of the code never reaches for
``os.getenv``. Missing or nonsensical values fail loudly at startup with a
message naming the variable, instead of surfacing as a confusing error on the
first user request.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from .errors import ConfigError

DEFAULT_API_BASE_URL = "https://api.podcastindex.org/api/1.0"

# The Bot API rejects uploads above 50 MB. Stay under it so a slightly
# oversized ID3 header or container overhead cannot push us over the edge.
DEFAULT_MAX_UPLOAD_BYTES = 45 * 1024 * 1024


@dataclass(frozen=True)
class Settings:
    """Immutable runtime configuration."""

    bot_token: str
    api_key: str
    api_secret: str
    api_base_url: str = DEFAULT_API_BASE_URL

    # --- paging -----------------------------------------------------------
    podcasts_per_page: int = 5
    episodes_per_page: int = 5
    #: Hard cap on how many episodes we hold per user session.
    max_episodes_per_session: int = 500

    # --- limits -----------------------------------------------------------
    max_cut_seconds: int = 15 * 60
    #: Clip length used when the user names a single moment rather than a range.
    default_clip_seconds: int = 60
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    #: Refuse to download source files larger than this in the fallback path.
    max_source_bytes: int = 400 * 1024 * 1024

    # --- timeouts (seconds) -----------------------------------------------
    api_timeout: float = 15.0
    probe_timeout: float = 30.0
    ffmpeg_timeout: float = 300.0
    download_timeout: float = 600.0
    upload_timeout: float = 300.0
    #: Drop an idle conversation after this long so nobody is stuck forever.
    conversation_timeout: int = 20 * 60

    # --- concurrency ------------------------------------------------------
    #: Simultaneous cutting jobs across the whole bot. ffmpeg is the bottleneck.
    max_concurrent_jobs: int = 2

    #: Scratch space for temporary audio. One subdirectory per job.
    work_dir: Path = field(default_factory=lambda: Path("/tmp/podcast-cutter"))

    def __post_init__(self) -> None:
        if self.max_cut_seconds <= 0:
            raise ConfigError("MAX_CUT_SECONDS must be positive.")
        if self.max_concurrent_jobs < 1:
            raise ConfigError("MAX_CONCURRENT_JOBS must be at least 1.")


def _env(name: str) -> str:
    """Read one variable, tolerating quotes left in by the loader.

    ``python-dotenv`` strips surrounding quotes from a ``.env`` file, but
    ``docker run --env-file`` passes them through literally. A token arriving
    as ``"123:abc"`` then fails authentication for no visible reason, so strip
    a matching pair here rather than making that someone's afternoon.
    """
    value = (os.getenv(name) or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


def _required(name: str) -> str:
    value = _env(name)
    if not value:
        raise ConfigError(
            f"Environment variable {name} is not set. "
            "Copy .env.example to .env and fill it in."
        )
    return value


def _positive_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}.") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be positive, got {value}.")
    return value


def load_settings() -> Settings:
    """Read the environment (and ``.env``) into a validated :class:`Settings`."""
    load_dotenv()

    base_url = _env("PODCAST_API_BASEURL") or DEFAULT_API_BASE_URL
    # A trailing slash would turn every request path into a double slash.
    base_url = base_url.rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError(
            "PODCAST_API_BASEURL must start with http:// or https://, "
            f"got {base_url!r}."
        )

    return Settings(
        bot_token=_required("BOT_TOKEN"),
        api_key=_required("PODCAST_API_KEY"),
        api_secret=_required("PODCAST_API_SECRET"),
        api_base_url=base_url,
        max_cut_seconds=_positive_int("MAX_CUT_SECONDS", Settings.max_cut_seconds),
        max_concurrent_jobs=_positive_int(
            "MAX_CONCURRENT_JOBS", Settings.max_concurrent_jobs
        ),
        work_dir=Path(_env("WORK_DIR") or "/tmp/podcast-cutter"),
    )
