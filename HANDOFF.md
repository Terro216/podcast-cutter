# Handoff — podcast-cutter, 2026-08-12

> **Runtime note, 2026-08-13.** The 13 August audit's pre-deploy checklist has
> been carried out: `podcast-data` snapshotted (`/data/backups/predeploy-*.db`,
> quick_check ok), the test/lint suite rerun green (775 pass, ruff clean), and
> the `harden-source-urls` branch deployed with `docker compose up -d --build`.
> Verified after: the running image contains the `-max_redirects` guard
> (`audio.py`) and the ASR-lock fix (`asr.py`), the bot came up as
> @podcast_cutter_bot through the `media-proxy`, dense search and ASR ready, no
> errors. The audit's **no-healthcheck** gap is now closed: a heartbeat-based
> Docker healthcheck is live (`e3b5785`), container reports `healthy`. Transient
> `Bad Gateway` from Telegram is handled by PTB's retries. Offsite backups to
> Yandex Disk are **live** as of
> 2026-08-13: first snapshot `c01f794d` at `yadisk:/backups/podcast-cutter`,
> restore round-trip validated, three systemd timers enabled (daily 04:57,
> weekly Sun 06:42, monthly 1st Sat) — see `docs/backup-policy.md`. The restic
> password is in `.env`; **its Bitwarden copy is still owed.**

Working notes for whoever picks this up. `README.md` describes the project as
it is, `ROADMAP.md` where it is going and why; this file records **where we
stopped and what is already proven**.

Both decisions that used to be open are closed. Routing audio through a second
egress is done and deployed (§3). Searching an episode by what was said is
built, deployed and warmed (§3a).

---

## 1. State right now

Deployed and running: **@podcast_cutter_bot** on big-one, container
`podcast-cutter`, healthy, 0 restarts, no errors in the log.

Recent history, newest first, on branch **`harden-source-urls`**:

| commit | what |
| --- | --- |
| _this_ | the listening queue moves into SQLite; a wait has a number |
| `e3b5785` | a Docker healthcheck that catches a wedged event loop |
| `c6eaf56` | offsite backup to Yandex Disk, the arr/vaultwarden shape |
| `e3bef37` | a transcription timeout no longer frees the model mid-decode |
| `7483267` | ffmpeg no longer follows a redirect the address check never saw |
| `fd7efcc` | small/speechkit baselines guarded by CI; the one honest 6.2% false hit |
| `b414df8` | the small and speechkit fixtures; make_fixtures passes the basket's language |
| `1ccc566` | SpeechKit as the second recogniser behind the one-method door |
| `91d5f3d` | an expired session says so, and `/reset` exists |
| `7ce2d4f` | progress bars that count the right thing, and a bar for indexing |
| `e4e3f7c` | feeds pagination actually fetches the next page |
| `df40650` | hybrid retrieval: search by meaning, refuse by measurement |
| `b3af46d` | concurrent updates, per-user budgets, the transcription queue |
| `1d255b9` | a transcription timeout journals as failure; README layout; bot-side findings |
| `f9b84ce` | clip placement on the phrase; distinct moments no longer merge |
| `6c02926` | an extreme compression ratio convicts on its own (the §16 decoder loop) |
| `c02c7d2` | the answer key: 104 queries, first baselines, the base-model price measured |
| `603dae4` | the regression guard could not fire below one query's wobble; fixed pre-baseline |
| `116197d` | the eight reference transcripts |
| `44701e7` | evaluation baskets: how often the search is wrong, and which way |
| `a866f4b` | bench what a Whisper model costs on this host |
| `299b2e2` | place the clip on the spoken word; quote the match back |
| `4652af5` | real progress, estimate and rotating notes while transcribing |
| `8360846` | `TELEGRAM_PROXY` — this host can no longer reach Telegram |
| `ce3987d` | the search UI: ask a phrase, get moments, open the editor |
| `ca1e13e` | the engine: transcribe, judge, window, index, search |
| `dccbf01` | `ROADMAP.md` |
| `b94c57c` | bound where an episode URL may point |

