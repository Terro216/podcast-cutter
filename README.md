# podcast-cutter

A Telegram bot that finds a podcast episode, takes a time range, and sends back
just that segment as an audio file.

Telegram bot: https://t.me/podcast_cutter_bot

## What it does

- **Search a podcast by name**, then browse its episodes (paginated, or filter
  by typing part of a title).
- **Search by person or keyword** across every episode in the directory.
- **Trending** podcasts and a **random episode** shortcut.
- Send a range like `01:20-02:00` and get the cut back, tagged with the podcast
  and episode name.

Accepted time formats: `MM:SS`, `HH:MM:SS`, raw seconds, and compound forms
(`1h30m`). Ranges may be separated by `-`, `–`, `..` or `to`.

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
| `MAX_CONCURRENT_JOBS` | no       | Simultaneous ffmpeg jobs (2)             |
| `WORK_DIR`            | no       | Scratch space for in-flight cuts         |

Missing or malformed values fail at startup with a message naming the variable,
rather than surfacing later as a confusing runtime error.

## Running

### Docker (recommended)

```shell
docker compose up -d --build
docker compose logs -f
```

The image ships ffmpeg and runs as an unprivileged user.

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
  text.py                  title/filename sanitising helpers
  states.py                conversation states and the per-user session
  keyboards.py             menus, pagination, callback-data vocabulary
  handlers.py              Telegram handlers
  app.py                   handler registration and lifecycle
```

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

## Sources

- https://core.telegram.org/bots
- https://docs.python-telegram-bot.org/
- https://podcastindex-org.github.io/docs-api/
