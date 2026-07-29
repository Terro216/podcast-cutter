"""Async client for the Podcast Index API.

Design notes
------------
* One :class:`httpx.AsyncClient` for the whole process, so connections are
  reused and every request inherits a timeout. The old code created a fresh
  ``requests`` call per query with no timeout at all, which could wedge a
  handler indefinitely.
* Responses are parsed into frozen dataclasses. Entries missing the fields we
  actually need (an id, a playable enclosure) are dropped at the boundary
  rather than blowing up with a ``KeyError`` three call frames later.
* Transient failures (timeouts, connection resets, 5xx) are retried with
  backoff; 4xx are not, since retrying them cannot help.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Settings
from .errors import ApiError, NotFoundError
from .text import one_line

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 0.5

#: Enclosure MIME types we know ffmpeg can handle. Anything video-shaped is
#: skipped: cutting it would produce a file we cannot send as audio.
_SKIPPED_ENCLOSURE_PREFIXES = ("video/",)


@dataclass(frozen=True, slots=True)
class Feed:
    """A podcast."""

    id: str
    title: str
    author: str

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Feed | None:
        feed_id = raw.get("id")
        if feed_id is None:
            return None
        return cls(
            id=str(feed_id),
            title=one_line(raw.get("title"), "Untitled podcast"),
            author=one_line(raw.get("author"), "Unknown author"),
        )


@dataclass(frozen=True, slots=True)
class Episode:
    """A single episode with a playable audio enclosure."""

    id: str
    title: str
    feed_title: str
    enclosure_url: str
    duration: int | None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Episode | None:
        episode_id = raw.get("id")
        url = (raw.get("enclosureUrl") or "").strip()
        # An episode with no audio URL cannot be cut, so never offer it.
        if episode_id is None or not url.startswith(("http://", "https://")):
            return None

        enclosure_type = (raw.get("enclosureType") or "").lower()
        if enclosure_type.startswith(_SKIPPED_ENCLOSURE_PREFIXES):
            return None

        raw_duration = raw.get("duration")
        duration = int(raw_duration) if isinstance(raw_duration, int) else None
        if duration is not None and duration <= 0:
            duration = None

        return cls(
            id=str(episode_id),
            title=one_line(raw.get("title"), "Untitled episode"),
            feed_title=one_line(raw.get("feedTitle"), "Podcast"),
            enclosure_url=url,
            duration=duration,
        )


def _parse_all(model: type, items: Sequence[dict[str, Any]] | None) -> list:
    """Parse a list of API entries, silently dropping unusable ones."""
    parsed = [model.from_api(item) for item in items or [] if isinstance(item, dict)]
    return [item for item in parsed if item is not None]


class PodcastIndexClient:
    """Thin async wrapper over the endpoints this bot actually uses."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.api_base_url,
            timeout=httpx.Timeout(settings.api_timeout),
            follow_redirects=True,
            headers={"User-Agent": "PodcastCutter/1.0"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- plumbing ----------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        header_time = str(int(time.time()))
        signature = hashlib.sha1(
            (self._settings.api_key + self._settings.api_secret + header_time).encode()
        ).hexdigest()
        return {
            "X-Auth-Key": self._settings.api_key,
            "X-Auth-Date": header_time,
            "Authorization": signature,
        }

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await self._client.get(
                    path, params=params, headers=self._auth_headers()
                )
            except httpx.HTTPError as exc:
                last_error = exc
                logger.warning(
                    "Podcast Index request %s failed (attempt %d/%d): %s",
                    path,
                    attempt,
                    _MAX_ATTEMPTS,
                    exc,
                )
            else:
                if response.status_code < 400:
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        raise ApiError(
                            "The podcast directory returned a malformed response."
                        ) from exc
                    if not isinstance(payload, dict):
                        raise ApiError(
                            "The podcast directory returned a malformed response."
                        )
                    return payload

                if response.status_code in (401, 403):
                    # Bad credentials are a deployment problem, not a user one.
                    logger.error(
                        "Podcast Index rejected our credentials (%s) for %s",
                        response.status_code,
                        path,
                    )
                    raise ApiError(
                        "The bot cannot authenticate with the podcast directory."
                    )

                if response.status_code == 429:
                    last_error = ApiError("Rate limited by the podcast directory.")
                elif response.status_code >= 500:
                    last_error = ApiError(
                        f"The podcast directory returned {response.status_code}."
                    )
                else:
                    raise ApiError(
                        f"The podcast directory returned {response.status_code}."
                    )

                logger.warning(
                    "Podcast Index %s returned %s (attempt %d/%d)",
                    path,
                    response.status_code,
                    attempt,
                    _MAX_ATTEMPTS,
                )

            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(_BACKOFF_BASE * 2 ** (attempt - 1))

        raise ApiError(
            "The podcast directory is not responding. Please try again in a moment."
        ) from last_error

    # -- endpoints ---------------------------------------------------------

    async def search_feeds(
        self, query: str, page: int = 1
    ) -> tuple[list[Feed], bool]:
        """Search podcasts by term.

        The upstream endpoint has no offset parameter, only ``max``. So we ask
        for one item beyond the end of the requested page and slice locally;
        the extra item tells us whether a next page exists.
        """
        page = max(1, page)
        per_page = self._settings.podcasts_per_page
        payload = await self._get(
            "/search/byterm", {"q": query, "max": per_page * page + 1}
        )

        feeds = _parse_all(Feed, payload.get("feeds"))
        if not feeds:
            raise NotFoundError(f"No podcasts found for “{one_line(query)}”.")

        start = (page - 1) * per_page
        window = feeds[start : start + per_page]
        if not window:
            # The user paged past the end (e.g. results shrank between calls).
            raise NotFoundError("No more podcasts on that page.")

        has_next = len(feeds) > page * per_page
        return window, has_next

    async def list_episodes(self, feed_id: str) -> list[Episode]:
        """All recent episodes of a feed, newest first."""
        payload = await self._get(
            "/episodes/byfeedid",
            {
                "id": feed_id,
                "max": self._settings.max_episodes_per_session,
            },
        )

        episodes = _parse_all(Episode, payload.get("items"))
        if not episodes:
            raise NotFoundError("This podcast has no downloadable episodes.")
        return episodes

    async def search_episodes_by_person(self, query: str) -> list[Episode]:
        """Episodes across all podcasts mentioning a person or keyword."""
        payload = await self._get(
            "/search/byperson",
            {"q": query, "max": self._settings.max_episodes_per_session},
        )

        episodes = _parse_all(Episode, payload.get("items"))
        if not episodes:
            raise NotFoundError(f"No episodes found for “{one_line(query)}”.")
        return episodes

    async def get_episode(self, episode_id: str) -> Episode:
        """One episode by id, for deep links and inline-mode hand-offs.

        The caller may not have the episode cached — a shared link can be
        opened by someone who never searched for it.
        """
        payload = await self._get("/episodes/byid", {"id": episode_id})

        raw = payload.get("episode")
        episode = Episode.from_api(raw) if isinstance(raw, dict) else None
        if episode is None:
            raise NotFoundError("That episode is no longer available.")
        return episode

    async def trending_feeds(self, limit: int = 10) -> list[Feed]:
        payload = await self._get("/podcasts/trending", {"max": limit})

        feeds = _parse_all(Feed, payload.get("feeds"))
        if not feeds:
            raise NotFoundError("No trending podcasts right now.")
        return feeds

    async def random_episode(self) -> Episode:
        """One random episode that actually has audio attached.

        The endpoint occasionally returns entries without an enclosure, so ask
        for a handful and take the first usable one instead of failing.
        """
        payload = await self._get("/episodes/random", {"max": 10})

        episodes = _parse_all(Episode, payload.get("episodes"))
        if not episodes:
            raise NotFoundError("Could not find a random episode. Try again.")
        return episodes[0]
