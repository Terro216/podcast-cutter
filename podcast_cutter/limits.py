"""Per-user budgets on what comes *in*.

PTB's ``AIORateLimiter`` bounds outgoing Telegram calls, which protects the
bot's token from flood limits — and does nothing about a person (or a chat
full of people following one `src_` link) pressing the expensive buttons as
fast as Telegram will deliver them. Searches hit the Podcast Index key, cuts
hold an ffmpeg slot for seconds, and a first search on an episode holds a CPU
core for minutes; each deserves its own ceiling, per user, because "the bot
was busy" must never depend on how pushy somebody else's afternoon was.

Sliding window rather than a refilling bucket: the question a refusal answers
is "how many did you do in the last hour", and a window is that question
verbatim, cheap enough at these sizes not to warrant an approximation.
"""

from __future__ import annotations

import time
from collections import deque


class Budget:
    """At most ``count`` events per ``window`` seconds, per integer key.

    ``count=0`` means the budget is off — every request is allowed and
    nothing is recorded. That is the rollback: one variable back to the
    previous behaviour.
    """

    def __init__(self, count: int, window_seconds: float) -> None:
        self.count = count
        self.window = window_seconds
        self._events: dict[int, deque[float]] = {}

    def allow(self, key: int, now: float | None = None) -> bool:
        """Record one event against ``key`` iff it fits the budget."""
        if self.count <= 0:
            return True
        moment = time.monotonic() if now is None else now
        events = self._events.setdefault(key, deque())
        horizon = moment - self.window
        while events and events[0] <= horizon:
            events.popleft()
        if len(events) >= self.count:
            return False
        events.append(moment)
        return True
