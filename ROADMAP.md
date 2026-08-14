# Roadmap — semantic search, shareable clips, and surviving a crowd

`overview.md` records what exists. This file records **where the project is
going next** and why, including the measurements the decisions rest on.

The occasion is a company pet-project contest, which is a deadline rather than
a design input — but its scoring does say plainly which parts of the project
are underweight, so §1 keeps that framing. Everything after it is engineering
that stands on its own.

---

## 1. Why this phase exists

The contest scores 100 points across six axes. Judged honestly, the project
today lands around 69:

| Axis | Points | Now | Why |
| --- | --- | --- | --- |
| Working result | 20 | ~20 | Live bot, deployed, uptime |
| Engineering quality | 20 | ~17 | Strong; no CI, no public repo |
| Difficulty vs resources | 10 | ~8 | Real, but not *shown* |
| Clarity of demo | 10 | ~2 | No video, no diagram, no script |
| Idea and originality | 20 | ~10 | **The gap.** "A bot that cuts podcasts" is a utility |
| Use, emotion, value | 20 | ~12 | Useful, but nothing makes anyone say *oh* |

The engineering is already at finalist level. The idea is not. So this phase
buys originality and emotion, and only then hardens what a crowd would break.

The single change that moves originality, emotion and the AI framing at once:
**stop requiring the user to know the timestamp.**

---

## 2. The core idea: find the moment by meaning

Today the user must already know *when* the thing was said. That is the ceiling
of the current concept. The target interaction:

> «найди, где говорят про блокировки» → three candidate moments with surrounding
> context → tap → the clip editor opens on that timestamp, already positioned.

This turns a utility into the thing the contest actually asks for — *«путь от
"а что если?" к "смотри, оно работает"»* — and it puts the project under their
AI Skills specification, which demands a stated `[INPUT] [PROCESS] [EVALS]
[OUTPUT]` workflow. **`[EVALS]` is where nearly every AI submission is thin**,
and it happens to be this project's strength: the test culture is already here.

### 2.1 No shortcut via feed transcripts

Checked before designing anything: the Podcast Index `transcriptUrl` field was
empty on 100 of 100 recent episodes, and on every checked episode of a RU feed
(«Запуск завтра»). Published transcripts are not a thing we can rely on.
Chapters likewise (`chaptersUrl` empty on the same sample).

**We have to transcribe ourselves.** This is a fact worth stating in the
application: a measured reason for the architecture, not a guess.

### 2.2 Language coverage is fine

Podcast Index carries RU feeds — «Запуск завтра» (1 result, `language=ru`),
«Радио-Т» (12), and 60 hits for the bare word «подкаст», mostly `ru`. The demo
will survive a judge typing the name of a podcast they actually listen to.

---

## 3. What the hardware allows

Measured on `big-one`, not assumed.

**The box:** 2× Xeon E5-2650 v2 (Ivy Bridge, 2013, 2.6 GHz) = 32 threads,
126 GB RAM, 429 GB free, no GPU. Crucially **no AVX2 and no FMA** — only AVX
and f16c. CTranslate2, the engine under faster-whisper, loses multiples on
that. The machine is also shared: load average wandered 4–10 during testing.

**faster-whisper, int8, RU podcast, 180 s sample, beam=1, VAD, `--cpus 8`:**

| Model | RTF | 50-min episode | Quality by ear |
| --- | --- | --- | --- |
| tiny | 0.04 | ~2.0 min | garbage — «руководством фокольте», «это мало забудь» |
| base | 0.07 | **~3.6 min** | «руководством факультета», «их всё это мало заботит» — correct |
| small | 0.23 | ~11.5 min | best, but not by much |

Fetching and decoding 180 s straight from the CDN took 2.7 s, so ASR is the
whole cost.

Two conclusions:

1. **Threads do not save us.** 4 → 8 threads bought only ~1.5×. The wall is the
   missing AVX2, not core count.
2. **`base` is the operating point.** The transcript exists only to *land on the
   moment* — nobody reads it, they listen to the clip. Optimise keyword recall,
   not WER. 3.6 min per episode, in the background, cached, is acceptable.

---

## 4. Two ASR backends

`ASR_BACKEND=local | speechkit | auto` — the same shape as the existing
`MEDIA_PROXY` / `MEDIA_PROXY_MODE` mechanism, which is already justified by
measurement in `README.md`. `auto` means local, spilling to cloud when the
queue backs up.

**The surprise:** SpeechKit async recognition is documented at ~10 s per minute
of audio, i.e. ~8 min for a 50-min episode. That is **slower than local `base`**
(3.6 min) and comparable to `small`. Cloud does not win on speed here. What it
wins is:

- it does not consume our CPU — the box stays free for cutting;
- better RU punctuation and number normalisation.

Its costs: audio must be uploaded (Object Storage in the documented flow),
results are retained 3 days, and the free quota is finite.

**The real reason to build both** is not dogfooding (pleasant, but minor): two
backends behind one interface make the evals in §5 a comparison rather than a
single number. One basket, two implementations, one table — and the `[EVALS]`
section writes itself.

**Revised twice.** The research established there is no recurring free tier,
which briefly demoted SpeechKit to a measurement bench. That was wrong for this
account: there is a **recurring monthly grant of ₽18 000 across all of Yandex
Cloud**, part of which is wanted for storage and backups rather than ASR.

At the published rates — ₽0.1515 per 15 s standard, ₽0.0381 per 15 s deferred —
a 50-minute episode costs **₽30.30** standard or **₽7.62 deferred**. So an
allocation of ₽5 000/month buys roughly **165 episodes** standard or **650
deferred**; even ₽2 000 buys ~260 deferred. For a bot of this size that is not
a bench, that is a working backend.

Therefore:

- **Deferred is the cloud default.** It is four times cheaper, and its one
  drawback — no latency guarantee — costs nothing here, because transcription
  is already a queued background job that reports its position (§7).
- **`ASR_BACKEND=local | speechkit | auto`** stands, with `auto` spilling to the
  cloud when the local queue backs up.
- **A monthly spend ceiling in config**, with the cloud path disabling itself
  and falling back to local when it is reached. The grant is shared with
  storage; ASR must not be able to eat the whole thing. This is also a good
  thirty seconds of the demo.

Confirmed useful: v3 accepts audio directly in `content` (no Object Storage
needed below 60 MB) and returns word-level timings. Worth checking in the
console rather than the docs: whether the grant renews as assumed and what it
has actually been spent on.

