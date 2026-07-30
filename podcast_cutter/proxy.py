"""An optional second egress route for episode audio.

Some hosts are unreachable from where the bot runs, and the reason is *where
the request comes from* rather than anything about the request. Two distinct
shapes of that, both measured against the live directory:

* ``traffic.megaphone.fm`` resolves, by GeoDNS, to an address that silently
  drops our packets — a ~9 second connect timeout, no HTTP refusal. Most of the
  directory is fronted by prefixes that redirect there, so this one host was
  17.5% of all episodes.
* Anchor (Spotify's free host) and a few others answer 403 to this address
  outright. Another 5%.

Fetching the same URLs from a second host succeeds, so audio — and only audio;
the directory API and Telegram are not blocked and have no business travelling
through another machine — can be routed through a proxy there.

The whole feature is behind ``MEDIA_PROXY``. Unset, every function here reports
"direct" and nothing changes. Configured, this module owns two decisions:

* **Which route to try, in what order.** In the default ``fallback`` mode the
  direct route goes first, so the ~78% of episodes that already work keep their
  existing path and latency, and the proxy is only consulted after a failure
  that looks like a blocked egress.
* **When to stop trusting the proxy.** A dead proxy must never cost more than
  the episodes it would have rescued, so a transport-level failure marks it
  down for a cooldown and everything falls back to direct until it recovers.
"""

from __future__ import annotations

import logging
import os
import time

import httpx

from .config import PROXY_MODE_ALWAYS, Settings

logger = logging.getLogger(__name__)

#: The two routes an audio fetch can take.
DIRECT = "direct"
PROXY = "proxy"

#: Statuses that mean "this address is unwelcome". Retrying cannot fix them,
#: but coming from somewhere else can.
BLOCKED_STATUSES = frozenset({401, 403, 451})

#: Proxy variables ffmpeg and libcurl-alikes pick up from the environment.
#: Stripped from the environment of a direct attempt so "direct" means direct
#: even if the container inherits one of them from somewhere else.
_PROXY_ENV_VARS = frozenset(
    {
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "ftp_proxy",
        "no_proxy",
    }
)


def is_blocked_status(status_code: int) -> bool:
    """Whether the host refused us because of who we appear to be."""
    return status_code in BLOCKED_STATUSES


def is_routing_failure(exc: BaseException) -> bool:
    """Whether a failure is plausibly about the route rather than the request.

    Everything httpx raises below the HTTP layer qualifies: connect timeouts
    (megaphone's silent drop), read timeouts on a stalled connection, proxy
    errors, resets. A response we could read and dislike does not — that is
    handled by status code instead.
    """
    return isinstance(exc, httpx.TransportError)


