"""Stage 5: quality checks + build the rendered manifest the publisher reads."""
from __future__ import annotations

import argparse
import hashlib
import pathlib
import subprocess
import sys

from . import config, jobs, state

MIN_BYTES = 200_000
PROFANITY = {
    "damn", "shit", "fuck", "bitch", "asshole", "bastard", "crap",
    "nazi", "kill yourself", "retard",
}


def _probe(path: str) -> dict:
    try:
        out = subprocess.run(
            [config.ffprobe_bin(), "-v", "error",
             "-show_entries", "format=duration,size:stream=codec_type,width,height",
             "-of", "json", path],
            check=True, capture_output=True, text=True, timeout=60,
        )
        import json

        data = json.loads(out.stdout)
        fmt = data.get("format", {})
        streams = data.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), {})
        audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
        return {
            "duration": float(fmt.get("duration", 0) or 0),
            "size": int(fmt.get("size", 0) or 0),
            "width": int(video.get("width", 0) or 0),
            "height": int(video.get("height", 0) or 0),
            "has_audio": bool(audio),
        }
    except Exception as exc:
        return {"error": str(exc)[:300]}


def _clean_caption(text: str) -> bool:
    low = str(text).lower()
    return not any(p in low for p in PROFANITY)


def run() -> tuple[int, int]:
    job = jobs.start("qa")
    queue = state.load_queue()
    rendered = state.load_rendered()
    posted = state.load_posted()
    seen_hashes = {
        p.get("hash") for p in posted.get("posts", []) if p.get("hash")
    }

    manifests: list[dict] = []
    passed = failed = 0

    for item in queue.get("clips", []):
        for clip in item.get("clips", []):
            if clip.get("stage") != "rendered":
                continue
            path = pathlib.Path(clip["path"])
            problems: list[str] = []

            if not path.is_file():
                problems.append("file missing")
            elif path.stat().st_size < MIN_BYTES:
                problems.append("file too small")

            info = _probe(str(path)) if path.is_file() else {}
            if "error" in info:
                problems.append(f"ffprobe: {info['error']}")
            else:
                if not info.get("has_audio"):
                    problems.append("no audio stream")
                if (info.get("width"), info.get("height")) != (1080, 1920):
                    problems.append(f"resolution {info.get('width')}x{info.get('height')} != 1080x1920")
                settings = item.get("settings", {})
                min_d = float(settings.get("min_duration", 18)) - 3
                max_d = float(settings.get("max_duration", 60)) + 3
                if info.get("duration", 0) < min_d:
                    problems.append(f"too short ({info.get('duration', 0):.1f}s)")
                if info.get("duration", 0) > max_d:
                    problems.append(f"too long ({info.get('duration', 0):.1f}s)")

            if not _clean_caption(str(clip.get("hook", "")) + " " + str(clip.get("caption", ""))):
                problems.append("profanity in caption/hook")

            h = hashlib.sha256(path.read_bytes()).hexdigest()[:24] if path.is_file() else ""
            if h and h in seen_hashes:
                problems.append("duplicate of an already-posted clip")

            if problems:
                clip["stage"] = "failed"
                clip["qa"] = problems
                failed += 1
                state.warn("qa", f"{clip.get('clip_id')}", problems=problems)
                continue

            clip["stage"] = "qa_passed"
            clip["hash"] = h
            clip["meta"] = {
                "width": info.get("width"),
                "height": info.get("height"),
                "duration": info.get("duration"),
                "size": info.get("size"),
            }
            manifests.append(
                {
                    "clip_id": clip.get("clip_id"),
                    "campaign": item.get("campaign"),
                    "path": str(path),
                    "hash": h,
                    "hook": clip.get("hook"),
                    "title": clip.get("title"),
                    "caption": clip.get("caption"),
                    "hashtags": clip.get("hashtags", []),
                    "duration": clip.get("duration"),
                    "meta": clip.get("meta"),
                    "stage": "qa_passed",
                    "r2_url": None,
                }
            )
            passed += 1

    rendered["clips"] = manifests
    state.save_rendered(rendered)
    state.save_queue(queue)
    jobs.finish(job, status="ok" if not failed else "partial", items=passed, failed=failed)
    return passed, failed


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description="QA rendered clips").parse_args(argv)
    ok, bad = run()
    print(f"qa: {ok} passed, {bad} failed")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
