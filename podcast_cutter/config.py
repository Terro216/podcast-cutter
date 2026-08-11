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

#: ``MEDIA_PROXY_MODE`` values.
#:
#: ``fallback`` fetches directly and only reaches for the proxy when a fetch
#: fails the way a blocked egress fails — a connect timeout or a 403 — so the
#: episodes that already work keep their existing route and their latency.
#: ``always`` sends audio through the proxy first and keeps direct as the
#: safety net. ``off`` is the kill switch: the URL stays configured, the
#: feature stops being used, and a rollback is one variable instead of two.
PROXY_MODE_FALLBACK = "fallback"
PROXY_MODE_ALWAYS = "always"
PROXY_MODE_OFF = "off"
PROXY_MODES = (PROXY_MODE_FALLBACK, PROXY_MODE_ALWAYS, PROXY_MODE_OFF)

#: Probed through the proxy at startup. Megaphone is the host the detour
#: exists for, so proving *it* answers is worth more than a generic target.
DEFAULT_PROXY_PROBE_URL = "https://traffic.megaphone.fm/"

#: ``ASR_BACKEND`` values. Only the local one exists so far; the name is
#: validated at startup anyway, because a typo should stop the bot rather than
#: surface as a failure on somebody's first search.
ASR_BACKEND_LOCAL = "local"
ASR_BACKENDS = (ASR_BACKEND_LOCAL,)


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
    #: Refuse episodes longer than this outright. The byte ceiling above bounds
    #: the download, but not the work: seeking and re-encoding scale with the
    #: source, and a feed can advertise an arbitrarily long file. Six hours
    #: clears the longest real podcasts with room to spare.
    max_source_seconds: int = 6 * 3600
    #: Allow episode URLs that resolve into private address space. Off in
    #: production; the integration tests turn it on because they serve audio
    #: from a real HTTP server on 127.0.0.1, which is the point of them.
    allow_private_sources: bool = False

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

    # --- speech recognition -----------------------------------------------
    #: The kill switch. Transcription is minutes of CPU where a cut is seconds,
    #: so it must be possible to stop it without stopping the bot.
    asr_enabled: bool = True
    asr_backend: str = "local"
    #: ``base`` measured at RTF 0.07 on this host against ``small``'s 0.23, for
    #: a difference that rarely changes which moment a search lands on.
    asr_model: str = "base"
    #: Scaling past this is poor on the production host — 4 to 8 threads bought
    #: only ~1.5x — so the default buys most of it without taking the machine.
    asr_threads: int = 8
    #: Whole episodes are decoded, so this is generous by necessity: six hours
    #: of audio at RTF 0.07 is roughly half an hour of work.
    transcribe_timeout: float = 3600.0

    #: Scratch space for temporary audio. One subdirectory per job.
    work_dir: Path = field(default_factory=lambda: Path("/tmp/podcast-cutter"))

    # --- durable state ----------------------------------------------------
    #: Database and log files live here. Mount it, or a redeploy takes the
    #: history with it: recreating a container discards its json log.
    data_dir: Path = field(default_factory=lambda: Path("data"))
    #: Journal rows older than this are deleted at startup. 0 keeps everything.
    log_retention_days: int = 90
    #: Rotating log file size and count, alongside stdout.
    log_file_bytes: int = 10 * 1024 * 1024
    log_file_count: int = 5

    #: Telegram user ids allowed to run /stats. Empty means nobody.
    admin_ids: frozenset[int] = frozenset()

    # --- media proxy ------------------------------------------------------
    #: HTTP proxy for episode audio fetches **only**; the directory API and
    #: Telegram always go direct. Empty — the default — disables the whole
    #: feature, so an unconfigured bot behaves exactly as it did before the
    #: proxy existed. One URL is the entire configuration surface: moving to a
    #: different egress host is a one-variable change.
    #:
    #: Must be ``http://``: ffmpeg only understands a plain HTTP proxy, which
    #: it uses for ``https://`` sources via CONNECT.
    media_proxy: str = ""
    #: HTTP proxy for the Telegram Bot API itself. Empty — the default — goes
    #: direct, which is what every deployment should want.
    #:
    #: This exists because on 2026-08-10 the production host's egress stopped
    #: reaching Telegram at all: `api.telegram.org` and `core.telegram.org` both
    #: became TCP black holes while GitHub and PyPI answered in under 150 ms,
    #: i.e. selective filtering rather than an outage. The bot cannot start
    #: without `getMe`, so there is no degraded mode to fall back to.
    #:
    #: Unlike :attr:`media_proxy` this has no fallback ordering: a proxy that
    #: works for one request works for all of them, and one that does not means
    #: no bot. Setting it therefore makes the proxy a hard dependency — worth
    #: knowing, because until now losing the tunnel only cost audio fetches.
    telegram_proxy: str = ""
    #: See :data:`PROXY_MODES`.
    media_proxy_mode: str = PROXY_MODE_FALLBACK
    media_proxy_probe_url: str = DEFAULT_PROXY_PROBE_URL
    #: How long the proxy is treated as dead after it fails at the transport
    #: level. Long enough not to retry a down host on every cut, short enough
    #: that a restarted proxy is picked up without restarting the bot.
    media_proxy_cooldown: float = 60.0
    #: Connect timeout for an attempt that still has a route behind it. The
    #: megaphone failure is a *silent* drop that costs ~9 s of SYN retries, and
    #: paying that before the detour would be most of the delay the user sees.
    media_proxy_connect_timeout: float = 8.0

    @property
    def proxy_enabled(self) -> bool:
        """Whether audio fetches may use the proxy at all."""
        return bool(self.media_proxy) and self.media_proxy_mode != PROXY_MODE_OFF

    @property
    def database_path(self) -> Path:
        return self.data_dir / "podcast_cutter.db"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "logs" / "bot.log"

    @property
    def asr_model_dir(self) -> Path:
        """Where recognition models live.

        On the mounted volume rather than in the image: the weights are an
        order of magnitude larger than everything else in the build, and baking
        them in means re-shipping them on every redeploy of a one-line change.
        """
        return self.data_dir / "models"

    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.admin_ids

    def __post_init__(self) -> None:
        if self.max_cut_seconds <= 0:
            raise ConfigError("MAX_CUT_SECONDS must be positive.")
        if self.max_source_seconds <= 0:
            raise ConfigError("MAX_SOURCE_SECONDS must be positive.")
        if self.max_source_seconds < self.max_cut_seconds:
            raise ConfigError(
                "MAX_SOURCE_SECONDS must be at least MAX_CUT_SECONDS, "
                f"got {self.max_source_seconds} < {self.max_cut_seconds}."
            )
        if self.max_concurrent_jobs < 1:
            raise ConfigError("MAX_CONCURRENT_JOBS must be at least 1.")
        if self.log_retention_days < 0:
            raise ConfigError("LOG_RETENTION_DAYS cannot be negative.")
        if self.asr_backend not in ASR_BACKENDS:
            raise ConfigError(
                f"ASR_BACKEND must be one of {', '.join(ASR_BACKENDS)}, "
                f"got {self.asr_backend!r}."
            )
        if self.asr_threads < 1:
            raise ConfigError("ASR_THREADS must be at least 1.")
        if self.media_proxy_mode not in PROXY_MODES:
            raise ConfigError(
                f"MEDIA_PROXY_MODE must be one of {', '.join(PROXY_MODES)}, "
                f"got {self.media_proxy_mode!r}."
            )
        if self.telegram_proxy and not self.telegram_proxy.startswith(
            ("http://", "https://", "socks5://")
        ):
            raise ConfigError(
                "TELEGRAM_PROXY must be an http://, https:// or socks5:// URL, "
                f"got {self.telegram_proxy!r}."
            )
        if self.media_proxy and not self.media_proxy.startswith("http://"):
            raise ConfigError(
                "MEDIA_PROXY must be an http:// URL — ffmpeg understands no "
                f"other kind of proxy — got {self.media_proxy!r}."
            )
        if self.media_proxy_cooldown < 0:
            raise ConfigError("media_proxy_cooldown cannot be negative.")
        if self.media_proxy_connect_timeout <= 0:
            raise ConfigError("media_proxy_connect_timeout must be positive.")


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


