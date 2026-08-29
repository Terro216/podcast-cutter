# podcast-cutter

A Telegram bot that finds a podcast episode and sends just the needed moment
as audio, a voice message, a round video note or a square video. The moment can
be selected by timestamp or found by what was said.

Telegram bot: https://t.me/podcast_cutter_bot

## What it does

- **Search a podcast by name**, then browse its episodes — paginated, with a
  page counter, or filtered by typing part of a title.
- **Search by person or keyword** across every episode in the directory.
- **Trending** podcasts, a **random episode**, and a **recent** list of what you
  looked at.
- **Find the moment by what was said.** Don't know the timestamp? Ask for a
  word or a phrase and the bot listens to the episode, finds where it comes
  up, and opens the clip editor there. The first search on an episode takes a
  few minutes; every one after it is instant.
- **Pick the moment without arithmetic.** Send `12:30` for a clip starting
  there, or `12:30-14:00` for an exact range, then nudge it with `◀ −15s` /
  `+1m ▶` buttons until it's right. Length presets are one tap.
- **Send as audio, a voice note, a circle or a video** — four formats on one
  row. Voice notes play inline, which is what you want for a short quote. The
  circle is the round video note that plays right in the conversation; the
  video is the same picture as a square file with a caption, for clips longer
  than the minute Telegram allows a circle. Both draw the sound in one of
  up to twelve skins in four families (choices whose artwork or operator loop is
  unavailable are hidden). The artwork shelf: cover (the episode art
  with a slow Ken Burns zoom), vinyl (the art spinning like the record it
  decorates). The glow shelf: aurora (the spectrum melted into a ridge of
  northern lights that breathes with the voice), party (a mirrored neon
  soundwave over a dark dance floor, spinning through the colour wheel) and
  lava (rising wax blobs that merge and flare with the speech). The generated
  and meme shelf:
  matrix (chunky green streams falling down the frame), fractal (an endless
  Mandelbrot dive) and DVD (the artwork, or a music-note fallback, ricocheting
  inside the visible boundary). The loop shelf is four curated videos — Roblox
  Parkour, GTA Mega Ramp, ASMR Cutting and Subway Surfers — each starting at a
  fresh random offset behind optional centred subtitles, see
  docs/video-skins.md. Every skin carries
  the title on up to two lines, a progress bar with a running clock, and
  subtitles when their bottom-of-editor toggle is enabled. A stored transcript
  makes that instant; otherwise the button warns that the first listen adds a
  few minutes before the clip is cut.
- **Nudge after the fact.** The result offers `↺ 15s earlier` / `15s later ↻`
  plus visual choices. They reopen the full editor and change its values; only
  the blue Cut button sends another file, so a correction cannot spend a cut
  by accident and the format can change too.
- **Share from anywhere.** Type `@podcast_cutter_bot some words` in any chat to
  post an episode with a link that opens the clip editor for whoever taps it.

Every screen has `‹ Back` and `☰ Menu`, and a breadcrumb line shows where you
are. Accepted time formats: `MM:SS`, `HH:MM:SS`, raw seconds, and compound
forms (`1h30m`). Ranges may be separated by `-`, `–`, `..` or `to`.

## Requirements

- Python 3.10+
- `ffmpeg` **and** `ffprobe` on `PATH`
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Podcast Index API credentials: https://api.podcastindex.org/signup

## Configuration

Copy `.env.example` to `.env` and fill in the three required values:

