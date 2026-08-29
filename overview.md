# Podcast Cutter — current overview

Updated 2026-08-26. `README.md` is the operator/user reference; `HANDOFF.md`
keeps the implementation history; `ROADMAP.md` contains the product rationale.
This page is the short current-state map.

Deployment note: the video/demo redesign, four curated loops and the follow-up
UX/privacy batch are live. The host loops are mounted read-only at
`/data/brainrot`; the final 2026-08-21 rollout passed `ruff` and `1022 passed,
8 skipped`. The DVD edge/result-copy follow-up raised the full suite to `1024
passed, 8 skipped` and started successfully on image
`sha256:30a4e2237775…`.

Read-only production verification on 2026-08-26 found both
`podcast-cutter` and `podcast-cutter-tunnel` running healthy with zero
restarts; the event-loop heartbeat was advancing every minute. The current
system map is [docs/architecture.md](docs/architecture.md).

## Product today

Podcast Cutter is a bilingual Telegram bot that searches Podcast Index,
opens an episode, finds a timestamp either manually or by what was said, and
sends the selected fragment as audio, voice, a circular video note or a square
video. Post-cut nudges and skin choices reopen the full editor without sending;
the next file goes out only after the blue Cut button. It also supports recent
episodes, inline sharing and deep links.

Search-by-speech is live: local Whisper produces timed utterances, FTS5 plus
Russian lemmatisation supplies lexical recall, multilingual E5 adds semantic
retrieval, and the result opens the ordinary clip editor. Transcription jobs
are durable in SQLite and resume after restart; repeated searches reuse the
stored transcript.

Visual output is 512×512 H.264 with twelve possible skins. Cover/Vinyl are
hidden without usable episode artwork; each of Roblox, GTA, ASMR Cutting and
Subway Surfers is hidden unless its exact operator-owned loop exists. Every
render starts at a fresh random offset; old/stale choices fall back to Aurora
rather than yielding an empty card. The Matrix is falling glyph rain, DVD
bounces inside an explicit visible boundary, and Lava uses rising/merging wax
fields instead of a rotating gradient. Skin demos are five seconds each,
include the full source card and burn `@podcast_cutter_bot` into the picture.

Every captioned result begins with a clickable `@podcast_cutter_bot`, then the
episode title, show/time range, full-episode link and Podcast Index attribution.
Video notes cannot carry captions. Every circle burns the bot handle into its
pixels and its result screen keeps the source link. Subtitles are an explicit
bottom-of-editor option: instant for a
stored transcript, or a clearly-labelled several-minute first listen otherwise.

## Reliability and public-use baseline

- Guarded URLs/redirects, source-duration ceiling, bounded concurrency and
  per-user rate limits.
- Range-first cutting with proxy fallback, codec-aware containers, ffprobe
  verification, upload-size handling and cancellation that kills ffmpeg.
- SQLCipher state, explicit Terms acceptance, privacy/delete commands,
  feed blocklist and retention controls.
- Encrypted off-host restic backups with integrity checks and proven restores.
- CI runs ruff and more than one thousand tests, including real ffmpeg paths
  and retrieval-basket structure. Full transcript-derived basket regressions
  run locally from gitignored private fixtures. Production deployment remains
  an explicit, approval-gated operation.

## Next plan, in order

1. **Prove adoption.** Publish the five prepared one-chat/one-tag links and
   short posts in [docs/seeding-posts.md](docs/seeding-posts.md). Measure tagged
   arrivals, first successful clip, repeat use and failure reasons, then fix
   the largest observed drop. Telegram does not expose forwards to bots, so
   actual sharing is a pilot-feedback question rather than a journal metric.
2. **Finish the search ruler.** Complete the by-ear answer-key pass. Use those
   results to decide whether English stemming or a larger recogniser earns its
   CPU/complexity cost.
3. **Package the demo.** The current architecture diagram and five short
   distribution posts are done. Record a reliable 3–5-minute fallback and set
   the BotFather-only avatar/inline placeholder.
4. **Close small operational debts.** Store the restic secret in Bitwarden and
   add external monitoring for the Telegram/media tunnel.
5. **Only then expand features.** Candidates are chapter-aware clip boundaries
   (offer or snap to publisher-supplied chapter starts/ends), embedded artwork
   for audio (write the episode cover into MP3/M4A metadata), queue ETA (show
   approximate minutes as well as position), and transcript LRU eviction
   (remove least-recently-used indexes after a configured storage ceiling).
   None is required by the current core flow; their order comes from pilot
   evidence, not novelty.

The strategic shift is simple: the core, AI path, safeguards and visual wow are
already built. The highest-value work now is helping real people reach and
share a useful first clip, then improving whichever part actually blocks them.
