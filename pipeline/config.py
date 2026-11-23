"""Central configuration, paths and environment loading."""
from __future__ import annotations

import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
STATE = ROOT / "state"
QUEUE = STATE / "queue"
RENDERED = STATE / "rendered"
LOGS = STATE / "logs"
OUT = ROOT / "out"
WORK = ROOT / "work"
DOCS = ROOT / "docs"


def _dotenv() -> None:
    """Load .env if present. Real environment variables always win."""
    path = ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_dotenv()


def env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)


def env_bool(key: str, default: bool = False) -> bool:
    val = os.environ.get(key)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def ensure_dirs() -> None:
    for path in (STATE, QUEUE, RENDERED, LOGS, OUT, WORK, DOCS):
        path.mkdir(parents=True, exist_ok=True)


def _yaml(name: str) -> dict:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("PyYAML missing - run: pip install -r requirements.txt") from exc
    path = CONFIG / name
    if not path.is_file():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def campaigns() -> dict:
    return _yaml("campaigns.yaml")


def accounts() -> dict:
    return _yaml("accounts.yaml")


def ffprobe_bin() -> str:
    return env("FFPROBE_BIN", "ffprobe")


def ffmpeg_bin() -> str:
    return env("FFMPEG_BIN", "ffmpeg")
