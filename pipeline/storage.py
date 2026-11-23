"""Stage 6: upload rendered clips to public storage.

Meta and TikTok fetch the video themselves, so every clip needs a URL that:
  * is public HTTPS with a valid certificate
  * serves the raw file (no redirect, no auth, no HTML page)
  * sends `Content-Type: video/mp4`

Backends (STORAGE_BACKEND):
  s3         any S3-compatible store - MinIO, Backblaze B2, Wasabi, R2  (best)
  nextcloud  WebDAV upload + public share link (works, but header/redirect caveats)
  r2         Cloudflare R2 (alias of s3 with R2 defaults)
"""
from __future__ import annotations

import pathlib
import urllib.parse

from . import config, jobs, state

BACKENDS = {"s3", "nextcloud", "r2"}


# --------------------------------------------------------------------------
# backend selection
# --------------------------------------------------------------------------
def backend() -> str:
    explicit = (config.env("STORAGE_BACKEND") or "").strip().lower()
    if not explicit:
        # config/accounts.yaml -> storage.backend (unless "auto")
        try:
            cfg = (config.accounts().get("storage") or {}).get("backend", "auto")
            if isinstance(cfg, str) and cfg.strip().lower() not in ("", "auto"):
                explicit = cfg.strip().lower()
        except Exception:
            pass
    if explicit in BACKENDS:
        return explicit
    if config.env("S3_ENDPOINT"):
        return "s3"
    if config.env("NC_URL"):
        return "nextcloud"
    return "r2"


def _s3_conf() -> dict:
    if backend() == "r2":
        account = config.env("R2_ACCOUNT_ID")
        return {
            "endpoint": f"https://{account}.r2.cloudflarestorage.com" if account else "",
            "key": config.env("R2_ACCESS_KEY_ID"),
            "secret": config.env("R2_SECRET_ACCESS_KEY"),
            "bucket": config.env("R2_BUCKET", "clips"),
            "region": "auto",
            "public_base": (config.env("R2_PUBLIC_BASE") or "").rstrip("/"),
            "path_style": False,
        }
    return {
        "endpoint": (config.env("S3_ENDPOINT") or "").rstrip("/"),
        "key": config.env("S3_ACCESS_KEY_ID"),
        "secret": config.env("S3_SECRET_ACCESS_KEY"),
        "bucket": config.env("S3_BUCKET", "clips"),
        "region": config.env("S3_REGION", "us-east-1"),
        "public_base": (config.env("S3_PUBLIC_BASE") or "").rstrip("/"),
        "path_style": (config.env("S3_PATH_STYLE", "true") or "true").lower() != "false",
    }


def _s3_client():
    try:
        import boto3
        from botocore.config import Config as BotoConfig
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("boto3 missing - pip install -r requirements.txt") from exc

    c = _s3_conf()
    if not (c["endpoint"] and c["key"] and c["secret"]):
        raise RuntimeError(
            "S3 backend not configured (S3_ENDPOINT / S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY)"
        )
    return boto3.client(
        "s3",
        endpoint_url=c["endpoint"],
        aws_access_key_id=c["key"],
        aws_secret_access_key=c["secret"],
        region_name=c["region"],
        config=BotoConfig(s3={"addressing_style": "path" if c["path_style"] else "auto"}),
    )


# --------------------------------------------------------------------------
# S3 / MinIO / R2
# --------------------------------------------------------------------------
def _s3_upload(path: pathlib.Path, key: str, content_type: str) -> str:
    c = _s3_conf()
    s3 = _s3_client()
    s3.upload_file(
        str(path),
        c["bucket"],
        key,
        ExtraArgs={"ContentType": content_type, "CacheControl": "public, max-age=86400"},
    )
    if c["public_base"]:
        return f"{c['public_base']}/{urllib.parse.quote(key)}"
    # fall back to endpoint/bucket/key (works if the bucket policy allows reads)
    return f"{c['endpoint']}/{c['bucket']}/{urllib.parse.quote(key)}"


# --------------------------------------------------------------------------
# Nextcloud
# --------------------------------------------------------------------------
def _nc_conf() -> dict:
    return {
        "url": (config.env("NC_URL") or "").rstrip("/"),
        "user": config.env("NC_USER"),
        "password": config.env("NC_APP_PASSWORD"),
        "folder": (config.env("NC_FOLDER") or "Clips").strip("/"),
        # Direct-raw-file URL. Meta/TikTok need the bytes with 200 + video/mp4,
        # so the default points at Nextcloud's public WebDAV endpoint (no
        # redirect). `/s/TOKEN/download` 302-redirects and is a fallback only.
        # Placeholders: {url} {token} {share} {name}
        "direct_template": config.env(
            "NC_DIRECT_TEMPLATE", "{url}/public.php/dav/files/{token}"
        ),
    }


def _nc_session():
    import requests

    c = _nc_conf()
    if not (c["url"] and c["user"] and c["password"]):
        raise RuntimeError("Nextcloud not configured (NC_URL / NC_USER / NC_APP_PASSWORD)")
    s = requests.Session()
    s.auth = (c["user"], c["password"])
    s.headers.update({"OCS-APIRequest": "true", "Accept": "application/json"})
    return s, c


