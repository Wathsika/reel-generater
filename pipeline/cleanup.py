"""Stage 10: cleanup + retention.

Frees disk during a run and prunes old state so the repo stays small.

Rules:
  * A source video is deleted once every clip cut from it has finished
    rendering/QA/upload/publish. If any clip failed, the source is kept so a
    re-run can retry it (use --force to delete anyway).
  * A clip's local .mp4 is deleted once it has been uploaded to storage.
    Publishing uses the public URL, so the local file is no longer needed.
  * Per-day state files (queue/rendered/logs) older than --prune-days are
    deleted. Cumulative files (posted/performance/jobs) are never pruned.

Note: on GitHub-hosted runners the whole VM is destroyed after the job, so
this is mainly about staying under the 14 GB disk cap mid-run and about
keeping the committed state directory tidy over months.
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sys

from . import config, jobs, state

# A clip in any of these states has been dealt with; it no longer blocks
# deleting its source. "failed" counts: the attempt is over, and on an
# ephemeral runner the local file is discarded at job end regardless.
SOURCE_DONE = {"rendered", "qa_passed", "uploaded", "published", "failed"}
# Only genuine pending work should keep a source alive.
SOURCE_BLOCKING = {"selected", "discovered", "transcribing", "transcribed"}


def _mb(n: int) -> float:
    return round(n / (1024 * 1024), 2)


def _dir_mb(path: pathlib.Path) -> float:
    if not path.exists():
        return 0.0
    return _mb(sum(f.stat().st_size for f in path.rglob("*") if f.is_file()))


def _unlink(path: pathlib.Path, seen: set[str], stats: dict, dry: bool) -> bool:
    key = str(path.resolve())
    if key in seen:
        return False
    if not path.is_file():
        return False
    seen.add(key)
    size = path.stat().st_size
    if not dry:
        try:
            path.unlink()
        except OSError as exc:
            state.warn("cleanup", f"could not delete {path.name}", reason=str(exc)[:200])
            return False
    stats["bytes"] += size
    stats["files"] += 1
    return True


def cleanup_clips(stats: dict, seen: set[str], dry: bool) -> None:
    """Delete local clip files once they're safely on R2."""
    queue = state.load_queue()
    for item in queue.get("clips", []):
        for clip in item.get("clips", []):
            if not clip.get("r2_url"):
                continue
            if clip.get("stage") not in {"uploaded", "published", "failed"}:
                continue
            path = clip.get("path")
            if path and _unlink(pathlib.Path(path), seen, stats, dry):
                clip["local_deleted"] = True
    state.save_queue(queue)

    rendered = state.load_rendered()
    for clip in rendered.get("clips", []):
        if not clip.get("r2_url"):
            continue
        path = clip.get("path")
        if path and _unlink(pathlib.Path(path), seen, stats, dry):
            clip["local_deleted"] = True
    state.save_rendered(rendered)


def cleanup_sources(stats: dict, seen: set[str], dry: bool, force: bool) -> None:
    """Delete long-form sources whose clips are all finished."""
    queue = state.load_queue()
    for item in queue.get("clips", []):
        if item.get("source_deleted"):
            continue
        clips = item.get("clips") or []
        if not clips:
            continue
        blocking = [c for c in clips if c.get("stage") in SOURCE_BLOCKING]
        if blocking and not force:
            continue
        base = pathlib.Path(item["path"])
        # source media + the audio/transcript sidecars transcribe creates
        for candidate in (base, base.with_suffix(".audio.mp3"), base.with_suffix(".words.json")):
            _unlink(candidate, seen, stats, dry)
        item["source_deleted"] = True
        stats["sources"] += 1
    state.save_queue(queue)


def cleanup_captions(stats: dict, seen: set[str], dry: bool) -> None:
    """Delete ASS caption files whose clip file is gone."""
    cap_dir = config.WORK / "captions"
    if not cap_dir.is_dir():
        return
    # A caption is stale if its sibling mp4 no longer exists in out/.
    live = {p.stem for p in (config.OUT).rglob("*.mp4")}
    for ass in cap_dir.glob("*.ass"):
        if ass.stem not in live:
            _unlink(ass, seen, stats, dry)


