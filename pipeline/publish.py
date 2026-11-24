"""Stage 7: publish to Instagram Reels, Facebook Reels and TikTok.

Only official APIs. Each platform posts independently so one failure never
blocks the others; results land in state/posted.json which the dashboard reads.

Two transports:

  * **Postiz** (`pipeline/postiz.py`) - one API key, and the accounts are
    connected by clicking through OAuth in the Postiz UI. Preferred when
    POSTIZ_URL + POSTIZ_API_KEY are set, because it removes three separate
    developer apps, three token flows and three upload protocols.
  * **direct** - the platform APIs implemented below. Used for any platform
    that has no Postiz channel, and for everything when Postiz is not set up.

`posting.transport` in config/accounts.yaml can force one: auto (default),
postiz or direct.
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import tempfile
import time

from . import jobs, postiz, state, tokens

POLL_INTERVAL = 10
POLL_TIMEOUT = 600

PLATFORMS = ("instagram", "facebook", "tiktok", "youtube")


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


# Credentials each platform needs when we talk to its API directly.
DIRECT_CREDS = {
    "instagram": ("IG_USER_ID", "IG_ACCESS_TOKEN"),
    "facebook": ("FB_PAGE_ID", "FB_PAGE_TOKEN"),
    "tiktok": ("TT_CLIENT_KEY", "TT_CLIENT_SECRET"),
}


def _platform_ready(name: str, cfg: dict) -> bool:
    """Can we post with the platform's own API?

    Enabled *and* able to authenticate: a checkbox without tokens is not a
    platform, it is a red run waiting to happen.
    """
    if not cfg.get(name, {}).get("enabled"):
        return False
    need = DIRECT_CREDS.get(name)
    if not need:
        # YouTube has no direct implementation here; Postiz can do it.
        return False
    return all(state.config.env(k) for k in need)


def _postiz_ready(name: str) -> bool:
    if not postiz.configured():
        return False
    try:
        return postiz.integration_for(name) is not None
    except Exception as exc:
        state.warn("publish", f"postiz could not list channels for {name}",
                   reason=str(exc)[:200])
        return False


def _transport(name: str, cfg: dict, mode: str) -> str | None:
    """How this platform will be posted to: 'postiz', 'direct' or None."""
    if not cfg.get(name, {}).get("enabled"):
        return None
    has_direct = _platform_ready(name, cfg)
    has_postiz = _postiz_ready(name)
    if mode == "postiz":
        return "postiz" if has_postiz else None
    if mode == "direct":
        return "direct" if has_direct else None
    # auto: Postiz wins - one credential fewer to expire.
    return "postiz" if has_postiz else ("direct" if has_direct else None)


def _local_file(clip: dict) -> str:
    """A local copy of the clip for Postiz's multipart upload.

    Clips rendered in this run are still on disk. Older ones are not (runners
    are ephemeral), so fall back to downloading the public copy.
    """
    path = (clip.get("path") or "").strip()
    if path and pathlib.Path(path).is_file():
        return path
    url = (clip.get("r2_url") or "").strip()
    if not url:
        raise RuntimeError("no local file and no public URL to upload")
    import requests

    dest = pathlib.Path(tempfile.gettempdir()) / f"postiz_{clip.get('clip_id') or 'clip'}.mp4"
    with requests.get(url, stream=True, timeout=300) as resp:
        resp.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in resp.iter_content(1 << 20):
                if chunk:
                    fh.write(chunk)
    if dest.stat().st_size == 0:
        raise RuntimeError("downloaded clip is empty")
    return str(dest)


def run() -> int:
    job = jobs.start("publish")
    manifests = state.recent_rendered()
    accounts = state.config.accounts()
    platforms = accounts.get("platforms", {})
    auto = bool(accounts.get("posting", {}).get("auto_approve", True))

    posted = 0
    failed = 0
    no_url = 0
    waiting = 0

    mode = str(accounts.get("posting", {}).get("transport") or "auto").lower()
    routes = {name: _transport(name, platforms, mode) for name in PLATFORMS}
    for name in PLATFORMS:
        if not platforms.get(name, {}).get("enabled") or routes[name]:
            continue
        if mode == "auto":
            state.warn("publish", f"{name} is enabled but has neither direct "
                       "credentials nor a Postiz channel - skipped until one is added")
        else:
            state.warn("publish", f"{name} is enabled but transport '{mode}' "
                       f"cannot post to it")

    if auto and not any(routes.values()):
        hint = ""
        if postiz.configured():
            hint = (" Postiz is reachable but has no connected channel, so open "
                    "its UI and connect Instagram/Facebook/TikTok once.")
        state.info("publish", "no platform is connected yet - clips stay in storage, "
                   "nothing is lost; add the tokens and they will post next run" + hint)
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
                waiting += 1
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
            # Only the direct transport needs a public URL - Meta and TikTok pull
            # the file themselves. Postiz uploads the local copy, so a storage
            # link hiccup no longer blocks posting.
            needs_url = any(r == "direct" for r in routes.values())
            if needs_url and not clip.get("r2_url"):
                clip["stage"] = "failed"
                clip["error"] = "no public URL from storage"
                failed += 1
                no_url += 1
                state.error("publish", "clip has no public URL - storage did not produce one",
                            clip_id=clip.get("clip_id"))
                state.save_rendered(rendered)
                continue

            # The upload stage now proves the link serves bytes. If it did not,
            # posting would only burn a platform call and report a confusing
            # failure there, so stop here with the real reason.
            if clip.get("url_ok") is False:
                clip["stage"] = "failed"
                clip["error"] = "the storage link serves no video - upload it again"
                failed += 1
                state.error("publish", "storage link is dead - not posting",
                            clip_id=clip.get("clip_id"))
                state.save_rendered(rendered)
                continue

            caption = _build_caption(clip)
            title = str(clip.get("title") or clip.get("hook") or "clip")
            results: list[dict] = []
            errors: list[str] = []

            # A Postiz post needs a file, not a URL, so upload the clip once and
            # reuse that media for every Postiz channel.
            media: dict | None = None
            if "postiz" in routes.values():
                try:
                    media = postiz.upload(_local_file(clip))
                except Exception as exc:
                    errors.append(f"postiz upload: {str(exc)[:200]}")
                    state.error("publish", "postiz upload failed",
                                clip_id=clip.get("clip_id"), reason=str(exc)[:300])

            for name in PLATFORMS:
                route = routes.get(name)
                if not route:
                    continue
                options = dict(platforms.get(name) or {})
                try:
                    if route == "postiz":
                        if media is None:
                            raise RuntimeError("the upload failed - see the error above")
                        results.append(postiz.publish(name, media, caption,
                                                      title=title, options=options))
                    elif name == "instagram":
                        results.append(publish_instagram(clip["r2_url"], caption))
                    elif name == "facebook":
                        results.append(publish_facebook(clip["r2_url"], caption,
                                                        local_path=clip.get("path")))
                    elif name == "tiktok":
                        results.append(publish_tiktok(clip["r2_url"], caption,
                                                      draft_mode=bool(options.get("draft_mode", True))))
                except Exception as exc:
                    errors.append(f"{name}: {str(exc)[:200]}")
                    state.error("publish", f"{name} failed", clip_id=clip.get("clip_id"),
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

    # A green run while every clip waits for approval is easy to misread as
    # "the pipeline did nothing". Say the count out loud, and put it in the job
    # note so the dashboard row explains it too.
    if waiting and not posted and not failed:
        state.info("publish",
                   f"{waiting} clip(s) are uploaded and waiting for approval - "
                   "next run with auto_approve ticked will post them",
                   waiting=waiting)

    note = f"{waiting} waiting for approval" if (waiting and not posted) else None
    jobs.finish(job, status="ok" if not failed else "partial", items=posted,
                failed=failed, note=note)
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