**792 tests pass, ruff clean** (8 hybrid basket rows skip unless
`EMBED_MODEL_DIR` points at the converted model — §6 has the command).
The answer key exists, the baselines are committed for all four transcript
variants, and the model comparison table is done — see §3b.

Two things about the deployment that were not true a week ago:

- The image is **1.51 GB**, up from 930 MB: faster-whisper pulls ctranslate2,
  onnxruntime, av, tokenizers and numpy. Model weights are *not* in it — they
  live on the volume at `/data/models` (142 MB) and survive redeploys.
- The container is pinned with `cpuset: "0-7"`. Checked against `lscpu -e` on
  big-one: those are cores 0–7 of socket 0, node 0, and their SMT siblings are
  16–23. Keep `ASR_THREADS` equal to the width of that set.

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

## 3a. Searching an episode by what was said — built, deployed, warmed

The full design and the reasoning are in `ROADMAP.md` §15; this is what a
maintainer needs in front of them.

**Shape.** `asr.py` is a one-method recogniser interface with faster-whisper
behind it. `transcripts.py` is everything between recognised speech and an
answer — quarantine, 30 s windows at a 15 s stride, clustering, placement —
and holds no model, so it runs in milliseconds. `indexer.py` is the pipeline
and the search. `listening.py` is the durable queue in front of it — one
episode at a time, from a line kept in SQLite. `store.py` gained schema 3.

**A transcript belongs to the audio, not the episode.** Its identity is
`(source_sha256, backend, model, chunker_version)`. Feeds insert
advertisements dynamically, so an episode can serve different bytes next month
and timestamps taken against the old ones would cut an advert.

**Measured on the production host**, `base`, 8 pinned cores, a 53-minute
Russian episode: **291 s, RTF 0.09**, 212 windows, nothing quarantined. That
episode is already in the production database under its real id
(`57684857183`), so a first user opening it gets an instant search.

**Three defects found by using it, all worth not reintroducing:**

1. Searching «нейросети» found nothing in an episode about neural networks. The
   recogniser had written «нейросетей» and never the exact form; FTS5 matches a
   token literally. Hence the lemma column — and note pymorphy3 *guesses* at
   words it does not know rather than passing them through, which is why the
   surface-form index sits behind it.
2. The clip opened twenty seconds early: the window was found on lemmas, but
   `locate_phrase` compared surface forms and so could not find the very word
   that produced the hit. Both match lemmas now.
3. Answers quoted the opening of a window rather than the text around the
   match, and buttons cut it mid-word. Quotations live in the message; buttons
   only number and stamp.

**Traps.** `esc()` runs `one_line()`, which strips and collapses whitespace —
a separator carried inside a fragment is eaten, and the parts arrive glued
("ту жевышкуна"). Join outside the escaping. And an edit throttle that swallows
a *stage change* looks like a hang: throttle within a stage, force on change.

**Off switch.** `ASR_ENABLED=false` stops transcription without stopping the
bot; a missing recognition library does the same by itself, since
`build_indexer` returns `None` rather than raising.

---

## 3b. Measuring the search — running, baselines committed

`ROADMAP.md` §16 has the design and the measurements. This is the operating
picture.

**The answer key is written**: 36 positive + 16 negative per language, all
four classes, drafted from the reference transcripts and text-verified against
them. What CI now holds every push against:

```
run                hit@1  hit@3    err  false
en/reference       63.9%  66.7%   1.8s   0.0%
en/reference+e5    69.4%  72.2%   2.6s   0.0%
en/asr             52.8%  55.6%   1.9s   0.0%
en/asr+e5          61.1%  69.4%   3.5s   0.0%
en/small           55.6%  58.3%   1.5s   0.0%
en/small+e5        61.1%  66.7%   1.9s   0.0%
en/speechkit       55.6%  58.3%   2.8s   0.0%
en/speechkit+e5    63.9%  66.7%   3.6s   6.2%   <- see below
ru/reference       61.1%  66.7%   1.0s   0.0%
ru/reference+e5    66.7%  69.4%   1.0s   0.0%
ru/asr             36.1%  36.1%   1.6s   0.0%
ru/asr+e5          44.4%  44.4%   1.4s   0.0%
ru/small           50.0%  50.0%   1.1s   0.0%
ru/small+e5        52.8%  52.8%   1.1s   0.0%
ru/speechkit       44.4%  52.8%   1.1s   0.0%
ru/speechkit+e5    44.4%  55.6%   1.1s   0.0%
```

