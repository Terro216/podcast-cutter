#!/usr/bin/env bash
# `validate` (default) restores the latest snapshot into staging and checks the
# database and manifest without touching production. `drill` is the same today —
# there is no service to stand up in isolation, one SQLite file is the whole
# state — but the verb is kept so the monthly timer and the cinemarr/vaultwarden
# muscle memory line up. Putting a snapshot back into the live database is a
# deliberate manual operation, written out in docs/backup-policy.md, precisely
# so no script can do it by accident.
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$REPO_DIR/.env"
COMPOSE=(docker compose --env-file "$ENV_FILE" -f "$REPO_DIR/docker-compose.yml")
MODE="${1:-validate}"
SNAPSHOT="${2:-latest}"

[ -f "$ENV_FILE" ] || { echo "No $ENV_FILE" >&2; exit 1; }
# shellcheck source=scripts/lib/compose-env.sh
source "$SCRIPT_DIR/lib/compose-env.sh"
load_compose_env
: "${BACKUP_PASSWORD:?BACKUP_PASSWORD is required}"
: "${YANDEX_DISK_TARGET:?YANDEX_DISK_TARGET is required}"
: "${BACKUP_STAGE:?BACKUP_STAGE is required}"

case "$MODE" in
    validate|drill) ;;
    *) echo "Usage: $0 [validate|drill] [snapshot]" >&2; exit 2 ;;
esac

exec "${COMPOSE[@]}" --profile backup run --rm \
    --entrypoint /usr/local/bin/podcast-cutter-restore backup "$SNAPSHOT"
