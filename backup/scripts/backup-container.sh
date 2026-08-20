#!/usr/bin/env bash
# App-aware offsite backup for podcast-cutter. Everything irreplaceable lives
# in one SQLite database — the journal, the recent-episode list, and the
# transcripts, which are the expensive artifact now (a reference pass is an
# overnight of CPU, a SpeechKit pass costs real money). Audio is never cached
# and the models are re-downloadable, so neither is backed up.
#
# The database is snapshotted with sqlcipher_export(), not `cp`: journal_mode
# is WAL and a plain copy under a concurrent writer is a torn file. Only after
# the encrypted copy passes SQLCipher HMAC and SQLite consistency checks is
# anything handed to restic. Mirrors cinemarr/vaultwarden: same repo layout,
# same flock, same current/previous rotation, same "any error leaves the last
# good backup untouched and exits nonzero".
set -Eeuo pipefail
umask 077

for name in BACKUP_PASSWORD YANDEX_DISK_TARGET DATABASE_KEY; do
    [ -n "${!name:-}" ] || { echo "Missing environment variable: $name" >&2; exit 1; }
done
[[ "$DATABASE_KEY" =~ ^[0-9a-fA-F]{64}$ ]] \
    || { echo 'DATABASE_KEY must be exactly 64 hexadecimal characters' >&2; exit 1; }

export RESTIC_PASSWORD="${BACKUP_PASSWORD}"
export RESTIC_REPOSITORY="${YANDEX_DISK_TARGET}"
export RCLONE_CONFIG=/config/rclone/rclone.conf
KEEP_DAILY="${BACKUP_KEEP_DAILY:-14}"
KEEP_WEEKLY="${BACKUP_KEEP_WEEKLY:-8}"
DB=/sources/data/podcast_cutter.db
PENDING=/backup/pending
LOG=/tmp/podcast-cutter-backup.log

notify() {
    local state=$1
    [ -n "${BACKUP_PUSH_URL:-}" ] || return 0
    [ -n "${BACKUP_PUSH_TOKEN:-}" ] || { echo 'BACKUP_PUSH_TOKEN is required when BACKUP_PUSH_URL is set' >&2; return 0; }
    curl --retry 3 --max-time 15 -fsS -X POST \
        -H "Authorization: Bearer ${BACKUP_PUSH_TOKEN}" \
        "${BACKUP_PUSH_URL}?success=$([ "$state" = success ] && echo true || echo false)" >/dev/null || true
}
cleanup() {
    status=$?
    if (( status == 0 )); then notify success; else notify failure; fi
    rm -rf "$PENDING"
}
exec > >(tee -a "$LOG") 2>&1

# The lock is taken BEFORE the cleanup trap is installed: a second concurrent
# run must exit without touching the shared staging dir (or pushing a false
# failure) while the first still owns it.
exec 9>/backup/.backup.lock
flock -n 9 || { echo 'Another backup is already running' >&2; exit 1; }
trap cleanup EXIT
rm -rf "$PENDING"
mkdir -p "$PENDING"/{data,repo}

echo '==> podcast-cutter database: authenticated SQLCipher export'
[ -f "$DB" ] || { echo "Database not found at $DB" >&2; exit 1; }
SNAPSHOT_DB="$PENDING/data/podcast_cutter.db"
user_version="$(sqlcipher -batch "$DB" <<SQL
.output /dev/null
PRAGMA key = "x'$DATABASE_KEY'";
.output stdout
PRAGMA user_version;
SQL
)"
[[ "$user_version" =~ ^[0-9]+$ ]] \
    || { echo 'could not read encrypted database version' >&2; exit 1; }
