"""Postiz transport: one API key instead of three developer apps.

Postiz (https://github.com/gitroomhq/postiz-app) is an open-source social media
scheduler. You connect Instagram, Facebook, TikTok, YouTube and X once in its
web UI - it owns the OAuth flow and the platform app review - and then any
machine can post with a single API key.

For this pipeline that removes the worst part of publishing:

  * no Meta app review, no Facebook Page token that expires every 60 days,
  * no TikTok audit before a draft can even be uploaded,
  * no separate upload protocol per platform (Meta pulls from a URL, TikTok
    wants PULL_FROM_URL, Facebook wants a 3-phase session),
  * the "which account is which id" problem is answered by `GET /integrations`.

It is entirely optional. Without POSTIZ_URL + POSTIZ_API_KEY the pipeline keeps
using the direct platform APIs in publish.py, so nothing breaks if Postiz is not
set up yet.

API: https://docs.postiz.com/public-api
    GET  {base}/integrations  -> connected channels
    POST {base}/upload        -> multipart file -> {id, path}
    POST {base}/posts         -> publish now

`base` is https://api.postiz.com/public/v1 (Postiz Cloud) or
https://<your-host>/api/public/v1 (self-hosted). POSTIZ_URL may be given as
either form; the /api/public/v1 suffix is added when it is missing.
"""
from __future__ import annotations

import pathlib
import time

from . import config, state

CLOUD = "https://api.postiz.com/public/v1"

# Postiz calls a connected account an "integration"; the identifier is what
# the platform is, the id is what this account is.
KNOWN = {
    "instagram": ("instagram", "instagram-standalone"),
    "facebook": ("facebook",),
    "tiktok": ("tiktok",),
    "youtube": ("youtube",),
}

_cache: dict[str, object] = {}


# --------------------------------------------------------------------------
# setup
# --------------------------------------------------------------------------
def configured() -> bool:
    return bool(config.env("POSTIZ_API_KEY") and config.env("POSTIZ_URL"))


def base_url() -> str:
    raw = (config.env("POSTIZ_URL") or "").strip().rstrip("/")
    if not raw:
        return CLOUD
    if raw.endswith("/public/v1"):
        return raw
    if raw.rstrip("/").endswith("/api"):
        return f"{raw}/public/v1"
    return f"{raw}/api/public/v1"


def _headers() -> dict[str, str]:
    return {"Authorization": (config.env("POSTIZ_API_KEY") or "").strip()}


def _timeout() -> int:
    try:
        return max(30, int(config.env("POSTIZ_TIMEOUT", "300")))
    except ValueError:
        return 300


def _check(resp, what: str) -> dict | list:
    """Turn a non-2xx answer into one readable line, not a stack trace."""
    if resp.status_code >= 400:
        detail = (resp.text or "").strip().replace("\n", " ")[:300]
        if resp.status_code == 401:
            detail = "the API key was rejected (POSTIZ_API_KEY)"
        elif resp.status_code == 403:
            detail = "this API key does not own that channel"
        elif resp.status_code == 429:
            detail = "rate limited (90 posts/hour on the create-post endpoint)"
        raise RuntimeError(f"postiz {what}: HTTP {resp.status_code} {detail}")
    try:
        return resp.json()
    except ValueError:
        return {}


# --------------------------------------------------------------------------
# channels
# --------------------------------------------------------------------------
def integrations(refresh: bool = False) -> list[dict]:
    """Every connected channel on this Postiz account (cached per run)."""
    if not refresh and "integrations" in _cache:
        return _cache["integrations"]  # type: ignore[return-value]
    import requests

    resp = requests.get(f"{base_url()}/integrations", headers=_headers(),
                        timeout=_timeout())
    data = _check(resp, "integrations")
    items = data if isinstance(data, list) else (data.get("integrations") or [])
    items = [i for i in items if isinstance(i, dict)]
    _cache["integrations"] = items
    return items


def _identifier(item: dict) -> str:
    for key in ("identifier", "providerIdentifier", "provider", "type"):
        value = str(item.get(key) or "").strip().lower()
        if value:
            return value
    return ""


def integration_for(platform: str) -> dict | None:
    """The connected channel for `platform`, or None."""
    names = KNOWN.get(platform, (platform,))
    for item in integrations():
        if _identifier(item) in names:
            return item
    return None


def connected_platforms() -> dict[str, str]:
    """platform -> integration id, for the platforms this repo knows about."""
    out: dict[str, str] = {}
    for item in integrations():
        ident = _identifier(item)
        for platform, names in KNOWN.items():
            if ident in names and item.get("id"):
                out.setdefault(platform, str(item["id"]))
    return out


def forget() -> None:
    """Drop the cache (tests, or after connecting an account mid-run)."""
    _cache.clear()


# --------------------------------------------------------------------------
# upload
# --------------------------------------------------------------------------
def upload(path: str | pathlib.Path, adjust: bool = False) -> dict:
    """Upload a clip and return {id, path} for use in a post.

    `adjust=True` lets Postiz rescale/re-encode to the platforms' limits; our
    renders are already 1080x1920 H.264, so it is off by default and the upload
    stays synchronous.
    """
    import requests

    file = pathlib.Path(path)
    if not file.is_file() or file.stat().st_size == 0:
        raise RuntimeError(f"postiz upload: file missing or empty ({file})")

    with open(file, "rb") as fh:
        resp = requests.post(
            f"{base_url()}/upload",
            headers=_headers(),
            files={"file": (file.name, fh, "video/mp4")},
            data={"adjust": "true" if adjust else "false"},
            timeout=max(600, _timeout()),
        )
    media = _check(resp, "upload")
    if not isinstance(media, dict) or not media.get("path"):
        raise RuntimeError(f"postiz upload: unexpected answer {str(media)[:200]}")

    if media.get("status") == "processing":
        media = _wait_media(str(media.get("id") or ""), media)
    return {"id": str(media.get("id") or ""), "path": str(media["path"])}


