# Video skins

What each skin draws, what it needs, and how to feed the one that needs
feeding. The render behaviour lives in `podcast_cutter/video.py`; the button
labels in `keyboards.py`/`i18n.py`. This page is for the operator.

## The lineup

As they appear on the keyboard (the 🎨 demo button under the skin rows
renders a 5-second sample of the current clip in every *available* skin and
sends them in a row — charged as one cut). Each item carries the episode/source
card; demo pixels also carry `@podcast_cutter_bot`, because a forwarded video
note cannot keep a separate Telegram caption:

| Skin | Picture | Needs |
| --- | --- | --- |
| cover | Episode artwork with a slow Ken Burns zoom | artwork in the feed |
| vinyl | The artwork spinning like a record, vignetted into the circle | artwork in the feed |
| dvd | Artwork, or a built-in note, touching the exact visible boundary on every ricochet | — |
| aurora | The spectrum melted into northern lights over a black sky | — |
| party | A crisp equalizer with falling peaks and its reflection, hue spinning | — |
| lava | Five rising, merging wax blobs with a subtle voice-driven heat glow | — |
| matrix | Independent code streams with bright heads, phosphor trails and green glow | — |
| fractal | An endless Mandelbrot dive, flushed with colour by the voice | — |
| roblox | Roblox Parkour from a fresh random start | `01-roblox-parkour.mp4` |
| gta | GTA Mega Ramp from a fresh random start | `02-gta-5-mega-ramp.mp4` |
| asmr | ASMR Cutting from a fresh random start | `03-asmr-cutting.mp4` |
| subway | Subway Surfers from a fresh random start | `04-subway-surfers.mp4` |

Cover and Vinyl are absent from the keyboard and demo when the feed ships no
image. DVD stays because its bouncing music-note fallback is a complete look
of its own. Square video hits the literal frame edges; the note follows chords
whose endpoints touch Telegram's circular crop. If a feed advertises a broken
or unreachable image, Cover/Vinyl
fall back to Aurora at render time and the demo omits them. Each of the four
loop looks is likewise absent when its exact named file is missing; an old
button or a file removed between menu and render falls back to Aurora
instead of producing an empty card. Retired
first-generation skins (`bars`,
`spectrum`, `scope`, `vhs`, `brainrot`) still arrive from buttons on old
messages;
`video.LEGACY_SKINS` maps each to its closest heir.

## Feeding the loop skins

The host directory defaults to `/srv/podcast-cutter/brainrot/loops` and is
mounted read-only at `/data/brainrot` inside the bot. Override the host side
with `BRAINROT_SOURCE_DIR` if needed. The four filenames in the table are an
API: each button checks only its own exact file, so Roblox can never serve GTA.

All current assets are five-minute, video-only H.264 High, `yuv420p`, 30 fps,
720×720 MP4 with a two-second GOP. The renderer chooses a new random valid
offset for every request and loops only if the requested clip reaches the end.
The square source avoids decoding and immediately discarding the sides of a
16:9 720p frame. No restart is needed when replacing a file, although removing
one after a keyboard was shown makes that stale tap fall back to Aurora.

The uploaded archive and extracted source files live one level above `loops/`
and are intentionally not visible in the container. The database backup does
not include these large, replaceable assets; retain the source archive or back
it up separately if recreating the loops would be inconvenient.

### Current asset provenance

| File | Supplied source |
| --- | --- |
| `01-roblox-parkour.mp4` | `youtu.be/AlGDfjRQY54` |
| `02-gta-5-mega-ramp.mp4` | `youtu.be/weAUrmRLpnk` |
| `03-asmr-cutting.mp4` | `youtu.be/BWbXHJyTAy8` |
| `04-subway-surfers.mp4` | `youtu.be/i0M4ARe9v0Y` |

The supplied titles describe these as no-copyright/copyright-free footage,
but a title is not itself a licence grant. Keep evidence of the uploader's
actual licence/terms before treating the files as cleared for public use.

The repo ships no footage on purpose: gameplay recordings are somebody's
copyrighted work, so sourcing clips you have the right to use — your own
recordings, CC-licensed loops — is the operator's call and the operator's
responsibility. The render is 512×512, so the curated 720×720 files leave
enough headroom for Telegram's re-encode without wasting 4K decode work.

## Costs

The old 384×384 benchmark remains in the module docstring for historical
comparison. Current output is 512×512 with CRF 23 and the `fast` x264 preset,
which gives the title, timer and subtitles more source detail before Telegram
re-encodes them. The encoder keeps a 1 Mbit/s VBV cap because noisy generators
would otherwise push a five-minute square video past the Bot API upload
ceiling.
