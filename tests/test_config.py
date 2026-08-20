from dataclasses import FrozenInstanceError

import pytest

from podcast_cutter.config import DEFAULT_API_BASE_URL, Settings, load_settings
from podcast_cutter.errors import ConfigError

REQUIRED = {
    "BOT_TOKEN": "123:abc",
    "PODCAST_API_KEY": "key",
    "PODCAST_API_SECRET": "secret",
    "DATABASE_KEY": "ab" * 32,
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (
        *REQUIRED,
        "PODCAST_API_BASEURL",
        "MAX_CUT_SECONDS",
        "MAX_SOURCE_SECONDS",
        "MAX_CONCURRENT_JOBS",
        "WORK_DIR",
        "TELEGRAM_PROXY",
        "PODCAST_BLOCKLIST",
        "TERMS_VERSION",
        "LEGAL_BASE_URL",
        "LEGAL_CONTACT",
        "ASR_JOB_RETENTION_HOURS",
        "USER_DATA_RETENTION_DAYS",
    ):
        monkeypatch.delenv(name, raising=False)
    # load_settings() calls load_dotenv(); stop it from picking up a real .env.
    monkeypatch.setattr("podcast_cutter.config.load_dotenv", lambda *a, **k: False)


def with_env(monkeypatch, **overrides):
    for key, value in {**REQUIRED, **overrides}.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


class TestRequiredValues:
    @pytest.mark.parametrize("missing", sorted(REQUIRED))
    def test_names_the_variable_that_is_missing(self, monkeypatch, missing):
        with_env(monkeypatch, **{missing: None})
        with pytest.raises(ConfigError, match=missing):
            load_settings()

    def test_blank_counts_as_missing(self, monkeypatch):
        with_env(monkeypatch, BOT_TOKEN="   ")
        with pytest.raises(ConfigError, match="BOT_TOKEN"):
            load_settings()

    def test_values_are_stripped(self, monkeypatch):
        with_env(monkeypatch, BOT_TOKEN="  123:abc  ")
        assert load_settings().bot_token == "123:abc"

    @pytest.mark.parametrize("value", ["short", "z" * 64, "ab" * 31])
    def test_database_key_is_a_raw_256_bit_hex_value(self, monkeypatch, value):
        with_env(monkeypatch, DATABASE_KEY=value)
        with pytest.raises(ConfigError, match="DATABASE_KEY"):
            load_settings()


class TestQuotedValues:
    """`docker run --env-file` passes quotes through literally."""

    @pytest.mark.parametrize("raw", ['"123:abc"', "'123:abc'", '  "123:abc"  '])
    def test_surrounding_quotes_are_removed(self, monkeypatch, raw):
        with_env(monkeypatch, BOT_TOKEN=raw)
        assert load_settings().bot_token == "123:abc"

    def test_quoted_base_url_is_accepted(self, monkeypatch):
        with_env(
            monkeypatch, PODCAST_API_BASEURL='"https://api.podcastindex.org/api/1.0"'
        )
        assert load_settings().api_base_url == "https://api.podcastindex.org/api/1.0"

    def test_quoted_numbers_are_accepted(self, monkeypatch):
        with_env(monkeypatch, MAX_CUT_SECONDS='"300"')
        assert load_settings().max_cut_seconds == 300

    @pytest.mark.parametrize("raw", ['"unbalanced', "quo'ted", 'a"b'])
    def test_only_matching_outer_pairs_are_stripped(self, monkeypatch, raw):
        # A quote that is part of the value must survive.
        with_env(monkeypatch, BOT_TOKEN=raw)
        assert load_settings().bot_token == raw

    def test_an_empty_quoted_value_counts_as_missing(self, monkeypatch):
        with_env(monkeypatch, BOT_TOKEN='""')
        with pytest.raises(ConfigError, match="BOT_TOKEN"):
            load_settings()


class TestBaseUrl:
    def test_defaults(self, monkeypatch):
        with_env(monkeypatch)
        assert load_settings().api_base_url == DEFAULT_API_BASE_URL

    def test_trailing_slash_is_removed(self, monkeypatch):
        # ".../api/1.0/" + "/search/byterm" used to produce a double slash.
        with_env(
            monkeypatch, PODCAST_API_BASEURL="https://api.podcastindex.org/api/1.0/"
        )
        assert load_settings().api_base_url == "https://api.podcastindex.org/api/1.0"

    def test_rejects_a_non_http_url(self, monkeypatch):
        with_env(monkeypatch, PODCAST_API_BASEURL="api.podcastindex.org")
        with pytest.raises(ConfigError, match="PODCAST_API_BASEURL"):
            load_settings()


class TestPodcastBlocklist:
    def test_empty_by_default(self, monkeypatch):
        with_env(monkeypatch)
        assert not load_settings().podcast_blocklist

    def test_reads_feed_ids(self, monkeypatch):
        with_env(monkeypatch, PODCAST_BLOCKLIST="12, 34 56")
        settings = load_settings()
        assert settings.podcast_blocklist == frozenset({"12", "34", "56"})
        assert settings.is_podcast_blocked("34")


class TestNumericOverrides:
    def test_reads_an_override(self, monkeypatch):
        with_env(monkeypatch, MAX_CUT_SECONDS="300")
        assert load_settings().max_cut_seconds == 300

    @pytest.mark.parametrize("value", ["abc", "0", "-5", "1.5"])
    def test_rejects_nonsense(self, monkeypatch, value):
        with_env(monkeypatch, MAX_CUT_SECONDS=value)
        with pytest.raises(ConfigError, match="MAX_CUT_SECONDS"):
            load_settings()

    def test_blank_falls_back_to_the_default(self, monkeypatch):
        with_env(monkeypatch, MAX_CUT_SECONDS="")
        assert load_settings().max_cut_seconds == Settings.max_cut_seconds


class TestSourceCeiling:
    """The byte limit bounds the download; this one bounds the work."""

    def test_read_from_the_environment(self, monkeypatch):
        with_env(monkeypatch, MAX_SOURCE_SECONDS="7200")
        assert load_settings().max_source_seconds == 7200

    def test_refuses_a_ceiling_below_the_cut_limit(self):
        # Otherwise every episode long enough to cut from is also too long
        # to open, and no interval can ever be served.
        with pytest.raises(ConfigError, match="MAX_SOURCE_SECONDS"):
            Settings(
                bot_token="t",
                api_key="k",
                api_secret="s",
                max_cut_seconds=900,
                max_source_seconds=600,
            )

    def test_default_clears_the_longest_real_episodes(self):
        assert Settings(
            bot_token="t", api_key="k", api_secret="s"
        ).max_source_seconds >= 4 * 3600

    def test_private_sources_are_refused_by_default(self):
        assert not Settings(
            bot_token="t", api_key="k", api_secret="s"
        ).allow_private_sources


class TestTelegramProxy:
    """The escape hatch for a host that cannot reach Telegram at all.

    Added after the production host's egress started black-holing
    api.telegram.org while ordinary sites answered normally.
    """

    def test_direct_by_default(self):
        assert Settings(
            bot_token="t", api_key="k", api_secret="s"
        ).telegram_proxy == ""

    def test_read_from_the_environment(self, monkeypatch):
        with_env(monkeypatch, TELEGRAM_PROXY="http://media-proxy:3128")
        assert load_settings().telegram_proxy == "http://media-proxy:3128"

    def test_a_trailing_slash_is_dropped(self, monkeypatch):
        with_env(monkeypatch, TELEGRAM_PROXY="http://media-proxy:3128/")
        assert load_settings().telegram_proxy == "http://media-proxy:3128"

    @pytest.mark.parametrize(
        "value", ["media-proxy:3128", "ftp://x", "3128", "//media-proxy:3128"]
    )
    def test_refuses_something_that_is_not_a_proxy_url(self, value):
        # At startup, naming the variable — rather than as a connection error
        # on the first update, which says nothing about why.
        with pytest.raises(ConfigError, match="TELEGRAM_PROXY"):
            Settings(
                bot_token="t", api_key="k", api_secret="s", telegram_proxy=value
            )

    @pytest.mark.parametrize(
        "value",
        [
            "http://media-proxy:3128",
            "https://proxy.example:8443",
            "socks5://127.0.0.1:1080",
        ],
    )
    def test_accepts_the_schemes_httpx_understands(self, value):
        assert Settings(
            bot_token="t", api_key="k", api_secret="s", telegram_proxy=value
        ).telegram_proxy == value

    def test_it_is_independent_of_the_media_proxy(self):
        """They solve different problems and must be settable apart: audio
        needs a detour far more often than the API does."""
        settings = Settings(
            bot_token="t",
            api_key="k",
            api_secret="s",
            telegram_proxy="http://a:3128",
            media_proxy="http://b:3128",
        )
        assert settings.telegram_proxy != settings.media_proxy
        assert settings.proxy_enabled


class TestInvariants:
    def test_upload_limit_stays_under_telegrams_50mb(self):
        assert Settings(bot_token="t", api_key="k", api_secret="s").max_upload_bytes < (
            50 * 1024 * 1024
        )

    def test_settings_are_immutable(self):
        settings = Settings(bot_token="t", api_key="k", api_secret="s")
        with pytest.raises(FrozenInstanceError):
            settings.bot_token = "other"  # type: ignore[misc]