| Variable              | Required | Purpose                                  |
| --------------------- | -------- | ---------------------------------------- |
| `BOT_TOKEN`           | yes      | Telegram bot token                       |
| `PODCAST_API_KEY`     | yes      | Podcast Index key                        |
| `PODCAST_API_SECRET`  | yes      | Podcast Index secret                     |
| `DATABASE_KEY`        | yes      | 64-hex SQLCipher key (`openssl rand -hex 32`) |
| `PODCAST_API_BASEURL` | no       | Defaults to the public Podcast Index API |
| `PODCAST_BLOCKLIST`   | no       | Comma-separated blocked feed ids (empty) |
| `TERMS_VERSION`       | no       | Acceptance version (`2026-08-18`)        |
| `LEGAL_BASE_URL`      | no       | Public directory containing legal pages  |
| `LEGAL_CONTACT`       | no       | Rights/privacy contact                    |
| `MAX_CUT_SECONDS`     | no       | Longest interval a user may request (900)|
| `MAX_SOURCE_SECONDS`  | no       | Longest episode opened at all (21600)    |
| `ASR_ENABLED`         | no       | Kill switch for transcription (true)     |
| `ASR_BACKEND`         | no       | Recognition backend (`local`)            |
| `ASR_MODEL`           | no       | Whisper model size (`base`)              |
| `ASR_THREADS`         | no       | CPU threads for recognition (8)          |
| `EMBED_MODEL_DIR`     | no       | Converted e5 model dir; empty = lexical only |
| `MAX_CONCURRENT_JOBS` | no       | Simultaneous ffmpeg jobs (2)             |
| `RATE_INPUT_PER_MINUTE` | no     | Per-user messages/buttons per minute (20); 0 = off |
| `RATE_CUTS_PER_HOUR`  | no       | Per-user cuts per hour (30); 0 = off     |
| `RATE_ASR_PER_DAY`    | no       | Per-user first transcriptions per day (10); 0 = off |
| `WORK_DIR`            | no       | Scratch space for in-flight cuts         |
| `DATA_DIR`            | no       | Database and log files (`/data` in Docker)|
| `LOG_RETENTION_DAYS`  | no       | Journal retention, 0 keeps everything (90)|
| `ASR_JOB_RETENTION_HOURS` | no   | Finished queue-row retention (24)         |
| `USER_DATA_RETENTION_DAYS` | no  | Inactive profile/recents retention (365)  |
| `ADMIN_IDS`           | no       | Telegram ids allowed to run `/stats`     |
| `TELEGRAM_PROXY`      | no       | Proxy for the Bot API; empty = direct    |
| `MEDIA_PROXY`         | no       | Proxy for audio fetches; empty = direct  |
| `MEDIA_PROXY_MODE`    | no       | `fallback` (default), `always` or `off`  |

Missing or malformed values fail at startup with a message naming the variable,
rather than surfacing later as a confusing runtime error.

## Running

### Docker (recommended)

```shell
docker compose up -d --build
docker compose logs -f
```

The image ships ffmpeg and runs as an unprivileged user. A named volume is
mounted at `/data` for the journal and log files — see below for why that
matters.

### Operating it

`/stats` prints a panel: clips cut, success rate, timings, busiest podcasts and
what failed. It is admin-only. Put your Telegram id in `ADMIN_IDS`; if you do
not know it, send `/stats` once and the log will tell you:

```
Ignoring /stats from user 12345678. Set ADMIN_IDS=12345678 to allow it.
```

**Where people come from.** Hand out `https://t.me/<bot>?start=src_<tag>` —
one tag per place you post it — and `/stats` reports how many distinct people
arrived through each. Tags are lowercased, stripped to `a-z0-9_-` and cut to 32
characters before they touch the journal. Five ready-to-use links, five short
Russian posts and an honest measurement checklist are in
[docs/seeding-posts.md](docs/seeding-posts.md). `/stats` shows arrivals and
global cut outcomes; cohort conversion requires an aggregate journal query.
Telegram does not expose forwards to bots, so it cannot measure actual sharing.

**The bot's own profile is set from code**, in `_on_startup`: the command list,
the short description shown in the profile, and the description on the empty
chat screen. Editing those in @BotFather works until the next restart, which
overwrites them. Two things have no API and remain BotFather's alone — the
avatar (`/setuserpic`) and the inline placeholder (`/setinline`).

The journal is SQLCipher-encrypted. For an operator-only ad-hoc query, reuse
the key already present inside the running container rather than putting it on
the command line:

```shell
docker compose exec -T podcast-cutter python - <<'PY'
import os
from sqlcipher3 import dbapi2 as sqlite3
from podcast_cutter.database import key_connection

connection = sqlite3.connect("/data/podcast_cutter.db")
key_connection(connection, os.environ["DATABASE_KEY"])
print(connection.execute(
    "SELECT outcome, count(*) FROM events "
    "WHERE action='cut' GROUP BY outcome"
).fetchall())
connection.close()
PY
```