---

## 5. Evals

Built **before** tuning, or the tuning is blind. **Status: the apparatus is
built and green; the answer key is being written — see §16.**

**Baskets:** 25–30 queries per language, RU and EN, across 3–4 podcasts each,
deliberately spanning recording conditions — studio, remote/Skype, noisy field.

**Three query classes:**

1. **Literal quote** — «где говорят про фолдинг белков». Tests ASR.
2. **Meaning, no shared words** — «где спорят, заменит ли ИИ учёных». Tests
   embeddings.
3. **Negatives** — things not present in the episode. The bot must answer
   *not found* rather than return its best bad guess.

Class 3 is the standard hole in semantic search and the most likely question
from a judge. It is not optional.

**Metrics:**

- `hit@1` / `hit@3`, a hit being a returned start within **±15 s** of the
  reference timestamp;
- WER on five hand-corrected segments per language, purely to compare backends;
- an LLM judge for "does this moment actually answer the query", which no
  formula captures well.

**Ground truth without hand-transcribing hours:** generate with `large-v3` on
the M2 Pro, then hand-correct. Twenty minutes per segment instead of an evening.

**Where the reference tooling lives:** the Mac and any cloud model are a
measurement bench, not part of the product. The product is what runs on
`big-one`.

**Wiring:** basket as YAML in the repo, executed by pytest. Evals then live in
CI and count as engineering quality, not as a slide.

---

## 6. Done: shareable clips, the video note

Emotion, cheaply, and every note posted in someone else's chat advertises the
bot. **Shipped**: a third delivery format on the clip editor, four skins on a
button, subtitles burned in from the transcript when one exists.

**Measured render, 60 s at 384×384, `--cpus 4`:**

| ffmpeg filter | Time | Size |
| --- | --- | --- |
| `showfreqs` (bars, Winamp-ish) | 5 s | 2.9 MB |
| `showcqt` (spectrum, prettier) | 3 s | 1.7 MB |
| `avectorscope` (oscilloscope) | 4 s | 797 KB |

Re-measured in the production image with the final graphs — title, span,
progress bar and subtitles on top of the visualiser, eight pinned cores:
bars 2.5 s / 3.3 MB, spectrum 1.5 s / 0.9 MB, scope 1.9 s / 0.8 MB, cover
2.7 s / 0.9 MB. The image needed nothing added: Debian's ffmpeg ships libx264,
every visualiser, `drawtext` with the DejaVu fonts, and libass.

**Telegram constraints that drive the design:** a video note is a square MPEG4
**of at most one minute**, and **cannot be sent by URL** — upload only. So
server-side rendering is mandatory, and clips longer than 60 s cannot be notes.
Between one and five minutes the same render goes out as an ordinary square
video with the attribution in its caption (`sendVideoNote` has no caption
parameter at all, so for a note the attribution lives on the result screen);
past five minutes the cut button refuses before any work is spent — encode
time and upload size both scale with length, and the busiest skin measures
~3.3 MB a minute.

**Skins** are overlays on the visualiser — a title, the time span, a progress
strip slid across by `overlay`'s `t` expression. Four presets on a button:
bars, spectrum, oscilloscope, cover art. Everything textual reaches ffmpeg
through files (`textfile=`, an `.ass` script), never inline in the graph,
because episode titles contain every character that is an operator to the
graph parser. Subtitles come from the same transcript the search runs on —
and pass the same quarantine, because a decoder loop kept out of the index
has no business burned into a video. An episode nobody has searched gets no
subtitles rather than a transcription: minutes of CPU for a caption is the
wrong trade until someone asks for the words.

**Found on the way: a corrupt cover image does not fail the render, it hangs
it.** ffmpeg 7.1.5 given an undecodable second input prints `Invalid data`
and then sits until the timeout — reproduced on the host. So artwork is
test-decoded (one frame to `null`, `-map 0:v:0` so a video stream is
mandatory) before it is allowed near a render, and a render that still fails
quickly with artwork is retried once without it. Artwork is decoration: it
may be dropped, it may never cost the clip.

~~Rendering is an encode, not a stream copy: it belongs in the queue (§7).~~
**Decided the other way, on the numbers.** A render is 1.5–2.7 s — the cost
class of the cut that feeds it, not of a transcription — and it cannot start
before that cut exists, so it runs inside the same `_job_slots` slot as the
cut, as a few more seconds of the same job. The durable SQLite queue earns
its complexity protecting minutes of CPU across a redeploy; a render lost to
a redeploy costs one more tap of the same button. Generalising the
episode-keyed listening queue for a per-clip task would have bought nothing
but states to maintain.

---

## 7. Queues and not falling over

Today: a global semaphore of 2 cuts, one job per user, and PTB's
`AIORateLimiter` — which bounds **outgoing** Telegram calls, not the incoming
flood. Handing out `src_`-tagged links across podcast chats will find every one
of these:

- ~~**A separate pool for transcription.**~~ **Done** — its own single slot,
  apart from the cut pool.
- ~~**Deduplicate by episode.**~~ **Done** — in `Indexer._in_flight` for
  concurrent callers, and now in the queue itself: the line is of episodes, so
  ten people wanting one episode are one job and ten waiters.
- ~~**Queue in SQLite, not memory.**~~ **Done** — `asr_jobs` in `store.py`,
  drained by the single worker in `listening.py`. A row is a waiter; a restart
  requeues whatever was mid-flight and finishes it. Deliberately **not** a
  `SCHEMA_VERSION` bump: the migration path rebuilds `_DERIVED_TABLES`, which
  includes `transcripts`, so bumping it to add an unrelated table would have
  cost production every warmed transcript it holds.
- ~~**Visible position**~~ **Done** — `2nd in line`, counted in episodes.
  **ETA is not**: an estimate needs the queue's own history of how long an
  episode of a given length actually took, which `measured_rtf` has for the
  running job but not for the ones behind it.
- ~~**A cap on queue length**~~ **Done** — `MAX_ASR_QUEUE` episodes, and
  joining an episode already in line is free because it adds no work.
- ~~**A per-user token bucket on input**: N searches/min, M cuts/hour, K
  transcriptions/day.~~ **Done** — `RATE_INPUT_PER_MINUTE`,
  `RATE_CUTS_PER_HOUR`, `RATE_ASR_PER_DAY`; 0 disables, admins exempt,
  refusals journalled as `action=limit`.