**The comparison table is complete** (2026-08-12): all four variants of all
eight episodes are committed and CI guards every row above. What it says:

* **Russian is where recognisers differ.** small returns half of what `base`
  loses against the reference (36.1 → 50.0 of 66.7), SpeechKit one query more
  (52.8) — and they miss *different* quotes (small garbles «пересказывание в
  Википедии», SpeechKit «опростоволоситься» and, surprisingly, «штраф 100
  миллионов»), so neither dominates. English does not care: all three land on
  58.3 lexical, and `base`+e5 is even a query ahead of both.
* **SpeechKit's real wins are wall-clock and CPU, not quality**: RTF 0.05
  measured over 6.2 h (a 50-min episode in ~3 min — faster than `base`'s 4),
  zero local cores, ₽30.30/episode standard rate. small costs RTF ~0.18 of
  local CPU and nothing else.
* **The first false hit ever measured**: «emotional intelligence» (tw-n2)
  against en/speechkit+e5 clears the dense-refusal conjunction — sim 0.845
  vs the 0.84 floor, margin 0.065 vs 0.05 — on a garbled window about people
  crying. Both tightenings were *measured before being declined*:
  MIN_SIMILARITY 0.85 costs 1–3 hits in every EN hybrid row (the e5 gains
  live in the 0.84–0.85 band); MIN_MARGIN 0.07 costs ~1 hit in every hybrid
  row of *both* languages. The pair sits on the Pareto frontier, so the
  honest 6.2% is committed as the en/speechkit+e5 baseline instead, with the
  note in `en.yaml`. Recalibrate before ever shipping speechkit for English.
* **No user-facing «быстро/поточнее» switch**: the two candidates are one
  query apart on RU and identical on EN — a button between them would be
  choice without difference. The supported move, when wanted, is
  `ASR_BACKEND=speechkit` (or `auto`) for RU quality at better-than-base
  latency and zero CPU — blocked on the §4 spend ceiling (ROADMAP) and a
  deliberate deploy decision, not on any further measurement.

The `+e5` rows are hybrid retrieval (`ROADMAP.md` §16a): lexical + dense over
`multilingual-e5-small`, fused by reciprocal rank, with a refusal floor
measured against the negatives. They run wherever the converted model exists.

What the numbers already say: `base` costs Russian **30 points of hit@3**
(66.7 → 36.1) and English only 11 — the model, not the retriever, is the
Russian bottleneck. The `meaning` class is 0% across the board, which is the
embeddings ticket (§11 step 3) priced before it is built. Negatives are clean
everywhere. One drafting lesson is recorded in `ru.yaml`: «данные» failed as a
negative because its *lemma* is «данный» and every hour of speech contains «в
данном случае» — a negative has to differ by lemma, not by surface form.

**Still owed on the key itself:** the by-ear pass. Every timestamp was checked
against the reference *text*, nobody has yet listened to ±30 s around each
answer (`scripts/draft_queries.py --verify` prints the listening list), and
`annotation.fully_corrected` is still empty. The basket yamls say
`method: text-verified` until that happens.

**Shape.** `podcast_cutter/evals.py` holds the query classes, the scoring and
the runner; `evals/baskets/{ru,en}.yaml` hold the queries and the episodes;
`evals/fixtures/` holds committed transcripts; `tests/test_baskets.py` runs the
whole thing on every push. `scripts/make_fixtures.py` produces the fixtures and
`scripts/draft_queries.py` helps write and then check the answer key.

**Both runs are offline.** Searching a transcript is milliseconds; *producing*
one is the cost. So both the reference (`large-v3`) and the shipped model's
output (`base`) are committed, and CI reports the gap between them rather than
somebody occasionally remembering to measure it. The consequence to remember:
**changing `ASR_MODEL` makes the `asr` fixtures stale**, and nothing detects
that automatically — regenerate them.

**A basket asserts no regression, not a quality bar.** Each carries a
`baseline:` block of the numbers it last produced. A metric that moves the
wrong way by more than one query's wobble fails the test; one that improves is
re-committed by hand, which makes that block's git history the record of
whether the search is getting better.

**Measured, so the plan could stop guessing** — 180 s RU sample, int8, socket 1
(`--cpuset-cpus 8-15 --cpuset-mems 1`), eight physical cores: `base` RTF 0.079,
`medium` 0.665, `large-v3` 1.252. `base` reproducing §3's 0.07–0.09 is what
makes the other two trustworthy.

The real pass then came in at an aggregate **RTF 1.068** — 6.13 h of audio in
6.54 h — so the short sample was 17% pessimistic, denser speech than a whole
episode where VAD drops the pauses. Russian ran ~8% slower than English.

**Do not move this to a laptop.** An M2 Pro did the same episode at RTF 1.06
against big-one's 1.10: four percent, not the several times the plan assumed.
CTranslate2's int8 kernels are tuned for x86 and its CPU path ignores Apple's
accelerators. Run it here, where the audio is already cached in the
`podcast-asr-bench` volume, with `--cpu-shares 256` — `cpuset` confines the
container to socket 1 but does *not* reserve it, so the low share is what
actually keeps a Jellyfin transcode ahead of an overnight eval.

**Making the reference fixtures off-host.** The pass does not need the bot's
dependency set — no `poetry install`, no `python-telegram-bot`. Verified in a
bare `python:3.12-slim`:

```shell
brew install ffmpeg                       # or apt
pip install faster-whisper httpx python-dotenv pymorphy3 pyyaml
python scripts/make_fixtures.py evals/baskets/ru.yaml evals/baskets/en.yaml \
    --variant reference --model large-v3 --threads 10
```

It downloads the episodes itself and writes into `evals/fixtures/`. Both
variants of one episode must come from the same recording or the comparison is
partly between two recordings, so each fixture records the **download's**
SHA-256 and the script refuses to write one that disagrees with its sibling.

Hash the download, never the decode. The first attempt hashed the decoded PCM
and failed on episode one of a laptop run: macOS ffmpeg and the container's
7.1.5 produce different PCM from an identical mp3. big-one re-downloading and
re-decoding reproduced its own hash exactly, which is what pinned the
difference on the decoder rather than on the feed.

**Traps.**

* Run benches and fixture jobs on **socket 1**. Production is pinned to
  `cpuset: "0-7"`, and `--cpus` is a CFS quota that lets threads wander across
  sockets onto SMT siblings and remote memory — which is what made §3's
  thread-scaling conclusion unsupportable in the first place (§13.1).
* A container writing into the repo writes as root; the tree is uid 1001.
  `chown -R 1001:1001 /app/evals` at the end of the command, or Mutagen carries
  root-owned files back to DE.
* Output from a long `docker run` through the SSH shim buffers and may not
  reach the terminal. `docker logs <id>` shows it immediately.
* pyyaml is a **dev** dependency. The bot never loads a basket, `evals.py`
  imports yaml lazily, and the test command in §6 installs it.

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

1. **The answer key has not been listened to.** It is written, text-verified
   and green in CI (§3b), but the ±30 s by-ear pass and the two
   fully-corrected episodes from §16's method have not happened. Until then a
   reference timestamp is only as good as large-v3's text. `scripts/
   draft_queries.py --verify` prints exactly what to listen to.
1a. **A full review (2026-08-12) left documented defects in the search path.**
   In value order: ~~(a) `cluster()` compares window *edges*, not moment
   positions~~ and ~~(b) `locate_phrase` places the clip on the first
   occurrence of *any* query word and searches quarantined utterances~~ —
   **both already fixed in `f9b84ce`** (cluster compares starts; placement
   filters to indexable utterances and to words ≥3 chars). The `ls-t1`
   ranking-3rd symptom attributed to (a) was **misdiagnosed**: a 2026-08-13
   diagnostic showed clustering working (20 raw hits → 15 distinct moments),
   and the two top moments it "misses" (42:15, 46:46) are *real* «Кофлан»
   mentions — the word is spoken 13× in the reference but `ls-t1`'s `at:`
   lists only 8. That is answer-key incompleteness (the owed by-ear pass,
   item 1), not a search bug. Still genuinely open: ~~(c) an ASR timeout
   releases `LocalWhisper._lock`
   while the worker still decodes~~ **fixed** (`e3bef37`): the lock is held
   until the worker thread actually finishes, so a post-timeout retry waits
   rather than starting a second decode on the same model; a test drives the
   sequence; (d) `NORMALIZER_VERSION` is written to the row but never compared
   at search time, so bumping it (or losing pymorphy3 in one environment)
   silently splits the lemma index and the lemma query into different
   languages; (e) `Indexer.transcript_id`'s episode-id fast path never
   re-checks the audio hash, which is the one place the dynamic-ad guarantee
   does not hold end-to-end — note the honest fix (re-fetch + re-hash every
   search) would cost the instant-search property, so this is a tradeoff to
   decide, not a bug to squash; (f) two episode ids serving identical bytes
   can race into an `IntegrityError` after a full transcription; ~~temp files
   leak on the timeout path~~ **fixed** in `e3bef37` (the decoded WAV is
   unlinked on the timeout path too). Fix (a) and (b) next — the baskets will
   show both moving.
2. **English morphology — the lemma column is inert, not inaccurate.** Checked
   rather than assumed: `lemmatize("proteins folding rapidly")` returns the
   string unchanged, and so do `investors`, `companies`, `studies`. pymorphy3's
   guesser never fires on Latin script, so both indexed columns match English
   literally and `protein` does not find `proteins`. FTS5 ships `porter`, but a
   tokenizer is per table, so the fix is another column or another table.
   Deliberately still open: the EN basket exists to price it first.
3. ~~**A decoding loop survives quarantine.**~~ **Fixed, after the baskets
   priced it**: a compression ratio beyond twice the threshold now counts as
   a second signal (`runaway`), so the 448-character «генегенеген…» (cr
   24.08) and lora-spies' «бззззз…» (cr 6.06) are excluded. The basket run
   after the fix moved **no metric in any of the four runs** — the change
   removed only hallucinated windows, and that is now a demonstrated fact
   rather than a hope. `CHUNKER_VERSION` went 1 → 2, so production's warmed
   transcripts are stale by design: the first search against each re-runs
   transcription once.

3a. **The same review found bot-side defects.** (a) is fixed: the
   application now runs `concurrent_updates(32)`, the busy flag is claimed
   before the first await in both heavy handlers (a test holds two
   simultaneous taps against it), transcription has its own single slot
   apart from the cut pool with a bounded queue (`MAX_ASR_QUEUE`), and
   per-user budgets guard input, cuts and first listens —
   `RATE_INPUT_PER_MINUTE` / `RATE_CUTS_PER_HOUR` / `RATE_ASR_PER_DAY`,
   0 = off, admins exempt, refusals journalled as `action=limit`. Still
   open from the same review: (b) the SSRF guard checks addresses before
   the cut. The **redirect half is fixed** (`7483267`): `-max_redirects 0`
   on every remote ffmpeg/ffprobe input, so a host that answers resolution
   politely can no longer 302 ffmpeg into `169.254.169.254` after the check
   passed — reproduced on ffmpeg 7.1.5 (a default ffprobe *did* connect to
   the metadata address) and pinned by a test that probes a redirecting URL
   directly. **Still open: DNS rebinding** — the same host resolving public
   for the guard and private for ffmpeg's own second resolution; closing it
   needs the validated address pinned into the fetch, fragile for https, so
   it is a separate phase. (c) ~~feeds pagination re-rendering page 1~~ — fixed: a page
   flip on FEEDS now fetches that page through `_search_feeds`. (d)
   `‹ Back` restores a prompt screen without restoring `awaiting`, so
   typing into a restored "send a phrase" prompt runs a podcast-name
   search instead. (e) Typing on GLOBAL/RECENT sets `episode_filter`, which
   those screens ignore. (f) ~~the progress bar rendering seconds as
   kilobytes~~ — fixed; the bar now takes a unit renderer, and the
   embedding stage got a real bar of its own. (g) `MAX_CUT_SECONDS` below 60
   passes validation but the default clip is a fixed 60. (h)
   ~~`sweep_work_dir` removes only `cut-*`, so a crash mid-transcription
   leaks an `asr-<id>` directory with a full episode in it.~~ **Fixed** with
   the SQLite queue: the sweep covers `asr-*` too, which also stops a resumed
   job from inheriting a half-finished download at the same path.
4. ~~**Queues and abuse limits — largely done, two pieces remain.**~~
   **Done.** Per-user budgets on input/cuts/first-listens exist (see 3a),
   inline is covered, transcription has its own slot and refuses when full.
   The last two pieces are now closed: the queue lives in `asr_jobs` in
   SQLite, drained by a single worker (`listening.py`), and a waiting search
   is told `2nd in line` rather than «queued». Three things about it worth
   not rediscovering:
   - **The line is of episodes, not of people.** Ten people wanting one
     episode are one job and ten waiters, so a position number counts
     episodes ahead and `MAX_ASR_QUEUE` caps episodes — joining a queued
     episode is free, because it adds no work.
   - **No `SCHEMA_VERSION` bump.** `_migrate` rebuilds `_DERIVED_TABLES`,
     which includes `transcripts`; bumping 4→5 to add an unrelated table
     would have thrown away every warmed transcript on production. The new
     table arrives through `CREATE TABLE IF NOT EXISTS` instead, and it is
     deliberately *not* in `_DERIVED_TABLES` — a future bump must not drop
     the pending work this exists to protect.
   - **A restart resumes the transcript, not the screen.** Sessions are not
     persisted (§4), so a job that outlives its request finishes and sends a
     message with a `?start=ep_…` link; the search after it is instant. Two
     failed attempts and an episode is given up on, so one bad episode
     cannot become a boot loop that re-downloads it every start. Still open
     here: an **ETA** beside the position, which needs per-length history the
     queue does not keep, and LRU eviction of old transcripts.
5. ~~**Backups.** Nothing is backed up.~~ **Done and live (2026-08-13).**
   Encrypted restic → rclone-native-yandex, the cinemarr/vaultwarden shape:
   the SQLite database (`.backup`, verified against the live WAL, quick_check)
   snapshotted daily to `yadisk:/backups/podcast-cutter`, weekly prune/check,
   monthly read-data check + restore drill. Code in `backup/` and `scripts/`,
   timers in `deploy/systemd/`, full policy in `docs/backup-policy.md`. The
   Yandex token is the one already shared across stacks — re-authorising would
   have invalidated it for cinemarr/vaultwarden, so it was reused, not
   re-issued. **Owed:** the restic password's Bitwarden copy.
6. **Avatar and inline placeholder** — the last two things only @BotFather can
   set (`/setuserpic`, `/setinline`); commands and both descriptions are
   published from `_on_startup` and overwrite anything set there by hand.
7. `MAX_CUT_SECONDS` is still 900. A fifteen-minute extract is hard to call a
   citation; see `ROADMAP.md` §13.4.
8. Caching of directory searches — identical queries each hit the API.
9. Cancel during a cut leaves ffmpeg running.
10. Chapter-aware clip boundaries; embedded cover art in clips.
11. The audio detour has no monitoring beyond the startup check and the
    journal. `gatus` already runs on DE and could watch the proxy directly —
    and it now matters more, because Telegram goes through the same tunnel.

---

## 6. Commands you will want

```shell
# tests + lint (runs in the image, on big-one).
# pyyaml is dev-only — the baskets need it, the bot never loads one.
docker build -q -t podcast-cutter:test . && \
  tar cf - tests pyproject.toml scripts main.py podcast_cutter evals | \
  docker run --rm -i --user root -e HOME=/tmp podcast-cutter:test bash -c '
    cd /app && tar xf - && pip -q install pytest pytest-asyncio ruff pyyaml
    ruff check . --output-format concise && pytest -q'

# how expensive is a model on this host? Socket 1, because production is
# pinned to socket 0 — see the cpuset note in §1.
docker run --rm --user root --cpuset-cpus 8-15 --cpuset-mems 1 \
  -v podcast-asr-bench:/bench -e HF_HOME=/bench/hf \
  -v /home/me/server/projects/podcast-cutter:/app -w /app \
  --entrypoint python podcast-cutter-podcast-cutter:latest \
  scripts/bench_asr.py --sample /bench/sample_ru_180.wav \
  --models base,large-v3 --threads 8

# the basket fixtures. `asr` is ~20 min per basket, `reference` is overnight.
# Resumable: an episode whose fixture exists is skipped.
docker run --rm --user root --cpuset-cpus 8-15 --cpuset-mems 1 \
  -v podcast-asr-bench:/bench -e HF_HOME=/bench/hf \
  -v /home/me/server/projects/podcast-cutter:/app -w /app \
  --entrypoint sh podcast-cutter-podcast-cutter:latest -c '
    python scripts/make_fixtures.py evals/baskets/ru.yaml evals/baskets/en.yaml \
      --variant asr --model base --work /bench/work
    chown -R 1001:1001 /app/evals'

# the hybrid basket rows (the +e5 baselines). ~6 min: embedding 16 fixtures
# through the int8 encoder is the cost; the searches stay milliseconds.
docker run --rm --user root --cpuset-cpus 8-15 --cpuset-mems 1 \
  -v podcast-asr-bench:/bench -v /home/me/server/projects/podcast-cutter:/app \
  -w /app -e EMBED_MODEL_DIR=/bench/models/multilingual-e5-small-ct2 \
  --entrypoint sh podcast-cutter-podcast-cutter:latest -c '
    pip -q install pyyaml pytest pytest-asyncio
    python -m pytest tests/test_baskets.py -q -k hybrid'

# re-converting the embedding model, should it ever be lost or upgraded
# (one-off container with torch; writes into the bench volume)
docker run --rm --user root -v podcast-asr-bench:/bench -e HF_HOME=/bench/hf \
  python:3.12-slim bash -c '
    pip -q install torch --index-url https://download.pytorch.org/whl/cpu
    pip -q install transformers ctranslate2 sentencepiece protobuf huggingface_hub
    ct2-transformers-converter --model intfloat/multilingual-e5-small \
      --output_dir /bench/models/multilingual-e5-small-ct2 --quantization int8 --force
    python -c "from huggingface_hub import hf_hub_download; import shutil; \
      shutil.copy(hf_hub_download(\"intfloat/multilingual-e5-small\", \
      \"tokenizer.json\"), \"/bench/models/multilingual-e5-small-ct2/\")"'

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

# transcription end to end against a real episode, with diagnostics saying
# whether a miss is the recogniser's or the index's. WORK_DIR makes it cheap
# to re-run: the transcript is keyed on the audio hash, so a second run only
# re-asks the questions.
docker run --rm -v /home/me/server/projects/podcast-cutter:/app -w /app \
  -v podcast-asr-check:/work -e WORK_DIR=/work --cpus 8 python:3.12-slim sh -c \
  'apt-get update -qq && apt-get install -y -qq ffmpeg && \
   pip install -q "python-telegram-bot[job-queue,rate-limiter]>=22.8" httpx \
     python-dotenv faster-whisper pymorphy3 && \
   python scripts/check_transcribe.py <url> нейросети "фраза которой нет"'

# what has been transcribed, and how fast this host actually is
docker compose exec -T podcast-cutter sqlite3 -header -column \
  /data/podcast_cutter.db \
  "SELECT id, episode_title, duration_s, ms, language FROM transcripts"

# is Telegram reachable from here at all? (it was not, on 2026-08-10)
docker run --rm alpine sh -c \
  'apk add -q curl; curl -s -o /dev/null -w "%{http_code} %{time_total}s\n" \
   --max-time 15 https://api.telegram.org/'
```

A real end-to-end cut (mp3 + voice note) can be exercised the way the last
session did it: `cut_episode` against a live enclosure inside the container.
`tests/test_cut_integration.py` covers the same paths against a local HTTP
server, so prefer that for routine work.
