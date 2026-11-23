"""Token handling for TikTok / Meta, plus sync to GitHub Actions secrets.

The repo is public, so rotated tokens are never committed. Instead we push
them into an Actions secret (SECRETS_ADMIN_PAT must have secrets:write) so
the next run can pick them up. If that PAT is absent we keep them in the
job artifact instead.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import tempfile

from . import state

STATE_FILE = state.tokens_path()


def get(platform: str) -> dict:
    data = state.load_tokens()
    return data.get(platform, {})


def put(platform: str, payload: dict) -> None:
    data = state.load_tokens()
    data[platform] = payload
    data["updated"] = state.now()
    state.save_tokens(data)
    _sync_to_secret(data)


def _sync_to_secret(data: dict) -> None:
    """Best-effort: mirror the token store into a GitHub Actions secret."""
    pat = os.environ.get("SECRETS_ADMIN_PAT") or os.environ.get("GH_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not (pat and repo):
        return
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
            json.dump(data, fh)
            tmp = fh.name
        subprocess.run(
            ["gh", "secret", "set", "STATE_TOKENS", "--repo", repo, "--body", f"@{tmp}"],
            check=True, capture_output=True, timeout=60,
            env={**os.environ, "GH_TOKEN": pat},
        )
    except Exception as exc:  # pragma: no cover
        state.warn("tokens", "could not sync tokens to secrets", reason=str(exc)[:200])
    finally:
        if "tmp" in dir() and os.path.exists(tmp):
            os.unlink(tmp)


def restore_from_env() -> None:
    """Called at job start: hydrate the local token store from the secret."""
    raw = os.environ.get("STATE_TOKENS")
    if not raw or STATE_FILE.is_file():
        return
    try:
        data = json.loads(raw)
        state.save_tokens(data)
    except json.JSONDecodeError:
        state.warn("tokens", "STATE_TOKENS secret is not valid JSON")


# --------------------------------------------------------------------------
# TikTok OAuth
# --------------------------------------------------------------------------
TT_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"


def tiktok_refresh() -> dict:
    import requests

    cur = get("tiktok")
    refresh = cur.get("refresh_token")
    client_key = os.environ.get("TT_CLIENT_KEY")
    client_secret = os.environ.get("TT_CLIENT_SECRET")
    if not (refresh and client_key and client_secret):
        raise RuntimeError("TikTok tokens/client not configured")
    resp = requests.post(
        TT_TOKEN_URL,
        data={
            "client_key": client_key,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "access_type": "offline",
        },
        timeout=60,
    )
    resp.raise_for_status()
    payload = resp.json().get("data", resp.json())
    new = {
        "access_token": payload.get("access_token"),
        "refresh_token": payload.get("refresh_token", refresh),
        "open_id": payload.get("open_id", cur.get("open_id")),
        "expires_in": payload.get("expires_in"),
        "updated": state.now(),
    }
    put("tiktok", new)
    return new


def tiktok_access_token() -> str:
    cur = get("tiktok")
    tok = cur.get("access_token")
    if not tok:
        raise RuntimeError("TikTok not authorised - run scripts/tiktok_oauth.py")
    # Refresh opportunistically; the API will 401 if stale and we retry once.
    try:
        return tiktok_refresh().get("access_token") or tok
    except Exception:
        return tok


# --------------------------------------------------------------------------
# Meta (Instagram / Facebook) long-lived token refresh
# --------------------------------------------------------------------------
def meta_refresh() -> str | None:
    import requests

    cur = get("meta")
    token = cur.get("access_token") or os.environ.get("IG_ACCESS_TOKEN") or os.environ.get("FB_PAGE_TOKEN")
    app_id = os.environ.get("IG_APP_ID") or os.environ.get("FB_APP_ID")
    app_secret = os.environ.get("IG_APP_SECRET") or os.environ.get("FB_APP_SECRET")
    if not (token and app_id and app_secret):
        return token
    try:
        resp = requests.get(
            "https://graph.facebook.com/v21.0/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": token,
            },
            timeout=60,
        )
        resp.raise_for_status()
        new_token = resp.json().get("access_token", token)
        put("meta", {"access_token": new_token, "updated": state.now()})
        return new_token
    except Exception as exc:  # pragma: no cover
        state.warn("tokens", "meta refresh failed, using existing", reason=str(exc)[:200])
        return token


def account_health() -> dict:
    """What the dashboard shows under 'Accounts'."""
    meta = get("meta")
    tt = get("tiktok")
    return {
        "instagram": {
            "enabled": True,
            "configured": bool(meta.get("access_token") or os.environ.get("IG_ACCESS_TOKEN")),
            "user_id": bool(os.environ.get("IG_USER_ID")),
        },
        "facebook": {
            "enabled": bool(os.environ.get("FB_PAGE_ID")),
            "configured": bool(meta.get("access_token") or os.environ.get("FB_PAGE_TOKEN")),
        },
        "tiktok": {
            "enabled": True,
            "configured": bool(tt.get("access_token")),
            "open_id": bool(tt.get("open_id")),
            "last_refresh": tt.get("updated"),
        },
        "llm": {
            "groq": bool(os.environ.get("GROQ_API_KEY")),
            "gemini": bool(os.environ.get("GEMINI_API_KEY")),
        },
    }