- ~~**A ceiling on source duration and size.**~~ **Done** —
  `MAX_SOURCE_SECONDS` and `MAX_SOURCE_BYTES`, checked before transcription as
  well as before a cut.
- ~~**Limits must apply to inline mode**~~ **Done** — the inline handler
  charges the same input budget and answers empty when it is spent, keeping
  the "open the bot" button so the polite path stays available.
- ~~**A kill switch**~~ **Done** — `ASR_ENABLED=false` stops transcription
  without stopping the bot.

Cache size is a non-issue: ~50 KB of text and ~0.8 MB of embeddings per
episode. Add LRU eviction anyway.

**What is left here**: an ETA beside the position, and LRU eviction. Neither
is load-bearing — the section's actual failure modes are closed.

---

## 8. Backups

Everything valuable is in the `podcast-data` volume: the journal, the recent
list, logs, and soon transcripts.

- **Never `cp` the database.** `journal_mode=WAL` is on; a copy under write is
  a torn database. Use `sqlite3 .backup`, then gzip.
- **Cron, N generations, off the volume** and ideally off the machine.
  Transcripts are the expensive artifact and must be included; audio is never
  cached and does not matter.
- **Off the machine now has an obvious destination:** Object Storage, from the
  same monthly grant that funds ASR (§4). The database and transcripts are tens
  of megabytes, so this costs approximately nothing against ₽18 000 — the part
  of the grant reserved for storage is more than enough, and it removes the
  "backup lives on the machine it protects" problem entirely.
- **Rehearse a restore once.** An untested backup is not a backup.
- **Order against retention:** `LOG_RETENTION_DAYS` purges journal rows at
  startup, so the backup must run before the purge or history walks out.

---

## 9. Cheap points, deliberately not skipped

- **CI** (pytest + ruff) — absent today, and it is worth real points.
- **Public repository.** The contest offers open-source and Habr support, which
  only applies to something public.
- **Real users before submission.** `?start=src_<tag>` and per-source stats
  already exist; seed tags across podcast chats a few weeks ahead so "value" is
  a number rather than a claim.
- **Search caching.** Identical searches each hit the API today; on stage that
  is latency in front of an audience.
- **Cancel should kill the ffmpeg job**, not just leave the screen. Small, and
  exactly the kind of thing a judge pokes.

Explicitly dropped: **the Mini App.** A waveform in a web view costs a frontend
and HTTPS hosting and scores less than transcript search.

---

## 10. The demo

The admission rule is "explainable in 3–5 minutes", so the demo is a designed
artifact, not an afterthought:

1. **0:30 — the pain.** Heard a great thought in a podcast, want to send it to
   a friend, and they will not listen to an hour.
2. **1:30 — live.** One search by meaning, one clip, one video note into a chat.
3. **1:00 — difficulty #1: stream-copy over HTTP range requests.** A clip from
   the middle of an hour-long episode in seconds, because only the needed bytes
   are fetched. This is the trick that gets remembered.
4. **1:00 — difficulty #2: CDNs that refuse this server.** With the numbers
   already measured — `traffic.megaphone.fm` is a sixth of the directory,
   Anchor answers 403 — and a `fallback` mode that leaves working routes alone.
5. **0:30 — numbers from `/stats`, and what is next.**

Plus one architecture diagram, and a recorded video as insurance: venue
internet fails more often than anyone plans for.

---

## 11. Order of work

0. ~~Scheme validation, `-protocol_whitelist`, redirect checking and a source
   duration ceiling.~~ **Done** — see §14.
1. ~~Transcription on `base`, cache in SQLite, FTS5 — minimal working search.~~
   **Done and deployed** — §15.
2. ~~RU/EN baskets and the pytest runner. **Before** tuning.~~ **Done** —
   answer key written and baselines committed; the by-ear pass remains. §16.
3. ~~Embeddings on top, negatives.~~ **Done** — hybrid retrieval with a
   measured refusal floor, §16a. The LLM judge remains open.
4. ~~SpeechKit as the second backend, and the comparison table.~~ **Done** —
   backend in `asr.py`, all four variants fixtured and guarded by CI, table
   and verdict in `HANDOFF.md` §3b. Remaining from §4: the spend ceiling,
   without which the cloud path stays an operator's manual choice.
5. ~~Queues, limits, source ceiling, backups.~~ **Done** — §7 is closed but
   for an ETA and LRU eviction; backups are live (§8, `docs/backup-policy.md`).
6. ~~Video notes with presets and subtitles.~~ **Done** — §6: four skins,
   subtitles from the warmed transcript, notes up to a minute and square
   video up to five, rendered in the cut pool by decision rather than by
   default.
7. CI, public repo, `src_` links seeded.

The video note is tempting to pull forward — it is fast and it is the wow. But
doing it before the baskets risks showing up with a meme instead of an AI
project. The measurements say the foundation is a couple of evenings, so the
order holds.

---

## 12. Answered by research (8 Aug 2026)

A research pass was run against current sources. Verdicts:

| Question | Verdict |
| --- | --- |
| Is AVX-only the ceiling? | **Unproven, and my reasoning was wrong** — see §13.1 |
| whisper.cpp on Ivy Bridge? | No current benchmark exists. Do not migrate on faith |
| Embedding model | `multilingual-e5-small`, CTranslate2 int8. `base` buys ~1.9 MRR points for 2.4× the parameters |
| FTS5 and Russian | No stemmer built in; `porter` is English, `trigram` is substring search. Lemmatise at index time with `pymorphy3` (alive, MIT — but its dictionaries date to 2022) |
| `sqlite-vec` | Still pre-v1, ANN alpha. Exact NumPy over a few hundred vectors is right |
| SpeechKit | No recurring free tier. Direct `content` upload works; word timings exist; "10 s per minute" is guidance, not an SLA |
| Whisper hallucination | Real and unsolved. VAD reduces, does not remove. Quarantine on metadata, do not trust thresholds to reject |
| Copyright | No safe number of seconds. A public RSS link grants nothing. See §13.4 |

## 13. What the research changed

Not everything in the report survived checking, so this section separates what
was adopted from what was rejected and why.

### 13.1 A correction to §3

