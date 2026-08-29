# Podcast Cutter architecture

Current as of 2026-08-26. This is the presentation-sized system map; the
operator details live in `README.md`, `HANDOFF.md`, `deploy/README.md` and
`docs/backup-policy.md`.

```mermaid
flowchart LR
    user["Telegram user"] <--> tg["Telegram Bot API"]

    subgraph prod["big-one · production"]
        bot["Python Telegram bot<br/>routers · screens · limits"]
        directory["Podcast Index client<br/>search · metadata · 5 min cache"]
        guard["Source guard<br/>scheme · DNS/IP · redirects · duration"]
        cutter["ffprobe + ffmpeg<br/>range cut · download fallback"]
        renderer["512×512 renderer<br/>skins · subtitles · attribution"]
        queue["Durable first-listen queue<br/>one episode at a time"]
        indexer["Indexer<br/>hash · Whisper · quarantine · windows"]
        search["Hybrid retrieval<br/>FTS5 RU lemmas + multilingual E5"]
        db[("SQLCipher SQLite<br/>users · recents · events<br/>jobs · transcripts · vectors")]
        assets[("Mounted assets<br/>models · four loop videos")]
        tunnel["media-proxy sidecar<br/>restricted SSH forward"]
        backup["Authenticated SQLCipher export<br/>restic + rclone"]
    end

    pi["Podcast Index API"]
    media["Podcast publisher / CDN"]
    de["DE egress<br/>loopback tinyproxy"]
    yadisk["Encrypted Yandex Disk repository"]

    tg <-->|"long polling and sends<br/>direct or TELEGRAM_PROXY"| bot
    bot --> directory --> pi

    bot -->|"timestamp cut"| guard
    bot -->|"search by speech / subtitles"| queue
    queue <--> db
    queue --> indexer --> guard
    indexer --> search
    search <--> db
    db -->|"three moments"| bot

    guard -->|"direct first"| media
    guard -. "fallback for blocked media" .-> tunnel --> de --> media
    guard --> cutter
    cutter -->|"audio / voice"| bot
    cutter --> renderer -->|"circle / square video"| bot
    db -->|"stored subtitle timings"| renderer
    assets --> indexer
    assets --> renderer

    db --> backup --> yadisk
```

## The five paths to explain in a demo

1. **Find an episode.** Telegram input goes through the screen/router layer to
   Podcast Index. Directory metadata is cached only when the response permits
   it.
2. **Cut a known moment.** The source is validated, probed and cut by HTTP
   range where possible. A verified local-download/re-encode path catches
   hostile sources and codecs.
3. **Find a moment by speech.** The durable queue fetches and hashes the real
   episode bytes, Whisper produces timed utterances, suspicious spans are
   quarantined, and lexical plus semantic retrieval returns three moments.
4. **Make it shareable.** The same audio can leave as audio, a voice message,
   a round video note or a square video. Video rendering adds the selected
   skin, optional subtitles, source context and `@podcast_cutter_bot`.
5. **Survive failure.** SQLCipher holds the expensive and personal state;
   authenticated exports go into encrypted off-host backups. The media tunnel
   is fallback-only, so its failure does not take working direct sources down.

## Boundaries that are deliberate

- Podcast Index and Telegram are not sent through the media detour. Only
  publisher audio uses it when direct fetching fails.
- Audio is temporary; transcripts and their search index are durable. Loop
  videos and model weights are replaceable mounted assets, not database rows.
- A queue item is an episode, not a person: ten people waiting for the same
  episode share one transcription.
- Session screens are intentionally in memory. After a restart the expensive
  transcription resumes and the user receives a deep link back to the episode;
  a stale interaction screen is not reconstructed.
- The bot can record that a tagged visitor made a successful cut, but Telegram
  does not tell bots whether the resulting file was forwarded. “Shared” must
  therefore be learned from user feedback, not claimed as an analytics event.

## Deployment shape

- `podcast-cutter` is the only application container and is pinned to eight
  physical cores for local ASR.
- `podcast-cutter-tunnel` is an independently restartable optional sidecar.
- `/data` is the persistent volume; `/data/brainrot` is a read-only host mount.
- The backup container runs only from systemd-driven scripts and never owns the
  Docker socket.
- Production deploys must name the service:
  `docker compose up -d --build --no-deps podcast-cutter`. This leaves the
  healthy tunnel untouched.