def prune_state(days: int, stats: dict, seen: set[str], dry: bool) -> None:
    if days < 1:
        return
    cutoff = dt.date.today() - dt.timedelta(days=days)
    for sub in ("queue", "rendered", "logs"):
        for f in (config.STATE / sub).glob("*.json"):
            try:
                file_date = dt.date.fromisoformat(f.stem)
            except ValueError:
                continue
            if file_date < cutoff and _unlink(f, seen, stats, dry):
                stats["state_files"] += 1


def cleanup_remote(remote_days: int, stats: dict, dry: bool) -> None:
    """Delete clip objects from storage older than N days.

    Platforms download the file when publishing, so after a few days the
    remote copy is dead weight - important on a self-hosted VPS where disk is
    finite. Never touches anything younger than the cutoff, so a retry can
    still publish it.
    """
    if remote_days < 1:
        return
    from . import storage

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=remote_days)
    try:
        objects = storage.list_objects()
    except Exception as exc:
        state.warn("cleanup", "could not list remote storage", reason=str(exc)[:250])
        return

    for key, modified in objects:
        if not str(key).lower().endswith(".mp4"):
            continue
        if modified is None or modified >= cutoff:
            continue
        if dry:
            stats["remote"] += 1
            continue
        try:
            storage.delete(str(key))
            stats["remote"] += 1
        except Exception as exc:
            state.warn("cleanup", f"could not delete {key}", reason=str(exc)[:250])


def run(prune_days: int = 30, remote_days: int = 7, force: bool = False,
        dry: bool = False) -> dict:
    job = jobs.start("cleanup")
    stats = {"bytes": 0, "files": 0, "sources": 0, "state_files": 0, "remote": 0}
    seen: set[str] = set()

    cleanup_clips(stats, seen, dry)
    cleanup_sources(stats, seen, dry, force)
    cleanup_captions(stats, seen, dry)
    prune_state(prune_days, stats, seen, dry)
    cleanup_remote(remote_days, stats, dry)

    # Drop now-empty directories so listings stay clean.
    if not dry:
        for sub in ("sources", "gameplay", "captions"):
            d = config.WORK / sub
            if d.is_dir() and not any(d.iterdir()):
                try:
                    d.rmdir()
                except OSError:
                    pass

    summary = {
        "freed_mb": _mb(stats["bytes"]),
        "files_deleted": stats["files"],
        "sources_deleted": stats["sources"],
        "state_files_deleted": stats["state_files"],
        "remote_deleted": stats["remote"],
        "remote_days": remote_days,
        "work_mb": _dir_mb(config.WORK),
        "out_mb": _dir_mb(config.OUT),
        "state_mb": _dir_mb(config.STATE),
        "dry_run": dry,
    }
    state.write_json(config.STATE / "storage.json", {"updated": state.now(), **summary})
    jobs.finish(job, status="ok", items=stats["files"])
    state.info(
        "cleanup",
        f"freed {summary['freed_mb']} MB across {stats['files']} file(s)",
        sources=stats["sources"],
        state_files=stats["state_files"],
        remote=stats["remote"],
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Delete sources/clips and prune old state")
    parser.add_argument("--prune-days", type=int, default=30, help="delete per-day state older than N days")
    parser.add_argument("--remote-days", type=int, default=7, help="delete clips from remote storage older than N days (0 = never)")
    parser.add_argument("--force", action="store_true", help="delete sources even if some clips failed")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    s = run(prune_days=args.prune_days, remote_days=args.remote_days,
            force=args.force, dry=args.dry_run)
    print(
        f"cleanup: freed {s['freed_mb']} MB, {s['sources_deleted']} source(s), "
        f"{s['state_files_deleted']} old state file(s), "
        f"{s['remote_deleted']} remote clip(s)"
        + (" (dry run)" if s["dry_run"] else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