The claim "threads do not save us, the wall is missing AVX2" **does not follow
from the measurement**. Sublinear 4 → 8 scaling is equally explained by
Whisper's autoregressive decoder, which does not parallelise well within one
recording, and by `--cpus` being a CFS quota that lets threads wander across
two sockets and land on SMT siblings and remote memory.

The missing AVX2 is real; its cost is unmeasured. What changes is the
experiment, not the architecture:

- pin with `cpuset-cpus` + `cpuset-mems` to physical cores of **one** socket,
  no HT siblings;
- scale 1/2/4/8 physical cores after a warm-up run;
- repeat under `CT2_FORCE_CPU_ISA=AVX`, and confirm the selected backend with
  `CT2_VERBOSE=1`.

That separates ISA cost from scaling cost. The `base`-as-operating-point
conclusion is unaffected.

### 13.2 Adopted, in priority order

1. **Enclosure URLs are third-party input and were only half-checked.** Two
   claims made while triaging this turned out to be overstated, and the
   accurate version is narrower:
   - `Episode.from_api` (`api.py:77`) *did* already require `http://` or
     `https://`, so `file:///…` could never arrive as an episode.
   - ffmpeg 8.1.2 already refuses a playlist naming `file:///etc/passwd`
     (`not in allowed_segment_extensions`), verified on the host.

   What was genuinely missing: nothing checked *where* a hostname pointed, and
   nothing checked the redirect chain, so a feed naming a host that resolves to
   `169.254.169.254` — or an ordinary CDN host that redirects there — had this
   server fetch from its own network. Fixed in §14.
2. **Bind the index to the audio, not to the episode.** Dynamic ad insertion
   means the same GUID can serve different bytes next month, and old timestamps
   then cut an ad. Store `source_sha256`, the post-redirect URL, ETag,
   Last-Modified, duration, and the ASR/chunker versions. Hash mismatch
   invalidates the transcript. Cheap now, expensive later.
3. **Sequential `transcribe`, never the batched pipeline.** In faster-whisper
   1.2.1 `compression_ratio_threshold`, `log_prob_threshold`,
   `no_speech_threshold`, `condition_on_previous_text` and
   `hallucination_silence_threshold` are unused or overridden under batching —
   exactly the parameters we would be relying on.
4. **Cluster overlapping windows before taking the top three**, or 30 s windows
   at 15 s stride return one moment three times and the feature looks broken.
5. **Hybrid lexical + dense from the first version.** Names, library
   identifiers, law numbers and exact quotes are the main query type here, and
   dense retrieval is weakest exactly there. FTS5 is already in SQLite and RRF
   needs no score calibration.
6. **Quarantine, don't reject.** Keep `avg_logprob`, `no_speech_prob`,
   `compression_ratio` and repetition signals per segment; one signal demotes,
   two independent signals drop from the index. The thresholds are fallback
   triggers, not guarantees.
7. Windows of ~30 s at 15 s stride, snapped to speech boundaries, with word
   timings kept so the clip is built from the matched words plus ~2 s of
   context rather than from the whole index window.

### 13.3 Rejected or unverified

- **`VibeVoice-ASR-BitNet`** — cited to `microsoft/VibeVoice`, which is a
  speech *synthesis* project. Looks like a conflation. Dropped.
- **`Harrier OSS v1`** — could not confirm the model exists. Irrelevant to us
  either way.
- The paragraph on EmbeddingGemma / Nomic v2 / GTE / BitEmbed cites a
  Qwen3-Embedding page that does not support it. Treat that whole comparison as
  unsourced.
- **OpenVINO 2026.0 requiring AVX2** — plausible, but the citation points at
  2025 release notes. Moot: we are not using OpenVINO.
- **GigaAM Multilingual** — plausible and genuinely interesting for Russian,
  but unverified, and a second runtime for one language means two
  normalisations, two timestamp behaviours and two sets of thresholds. Not now.
- **"ffmpeg with no network access"** — incompatible with this project. Cutting
  by HTTP range request straight from the CDN is the core trick and the best
  thirty seconds of the demo. The answer is a validated URL and a protocol
  whitelist, not an offline ffmpeg. The report did not notice the conflict.

### 13.4 Copyright: the risk is real, the prescription is not

The legal reading is sound — Article 1274 citation requires a justified
purpose, a justified extent, and named author and source; a public RSS URL
grants no licence; there is no safe number of seconds.

The recommendation — index nothing without publisher opt-in — is
disproportionate for a non-commercial personal project, and taken literally it
means an empty catalogue and nothing to demo. The proportionate stack is the
one the same report lists as risk-reducing, minus the opt-in gate:

- **Short clips by default.** `MAX_CUT_SECONDS` is currently **900** — a
  fifteen-minute extract is not a citation by any reading. This is the most
  exposed thing in the project and it predates this phase. Default to 30–60 s,
  with the ceiling reserved for the user's own explicit range.
- Attribution in the same message: show, episode, publisher, timestamps, and a
  link to the full episode.
- No permanent audio storage — already true.
- No walking an episode end to end in consecutive clips, and no way to pull the
  source audio through the bot.
- A complaint command and an immediate feed/episode denylist, with no
  requirement to prove infringement first.

Publisher opt-in remains the right answer *if* this ever stops being a pet
project.

### 13.5 Scaled down deliberately

The report specifies a 200-query basket with two annotators and an
eleven-field version matrix. For a deadline-bound pet project: **60–80 queries**
(30 RU and 30 EN positive, 10 + 10 null) is enough for the metric not to be
noise, one annotator with `large-v3` assistance is enough, and five version
fields — ASR model, chunker, embedding model, normaliser, `source_sha256` —
carry the reindex decisions that matter.

Worth keeping in full: running the baskets **twice**, once on a hand-corrected
transcript and once on ASR output. The gap between them is the price of `base`,
and without it you cannot tell whether to change the model or the retriever.

---

## 14. Done: bounding what an episode URL may reach

Step 0 of §11, shipped. **558 tests pass** (up from 448); ruff clean.

**New module `urls.py`.** `ensure_safe_source` refuses anything but `http`/
`https`, and refuses a hostname resolving into private, loopback, link-local
(cloud metadata), reserved or multicast space — checking *every* address a name
returns, since answering with one public and one private address is a real
technique. `redirect_guard` is an httpx response hook applying the same test to
each hop; it was verified that httpx fires response hooks on intermediate
redirects, so raising there stops the chain before the next request is made.

