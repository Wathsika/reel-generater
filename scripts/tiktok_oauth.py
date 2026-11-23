"""One-time TikTok OAuth helper.

TikTok tokens expire, so this is only run ONCE by you (not in CI). It prints
the JSON you paste into the `STATE_TOKENS` repository secret; after that the
pipeline refreshes tokens automatically.

Usage:
    set TT_CLIENT_KEY=...
    set TT_CLIENT_SECRET=...
    python scripts/tiktok_oauth.py
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.parse

SCOPES = "user.info.basic,video.publish,video.upload"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"


def main() -> int:
    try:
        import requests
    except ImportError:
        print("pip install -r requirements.txt first")
        return 1

    client_key = os.environ.get("TT_CLIENT_KEY") or input("TT_CLIENT_KEY: ").strip()
    client_secret = os.environ.get("TT_CLIENT_SECRET") or input("TT_CLIENT_SECRET: ").strip()
    redirect = os.environ.get("TT_REDIRECT_URI", "https://localhost/callback")

    auth_url = "https://www.tiktok.com/v2/auth/oauth/authorize/?" + urllib.parse.urlencode(
        {
            "client_key": client_key,
            "scope": SCOPES,
            "response_type": "code",
            "redirect_uri": redirect,
            "state": "clipfactory",
        }
    )

    print("\n=== STEP 1 ===")
    print("Open this URL, log in, and approve access:\n")
    print(auth_url)
    print("\nAfter approving you land on your redirect URL. Copy the `code` value")
    print("from the address bar (everything after `code=` and before `&`).\n")

    code = input("=== STEP 2 === paste the code here: ").strip()
    if not code:
        print("no code provided")
        return 1

    resp = requests.post(
        TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": client_key,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect,
        },
        timeout=60,
    )
    if resp.status_code >= 400:
        print("token exchange failed:", resp.status_code, resp.text[:400])
        return 1

    data = resp.json()
    payload = {
        "tiktok": {
            "access_token": data.get("access_token"),
            "refresh_token": data.get("refresh_token"),
            "open_id": data.get("open_id"),
            "scope": data.get("scope"),
            "expires_in": data.get("expires_in"),
            "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        }
    }

    print("\n=== STEP 3 ===")
    print("Copy everything between the lines into the GitHub secret STATE_TOKENS:\n")
    print("-" * 60)
    print(json.dumps(payload, indent=2))
    print("-" * 60)
    print("\nRepo -> Settings -> Secrets and variables -> Actions -> New secret")
    print("Name: STATE_TOKENS   Value: the JSON above\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
