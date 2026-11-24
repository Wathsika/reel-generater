"""Download a source video with yt-dlp, and notice when it went wrong.

yt-dlp does the hard part (Twitch VODs, YouTube, HLS). The part it cannot know
is what *this* pipeline needs, and a bare `yt-dlp -o out.mp4` call gets three
things wrong often enough to matter:

  * **A half-finished download is reused forever.** `discover` skips a source
    when the file exists, so a truncated file (runner killed, network drop) is
    kept as if it were complete. Here every file is probed: a video stream must
    exist, and its length must match the requested window.
  * **No retries.** A single 403 or timeout used to drop the video for good.
  * **No codec preference.** MP4/H.264 + AAC is what Instagram, TikTok and
    Facebook accept without a re-encode; VP9/Opus in an MP4 makes Meta refuse
    the file.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess

from . import config, state

MIN_BYTES = 64 * 1024

# Prefer H.264 + AAC so the clip is remuxed, not re-encoded, by every platform.
# The plain `bv*+ba` steps keep working on sources that only serve VP9/AV1.
def format_selector(height: int = 1080) -> str:
    return (
        f"bv*[height<={height}][vcodec^=avc1]+ba[acodec^=mp4a]/"
        f"bv*[height<={height}]+ba/"
        f"b[height<={height}]/b"
    )


def _cookies() -> list[str]:
    """Optional cookie file - YouTube asks for one on age/bot checks."""
    raw = (config.env("YTDLP_COOKIES") or "").strip()
    if raw:
        path = config.WORK / "cookies.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file() or path.read_text(encoding="utf-8", errors="ignore") != raw:
            path.write_text(raw + "\n", encoding="utf-8")
        return ["--cookies", str(path)]
    given = (config.env("YTDLP_COOKIES_FILE") or "").strip()
    if given and pathlib.Path(given).is_file():
        return ["--cookies", given]
    return []


def _section_seconds(section: str | None) -> float:
    """"00:10:00-00:20:00" -> 600 seconds (0 when unknown)."""
    if not section:
        return 0.0
    parts = str(section).replace("*", "").split("-")
    if len(parts) != 2:
        return 0.0

    def secs(text: str) -> float:
        bits = [b for b in str(text).strip().split(":") if b != ""]
        try:
            nums = [float(b) for b in bits]
        except ValueError:
            return 0.0
        total = 0.0
        for n in nums:
            total = total * 60 + n
        return total

    start, end = secs(parts[0]), secs(parts[1])
    return max(0.0, end - start) if end > start else 0.0


def probe(path: str | pathlib.Path) -> dict:
    """{'duration': seconds, 'width': px, 'height': px} or {} if unreadable."""
    try:
        proc = subprocess.run(
            [config.ffprobe_bin(), "-v", "error",
             "-select_streams", "v:0",
             "-show_entries", "stream=width,height:format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=180,
        )
        if proc.returncode != 0:
            return {}
        data = json.loads(proc.stdout or "{}")
        streams = data.get("streams") or []
        if not streams:
            return {}
        fmt = data.get("format") or {}
        return {
            "duration": float(fmt.get("duration") or 0),
            "width": int(streams[0].get("width") or 0),
            "height": int(streams[0].get("height") or 0),
        }
    except (subprocess.SubprocessError, ValueError, OSError, IndexError):
        return {}


def looks_complete(path: pathlib.Path, expect: float = 0.0) -> bool:
    """Is this a whole video, or the remains of an interrupted download?"""
    if not path.is_file() or path.stat().st_size < MIN_BYTES:
        return False
    info = probe(path)
    if not info.get("width") or not info.get("height"):
        return False
    if expect >= 60 and info.get("duration", 0) < expect * 0.8:
        return False
    return True


def _cmd(url: str, dest: pathlib.Path, section: str | None, height: int,
         force: bool) -> list[str]:
    cmd = [
        *config.ytdlp_cmd(),
        "-f", format_selector(height),
        "--merge-output-format", "mp4",
        "-o", str(dest),
        "--no-playlist",
        "--no-progress",
        "--newline",
        "--retries", "10",
        "--fragment-retries", "10",
        "--socket-timeout", "30",
        "--concurrent-fragments", "4",
        "--no-warnings",
    ]
    cmd += _cookies()
    if force:
        # A corrupt leftover is the usual reason a retry fails identically.
        cmd += ["--force-overwrites"]
    if section:
        # Stream VODs are hours long: pull only the interesting window.
        cmd += ["--download-sections", f"*{section}"]
        exe = pathlib.Path(config.ffmpeg_bin())
        if exe.parent and str(exe.parent) not in {".", ""}:
            cmd += ["--ffmpeg-location", str(exe.parent)]
    cmd.append(url)
    return cmd


def download(
    url: str,
    dest: pathlib.Path,
    section: str | None = None,
    height: int = 1080,
    tries: int = 2,
) -> pathlib.Path | None:
    """Fetch `url` (or one window of it) to `dest`. None means it failed."""
    dest = pathlib.Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    expect = _section_seconds(section)

    if dest.is_file():
        if looks_complete(dest, expect):
            return dest
        # Never reuse a broken file: that is how a truncated VOD becomes a
        # source with no audio for the rest of its life.
        state.warn("discover", "discarding an incomplete download",
                   file=dest.name, size=dest.stat().st_size)
        dest.unlink(missing_ok=True)
        for leftover in dest.parent.glob(dest.name + ".*"):
            if leftover.name.endswith((".part", ".ytdl")):
                leftover.unlink(missing_ok=True)

    last = ""
    for attempt in range(1, max(1, tries) + 1):
        cmd = _cmd(url, dest, section, height, force=attempt > 1)
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=5400)
        except FileNotFoundError:
            state.error("discover", "yt-dlp is not installed",
                        reason="pip install -r requirements.txt")
            return None
        except subprocess.TimeoutExpired:
            last = "the download took longer than 90 minutes"
            state.warn("discover", f"download timed out (attempt {attempt}/{tries})",
                       url=url[:110])
            continue

        if proc.returncode == 0 and looks_complete(dest, expect):
            info = probe(dest)
            state.info("discover", f"downloaded {dest.name}",
                       size_mb=round(dest.stat().st_size / 1048576, 1),
                       duration_s=int(info.get("duration", 0)),
                       size=f"{info.get('width')}x{info.get('height')}")
            return dest

        err = (proc.stderr or b"").decode("utf-8", "replace")
        lines = [l for l in err.splitlines()
                 if l.strip() and not l.startswith(("WARNING", "[download]"))]
        last = (lines[-1] if lines else f"exit code {proc.returncode}")
        if proc.returncode == 0:
            last = "the file finished but is not a usable video"
        if "not a bot" in err.lower() or "sign in to confirm" in err.lower():
            # Datacenter IPs are asked to prove they are human. Nothing in the
            # pipeline can answer that: it needs a signed-in session.
            last += (" | YouTube wants a signed-in session from this runner: add a "
                     "YTDLP_COOKIES secret (a cookies.txt exported from a logged-in "
                     "browser) or paste the original Twitch URL instead.")
        last = last[:420]
        state.warn("discover", f"download failed (attempt {attempt}/{tries})",
                   reason=last, url=url[:110])
        if dest.is_file() and not looks_complete(dest, expect):
            dest.unlink(missing_ok=True)

    state.error("discover", f"download failed: {url}", reason=last or "unknown")
    # Also surface it on the run page. A red job whose only line is
    # "Process completed with exit code 1" tells nobody what to fix.
    print(f"::error title=Download refused::{last or 'yt-dlp could not fetch this URL'}")
    return None


def video_id(url: str) -> str:
    """A short stable name for a URL (used for file names)."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", str(url)).strip("_")
    return cleaned[-64:] if len(cleaned) > 64 else cleaned