An unresolvable name is **allowed through** deliberately. The fetch that
follows fails a moment later with a message that names the real problem, while
refusing here would turn a DNS hiccup into a permanent, undiagnosable "unsafe"
verdict for a legitimate episode.

**`-protocol_whitelist` on every ffmpeg and ffprobe input:** remote inputs get
`http,https,tcp,tls,crypto` (no `file`), local ones get `file`. Verified on the
host against ffmpeg 8.1.2, because two things had to be true and neither was
obvious: the whitelist placed before `-i` constrains the input without
preventing the output being written through the file protocol, and it does
block a nested `file:` open (`Protocol 'file' not on whitelist`).

**`MAX_SOURCE_SECONDS`** (default 6 h) refuses an over-long episode after the
probe, on both the remote and the local path — the remote probe returns nothing
for some hosts, so the local check is a first look, not a repeat. Startup
refuses a ceiling below `MAX_CUT_SECONDS`, which would make every servable
episode unopenable.

**`UnsafeSourceError`**, code `unsafe_source`, keeps the journal's failure
taxonomy meaningful: a refusal is not a failed cut, it is a cut never
attempted, and `/stats` will show whether this ever fires in the wild.

**`allow_private_sources`** exists because the integration tests serve real
audio over a real HTTP server on `127.0.0.1`, which is what makes them worth
having. Off in production, asserted by a test.

One behaviour changed for the better: `file:///etc/passwd` used to be caught
inside the downloader, *after* a probe and a streaming cut had been attempted
against it. It is now refused before anything opens it.

### Not done here

`MAX_CUT_SECONDS` is still 900. Shortening the default clip is §13.4's call and
a product decision, not a security fix, so it is deliberately left for the step
that also adds attribution to the message.

---

## 15. In progress: transcription and lexical search

Step 1 of §11. The engine exists and is tested; it is not yet reachable from a
chat, which is the next piece.

**`transcripts.py`** holds everything that happens to recognised speech before
it becomes an answer, deliberately apart from any recogniser so it runs in
milliseconds without a model:

- *Quarantine.* Whisper invents text on silence and music, and an invention is
  indistinguishable from speech to an index — it becomes a confident wrong
  answer. Signals are collected (repetition, a decoding loop, low confidence
  agreeing with high no-speech probability, more words than the span can hold);
  one demotes, two independent ones exclude. Nothing is deleted: every metric
  stays on the row, because a quarantine decision has to be reviewable.
- *Windowing.* 30 s windows at a 15 s stride, so a phrase spanning two
  utterances lands whole inside some window.
- *Clustering.* At 50% overlap, neighbours say nearly the same thing, so hits
  are collapsed before the answer is cut to three — otherwise the top three are
  one moment shown three times while the retriever is working perfectly.
- *Placement.* `locate_phrase` finds the matched word's own timestamp and pads
  back 2 s, because word timings are not editing-grade.

**Schema v2** adds `transcripts`, `utterances`, `windows` and an
external-content FTS5 index with the triggers that keep it in step. A
transcript's identity is `(source_sha256, backend, model, chunker_version)` —
the bytes, not the episode id, for the dynamic-ad reason in §13.2. Everything
is written in one transaction: a transcript row without windows is an episode
that looks searchable and answers nothing, which is exactly what a crash
between two commits would leave.

**`asr.py`** is a one-method interface with faster-whisper behind it, so the
SpeechKit backend and the evaluation baskets both plug in without the pipeline
knowing. Sequential decoding and `temperature=0.0`, for the reasons in §13.2.

**`indexer.py`** is the pipeline: guarded fetch → hash → decode to 16 kHz mono
→ recognise → judge → window → store, then search. Concurrent askers for one
episode share a single job, so a crowd from one chat is one transcription and
many waiters. `ASR_ENABLED` is the kill switch.

**Tests: 635 passing**, of which about 90 are new. The recogniser is faked
throughout — what a fake cannot answer is whether a person's phrasing finds the
moment somebody actually said, and that question belongs to the baskets.
`scripts/check_transcribe.py` runs the real thing against a real episode.

### What a real episode showed

Run against «Запуск завтра», 53:13 of Russian audio, `base`, 8 cores:
**282 s, RTF 0.09**, language detected as `ru`, 212 windows, nothing
quarantined.

The negative case was genuinely negative — «квантовая телепортация» is absent
from the transcript and came back empty — and «белки» and «лекарства» found
real moments. But **«нейросети» found nothing in an episode about neural
networks.** The diagnostic printed what the recogniser had actually written:
«нейросетей» at 1:34 and 14:15, and the exact form «нейросети» not once. FTS5
matches a token literally, so the search for the episode's own subject was
empty.

Fixed with `pymorphy3` lemmatisation into a second indexed column. Verified on
the same episode: «нейросети» now returns 13:59, 49:58 and 1:12, and
«лекарство» in the singular finds «лекарств», «лекарство» and «лекарства».

Two things worth keeping in mind, both learned rather than assumed:

* **pymorphy3 guesses at words it does not know** rather than passing them
  through — «нейросеиц» becomes «нейросеица». Harmless, because the same guess
  applies to the query, and the surface-form index sits behind it for when the
  two guesses differ. A test says so, because the opposite is the intuitive
  assumption.
* **`base` mangles domain terms**: «нейросеиц», «оминокислого», «голки в 100
  гисены» for "иголки в стоге сена". Common words are fine. Searching for the
  garbled phrase correctly finds nothing, which is honest but is exactly the
  entity recall the baskets need to measure — the answer is a metric, not a
  bigger model chosen blind.

### Wired into the bot

Two screens: one asking what to look for, one showing what was found. The
clip editor grew a `🔎 Find a moment by what was said` button, hidden entirely
when `ASR_ENABLED` is off, and a found moment opens that same editor at its
timestamp — so everything already built for adjusting and cutting applies
without a second path through the code.

Decisions that were not obvious:

- **The waiting screen tells the truth about which case it is in.** A first
  search costs minutes and every later one is instant, so the screen checks
  before promising, and the progress message names the stage rather than
  repeating "still working". A user not told that the first search is slow
  assumes the bot has hung, and taps again.
- **A moment button carries its timestamp, not a list index.** Buttons outlive
  sessions; an index into a list that no longer exists is a wrong answer, where
  a timestamp is still correct on a message scrolled past days ago.
