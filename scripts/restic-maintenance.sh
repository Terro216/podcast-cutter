#!/usr/bin/env bash
# Repository upkeep, kept apart from the daily backup so a slow prune or a
# read-data check never delays taking the snapshot. `weekly` prunes and checks
# structure; `monthly` re-reads every pack and runs a restore drill.
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$REPO_DIR/.env"
COMPOSE=(docker compose --env-file "$ENV_FILE" -f "$REPO_DIR/docker-compose.yml")
MODE="${1:-weekly}"

# shellcheck source=scripts/lib/compose-env.sh
source "$SCRIPT_DIR/lib/compose-env.sh"
load_compose_env

notify() {
    local state=$1 url=""
    case "$MODE" in
        weekly) url="${BACKUP_PUSH_URL_WEEKLY:-}" ;;
        monthly) url="${BACKUP_PUSH_URL_MONTHLY:-}" ;;
    esac
    [ -n "$url" ] || return 0
    [ -n "${BACKUP_PUSH_TOKEN:-}" ] || { echo 'BACKUP_PUSH_TOKEN is required when a push URL is set' >&2; return 0; }
    curl --retry 3 --max-time 15 -fsS -X POST \
        -H "Authorization: Bearer ${BACKUP_PUSH_TOKEN}" \
        "${url}?success=$([ "$state" = success ] && echo true || echo false)" >/dev/null || true
}
on_exit() {
    if (( $? == 0 )); then notify success; else notify failure; fi
}

run_restic() {
    # The variables expand inside the backup container, not in this shell.
    # shellcheck disable=SC2016
    "${COMPOSE[@]}" --profile backup run --rm --no-deps --entrypoint /bin/bash backup \
        -ec 'export RESTIC_PASSWORD="$BACKUP_PASSWORD" RESTIC_REPOSITORY="$YANDEX_DISK_TARGET" RCLONE_CONFIG=/config/rclone/rclone.conf; exec restic "$@"' -- "$@"
}

case "$MODE" in
    weekly|monthly) trap on_exit EXIT ;;
    *) echo "Usage: $0 [weekly|monthly]" >&2; exit 2 ;;
esac

# A prune or check killed by a timeout leaves a stale lock that would otherwise
# block both maintenance and the backups themselves. Not --remove-all: a live
# lock from a concurrent backup is left alone.
run_restic unlock

case "$MODE" in
    weekly)
        run_restic prune --max-unused 10%
        run_restic check
        ;;
    monthly)
        run_restic check --read-data
        "$SCRIPT_DIR/restore.sh" drill latest
        ;;
esac
