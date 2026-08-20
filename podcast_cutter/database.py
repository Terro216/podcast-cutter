"""SQLCipher keying and verification shared by runtime and migration tools."""

from __future__ import annotations

import re
from pathlib import Path

from sqlcipher3 import dbapi2 as sqlite3

_RAW_KEY = re.compile(r"[0-9a-fA-F]{64}")


def validate_key(key: str) -> str:
    """Return a normalized raw SQLCipher key or reject ambiguous material."""
    value = key.strip()
    if not _RAW_KEY.fullmatch(value):
        raise ValueError(
            "DATABASE_KEY must contain exactly 64 hexadecimal characters"
        )
    return value.lower()


def key_connection(connection: sqlite3.Connection, key: str) -> None:
    """Key a fresh connection and prove SQLCipher is actually active.

    This must run before anything that reads a database page. A raw 256-bit
    key has a fixed safe alphabet and avoids passing a secret as a process
    argument or relying on passphrase derivation defaults.
    """
    raw = validate_key(key)
    connection.execute(f'''PRAGMA key = "x'{raw}'"''')
    connection.execute("PRAGMA cipher_memory_security = ON")
    status = connection.execute("PRAGMA cipher_status").fetchone()
    if not status or int(status[0]) != 1:
        raise RuntimeError("SQLCipher did not accept DATABASE_KEY")
    connection.execute("SELECT count(*) FROM sqlite_master").fetchone()


def verify_encrypted_database(path: Path, key: str) -> tuple[int, int]:
    """Authenticate every page and return transcript/event counts."""
    connection = sqlite3.connect(path)
    try:
        key_connection(connection, key)
        if connection.execute("PRAGMA cipher_integrity_check").fetchall():
            raise RuntimeError("SQLCipher integrity check failed")
        quick = connection.execute("PRAGMA quick_check").fetchone()
        if not quick or quick[0] != "ok":
            raise RuntimeError("SQLite quick_check failed")
        transcripts = int(
            connection.execute("SELECT count(*) FROM transcripts").fetchone()[0]
        )
        events = int(
            connection.execute("SELECT count(*) FROM events").fetchone()[0]
        )
        return transcripts, events
    finally:
        connection.close()
