"""Stage 7: publish to Instagram Reels, Facebook Reels and TikTok.

Only official APIs. Each platform posts independently so one failure never
blocks the others; results land in state/posted.json which the dashboard reads.
"""
from __future__ import annotations

import argparse
import sys
import time

from . import jobs, state, tokens

POLL_INTERVAL = 10
POLL_TIMEOUT = 600


# --------------------------------------------------------------------------
# Instagram Reels (Graph API)
# --------------------------------------------------------------------------
def publish_instagram(url: str, caption: str) -> dict:
    import requests

    ig_user = state.config.env("IG_USER_ID")
    token = tokens.meta_refresh() or state.config.env("IG_ACCESS_TOKEN")
    if not (ig_user and token):
        raise RuntimeError("Instagram not configured (IG_USER_ID / IG_ACCESS_TOKEN)")

    base = "https://graph.facebook.com/v21.0"
    headers = {"Authorization": f"Bearer {token}"}

    create = requests.post(
        f"{base}/{ig_user}/media",
        headers=headers,
        data={
            "media_type": "REELS",
            "video_url": url,
            "caption": caption[:2200],
            "share_to_feed": "true",
            "cover_frame_ms": "3500",
        },
        timeout=120,
    )
    create.raise_for_status()
    container = create.json()["id"]

    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        status = requests.get(f"{base}/{container}", headers=headers,
                              params={"fields": "status_code,status"}, timeout=60).json()
        code = status.get("status_code")
        if code == "FINISHED":
            break
        if code in {"ERROR", "EXPIRED"}:
            raise RuntimeError(f"IG container {code}: {status.get('status')}")
        time.sleep(POLL_INTERVAL)
    else:
        raise RuntimeError("IG container timed out")

    pub = requests.post(f"{base}/{ig_user}/media_publish", headers=headers,
                        data={"creation_id": container}, timeout=120)
    pub.raise_for_status()
    media_id = pub.json()["id"]
    return {"platform": "instagram", "post_id": media_id,
            "url": f"https://www.instagram.com/reel/{media_id}/"}


# --------------------------------------------------------------------------
# Facebook Reels (Graph API, Page token)
# --------------------------------------------------------------------------
def publish_facebook(url: str, caption: str) -> dict:
    import requests

    page = state.config.env("FB_PAGE_ID")
    token = tokens.meta_refresh() or state.config.env("FB_PAGE_TOKEN")
    if not (page and token):
        raise RuntimeError("Facebook not configured (FB_PAGE_ID / FB_PAGE_TOKEN)")

    base = "https://graph.facebook.com/v21.0"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.post(
        f"{base}/{page}/video_reels",
        headers=headers,
        data={"video_url": url, "description": caption[:5000]},
        timeout=120,
    )
    resp.raise_for_status()
    reel_id = resp.json()["id"]

    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        status = requests.get(f"{base}/{reel_id}", headers=headers,
                              params={"fields": "status_code"}, timeout=60).json()
        code = status.get("status_code")
        if code == "FINISHED":
            break
        if code in {"ERROR", "EXPIRED"}:
            raise RuntimeError(f"FB reel {code}")
        time.sleep(POLL_INTERVAL)
    else:
        raise RuntimeError("FB reel processing timed out")

    return {"platform": "facebook", "post_id": reel_id,
            "url": f"https://www.facebook.com/reel/{reel_id}/"}


