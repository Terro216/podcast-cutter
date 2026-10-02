"""Observe the actual Telegram poll, independently of the event-loop heartbeat."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from telegram.error import TimedOut
from telegram.request import HTTPXRequest

logger = logging.getLogger(__name__)

# A normal long poll takes at most 10 seconds plus network timeouts. Bound the
# whole request too, including time spent waiting inside the HTTP connection
# pool. The watchdog covers a stuck cancellation or a polling task that died.
POLLING_REQUEST_TIMEOUT = 45.0
POLLING_STALL_SECONDS = 180.0


class PollingRequest(HTTPXRequest):
    """PTB's dedicated getUpdates transport with progress and success markers.

    Completed failures count as progress: PTB is still retrying a network
    outage. Only successful polls renew the health marker, including empty
    polls, so an idle bot stays healthy without hiding a broken Telegram path.
    """

    def __init__(self, heartbeat_path: Path, proxy: str = "") -> None:
        super().__init__(connection_pool_size=1, proxy=proxy or None)
        self.heartbeat_path = heartbeat_path
        self.last_completed_at: float | None = None

    def start_monitoring(self) -> None:
        # Arm after startup work, before polling begins. A previous process's
        # successful poll must not make this process look ready.
        self.last_completed_at = time.monotonic()
        try:
            self.heartbeat_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not clear the previous polling heartbeat: %s", exc)

    @property
    def stalled_for(self) -> float:
        if self.last_completed_at is None:
            return 0.0
        return time.monotonic() - self.last_completed_at

    async def do_request(self, *args, **kwargs) -> tuple[int, bytes]:
        try:
            status, payload = await asyncio.wait_for(
                super().do_request(*args, **kwargs), timeout=POLLING_REQUEST_TIMEOUT
            )
        except asyncio.TimeoutError as exc:
            logger.warning(
                "Telegram polling exceeded %.0fs; retrying", POLLING_REQUEST_TIMEOUT
            )
            raise TimedOut("Telegram polling exceeded its total timeout") from exc
        finally:
            self.last_completed_at = time.monotonic()

        if status == 200:
            try:
                self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
                self.heartbeat_path.write_text(str(time.time()))
            except OSError as exc:
                logger.warning("Could not write polling heartbeat: %s", exc)
        return status, payload
