"""Durable storage: an event journal and the per-user recent list.

Why SQLite rather than ``PicklePersistence``:

* The session is a working set that changes shape as the bot evolves. Pickling
  it would tie on-disk data to the current class layout, so adding a field to
  ``Episode`` would make old records deserialise into objects missing it — an
  ``AttributeError`` in production, at request time.
* PTB loads its persistence file wholesale at startup, so one corrupt record
  stops the bot from starting. Here a bad row breaks a row.

Only what outlives a session is stored: what happened (the journal, which is
what tells you whether the bot actually works) and the recent-episode list.

Every write is best-effort. Losing a statistics row must never cost a user
their clip, so failures are logged and swallowed.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .api import Episode

logger = logging.getLogger(__name__)

#: Bumped when the schema changes; see ``_migrate``.
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    at            REAL    NOT NULL,
    user_id       INTEGER,
    action        TEXT    NOT NULL,
    outcome       TEXT,
    episode_id    TEXT,
    feed_title    TEXT,
    episode_title TEXT,
    start_s       INTEGER,
    length_s      INTEGER,
    as_voice      INTEGER,
    size_bytes    INTEGER,
    ms            INTEGER,
    detail        TEXT
);
CREATE INDEX IF NOT EXISTS events_at ON events (at);
CREATE INDEX IF NOT EXISTS events_action_at ON events (action, at);

CREATE TABLE IF NOT EXISTS recents (
    user_id       INTEGER NOT NULL,
    episode_id    TEXT    NOT NULL,
    title         TEXT    NOT NULL,
    feed_title    TEXT    NOT NULL,
    enclosure_url TEXT    NOT NULL,
    duration      INTEGER,
    at            REAL    NOT NULL,
    PRIMARY KEY (user_id, episode_id)
);
CREATE INDEX IF NOT EXISTS recents_user_at ON recents (user_id, at DESC);
"""


@dataclass(frozen=True, slots=True)
class Event:
    """One thing that happened, as recorded in the journal."""

    action: str
    user_id: int | None = None
    outcome: str | None = None
    episode_id: str | None = None
    feed_title: str | None = None
    episode_title: str | None = None
    start_s: int | None = None
    length_s: int | None = None
    as_voice: bool | None = None
    size_bytes: int | None = None
    ms: int | None = None
    detail: str | None = None


@dataclass
class Stats:
    """Aggregates over a window, for the ``/stats`` panel."""

    window_hours: int
    cuts_ok: int = 0
    cuts_failed: int = 0
    unique_users: int = 0
    voice_share: float = 0.0
    median_ms: int | None = None
    slowest_ms: int | None = None
    total_bytes: int = 0
    top_podcasts: list[tuple[str, int]] = field(default_factory=list)
    failures: list[tuple[str, int]] = field(default_factory=list)
    actions: list[tuple[str, int]] = field(default_factory=list)

    @property
    def cuts_total(self) -> int:
        return self.cuts_ok + self.cuts_failed

    @property
    def success_rate(self) -> float | None:
        return self.cuts_ok / self.cuts_total if self.cuts_total else None