- **An empty answer is a screen, not an error.** It says the words may not have
  been spoken *or* may have been misheard, and it still offers a way on. It is
  also journalled as `empty` rather than `ok`, or the panel could not tell a
  working search from a useless one.
- **No recogniser is not a stopped bot.** If the library is missing or fails to
  load, the indexer is simply absent and everything that worked before still
  works.

Deployment: `HF_HOME` puts model downloads on the `/data` volume so a redeploy
does not refetch them, and `cpuset: "0-7"` pins the container to physical cores
of one socket — verified against `lscpu -e` on big-one, where 0–7 are cores 0–7
of socket 0 and their siblings are 16–23.

### Deployed, and what using it found

Live on big-one since 2026-08-11. The image went from 930 MB to 1.51 GB;
weights live on the volume, not in a layer. One episode is warmed into the
production index under its real id.

Three defects surfaced within an hour of real use, and every one of them was
invisible to a test suite that had never heard a real voice:

1. **«нейросети» found nothing in an episode about neural networks.** Fixed
   with lemmatisation — §15 above.
2. **The clip opened twenty seconds early.** The window was found on lemmas,
   but the word-level placement compared surface forms, so it could not find
   the very word that produced the hit and fell back to the window's start. The
   same bug as (1), one layer down.
3. **Answers quoted the wrong text.** Buttons showed the opening of a
   thirty-second window — which begins wherever the clock said — so three
   results read as three unrelated fragments, none containing the phrase.
   Quotations moved into the message; buttons only number and stamp.

Then, from a second round of use: a 30-minute episode looked hung, because a
first transcription is minutes behind one unchanging line. faster-whisper
yields segments as it decodes and each knows where it ends in the audio, so
the bar now measures real work, the remaining time is derived from work done
rather than the opening estimate, and the estimate itself comes from the
median of this host's own past runs.

**The lesson for the next step:** every one of these was found by a person
noticing something odd. That is not a method, and it does not scale to the
question the baskets exist to answer — how *often* is a search wrong, and in
which of the several possible ways.

### Still to do in this step

- English morphology. `unicode61` finds `protein` but does not connect it to
  `proteins`, and pymorphy3 is a Russian dictionary. FTS5 has `porter`, but a
  tokenizer is per table, so this is another column or another table. Left
  until the baskets say how much it actually costs.

---

## 16. Done: the baskets

Step 2 of §11. The apparatus is built, **all sixteen transcript fixtures
exist** — eight episodes × `base` and `large-v3` — and the answer key is
written: 36 positive + 16 negative per language, drafted from the reference
transcripts and text-verified against them (the by-ear pass is still owed —
the yamls say `method: text-verified` until it happens). The first committed
baselines:

```
run             hit@1  hit@3    err  false
en/reference    63.9%  66.7%   1.8s   0.0%
en/asr          52.8%  55.6%   1.9s   0.0%
ru/reference    61.1%  66.7%   1.0s   0.0%
ru/asr          36.1%  36.1%   1.6s   0.0%
```

Three readings worth writing down. `base` costs Russian **thirty points of
hit@3** and English eleven — so the Russian bottleneck is the recogniser, not
the retriever, and a better model (or SpeechKit, §4) is worth more than any
retrieval tuning for RU. `meaning` is 0% in all four runs — the embeddings
step (§11.3) now starts from a number instead of a feeling. And the negatives
are clean in every run, so the refusal behaviour survives both models.

What the fixtures already say, before a single query is written:

| | reference (`large-v3`) | asr (`base`) |
| --- | --- | --- |
| utterances | 371–1672 | 354–1325 |
| words | within 1% of each other, every episode | |
| quarantined | **0 in all eight** | 17, 9, 2, 1 in four of them |

The word counts landing within a percent is the reassuring part: both models
hear the same *amount* of speech, and the whole difference is in what they
wrote and how they cut it. The quarantine column is the interesting part —
every decoding loop and «бззззз» artifact belongs to `base`, so the defect
below is a property of what ships, invisible in the reference, and therefore
something the gap between the two runs will price directly.

### The design decision that changed the shape

§5 assumed one run in CI and one run by hand. That is backwards. The expensive
part is *producing* a transcript, not searching one — searching is milliseconds
— so **both** transcripts are committed as fixtures and **both** runs are
offline, deterministic, and in CI. The gap between them is then a number CI
reports on every push rather than a number somebody occasionally remembers to
go and measure.

The cost is that a fixture has to be regenerated when the model changes, which
is exactly the event `TranscriptKey`'s `asr_model` field already tracks.

### What exists

* **`podcast_cutter/evals.py`** — the query classes, the scoring, the roll-up
  and the runner. It goes through the real `Indexer.search` and the real store
  rather than calling the retrieval helpers directly: an eval that reimplements
  the path it measures measures the reimplementation, and both defects this
  project has had in placement and clustering lived *between* those helpers.
* **`evals/baskets/{ru,en}.yaml`** — four shows per language, chosen for
  recording condition as §5 asks, with every enclosure checked reachable from
  big-one first. An episode this server cannot fetch is not a candidate, and
  several obvious ones are not: BBC's `open.live.bbc.co.uk` read-timed out on
  both routes, and Радио-Т — the best Russian crosstalk case there is — never
  came back with a duration the API would filter on.

  | | show | episode | min | condition |
  | --- | --- | --- | --- | --- |
  | ru | Запуск завтра | нейросети и лекарства | 53 | studio |
  | ru | Мы обречены | «Вот уволюсь и сделаю свой стартап» | 63 | remote |
  | ru | Дневники Лоры Палны | «Шпионы» | 59 | studio |
  | ru | Заварили бизнес | итоги сезона, ч.1 | 28 | field |
  | en | 99% Invisible | The Borrowed Nature of Biomimicry | 32 | studio |
  | en | Hidden Brain | How Feelings Make Us Smarter | 48 | studio |
  | en | This Week in Startups | E2308 | 58 | remote |
  | en | TWISTA | Investor Special | 29 | remote |

  Two of those are chosen for being hard rather than representative. The
  Заварили episode is published «без редактуры и монтажа» — unedited, with
  crosstalk and uneven levels — and TWISTA is Australian, where Whisper's
  English training skews heavily North American. A basket of four well-produced
  US shows would report a number that does not survive the directory.
* **`tests/test_baskets.py`** — runs both baskets over both variants, prints
  the table unconditionally, and fails on regression.
