"""Stage 6: upload rendered clips to Cloudflare R2.

Meta and TikTok both fetch the video from a public URL, so every clip needs
a stable public address. R2 has 10GB free storage and zero egress fees.
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import urllib.parse

from . import config, jobs, state


def _client():
    try:
        import boto3
        from botocore.config import Config as BotoConfig
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("boto3 missing - pip install -r requirements.txt") from exc

    account = config.env("R2_ACCOUNT_ID")
    key = config.env("R2_ACCESS_KEY_ID")
    secret = config.env("R2_SECRET_ACCESS_KEY")
    if not (account and key and secret):
        raise RuntimeError("R2 credentials not set (R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY)")

    return boto3.client(
        "s3",
        endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
        aws_access_key_id=key,
        aws_secret_access_key=secret,
        region_name="auto",
        config=BotoConfig(s3={"addressing_style": "path"}),
    )


def public_url(bucket: str, key: str) -> str:
    base = (config.env("R2_PUBLIC_BASE") or "").rstrip("/")
    if base:
        return f"{base}/{urllib.parse.quote(key)}"
    # R2.dev public domain - must be enabled on the bucket (Settings -> Public Access)
    account = config.env("R2_ACCOUNT_ID", "")
    return f"https://pub-{account}.r2.dev/{urllib.parse.quote(key)}" if account else ""


def upload(path: pathlib.Path, key: str, content_type: str = "video/mp4") -> str:
    s3 = _client()
    bucket = config.env("R2_BUCKET", "clips")
    s3.upload_file(str(path), bucket, key, ExtraArgs={"ContentType": content_type})
    return public_url(bucket, key)


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
    argparse.ArgumentParser(description="Upload clips to R2").parse_args(argv)
    n = run()
    print(f"uploaded {n} clip(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