**The volume is not optional.** Container logs do not survive a redeploy:
`docker compose up --build` creates a new container and the old json log goes
with it. Anything you want to keep has to be under `/data`.

Rows older than `LOG_RETENTION_DAYS` are deleted at startup. The journal stores
real Telegram user ids alongside action outcomes and episode metadata, but not
raw podcast/person/transcript search phrases. Pick a retention window you are
comfortable with; `0` disables automatic expiry and is not recommended for a
public bot. Finished ASR delivery rows and inactive profile data have the
separate, shorter controls listed above.

## Public-use and data controls

The first content request requires explicit acceptance of the current Terms
version. `/terms`, `/privacy`, `/mydata`, `/delete_me` and `/copyright` remain
available as user controls. The full bilingual documents are
[Terms of Use](docs/TERMS.md) and [Privacy Policy](docs/PRIVACY.md).

`PODCAST_BLOCKLIST` is an operational takedown control keyed by stable Podcast
Index feed id. It is empty for the initial public beta. Empty does **not** mean
that every podcast granted a licence: users must have permission or another
lawful basis for each requested use. A block is rechecked before download,
rendering and transcription, including durable jobs resumed after a restart.

Metadata comes via Podcast Index; Podcast Cutter is independent and is not
affiliated with or endorsed by Podcast Index, Telegram or podcast publishers.
API responses are cached only when their response headers explicitly permit
it. Clips keep source metadata where the output format supports it and link
back to the publisher-facing episode page when one is available.

Production's Privacy Policy URL is already published through @BotFather; a new
bot identity must publish it before public use. Durable state is encrypted at
rest with SQLCipher whenever the application is loaded through
`load_settings()`; `DATABASE_KEY` is mandatory there. The accepted
pet-project recovery policy keeps the complete `.env` (including that key)
beside the encrypted database inside the separately encrypted restic snapshot.
The live database, WAL and SHM also use a private umask and mode `0600`.

### First SQLCipher migration

Generate `DATABASE_KEY` once and keep it unchanged across deploys and restores:

```bash
openssl rand -hex 32
```

Put the result in `.env`. An existing plaintext database is deliberately not
converted during normal startup: a wrong key or accidental rollback must fail
closed instead of silently replacing user data. With the bot stopped, run:

```bash
docker compose run --rm --no-deps podcast-cutter \
  python -m podcast_cutter.migrate_sqlcipher
```

The command authenticates the source, exports it into SQLCipher, verifies both
SQLite consistency and SQLCipher page HMACs, atomically installs it and leaves
the old database as `podcast_cutter.db.plaintext-<timestamp>`. Keep that file
only through the first smoke test, then remove it: encryption at rest is not
complete while a plaintext rollback remains on the volume.

### Locally

```shell
poetry install
poetry run python main.py
```

## Development

```shell
poetry install            # includes the dev group
poetry run pytest         # full suite
poetry run ruff check .   # lint
```

`tests/test_cut_integration.py` renders real audio with ffmpeg and serves it
over a local HTTP server, so it exercises the same path production does:
redirect resolution, remote probing, the download fallback, and 403/404
handling. Those tests skip automatically when ffmpeg is unavailable.

### How the search is measured

Every search defect so far was found by a person noticing something odd. That
is not a method, and it cannot answer the question that matters — how *often* a
search is wrong, and in which of several ways. So there are evaluation baskets:
`evals/baskets/{ru,en}.yaml`, four shows per language chosen to span how they
were recorded, with queries in four classes. A literal quote tests the
recogniser. A query sharing no words with what was said tests the retriever. A
phrase said several times tests whether three answers are three moments or one
moment listed three times. And a **negative** — something never said — tests the
only failure that costs trust, because a search that always returns its best
guess turns a recognition error into a confident lie.

`hit@3` within ±15 s of the reference is the headline, with `hit@1`, the median
start error and the false-hit rate beside it, RU and EN kept apart.

