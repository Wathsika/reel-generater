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
        # Placeholders: {url} {token} {share} {key} {name}
        "direct_template": config.env(
            "NC_DIRECT_TEMPLATE", "{url}/public.php/dav/files/{token}/{key}"
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


def _nc_quote(text: str) -> str:
    return "/".join(urllib.parse.quote(p, safe="") for p in text.split("/"))


def _nc_share(session, c) -> tuple[str, str]:
    """Reuse (or create) ONE public read-only link share for NC_FOLDER.

    Creating a share per clip would litter Nextcloud with thousands of shares
    and hit its share limits. A single folder share is reused for everything.
    Returns (token, share_url).
    """
    ocs = f"{c['url']}/ocs/v2.php/apps/files_sharing/api/v1/shares"
    path = "/" + c["folder"]

    r = session.get(ocs, params={"path": path, "reshares": "false"}, timeout=60)
    if r.status_code < 400:
        try:
            shares = r.json().get("ocs", {}).get("data") or []
        except ValueError:
            shares = []
        if isinstance(shares, dict):
            shares = [shares]
        for s in shares:
            try:
                if int(s.get("share_type", -1)) != 3:      # 3 = public link
                    continue
                if s.get("password"):
                    continue                               # needs a password - unusable
                if not (int(s.get("permissions", 0)) & 1):  # 1 = read
                    continue
            except (TypeError, ValueError):
                continue
            if s.get("expiration"):
                state.warn("upload", "Nextcloud share expires soon",
                           folder=path, expires=s.get("expiration"))
            return s.get("token") or "", (s.get("url") or "").rstrip("/")

    r = session.post(ocs, data={"path": path, "shareType": 3, "permissions": 1},
                     timeout=120)
    if r.status_code >= 400:
        raise RuntimeError(
            f"Nextcloud could not share /{c['folder']} ({r.status_code}). "
            "Remove the password/expiry from an existing link share of that "
            "folder, or point NC_FOLDER at a fresh folder. " + r.text[:300]
        )
    data = r.json().get("ocs", {}).get("data", {})
    token = data.get("token") or ""
    url = (data.get("url") or "").rstrip("/")
    if not url:
        raise RuntimeError(f"Nextcloud share returned no url: {r.text[:300]}")
    if data.get("expiration"):
        state.warn("upload", "Nextcloud share has an expiry date",
                   folder=path, expires=data.get("expiration"))
    return token, url


def _nc_upload(path: pathlib.Path, key: str, content_type: str) -> str:
    session, c = _nc_session()
    user = urllib.parse.quote(c["user"], safe="")
    dav_root = f"{c['url']}/remote.php/dav/files/{user}"

    parts = (c["folder"] + "/" + key).split("/")
    folder_parts = parts[:-1]
    # create the folder chain: Clips, Clips/<date>
    for i in range(1, len(folder_parts) + 1):
        sub = _nc_quote("/".join(folder_parts[:i]))
        _nc_mkcol(session, f"{dav_root}/{sub}")

    file_url = dav_root + "/" + _nc_quote("/".join(parts))
    with open(path, "rb") as fh:
        put = session.put(file_url, data=fh, headers={"Content-Type": content_type},
                          timeout=1800)
    if put.status_code not in (201, 204):
        raise RuntimeError(f"Nextcloud upload -> {put.status_code} {put.text[:200]}")

    token, share_url = _nc_share(session, c)

    # Raw file at /public.php/dav/files/<token>/<key> -> 200 + video/mp4.
    # If the server gave no token, fall back to /download (302, less reliable).
    if not token and "{token}" in c["direct_template"]:
        return share_url + "/download"
    try:
        return c["direct_template"].format(
            url=c["url"],
            token=token,
            share=share_url,
            key=_nc_quote(key),
            name=path.name,
        )
    except KeyError as exc:
        raise RuntimeError(f"bad NC_DIRECT_TEMPLATE placeholder {exc}") from exc


def _nc_delete(key: str) -> None:
    session, c = _nc_session()
    user = urllib.parse.quote(c["user"], safe="")
    url = f"{c['url']}/remote.php/dav/files/{user}/" + _nc_quote(c["folder"] + "/" + key)
    r = session.delete(url, timeout=120)
    if r.status_code not in (200, 204, 404):
        raise RuntimeError(f"Nextcloud delete {key} -> {r.status_code}")


def _nc_list(prefix: str = "") -> list[tuple[str, object]]:
    """Walk NC_FOLDER (2 levels: date folders -> files) via PROPFIND."""
    import email.utils
    import xml.etree.ElementTree as ET

    session, c = _nc_session()
    user = urllib.parse.quote(c["user"], safe="")
    base_path = f"/remote.php/dav/files/{c['user']}/{c['folder']}".rstrip("/")
    root = f"{c['url']}/remote.php/dav/files/{user}/{_nc_quote(c['folder'])}"

    def propfind(url: str, depth: int):
        r = session.request("PROPFIND", url, headers={"Depth": str(depth)}, timeout=120)
        if r.status_code not in (207, 200):
            raise RuntimeError(f"Nextcloud PROPFIND -> {r.status_code}")
        return ET.fromstring(r.content)

    def parse(xml, folder_url: str):
        out = []
        for resp in xml.findall("{DAV:}response"):
            href = resp.findtext("{DAV:}href") or ""
            full = urllib.parse.unquote(href)
            if not full.startswith(base_path + "/"):
                continue
            rel = full[len(base_path) + 1:]
            if rel.endswith("/"):
                rel = rel.rstrip("/")
            is_dir = resp.find(".//{DAV:}collection") is not None or full.endswith("/")
            lm = resp.findtext(".//{DAV:}getlastmodified")
            when = None
            if lm:
                try:
                    when = email.utils.parsedate_to_datetime(lm)
                except (TypeError, ValueError):
                    when = None
            out.append((rel, is_dir, when, folder_url))
        return out

    objs: list[tuple[str, object]] = []
    seen: set[str] = set()

    def add(rel: str, when) -> None:
        if rel in seen:
            return
        seen.add(rel)
        objs.append((rel, when))

    top = parse(propfind(root, 1), root)
    for rel, is_dir, when, _ in top:
        if not rel:
            continue                      # the folder itself
        if not is_dir:
            if rel.startswith(prefix) and rel.lower().endswith(".mp4"):
                add(rel, when)
            continue
        sub_url = f"{root}/{_nc_quote(rel)}"
        try:
            for srel, sdir, swhen, _ in parse(propfind(sub_url, 1), sub_url):
                if sdir or not srel.lower().endswith(".mp4"):
                    continue
                if srel.startswith(prefix):
                    add(srel, swhen)
        except Exception as exc:  # a single bad folder must not kill the sweep
            state.warn("cleanup", f"could not list {rel}", reason=str(exc)[:200])
    return objs


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


def _s3_list(prefix: str = "") -> list[tuple[str, object]]:
    c = _s3_conf()
    s3 = _s3_client()
    out: list[tuple[str, object]] = []
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=c["bucket"], Prefix=prefix
    ):
        for obj in page.get("Contents", []):
            out.append((obj["Key"], obj.get("LastModified")))
    return out


