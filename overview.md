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

**Interface**
- Screen stack with `‹ Back` and `☰ Menu` everywhere, plus a breadcrumb line.
- Pagination shows position and total (`‹ 2/7 ›`) instead of bare arrows.
- Coloured buttons via Bot API 9.4 `style`, so the primary action is obvious.
- Clip editor: length presets, `◀ −15s` / `+1m ▶` nudges, and a bare timestamp
  (`12:30`) meaning "a clip starting here".
- Live download progress bar, throttled to one edit every few seconds, plus a
  native upload indicator.
- Audio file or voice note, chosen with a toggle.
- Post-cut nudges that re-cut, and one-tap "another clip from this episode".
- Recent-episode list; typing on any list filters it in place.
- Inline mode (`@bot query` in any chat) and `?start=ep_…` deep links.

**Quality**
- 341 tests: routers driven through fakes, screens rendered as pure functions,
  and end-to-end cutting against audio served over a local HTTP server
  (redirects, 403, 404, codec fallbacks, oversized ID3 tags).
- ruff clean; runs as a non-root user in Docker.

## Not done

- **Mini App.** A web view with a waveform would beat any button-based interval
  picker, but it needs a separate frontend and HTTPS hosting.
- **Persistence.** Sessions and the recent list are in-memory, so a restart
  clears them. `PicklePersistence` plus a volume would fix it, at the cost of
  needing care whenever the `Session` dataclass changes.
- **Caching.** Identical searches from different users each hit the API.
- **Cancel during a cut.** Cancel leaves the screen but does not kill a running
  ffmpeg job.
- **Cover art.** Clips carry title/artist/album tags but no embedded artwork.
- **Chapter awareness.** Feeds that publish chapters could offer them as
  one-tap clip boundaries.