**Each basket is run twice**: once over a reference transcript and once over
what the shipped model actually produced from the same audio. Neither number
means much alone — the gap between them is the price of running `base`, and
without it there is no telling whether to change the recogniser or the
retriever. Full transcripts reproduce third-party speech, so they live only in
the gitignored `private/eval-fixtures/` (or `EVAL_FIXTURES_DIR`), not in this
public repository. With that local directory present both runs are seconds of
pure CPU with no model and no network:

```shell
poetry run pytest tests/test_baskets.py -s
```

Public CI still validates basket syntax, size and query classes, but skips the
transcript-derived rows when those private files are absent.

A basket does not assert an absolute quality bar — nobody knows in advance what
`hit@3` should be on four particular episodes. It carries the numbers it last
produced and fails when one moves the wrong way, so an improvement is committed
deliberately and the history of that block is the record of whether the search
is getting better.

`scripts/check_api.py` is a live smoke check against the Podcast Index API; it
needs real credentials and network access.

```shell
poetry run python scripts/check_api.py "Lex Fridman"
```

## Documentation map

- [Current overview](overview.md) — what is live and what comes next.
- [Architecture](docs/architecture.md) — the presentation-sized system map
  and its five request paths.
- [Five pilot links and posts](docs/seeding-posts.md) — one tagged link per
  podcast chat, plus what can and cannot be measured.
- [Video skins](docs/video-skins.md) — rendering behaviour and loop assets.
- [Backup policy](docs/backup-policy.md) — encrypted snapshots and restore.
- [Roadmap](ROADMAP.md) — product rationale and deferred ideas.
- [Handoff](HANDOFF.md) — production topology, measurements and operator notes.

## Layout

```
main.py                    entry point, nothing else
podcast_cutter/
  config.py                environment loading and validation
  errors.py                exception hierarchy with user-facing messages
  api.py                   async Podcast Index client, typed Feed/Episode
  audio.py                 interval parsing, ffprobe, ffmpeg cutting
  urls.py                  where an episode URL is allowed to point
  proxy.py                 the media detour: routes, fallback, breaker
  video.py                 the video note: skins, subtitles, the render
  text.py                  escaping, filenames, progress bars
  states.py                screen stack and the per-user session
  store.py                 SQLCipher journal, transcripts and the FTS index
  asr.py                   the recogniser interface, faster-whisper behind it
  transcripts.py           quarantine, windowing, clustering, placement
  indexer.py               transcription pipeline and the search
  listening.py             the durable queue of first listens
  evals.py                 the basket runner — how often search is wrong
  screens.py               pure state → (text, keyboard) renderers
  keyboards.py             menus, pagination, callback-data vocabulary
  handlers.py              the text router, callback router and cut job
  app.py                   handler registration and lifecycle
```

### How navigation works

There is no `ConversationHandler`. A screen stack in `Session` says where the
user is, and an explicit `awaiting` field says what typing means right now.
Every update reaches one of two routers, so a message can never land in a state
with no handler — which is exactly how the previous version stranded people.

Paging *replaces* the current screen rather than pushing onto the stack, so
`‹ Back` leaves a list in one step instead of walking back through every page.
Sessions expire on next use rather than on a timer, so nobody is interrupted by
an unprompted "your session ended".

Button colours come from Bot API 9.4 (`style`: `primary` / `success` /
`danger`) and need `python-telegram-bot` 22.7+. Clients older than February 2026
ignore them and render the default style, so they are decoration, never meaning.

### How cutting works

1. Stream-copy directly from the episode URL — ffmpeg uses HTTP range requests,
   so this touches only the bytes around the interval and takes seconds.
2. If that fails or yields something undecodable, download the episode once and
   stream-copy from the local file.
3. If a stream copy is impossible — the source codec has no matching container —
   re-encode to MP3.

The output container follows the source codec (AAC lands in `.m4a`, not `.mp3`),
and the result is verified with ffprobe before being sent, because ffmpeg can
exit successfully having written an unplayable fragment.

### How a video note is made

A circle or a video is the cut audio drawn by an ffmpeg visualiser under a
skin — title, time span, a progress bar, and optional subtitles from the stored
transcript (quarantined spans are excluded there too: an invented line must
not be burned into a video). If the user explicitly asks for subtitles before
a transcript exists, the durable listening queue prepares it first. The render
then runs as the tail of the same cut job, in the same concurrency slot.

