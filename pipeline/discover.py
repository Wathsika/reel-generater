"""Stage 1: queue source videos and download ONE of them per run.

Sources come from two places:

  * `sources:` under an enabled campaign in config/campaigns.yaml  (permanent)
  * the URLs you paste into the Run-workflow form                  (one-off)

Pasted URLs are remembered in state/pending.json, so you can paste ten links
at once. Each run takes the first one, prepares it (length -> chat hotspot ->
window) and downloads only that slice. When the rest of the pipeline has
finished with it, the workflow starts another run for the next one. A long
list is therefore worked through in order, one video at a time, and no single
run can grow unbounded or blow the 6-hour limit.

A source that fails twice is dropped (state/sources.json keeps the reason), so
the chain always terminates. Pasting a URL again clears that record: an
explicit request always wins.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys

from . import config, heatmap, jobs, state

DEFAULT_WINDOW = 4500       # 75 min - one transcription budget unit
MAX_WINDOW = 7200           # never transcribe more than 2h in one go
MAX_TRIES = 2               # download attempts before a source is dropped
MAX_PENDING = 50            # guard against a paste of 500 links


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


# --------------------------------------------------------------------------
# The waiting list: pasted URLs (state/pending.json) and processed ones
# (state/sources.json), both committed so they survive between runs.
# --------------------------------------------------------------------------
def _pending_path() -> pathlib.Path:
    return config.STATE / "pending.json"


def _seen_path() -> pathlib.Path:
    return config.STATE / "sources.json"


def _split_urls(raw: str) -> list[str]:
    """Accept URLs separated by commas, spaces, semicolons or newlines."""
    return [c for c in re.split(r"[\s,;]+", raw or "") if c.startswith(("http://", "https://"))]


def _load_pending() -> list[dict]:
    data = state.read_json(_pending_path(), {}) or {}
    return [p for p in data.get("urls", []) if isinstance(p, dict) and p.get("url")]


def _save_pending(items: list[dict]) -> None:
    state.write_json(_pending_path(), {"updated": state.now(), "urls": items})


def _load_seen() -> dict:
    """Entries are keyed by URL (older files may use 'url|window')."""
    raw = state.read_json(_seen_path(), {}) or {}
    seen: dict = {}
    for key, rec in raw.items():
        url = str(key).split("|")[0]
        if isinstance(rec, dict):
            seen[url] = rec
    return seen


def _save_seen(seen: dict) -> None:
    if len(seen) > 500:
        newest = sorted(seen.items(), key=lambda kv: kv[1].get("last_run") or "", reverse=True)
        seen = dict(newest[:500])
    state.write_json(_seen_path(), seen)


def _absorb(seen: dict, pending: list[dict]) -> list[dict]:
    """Turn URLs pasted into the Run-workflow form into pending work."""
    pasted = _split_urls(config.env("EXTRA_SOURCE_URL", "") or "")
    if not pasted:
        return pending
    section = (config.env("EXTRA_SOURCE_SECTION", "") or "").strip() or None
    campaign = (config.env("EXTRA_SOURCE_CAMPAIGN", "") or "").strip() or None
    changed = False
    for url in pasted:
        if any(p["url"] == url for p in pending):
            continue
        if len(pending) >= MAX_PENDING:
            state.warn("discover", f"waiting list is full ({MAX_PENDING}), ignoring extra URLs",
                       url=url[:110])
            break
        pending.append({"url": url, "section": section, "campaign": campaign,
                        "tries": 0, "added": state.now()})
        # pasting a link means "do it again", even if it was processed before
        seen.pop(url, None)
        changed = True
    if changed:
        _save_seen(seen)
    state.info("discover", f"{len(pasted)} pasted link(s), {len(pending)} waiting")
    return pending


def _candidates(campaigns: list[dict], defaults: dict,
                pending: list[dict], seen: dict) -> list[dict]:
    """Everything that still needs work, in the order it should be done.

    Pasted links first (in the order they were given), then the permanent
    `sources:` of enabled campaigns.
    """
    by_id = {str(c.get("id")): c for c in campaigns}
    out: list[dict] = []

    for p in pending:
        if int(p.get("tries") or 0) >= MAX_TRIES:
            continue
        camp = by_id.get(str(p.get("campaign") or ""), campaigns[0] if campaigns else {})
        out.append({
            "url": p["url"],
            "section": p.get("section") or None,
            "campaign": camp.get("id"),
            "settings": {**defaults, **{k: v for k, v in camp.items() if k != "sources"}},
            "from_pending": True,
            "tries": int(p.get("tries") or 0),
        })

    queued = {p["url"] for p in pending}
    for camp in campaigns:
        settings = {**defaults, **{k: v for k, v in camp.items() if k != "sources"}}
        for raw in camp.get("sources") or []:
            url = str(raw).strip()
            if not url or url in queued:
                continue
            rec = seen.get(url) or {}
            if str(rec.get("status") or "done") == "done" and rec:
                continue
            if int(rec.get("tries") or 0) >= MAX_TRIES:
                continue
            out.append({
                "url": url,
                "section": str(settings.get("source_section") or "") or None,
                "campaign": camp.get("id"),
                "settings": settings,
                "from_pending": False,
                "tries": int(rec.get("tries") or 0),
            })
    return out


def pending_count() -> int:
    """How many videos are still waiting (used by the workflow to chain runs)."""
    cfg = config.campaigns()
    campaigns = [c for c in cfg.get("campaigns", []) if c.get("enabled")]
    return len(_candidates(campaigns, cfg.get("defaults", {}),
                           _load_pending(), _load_seen()))


def run(limit: int | None = 1) -> int:
    job = jobs.start("discover")
    cfg = config.campaigns()
    defaults = cfg.get("defaults", {})
    campaigns = [c for c in cfg.get("campaigns", []) if c.get("enabled")]

    seen = _load_seen()
    pending = _absorb(seen, _load_pending())

    todo = _candidates(campaigns, defaults, pending, seen)
    if not todo:
        _save_pending(pending)
        jobs.finish(job, status="ok", items=0, note="waiting list empty")
        state.info("discover", "nothing waiting - paste a link or add one to campaigns.yaml")
        return 0

    queue = state.load_queue()
    existing = set()
    for c in queue.get("clips", []):
        existing.add(c.get("source_key") or c.get("source_url"))
        if c.get("source_url"):
            existing.add(c.get("source_url"))

    added = 0
    for cand in todo:
        if limit is not None and added >= limit:
            break
        url = cand["url"]
        settings = dict(cand["settings"])
        section = cand["section"]
        auto = section is None
        window = int(settings.get("source_window") or DEFAULT_WINDOW)

        peaks: list[int] = []
        meta: dict = {}
        if auto:
            # Only a URL was supplied: fetch the length, then let the audience
            # heatmap choose the slice worth transcribing.
            meta = _probe(url)
            section, peaks = _auto_section(url, meta.get("duration", 0), window)
            if section:
                state.info("discover", f"auto window {section} for {url[:90]}",
                           source=meta.get("uploader") or "")

        key = f"{url}|{section}" if section else url
        if key in existing or url in existing:
            pending = [p for p in pending if p["url"] != url]
            # already handled today: record it again so a pasted duplicate does
            # not silently redo the same video tomorrow
            seen.setdefault(url, {"status": "done", "section": section,
                                  "last_run": state.now()})
            state.warn("discover", "already handled today - paste it again tomorrow to redo",
                       url=url[:110])
            continue

        safe = "".join(ch if ch.isalnum() else "_" for ch in key)[-60:]
        dest = config.WORK / "sources" / f"{safe}.mp4"
        got = _download(url, dest, section=section)

        if got is None:
            # Failure: try once more on a later run, then drop it so the queue
            # keeps moving and the chain of runs eventually stops.
            tries = cand["tries"] + 1
            pending = [p for p in pending if p["url"] != url]
            if cand["from_pending"] and tries < MAX_TRIES:
                pending.append({"url": url, "section": cand["section"],
                                "campaign": cand["campaign"], "tries": tries,
                                "added": state.now()})
            else:
                seen[url] = {"status": "failed", "tries": tries,
                             "section": section, "last_run": state.now()}
                state.warn("discover", "source dropped after repeated failures",
                           url=url[:110], tries=tries)
            continue

        item_settings = dict(settings)
        if peaks:
            item_settings["focus_seconds"] = peaks
        queue.setdefault("clips", []).append(
            {
                "source_key": key,
                "source_url": url,
                "source_section": section,
                "source_title": meta.get("title") or "",
                "source_uploader": meta.get("uploader") or "",
                "auto_section": auto,
                "path": str(got),
                "campaign": cand["campaign"],
                "settings": item_settings,
                "stage": "discovered",
            }
        )
        existing.update({key, url})
        pending = [p for p in pending if p["url"] != url]
        seen[url] = {
            "status": "done",
            "section": section,
            "title": meta.get("title") or "",
            "uploader": meta.get("uploader") or "",
            "first_seen": (seen.get(url) or {}).get("first_seen") or state.now(),
            "last_run": state.now(),
        }
        added += 1
        state.save_queue(queue)

    _save_pending(pending)
    _save_seen(seen)
    queue["campaigns"] = [c.get("id") for c in campaigns]
    state.save_queue(queue)

    remaining = len(_candidates(campaigns, defaults, pending, seen))
    jobs.finish(job, status="ok", items=added,
                note=f"{remaining} still waiting" if remaining else "queue empty")
    if remaining:
        state.info("discover", f"{remaining} more video(s) waiting", waiting=remaining)
    return added


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Queue and download source videos")
    parser.add_argument("--limit", type=int, default=1,
                        help="videos to download this run (default 1)")
    parser.add_argument("--pending", action="store_true",
                        help="print how many videos are still waiting, then exit")
    args = parser.parse_args(argv)
    if args.pending:
        print(pending_count())
        return 0
    n = run(limit=args.limit)
    print(f"discovered {n} source(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
