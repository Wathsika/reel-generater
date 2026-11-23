"""Stage 2: transcribe sources to word-level timestamps.

Groq whisper-large-v3 (free tier) by default; falls back to local
faster-whisper if WHISPER_BACKEND=local or no API key is present.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import tempfile

from . import config, jobs, state

CHUNK_SECONDS = 600  # stay well under API upload limits
SAMPLE_RATE = 16000


def _extract_audio(src: str, dest: pathlib.Path) -> pathlib.Path | None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                config.ffmpeg_bin(), "-y", "-i", src,
                "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
                "-c:a", "libmp3lame", "-b:a", "48k",
                str(dest),
            ],
            check=True,
            capture_output=True,
            timeout=1200,
        )
        return dest if dest.exists() and dest.stat().st_size > 0 else None
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        state.error("transcribe", "audio extraction failed", reason=str(exc)[:300])
        return None


def _duration(path: str) -> float:
    try:
        out = subprocess.run(
            [config.ffprobe_bin(), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            check=True, capture_output=True, text=True, timeout=60,
        )
        return float(out.stdout.strip())
    except Exception:
        return 0.0


def _chunk(audio: pathlib.Path, start: float, length: float, out: pathlib.Path) -> None:
    subprocess.run(
        [config.ffmpeg_bin(), "-y", "-ss", str(start), "-t", str(length), "-i", str(audio),
         "-c:a", "libmp3lame", "-b:a", "48k", str(out)],
        check=True, capture_output=True, timeout=600,
    )


def _transcribe_groq(chunk: pathlib.Path) -> list[dict]:
    import requests

    key = config.env("GROQ_API_KEY")
    if not key:
        raise RuntimeError("GROQ_API_KEY not set")
    with open(chunk, "rb") as fh:
        resp = requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {key}"},
            files={"file": (chunk.name, fh, "audio/mpeg")},
            data={
                "model": "whisper-large-v3",
                "response_format": "verbose_json",
                "timestamp_granularities[]": "word",
            },
            timeout=300,
        )
    resp.raise_for_status()
    payload = resp.json()
    words = payload.get("words") or []
    if words:
        return [{"word": w.get("word", ""), "start": w["start"], "end": w["end"]} for w in words]
    # fall back to segment-level if word granularity unavailable
    return [
        {"word": seg.get("text", "").strip(), "start": seg["start"], "end": seg["end"]}
        for seg in payload.get("segments", [])
    ]


def _transcribe_local(chunk: pathlib.Path) -> list[dict]:
    from faster_whisper import WhisperModel  # optional dependency

    model = WhisperModel("small", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(chunk), word_timestamps=True, vad_filter=True)
    words: list[dict] = []
    for seg in segments:
        for w in seg.words or []:
            words.append({"word": w.word, "start": float(w.start), "end": float(w.end)})
    return words


def transcribe_one(src: str) -> dict:
    audio = pathlib.Path(src).with_suffix(".audio.mp3")
    if _extract_audio(src, audio) is None:
        raise RuntimeError("could not extract audio")
    total = _duration(str(audio))
    if total <= 0:
        raise RuntimeError("could not determine audio duration")

    backend = (config.env("WHISPER_BACKEND", "") or ("groq" if config.env("GROQ_API_KEY") else "local")).lower()
    words: list[dict] = []
    with tempfile.TemporaryDirectory() as td:
        for idx, start in enumerate(range(0, int(total), CHUNK_SECONDS)):
            length = min(CHUNK_SECONDS, total - start)
            piece = pathlib.Path(td) / f"chunk_{idx:03d}.mp3"
            _chunk(audio, start, length, piece)
            fn = _transcribe_groq if backend == "groq" else _transcribe_local
            try:
                batch = fn(piece)
            except Exception as exc:
                if backend == "groq":
                    state.warn("transcribe", "groq failed, trying local", reason=str(exc)[:200])
                    batch = _transcribe_local(piece)
                else:
                    raise
            for w in batch:
                words.append({**w, "start": w["start"] + start, "end": w["end"] + start})

    return {"duration": total, "words": words, "backend": backend}


def run() -> int:
    job = jobs.start("transcribe")
    queue = state.load_queue()
    done = failed = 0
    for item in queue.get("clips", []):
        if item.get("stage") not in {"discovered", "transcribing"}:
            continue
        item["stage"] = "transcribing"
        state.save_queue(queue)
        try:
            result = transcribe_one(item["path"])
            tpath = pathlib.Path(item["path"]).with_suffix(".words.json")
            tpath.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            item["transcript"] = str(tpath)
            item["duration"] = result["duration"]
            item["stage"] = "transcribed"
            done += 1
        except Exception as exc:
            item["stage"] = "failed"
            item["error"] = str(exc)[:400]
            state.error("transcribe", f"failed for {item.get('source_url')}", reason=str(exc)[:300])
            failed += 1
        state.save_queue(queue)

    jobs.finish(job, status="ok" if not failed else "partial", items=done, failed=failed)
    return done


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description="Transcribe sources").parse_args(argv)
    n = run()
    print(f"transcribed {n} source(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
