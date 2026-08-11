# podcast-cutter

A Telegram bot that finds a podcast episode, takes a time range, and sends back
just that segment as an audio file.

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
- **Send as an audio file or a voice note** — voice notes play inline in chat,
  which is what you want for a short quote.
- **Nudge after the fact.** The result offers `↺ 15s earlier` / `15s later ↻`
  and re-cuts, so finding the exact line is a couple of taps.
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
| `PODCAST_API_BASEURL` | no       | Defaults to the public Podcast Index API |
| `MAX_CUT_SECONDS`     | no       | Longest interval a user may request (900)|
| `MAX_SOURCE_SECONDS`  | no       | Longest episode opened at all (21600)    |
| `ASR_ENABLED`         | no       | Kill switch for transcription (true)     |
| `ASR_BACKEND`         | no       | Recognition backend (`local`)            |
| `ASR_MODEL`           | no       | Whisper model size (`base`)              |
| `ASR_THREADS`         | no       | CPU threads for recognition (8)          |
| `MAX_CONCURRENT_JOBS` | no       | Simultaneous ffmpeg jobs (2)             |
| `WORK_DIR`            | no       | Scratch space for in-flight cuts         |
| `DATA_DIR`            | no       | Database and log files (`/data` in Docker)|
| `LOG_RETENTION_DAYS`  | no       | Journal retention, 0 keeps everything (90)|
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
characters before they touch the journal.

**The bot's own profile is set from code**, in `_on_startup`: the command list,
the short description shown in the profile, and the description on the empty
chat screen. Editing those in @BotFather works until the next restart, which
overwrites them. Two things have no API and remain BotFather's alone — the
avatar (`/setuserpic`) and the inline placeholder (`/setinline`).

The journal itself is plain SQLite, so anything the panel does not answer is a
query away:

```shell
docker compose exec podcast-cutter \
  sqlite3 /data/podcast_cutter.db \
  "SELECT outcome, count(*) FROM events WHERE action='cut' GROUP BY outcome"
```

**The volume is not optional.** Container logs do not survive a redeploy:
`docker compose up --build` creates a new container and the old json log goes
with it. Anything you want to keep has to be under `/data`.

Rows older than `LOG_RETENTION_DAYS` are deleted at startup. The journal stores
real Telegram user ids alongside what was searched and cut, so pick a retention
window you are comfortable with, or set `0` to keep everything.

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

`scripts/check_api.py` is a live smoke check against the Podcast Index API; it
needs real credentials and network access.

```shell
poetry run python scripts/check_api.py "Lex Fridman"
```

## Layout

```
main.py                    entry point, nothing else
podcast_cutter/
  config.py                environment loading and validation
  errors.py                exception hierarchy with user-facing messages
  api.py                   async Podcast Index client, typed Feed/Episode
  audio.py                 interval parsing, ffprobe, ffmpeg cutting
  text.py                  escaping, filenames, progress bars
  states.py                screen stack and the per-user session
  store.py                 SQLite journal and the persisted recent list
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

### How searching inside an episode works

An episode is transcribed once, on the first search anyone makes against it,
and the result is stored. What is stored is keyed on the **SHA-256 of the audio
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
