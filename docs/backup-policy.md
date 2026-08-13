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

## Going live — one-time provisioning (not yet done)

The code, image and timers are committed and dry-run-verified against a local
repo. Making it live needs secrets the repo must never carry:

1. **rclone config.** Reuse the shared Yandex token: copy an existing stack's
   `rclone.conf` to `BACKUP_RCLONE_DIR` on big-one (default
   `/home/me/.podcast-cutter/rclone/rclone.conf`), or `rclone config` a
   `yadisk` remote there. Confirm with
   `rclone lsd yadisk: --config <that file>`.
2. **`.env`.** Set `BACKUP_PASSWORD` (generate; also store in Bitwarden),
   `YANDEX_DISK_TARGET=rclone:yadisk:/backups/podcast-cutter`,
   `BACKUP_STAGE=/srv/podcast-cutter-backup`, `BACKUP_RCLONE_DIR`, and the
   `BACKUP_KEEP_*`. Optionally the gatus `BACKUP_PUSH_*` heartbeats.
3. **First backup:** `scripts/backup.sh --build`. It initialises the repo on
   the first run. Then `scripts/restore.sh validate` to prove the round trip.
4. **Install the timers:** copy `deploy/systemd/*` to `/etc/systemd/system/`,
   `systemctl daemon-reload`, then `systemctl enable --now
   podcast-cutter-backup.timer podcast-cutter-backup-weekly.timer
   podcast-cutter-backup-monthly.timer`.
