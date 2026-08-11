"""How fast a Whisper model actually runs on this host, and on which cores.

`ROADMAP.md` §3 picked `base` as the operating point from a table of RTF
numbers, and §13.1 records why that table is not yet an explanation: the
sublinear 4 → 8 scaling was attributed to the missing AVX2, and that does not
follow from the measurement. `--cpus` is a CFS quota, so threads wander across
two sockets and land on SMT siblings and remote memory; Whisper's decoder is
autoregressive and does not parallelise well within one recording either.

This script exists so those are separate questions rather than one guess:

    # a model's cost, on physical cores of one socket
    python scripts/bench_asr.py --sample /bench/sample_ru_180.wav \\
        --models base,large-v3 --threads 8

    # how it scales, same cores, same sample
    python scripts/bench_asr.py --sample /bench/sample_ru_180.wav \\
        --models base --threads 1,2,4,8

Pin the container, do not use `--cpus`: on this host socket 0 is CPUs 0-7 and
socket 1 is CPUs 8-15, with SMT siblings at 16-23 and 24-31. Production is
pinned to 0-7, so a bench belongs on the other socket:

    docker run --rm --user root --cpuset-cpus 8-15 --cpuset-mems 1 ...

Decoding options are copied from `asr.LocalWhisper` rather than chosen here.
A bench that decodes differently from production measures something production
never runs.
"""

from __future__ import annotations

import argparse
import time
import wave
from pathlib import Path


def _audio_seconds(path: Path) -> float:
    """Length of the sample, from the WAV header.

    The sample is PCM by construction — this script's whole input contract is
    what the recogniser wants — so the header is authoritative and this needs
    neither ffprobe nor a subprocess.
    """
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / float(handle.getframerate())


def _run_once(model_name: str, sample: Path, threads: int, compute: str) -> dict:
    from faster_whisper import WhisperModel

    loading = time.monotonic()
    model = WhisperModel(
        model_name,
        device="cpu",
        compute_type=compute,
        cpu_threads=threads,
    )
    load_s = time.monotonic() - loading

    started = time.monotonic()
    segments, info = model.transcribe(
        str(sample),
        beam_size=1,
        vad_filter=True,
        word_timestamps=True,
        condition_on_previous_text=False,
        temperature=0.0,
    )
    # `segments` is a generator and nothing is decoded until it is consumed;
    # timing the call alone would time building an iterator.
    text = " ".join(segment.text.strip() for segment in segments)
    decode_s = time.monotonic() - started

    return {
        "load_s": load_s,
        "decode_s": decode_s,
        "language": getattr(info, "language", None),
        "text": text,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", required=True, type=Path)
    parser.add_argument("--models", default="base")
    parser.add_argument("--threads", default="8")
    parser.add_argument("--compute", default="int8")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument(
        "--transcript-dir",
        type=Path,
        help="Write each run's text here, so quality is compared by eye too.",
    )
    args = parser.parse_args()

    seconds = _audio_seconds(args.sample)

    try:
        import ctranslate2

        print(
            f"ctranslate2 {ctranslate2.__version__}, cpu compute types: "
            f"{sorted(ctranslate2.get_supported_compute_types('cpu'))}"
        )
    except Exception as exc:  # pragma: no cover - diagnostics only
        print(f"ctranslate2 not introspectable: {exc}")

    print(f"sample {args.sample} — {seconds:.1f}s of audio\n")
    print(f"{'model':<12} {'thr':>4} {'load':>7} {'decode':>9} {'RTF':>7}  lang")

    for model_name in args.models.split(","):
        for threads in (int(value) for value in args.threads.split(",")):
            for attempt in range(args.repeat):
                result = _run_once(
                    model_name.strip(), args.sample, threads, args.compute
                )
                rtf = result["decode_s"] / seconds
                print(
                    f"{model_name:<12} {threads:>4} "
                    f"{result['load_s']:>6.1f}s {result['decode_s']:>8.1f}s "
                    f"{rtf:>7.3f}  {result['language']}",
                    flush=True,
                )
                if args.transcript_dir and attempt == 0:
                    args.transcript_dir.mkdir(parents=True, exist_ok=True)
                    target = args.transcript_dir / f"{model_name}-{threads}.txt"
                    target.write_text(result["text"], encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
