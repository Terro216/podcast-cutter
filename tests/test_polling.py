"""Regressions for a live event loop with dead Telegram polling."""

from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
from telegram.error import NetworkError, TimedOut
from telegram.request import HTTPXRequest

from podcast_cutter import app, polling
from podcast_cutter.config import Settings
from podcast_cutter.polling import POLLING_STALL_SECONDS, PollingRequest

POLL_URL = "https://api.telegram.org/bot123:fake/getUpdates"


def mock_telegram(monkeypatch, handler):
    monkeypatch.setattr(
        HTTPXRequest,
        "_build_client",
        lambda self: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_empty_poll_is_healthy_and_previous_process_marker_is_cleared(
    tmp_path, monkeypatch
):
    mock_telegram(
        monkeypatch,
        lambda request: httpx.Response(200, json={"ok": True, "result": []}),
    )
    marker = tmp_path / "polling"
    marker.touch()
    async with PollingRequest(marker) as request:
        request.start_monitoring()
        assert not marker.exists()
        assert await request.post(POLL_URL, None) == []
        assert time.time() - marker.stat().st_mtime < 5
        assert request.stalled_for < 5


@pytest.mark.parametrize("transport_error", [False, True])
async def test_network_failure_is_progress_but_not_healthy(
    tmp_path, monkeypatch, transport_error
):
    def fail(request):
        if transport_error:
            raise httpx.ConnectError("offline")
        return httpx.Response(502, json={"ok": False, "description": "Bad Gateway"})

    mock_telegram(monkeypatch, fail)
    marker = tmp_path / "polling"
    async with PollingRequest(marker) as request:
        request.start_monitoring()
        request.last_completed_at -= 600
        with pytest.raises(NetworkError):
            await request.post(POLL_URL, None)
        assert request.stalled_for < 5
        assert not marker.exists()


async def test_hung_request_times_out_and_next_poll_recovers(tmp_path, monkeypatch):
    calls = 0
    cancelled = asyncio.Event()

    async def respond(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return httpx.Response(200, json={"ok": True, "result": []})

    mock_telegram(monkeypatch, respond)
    monkeypatch.setattr(polling, "POLLING_REQUEST_TIMEOUT", 0.02)
    marker = tmp_path / "polling"
    async with PollingRequest(marker) as request:
        request.start_monitoring()
        with pytest.raises(TimedOut):
            await asyncio.wait_for(request.post(POLL_URL, None), timeout=1)
        assert cancelled.is_set()
        assert not marker.exists()
        assert await request.post(POLL_URL, None) == []
        assert marker.exists()


@pytest.mark.parametrize(
    "stalled, running", [(False, True), (True, True), (True, False)]
)
async def test_watchdog_restarts_stuck_polling_but_respects_shutdown(
    tmp_path, monkeypatch, stalled, running
):
    settings = Settings(
        bot_token="123:fake", api_key="k", api_secret="s", data_dir=tmp_path
    )
    async with PollingRequest(settings.polling_heartbeat_path) as request:
        request.start_monitoring()
        if stalled:
            request.last_completed_at -= POLLING_STALL_SECONDS + 1
        application = SimpleNamespace(
            running=True,
            updater=SimpleNamespace(running=running),
            bot_data={"settings": settings, "polling_request": request},
        )

        def exit_process(code):
            raise SystemExit(code)

        monkeypatch.setattr(app.os, "_exit", exit_process)
        context = SimpleNamespace(application=application)
        if stalled and running:
            with pytest.raises(SystemExit) as exc:
                await app._heartbeat_job(context)
            assert exc.value.code == 1
            assert not settings.heartbeat_path.exists()
        else:
            await app._heartbeat_job(context)
            assert settings.heartbeat_path.exists()


@pytest.mark.parametrize("telegram_proxy", ["", "http://proxy.test:3128"])
def test_real_startup_answers_an_update_that_was_waiting_before_restart(
    tmp_path, monkeypatch, telegram_proxy
):
    """Exercise run_polling, startup, the real /help handler and shutdown offline.

    The fake Telegram server implements drop_pending_updates, so restoring
    that flag loses the waiting command and this test fails on the missing reply.
    """
    pending = [{
        "update_id": 100,
        "message": {
            "message_id": 10,
            "date": int(time.time()),
            "chat": {"id": 500, "type": "private"},
            "from": {"id": 500, "is_bot": False, "first_name": "Tester"},
            "text": "/help",
            "entities": [{"type": "bot_command", "offset": 0, "length": 5}],
        },
    }]
    replies = []
    application = None

    async def respond(request):
        method = request.url.path.rsplit("/", 1)[-1]
        params = parse_qs(request.content.decode())
        result = True
        if method == "getMe":
            result = {
                "id": 123, "is_bot": True, "first_name": "Bot", "username": "test_bot"
            }
        elif method == "deleteWebhook":
            if params.get("drop_pending_updates") == ["true"]:
                pending.clear()
        elif method == "getUpdates":
            offset = int(params.get("offset", [0])[0])
            pending[:] = [item for item in pending if item["update_id"] >= offset]
            result = list(pending)
            await asyncio.sleep(0.01)
        elif method == "sendMessage":
            replies.append(params["text"][0])
            result = {
                "message_id": 11,
                "date": int(time.time()),
                "chat": {"id": 500, "type": "private"},
                "text": replies[-1],
            }
            application.stop_running()
        return httpx.Response(200, json={"ok": True, "result": result})

    mock_telegram(monkeypatch, respond)
    settings = Settings(
        bot_token="123:fake", api_key="k", api_secret="s", data_dir=tmp_path,
        work_dir=tmp_path / "work", asr_enabled=False, telegram_proxy=telegram_proxy,
    )
    monkeypatch.setattr(app, "load_settings", lambda: settings)
    monkeypatch.setattr(app, "configure_logging", lambda: None)
    monkeypatch.setattr(app, "add_file_logging", lambda settings: None)
    monkeypatch.setattr(app, "ensure_ffmpeg_available", lambda: None)
    build = app.build_application
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def build_and_bound_shutdown(settings):
        nonlocal application
        application = build(settings)
        loop.call_later(3, application.stop_running)
        return application

    monkeypatch.setattr(app, "build_application", build_and_bound_shutdown)
    previous_umask = os.umask(0o077)
    try:
        app.run()
    finally:
        os.umask(previous_umask)
        if not loop.is_closed():
            loop.close()
        asyncio.set_event_loop(None)
    assert len(replies) == 1
    assert settings.polling_heartbeat_path.exists()
    assert not pending
