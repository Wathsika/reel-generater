"""Audience heatmap: where did live chat spike during a stream?

Streamers' VODs keep their chat replay. Message volume is a decent proxy for
"something happened here" - the audience spams emotes when a play, a reaction
or a big hit lands. We sample chat density on a coarse grid (one GQL call per
SAMPLE_STEP seconds) and use two things:

  * best_window()  - pick the busiest N-minute window to clip from
  * peaks()        - timestamps to hint the moment picker at

This is best-effort. Twitch only, no auth, bounded by a time budget; every
failure path returns None so the pipeline falls back to plain defaults.
"""
from __future__ import annotations

import json
import re
import time

import requests

from . import config, state

GQL = "https://gql.twitch.tv/gql"
CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"  # public web client id
SAMPLE_STEP = 120          # seconds of VOD per density sample
BUDGET_SECONDS = 240       # never spend more than this on chat

_QUERY = """query VideoCommentsByOffsetOrCursor($videoID: ID!, $contentOffsetSeconds: Int) {
  video(id: $videoID) {
    comments(contentOffsetSeconds: $contentOffsetSeconds) {
      edges { node { id contentOffsetSeconds } }
    }
  }
}"""


def twitch_vod_id(url: str) -> str | None:
    m = re.search(r"twitch\.tv/videos/(\d+)", url or "")
    return m.group(1) if m else None


def _density_at(vod_id: str, offset: int) -> float | None:
    """Messages per minute around `offset`, or None on failure."""
    try:
        resp = requests.post(
            GQL,
            headers={"Client-ID": CLIENT_ID, "Content-Type": "application/json"},
            json={"query": _QUERY, "variables": {
                "videoID": vod_id, "contentOffsetSeconds": int(offset),
            }},
            timeout=20,
        )
        resp.raise_for_status()
        edges = (
            (((resp.json().get("data") or {}).get("video") or {})
             .get("comments") or {}).get("edges") or []
        )
        offsets = [
            int(e["node"]["contentOffsetSeconds"])
            for e in edges if e.get("node") and e["node"].get("contentOffsetSeconds") is not None
        ]
        if not offsets:
            return 0.0
        span = max(10.0, float(max(offsets) - min(offsets)))
        return round(len(offsets) / (span / 60.0), 2)
    except Exception:
        return None


def _cache_path(vod_id: str, start: int, end: int, step: int):
    return config.WORK / "heatmap" / f"tw{vod_id}_{start}_{end}_{step}.json"


def twitch_samples(vod_id: str, start: int, end: int, step: int = SAMPLE_STEP) -> list[dict] | None:
    """Sample chat density across [start, end]. Cached inside work/."""
    cache = _cache_path(vod_id, start, end, step)
    if cache.is_file():
        try:
            return json.loads(cache.read_text(encoding="utf-8")).get("samples")
        except Exception:
            pass

    t0 = time.monotonic()
    samples: list[dict] = []
    failures = 0
    for offset in range(int(start), int(end), step):
        if time.monotonic() - t0 > BUDGET_SECONDS:
            state.warn("heatmap", "chat budget hit", vod=vod_id, sampled=len(samples))
            break
        rate = _density_at(vod_id, offset)
        if rate is None:
            failures += 1
            if failures >= 3 and not samples:
                return None
            continue
        samples.append({"t": offset, "rate": rate})

    if not samples:
        return None

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"vod": vod_id, "samples": samples}), encoding="utf-8")
    return samples


def best_window(samples: list[dict], length: int, step: int = SAMPLE_STEP) -> tuple[int, int] | None:
    """Busiest `length`-second window, by summed chat density."""
    if not samples:
        return None
    best_start, best_score = None, -1.0
    for s in samples:
        lo, hi = s["t"], s["t"] + length
        total = sum(x["rate"] for x in samples if lo <= x["t"] < hi)
        if total > best_score:
            best_start, best_score = s["t"], total
    if best_start is None or best_score <= 0:
        return None
    return best_start, best_start + length


def peaks(samples: list[dict], top: int = 8, min_gap: int = 300) -> list[int]:
    """Timestamps of the busiest moments, spread out so they aren't duplicates."""
    picked: list[int] = []
    for s in sorted(samples, key=lambda x: -x["rate"]):
        if s["rate"] <= 0:
            continue
        if all(abs(s["t"] - p) >= min_gap for p in picked):
            picked.append(int(s["t"]))
        if len(picked) >= top:
            break
    return sorted(picked)


def analyse(url: str, start: int = 0, end: int | None = None,
            step: int = SAMPLE_STEP) -> dict | None:
    """Chat heatmap for a VOD window. Returns None when unavailable."""
    vod = twitch_vod_id(url)
    if not vod or end is None or end <= start:
        return None
    samples = twitch_samples(vod, start, end, step)
    if not samples:
        return None
    return {
        "platform": "twitch",
        "vod": vod,
        "samples": samples,
        "peaks": peaks(samples),
    }
