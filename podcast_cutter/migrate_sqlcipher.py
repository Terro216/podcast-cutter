"""One-time, offline conversion of the production SQLite file to SQLCipher."""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from sqlcipher3 import dbapi2 as sqlite3

from .database import validate_key, verify_encrypted_database

_SQLITE_HEADER = b"SQLite format 3\x00"


def _header(path: Path) -> bytes:
    with path.open("rb") as handle:
        return handle.read(len(_SQLITE_HEADER))


def migrate(path: Path, key: str) -> Path | None:
    """Encrypt ``path`` atomically and retain its plaintext predecessor.

    Returns the retained plaintext path. ``None`` means the database was
    already encrypted with this key. The caller must stop every writer first.
    """
    key = validate_key(key)
    if not path.is_file():
        raise FileNotFoundError(path)

    if _header(path) != _SQLITE_HEADER:
        verify_encrypted_database(path, key)
        return None

    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    encrypted = path.with_name(f".{path.name}.{timestamp}.sqlcipher-new")
    plaintext = path.with_name(f"{path.name}.plaintext-{timestamp}")
    if encrypted.exists() or plaintext.exists():
        raise FileExistsError("migration output already exists; retry later")

    source = sqlite3.connect(path)
    try:
        # With the application stopped, fold any committed WAL pages into the
        # source before exporting one coherent snapshot.
        source.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        quick = source.execute("PRAGMA quick_check").fetchone()
        if not quick or quick[0] != "ok":
            raise RuntimeError("plaintext database quick_check failed")
        version = int(source.execute("PRAGMA user_version").fetchone()[0])

        source.execute(
            f'''ATTACH DATABASE ? AS encrypted KEY "x'{key}'"''',
            (str(encrypted),),
        )
        try:
            source.execute("SELECT sqlcipher_export('encrypted')").fetchone()
            source.execute(f"PRAGMA encrypted.user_version = {version}")
        finally:
            source.execute("DETACH DATABASE encrypted")
    except Exception:
        encrypted.unlink(missing_ok=True)
        raise
    finally:
        source.close()

    verify_encrypted_database(encrypted, key)
    encrypted.chmod(0o600)

    os.replace(path, plaintext)
    try:
        os.replace(encrypted, path)
    except Exception:
        os.replace(plaintext, path)
        raise
    path.chmod(0o600)
    plaintext.chmod(0o600)

    for suffix in ("-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    return plaintext


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Offline plaintext SQLite to SQLCipher migration"
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.environ.get("DATA_DIR", "/data")) / "podcast_cutter.db",
    )
    args = parser.parse_args()
    key = os.environ.get("DATABASE_KEY", "")
    retained = migrate(args.database, key)
    if retained is None:
        print("Database is already encrypted and verified.")
    else:
        print(f"Encrypted database verified. Plaintext retained at {retained}")


if __name__ == "__main__":
    main()