def list_objects(prefix: str = "") -> list[tuple[str, object]]:
    """[(key, last_modified)] for every clip currently in storage."""
    if backend() == "nextcloud":
        return _nc_list(prefix)
    return _s3_list(prefix)


def delete(key: str) -> None:
    if backend() == "nextcloud":
        return _nc_delete(key)
    c = _s3_conf()
    _s3_client().delete_object(Bucket=c["bucket"], Key=key)


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
        "folder": c["folder"],
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
            clip["storage_key"] = key
            clip["storage_backend"] = backend()
            clip["stage"] = "uploaded"
            done += 1
        except Exception as exc:
            clip["stage"] = "failed"
            clip["error"] = str(exc)[:400]
            state.error("upload", f"{clip.get('clip_id')}", reason=str(exc)[:300])
            failed += 1
        state.save_rendered(rendered)

    if failed and not done:
        state.error("upload", "every upload failed - the storage settings need attention")
        jobs.finish(job, status="failed", items=0, failed=failed)
        return -1

    jobs.finish(job, status="ok" if not failed else "partial", items=done, failed=failed)
    return done


def main(argv: list[str] | None = None) -> int:
    import argparse

    argparse.ArgumentParser(description="Upload clips to public storage").parse_args(argv)
    n = run()
    if n < 0:
        print("upload: NOTHING WAS UPLOADED - see the error log")
        return 1
    print(f"uploaded {n} clip(s) via {backend()}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
