# Handoff — podcast-cutter, 2026-07-30

Working notes for whoever picks this up. `README.md` describes the project as
it is; this file records **where we stopped and what is already proven**.

The decision that used to be open — routing audio fetches through a second
egress — is **done and deployed**. §3 now records what was built and measured
rather than what was being considered.

---

## 1. State right now

Deployed and running: **@podcast_cutter_bot** on big-one, container
`podcast-cutter`, healthy, no errors in the log.

Git tree is clean. Recent history, newest first:

| commit | what |
| --- | --- |
| `d9a85b1` | error taxonomy (Blocked/Unreachable/Unreadable/Timeout) + `scripts/check_reachability.py` |
| `cd52f08` | SQLite journal, rotating logs on a volume, `/stats`, retention |
| `967ce12` | interface rebuilt around a screen stack; PTB 21.1.1 → 22.8 |
| `18499e0` | strip source ID3 tags; tolerate quoted env values |
| `7f92218` | the big refactor out of `main.py` + `utils/` |

**448 tests pass, ruff clean.** Verify with the command in §6.

Configured and working: `ADMIN_IDS=87752988` (so `/stats` works), journal at
`/data/podcast_cutter.db`, logs at `/data/logs/bot.log`, both on the named
volume `podcast-data`.

Inline mode is enabled in @BotFather and answering. Note what it searches:
`/search/byperson` matches only people credited in a feed, so anything else —
a topic, a podcast's own name — falls back to a `byterm` podcast search and
the top match's episodes. Both the empty and the failed branch are journalled
as `action=inline` with an `outcome`, so a silent inline is diagnosable from
the journal rather than from guessing.

---

## 2. Read this before touching anything — the topology

Two machines, and confusing them wasted a lot of effort in the last session:

```
DE (this dev box)                 big-one (production)
hostname instance168669…          94.126.204.131
egress 178.17.48.243              ← the bot runs HERE
  shell / my commands run here
  VPN stack lives here (amn0)
```

* `docker`, `npm`, `node`, … are **shims** at `/home/me/bin/remote-shims/*`
  that forward over SSH to `big-one`. So `docker compose ...` acts on
  production. `docker context ls` reports big-one's socket, not DE's.
* `ldocker`, `lnpm`, `lnode`, … are the **local** binaries on DE.
* Files reach big-one via Mutagen. `docker-compose` (v1, hyphen) is *not*
  shimmed — only `docker compose`.
* A plain `curl` in the shell measures **DE's** egress. To measure the bot's,
  run it inside the container: `docker compose exec -T podcast-cutter …`.
* `!`-prefixed commands the user runs land in the same shell, i.e. also DE.

Other traps found the hard way:

* `.env` values are **quoted** (`BOT_TOKEN="…"`). `python-dotenv` strips
  quotes, `docker run --env-file` does not — `config._env()` strips them now.
* Never leave `.env.bak*` lying around; `.gitignore` covers `.env*` since
  `39090f3`, but `git add -A` is used a lot here.
* The Bash tool's safety classifier goes down sometimes. `Read`/`Write`/`Edit`
  keep working; wait and retry rather than working around it.

---

## 3. Routing media fetches through a proxy — built, deployed, measured

### What is running now

| where | what |
| --- | --- |
| DE | `deploy/media-proxy/` — tinyproxy, `network_mode: host`, `Listen 127.0.0.1:3128`. Started with `ldocker compose -f deploy/media-proxy/docker-compose.yml up -d`. |
| DE | one line in `~/.ssh/authorized_keys`: `restrict,port-forwarding,permitopen="127.0.0.1:3128",command="/bin/false"` |
| big-one | sidecar `podcast-cutter-tunnel` (`deploy/tunnel/`), autossh holding `-L 0.0.0.0:3128:127.0.0.1:3128` to DE:44222, behind compose profile `proxy` |
| big-one | key + pinned host key at `~/.podcast-cutter/tunnel/` (outside the repo) |
| big-one | `.env`: `MEDIA_PROXY=http://media-proxy:3128`, `MEDIA_PROXY_MODE=fallback`, `COMPOSE_PROFILES=proxy`, `TUNNEL_HOST=178.17.48.243` |