sqlcipher -batch "$DB" <<SQL
.output /dev/null
PRAGMA key = "x'$DATABASE_KEY'";
.output stdout
ATTACH DATABASE '$SNAPSHOT_DB' AS snapshot KEY "x'$DATABASE_KEY'";
SELECT sqlcipher_export('snapshot');
PRAGMA snapshot.user_version = $user_version;
DETACH DATABASE snapshot;
SQL
[ "$(sqlcipher -batch "$SNAPSHOT_DB" <<SQL
.output /dev/null
PRAGMA key = "x'$DATABASE_KEY'";
.output stdout
PRAGMA quick_check;
SQL
)" = ok ] \
    || { echo 'database quick_check failed' >&2; exit 1; }
[ -z "$(sqlcipher -batch "$SNAPSHOT_DB" <<SQL
.output /dev/null
PRAGMA key = "x'$DATABASE_KEY'";
.output stdout
PRAGMA cipher_integrity_check;
SQL
)" ] || { echo 'database cipher_integrity_check failed' >&2; exit 1; }
transcripts="$(sqlcipher -batch "$SNAPSHOT_DB" <<SQL
.output /dev/null
PRAGMA key = "x'$DATABASE_KEY'";
.output stdout
SELECT count(*) FROM transcripts;
SQL
)"
events="$(sqlcipher -batch "$SNAPSHOT_DB" <<SQL
.output /dev/null
PRAGMA key = "x'$DATABASE_KEY'";
.output stdout
SELECT count(*) FROM events;
SQL
)"
echo "    ${transcripts} transcripts, ${events} journal rows"

# The declarative side of the deployment, so a restore reconstructs the stack
# and not only its data. `.env` carries the secrets that make the rest work;
# it rides inside the encrypted repository and nowhere else.
echo '==> repository configuration'
[ -f /repo/.env ] && cp /repo/.env "$PENDING/repo/env"
cp /repo/docker-compose.yml "$PENDING/repo/"
tar -C /repo --exclude=.git --exclude=.env --exclude=evals/fixtures \
    -czf "$PENDING/repo/declarative-config.tar.gz" \
    docker-compose.yml Dockerfile backup deploy scripts docs \
    podcast_cutter main.py pyproject.toml 2>/dev/null || true

jq -n \
    --arg created_at "$(date -u +%FT%TZ)" \
    --argjson transcripts "$transcripts" \
    --argjson events "$events" \
    '{format:2,created_at:$created_at,database:"sqlcipher4",
      artifacts:["db","repo"],
      counts:{transcripts:$transcripts,events:$events}}' \
    >"$PENDING/manifest.json"

echo '==> Restic snapshot'
if ! restic cat config >/dev/null 2>&1; then
    rclone_target="${RESTIC_REPOSITORY#rclone:}"
    if [ -n "$(rclone lsf "$rclone_target" --max-depth 1 2>/dev/null)" ]; then
        echo 'Remote directory is non-empty but is not readable as this restic repository' >&2
        exit 1
    fi
    echo 'Restic repository is empty; initializing'
    restic init
fi
backup_json="$(restic backup --json --host big-one --tag podcast-cutter "$PENDING")"
snapshot_id="$(jq -rs 'map(select(.message_type == "summary"))[-1].snapshot_id' <<<"$backup_json")"
[ -n "$snapshot_id" ] && [ "$snapshot_id" != null ]
restic snapshots --json "$snapshot_id" | jq -e --arg id "$snapshot_id" 'any(.[]; .id == $id)' >/dev/null
restic forget --group-by host,tags --host big-one --tag podcast-cutter \
    --keep-daily "$KEEP_DAILY" --keep-weekly "$KEEP_WEEKLY"

# The remote restic snapshot is encrypted and now owns the recovery copy of
# `.env`. Do not retain a decrypted duplicate in the local staging rotation.
rm -f "$PENDING/repo/env"

# Only after the remote snapshot is confirmed: rotate the local staging copy so
# the last good backup survives a failed next run.
rm -rf /backup/previous
[ ! -d /backup/current ] || mv /backup/current /backup/previous
mv "$PENDING" /backup/current

trap - EXIT
notify success
echo "Backup complete: $snapshot_id"