* **`scripts/make_fixtures.py`** — the expensive half. Resumable, it keeps the
  decoded audio so both variants come from one recording, and it records the
  **download's** SHA-256 and refuses to write a variant whose source no longer
  matches a sibling's. That guard exists because the reference pass runs on a
  different machine days later, which is exactly when dynamic ad insertion
  would make the two runs a comparison of two different recordings. Checked at
  the time: re-fetching `hidden-brain-feelings` and `twist-duct-tape` — both
  behind podtrac/simplecast/libsyn chains — came back byte-identical, so the
  hazard is not live today. That is a fact about one afternoon, not a property
  of podcast hosting, hence the guard.

  **It hashed the decoded PCM first, and that was wrong.** The first reference
  pass on a laptop failed on episode one: macOS ffmpeg and the container's
  7.1.5 turn one identical mp3 into two different byte streams, so the check
  fired everywhere and blamed the feed for it. Diagnosed rather than guessed —
  big-one re-downloading *and* re-decoding `zapusk-neuro` reproduced the stored
  hash exactly, which puts the difference in the laptop's decoder and nowhere
  else. Hashing the download is both correct and what `indexer._transcribe`
  already did. The mistake was inventing a second, cleverer notion of identity
  beside the one production had already settled on.

  It needs no `poetry install`: `faster-whisper httpx python-dotenv pymorphy3
  pyyaml` and ffmpeg are enough, verified by running it in a bare
  `python:3.12-slim` with no `python-telegram-bot` present at all.
* **`scripts/draft_queries.py`** — the part that makes writing sixty queries an
  evening instead of a week. `--draft` proposes candidates by how a word is
  distributed across the basket, which needs no stopword list and no dictionary
  — both of which would be one more thing to be wrong in a second language.
  The useful case is the negatives: a word the *other* episodes lean on and
  this one never says is a negative a listener could plausibly type and be
  wrong about, which is a better test than one somebody invented.
  `--verify` then prints the reference text around each claimed timestamp
  beside what both variants return, so a wrong `at:` shows up as text that does
  not contain the phrase.
* **`scripts/bench_asr.py`** — the model-cost bench §13.1 asked for.

### A basket asserts no regression, not a quality bar

Nobody knows in advance what `hit@3` *should* be on four particular episodes,
and a threshold invented before the first measurement is a guess wearing a
requirement's clothes. So each basket carries a `baseline:` block of the
numbers it last produced, and the test fails when a number moves the wrong way
by more than one query's worth of wobble. A number that improves is re-committed
deliberately, which makes the git history of that block the record of whether
the search is getting better.

Slack is per metric and not one figure: rates are fractions and the start error
is seconds, so a single tolerance would be either hair-trigger on one or inert
on the other.

### Measured: what a reference transcript costs here

The plan said "generate with `large-v3` on the M2 Pro". Before building around
that, `large-v3` was measured on big-one itself — 180 s RU sample, int8,
`--cpuset-cpus 8-15 --cpuset-mems 1`, i.e. eight physical cores of the socket
production is *not* pinned to:

| Model | load | decode 180 s | RTF | 53-min episode |
| --- | --- | --- | --- | --- |
| `base` | 6.0 s | 14.3 s | **0.079** | ~4.2 min |
| `medium` | 23.0 s | 119.7 s | **0.665** | ~35 min |
| `large-v3` | 42.0 s | 225.4 s | **1.252** | ~67 min |

`base` at 0.079 reproduces the 0.07–0.09 in §3, which is what makes the other
two rows believable. The extrapolation that had been reasoned from parameter
counts said 1.5–2 h per episode for `large-v3`; the measurement says 1.1 h.

**Then the real pass ran, and the sample was 17% pessimistic.** Eight episodes,
6.13 h of audio, finished in 6.54 h — an aggregate RTF of **1.068** against the
1.252 the 180-second sample predicted. A three-minute excerpt of continuous
speech is denser than a whole episode, where VAD drops the pauses. Russian cost
about 8% more than English (~1.10 against ~1.02), which is worth remembering
the next time a duration is estimated for a Russian feed.

### The laptop was not faster, which is why it was worth measuring

§5 said to generate the reference on an M2 Pro, and the plan was built on that
being several times quicker than the old Xeons. It is not. The same episode,
`large-v3` int8, on both:

| | RTF | wall clock |
| --- | --- | --- |
| M2 Pro, 10 threads | 1.06 | 56 min |
| big-one, 8 pinned cores | 1.10 | 58 min |

Four percent. CTranslate2's int8 kernels are far better optimised for x86 than
for ARM, and its CPU path uses none of Apple's accelerators; four of the ten
threads also landed on efficiency cores. So the whole reason for the manual
off-host step — speed — did not exist, and the pass moved back to the server,
where the audio was already cached and nobody's fans were involved.

The run itself is pinned to socket 1 with `--cpu-shares 256`. `cpuset` confines
the container to those cores but does **not** reserve them, so the low share is
what actually keeps a Jellyfin transcode ahead of an overnight eval.

**The quality difference is the class that breaks lexical search**, which is
the part RTF cannot tell you. On the same sample:

| `base` | `large-v3` |
| --- | --- |
| «в изотипервелении защиты» | «без этой первой линии защиты» |
| «первые статии» | «первые стадии» |
| «в одной инстрации» | «Новая администрация» |
| «привык**слышать**» | «привык слышать» |

The last one matters more than it looks: FTS5 tokenises on word boundaries, so
a glued pair is one token and a search for either half misses it entirely.

And the honest limit: «асимптотически» came back wrong from `base`, `medium`
*and* `large-v3`. A `large-v3` transcript is a much better reference, not a
correct one — which is the whole argument for the hand pass below.

### Ground truth: spot-verified, plus two episodes in full

Hand-transcribing six hours is not realistic and short excerpts would cheat:
with only ten minutes of context, "this was never said" is trivially easy and
the false-hit rate — the number the negatives exist to produce — comes out
flattering. So:

* `large-v3` produces the reference transcript;
* queries are drafted *from* it, so the timestamp is usually already right;
* the ±30 s around each answer is listened to and corrected — 60 queries at
  about a minute each, an evening rather than a week;
* **one RU and one EN episode are corrected in full**, which is what makes the
  cheap method auditable: it measures how much the unchecked reference
  flatters itself, and it gives a real WER figure at two points.

### Found while building it: the quarantine's two-signal rule has a hole

