"""Stage 8: pull view/like stats back so the dashboard can show performance."""
from __future__ import annotations

import argparse
import sys

from . import jobs, state, tokens


def _ig_stats(media_id: str) -> dict:
    import requests

    token = tokens.meta_refresh() or state.config.env("IG_ACCESS_TOKEN")
    resp = requests.get(
        f"https://graph.facebook.com/v21.0/{media_id}/insights",
        headers={"Authorization": f"Bearer {token}"},
        params={"metric": "plays,likes,comments,shares,saves"},
        timeout=60,
    )
    resp.raise_for_status()
    return {
        d["name"]: d.get("values", [{}])[0].get("value", 0)
        for d in resp.json().get("data", [])
    }


def _fb_stats(video_id: str) -> dict:
    import requests

    token = tokens.meta_refresh() or state.config.env("FB_PAGE_TOKEN")
    resp = requests.get(
        f"https://graph.facebook.com/v21.0/{video_id}",
        headers={"Authorization": f"Bearer {token}"},
        params={"fields": "plays,likes,comments"},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    return {"plays": data.get("plays", 0), "likes": data.get("likes", 0),
            "comments": data.get("comments", 0)}


def _tt_stats() -> dict:
    import requests

    access = tokens.tiktok_access_token()
    resp = requests.post(
        "https://open.tiktokapis.com/v2/video/query/?fields=id,title,like_count,comment_count,share_count,view_count",
        headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"},
        json={"filters": {"video_ids": []}, "max_count": 20},
        timeout=60,
    )
    resp.raise_for_status()
    return {
        v["id"]: {"views": v.get("view_count", 0), "likes": v.get("like_count", 0),
                  "comments": v.get("comment_count", 0), "shares": v.get("share_count", 0)}
        for v in resp.json().get("data", {}).get("videos", [])
    }


def run() -> int:
    job = jobs.start("analytics")
    posted = state.load_posted()
    perf = state.load_performance()
    by_id = {c.get("clip_id"): c for c in perf.get("clips", [])}

    tt_cache: dict | None = None
    updated = 0

    for entry in posted.get("posts", []):
        key = entry.get("post_id", "")
        platform = entry.get("platform")
        stats: dict = {}
        try:
            if platform == "instagram":
                stats = _ig_stats(key)
            elif platform == "facebook":
                stats = _fb_stats(key)
            elif platform == "tiktok":
                if tt_cache is None:
                    tt_cache = _tt_stats()
                stats = tt_cache.get(key, {})
        except Exception as exc:
            state.warn("analytics", f"stats failed for {platform}", reason=str(exc)[:200])
            continue

        if not stats:
            continue

        rec = by_id.setdefault(entry.get("clip_id"), {
            "clip_id": entry.get("clip_id"),
            "campaign": entry.get("campaign"),
            "platforms": {},
        })
        rec.setdefault("platforms", {})[platform] = {
            **stats,
            "post_id": key,
            "url": entry.get("url"),
            "captured_at": state.now(),
        }
        rec["updated"] = state.now()
        updated += 1

    # daily rollup for the chart
    perf.setdefault("daily", {})
    today = state.today()
    day = perf["daily"].setdefault(today, {"views": 0, "clips": 0})
    day["clips"] = len(posted.get("posts", []))
    day["views"] = sum(
        p.get("views", 0) or 0
        for rec in by_id.values()
        for p in rec.get("platforms", {}).values()
    )

    perf["clips"] = list(by_id.values())
    perf["updated"] = state.now()
    state.save_performance(perf)
    jobs.finish(job, status="ok", items=updated)
    return updated


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description="Pull analytics").parse_args(argv)
    n = run()
    print(f"updated stats for {n} post(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
