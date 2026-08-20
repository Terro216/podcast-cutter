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
import contextlib
import json
import logging
import sqlite3 as stdlib_sqlite3
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from sqlcipher3 import dbapi2 as sqlite3

from .api import Episode
from .database import key_connection
from .transcripts import (
    Moment,
    TranscriptBuild,
    Utterance,
    Word,
    is_indexable,
    lemmatize,
    normalize,
    normalizer_identity,
)

logger = logging.getLogger(__name__)

#: Bumped when the schema changes; see ``_migrate``.
SCHEMA_VERSION = 5

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

-- The user's chosen interface language. A row exists only after an explicit
-- choice: auto-detection from Telegram's language_code is deliberately not
-- written down, so it keeps following the client until the user decides.
CREATE TABLE IF NOT EXISTS users (
    user_id           INTEGER PRIMARY KEY,
    language          TEXT    NOT NULL,
    at                REAL    NOT NULL,
    terms_version     TEXT,
    terms_accepted_at REAL
);

CREATE TABLE IF NOT EXISTS recents (
    user_id       INTEGER NOT NULL,
    episode_id    TEXT    NOT NULL,
    title         TEXT    NOT NULL,
    feed_title    TEXT    NOT NULL,
    enclosure_url TEXT    NOT NULL,
    duration      INTEGER,
    feed_id       TEXT    NOT NULL DEFAULT '',
    author        TEXT    NOT NULL DEFAULT '',
    episode_url   TEXT    NOT NULL DEFAULT '',
    at            REAL    NOT NULL,
    PRIMARY KEY (user_id, episode_id)
);
CREATE INDEX IF NOT EXISTS recents_user_at ON recents (user_id, at DESC);

-- The first-listen queue.
--
-- Transcription is minutes of CPU, so a restart that forgets who was waiting
-- throws away work nobody can see was lost. A row here is one *waiter*: the
-- queue itself is of episodes, and everyone waiting on the same one is served
-- by a single job, which is why `episode_id` is not unique.
--
-- `chat_id` is what makes a resumed job deliverable. A session does not
-- survive a restart on purpose (no PicklePersistence — see HANDOFF §4), so a
-- job that outlives the coroutine that asked for it finishes the transcript
-- and says so in the chat; the search that follows is instant.
--
-- Note for whoever changes this table's shape: it is *not* in
-- `_DERIVED_TABLES`, so the rebuild path in `_migrate` will not recreate it.
-- It holds pending work rather than derived data, and dropping it would lose
-- exactly what it exists to protect.
CREATE TABLE IF NOT EXISTS asr_jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id    TEXT    NOT NULL,
    audio_url     TEXT    NOT NULL,
    episode_title TEXT,
    feed_title    TEXT,
    feed_id       TEXT    NOT NULL DEFAULT '',
    user_id       INTEGER NOT NULL,
    chat_id       INTEGER NOT NULL,
    -- queued → running → done | failed | abandoned
    state         TEXT    NOT NULL DEFAULT 'queued',
    -- Counted so a job that kills the bot cannot be retried forever: a crash
    -- loop that re-downloads an episode every boot is worse than one lost job.
    attempts      INTEGER NOT NULL DEFAULT 0,
    at            REAL    NOT NULL,
    started_at    REAL,
    finished_at   REAL,
    outcome       TEXT
);
CREATE INDEX IF NOT EXISTS asr_jobs_state ON asr_jobs (state, id);

-- Transcripts.
--
-- Keyed on the SHA-256 of the bytes actually fetched, not on the episode id.
-- Podcasts insert advertisements dynamically, so the same episode can serve
-- different audio next month while keeping its id; timestamps taken against
-- the old bytes would then cut an advert or the middle of a sentence. When the
-- hash does not match, the transcript is not stale, it is wrong.
--
-- The recognising model and the chunker are part of the key for the same
-- reason: two rows produced by different rules are not interchangeable, and
-- keeping both is what makes a re-index a comparison rather than a leap.
CREATE TABLE IF NOT EXISTS transcripts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id      TEXT    NOT NULL,
    episode_title   TEXT,
    feed_title      TEXT,
    feed_id         TEXT    NOT NULL DEFAULT '',
    source_url      TEXT    NOT NULL,
    source_sha256   TEXT    NOT NULL,
    source_bytes    INTEGER,
    duration_s      INTEGER,
    language        TEXT,
    asr_backend     TEXT    NOT NULL,
    asr_model       TEXT    NOT NULL,
    chunker_version INTEGER NOT NULL,
    normalizer_version INTEGER NOT NULL,
    -- Which model produced the rows in window_vectors, NULL when none did.
    -- Written *and read*: search only trusts vectors whose model matches the
    -- one it would query with. The lemma column's unread version field is the
    -- cautionary tale here.
    embedding_model TEXT,
    quarantined     INTEGER NOT NULL DEFAULT 0,
    ms              INTEGER,
    at              REAL    NOT NULL,
    UNIQUE (source_sha256, asr_backend, asr_model, chunker_version)
);
CREATE INDEX IF NOT EXISTS transcripts_episode ON transcripts (episode_id, at DESC);

