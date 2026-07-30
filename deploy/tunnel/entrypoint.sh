#!/bin/sh
# Hold an SSH forward to the egress proxy and keep it up.
#
# Direction matters: this dials *out* from production. The obvious alternative —
# a reverse tunnel pushed from the dev box — lands on production's loopback,
# which a bridge-network container cannot reach, and fixing that means either
# host networking for the bot or GatewayPorts in production's sshd. Dialling out
# puts the forward's near end inside this compose network instead, where the bot
# already is, and changes nothing about either host.
set -eu

: "${TUNNEL_HOST:?TUNNEL_HOST is required}"
TUNNEL_PORT="${TUNNEL_PORT:-44222}"
TUNNEL_USER="${TUNNEL_USER:-me}"
TUNNEL_REMOTE="${TUNNEL_REMOTE:-127.0.0.1:3128}"
TUNNEL_LISTEN_PORT="${TUNNEL_LISTEN_PORT:-3128}"
SSH_KEY="${SSH_KEY:-/keys/tunnel_key}"
KNOWN_HOSTS="${KNOWN_HOSTS:-/keys/known_hosts}"

if [ ! -r "$SSH_KEY" ]; then
    echo "tunnel: no readable key at $SSH_KEY" >&2
    exit 1
fi

# The key is mounted read-only from the host, so its mode is whatever the host
# says. Copy it somewhere we own to guarantee the 0600 OpenSSH insists on.
install -m 700 -d /tmp/ssh
install -m 600 "$SSH_KEY" /tmp/ssh/key

# Pinned host key, no interactive trust-on-first-use: the far end is a fixed
# machine we set this up on, so there is no reason to accept a new key ever.
if [ -r "$KNOWN_HOSTS" ]; then
    install -m 600 "$KNOWN_HOSTS" /tmp/ssh/known_hosts
    HOST_KEY_OPTS="-o StrictHostKeyChecking=yes -o UserKnownHostsFile=/tmp/ssh/known_hosts"
else
    echo "tunnel: no known_hosts at $KNOWN_HOSTS, accepting the key on first use" >&2
    HOST_KEY_OPTS="-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/tmp/ssh/known_hosts"
fi

echo "tunnel: forwarding 0.0.0.0:${TUNNEL_LISTEN_PORT} to ${TUNNEL_REMOTE}" \
     "via ${TUNNEL_USER}@${TUNNEL_HOST}:${TUNNEL_PORT}"

# -M 0 leaves the liveness monitoring to ServerAlive*, which is what a keepalive
#      on a single forward wants; autossh's own monitoring port would need a
#      second forward for nothing.
# GatewayPorts=yes because the listener must accept connections from the bot's
#      container, not just this one's loopback.
# ExitOnForwardFailure=yes so a refused forward — a permitopen that does not
#      match, say — kills the process and Docker restarts it, instead of leaving
#      a connected session with no tunnel behind it.
exec autossh -M 0 -N -T \
    -i /tmp/ssh/key \
    -p "$TUNNEL_PORT" \
    -L "0.0.0.0:${TUNNEL_LISTEN_PORT}:${TUNNEL_REMOTE}" \
    -o GatewayPorts=yes \
    -o ExitOnForwardFailure=yes \
    -o IdentitiesOnly=yes \
    -o BatchMode=yes \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -o TCPKeepAlive=yes \
    $HOST_KEY_OPTS \
    "${TUNNEL_USER}@${TUNNEL_HOST}"
