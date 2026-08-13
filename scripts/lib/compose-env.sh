#!/usr/bin/env bash
# Load values from a Docker Compose env-file without executing it as shell code.
# Callers define ENV_FILE and COMPOSE before invoking load_compose_env.

load_compose_env() {
    : "${ENV_FILE:?ENV_FILE is required}"
    declare -p COMPOSE >/dev/null 2>&1 || { echo 'COMPOSE array is required' >&2; return 1; }

    local dump raw name value
    local -A declared=()
    dump="$(mktemp)"
    chmod 0600 "$dump"
    if ! "${COMPOSE[@]}" config --environment >"$dump"; then
        rm -f "$dump"
        echo "Failed to parse ${ENV_FILE} with docker compose" >&2
        return 1
    fi

    while IFS= read -r raw || [ -n "$raw" ]; do
        raw="${raw%$'\r'}"
        [[ "$raw" =~ ^[[:space:]]*(#|$) ]] && continue
        if [[ "$raw" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*= ]]; then
            declared["${BASH_REMATCH[1]}"]=1
        else
            rm -f "$dump"
            echo "Invalid line in ${ENV_FILE}: ${raw%%=*}" >&2
            return 1
        fi
    done <"$ENV_FILE"

    while IFS='=' read -r name value; do
        [[ ${declared[$name]+yes} ]] || continue
        printf -v "$name" '%s' "$value"
        export "${name?}"
    done <"$dump"
    rm -f "$dump"
}
