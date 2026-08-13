# Backup policy — podcast-cutter

Source of truth for the implementation: `scripts/backup.sh`, `backup/`,
`scripts/restore.sh`, `scripts/restic-maintenance.sh` and `deploy/systemd/`.
This mirrors the cinemarr and vaultwarden stacks deliberately — same tools
(restic + rclone-native-yandex), same repo layout (`yadisk:/backups/<stack>`),
same flock and rotation discipline — so one operator understands all three.

## What is backed up, and what is not

Everything irreplaceable lives in **one** SQLite database,
`/data/podcast_cutter.db` on the `podcast-data` volume:

| Artifact | Why it matters |
| --- | --- |
| `transcripts` + `utterances` + vectors | The expensive artifact. A reference pass is an overnight of CPU; a SpeechKit pass costs real money. |
| `events` (the journal) | Usage history, `src_` attribution, `/stats`. |
| recent-episode list | The only per-user state worth keeping. |
| `.env` + declarative config | So a restore reconstructs the stack, not only its data. |

**Not** backed up, on purpose: cached audio (never stored), the ASR/embedding
models (`/data/models`, re-downloadable and re-convertible — see HANDOFF §6),
and rotating logs. Losing a model costs one download; backing it up nightly
would dwarf the data that actually matters.

## Consistency

- The database is snapshotted with the **SQLite Online Backup API**
  (`.backup`), never `cp`: `journal_mode=WAL` is on and a plain copy under a
  concurrent writer is a torn file. Verified that `.backup` works from a
  read-only mount of the live WAL database.
- Every copy passes `PRAGMA quick_check` before anything reaches restic.
- Only after the remote snapshot is confirmed by id are the local
  `current`/`previous` staging generations rotated. Any error leaves the last
  good `current` in place and exits nonzero.
- Concurrent runs are blocked by `flock`, taken **before** the cleanup trap so
  a second run cannot delete the first's staging dir. The Docker socket is
  never mounted into the backup container; the data volume is read-only.

## Storage and encryption

```text
/srv/podcast-cutter-backup/{current,previous}   (BACKUP_STAGE, host)
  -> restic 0.19.1 (encrypted)
  -> rclone 1.74.4 native yandex
  -> yadisk:/backups/podcast-cutter
```

- restic/rclone/Alpine images are digest-pinned in `backup/Dockerfile`.
- The restic password (`BACKUP_PASSWORD`) is in `.env` **and** must have a copy
  in the user's Bitwarden. Without it the repository is unrecoverable.
- rclone's OAuth config is this stack's own copy at `BACKUP_RCLONE_DIR`,
  mounted read-write so rclone can persist refreshed tokens. It is **not** the
  cinemarr or vaultwarden volume — tying the offsite path to another project's
  volume is the coupling those stacks' notes warn against. The Yandex refresh
  token itself is shared across stacks (rclone's built-in Yandex app issues one
  token per (app, user)); that is a known, accepted property, not a mistake.
- Never clean the repository with `rclone sync --delete` or by hand-removing
  `data/`, `index/`, or pack files. Use `restic forget`/`prune` only.

## Schedule (systemd timers on big-one)

| Timer | When (Europe/Moscow) | Does |
| --- | --- | --- |
| `podcast-cutter-backup.timer` | daily 04:50 | snapshot + `forget` (keep 14 daily, 8 weekly) |
| `podcast-cutter-backup-weekly.timer` | Sun 06:40 | `prune --max-unused 10%` + `check` |
| `podcast-cutter-backup-monthly.timer` | 1st Sat 08:00 | `check --read-data` + restore drill |

Times sit after cinemarr's own windows so the two stacks never hold restic
locks against the shared Yandex account at once. **Order against retention:**
`LOG_RETENTION_DAYS` purges journal rows at the bot's startup, so the daily
backup runs well before any manual redeploy would.

## Restore

- `scripts/restore.sh validate [snapshot]` — restore into staging, `quick_check`
  the database, confirm the manifest shape and that `transcripts` is non-empty.
  Never touches production. `drill` is the same today (one SQLite file is the
  whole state) and is what the monthly timer runs.
- **Into production** is a deliberate manual operation — no script does it by
  accident:
  1. `scripts/restore.sh validate <snapshot>` and read the counts.
  2. `docker compose stop podcast-cutter` (drop the WAL writer).
  3. Copy the restored `data/podcast_cutter.db` from `BACKUP_STAGE/current`
     over `/data/podcast_cutter.db` in the volume, and delete any stale
     `-wal`/`-shm` beside it.
  4. `docker compose start podcast-cutter`, watch the log for `Store ready`.

An untested backup is not a backup — the monthly drill exists for that.

## Going live — done 2026-08-13

Provisioned and verified on big-one:

1. **rclone config.** The Yandex `[yadisk]` remote is **shared across all
   stacks** — `~/.config/rclone/rclone.conf` and `/srv/arr/rclone/rclone.conf`
   carry the identical token (checked by fingerprint). rclone issues one token
   per (built-in app, user), so `rclone authorize` would have re-minted and
   invalidated it for cinemarr and vaultwarden. The token was therefore
   **reused, not re-issued**: a copy sits at
   `/home/me/.podcast-cutter/rclone/rclone.conf` (`BACKUP_RCLONE_DIR`), its own
   file so nothing couples to another stack's volume. A separate OAuth app is
   the documented long-term un-sharing (accepted risk).
2. **`.env`** carries `BACKUP_PASSWORD`, `YANDEX_DISK_TARGET`, `BACKUP_STAGE`,
   `BACKUP_RCLONE_DIR`, `BACKUP_KEEP_*`, `TZ`. **The restic password's Bitwarden
   copy is still owed** — without it the repository is unrecoverable.
3. **First backup done:** snapshot `c01f794d` at
   `yadisk:/backups/podcast-cutter`, and `scripts/restore.sh validate`
   round-tripped it (restored counts matched the manifest).
4. **Timers enabled** in `/etc/systemd/system/`: daily 04:57, weekly Sun 06:42,
   monthly 1st Sat 08:00 (Europe/Moscow), all after cinemarr's windows.