Not looked for. The drafting tool proposes candidate queries by picking out
distinctive words, and it proposed a 448-character one:

```
obrecheny-startup, 54:30
  «но не, но не генегенегенегенегенеген…»  (448 chars)
  compression_ratio = 24.08   avg_logprob = -0.06   no_speech_prob = 0.50
  signals = ['repetitive']    indexable = True
```

A textbook Whisper decoding loop — the exact failure §13.2's quarantine exists
for — **stays in the index**, because `is_indexable` needs two independent
signals and this trips only one. Each of the others misses for a reason:

* `looping` counts a repeated four-word phrase, and the loop is inside a
  *single token*, so there are fewer than four words to count;
* `silence` needs `no_speech_prob > 0.6` *and* `avg_logprob < -1.0`, and the
  model is supremely confident in its invention: −0.06;
* `unsure` needs the same low confidence, so it misses too;
* `too_dense` counts words per second, and this is one enormous word.

`lora-spies` has the same shape at 20:58 — «бззззззз…», `compression_ratio`
6.06, one signal, indexed.

**Not fixed until the baskets could price it — then fixed.** A compression
ratio ten times the threshold is conclusive on its own, and the fix is exactly
the obvious one: past `CONCLUSIVE_COMPRESSION_RATIO` (2× the threshold) the
ratio counts as a second signal, and the two-signal rule convicts. The order
mattered: with baselines committed first, the after-run could show that **no
metric moved in any of the four runs** — the fix removed only hallucinated
windows. That is the difference between "this change is safe" as a claim and
as a measurement.

Worth noting what this says about the method: the baskets caught something in
their first hour of existence, and it was caught by the *drafting* tool, before
a single query had been written.

### Checked while building it: English gets no morphology at all

§15 recorded the English gap as "pymorphy3 is a Russian dictionary", which
implied it does something imperfect to English words. It does not do anything:
`lemmatize("proteins folding rapidly")` returns the string unchanged, and the
same for `investors`, `companies`, `studies`. pymorphy3's guesser never fires
on Latin script.

So the lemma column is **inert** for English rather than unreliable, and both
indexed columns match literally. That is a cleaner problem than the one §15
described — nothing has to be undone, something has to be added.

The drafting tool then showed what it costs without being asked. Run over
`invisible-biomimicry`, its own candidate list contains the evidence:

```
mention candidates      15×  tunnel        0:19, 4:23, 4:52, 5:02, 7:51
                         7×  tunnels       4:28, 4:34, 5:56, 5:57, 7:21
negative candidates     53×  emotional
                        37×  emotions
```

`tunnel` and `tunnels` are two different index tokens describing one subject,
so a search for either misses the other's occurrences outright. Those pairs are
the cheapest possible measurement of the missing stemmer, and they write
themselves into the EN `quote` class.

Worth noting alongside: English entity mangling is not milder than Russian.
`base` wrote `biomemically` for "biomimetically" and `endomologist` for
"entomologist" in the same episode.

### Metrics

`hit@3` at ±15 s is the headline, with `hit@1`, the median start error, and the
false-hit rate on negatives beside it, RU and EN separately and split again by
query class — `quote` tests the recogniser, `meaning` tests the retriever,
`negative` tests the refusal, and one average over the three says nothing about
which one moved.

Two details that are not cosmetic:

* **A positive query carries a *list* of acceptable timestamps.** The `mention`
  class exists for phrases said several times; with one reference, answering at
  the second and third real occurrences would score as two misses.
* **The median start error is reported, not the mean.** Placement lands either
  on the matched word or, when `locate_phrase` cannot find it, on the window
  start. That is bimodal, and a mean over it describes neither case.
* **Its floor is ~2 s, not 0.** A reference names when the word was said and
  `CLIP_LEAD_IN` opens the clip two seconds earlier on purpose, so perfect
  placement measures as two seconds of error. Worth writing down before the
  first person reads 2.0 s as a defect.

## 16a. Done: hybrid retrieval, priced by the basket it was built under

Step 3 of §11, minus the LLM judge. `multilingual-e5-small` (§12's verdict)
on CTranslate2 int8 — the engine faster-whisper already ships, so the image
gains no dependency, only 120 MB of weights on the volume. Windows are
embedded at index time (seconds, against recognition's minutes), a query
fuses lexical and dense hits by reciprocal rank (§13.2: RRF needs no score
calibration), and the answer pipeline downstream — clustering, placement,
dedup — is untouched.

**The refusal floor was measured, not chosen.** Over all 208 basket queries:
the strongest absolute similarity any negative reaches is 0.842, the
strongest margin over its episode's median is 0.0545 — and no negative
clears both. So a dense hit must clear both (0.84 and 0.05), which held
false hits at **0.0% in all eight runs** while buying:

```
             lexical→hybrid, hit@3
en/reference   66.7 → 72.2
en/asr         55.6 → 69.4   ← +13.8, dense rescues base's garbles
ru/reference   66.7 → 69.4
ru/asr         36.1 → 44.4
meaning class  0% → up to 25% (en/asr @3)
```

The en/asr jump is the surprise worth keeping: dense retrieval recovers
quotes whose words `base` mangled — «hypochondriac» lost by the recogniser is
still found by meaning. The honest limits, also on record: ru/asr meaning is
still 0% (base's Russian garbles poison window embeddings too), and e5-small
packs every score into a 0.78–0.87 band, which is why the floor is a
conjunction of two razor-thin criteria rather than one comfortable number. A
bigger e5 is the obvious next experiment, and the `+e5` baseline rows will
price it the day it is tried.

### Smoke-run on real audio, before any of it was believed

Six hand-made queries against the committed `base` transcript of
`zapusk-neuro` — not the answer key, just enough to prove the path works on a
790-utterance transcript rather than on the synthetic one in the unit tests:

```
run                        n  hit@1  hit@3    err  false
ru/asr                     6 100.0% 100.0%   2.6s   0.0%
```

Both negatives came back empty, «нейросети» returned three distinct moments —
so §15's lemmatisation fix still holds — and «лекарство» landed on three
different occurrences, which is the clustering working. One query, «белки»,
scored `distinct=1` against four references: two of the returned moments were
real occurrences the hand-made list simply did not contain. That is not a
tooling failure, it is the reason `at:` has to be written from the transcript
rather than from memory, and it is exactly what the drafting tool is for.
