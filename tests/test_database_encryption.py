import sqlite3 as plaintext_sqlite

import pytest
from sqlcipher3 import dbapi2 as encrypted_sqlite

from podcast_cutter.database import verify_encrypted_database
from podcast_cutter.migrate_sqlcipher import migrate
from podcast_cutter.store import Store

KEY = "ab" * 32
OTHER_KEY = "cd" * 32
SQLITE_HEADER = b"SQLite format 3\x00"


def header(path) -> bytes:
    return path.read_bytes()[: len(SQLITE_HEADER)]


class TestEncryptedStore:
    def test_new_database_is_not_plaintext_sqlite(self, tmp_path):
        path = tmp_path / "encrypted.db"
        store = Store(path, key=KEY)
        store.connect()
        assert store._connection is not None
        assert store._connection.execute("PRAGMA temp_store").fetchone()[0] == 2
        store._execute(
            "INSERT INTO events (at, action) VALUES (?, ?)", (1, "test")
        )
        store.close()

        assert header(path) != SQLITE_HEADER
        assert verify_encrypted_database(path, KEY) == (0, 1)
        with pytest.raises(plaintext_sqlite.DatabaseError):
            plaintext_sqlite.connect(path).execute(
                "SELECT count(*) FROM events"
            ).fetchone()

    def test_wrong_key_is_rejected_at_connect(self, tmp_path):
        path = tmp_path / "encrypted.db"
        store = Store(path, key=KEY)
        store.connect()
        store.close()

        with pytest.raises(encrypted_sqlite.DatabaseError, match="database|encrypted"):
            Store(path, key=OTHER_KEY).connect()


class TestPlaintextMigration:
    def test_preserves_data_and_retains_a_plaintext_rollback(self, tmp_path):
        path = tmp_path / "podcast_cutter.db"
        store = Store(path)
        store.connect()
        store._execute(
            "INSERT INTO events (at, action) VALUES (?, ?)", (1, "before")
        )
        store.close()
        assert header(path) == SQLITE_HEADER

        retained = migrate(path, KEY)

        assert retained is not None
        assert retained.stat().st_mode & 0o777 == 0o600
        assert header(retained) == SQLITE_HEADER
        assert header(path) != SQLITE_HEADER
        assert verify_encrypted_database(path, KEY) == (0, 1)

        reopened = Store(path, key=KEY)
        reopened.connect()
        assert reopened._execute("SELECT action FROM events")[0][0] == "before"
        reopened.close()

    def test_repeated_migration_is_a_verified_noop(self, tmp_path):
        path = tmp_path / "podcast_cutter.db"
        store = Store(path)
        store.connect()
        store.close()

        migrate(path, KEY)

        assert migrate(path, KEY) is None