def _nc_mkcol(session, url: str) -> None:
    # 201 created, 405 already exists - both fine
    r = session.request("MKCOL", url, timeout=60)
    if r.status_code not in (201, 405):
        raise RuntimeError(f"Nextcloud MKCOL {url} -> {r.status_code}")


def _nc_upload(path: pathlib.Path, key: str, content_type: str) -> str:
    import requests

    session, c = _nc_session()
    user = urllib.parse.quote(c["user"], safe="")
    dav_root = f"{c['url']}/remote.php/dav/files/{user}"

    parts = (c["folder"] + "/" + key).split("/")
    folder_parts = parts[:-1]
    # create the folder chain: Clips, Clips/<date>
    for i in range(1, len(folder_parts) + 1):
        sub = "/".join(urllib.parse.quote(p, safe="") for p in folder_parts[:i])
        _nc_mkcol(session, f"{dav_root}/{sub}")

    file_url = dav_root + "/" + "/".join(urllib.parse.quote(p, safe="") for p in parts)
    with open(path, "rb") as fh:
        put = session.put(file_url, data=fh, headers={"Content-Type": content_type}, timeout=1800)
    if put.status_code not in (201, 204):
        raise RuntimeError(f"Nextcloud upload -> {put.status_code} {put.text[:200]}")

    # create (or reuse) a public read-only share
    share = requests.post(
        f"{c['url']}/ocs/v2.php/apps/files_sharing/api/v1/shares",
        auth=(c["user"], c["password"]),
        headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        data={
            "path": "/" + c["folder"] + "/" + key,
            "shareType": 3,      # public link
            "permissions": 1,    # read only
        },
        timeout=120,
    )
    if share.status_code >= 400:
        raise RuntimeError(f"Nextcloud share -> {share.status_code} {share.text[:300]}")
    data = share.json().get("ocs", {}).get("data", {})
    share_url = (data.get("url") or "").rstrip("/")
    token = data.get("token") or ""
    if not share_url:
        raise RuntimeError(f"Nextcloud share returned no url: {share.text[:300]}")

    # A single-file share exposes the raw file at /public.php/dav/files/<token>
    # (200 + video/mp4, no redirect). If the API gave no token, fall back to
    # the /download link, which 302-redirects - some clients still accept it.
    if not token and "{token}" in c["direct_template"]:
        return share_url + "/download"
    try:
        return c["direct_template"].format(
            url=c["url"], token=token, share=share_url, name=path.name
        )
    except KeyError as exc:
        raise RuntimeError(f"bad NC_DIRECT_TEMPLATE placeholder {exc}") from exc


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------
def public_url(bucket: str, key: str) -> str:
    """Kept for callers that just want to build a URL without uploading."""
    if backend() == "nextcloud":
        return ""
    c = _s3_conf()
    base = c["public_base"] or f"{c['endpoint']}/{c['bucket']}"
    return f"{base}/{urllib.parse.quote(key)}"


def upload(path: pathlib.Path, key: str, content_type: str = "video/mp4") -> str:
    if backend() == "nextcloud":
        return _nc_upload(path, key, content_type)
    return _s3_upload(path, key, content_type)


def status() -> dict:
    """What the dashboard shows on the Storage card."""
    b = backend()
    if b in {"s3", "r2"}:
        c = _s3_conf()
        return {
            "backend": "Cloudflare R2" if b == "r2" else "S3 / MinIO",
            "bucket": c["bucket"],
            "configured": bool(c["endpoint"] and c["key"] and c["secret"]),
            "public": bool(c["public_base"]),
        }
    c = _nc_conf()
    return {
        "backend": "Nextcloud",
        "bucket": c["folder"],
        "configured": bool(c["url"] and c["user"] and c["password"]),
        "public": bool(c["url"]),
    }


def run() -> int:
    job = jobs.start("upload")
    rendered = state.load_rendered()
    done = failed = 0
    date = rendered.get("date", state.today())

    for clip in rendered.get("clips", []):
        if clip.get("stage") not in {"qa_passed", "uploaded"}:
            continue
        if clip.get("r2_url"):
            clip["stage"] = "uploaded"
            done += 1
            continue
        path = pathlib.Path(clip["path"])
        if not path.is_file():
            clip["stage"] = "failed"
            clip["error"] = "local file missing at upload time"
            failed += 1
            continue
        try:
            key = f"{date}/{clip['clip_id']}.mp4"
            clip["r2_url"] = upload(path, key)
            clip["stage"] = "uploaded"
            done += 1
        except Exception as exc:
            clip["stage"] = "failed"
            clip["error"] = str(exc)[:400]
            state.error("upload", f"{clip.get('clip_id')}", reason=str(exc)[:300])
            failed += 1
        state.save_rendered(rendered)

    jobs.finish(job, status="ok" if not failed else "partial", items=done, failed=failed)
    return done


def main(argv: list[str] | None = None) -> int:
    import argparse

    argparse.ArgumentParser(description="Upload clips to public storage").parse_args(argv)
    n = run()
    print(f"uploaded {n} clip(s) via {backend()}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
