"""Stage 4: render vertical clips with ffmpeg.

Layouts:
  crop        - 9:16 centre crop of the single source (default)
  split_v     - speaker top / gameplay bottom (needs campaign gameplay_source)
  gameplay_bg - gameplay full-screen with the speaker in a top-left box
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

from . import captions as cap
from . import config, jobs, state

W, H = 1080, 1920


def _run(cmd: list[str], cwd: pathlib.Path | None = None, timeout: int = 1800) -> None:
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace")
        # ffmpeg dumps its build config at the end on failure - strip it so the
        # real error is what we log.
        lines = [
            l for l in err.splitlines()
            if "enable-" not in l and not l.startswith("--") and "built with gcc" not in l
        ]
        tail = "\n".join(lines[-25:])[-1200:]
        raise RuntimeError(f"ffmpeg failed ({proc.returncode}): {tail}")


def _base_args() -> list[str]:
    return [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-r", "30",
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
    ]


def _crop_graph(src: str, start: float, dur: float, ass_name: str) -> list[str]:
    vf = (
        f"crop='min(iw,ih*9/16)':'min(ih,iw*16/9)',"
        f"scale={W}:{H}:flags=lanczos,setsar=1,ass={ass_name}"
    )
    return ["-ss", str(start), "-t", str(dur), "-i", src, "-vf", vf, "-map", "0:v", "-map", "0:a"]


def _split_graph(src: str, gameplay: str, start: float, dur: float, ass_name: str) -> list[str]:
    fc = (
        f"[1:v]scale={W}:960:force_original_aspect_ratio=increase,"
        f"crop={W}:960,setsar=1[top];"
        f"[0:v]scale={W}:960:force_original_aspect_ratio=increase,"
        f"crop={W}:960,setsar=1,setsar=1[bot];"
        f"[top][bot]vstack=inputs=2,ass={ass_name}[v]"
    )
    return [
        "-stream_loop", "-1", "-t", str(dur), "-i", gameplay,
        "-ss", str(start), "-t", str(dur), "-i", src,
        "-filter_complex", fc, "-map", "[v]", "-map", "1:a",
    ]


def _gameplay_bg_graph(src: str, gameplay: str, start: float, dur: float, ass_name: str) -> list[str]:
    fc = (
        f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
        f"crop={W}:{H},setsar=1[bg];"
        f"[1:v]scale=520:-2[pc];"
        f"[bg][pc]overlay=24:24,ass={ass_name}[v]"
    )
    return [
        "-stream_loop", "-1", "-t", str(dur), "-i", gameplay,
        "-ss", str(start), "-t", str(dur), "-i", src,
        "-filter_complex", fc, "-map", "[v]", "-map", "1:a",
    ]


def _load_words(item: dict) -> list[dict]:
    tpath = pathlib.Path(item["transcript"])
    if not tpath.is_file():
        return []
    return json.loads(tpath.read_text(encoding="utf-8")).get("words", [])


def _fetch_gameplay(url: str) -> pathlib.Path | None:
    if not url:
        return None
    if url.startswith("local:"):
        p = pathlib.Path(url[6:])
        return p if p.is_file() else None
    dest = config.WORK / "gameplay" / ("".join(c if c.isalnum() else "_" for c in url)[-60:] + ".mp4")
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["yt-dlp", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
             "--merge-output-format", "mp4", "-o", str(dest), "--no-playlist", url],
            check=True, capture_output=True, timeout=1800,
        )
        return dest if dest.exists() else None
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        state.warn("render", "gameplay download failed", reason=str(exc)[:200])
        return None


def run() -> int:
    job = jobs.start("render")
    queue = state.load_queue()
    out_dir = config.OUT / state.today()
    cap_dir = config.WORK / "captions"
    out_dir.mkdir(parents=True, exist_ok=True)
    cap_dir.mkdir(parents=True, exist_ok=True)

    rendered = 0
    failed = 0

    for item in queue.get("clips", []):
        # Gate on the clips themselves, not the item stage: an item stays
        # "rendered" forever after its first run, which would otherwise make
        # every re-run silently skip it.
        pending = [c for c in item.get("clips", []) if c.get("stage") == "selected"]
        if not pending:
            continue
        settings = item.get("settings", {})
        layout = str(settings.get("layout", "crop"))

        # ffmpeg runs with cwd set to the caption dir (so the ass= filter can
        # use a plain filename), so every path must be absolute here.
        if item.get("path"):
            item["path"] = str(pathlib.Path(item["path"]).resolve())
        if item.get("transcript"):
            item["transcript"] = str(pathlib.Path(item["transcript"]).resolve())

        words = _load_words(item)
        gameplay_path: pathlib.Path | None = None

        if layout in {"split_v", "gameplay_bg"}:
            gameplay_path = _fetch_gameplay(str(settings.get("gameplay_source") or ""))
            if gameplay_path is None:
                state.warn("render", "no gameplay source, falling back to crop", campaign=item.get("campaign"))
                layout = "crop"
            else:
                gameplay_path = gameplay_path.resolve()

        for idx, clip in enumerate(item.get("clips", [])):
            if clip.get("stage") in {"rendered", "published"}:
                continue
            clip_id = f"{item['campaign'] or 'src'}_{int(clip['start'])}_{idx}"
            safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in clip_id)
            out_path = out_dir / f"{safe_id}.mp4"
            try:
                ass_path = cap.from_clip(clip, words, cap_dir, settings, safe_id)
                dur = float(clip["end"]) - float(clip["start"])
                if layout == "split_v":
                    args = _split_graph(item["path"], str(gameplay_path), float(clip["start"]), dur, ass_path.name)
                elif layout == "gameplay_bg":
                    args = _gameplay_bg_graph(item["path"], str(gameplay_path), float(clip["start"]), dur, ass_path.name)
                else:
                    args = _crop_graph(item["path"], float(clip["start"]), dur, ass_path.name)

                cmd = [config.ffmpeg_bin(), "-y", *args, *(_base_args()), str(out_path)]
                # ass filter resolves relative to cwd - run ffmpeg inside the caption dir
                _run(cmd, cwd=cap_dir)

                clip["clip_id"] = safe_id
                clip["path"] = str(out_path.resolve())
                clip["duration"] = round(dur, 2)
                clip["layout"] = layout
                clip["stage"] = "rendered"
                rendered += 1
            except Exception as exc:
                clip["stage"] = "failed"
                clip["error"] = str(exc)[:500]
                state.error("render", f"failed {safe_id}", reason=str(exc)[:400])
                failed += 1
            state.save_queue(queue)

        item["stage"] = "rendered" if all(c.get("stage") in {"rendered", "published"} for c in item.get("clips", [])) else item["stage"]
        state.save_queue(queue)

    jobs.finish(job, status="ok" if not failed else "partial", items=rendered, failed=failed)
    return rendered


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description="Render vertical clips").parse_args(argv)
    n = run()
    print(f"rendered {n} clip(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
