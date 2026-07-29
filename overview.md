# Podcast Cutter Bot — status and roadmap

## What this is

`podcast-cutter` is a Telegram bot built on `python-telegram-bot`. Users search
for podcasts, browse episodes, name a time interval, and get that segment back
as an audio file. See `README.md` for setup and layout.

This document tracks what is done and what is not. It was originally a plan
written before the core existed; the items below reflect the current code.

## Done

**Core**
- Audio engine on ffmpeg: stream-copy over HTTP range requests, download-then-cut
  fallback, and re-encode to MP3 when no container fits the source codec.
- Output container follows the source codec, so AAC/M4A episodes work.
- Results are verified with ffprobe before sending — ffmpeg can exit 0 having
  written an unplayable fragment.
- Interval parser accepting `MM:SS`, `HH:MM:SS`, raw seconds and `1h30m`, with
  several range separators, validated against the episode length.

**Safeguards**
- Cut duration capped (default 15 min); oversized results are re-encoded before
  being refused, so the Telegram upload limit is respected.
- Timeouts on every external call: API requests, ffprobe, ffmpeg, downloads and
  uploads. Nothing can hang the bot indefinitely.
- Concurrency: a global semaphore bounds simultaneous ffmpeg jobs, and each user
  is limited to one cut at a time.
- Every job gets its own temporary directory, removed in a `finally`.
- Conversation timeout plus `allow_reentry`, so no state is a dead end and menu
  buttons always work.
- Typed errors with user-facing messages, and a global error handler.
- Config validated at startup, naming any missing variable.

**Quality**
- 180 tests, including end-to-end cutting against audio served over a local HTTP
  server (redirects, 403, 404, codec fallbacks).
- ruff clean; runs as a non-root user in Docker.

## Not done

- **Live progress bar.** Status messages are sent ("cutting", "uploading"), but
  there is no percentage or bar during long downloads.
- **ID3 tags and cover art.** The upload carries title and performer, but the
  file itself gets no embedded tags or podcast artwork.
- **Caching.** Search results live only in the user's session; identical queries
  from different users each hit the API. Redis or an in-memory TTL cache would
  cut latency and API load.
- **Persistence.** Sessions are in-memory, so a restart drops in-flight
  conversations. `PicklePersistence` would fix that.
- **Cancel during a cut.** The cancel button leaves the conversation but does not
  kill a running ffmpeg job.
- **Episode-length validation before listing.** Episodes whose duration the API
  does not report are only checked once ffprobe runs.
