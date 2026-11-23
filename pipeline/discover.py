"""Stage 1: discover campaign sources and build the daily queue.

Campaign-based clipping: the rights holder supplies the source videos. We
only ingest sources listed under enabled campaigns in config/campaigns.yaml.
"""
from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

from . import config, jobs, state


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


def run(limit: int | None = None) -> int:
    job = jobs.start("discover")
    cfg = config.campaigns()
    defaults = cfg.get("defaults", {})
    campaigns = [c for c in cfg.get("campaigns", []) if c.get("enabled")]
    if not campaigns:
        jobs.finish(job, status="ok", items=0, note="no enabled campaigns")
        state.warn("discover", "no enabled campaigns in config/campaigns.yaml")
        return 0

    queue = state.load_queue()
    existing_ids = {
        c.get("source_key") or c.get("source_url") for c in queue.get("clips", [])
    }
    added = 0
    sources: list[dict] = []

    for camp in campaigns:
        camp_defaults = {**defaults, **{k: v for k, v in camp.items() if k != "sources"}}
        section = str(camp_defaults.get("source_section") or "") or None
        for url in camp.get("sources", []):
            key = f"{url}|{section}" if section else url
            if key in existing_ids:
                continue
            if limit is not None and added >= limit:
                break
            safe = "".join(ch if ch.isalnum() else "_" for ch in key)[-60:]
            dest = config.WORK / "sources" / f"{safe}.mp4"
            got = _download(url, dest, section=section)
            if got is None:
                continue
            sources.append(
                {
                    "source_key": key,
                    "source_url": url,
                    "source_section": section,
                    "path": str(got),
                    "campaign": camp.get("id"),
                    "settings": camp_defaults,
                    "stage": "discovered",
                }
            )
            added += 1

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
