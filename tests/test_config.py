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


class TestInvariants:
    def test_upload_limit_stays_under_telegrams_50mb(self):
        assert Settings(bot_token="t", api_key="k", api_secret="s").max_upload_bytes < (
            50 * 1024 * 1024
        )

    def test_settings_are_immutable(self):
        settings = Settings(bot_token="t", api_key="k", api_secret="s")
        with pytest.raises(FrozenInstanceError):
            settings.bot_token = "other"  # type: ignore[misc]
