"""Stage 1: discover campaign sources and build the daily queue.

Campaign-based clipping: the rights holder supplies the source videos. We
only ingest sources listed under enabled campaigns in config/campaigns.yaml.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

from . import config, heatmap, jobs, state

DEFAULT_WINDOW = 4500       # 75 min - one transcription budget unit
MAX_WINDOW = 7200           # never transcribe more than 2h in one go


def _download(url: str, dest: pathlib.Path, section: str | None = None) -> pathlib.Path | None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    if url.startswith("local:"):
        src = pathlib.Path(url[6:])
        if src.is_file():
            return src
        state.error("discover", f"local source missing: {src}")
        return None
    if url.startswith("http://") or url.startswith("https://"):
        cmd = [
            *config.ytdlp_cmd(),
            "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
            "--merge-output-format", "mp4",
            "-o", str(dest),
            "--no-playlist",
        ]
        if section:
            # stream VODs are hours long: pull only the interesting window
            cmd += ["--download-sections", f"*{section}"]
            ffmpeg = config.ffmpeg_bin()
            ffdir = pathlib.Path(ffmpeg).parent
            if str(ffdir) not in {".", ""}:
                cmd += ["--ffmpeg-location", str(ffdir)]
        cmd.append(url)
        try:
            subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                timeout=1800,
            )
            return dest if dest.exists() else None
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
            stderr = getattr(exc, "stderr", b"") or b""
            reason = stderr.decode("utf-8", "replace")[-300:] if stderr else str(exc)[:300]
            state.error("discover", f"download failed: {url}", reason=reason)
            return None
    return None


def _probe(url: str) -> dict:
    """VOD metadata without downloading: duration, title, uploader."""
    try:
        proc = subprocess.run(
            [*config.ytdlp_cmd(), "-J", "--no-warnings", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
        )
        if proc.returncode != 0:
            return {}
        data = json.loads(proc.stdout)
        return {
            "title": data.get("title") or "",
            "uploader": data.get("uploader") or "",
            "duration": float(data.get("duration") or 0),
        }
    except (subprocess.SubprocessError, ValueError, OSError):
        return {}


def _hms(seconds: int) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _sample_step(duration: float) -> int:
    """Coarse grid, but never more than ~60 chat samples per VOD."""
    return max(120, int(duration // 60) // 30 * 30)


def _auto_section(url: str, duration: float, window: int) -> tuple[str | None, list[int]]:
    """Pick the busiest `window`-second slice of a VOD, using chat replay.

    Only the URL is needed for this: duration comes from yt-dlp metadata and
    the hotspot from Twitch chat density. Returns (section, peak_seconds).
    Falls back to the opening `window` when chat is unavailable, so YouTube,
    Drive or chat-less sources still work.
    """
    if duration <= 0:
        return None, []

    window = max(600, min(int(window), MAX_WINDOW))
    if duration <= window + 60:
        return None, []           # short source: take all of it

    info = heatmap.analyse(url, 0, int(duration), step=_sample_step(duration))
    if info and info.get("samples"):
        # summarise so the queue (and repo) stays small
        best = heatmap.best_window(info["samples"], window)
        if best:
            start = min(int(best[0]), max(0, int(duration) - window))
            return f"{_hms(start)}-{_hms(start + window)}", info.get("peaks") or []
        return f"{_hms(0)}-{_hms(window)}", info.get("peaks") or []

    if heatmap.twitch_vod_id(url):
        state.warn("discover", "chat heatmap unavailable, using opening window",
                   url=url[:120])
    else:
        state.info("discover", "no chat replay for this source, using opening window",
                   url=url[:120])
    return f"{_hms(0)}-{_hms(window)}", []


def _one_off(
    campaigns: list[dict], url: str, section: str = "", campaign_id: str = "",
) -> list[dict]:
    """Replace the campaign list with a single ad-hoc run for `url`.

    Used by the workflow's "source_url" input so a new VOD can be processed
    without editing or committing a file. Settings (rules, keywords, layout)
    are inherited from `campaign_id`, or the first enabled campaign, so the
    campaign brief still steers the moment picker.
    """
    url = (url or "").strip()
    if not url:
        return campaigns

    target: dict = {}
    if campaign_id:
        target = next((c for c in campaigns if str(c.get("id")) == campaign_id), {})
    elif campaigns:
        target = campaigns[0]

    camp = {k: v for k, v in target.items() if k != "sources"}
    camp.setdefault("id", "adhoc")
    camp["sources"] = [url]
    if section:
        camp["source_section"] = section
    else:
        # a different VOD needs its own window: let discover pick one from the
        # chat heatmap instead of inheriting the configured campaign's window
        camp.pop("source_section", None)
        camp["auto_section"] = True
    return [camp]


def run(limit: int | None = None) -> int:
    job = jobs.start("discover")
    cfg = config.campaigns()
    defaults = cfg.get("defaults", {})
    campaigns = [c for c in cfg.get("campaigns", []) if c.get("enabled")]
    campaigns = _one_off(
        campaigns,
        config.env("EXTRA_SOURCE_URL", "") or "",
        config.env("EXTRA_SOURCE_SECTION", "") or "",
        config.env("EXTRA_SOURCE_CAMPAIGN", "") or "",
    )
    if not campaigns:
        jobs.finish(job, status="ok", items=0, note="no enabled campaigns")
        state.warn("discover", "no enabled campaigns in config/campaigns.yaml")
        return 0

    queue = state.load_queue()
    existing_ids = {
        c.get("source_key") or c.get("source_url") for c in queue.get("clips", [])
    }
    # Yesterday's queue is gone tomorrow, so remember processed sources for good.
    # Without this, the daily schedule would re-clip the same VOD every day and
    # republish the same moments. An explicit source_url always wins, so you can
    # deliberately re-process a video.
    seen_path = config.STATE / "sources.json"
    seen: dict = state.read_json(seen_path, {}) or {}
    one_off = bool((config.env("EXTRA_SOURCE_URL", "") or "").strip())
    added = 0
    sources: list[dict] = []

    for camp in campaigns:
        camp_defaults = {**defaults, **{k: v for k, v in camp.items() if k != "sources"}}
        section = str(camp_defaults.get("source_section") or "") or None
        window = int(camp_defaults.get("source_window") or DEFAULT_WINDOW)
        auto = bool(camp_defaults.get("auto_section"))
        for url in camp.get("sources", []):
            key = f"{url}|{section}" if section else url
            if key in existing_ids:
                continue
            if limit is not None and added >= limit:
                break

            peaks: list[int] = []
            meta: dict = {}
            if section is None:
                # Only a URL was supplied: fetch the length, then let the
                # audience heatmap choose the slice worth transcribing.
                meta = _probe(url)
                section, peaks = _auto_section(url, meta.get("duration", 0), window)
                if section:
                    state.info("discover", f"auto window {section} for {url[:90]}",
                               source=meta.get("uploader") or "")
                key = f"{url}|{section}" if section else key
                if key in existing_ids:
                    continue

            if not one_off and key in seen:
                state.info(
                    "discover", "source already processed, skipping",
                    url=url[:110],
                    hint="paste it into source_url to force a re-run",
                )
                continue

            safe = "".join(ch if ch.isalnum() else "_" for ch in key)[-60:]
            dest = config.WORK / "sources" / f"{safe}.mp4"
            got = _download(url, dest, section=section)
            if got is None:
                continue
            item_settings = dict(camp_defaults)
            if peaks:
                item_settings["focus_seconds"] = peaks
            sources.append(
                {
                    "source_key": key,
                    "source_url": url,
                    "source_section": section,
                    "source_title": meta.get("title") or "",
                    "source_uploader": meta.get("uploader") or "",
                    "auto_section": auto,
                    "path": str(got),
                    "campaign": camp.get("id"),
                    "settings": item_settings,
                    "stage": "discovered",
                }
            )
            added += 1
            seen[key] = {
                "url": url,
                "section": section,
                "title": meta.get("title") or "",
                "first_seen": (seen.get(key) or {}).get("first_seen") or state.now(),
                "last_run": state.now(),
            }

    if len(seen) > 500:
        newest = sorted(seen.items(), key=lambda kv: kv[1].get("last_run") or "", reverse=True)
        seen = dict(newest[:500])
    state.write_json(seen_path, seen)

    queue.setdefault("clips", []).extend(sources)
    queue["campaigns"] = [c.get("id") for c in campaigns]
    state.save_queue(queue)
    jobs.finish(job, status="ok", items=added)
    return added


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Discover campaign sources")
    parser.add_argument("--limit", type=int, default=None, help="max new sources this run")
    args = parser.parse_args(argv)
    n = run(limit=args.limit)
    print(f"discovered {n} source(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