The tunnel is dialled **from** big-one, which is the one thing that differs from
option A as it was written below: a reverse tunnel lands on big-one's loopback,
which a bridge-network container cannot reach, and fixing that needs either
`network_mode: host` for the bot or `GatewayPorts` in production's sshd.
Dialling out puts the near end inside the compose network, where the bot already
is, and neither host's configuration changes.

`deploy/README.md` has the full rationale, the setup commands, how to move the
proxy to another host, and the rollback table.

### Measured after deployment

```
40 trending episodes, CONCURRENCY=1, from inside the production container:
  through the proxy:  39/39 reachable (100%)
  direct:             35/39 reachable  (90%)
                        3 × ConnectTimeout  traffic.megaphone.fm
                        1 × 403             anchor.fm
```

Same failure signature as the pre-work measurement below; the direct rate reads
better than the earlier 78% only because trending feeds differ run to run. Every
episode that failed direct succeeded through the proxy.

A real cut, end to end, of an episode big-one cannot fetch: a
podtrac → pdst.fm → megaphone chain that times out directly came back as a
30-second `cut.mp3`, 1.2 MB, ffprobe-verified, `route=proxy`.

And the guarantee that mattered most: with the sidecar stopped, the startup
check reports `ConnectError: Temporary failure in name resolution`, the breaker
opens, `routes()` collapses to `('direct',)`, and a cut completes in 4.3 s. A
dead proxy costs the episodes it would have rescued and nothing else.

### What the bot does with it

* `MEDIA_PROXY` empty is the default and means the previous behaviour exactly —
  `subprocess_env` even returns `None` so children inherit the environment
  unchanged. `MEDIA_PROXY_MODE=off` keeps the URL and stops using it.
* `fallback` (default) resolves direct first; the proxy is tried only after a
  connect failure or a 401/403/451. `always` reverses the order.
* The route is chosen once per job by `_resolve_url` and is **sticky** —
  resolving through one route and fetching through another risks a 403 on
  URLs signed for the client that resolved them.
* An attempt with another route behind it gets an 8 s connect timeout instead of
  15 s: megaphone's failure is a silent drop, and the full wait would be most of
  the delay the user sees.
* ffmpeg gets `http_proxy` only. It honours that for `https://` sources via
  CONNECT and ignores `https_proxy` — `test_ffmpeg_itself_honours_http_proxy`
  now pins this against a real proxy, so it is a test rather than a note here.
* A transport failure on the proxy marks it down for 60 s for the whole bot.
* Cuts the detour earned carry `detail='route=proxy'` in the journal; the
  startup check writes an `action='proxy'` row.

### Traps worth knowing

* `restrict` in `authorized_keys` does **not** prevent command execution — it
  disables ptys and forwarding. `ssh -i tunnel_key … id` ran fine until
  `command="/bin/false"` was added. Verified after fixing: no command, no
  forward except `127.0.0.1:3128`, and that one carries traffic.
* tinyproxy 1.11 dropped `StartServers`/`MinSpareServers`/`MaxSpareServers` and
  warns on every start if they are present.
* `network_mode: host` for the proxy is load-bearing, not tidiness: the
  megaphone failure is a GeoDNS answer, so a container resolving through
  Docker's embedded DNS could be handed the same dead address the bot gets.
* Compose interpolates the whole file regardless of active profiles, so
  `${TUNNEL_HOST:?...}` would break `up` for a deployment that wants no proxy.
  The entrypoint checks instead.
* Not decided by measurement: the proxy carries full-episode downloads too when
  the streaming path falls back, so DE's bandwidth is in that path. Rare, and
  the streaming path only pulls bytes around the interval.

### The problem, as measured before the work

`scripts/check_reachability.py`, 40 trending episodes, sequential, with
failures attributed to the host that actually failed:

```
big-one:  31/40 reachable (78%)
  7 × ConnectTimeout  traffic.megaphone.fm   (17.5%)
  1 × 403             anchor.fm
  1 × 403             storage.warroom.org  (via chrt.fm)
```

Then the same 8 failing URLs fetched from DE: **8 of 8 succeeded (206)**.

Two distinct causes, both about *where the request comes from*:

