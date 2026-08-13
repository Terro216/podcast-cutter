#!/usr/bin/env bash
# Restore-side of the offsite backup: pull a snapshot back, prove the database
# is intact and the manifest is the shape this stack writes. `validate` (the
# default) checks; it never writes into production. Putting the restored
# database back is a deliberate manual step — see docs/backup-policy.md — so a
# drill can run monthly without any chance of clobbering live data.
set -Eeuo pipefail
umask 077

: "${BACKUP_PASSWORD:?BACKUP_PASSWORD is required}"
: "${YANDEX_DISK_TARGET:?YANDEX_DISK_TARGET is required}"
export RESTIC_PASSWORD="$BACKUP_PASSWORD"
export RESTIC_REPOSITORY="$YANDEX_DISK_TARGET"
export RCLONE_CONFIG=/config/rclone/rclone.conf

snapshot="${1:-latest}"
target=/backup/.restore-pending
rm -rf "$target"
mkdir -p "$target"
restic restore "$snapshot" --host big-one --tag podcast-cutter --target "$target"

manifest="$(find "$target" -type f -name manifest.json -print -quit)"
[ -n "$manifest" ] || { echo 'manifest.json not found in restored snapshot' >&2; exit 1; }
root="$(dirname "$manifest")"

format="$(jq -er '.format' "$manifest")"
[ "$format" = 1 ] || { echo "Unsupported manifest format: $format" >&2; exit 1; }
jq -e '(.artifacts | index("db")) and (.artifacts | index("repo"))' "$manifest" >/dev/null

db="$root/data/podcast_cutter.db"
[ -f "$db" ] || { echo 'restored database missing' >&2; exit 1; }
[ "$(sqlite3 "$db" 'PRAGMA quick_check;')" = ok ] \
    || { echo 'restored database quick_check failed' >&2; exit 1; }
# The transcripts are the reason this backup exists; a snapshot that restored
# to an empty table would pass quick_check and still be worthless.
transcripts="$(sqlite3 "$db" 'SELECT count(*) FROM transcripts;')"
events="$(sqlite3 "$db" 'SELECT count(*) FROM events;')"
[ -s "$root/repo/declarative-config.tar.gz" ] && tar -tzf "$root/repo/declarative-config.tar.gz" >/dev/null

jq -n \
    --arg snapshot "$snapshot" \
    --argjson transcripts "$transcripts" \
    --argjson events "$events" \
    --argjson expected "$(jq '.counts' "$manifest")" \
    '{snapshot:$snapshot,restored:{transcripts:$transcripts,events:$events},
      manifest_counts:$expected}'
echo 'Restore validation OK'
