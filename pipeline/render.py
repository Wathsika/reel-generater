"""Stage 4: render vertical clips with ffmpeg.

Layouts:
  crop        - 9:16 centre crop of the single source (default)
  split_v     - speaker top / gameplay bottom (needs campaign gameplay_source)
  gameplay_bg - gameplay full-screen with the speaker in a top-left box
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import subprocess
import sys

from . import captions as cap
from . import config, jobs, state

W, H = 1080, 1920

# Where the logo sits by default: bottom-right, clear of the caption block
# (captions live around y=1200-1360) and clear of the hook (top centre).
LOGO_POSITIONS = {
    "tl": "{m}:{m}",
    "tr": "W-w-{m}:{m}",
    "bl": "{m}:H-h-{m}",
    "br": "W-w-{m}:H-h-{m}",
    "tc": "(W-w)/2:{m}",
    "bc": "(W-w)/2:H-h-{m}",
    "center": "(W-w)/2:(H-h)/2",
}


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


def _logo_filter(idx: int, settings: dict) -> tuple[str, str]:
    """Return (pre-chain, overlay-filter) placing input `idx` as the logo."""
    width = int(settings.get("logo_width") or 260)
    margin = int(settings.get("logo_margin") or 40)
    opacity = float(settings.get("logo_opacity") or 1.0)
    pos = str(settings.get("logo_position") or "br").lower()

    chain = f"[{idx}:v]scale={width}:-2,format=rgba"
    if 0.0 < opacity < 1.0:
        chain += f",colorchannelmixer=aa={opacity}"

    xy = LOGO_POSITIONS.get(pos, LOGO_POSITIONS["br"]).format(m=margin)
    return chain, f"overlay={xy}:format=auto"


def _logo_input(logo: pathlib.Path, dur: float) -> list[str]:
    return ["-loop", "1", "-t", str(dur), "-i", str(logo)]


def _crop_graph(
    src: str, start: float, dur: float, ass_name: str,
    logo: pathlib.Path | None = None, settings: dict | None = None,
) -> list[str]:
    base = (
        f"crop='min(iw,ih*9/16)':'min(ih,iw*16/9)',"
        f"scale={W}:{H}:flags=lanczos,setsar=1,ass={ass_name}"
    )
    head = ["-ss", str(start), "-t", str(dur), "-i", src]
    if logo is None:
        return [*head, "-vf", base, "-map", "0:v", "-map", "0:a"]
    pre, ov = _logo_filter(1, settings or {})
    fc = f"[0:v]{base}[v0];{pre}[lg];[v0][lg]{ov}[v]"
    return [*head, *_logo_input(logo, dur), "-filter_complex", fc, "-map", "[v]", "-map", "0:a"]


def _split_graph(
    src: str, gameplay: str, start: float, dur: float, ass_name: str,
    logo: pathlib.Path | None = None, settings: dict | None = None,
) -> list[str]:
    fc = (
        f"[1:v]scale={W}:960:force_original_aspect_ratio=increase,"
        f"crop={W}:960,setsar=1[top];"
        f"[0:v]scale={W}:960:force_original_aspect_ratio=increase,"
        f"crop={W}:960,setsar=1,setsar=1[bot];"
        f"[top][bot]vstack=inputs=2,ass={ass_name}"
    )
    head = [
        "-stream_loop", "-1", "-t", str(dur), "-i", gameplay,
        "-ss", str(start), "-t", str(dur), "-i", src,
    ]
    if logo is None:
        return [*head, "-filter_complex", fc + "[v]", "-map", "[v]", "-map", "1:a"]
    pre, ov = _logo_filter(2, settings or {})
    fc = f"{fc}[v0];{pre}[lg];[v0][lg]{ov}[v]"
    return [*head, *_logo_input(logo, dur), "-filter_complex", fc, "-map", "[v]", "-map", "1:a"]


def _gameplay_bg_graph(
    src: str, gameplay: str, start: float, dur: float, ass_name: str,
    logo: pathlib.Path | None = None, settings: dict | None = None,
) -> list[str]:
    fc = (
        f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
        f"crop={W}:{H},setsar=1[bg];"
        f"[1:v]scale=520:-2[pc];"
        f"[bg][pc]overlay=24:24,ass={ass_name}"
    )
    head = [
        "-stream_loop", "-1", "-t", str(dur), "-i", gameplay,
        "-ss", str(start), "-t", str(dur), "-i", src,
    ]
    if logo is None:
        return [*head, "-filter_complex", fc + "[v]", "-map", "[v]", "-map", "1:a"]
    pre, ov = _logo_filter(2, settings or {})
    fc = f"{fc}[v0];{pre}[lg];[v0][lg]{ov}[v]"
    return [*head, *_logo_input(logo, dur), "-filter_complex", fc, "-map", "[v]", "-map", "1:a"]


def _resolve_local(ref: str) -> pathlib.Path | None:
    path = pathlib.Path(ref)
    if not path.is_absolute():
        path = config.ROOT / path
    return path if path.is_file() else None


def _fetch_logo(ref: str) -> pathlib.Path | None:
    """Fetch the campaign's required logo image.

    Accepts `local:<path>`, a plain repo-relative path (e.g.
    `assets/clipup.png`), or an https URL. A repo-relative file is best for CI
    because it needs no network access at render time.
    """
    ref = (ref or "").strip()
    if not ref:
        return None
    if ref.startswith("local:"):
        found = _resolve_local(ref[6:])
    elif ref.startswith("http://") or ref.startswith("https://"):
        ext = pathlib.Path(ref.split("?")[0]).suffix.lower()
        if ext not in {".png", ".jpg", ".jpeg", ".webp"}:
            ext = ".png"
        dest = config.WORK / "logos" / (hashlib.sha1(ref.encode()).hexdigest()[:12] + ext)
        if dest.is_file() and dest.stat().st_size > 0:
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            import requests

            resp = requests.get(ref, timeout=60)
            resp.raise_for_status()
            dest.write_bytes(resp.content)
            return dest
        except Exception as exc:  # pragma: no cover - network
            state.warn("render", "logo download failed", reason=str(exc)[:200])
            return None
    else:
        found = _resolve_local(ref)
    if found is None:
        state.warn("render", "logo file not found", path=ref)
    return found


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
            [*config.ytdlp_cmd(), "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
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
    waiting = 0

    for item in queue.get("clips", []):
        # Gate on the clips themselves, not the item stage: an item stays
        # "rendered" forever after its first run, which would otherwise make
        # every re-run silently skip it. A missing stage also counts: queue
        # files written before the stage was recorded must still render.
        pending = [c for c in item.get("clips", []) if c.get("stage") in (None, "selected")]
        if not pending:
            continue
        waiting += len(pending)
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

        # Campaign logos are mandatory for some campaigns (e.g. ClipUp). The
        # text watermark above is separate and can coexist with it.
        logo_path = _fetch_logo(str(settings.get("logo") or settings.get("watermark_image") or ""))
        if logo_path is not None:
            logo_path = logo_path.resolve()

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
                    args = _split_graph(item["path"], str(gameplay_path), float(clip["start"]), dur, ass_path.name, logo_path, settings)
                elif layout == "gameplay_bg":
                    args = _gameplay_bg_graph(item["path"], str(gameplay_path), float(clip["start"]), dur, ass_path.name, logo_path, settings)
                else:
                    args = _crop_graph(item["path"], float(clip["start"]), dur, ass_path.name, logo_path, settings)

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

    # The 2026-11-23 run picked a clip, rendered nothing, and still reported
    # success because the stage filter never matched. Never let that happen
    # quietly again: if work was waiting and not one clip came out, fail.
    if waiting and not rendered and not failed:
        state.error("render", "clips were selected but none was rendered - this is a pipeline bug")
        jobs.finish(job, status="failed", items=0)
        return -1

    jobs.finish(job, status="ok" if not failed else "partial", items=rendered, failed=failed)
    return rendered


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description="Render vertical clips").parse_args(argv)
    n = run()
    if n < 0:
        print("render: NOTHING WAS PRODUCED - see the error log")
        return 1
    print(f"rendered {n} clip(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