1. **megaphone, 17.5% — the big one.** GeoDNS hands big-one
   `35.229.87.57`; TCP connect to it times out after 8s. From DE the same name
   resolves to `35.186.224.24` and connects in 3.5 ms. Packets are silently
   dropped, it is not an HTTP refusal. Everything fronted by `podtrac.com`,
   `pscrb.fm`, `pdst.fm`, `prfx.byspotify.com` redirects to megaphone, so this
   one host accounts for most of the loss.
2. **403 blocks, 5%.** anchor.fm (Spotify's free host) and warroom refuse
   big-one's address outright. Extracting the CloudFront URL embedded in the
   anchor.fm path does **not** help — that 403s from big-one too.

### Proven facts to build on

| claim | how it was verified |
| --- | --- |
| ffmpeg honours **`http_proxy`** for `https://` URLs, via CONNECT | ran a logging CONNECT proxy in the container; it saw `CONNECT media.blubrry.com:443` and ffprobe succeeded |
| ffmpeg **ignores `https_proxy`** | same test with only that var set: 0 proxy requests, went direct |
| big-one reaches DE on 443, 80, 44222 | TCP connect, ~40 ms each |
| big-one is **not** on DE's VPN | `172.29.172.1:443` times out |
| DE → big-one SSH works | `~/.ssh/config` host `big-one`, port 44222, key present |
| DE fetches everything big-one cannot | the 8/8 test above |

### Design agreed for the bot side

Proxy **only the audio fetches**. Podcast Index and Telegram traffic stay
direct: they are not blocked, and the bot token has no business travelling
through another host.

* New setting `MEDIA_PROXY` (empty = today's behaviour, so it is inert until
  configured). One URL, nothing else — moving to a different VPN server is a
  one-variable change, which is what the user asked for.
* `audio._resolve_url` and `audio._download` pass `proxy=` to their httpx
  clients.
* ffmpeg/ffprobe get `http_proxy` in their environment (**not** `https_proxy`).
* **Fall back to direct if the proxy itself is unreachable.** This is the
  "nothing breaks" guarantee: a dead proxy must not take the working 78% down
  with it. Log it loudly and journal it.
* Log proxy reachability once at startup so a silently broken proxy is visible.

### Transport — decided: a variant of A

Kept below because the reasoning still applies and B and C remain the
alternatives if the SSH forward ever becomes the wrong shape.

B was rejected because a public port needs an ACL or a password, and ffmpeg's
CONNECT path cannot be relied on to send proxy credentials. C was rejected
because it means editing the AmneziaWG config on a live VPN server with
clients — the one thing that would actually touch the existing stack.

**Inventory of DE, gathered for exactly this question** (re-verified during the
work: 3128 free, `ip rule` still empty, sshd on 44222 with password auth off):

```
containers (ldocker ps):
  fake_site_caddy   caddy:alpine
  gatus             127.0.0.1:8082
  amnezia-awg       0.0.0.0:33007/udp
  amnezia-awg2      0.0.0.0:39199/udp
  amnezia-openvpn   0.0.0.0:45764/udp
  amnezia-ipsec     0.0.0.0:500/udp, 4500/udp
  amnezia-dns       53

TCP ports already listening on DE:
  53 80 443 1015 1188 2019 2053 2055 2096 4430
  8080 8082 8443 9091 11111 20530 44222 62789

interfaces: ens3 178.17.48.243/32 · docker0 172.17.0.1 · amn0 172.29.172.1/24
routing: no policy rules, single default via ens3 — the Amnezia containers
         serve VPN *clients*, they do not redirect DE's own traffic
```

So a new proxy on DE would be additive: its own container, a free TCP port,
no routing or iptables changes, nothing shared with the Amnezia stack. That
still needs stating in terms the user accepts, plus a port choice (e.g. 3128 —
free) and a rollback story.

The three options as presented:

* **A — SSH reverse tunnel (was the recommendation).** Proxy binds
  `127.0.0.1:3128` on DE; DE holds `ssh -R 3128:127.0.0.1:3128 big-one`.
  Nothing is exposed publicly, reuses the existing key. Needs a systemd unit
  on DE to keep the tunnel up, and the bot container on `network_mode: host`
  to reach big-one's loopback (or `GatewayPorts clientspecified` in big-one's
  sshd, which is a change to production sshd — prefer the former).
