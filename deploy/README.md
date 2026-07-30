# The audio detour

Roughly a fifth of the directory is unfetchable from the machine the bot runs
on, and the reason is the source address rather than anything about the request:
`traffic.megaphone.fm` resolves by GeoDNS to an address that silently drops our
packets, and Anchor-hosted feeds answer 403 outright. The same URLs succeed from
the dev box. So audio fetches — and only audio fetches; the directory API and
Telegram are neither blocked nor anybody else's business — can take a detour
through a proxy there.

```
production                                     dev box (egress the CDNs like)
compose project “podcast-cutter”               compose project “media-proxy”
┌──────────────────────────┐                   ┌──────────────────────────┐
│ podcast-cutter           │                   │ tinyproxy                │
│  MEDIA_PROXY=            │                   │  network_mode: host      │
│   http://media-proxy:3128│                   │  Listen 127.0.0.1:3128   │
└───────────┬──────────────┘                   └───────────▲──────────────┘
            │ compose network                              │ loopback only
┌───────────▼──────────────┐   autossh -L, dials out       │
│ media-proxy (sidecar)    │═══════════════════════════════┘
│  profiles: ["proxy"]     │   key restricted to permitopen=127.0.0.1:3128
└──────────────────────────┘
```

## Why this shape

**The tunnel is dialled from production, not pushed from the dev box.** A
reverse tunnel lands on production's loopback, which a bridge-network container
cannot reach; fixing that needs either host networking for the bot or
`GatewayPorts` in production's sshd. Dialling out puts the near end inside the
compose network, where the bot already is, and changes nothing about either
host's configuration.

**Nothing is published anywhere.** The proxy listens on the dev box's loopback.
The only way in is an SSH forward, authorised by a dedicated key that carries

```
restrict,port-forwarding,permitopen="127.0.0.1:3128",command="/bin/false"
```

so it cannot allocate a pty, forward an agent, open any other forward, or run a
command. Each of those four pieces earns its place: `restrict` turns everything
off, `port-forwarding` turns back on the one thing we need, `permitopen` narrows
it to the proxy — and `command` is what actually refuses a shell, because
`restrict` on its own does not. That last one is worth testing rather than
trusting: `ssh -i tunnel_key … me@<dev box> id` must print nothing.

No new port on any public interface, no ACL to get wrong, no proxy password
(which ffmpeg's CONNECT path would not reliably send anyway).

**It is additive on the dev box.** Its own compose project, its own container,
no iptables or routing changes, nothing shared with the VPN stack: `host`
networking publishes no ports, so no NAT rules are added, and the Amnezia
containers serve VPN clients rather than redirecting the host's own traffic.
`host` networking is also what makes the detour *work* — the proxy resolves
GeoDNS names exactly as the host does, where the answers are the good ones.

**A broken detour costs nothing.** `MEDIA_PROXY` unset is the bot's default and
its behaviour is byte-identical to before. Configured but unreachable: the bot
notices at the transport level, logs it, journals it, marks the proxy down for a
cooldown, and fetches directly. See `podcast_cutter/proxy.py`.

## Bringing it up

On the dev box — note `ldocker`, the *local* docker; plain `docker` in this
project forwards to production:

```shell
ldocker compose -f deploy/media-proxy/docker-compose.yml up -d --build
curl -x http://127.0.0.1:3128 -s -o /dev/null -w '%{http_code}\n' \
  -r 0-1 https://traffic.megaphone.fm/
```

Then the key. Generated on the dev box, private half copied to production,
public half authorised for one forward and nothing else:

```shell
ssh-keygen -t ed25519 -N '' -C 'podcast-cutter tunnel' -f /tmp/tunnel_key
printf 'restrict,port-forwarding,permitopen="127.0.0.1:3128",command="/bin/false" %s\n' \
  "$(cat /tmp/tunnel_key.pub)" >> ~/.ssh/authorized_keys

ssh big-one 'mkdir -p ~/.podcast-cutter/tunnel && chmod 700 ~/.podcast-cutter/tunnel'
scp /tmp/tunnel_key big-one:~/.podcast-cutter/tunnel/tunnel_key
ssh-keyscan -p 44222 -H <dev box address> > /tmp/known_hosts   # pin it
scp /tmp/known_hosts big-one:~/.podcast-cutter/tunnel/known_hosts
shred -u /tmp/tunnel_key /tmp/tunnel_key.pub
```

Finally, in production's `.env`:

```
MEDIA_PROXY=http://media-proxy:3128
MEDIA_PROXY_MODE=fallback
TUNNEL_HOST=<dev box address>
COMPOSE_PROFILES=proxy
```

and `docker compose up -d --build`. The log says which way it went:

```
Media proxy http://media-proxy:3128 reached https://traffic.megaphone.fm/ (200) in 61 ms; mode=fallback.
```

## Moving it to another host

Nothing in the bot's code names a host. Copy `deploy/media-proxy/` to the new
machine, bring it up, add the same public key line to its `authorized_keys`,
pin its host key, and change `TUNNEL_HOST` in production's `.env`. If the new
machine reaches the proxy some other way — a WireGuard peer, a private network —
drop the sidecar (`COMPOSE_PROFILES=`) and point `MEDIA_PROXY` straight at it.
Either way the bot is unchanged and unrebuilt.

## Turning it off

| how much | what to do |
| --- | --- |
| stop using the proxy, keep it configured | `MEDIA_PROXY_MODE=off` in `.env`, `docker compose up -d` |
| remove the detour from production | drop `COMPOSE_PROFILES=proxy` and `MEDIA_PROXY`, `docker compose up -d` |
| remove the proxy from the dev box | `ldocker compose -f deploy/media-proxy/docker-compose.yml down` |
| revoke access entirely | delete the `permitopen=` line from the dev box's `~/.ssh/authorized_keys` |

## Checking what it earns

```shell
# how much of the directory each route can fetch, from production
docker compose exec -T -e CONCURRENCY=1 podcast-cutter \
  python - 40 < scripts/check_reachability.py
docker compose exec -T -e CONCURRENCY=1 -e MEDIA_PROXY= podcast-cutter \
  python - 40 < scripts/check_reachability.py

# cuts that only happened because of the detour
docker compose exec -T podcast-cutter sqlite3 -header -column \
  /data/podcast_cutter.db \
  "SELECT count(*) FROM events
    WHERE action='cut' AND outcome='ok' AND detail='route=proxy'"
```
