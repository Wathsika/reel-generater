"""Job tracker.

Records every pipeline run into state/jobs.json so the dashboard can show
what is running right now, what finished, what failed, and how long it took.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from . import state

JOBS_PATH = state.config.STATE / "jobs.json"

# Stages in pipeline order - drives the dashboard's flow diagram.
STAGES = [
    ("discover", "Find sources"),
    ("transcribe", "Transcribe"),
    ("select", "Pick moments"),
    ("render", "Render clips"),
    ("qa", "Quality check"),
    ("upload", "Upload to R2"),
    ("publish", "Publish"),
    ("analytics", "Pull stats"),
    ("cleanup", "Cleanup"),
]


def _load() -> dict:
    data = state.read_json(JOBS_PATH, {"runs": []})
    data.setdefault("runs", [])
    return data


def _save(data: dict) -> None:
    data["updated"] = state.now()
    # keep the most recent 500 runs
    data["runs"] = data["runs"][-500:]
    state.write_json(JOBS_PATH, data)


def start(stage: str, **meta: Any) -> str:
    """Mark a stage as running. Returns a job id."""
    job_id = f"{stage}-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    data = _load()
    data["runs"].append(
        {
            "id": job_id,
            "stage": stage,
            "status": "running",
            "started": state.now(),
            "started_ts": time.time(),
            "ended": None,
            "duration_s": None,
            "items": None,
            "error": None,
            "meta": meta,
        }
    )
    _save(data)
    return job_id


def finish(job_id: str, status: str = "ok", items: int | None = None, error: str | None = None, **meta: Any) -> None:
    data = _load()
    for run in data["runs"]:
        if run["id"] == job_id:
            run["status"] = status
            run["ended"] = state.now()
            if run.get("started_ts"):
                run["duration_s"] = round(time.time() - run["started_ts"], 1)
            run["items"] = items
            run["error"] = error
            if meta:
                run.setdefault("meta", {}).update(meta)
            break
    _save(data)
    if status == "failed":
        state.error(run.get("stage", "?"), error or "job failed", job_id=job_id)
    elif status == "ok":
        state.info(run.get("stage", "?"), f"completed ({items if items is not None else '?'} items)", job_id=job_id)


def fail(job_id: str, error: str) -> None:
    finish(job_id, status="failed", error=error)


def recent(limit: int = 40) -> list[dict]:
    runs = _load()["runs"]
    return list(reversed(runs[-limit:]))


def active() -> list[dict]:
    """Jobs still marked running (i.e. ongoing work)."""
    return [r for r in _load()["runs"] if r["status"] == "running"]


def last_by_stage() -> dict[str, dict | None]:
    out: dict[str, dict | None] = {s: None for s, _ in STAGES}
    for run in reversed(_load()["runs"]):
        if run["stage"] in out and out[run["stage"]] is None:
            out[run["stage"]] = run
    return out