The two formats share the 512 px renderer but not the layout. Telegram crops
a note to the circle inscribed in the square, so the round layout moves every
piece of text inside the circle, shortens the title to what the chord fits,
and shrinks the progress bar to a centred track. The square video uses the
full frame and carries the attribution in its caption; a note cannot
(`sendVideoNote` has no caption), so `@podcast_cutter_bot` is burned into the
picture and the result screen keeps the source link.
A circle past one minute is refused with a pointer at the video format,
never silently converted; a video past five minutes is refused too.
The demo button renders only five seconds per available skin. Every demo gets
the full source card, and the bot handle is burned into the pixels so a
forwarded circle keeps its provenance even though video notes cannot carry a
caption.

Cover art comes from the episode's own artwork URL, is checked by the same
source-address rules as audio, and is test-decoded before use — a corrupt
image would otherwise hang the encoder rather than fail it.

### How searching inside an episode works

An episode is transcribed once, on the first search or explicit subtitle
request anyone makes against it, and the result is stored. What is stored is
keyed on the **SHA-256 of the audio
that was actually fetched**, not on the episode id: feeds insert advertisements
dynamically, so the same episode can serve different bytes next month, and
timestamps taken against the old ones would cut an advert.

Recognition is `faster-whisper` on the CPU. `base` was measured on this host at
RTF 0.07–0.09 — a 50-minute episode in under five minutes — against `small`'s
0.23, for a difference that rarely changes which moment a search lands on. The
transcript exists to *locate* a moment; what comes back is the audio itself.

Whisper invents text on silence and music, usually as repetition, and an
invention is indistinguishable from speech once it is in an index. Each
recognised span therefore carries its own metrics, one suspicious signal
demotes it and two independent ones drop it from the index, and nothing is
deleted — a quarantine decision has to be reviewable.

Search runs over 30-second windows at a 15-second stride, so a phrase spanning
two spoken sentences still lands whole inside one window; overlapping hits are
collapsed before three answers are shown, or the three would be one moment
listed three times. The clip opens on the matched word's own timestamp, padded
back two seconds, because word timings are not editing-grade.

**Search matches meaning as well as words** when `EMBED_MODEL_DIR` points at a
converted `multilingual-e5-small` (CTranslate2 int8 — the engine
faster-whisper already ships, so this costs no new dependency, only weights
on the volume). Windows are embedded once at index time; a query fuses
lexical and dense hits by reciprocal rank, which is how «где рассказывают про
эффект пустышки» finds an episode that only ever says «плацебо». A dense hit
counts only past an absolute similarity floor *and* a margin over the
episode's own background — both measured against the evaluation baskets — so
a phrase that was never said still gets the honest empty answer instead of
the nearest thing lying around.

**Russian needs lemmatisation, and this is not theoretical.** On a real episode
about neural networks the recogniser wrote «нейросетей» and never once the
exact form «нейросети», and FTS5 matches a token literally — so searching the
episode's own subject returned nothing. Windows are therefore indexed twice,
as surface forms and as `pymorphy3` lemmas, and a query tries the lemmas first.
The surface index stays behind it because pymorphy3 *guesses* at words it does
not know rather than leaving them alone, and an exact phrase should not depend
on a dictionary agreeing with it.

Transcription is minutes of CPU where a cut is seconds. `ASR_ENABLED=false`
switches it off without stopping the bot, `cpuset` in `docker-compose.yml` pins
it to physical cores of one socket, and everyone asking about the same episode
shares one job rather than starting another.

**The queue of first listens lives in SQLite, and it is a queue of episodes.**
One episode is listened to at a time; everybody waiting on that one is served
by the single job, so the position a person is shown — `2nd in line` rather
than a bare "queued" — counts episodes ahead of theirs and not people. Being
in the database rather than in memory is what makes a redeploy survivable:
waiting jobs used to be discarded with the process, which threw away the most
expensive thing the bot does in the operation it performs most often. On the
way back up, anything left mid-flight is picked up from the front of the line.