def _non_negative_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}.") from exc
    if value < 0:
        raise ConfigError(f"{name} cannot be negative, got {value}.")
    return value


def _flag(name: str, default: bool) -> bool:
    """Parse a boolean the way people actually write them in a ``.env``."""
    raw = _env(name).lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(
        f"{name} must be true or false, got {raw!r}."
    )


def _id_set(name: str) -> frozenset[int]:
    """Parse a comma- or space-separated list of Telegram user ids."""
    raw = _env(name)
    if not raw:
        return frozenset()

    ids = set()
    for chunk in raw.replace(",", " ").split():
        try:
            ids.add(int(chunk))
        except ValueError as exc:
            raise ConfigError(
                f"{name} must be a comma-separated list of numeric Telegram "
                f"user ids; {chunk!r} is not one."
            ) from exc
    return frozenset(ids)


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
        max_source_seconds=_positive_int(
            "MAX_SOURCE_SECONDS", Settings.max_source_seconds
        ),
        max_concurrent_jobs=_positive_int(
            "MAX_CONCURRENT_JOBS", Settings.max_concurrent_jobs
        ),
        work_dir=Path(_env("WORK_DIR") or "/tmp/podcast-cutter"),
        data_dir=Path(_env("DATA_DIR") or "data"),
        log_retention_days=_non_negative_int(
            "LOG_RETENTION_DAYS", Settings.log_retention_days
        ),
        admin_ids=_id_set("ADMIN_IDS"),
        asr_enabled=_flag("ASR_ENABLED", Settings.asr_enabled),
        asr_backend=(_env("ASR_BACKEND") or Settings.asr_backend).lower(),
        asr_model=_env("ASR_MODEL") or Settings.asr_model,
        asr_threads=_positive_int("ASR_THREADS", Settings.asr_threads),
        telegram_proxy=_env("TELEGRAM_PROXY").rstrip("/"),
        media_proxy=_env("MEDIA_PROXY").rstrip("/"),
        media_proxy_mode=(
            _env("MEDIA_PROXY_MODE") or Settings.media_proxy_mode
        ).lower(),
        media_proxy_probe_url=(
            _env("MEDIA_PROXY_PROBE_URL") or DEFAULT_PROXY_PROBE_URL
        ),
    )
