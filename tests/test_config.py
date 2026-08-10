from dataclasses import FrozenInstanceError

import pytest

from podcast_cutter.config import DEFAULT_API_BASE_URL, Settings, load_settings
from podcast_cutter.errors import ConfigError

REQUIRED = {
    "BOT_TOKEN": "123:abc",
    "PODCAST_API_KEY": "key",
    "PODCAST_API_SECRET": "secret",
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


class TestInvariants:
    def test_upload_limit_stays_under_telegrams_50mb(self):
        assert Settings(bot_token="t", api_key="k", api_secret="s").max_upload_bytes < (
            50 * 1024 * 1024
        )

    def test_settings_are_immutable(self):
        settings = Settings(bot_token="t", api_key="k", api_secret="s")
        with pytest.raises(FrozenInstanceError):
            settings.bot_token = "other"  # type: ignore[misc]