-- What the recogniser actually said, with word timings kept as JSON so a clip
-- can start on the word that matched rather than on a window boundary. This is
-- the source of truth; windows below are derived and disposable.
CREATE TABLE IF NOT EXISTS utterances (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    transcript_id INTEGER NOT NULL REFERENCES transcripts (id) ON DELETE CASCADE,
    start_ms      INTEGER NOT NULL,
    end_ms        INTEGER NOT NULL,
    text          TEXT    NOT NULL,
    words_json    TEXT,
    avg_logprob   REAL,
    no_speech_prob REAL,
    compression_ratio REAL,
    signals       TEXT,
    indexable     INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS utterances_transcript
    ON utterances (transcript_id, start_ms);

-- The search units: overlapping windows of about thirty seconds.
CREATE TABLE IF NOT EXISTS windows (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    transcript_id INTEGER NOT NULL REFERENCES transcripts (id) ON DELETE CASCADE,
    start_ms      INTEGER NOT NULL,
    end_ms        INTEGER NOT NULL,
    text          TEXT    NOT NULL,
    text_normalized TEXT  NOT NULL,
    text_lemmas   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS windows_transcript ON windows (transcript_id, start_ms);

-- Two indexed columns because they answer different questions. `unicode61`
-- matches a token literally and none of the built-in tokenizers know Russian,
-- so the surface form alone cannot find «нейросетей» from «нейросети» — the
-- lemma column exists for exactly that, and it is not theoretical: on a real
-- episode the recogniser wrote «нейросетей» four times and «нейросети» never.
-- The surface column stays because lemmatisation guesses, and an exact phrase
-- should not depend on a dictionary agreeing with it.
CREATE VIRTUAL TABLE IF NOT EXISTS windows_fts USING fts5 (
    text_normalized,
    text_lemmas,
    content='windows',
    content_rowid='id',
    tokenize='unicode61'
);

-- Dense vectors for the same windows, one blob of float32 per row. A plain
-- table rather than a vector index on purpose: an episode is a few hundred
-- windows, and §12's research verdict was that exact NumPy over that beats a
-- pre-v1 ANN extension. NULLs never appear — a window either has a vector
-- from the model named on its transcript row, or no row here at all, which is
-- how search knows to stay lexical for that transcript.
CREATE TABLE IF NOT EXISTS window_vectors (
    window_id INTEGER PRIMARY KEY REFERENCES windows (id) ON DELETE CASCADE,
    vector    BLOB NOT NULL
);

-- External content means FTS5 holds no copy of the text, so it has to be told
-- about every change or queries silently return rows that no longer exist.
CREATE TRIGGER IF NOT EXISTS windows_ai AFTER INSERT ON windows BEGIN
    INSERT INTO windows_fts (rowid, text_normalized, text_lemmas)
    VALUES (new.id, new.text_normalized, new.text_lemmas);
END;
CREATE TRIGGER IF NOT EXISTS windows_ad AFTER DELETE ON windows BEGIN
    INSERT INTO windows_fts (windows_fts, rowid, text_normalized, text_lemmas)
    VALUES ('delete', old.id, old.text_normalized, old.text_lemmas);
END;
CREATE TRIGGER IF NOT EXISTS windows_au AFTER UPDATE ON windows BEGIN
    INSERT INTO windows_fts (windows_fts, rowid, text_normalized, text_lemmas)
    VALUES ('delete', old.id, old.text_normalized, old.text_lemmas);
    INSERT INTO windows_fts (rowid, text_normalized, text_lemmas)
    VALUES (new.id, new.text_normalized, new.text_lemmas);
END;
"""

#: Tables that only ever held derived data, and are therefore safe to rebuild
#: rather than migrate. Ordered so a drop never leaves a dangling reference.
_DERIVED_TABLES = (
    "window_vectors",
    "windows_fts",
    "windows",
    "utterances",
    "transcripts",
)


@dataclass(frozen=True, slots=True)
class TranscriptKey:
    """What makes two transcripts the same transcript.

    The episode id is carried for lookup but is not part of the identity: the
    bytes are. An episode that re-serves itself with a different advertisement
    is different audio, and timestamps from the old one point at the wrong
    words.
    """

    episode_id: str
    source_sha256: str
    asr_backend: str
    asr_model: str
    chunker_version: int


def _fts_query(raw: str, column: str) -> str:
    """Turn what a person typed into something FTS5 will accept.

    Users type apostrophes, quotes and stray punctuation, all of which are
    operators to FTS5 — an unescaped one is a syntax error rather than a
    search. Each word becomes its own quoted term, so the query means "these
    words", and nothing a user can type is an operator.

    Scoped to one column, because the two indexed columns hold different
    renderings of the same text and matching a surface form against lemmas
    would be luck rather than search.
    """
    prepared = lemmatize(raw) if column == "text_lemmas" else normalize(raw)
    words = prepared.split()
    if not words:
        return ""
    terms = " ".join(f'"{word}"' for word in words)
    return f"{column} : ({terms})"


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


@dataclass(frozen=True, slots=True)
class AsrJob:
    """One person waiting for one episode to be listened to."""

    id: int
    episode_id: str
    audio_url: str
    episode_title: str | None
    feed_title: str | None
    feed_id: str
    user_id: int
    chat_id: int
    attempts: int


@dataclass(frozen=True, slots=True)
class AsrBatch:
    """One transcription and everyone it will answer."""

    episode_id: str
    jobs: list[AsrJob]

    @property
    def head(self) -> AsrJob:
        """The waiter whose request describes the work — they all agree on the
        episode, and the first one asked."""
        return self.jobs[0]

    @property
    def ids(self) -> list[int]:
        return [job.id for job in self.jobs]


def _as_job(row: sqlite3.Row) -> AsrJob:
    return AsrJob(
        id=int(row["id"]),
        episode_id=row["episode_id"],
        audio_url=row["audio_url"],
        episode_title=row["episode_title"],
        feed_title=row["feed_title"],
        feed_id=row["feed_id"],
        user_id=int(row["user_id"]),
        chat_id=int(row["chat_id"]),
        attempts=int(row["attempts"]),
    )


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
    #: Campaign tags from ``?start=src_…`` links, by how many distinct people
    #: arrived through each. Empty until such a link is handed out.
    sources: list[tuple[str, int]] = field(default_factory=list)

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

    def __init__(self, path: Path, *, key: str = "") -> None:
        self.path = path
        self.key = key
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
        if self.key:
            try:
                key_connection(connection, self.key)
            except Exception:
                connection.close()
                raise
        connection.row_factory = sqlite3.Row
        # SQLCipher encrypts the main DB and WAL. Keep SQLite's transient sort
        # and temp pages in RAM so it never creates an unkeyed temp database.
        connection.execute("PRAGMA temp_store=MEMORY")
        # WAL survives an unclean shutdown and lets reads proceed during writes.
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        # Off by default in SQLite, and without it the ON DELETE CASCADE that
        # ties utterances and windows to their transcript does nothing at all.
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(_SCHEMA)
        self._migrate(connection)
        connection.commit()
        self._connection = connection
        # SQLCipher creates the encrypted WAL beside the database. Keep both
        # private as defense in depth around encryption at rest.
        sidecars = (Path(f"{self.path}-wal"), Path(f"{self.path}-shm"))
        for candidate in (self.path, *sidecars):
            with contextlib.suppress(OSError):
                candidate.chmod(0o600)
        logger.info(
            "Store ready at %s (%s)",
            self.path,
            "SQLCipher" if self.key else "plaintext test mode",
        )

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        """Bring an existing file up to :data:`SCHEMA_VERSION`.

        The journal and the recent list are migrated properly if they ever
        change shape, because they hold the only things here that cannot be
        recreated. Transcripts are different: every row in them is derived from
        audio we can fetch again, so a shape change rebuilds them rather than
        rewriting them. That is the cheaper *and* the safer choice — a
        half-converted index answers questions wrongly, where a missing one
        just transcribes again.
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

        if 0 < current < SCHEMA_VERSION:
            # v5 adds legal acceptance and stable feed ids to durable rows.
            # Check first so an interrupted migration can safely be retried.
            columns = {
                table: {
                    row[1]
                    for row in connection.execute(
                        f"PRAGMA table_info({table})"
                    ).fetchall()
                }
                for table in ("users", "recents", "asr_jobs", "transcripts")
            }
            additions = {
                "users": (
                    ("terms_version", "TEXT"),
                    ("terms_accepted_at", "REAL"),
                ),
                "recents": (
                    ("feed_id", "TEXT NOT NULL DEFAULT ''"),
                    ("author", "TEXT NOT NULL DEFAULT ''"),
                    ("episode_url", "TEXT NOT NULL DEFAULT ''"),
                ),
                "asr_jobs": (
                    ("feed_id", "TEXT NOT NULL DEFAULT ''"),
                ),
                "transcripts": (
                    ("feed_id", "TEXT NOT NULL DEFAULT ''"),
                ),
            }
            for table, table_additions in additions.items():
                for name, declaration in table_additions:
                    if name not in columns[table]:
                        connection.execute(
                            f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"
                        )
            # Earlier builds journalled raw directory and transcript-search
            # phrases. They are not needed for aggregate reliability stats.
            connection.execute(
                "UPDATE events SET detail = NULL WHERE action IN "
                "('search', 'person', 'search_audio', 'inline')"
            )
            if current < 4:
                # v3 gave windows a lemma column; v4 gave them vectors and the
                # transcript row an embedding_model. Those derived shapes are
                # rebuilt. v4 -> v5 is additive and preserves transcripts.
                logger.info(
                    "Rebuilding transcript tables for schema %s", SCHEMA_VERSION
                )
                for table in _DERIVED_TABLES:
                    connection.execute(f"DROP TABLE IF EXISTS {table}")
                connection.executescript(_SCHEMA)

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
        except (sqlite3.OperationalError, stdlib_sqlite3.OperationalError):
            # RETURNING needs SQLite 3.35; fall back to counting first.
            counted = await self._run(
                "SELECT count(*) AS n FROM events WHERE at < ?", (cutoff,)
            )
            await self._run("DELETE FROM events WHERE at < ?", (cutoff,))
            return int(counted[0]["n"])
        return len(rows)

    # ------------------------------------------------------------------
    # Transcripts
    # ------------------------------------------------------------------

    async def find_transcript(self, key: TranscriptKey) -> int | None:
        """The id of a usable transcript for exactly these bytes and rules.

        Deliberately strict. A transcript made from different audio, by a
        different model, or under a different windowing rule is not a cache
        hit — it is a different answer that happens to concern the same
        episode.
        """
        return await asyncio.to_thread(self._find_transcript, key)

    async def transcript_for_episode(self, episode_id: str) -> int | None:
        """The newest transcript of an episode, whatever produced it.

        Used to answer "is this episode searchable yet" without downloading it
        to find out what its bytes hash to.
        """
        rows = await self._run(
            "SELECT id FROM transcripts WHERE episode_id = ? ORDER BY at DESC LIMIT 1",
            (episode_id,),
        )
        return int(rows[0]["id"]) if rows else None

    async def delete_transcripts_for_feeds(
        self, feed_ids: Iterable[str]
    ) -> int:
        """Remove indexed content covered by the active takedown list."""
        ids = [str(feed_id) for feed_id in feed_ids if feed_id]
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        rows = await self._run(
            f"DELETE FROM transcripts WHERE feed_id IN ({placeholders}) "
            "RETURNING id",
            ids,
        )
        return len(rows)

    async def delete_transcripts_for_episode(self, episode_id: str) -> int:
        rows = await self._run(
            "DELETE FROM transcripts WHERE episode_id = ? RETURNING id",
            (episode_id,),
        )
        return len(rows)

    def _save_transcript(
        self,
        key: TranscriptKey,
        meta: dict,
        build: TranscriptBuild,
        vectors: list[bytes] | None = None,
        embedding_model: str | None = None,
    ) -> int:
        """Write a transcript and everything derived from it, in one go.

        A single transaction on purpose: a transcript row with no windows would
        look like a searchable episode that silently answers nothing, and that
        is precisely the state a crash between two commits would leave behind.
        The same reasoning covers the vectors: half-embedded windows would make
        dense search quietly blind to half of an episode.
        """
        if self._connection is None:
            raise RuntimeError("Store is not connected")
        if vectors is not None and len(vectors) != len(build.windows):
            raise ValueError(
                f"{len(vectors)} vectors for {len(build.windows)} windows — "
                f"these must correspond one to one."
            )

        try:
            return self._insert_transcript(
                key, meta, build, vectors, embedding_model
            )
        except (sqlite3.IntegrityError, stdlib_sqlite3.IntegrityError):
            # Two episode ids can serve identical bytes — a show re-published,
            # or a feed listing an episode twice — and `find_transcript` only
            # rules that out at the moment it is asked, twenty minutes before
            # this insert.
            #
            # The listening queue serialises transcription within one process,
            # so the remaining window is two processes over one volume: the
            # overlap during a redeploy, where the outgoing container is still
            # finishing a job as the incoming one starts, or a script run
            # against the live database. Both are ordinary, and neither should
            # cost a user their answer after twenty minutes of waiting.
            #
            # The loser of that race has produced the same transcript by
            # definition: same audio, same model, same chunker. So this is not
            # an error, it is a duplicate, and the answer is the row that got
            # there first.
            existing = self._find_transcript(key)
            if existing is None:
                raise
            logger.info(
                "These bytes were transcribed concurrently (%s); keeping %s",
                key.source_sha256[:12],
                existing,
            )
            return existing

    def _find_transcript(self, key: TranscriptKey) -> int | None:
        rows = self._execute(
            """
            SELECT id FROM transcripts
            WHERE source_sha256 = ? AND asr_backend = ? AND asr_model = ?
              AND chunker_version = ?
            """,
            (key.source_sha256, key.asr_backend, key.asr_model, key.chunker_version),
        )
        return int(rows[0]["id"]) if rows else None

    def _insert_transcript(
        self,
        key: TranscriptKey,
        meta: dict,
        build: TranscriptBuild,
        vectors: list[bytes] | None = None,
        embedding_model: str | None = None,
    ) -> int:
        with self._lock:
            connection = self._connection
            with connection:  # commits, or rolls the whole thing back
                cursor = connection.execute(
                    """
                    INSERT INTO transcripts (
                        episode_id, episode_title, feed_title, feed_id, source_url,
                        source_sha256, source_bytes, duration_s, language,
                        asr_backend, asr_model, chunker_version,
                        normalizer_version, embedding_model, quarantined,
                        ms, at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        key.episode_id,
                        _clip(meta.get("episode_title")),
                        _clip(meta.get("feed_title")),
                        str(meta.get("feed_id") or ""),
                        meta.get("source_url", ""),
                        key.source_sha256,
                        meta.get("source_bytes"),
                        meta.get("duration_s"),
                        meta.get("language"),
                        key.asr_backend,
                        key.asr_model,
                        key.chunker_version,
                        normalizer_identity(),
                        embedding_model if vectors else None,
                        build.quarantined,
                        meta.get("ms"),
                        time.time(),
                    ),
                )
                transcript_id = int(cursor.lastrowid or 0)

                connection.executemany(
                    """
                    INSERT INTO utterances (
                        transcript_id, start_ms, end_ms, text, words_json,
                        avg_logprob, no_speech_prob, compression_ratio,
                        signals, indexable
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    [
                        (
                            transcript_id,
                            int(utterance.start * 1000),
                            int(utterance.end * 1000),
                            utterance.text,
                            json.dumps(
                                [
                                    [word.start, word.end, word.text]
                                    for word in utterance.words
                                ],
                                ensure_ascii=False,
                            )
                            if utterance.words
                            else None,
                            utterance.avg_logprob,
                            utterance.no_speech_prob,
                            utterance.compression_ratio,
                            ",".join(signals) or None,
                            int(is_indexable(signals)),
                        )
                        for utterance, signals in zip(
                            build.utterances, build.signals, strict=True
                        )
                    ],
                )

                connection.executemany(
                    """
                    INSERT INTO windows (
                        transcript_id, start_ms, end_ms, text,
                        text_normalized, text_lemmas
                    ) VALUES (?,?,?,?,?,?)
                    """,
                    [
                        (
                            transcript_id,
                            int(window.start * 1000),
                            int(window.end * 1000),
                            window.text,
                            normalize(window.text),
                            lemmatize(window.text),
                        )
                        for window in build.windows
                    ],
                )

                if vectors:
                    # executemany reports no rowids, so the freshly written
                    # windows are read back; AUTOINCREMENT ids preserve the
                    # insertion order that ties vector i to window i.
                    ids = [
                        row[0]
                        for row in connection.execute(
                            "SELECT id FROM windows WHERE transcript_id = ? "
                            "ORDER BY id",
                            (transcript_id,),
                        )
                    ]
                    connection.executemany(
                        "INSERT INTO window_vectors (window_id, vector) "
                        "VALUES (?, ?)",
                        list(zip(ids, vectors, strict=True)),
                    )

        return transcript_id

    async def save_transcript(
        self,
        key: TranscriptKey,
        meta: dict,
        build: TranscriptBuild,
        vectors: list[bytes] | None = None,
        embedding_model: str | None = None,
    ) -> int:
        return await asyncio.to_thread(
            self._save_transcript, key, meta, build, vectors, embedding_model
        )

    def _renormalize(self, transcript_id: int) -> bool:
        """Rebuild a transcript's text columns under today's rules.

        The cheap half of a re-index. `chunker_version` is part of the
        transcript key because changing it means the *audio* has to be heard
        again; the normaliser is not, because everything it needs is already
        in `windows.text` — so a mismatch costs a second of CPU rather than
        the twenty minutes a re-transcription would.
        """
        if self._connection is None:
            raise RuntimeError("Store is not connected")
        wanted = normalizer_identity()

        with self._lock:
            connection = self._connection
            row = connection.execute(
                "SELECT normalizer_version FROM transcripts WHERE id = ?",
                (transcript_id,),
            ).fetchone()
            if row is None or row["normalizer_version"] == wanted:
                return False

            windows = connection.execute(
                "SELECT id, text FROM windows WHERE transcript_id = ?",
                (transcript_id,),
            ).fetchall()
            with connection:  # the triggers rewrite the FTS rows for us
                connection.executemany(
                    "UPDATE windows SET text_normalized = ?, text_lemmas = ? "
                    "WHERE id = ?",
                    [
                        (normalize(w["text"]), lemmatize(w["text"]), w["id"])
                        for w in windows
                    ],
                )
                connection.execute(
                    "UPDATE transcripts SET normalizer_version = ? WHERE id = ?",
                    (wanted, transcript_id),
                )

        logger.info(
            "Re-normalised transcript %s (%s → %s) over %d windows",
            transcript_id,
            row["normalizer_version"],
            wanted,
            len(windows),
        )
        return True

    async def search_windows(
        self, transcript_id: int, query: str, limit: int = 20
    ) -> list[Moment]:
        """Lexical hits inside one transcript, best first and unclustered.

        BM25 in SQLite is a *lower is better* score, so it is negated: callers
        and :func:`~podcast_cutter.transcripts.cluster` both want "higher wins"
        and should not each have to remember which way round this one is.
        """
        # The stored columns and this query have to have been produced by the
        # same rules, or they are two languages that happen to share an
        # alphabet: FTS5 matches tokens literally, so an index built under
        # other rules does not fail, it quietly answers less. Checked here
        # because this is the only code that reads those columns — a version
        # written and never compared is a guarantee nobody is keeping.
        await asyncio.to_thread(self._renormalize, transcript_id)

        # Lemmas first, surface forms second. The lemma index is what makes
        # Russian work at all, and the surface index is the check on it: when
        # the dictionary mangles a word — names and jargon especially — the
        # exact form still finds itself.
        for column in ("text_lemmas", "text_normalized"):
            match = _fts_query(query, column)
            if not match:
                continue

            try:
                rows = await self._run(
                    """
                    SELECT w.start_ms, w.end_ms, w.text,
                           bm25(windows_fts) AS rank
                    FROM windows_fts
                    JOIN windows w ON w.id = windows_fts.rowid
                    WHERE windows_fts MATCH ? AND w.transcript_id = ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (match, transcript_id, limit),
                )
            except (sqlite3.OperationalError, stdlib_sqlite3.OperationalError):
                # A query FTS5 cannot parse is a user typing, not a bug.
                logger.info("FTS5 rejected a user search query")
                return []

            if rows:
                return [
                    Moment(
                        start=row["start_ms"] / 1000,
                        end=row["end_ms"] / 1000,
                        text=row["text"],
                        score=-float(row["rank"]),
                    )
                    for row in rows
                ]

        return []

    async def vector_rows(
        self, transcript_id: int, embedding_model: str
    ) -> list[sqlite3.Row]:
        """Every window of a transcript with its vector, in episode order.

        Empty unless the transcript's recorded embedding model is exactly the
        one asked for: vectors from a different model share a shape and
        nothing else, and comparing them would be a wrong answer with a
        confident score. Raw rows rather than an array, because this module
        must not require NumPy — the caller that can use vectors already has
        it.
        """
        return await self._run(
            """
            SELECT w.start_ms, w.end_ms, w.text, v.vector
            FROM windows w
            JOIN window_vectors v ON v.window_id = w.id
            JOIN transcripts t ON t.id = w.transcript_id
            WHERE w.transcript_id = ? AND t.embedding_model = ?
            ORDER BY w.start_ms
            """,
            (transcript_id, embedding_model),
        )

    async def measured_rtf(self, limit: int = 20) -> float | None:
        """How long recognition actually takes here, per second of audio.

        The median of recent runs rather than a constant, because the honest
        answer depends on the machine, the model and what else it is doing —
        and because this host is shared, so a figure measured on a quiet
        afternoon would mislead every evening.

        ``None`` until something has been transcribed, which is exactly when a
        caller should fall back to a documented default rather than invent one.
        """
        rows = await self._run(
            """
            SELECT ms, duration_s FROM transcripts
            WHERE ms > 0 AND duration_s > 0
            ORDER BY at DESC LIMIT ?
            """,
            (limit,),
        )
        ratios = sorted(row["ms"] / 1000 / row["duration_s"] for row in rows)
        if not ratios:
            return None
        middle = len(ratios) // 2
        if len(ratios) % 2:
            return ratios[middle]
        return (ratios[middle - 1] + ratios[middle]) / 2

    async def utterances_for(self, transcript_id: int) -> list[Utterance]:
        """Every utterance of a transcript, in order, with word timings."""
        rows = await self._run(
            """
            SELECT start_ms, end_ms, text, words_json, avg_logprob,
                   no_speech_prob, compression_ratio
            FROM utterances WHERE transcript_id = ? ORDER BY start_ms
            """,
            (transcript_id,),
        )

        result = []
        for row in rows:
            words = ()
            if row["words_json"]:
                with contextlib.suppress(ValueError, TypeError):
                    words = tuple(
                        Word(start=item[0], end=item[1], text=item[2])
                        for item in json.loads(row["words_json"])
                    )
            result.append(
                Utterance(
                    start=row["start_ms"] / 1000,
                    end=row["end_ms"] / 1000,
                    text=row["text"],
                    words=words,
                    avg_logprob=row["avg_logprob"],
                    no_speech_prob=row["no_speech_prob"],
                    compression_ratio=row["compression_ratio"],
                )
            )
        return result

    # ------------------------------------------------------------------
    # The first-listen queue
    # ------------------------------------------------------------------

    async def enqueue_asr_job(
        self, episode: Episode, user_id: int, chat_id: int
    ) -> int:
        """Record that this person is waiting for this episode.

        The episode is copied in rather than referenced, because the job has to
        be runnable by a process that never saw the session it came from.
        """
        rows = await self._run(
            """
            INSERT INTO asr_jobs (
                episode_id, audio_url, episode_title, feed_title, feed_id,
                user_id, chat_id, state, at
            ) VALUES (?,?,?,?,?,?,?, 'queued', ?)
            RETURNING id
            """,
            (
                episode.id,
                episode.enclosure_url,
                _clip(episode.title),
                _clip(episode.feed_title),
                episode.feed_id,
                user_id,
                chat_id,
                time.time(),
            ),
        )
        return int(rows[0]["id"])

    async def claim_asr_batch(self, max_attempts: int = 2) -> AsrBatch | None:
        """Take the oldest queued episode, with everyone waiting on it.

        One episode, not one row: ten people asking about the same popular
        episode must produce one transcription and ten waiters, and a queue
        that made them ten jobs would report a ten-deep line for one episode's
        worth of work.

        Called only from the single worker, so the read and the update need no
        transaction beyond the connection's own.
        """
        head = await self._run(
            """
            SELECT episode_id FROM asr_jobs
            WHERE state = 'queued' AND attempts < ?
            ORDER BY id LIMIT 1
            """,
            (max_attempts,),
        )
        if not head:
            return None
        episode_id = head[0]["episode_id"]

        await self._run(
            """
            UPDATE asr_jobs
            SET state = 'running', attempts = attempts + 1, started_at = ?
            WHERE state = 'queued' AND episode_id = ?
            """,
            (time.time(), episode_id),
        )
        rows = await self._run(
            "SELECT * FROM asr_jobs WHERE state = 'running' AND episode_id = ? "
            "ORDER BY id",
            (episode_id,),
        )
        return AsrBatch(
            episode_id=episode_id,
            jobs=[_as_job(row) for row in rows],
        )

    async def finish_asr_jobs(
        self, job_ids: Iterable[int], state: str, outcome: str | None = None
    ) -> None:
        ids = list(job_ids)
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        await self._run(
            f"UPDATE asr_jobs SET state = ?, finished_at = ?, outcome = ? "
            f"WHERE id IN ({placeholders})",
            (state, time.time(), outcome, *ids),
        )

    async def requeue_running_asr_jobs(self) -> int:
        """Put jobs interrupted by a restart back in line.

        A `running` row at startup means the process died holding it — nothing
        else can write that state — so it is picked up again from the top. The
        attempt counter already charged for the first try, which is what stops
        an episode that crashes the bot from doing it forever.
        """
        rows = await self._run(
            "UPDATE asr_jobs SET state = 'queued' WHERE state = 'running' "
            "RETURNING id"
        )
        return len(rows)

    async def abandon_exhausted_asr_jobs(self, max_attempts: int = 2) -> list[AsrJob]:
        """Jobs that have used up their retries, taken out of the queue."""
        rows = await self._run(
            "SELECT * FROM asr_jobs WHERE state = 'queued' AND attempts >= ? "
            "ORDER BY id",
            (max_attempts,),
        )
        jobs = [_as_job(row) for row in rows]
        await self.finish_asr_jobs(
            (job.id for job in jobs), "abandoned", "attempts"
        )
        return jobs

    async def asr_queue_episodes(self) -> list[str]:
        """Episodes in the queue right now, in the order they will be served.

        Distinct, because that is what a position number has to count: being
        told "third in line" when the two ahead are the same episode as each
        other is a wrong number, not a rounded one.
        """
        rows = await self._run(
            """
            SELECT episode_id, min(id) AS head FROM asr_jobs
            WHERE state IN ('queued', 'running')
            GROUP BY episode_id ORDER BY head
            """
        )
        return [row["episode_id"] for row in rows]

    async def asr_jobs_for(self, episode_id: str) -> list[AsrJob]:
        """Everyone currently waiting on one episode."""
        rows = await self._run(
            "SELECT * FROM asr_jobs WHERE episode_id = ? "
            "AND state IN ('queued', 'running') ORDER BY id",
            (episode_id,),
        )
        return [_as_job(row) for row in rows]

    async def purge_asr_jobs(self, older_than_days: int) -> int:
        """Drop finished queue rows. Shares the journal's retention window."""
        if older_than_days <= 0:
            return 0
        cutoff = time.time() - older_than_days * 86400
        rows = await self._run(
            "DELETE FROM asr_jobs WHERE state NOT IN ('queued', 'running') "
            "AND at < ? RETURNING id",
            (cutoff,),
        )
        return len(rows)

    async def purge_asr_jobs_hours(self, older_than_hours: int) -> int:
        """Drop terminal queue rows after their short diagnostic window."""
        if older_than_hours <= 0:
            return 0
        cutoff = time.time() - older_than_hours * 3600
        rows = await self._run(
            "DELETE FROM asr_jobs WHERE state NOT IN ('queued', 'running') "
            "AND finished_at < ? RETURNING id",
            (cutoff,),
        )
        return len(rows)

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    async def user_language(self, user_id: int) -> str | None:
        """The explicitly chosen language, or ``None`` when never chosen."""
        try:
            rows = await self._run(
                "SELECT language FROM users WHERE user_id = ?", (user_id,)
            )
        except Exception:
            logger.exception("Could not read a user's language")
            return None
        return rows[0]["language"] if rows else None

    async def terms_accepted(self, user_id: int, version: str) -> bool:
        rows = await self._run(
            "SELECT 1 FROM users WHERE user_id = ? AND terms_version = ?",
            (user_id, version),
        )
        return bool(rows)

    async def accept_terms(
        self, user_id: int, language: str, version: str
    ) -> None:
        now = time.time()
        await self._run(
            """
            INSERT INTO users (
                user_id, language, at, terms_version, terms_accepted_at
            ) VALUES (?,?,?,?,?)
            ON CONFLICT (user_id) DO UPDATE SET
                at = excluded.at,
                terms_version = excluded.terms_version,
                terms_accepted_at = excluded.terms_accepted_at
            """,
            (user_id, language, now, version, now),
        )

    async def set_user_language(self, user_id: int, language: str) -> None:
        """Record an explicit language choice. Never raises."""
        try:
            await self._run(
                """
                INSERT INTO users (user_id, language, at) VALUES (?,?,?)
                ON CONFLICT (user_id) DO UPDATE
                    SET language = excluded.language, at = excluded.at
                """,
                (user_id, language, time.time()),
            )
        except Exception:
            logger.exception("Could not save a user's language")

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
                    duration, feed_id, author, episode_url, at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT (user_id, episode_id) DO UPDATE SET
                    title = excluded.title,
                    feed_title = excluded.feed_title,
                    enclosure_url = excluded.enclosure_url,
                    duration = excluded.duration,
                    feed_id = excluded.feed_id,
                    author = excluded.author,
                    episode_url = excluded.episode_url,
                    at = excluded.at
                """,
                (
                    user_id,
                    episode.id,
                    _clip(episode.title),
                    _clip(episode.feed_title),
                    episode.enclosure_url,
                    episode.duration,
                    episode.feed_id,
                    episode.author,
                    episode.episode_url,
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
                SELECT episode_id, title, feed_title, enclosure_url, duration,
                       feed_id, author, episode_url
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
                feed_id=row["feed_id"],
                author=row["author"],
                episode_url=row["episode_url"],
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

    async def user_data(self, user_id: int) -> dict:
        """A compact export of every user-linked durable row."""
        users = await self._run(
            "SELECT language, terms_version, terms_accepted_at, at "
            "FROM users WHERE user_id = ?",
            (user_id,),
        )
        recent = await self._run(
            "SELECT episode_id, title, feed_title, at FROM recents "
            "WHERE user_id = ? ORDER BY at DESC",
            (user_id,),
        )
        counts = {}
        for table in ("events", "asr_jobs"):
            row = await self._run(
                f"SELECT count(*) AS n FROM {table} WHERE user_id = ?",
                (user_id,),
            )
            counts[table] = int(row[0]["n"])
        return {
            "profile": dict(users[0]) if users else None,
            "recents": [dict(row) for row in recent],
            **counts,
        }

    async def delete_user_data(self, user_id: int) -> dict[str, int]:
        """Delete every durable row directly associated with a Telegram id."""
        removed = {}
        for table in ("events", "recents", "asr_jobs", "users"):
            rows = await self._run(
                f"DELETE FROM {table} WHERE user_id = ? RETURNING 1",
                (user_id,),
            )
            removed[table] = len(rows)
        return removed

    async def purge_inactive_user_data(self, older_than_days: int) -> int:
        """Expire profiles and recents for users inactive past the policy."""
        if older_than_days <= 0:
            return 0
        cutoff = time.time() - older_than_days * 86400
        recents = await self._run(
            "DELETE FROM recents WHERE at < ? RETURNING 1", (cutoff,)
        )
        users = await self._run(
            "DELETE FROM users WHERE at < ? RETURNING 1", (cutoff,)
        )
        return len(recents) + len(users)

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

        # People, not visits: the same person opening the link twice says
        # nothing about whether the channel works.
        sources = await self._run(
            """
            SELECT detail, count(DISTINCT user_id) AS n FROM events
            WHERE action = 'start' AND detail IS NOT NULL AND at >= ?
            GROUP BY detail ORDER BY n DESC LIMIT 5
            """,
            (since,),
        )
        result.sources = [(row["detail"], int(row["n"])) for row in sources]

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
