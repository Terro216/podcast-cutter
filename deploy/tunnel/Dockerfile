# One end of the SSH forward that carries audio fetches to the proxy on the
# dev box. Lives in the bot's compose network, so the bot reaches it by name
# and nothing has to be published on either host.
FROM alpine:3.20

RUN apk add --no-cache autossh openssh-client

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# nc is in busybox; this only checks that the forward is accepting connections
# locally, which is what the bot will try. `docker compose` never gates the bot
# on it — a sick tunnel must degrade the bot to direct fetches, not stop it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD nc -z 127.0.0.1 "${TUNNEL_LISTEN_PORT:-3128}"

ENTRYPOINT ["/entrypoint.sh"]