def _wait_media(media_id: str, media: dict, timeout: int = 180) -> dict:
    """Poll /media/{id}/status until an adjusted file is ready."""
    import requests

    deadline = time.time() + timeout
    while media_id and time.time() < deadline:
        try:
            resp = requests.get(f"{base_url()}/media/{media_id}/status",
                                headers=_headers(), timeout=60)
            data = _check(resp, "media status")
        except Exception:
            break
        if isinstance(data, dict) and data.get("status") == "ready":
            media = {**media, **data}
            break
        time.sleep(4)
    return media


# --------------------------------------------------------------------------
# post
# --------------------------------------------------------------------------
def settings_for(platform: str, identifier: str, title: str, options: dict) -> dict:
    """The per-platform settings object Postiz requires."""
    draft = bool(options.get("draft_mode", False))
    if platform == "tiktok":
        return {
            "__type": "tiktok",
            "title": (title or "")[:90],
            "privacy_level": "SELF_ONLY" if draft else str(
                options.get("privacy_level") or "PUBLIC_TO_EVERYONE"),
            "duet": not draft,
            "stitch": not draft,
            "comment": True,
            "autoAddMusic": "no",
            "brand_content_toggle": False,
            "brand_organic_toggle": False,
            # UPLOAD lands in the TikTok inbox for manual publishing, which is
            # what an unaudited app is allowed to do. DIRECT_POST needs the
            # audit; the post then goes live on its own.
            "content_posting_method": "UPLOAD" if draft else "DIRECT_POST",
        }
    if platform == "instagram":
        return {
            "__type": "instagram-standalone" if identifier == "instagram-standalone"
            else "instagram",
            "post_type": "post",          # a video post is a Reel
        }
    if platform == "facebook":
        return {"__type": "facebook", "title": (title or "")[:100]}
    if platform == "youtube":
        return {
            "__type": "youtube",
            "title": (title or "clip")[:100],
            "type": "private" if draft else str(options.get("visibility") or "public"),
            "selfDeclaredMadeForKids": "no",
        }
    return {"__type": identifier or platform}


def publish(
    platform: str,
    media: dict,
    caption: str,
    title: str = "",
    options: dict | None = None,
) -> dict:
    """Publish an already-uploaded file to one platform. Returns a result row."""
    import requests

    channel = integration_for(platform)
    if not channel or not channel.get("id"):
        raise RuntimeError(f"postiz: no {platform} channel connected")
    identifier = _identifier(channel)
    options = options or {}

    body = {
        "type": "now",
        "date": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "shortLink": False,
        "tags": [],
        "posts": [{
            "integration": {"id": str(channel["id"])},
            "value": [{
                "content": caption,
                "image": [{"id": media.get("id", ""), "path": media["path"]}],
            }],
            "settings": settings_for(platform, identifier, title or caption, options),
        }],
    }

    resp = requests.post(f"{base_url()}/posts", headers={**_headers(),
                         "Content-Type": "application/json"},
                         json=body, timeout=max(300, _timeout()))
    data = _check(resp, f"post to {platform}")

    post_id = _post_id(data)
    result = {
        "platform": platform,
        "post_id": post_id,
        "transport": "postiz",
        "integration": str(channel["id"]),
    }
    if options.get("draft_mode"):
        result["draft"] = True
    return result


def _post_id(data) -> str:
    """Dig the created post id out of whatever shape Postiz answered with."""
    def find(obj) -> str:
        if isinstance(obj, dict):
            for key in ("postId", "id", "releaseId"):
                value = obj.get(key)
                if isinstance(value, (str, int)) and str(value).strip():
                    return str(value)
            for value in obj.values():
                got = find(value)
                if got:
                    return got
        elif isinstance(obj, list):
            for item in obj:
                got = find(item)
                if got:
                    return got
        return ""

    return find(data)


# --------------------------------------------------------------------------
# convenience
# --------------------------------------------------------------------------
def publish_file(
    platform: str,
    local_path: str,
    caption: str,
    title: str = "",
    options: dict | None = None,
) -> dict:
    """Upload a file and post it in one call (used by the CLI below)."""
    media = upload(local_path)
    return publish(platform, media, caption, title=title, options=options)


def main() -> int:
    """`python -m pipeline.postiz` - show what is connected, upload nothing."""
    if not configured():
        print("postiz: not configured (set POSTIZ_URL and POSTIZ_API_KEY)")
        return 2
    print("postiz url:", base_url())
    try:
        # never print the key
        platforms = connected_platforms()
    except Exception as exc:
        state.error("postiz", "could not list channels", reason=str(exc)[:300])
        print("postiz: FAILED -", exc)
        return 1
    if not platforms:
        print("postiz: reachable, but no Instagram/Facebook/TikTok/YouTube "
              "channel is connected yet - connect them in the Postiz UI")
        return 0
    for name, ident in platforms.items():
        print(f"  {name:<10} -> {ident}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
