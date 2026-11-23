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
# Facebook Reels (Graph API, Page token) - 3-phase Reels Publishing API:
#   start -> upload -> finish(video_state=PUBLISHED)
# --------------------------------------------------------------------------
def publish_facebook(url: str, caption: str, local_path: str | None = None) -> dict:
    import pathlib

    import requests

    page = state.config.env("FB_PAGE_ID")
    token = tokens.meta_refresh() or state.config.env("FB_PAGE_TOKEN")
    if not (page and token):
        raise RuntimeError("Facebook not configured (FB_PAGE_ID / FB_PAGE_TOKEN)")

    base = "https://graph.facebook.com/v21.0"
    headers = {"Authorization": f"Bearer {token}"}
    local_file = pathlib.Path(local_path) if local_path else None
    have_local = bool(local_file and local_file.is_file())

    # 1) START - ask for an upload session. If we have no local file, ask Meta
    # to pull it from the public R2 URL instead.
    start_data = {"upload_phase": "start"}
    if not have_local:
        start_data["file_url"] = url
    start = requests.post(f"{base}/{page}/video_reels", headers=headers,
                          data=start_data, timeout=120)
    if start.status_code >= 400 and not have_local:
        # some app versions reject file_url; retry to at least get a session
        start = requests.post(f"{base}/{page}/video_reels", headers=headers,
                              data={"upload_phase": "start"}, timeout=120)
    start.raise_for_status()
    payload = start.json()
    video_id = payload.get("video_id")
    upload_url = payload.get("upload_url")
    if not video_id:
        raise RuntimeError(f"FB start failed: {payload}")

    # 2) UPLOAD the binary to the returned URL (required unless file_url worked)
    if upload_url and have_local:
        size = local_file.stat().st_size
        with open(local_file, "rb") as fh:
            up = requests.post(
                upload_url,
                headers={
                    "Authorization": f"OAuth {token}",
                    "offset": "0",
                    "file_size": str(size),
                    "Content-Type": "video/mp4",
                },
                data=fh,
                timeout=1800,
            )
        up.raise_for_status()
    elif upload_url and not have_local:
        raise RuntimeError("FB needs the local file to upload (R2 pull not accepted)")

    # 3) FINISH -> publish the reel
    finish = requests.post(
        f"{base}/{page}/video_reels",
        headers=headers,
        data={
            "upload_phase": "finish",
            "video_id": video_id,
            "video_state": "PUBLISHED",
            "description": caption[:5000],
        },
        timeout=120,
    )
    finish.raise_for_status()

    # best-effort status poll (does not fail the publish)
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            st = requests.get(f"{base}/{video_id}", headers=headers,
                              params={"fields": "status"}, timeout=60).json()
            if str(st.get("status", "")).lower() in {"ready", "published", "complete"}:
                break
        except Exception:
            break
        time.sleep(5)

    return {"platform": "facebook", "post_id": video_id,
            "url": f"https://www.facebook.com/reel/{video_id}/"}


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


def _platform_ready(name: str, cfg: dict) -> bool:
    """Enabled *and* able to authenticate. A checkbox without tokens is not a
    platform, it is a red run waiting to happen."""
    if not cfg.get(name, {}).get("enabled"):
        return False
    need = {
        "instagram": ("IG_USER_ID", "IG_ACCESS_TOKEN"),
        "facebook": ("FB_PAGE_ID", "FB_PAGE_TOKEN"),
        "tiktok": ("TT_CLIENT_KEY", "TT_CLIENT_SECRET"),
    }[name]
    return all(state.config.env(k) for k in need)


def run() -> int:
    job = jobs.start("publish")
    manifests = state.recent_rendered()
    accounts = state.config.accounts()
    platforms = accounts.get("platforms", {})
    auto = bool(accounts.get("posting", {}).get("auto_approve", True))

    posted = 0
    failed = 0
    no_url = 0

    ready = {name: _platform_ready(name, platforms)
             for name in ("instagram", "facebook", "tiktok")}
    for name, ok in ready.items():
        if platforms.get(name, {}).get("enabled") and not ok:
            state.warn("publish", f"{name} is enabled but its credentials are "
                       f"missing - skipped until they are added")
    if auto and not any(ready.values()):
        state.info("publish", "no platform is connected yet - clips stay in storage, "
                   "nothing is lost; add the tokens and they will post next run")
        jobs.finish(job, status="ok", items=0, note="no platform credentials")
        return 0

    # Honour posting.max_clips_per_day. The clip count per video is now decided
    # by the model, so one long stream can yield 10 clips; without this they
    # would all go out within a minute, which looks like spam to the platforms.
    cap = int(accounts.get("posting", {}).get("max_clips_per_day") or 0)
    room = cap
    if cap > 0:
        today = state.today()
        already = {
            p.get("clip_id")
            for p in (state.load_posted().get("posts") or [])
            if str(p.get("posted_at", "")).startswith(today)
        }
        room = max(0, cap - len(already))
        if room == 0:
            state.info("publish", f"daily limit of {cap} clip(s) already reached - nothing to do")
            jobs.finish(job, status="ok", items=0, note="daily limit reached")
            return 0

    # Every unchanged clip from the last few days is a candidate, not just
    # today's: a clip held back by the daily cap, or uploaded just before
    # midnight, must still be published later.
    for rendered in manifests:
        for clip in rendered.get("clips", []):
            if clip.get("stage") != "uploaded" and clip.get("stage") != "published":
                continue
            if clip.get("stage") == "published":
                continue
            if not auto:
                state.info("publish", "auto_approve=false - waiting for manual approval",
                           clip_id=clip.get("clip_id"))
                continue
            if cap > 0 and posted >= room:
                # Record the surplus instead of silently dropping it, so the
                # dashboard shows exactly what the daily limit held back.
                clip["stage"] = "skipped"
                clip["error"] = f"daily posting limit reached ({cap}/day)"
                state.save_rendered(rendered)
                continue
            if not clip.get("r2_url"):
                clip["stage"] = "failed"
                clip["error"] = "no public URL from storage"
                failed += 1
                no_url += 1
                state.error("publish", "clip has no public URL - storage did not produce one",
                            clip_id=clip.get("clip_id"))
                state.save_rendered(rendered)
                continue

            caption = _build_caption(clip)
            results: list[dict] = []
            errors: list[str] = []

            if ready["instagram"]:
                try:
                    results.append(publish_instagram(clip["r2_url"], caption))
                except Exception as exc:
                    errors.append(f"instagram: {str(exc)[:200]}")
                    state.error("publish", "instagram failed", clip_id=clip.get("clip_id"),
                                reason=str(exc)[:300])

            if ready["facebook"]:
                try:
                    results.append(publish_facebook(clip["r2_url"], caption,
                                                    local_path=clip.get("path")))
                except Exception as exc:
                    errors.append(f"facebook: {str(exc)[:200]}")
                    state.error("publish", "facebook failed", clip_id=clip.get("clip_id"),
                                reason=str(exc)[:300])

            if ready["tiktok"]:
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

    if auto and failed and not posted:
        reason = ("no public URL from storage" if no_url
                  else "every platform rejected every clip - check the tokens")
        state.error("publish", reason)
        jobs.finish(job, status="failed", items=0, failed=failed)
        return -1

    jobs.finish(job, status="ok" if not failed else "partial", items=posted, failed=failed)
    return posted


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description="Publish clips").parse_args(argv)
    n = run()
    if n < 0:
        print("publish: NOTHING WAS PUBLISHED - see the error log")
        return 1
    print(f"published {n} clip(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
