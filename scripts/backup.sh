#!/usr/bin/env bash
# App-aware online backup. Host paths are mounted by Docker because this shell
# can be local while Docker points to production big-one through an SSH shim —
# so this runs the backup where the data is, not where the shell is.
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$REPO_DIR/.env"
COMPOSE=(docker compose --env-file "$ENV_FILE" -f "$REPO_DIR/docker-compose.yml")

[ -f "$ENV_FILE" ] || { echo "No $ENV_FILE" >&2; exit 1; }
# shellcheck source=scripts/lib/compose-env.sh
source "$SCRIPT_DIR/lib/compose-env.sh"
load_compose_env

for name in BACKUP_STAGE BACKUP_PASSWORD YANDEX_DISK_TARGET; do
    [ -n "${!name:-}" ] || { echo ".env is missing: $name" >&2; exit 1; }
done

BUILD=()
if [ "${1:-}" = --build ]; then BUILD=(--build); shift; fi
[ "$#" -eq 0 ] || { echo "Usage: $0 [--build]" >&2; exit 2; }
exec "${COMPOSE[@]}" --profile backup run --rm "${BUILD[@]}" backup
