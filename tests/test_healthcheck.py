"""The liveness heartbeat and the healthcheck that reads it.

The point of the pair is that it catches a *wedged event loop*, which a
process-alive check cannot: the bot rewrites the marker from a job on the loop,
and the healthcheck fails once that marker goes stale.
"""

from __future__ import annotations

import importlib.util
import time
from pathlib import Path

from podcast_cutter.app import _touch_heartbeat
from podcast_cutter.config import Settings


def _settings(data_dir: Path) -> Settings:
    return Settings(bot_token="t", api_key="k", api_secret="s", data_dir=data_dir)


def _load_healthcheck():
    path = Path(__file__).resolve().parent.parent / "scripts" / "healthcheck.py"
    spec = importlib.util.spec_from_file_location("healthcheck", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_touch_writes_a_fresh_marker(tmp_path):
    settings = _settings(tmp_path)
    _touch_heartbeat(settings)
    assert settings.heartbeat_path.exists()
    assert time.time() - settings.heartbeat_path.stat().st_mtime < 5


def test_touch_never_raises_on_an_unwritable_path(tmp_path):
    # A file where the health *directory* should be: mkdir must fail, and the
    # loop must not see the exception.
    (tmp_path / "health").write_text("not a directory")
    _touch_heartbeat(_settings(tmp_path))  # must not raise


def test_healthcheck_passes_on_a_fresh_beat(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _touch_heartbeat(settings)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert _load_healthcheck().main() == 0


def test_healthcheck_fails_when_the_marker_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert _load_healthcheck().main() == 1


def test_healthcheck_fails_on_a_stale_beat(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _touch_heartbeat(settings)
    stale = time.time() - 10 * 60
    import os

    os.utime(settings.heartbeat_path, (stale, stale))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert _load_healthcheck().main() == 1
