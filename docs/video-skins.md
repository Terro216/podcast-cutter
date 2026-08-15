# Video skins

What each skin draws, what it needs, and how to feed the one that needs
feeding. The render behaviour lives in `podcast_cutter/video.py`; the button
labels in `keyboards.py`/`i18n.py`. This page is for the operator.

## The lineup

As they appear on the keyboard (the 🎨 demo button under the skin rows
renders a 20-second sample of the current clip in *every* skin and sends
them in a row — charged as one cut):

| Skin | Picture | Needs |
| --- | --- | --- |
| cover | Episode artwork with a slow Ken Burns zoom | artwork in the feed |
| vinyl | The artwork spinning like a record, vignetted into the circle | artwork in the feed |
| dvd | The artwork ricocheting off the edges (of the circle, in a note) | artwork in the feed |
| aurora | The spectrum melted into northern lights over a black sky | — |
| party | A crisp equalizer with falling peaks and its reflection, hue spinning | — |
| lava | A lava lamp of warm gradient blobs that flares with the speech | — |
| matrix | Chunky phosphor-green streams falling down the frame | — |
| fractal | An endless Mandelbrot dive, flushed with colour by the voice | — |
| random | A random background loop behind big centred subtitles | files, see below |
| subway | Same, but only loops from the `subway/` subfolder | files, see below |

The artwork skins fall back to an honest dark card when the feed ships no
image (or a broken one): title, subtitles and progress stay, nothing
pretends to be another skin. Retired first-generation skins (`bars`,
`spectrum`, `scope`, `vhs`, `brainrot`) still arrive from buttons on old
messages;
`video.LEGACY_SKINS` maps each to its closest heir.

## Feeding the loop skins

`random` plays a random file from anywhere under `<DATA_DIR>/brainrot/` (the
compose volume: `/data/brainrot/` inside the container); `subway` draws only
from `/data/brainrot/subway/`, so themed footage stays themed while still
counting toward `random`'s pool. Either way the file is cropped to the
square, starts at a random offset and loops if the clip outlasts it.
Accepted suffixes: `.mp4`, `.mov`, `.mkv`, `.webm`, `.m4v`. No files — the
honest dark card.

Dropping a file in, from the host running the stack:

```
docker cp subway.mp4 podcast-cutter:/data/brainrot/subway/
```

(`docker exec podcast-cutter mkdir -p /data/brainrot/subway` first if it is the
volume's first file.) No restart needed; the directory is listed per render.

The repo ships no footage on purpose: gameplay recordings are somebody's
copyrighted work, so sourcing clips you have the right to use — your own
recordings, CC-licensed loops — is the operator's call and the operator's
responsibility. Keep files at 384×384-ish quality in mind: the render crops
to a square and never upscales beyond `NOTE_SIZE`, so a 480p vertical clip
is already more than enough.

## Costs

Measured on 30 s of real speech at 384×384 with eight pinned cores (see the
module docstring in `video.py`, which is the canonical copy): the cheap end
is brainrot/cover/aurora/vinyl at 2.5–4.4 s, the expensive end is matrix at
~24 s. Every skin stays comfortably inside one cut-job slot. The encoder
carries a 1 Mbit/s VBV cap because the noisy generators (fractal, matrix)
would otherwise push a five-minute square video past the Bot API upload
ceiling.