What a restart cannot restore is the screen. Sessions are a two-minute working
set and are deliberately not persisted, so a job that outlives the request that
made it finishes the transcript and says so in the chat, with a link back into
the episode — where the search is now instant. An episode that fails twice in a
row is given up on rather than retried forever, so one bad episode cannot turn
into a boot loop that re-downloads it on every start.

### Where an episode URL is allowed to point

An enclosure URL is third-party input. Anyone may submit a feed to the
directory, so a URL coming back from the API is worth exactly as much as one a
stranger typed, and it is checked before anything opens it:

- the scheme must be `http` or `https`, so `file:`, `concat:` and the rest of
  what ffmpeg speaks are refused outright;
- the hostname must not resolve into this server's own network — loopback,
  private ranges, and the link-local address cloud metadata lives on;
- **every hop of a redirect chain is checked**, because the interesting hop is
  rarely the first one: an ordinary CDN hostname can redirect inward;
- ffmpeg runs with `-protocol_whitelist`, so an input that answers with a
  playlist cannot name a local file as its next source. Current ffmpeg already
  refuses that, which makes this a second lock rather than the only one.

A name that does not resolve is allowed through on purpose: the fetch that
follows fails a moment later with a message that says what happened, whereas
refusing here would turn a DNS hiccup into an unexplainable permanent verdict.
Refusals are journalled as `unsafe_source`, so `/stats` shows whether this ever
fires in the wild.

`MAX_SOURCE_SECONDS` bounds the other direction: `MAX_SOURCE_BYTES` limits what
gets downloaded, but seeking and re-encoding scale with the source, and a feed
can advertise a file of any length.

### Episodes the server cannot reach

Some hosts refuse or silently drop requests from a given server, and the reason
is the source address rather than anything about the request: measured against
40 trending episodes, `traffic.megaphone.fm` — which most analytics prefixes
redirect to — accounted for a sixth of the directory on its own, and
Anchor-hosted feeds answer 403.

`MEDIA_PROXY` routes **audio fetches only** through a proxy somewhere the CDNs
are happier; the directory API goes direct, and so does Telegram unless
`TELEGRAM_PROXY` says otherwise — see below. It defaults to
empty, in which case none of this is active.

With `MEDIA_PROXY_MODE=fallback` — the default — episodes are fetched directly
and the proxy is consulted only after a fetch fails the way a blocked egress
fails, so anything that already worked keeps its route and its latency. A proxy
that stops answering is logged, journalled, and taken out of rotation for a
minute at a time; fetches carry on directly meanwhile. `off` keeps the URL
configured but stops using it, which is the one-variable rollback.

`deploy/README.md` covers a working setup — a loopback proxy plus an SSH
forward — and how to move it elsewhere.

### When Telegram itself is unreachable

On 2026-08-10 the production host stopped reaching Telegram at all. Measured
from that host: `api.telegram.org` and `core.telegram.org` were TCP black holes
— DNS resolved, `connect` never completed, 20 s timeouts — while `api.github.com`
and `pypi.org` answered in under 150 ms and the same request from another
egress got a `302` in 24 ms. Selective filtering, not an outage, and the same
filter that already blocks `traffic.megaphone.fm`.

`TELEGRAM_PROXY` sends the Bot API through a proxy, and it is set for both the
main connection pool and the long-polling one — routing only the first leaves a
bot that can answer but cannot hear.

Two things make this different from `MEDIA_PROXY`, and both are the reason it
is off by default:

- **There is no fallback and no mode.** A proxy that works for one Bot API
  request works for all of them, and one that does not means no bot at all —
  `getMe` runs before anything else and a failure there is not a degraded mode,
  it is a crash loop.
- **It makes the proxy a hard dependency.** Losing the tunnel used to cost
  audio fetches; with this set it costs the whole bot. Nothing in
  `docker-compose.yml` enforces the ordering, on purpose: `restart:
  unless-stopped` plus PTB's bootstrap retries converge on their own once the
  sidecar is healthy, which is simpler than a `depends_on` that would drag the
  proxy profile into deployments that do not want it.

## Sources

- https://core.telegram.org/bots
- https://docs.python-telegram-bot.org/
- https://podcastindex-org.github.io/docs-api/
