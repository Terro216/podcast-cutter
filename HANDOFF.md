# Handoff — podcast-cutter, 2026-07-30

Working notes for whoever picks this up. `README.md` describes the project as
it is; this file records **where we stopped, what is already proven, and the one
decision still open**.

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

Still needs doing by hand: **inline mode is off.** Enable via @BotFather →
`/setinline`. The code is ready; until then `@podcast_cutter_bot query` in
other chats does nothing.

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

## 3. The open decision: routing media fetches through a proxy

### The problem, measured

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

### Transport — NOT decided

The user declined to choose until convinced it (a) will not conflict with the
existing VPN/proxy stack, (b) can be moved to another VPN server later, and
(c) will not break anything. Answering that is the next task.

**Inventory of DE, gathered for exactly this question:**

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

1. **The proxy work above** — recovers ~22% of episodes.
2. **Inline mode still off at BotFather** — one manual step.
3. Mini App with a waveform picker — needs a frontend and HTTPS hosting.
4. Caching of directory searches — identical queries each hit the API.
5. Cancel during a cut leaves ffmpeg running.
6. No embedded cover art in clips.
7. Chapter-aware clip boundaries for feeds that publish them.

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

# how much of the directory can production actually fetch
docker compose exec -T -e CONCURRENCY=1 podcast-cutter python - 40 \
  < scripts/check_reachability.py

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
