"""JSON state store.

Every pipeline stage reads/writes here. The dashboard renders exactly these
files, so if a stage records it, the dashboard shows it.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import tempfile
from typing import Any

from . import config

config.ensure_dirs()


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def today() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def read_json(path: pathlib.Path | str, default: Any = None) -> Any:
    path = pathlib.Path(path)
    if not path.is_file():
        return default if default is not None else {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default if default is not None else {}


def write_json(path: pathlib.Path | str, data: Any) -> None:
    """Atomic write so the dashboard never reads a half-written file."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# --------------------------------------------------------------------------
# Queue (daily work-in-progress)
# --------------------------------------------------------------------------
def queue_path(date: str | None = None) -> pathlib.Path:
    return config.QUEUE / f"{date or today()}.json"


def load_queue(date: str | None = None) -> dict:
    return read_json(queue_path(date), {"date": date or today(), "clips": []})


def save_queue(data: dict, date: str | None = None) -> None:
    data.setdefault("date", date or today())
    data["updated"] = now()
    write_json(queue_path(data.get("date", date)), data)


# --------------------------------------------------------------------------
# Rendered manifest
# --------------------------------------------------------------------------
def rendered_path(date: str | None = None) -> pathlib.Path:
    return config.RENDERED / f"{date or today()}.json"


def load_rendered(date: str | None = None) -> dict:
    return read_json(rendered_path(date), {"date": date or today(), "clips": []})


def save_rendered(data: dict, date: str | None = None) -> None:
    data.setdefault("date", date or today())
    data["updated"] = now()
    write_json(rendered_path(data.get("date", date)), data)


def recent_rendered(days: int = 7) -> list[dict]:
    """The newest rendered manifests, old to new.

    Publishing and link repair must not stop at midnight: a clip uploaded at
    23:50, or held back by the daily posting cap, still needs to go out later.
    """
    out: list[dict] = []
    for path in sorted(config.RENDERED.glob("*.json"))[-max(1, days):]:
        data = read_json(path, {})
        if data.get("clips"):
            out.append(data)
    return out


# --------------------------------------------------------------------------
# Posted log
# --------------------------------------------------------------------------
def posted_path() -> pathlib.Path:
    return config.STATE / "posted.json"


def load_posted() -> dict:
    return read_json(posted_path(), {"posts": []})


def append_post(entry: dict) -> None:
    data = load_posted()
    data.setdefault("posts", []).append(entry)
    data["updated"] = now()
    write_json(posted_path(), data)


# --------------------------------------------------------------------------
# Analytics
# --------------------------------------------------------------------------
def performance_path() -> pathlib.Path:
    return config.STATE / "performance.json"


def load_performance() -> dict:
    return read_json(performance_path(), {"clips": []})


def save_performance(data: dict) -> None:
    data["updated"] = now()
    write_json(performance_path(), data)


# --------------------------------------------------------------------------
# Token store (gitignored)
# --------------------------------------------------------------------------
def tokens_path() -> pathlib.Path:
    return config.STATE / "tokens.json"


def load_tokens() -> dict:
    return read_json(tokens_path(), {})


def save_tokens(data: dict) -> None:
    data["updated"] = now()
    write_json(tokens_path(), data)


# --------------------------------------------------------------------------
# Errors / run log (surfaced on the dashboard)
# --------------------------------------------------------------------------
def log_path(date: str | None = None) -> pathlib.Path:
    return config.LOGS / f"{date or today()}.json"


def load_log(date: str | None = None) -> dict:
    return read_json(log_path(date), {"entries": []})


def append_log(level: str, stage: str, message: str, **extra: Any) -> None:
    data = load_log()
    data.setdefault("entries", []).append(
        {"t": now(), "level": level, "stage": stage, "message": message, **extra}
    )
    # keep the last 500 entries
    data["entries"] = data["entries"][-500:]
    write_json(log_path(), data)


def error(stage: str, message: str, **extra: Any) -> None:
    append_log("error", stage, message, **extra)


def warn(stage: str, message: str, **extra: Any) -> None:
    append_log("warn", stage, message, **extra)


def info(stage: str, message: str, **extra: Any) -> None:
    append_log("info", stage, message, **extra)