# --------------------------------------------------------------------------
# TikTok (Content Posting API - Direct Post)
# --------------------------------------------------------------------------
def publish_tiktok(url: str, caption: str, draft_mode: bool = True) -> dict:
    import requests

    access = tokens.tiktok_access_token()
    headers = {"Authorization": f"Bearer {access}", "Content-Type": "application/json"}
    base = "https://open.tiktokapis.com/v2"

    publish_mode = "MEDIA_UPLOAD" if draft_mode else "DIRECT_POST"
    init_body = {
        "post_info": {
            "title": caption[:150][:2200],
            "privacy_level": "SELF_ONLY" if draft_mode else "PUBLIC_TO_EVERYONE",
            "disable_comment": False,
            "disable_duet": False,
            "disable_stitch": False,
            "video_cover_timestamp_ms": 1000,
        },
        "source_info": {"source": "PULL_FROM_URL", "video_url": url},
    }
    resp = requests.post(
        f"{base}/post/publish/{'video/init/' if publish_mode == 'DIRECT_POST' else 'inbox/video/init/'}",
        headers=headers, json=init_body, timeout=120,
    )
    resp.raise_for_status()
    data = resp.json().get("data", {})
    publish_id = data.get("publish_id") or data.get("upload_url")
    if not publish_id:
        raise RuntimeError(f"TikTok init failed: {resp.text[:400]}")

    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        st = requests.post(f"{base}/post/publish/status/fetch/", headers=headers,
                           json={"publish_id": publish_id}, timeout=60).json()
        status = st.get("data", {}).get("status")
        if status == "PUBLISH_COMPLETE":
            break
        if status in {"SEND_TO_USER_INBOX", "PUBLISH_READY"} and not draft_mode:
            time.sleep(POLL_INTERVAL)
            continue
        if status == "FAILED":
            raise RuntimeError(f"TikTok publish failed: {st}")
        time.sleep(POLL_INTERVAL)
    else:
        raise RuntimeError("TikTok publish timed out")

    return {"platform": "tiktok", "post_id": publish_id,
            "url": "https://www.tiktok.com/", "draft": draft_mode}


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------
def _build_caption(clip: dict) -> str:
    parts = [str(clip.get("caption") or clip.get("title") or "").strip()]
    tags = " ".join(clip.get("hashtags") or [])
    if tags:
        parts.append(tags)
    return "\n\n".join(p for p in parts if p)


def run() -> int:
    job = jobs.start("publish")
    rendered = state.load_rendered()
    accounts = state.config.accounts()
    platforms = accounts.get("platforms", {})
    auto = bool(accounts.get("posting", {}).get("auto_approve", True))

    posted = 0
    failed = 0

    for clip in rendered.get("clips", []):
        if clip.get("stage") != "uploaded" and clip.get("stage") != "published":
            continue
        if clip.get("stage") == "published":
            continue
        if not auto:
            state.info("publish", "auto_approve=false - waiting for manual approval",
                       clip_id=clip.get("clip_id"))
            continue
        if not clip.get("r2_url"):
            clip["stage"] = "failed"
            clip["error"] = "no public URL"
            failed += 1
            continue

        caption = _build_caption(clip)
        results: list[dict] = []
        errors: list[str] = []

        if platforms.get("instagram", {}).get("enabled"):
            try:
                results.append(publish_instagram(clip["r2_url"], caption))
            except Exception as exc:
                errors.append(f"instagram: {str(exc)[:200]}")
                state.error("publish", "instagram failed", clip_id=clip.get("clip_id"),
                            reason=str(exc)[:300])

        if platforms.get("facebook", {}).get("enabled"):
            try:
                results.append(publish_facebook(clip["r2_url"], caption))
            except Exception as exc:
                errors.append(f"facebook: {str(exc)[:200]}")
                state.error("publish", "facebook failed", clip_id=clip.get("clip_id"),
                            reason=str(exc)[:300])

        if platforms.get("tiktok", {}).get("enabled"):
            try:
                results.append(publish_tiktok(clip["r2_url"], caption,
                                              draft_mode=bool(platforms["tiktok"].get("draft_mode", True))))
            except Exception as exc:
                errors.append(f"tiktok: {str(exc)[:200]}")
                state.error("publish", "tiktok failed", clip_id=clip.get("clip_id"),
                            reason=str(exc)[:300])

        if results:
            clip["stage"] = "published"
            clip["results"] = results
            posted += 1
            for r in results:
                state.append_post({
                    "clip_id": clip.get("clip_id"),
                    "campaign": clip.get("campaign"),
                    "hash": clip.get("hash"),
                    "r2_url": clip.get("r2_url"),
                    "caption": caption[:300],
                    "posted_at": state.now(),
                    **r,
                })
        else:
            clip["stage"] = "failed"
            clip["error"] = "; ".join(errors) or "no platform accepted"
            failed += 1
        state.save_rendered(rendered)

    jobs.finish(job, status="ok" if not failed else "partial", items=posted, failed=failed)
    return posted


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description="Publish clips").parse_args(argv)
    n = run()
    print(f"published {n} clip(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