class Store:
    """Thin async wrapper over a single SQLite file.

    Volumes are low — a handful of writes per user action — so one connection
    guarded by a lock, with the blocking calls pushed to a worker thread, is
    both sufficient and much simpler than an async driver.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._connection: sqlite3.Connection | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path, check_same_thread=False, timeout=10.0
        )
        connection.row_factory = sqlite3.Row
        # WAL survives an unclean shutdown and lets reads proceed during writes.
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.executescript(_SCHEMA)
        self._migrate(connection)
        connection.commit()
        self._connection = connection
        logger.info("Store ready at %s", self.path)

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        """Bring an existing file up to :data:`SCHEMA_VERSION`.

        Nothing to do yet — the tables are created ``IF NOT EXISTS``. The hook
        exists so the first real migration has an obvious home.
        """
        current = connection.execute("PRAGMA user_version").fetchone()[0]
        if current == SCHEMA_VERSION:
            return
        if current > SCHEMA_VERSION:
            logger.warning(
                "Database schema is version %s, newer than this build's %s",
                current,
                SCHEMA_VERSION,
            )
            return
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    async def aclose(self) -> None:
        await asyncio.to_thread(self.close)

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------

    def _execute(self, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
        if self._connection is None:
            raise RuntimeError("Store is not connected")
        with self._lock:
            cursor = self._connection.execute(sql, tuple(params))
            rows = cursor.fetchall()
            self._connection.commit()
            return rows

    async def _run(self, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
        return await asyncio.to_thread(self._execute, sql, params)

    # ------------------------------------------------------------------
    # Journal
    # ------------------------------------------------------------------

    async def record(self, event: Event) -> None:
        """Append to the journal. Never raises."""
        try:
            await self._run(
                """
                INSERT INTO events (
                    at, user_id, action, outcome, episode_id, feed_title,
                    episode_title, start_s, length_s, as_voice, size_bytes,
                    ms, detail
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    time.time(),
                    event.user_id,
                    event.action,
                    event.outcome,
                    event.episode_id,
                    _clip(event.feed_title),
                    _clip(event.episode_title),
                    event.start_s,
                    event.length_s,
                    None if event.as_voice is None else int(event.as_voice),
                    event.size_bytes,
                    event.ms,
                    _clip(event.detail, 500),
                ),
            )
        except Exception:
            # Statistics are never worth failing a user's request over.
            logger.exception("Could not record event %s", event.action)

    async def purge(self, older_than_days: int) -> int:
        """Drop journal rows past the retention window. 0 disables purging."""
        if older_than_days <= 0:
            return 0
        cutoff = time.time() - older_than_days * 86400
        try:
            rows = await self._run(
                "DELETE FROM events WHERE at < ? RETURNING id", (cutoff,)
            )
        except sqlite3.OperationalError:
            # RETURNING needs SQLite 3.35; fall back to counting first.
            counted = await self._run(
                "SELECT count(*) AS n FROM events WHERE at < ?", (cutoff,)
            )
            await self._run("DELETE FROM events WHERE at < ?", (cutoff,))
            return int(counted[0]["n"])
        return len(rows)

    # ------------------------------------------------------------------
    # Recent episodes
    # ------------------------------------------------------------------

    async def remember_recent(self, user_id: int, episode: Episode) -> None:
        """Record that this user opened this episode. Never raises."""
        try:
            await self._run(
                """
                INSERT INTO recents (
                    user_id, episode_id, title, feed_title, enclosure_url,
                    duration, at
                ) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT (user_id, episode_id) DO UPDATE SET at = excluded.at
                """,
                (
                    user_id,
                    episode.id,
                    _clip(episode.title),
                    _clip(episode.feed_title),
                    episode.enclosure_url,
                    episode.duration,
                    time.time(),
                ),
            )
        except Exception:
            logger.exception("Could not save a recent episode")

    async def recent_episodes(self, user_id: int, limit: int) -> list[Episode]:
        """Most recently opened first. Returns an empty list on any problem."""
        try:
            rows = await self._run(
                """
                SELECT episode_id, title, feed_title, enclosure_url, duration
                FROM recents WHERE user_id = ? ORDER BY at DESC LIMIT ?
                """,
                (user_id, limit),
            )
        except Exception:
            logger.exception("Could not read recent episodes")
            return []

        return [
            Episode(
                id=row["episode_id"],
                title=row["title"],
                feed_title=row["feed_title"],
                enclosure_url=row["enclosure_url"],
                duration=row["duration"],
            )
            for row in rows
        ]

    async def trim_recents(self, user_id: int, keep: int) -> None:
        """Keep only the newest ``keep`` rows for a user."""
        try:
            await self._run(
                """
                DELETE FROM recents
                WHERE user_id = ? AND episode_id NOT IN (
                    SELECT episode_id FROM recents WHERE user_id = ?
                    ORDER BY at DESC LIMIT ?
                )
                """,
                (user_id, user_id, keep),
            )
        except Exception:
            logger.exception("Could not trim the recent list")

    # ------------------------------------------------------------------
    # Aggregates
    # ------------------------------------------------------------------

    async def stats(self, window_hours: int = 24) -> Stats:
        since = time.time() - window_hours * 3600
        result = Stats(window_hours=window_hours)

        cuts = await self._run(
            """
            SELECT outcome, as_voice, ms, size_bytes, feed_title
            FROM events WHERE action = 'cut' AND at >= ?
            """,
            (since,),
        )
        result.cuts_ok = sum(1 for row in cuts if row["outcome"] == "ok")
        result.cuts_failed = sum(1 for row in cuts if row["outcome"] != "ok")

        successful = [row for row in cuts if row["outcome"] == "ok"]
        if successful:
            durations = sorted(
                row["ms"] for row in successful if row["ms"] is not None
            )
            if durations:
                result.median_ms = durations[len(durations) // 2]
                result.slowest_ms = durations[-1]
            result.total_bytes = sum(
                row["size_bytes"] or 0 for row in successful
            )
            voiced = sum(1 for row in successful if row["as_voice"])
            result.voice_share = voiced / len(successful)

        result.top_podcasts = _top(
            (row["feed_title"] for row in successful if row["feed_title"]), 5
        )
        result.failures = _top(
            (
                row["outcome"]
                for row in cuts
                if row["outcome"] and row["outcome"] != "ok"
            ),
            5,
        )

        users = await self._run(
            "SELECT count(DISTINCT user_id) AS n FROM events WHERE at >= ?",
            (since,),
        )
        result.unique_users = int(users[0]["n"]) if users else 0

        actions = await self._run(
            """
            SELECT action, count(*) AS n FROM events WHERE at >= ?
            GROUP BY action ORDER BY n DESC
            """,
            (since,),
        )
        result.actions = [(row["action"], int(row["n"])) for row in actions]

        return result

    async def size_on_disk(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0


def _clip(value: str | None, limit: int = 200) -> str | None:
    if value is None:
        return None
    return value[:limit]


def _top(values: Iterable[str], limit: int) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