class MediaProxy:
    """Route selection and a circuit breaker around one proxy URL.

    One instance is shared by the whole bot, because the breaker state is the
    point: the first cut that discovers a dead proxy must spare every cut after
    it the same wait.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._url = settings.media_proxy if settings.proxy_enabled else ""
        #: ``monotonic()`` value before which the proxy is considered dead.
        self._down_until = 0.0
        self._down_reason = ""

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def configured(self) -> bool:
        """Whether a proxy is set up and not switched off."""
        return bool(self._url)

    @property
    def url(self) -> str | None:
        return self._url or None

    @property
    def available(self) -> bool:
        """Whether the proxy may be used right now."""
        return bool(self._url) and time.monotonic() >= self._down_until

    @property
    def down_reason(self) -> str:
        return self._down_reason if not self.available else ""

    def mark_down(self, reason: str) -> None:
        """Stop using the proxy for the cooldown window.

        Loud on purpose: a silently broken detour is worse than no detour,
        because the 22% it was added for goes back to failing with the errors
        it failed with before and nothing says why.
        """
        if not self._url:
            return
        first = self.available
        self._down_until = time.monotonic() + self._settings.media_proxy_cooldown
        self._down_reason = reason
        log = logger.warning if first else logger.info
        log(
            "Media proxy %s is unusable (%s). Falling back to direct fetches "
            "for %.0fs.",
            self._url,
            reason,
            self._settings.media_proxy_cooldown,
        )

    def mark_up(self) -> None:
        """Note that the proxy answered."""
        if not self._url:
            return
        if self._down_until:
            logger.info("Media proxy %s is working again.", self._url)
        self._down_until = 0.0
        self._down_reason = ""

    # ------------------------------------------------------------------
    # Route selection
    # ------------------------------------------------------------------

    def routes(self) -> tuple[str, ...]:
        """Routes to try for one fetch, best first."""
        if not self.available:
            return (DIRECT,)
        if self._settings.media_proxy_mode == PROXY_MODE_ALWAYS:
            return (PROXY, DIRECT)
        return (DIRECT, PROXY)

    def alternatives(self, route: str) -> tuple[str, ...]:
        """Routes still worth trying once ``route`` has failed."""
        return tuple(candidate for candidate in self.routes() if candidate != route)

    def is_last_resort(self, route: str) -> bool:
        """Whether nothing comes *after* ``route`` in the order.

        Deliberately about position rather than about which other routes exist:
        in fallback mode the proxy has the direct route as an alternative — it
        is just an alternative that already failed — so "is there anything left
        to try" and "is anything queued behind me" are different questions. A
        route we would not use at all counts as a last resort, so it gets the
        patient timeout rather than the impatient one.
        """
        routes = self.routes()
        return route not in routes or routes[-1] == route

    # ------------------------------------------------------------------
    # Applying a route
    # ------------------------------------------------------------------

    def httpx_proxy(self, route: str) -> str | None:
        """Value for ``httpx.AsyncClient(proxy=...)``."""
        return self._url if route == PROXY and self._url else None

    def subprocess_env(self, route: str) -> dict[str, str] | None:
        """Environment for an ffmpeg/ffprobe call on ``route``.

        ``http_proxy`` and nothing else: ffmpeg honours it for ``https://``
        sources, which it fetches by CONNECTing through the proxy, and ignores
        ``https_proxy`` completely. Both halves of that were verified against a
        logging proxy, and getting it backwards looks exactly like a proxy that
        is not being used.

        Returns ``None`` when the feature is off, so the child simply inherits
        our environment the way it always did.
        """
        if not self._url:
            return None
        env = {
            key: value
            for key, value in os.environ.items()
            if key.lower() not in _PROXY_ENV_VARS
        }
        if route == PROXY:
            env["http_proxy"] = self._url
        return env

    def connect_timeout(self, route: str, default: float) -> float:
        """Connect timeout to allow ``route``.

        An attempt with another route behind it gets a short one: the failure
        being routed around is a silent packet drop, and waiting out the full
        timeout before the detour would hand the user most of that delay for
        nothing. The last resort keeps the normal timeout — by then a slow host
        is worth waiting for.
        """
        if self.is_last_resort(route):
            return default
        return min(default, self._settings.media_proxy_connect_timeout)

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def check(self) -> bool:
        """Fetch the probe URL through the proxy; update the breaker.

        Any HTTP answer counts as success — the question is whether the proxy
        forwards traffic, not what the far end thinks of the request. Never
        raises: a failed check is a fact to record, not a reason to refuse to
        start.
        """
        if not self._url:
            return False

        probe_url = self._settings.media_proxy_probe_url
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(
                proxy=self._url,
                follow_redirects=True,
                timeout=httpx.Timeout(20.0, connect=10.0),
            ) as client:
                response = await client.get(
                    probe_url, headers={"Range": "bytes=0-1"}
                )
        except Exception as exc:  # noqa: BLE001 - a broken proxy is data
            self.mark_down(f"{type(exc).__name__}: {exc}")
            return False

        self.mark_up()
        logger.info(
            "Media proxy %s reached %s (%d) in %d ms; mode=%s.",
            self._url,
            probe_url,
            response.status_code,
            int((time.monotonic() - started) * 1000),
            self._settings.media_proxy_mode,
        )
        return True


__all__ = [
    "BLOCKED_STATUSES",
    "DIRECT",
    "PROXY",
    "MediaProxy",
    "is_blocked_status",
    "is_routing_failure",
]