* **B — public port with a source-IP ACL.** Fastest to wire; one wrong ACL
  and it is an open relay. DE already exposes ports, so it fits the posture.
* **C — WireGuard peer.** big-one joins DE's AmneziaWG. Cleanest long-term and
  reusable for other services, most setup.

Portability holds for all three: the bot only ever sees `MEDIA_PROXY`.

---

## 4. Deliberate decisions worth not re-litigating

* **No `ConversationHandler`.** A screen stack in `Session` plus an explicit
  `awaiting` field. Every update reaches one of two routers, so nothing can
  land in a state with no handler — the old code stranded users exactly that
  way. See `states.py`, `handlers.py`.
* **No `PicklePersistence`.** Pickling `Session` ties on-disk data to the class
  layout; adding a field to `Episode` would break old records at request time,
  and PTB refuses to start on a corrupt persistence file. Sessions are a
  two-minute working set. Only the recent-episode list is worth keeping, and it
  lives in SQLite with a schema we control.
* **`-map_metadata -1` on every cut.** Feeds ship enormous ID3 tags — the Lex
  Fridman feed carries 18 MB — and `-c copy` reproduced the whole thing, so a
  30-second clip weighed 20 MB. Now 352 KB, with our own tags.
* **Output container follows the source codec.** AAC lands in `.m4a`; writing
  it to `.mp3` with `-c copy` is what ffmpeg always rejects, and it used to
  fail every non-MP3 podcast.
* **Lossless codecs are re-encoded, not copied.** Rare in feeds, oversized for
  Telegram, and a copied FLAC keeps the source's total-sample count so players
  report the wrong length.
* **`-user_agent` only for http(s) sources.** With a local file ffmpeg aborts
  with "Option user_agent not found", which silently killed the entire
  download-then-cut fallback.
* **Cut output is verified with ffprobe.** ffmpeg exits 0 having written an
  unplayable fragment when a source is seek-hostile; the check sends such
  attempts down the fallback path instead of to the user.
* **Journal outcomes are stable `code` strings**, not exception class names, so
  SQL over `events` survives refactors.

---

## 5. Known gaps, roughly by value

1. **Inline mode still off at BotFather** — one manual step.
2. Mini App with a waveform picker — needs a frontend and HTTPS hosting.
3. Caching of directory searches — identical queries each hit the API.
4. Cancel during a cut leaves ffmpeg running.
5. No embedded cover art in clips.
6. Chapter-aware clip boundaries for feeds that publish them.
7. The audio detour has no monitoring beyond the startup check and the journal.
   `gatus` already runs on DE and could watch the proxy directly.

---

## 6. Commands you will want

```shell
# tests + lint (runs in the image, on big-one)
docker build -q -t podcast-cutter:test . && \
  tar cf - tests pyproject.toml scripts main.py | \
  docker run --rm -i --user root -e HOME=/tmp podcast-cutter:test bash -c '
    cd /app && tar xf - && pip -q install pytest pytest-asyncio ruff
    ruff check . --output-format concise && pytest -q'

# deploy
docker compose up -d --build && docker compose logs -f

# how much of the directory can production actually fetch, per route
docker compose exec -T -e CONCURRENCY=1 podcast-cutter python - 40 \
  < scripts/check_reachability.py
docker compose exec -T -e CONCURRENCY=1 -e MEDIA_PROXY= podcast-cutter \
  python - 40 < scripts/check_reachability.py

# the DE half of the detour (ldocker — the local one)
ldocker compose -f deploy/media-proxy/docker-compose.yml ps
docker compose logs media-proxy | tail

# the journal
docker compose exec -T podcast-cutter sqlite3 -header -column \
  /data/podcast_cutter.db \
  "SELECT outcome, count(*) FROM events WHERE action='cut' GROUP BY outcome"

# live API credentials check
docker compose exec -T podcast-cutter python - "Lex Fridman" < scripts/check_api.py
```

A real end-to-end cut (mp3 + voice note) can be exercised the way the last
session did it: `cut_episode` against a live enclosure inside the container.
`tests/test_cut_integration.py` covers the same paths against a local HTTP
server, so prefer that for routine work.
