"""Verify your clip storage actually works for Meta / TikTok.

It uploads a tiny probe video, requests the public URL exactly like Meta's
servers would (no cookies, no redirects followed), and reports whether the
response is acceptable: HTTP 200 + `Content-Type: video/mp4` + no redirect.

Run it on your laptop (it reads .env or real environment variables):

    python scripts/check_storage.py            # upload, check, delete probe
    python scripts/check_storage.py --keep     # keep the probe so you can
                                               # paste the URL into Meta's
                                               # Sharing Debugger
    python scripts/check_storage.py --file x.mp4

Exit code 0 = storage is usable. Anything else = do not start the pipeline yet.
"""
from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pipeline import config, storage  # noqa: E402  (config loads .env)


def make_probe(path: pathlib.Path) -> None:
    """1-second black 9:16 clip, or a dummy file if ffmpeg is unavailable."""
    ffmpeg = config.ffmpeg_bin()
    try:
        subprocess.run(
            [
                ffmpeg, "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=black:s=540x960:d=1",
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p", "-c:a", "aac", str(path),
            ],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 200_000)


def probe_url(url: str) -> dict:
    import requests

    # allow_redirects=False mirrors what a strict crawler does.
    r = requests.get(url, timeout=120, stream=True, allow_redirects=False)
    body = next(r.iter_content(1024), b"")
    info = {
        "status": r.status_code,
        "content_type": (r.headers.get("Content-Type") or "").split(";")[0].strip(),
        "content_length": r.headers.get("Content-Length") or "?",
        "location": r.headers.get("Location") or "",
        "first_bytes": len(body),
        "disposition": r.headers.get("Content-Disposition") or "",
    }
    r.close()
    return info


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check clip storage is publicly fetchable")
    ap.add_argument("--file", help="use this mp4 instead of generating a probe")
    ap.add_argument("--keep", action="store_true", help="do not delete the probe afterwards")
    args = ap.parse_args(argv)

    st = storage.status()
    print(f"backend      : {st.get('backend')}")
    print(f"bucket/folder: {st.get('bucket')}")
    print(f"configured   : {st.get('configured')}")
    if not st.get("configured"):
        print("\nFAIL: storage is not configured. Set the storage secrets first.")
        return 2

    tmp: pathlib.Path | None = None
    if args.file:
        src = pathlib.Path(args.file).resolve()
        if not src.is_file():
            print(f"FAIL: {src} not found")
            return 2
    else:
        tmp = pathlib.Path(tempfile.gettempdir()) / f"clip_probe_{int(time.time())}.mp4"
        make_probe(tmp)
        src = tmp

    key = f"_probe/{int(time.time())}.mp4"
    url = ""
    try:
        print(f"\nuploading    : {src.name} -> {key}")
        url = storage.upload(src, key)
        print(f"public url   : {url}")

        print("\nfetching like Meta would (no redirects, no cookies)...")
        info = probe_url(url)
        for k, v in info.items():
            print(f"  {k:14}: {v}")

        problems = []
        if info["status"] != 200:
            problems.append(f"HTTP {info['status']} (must be 200)")
        if not info["content_type"].startswith("video/"):
            problems.append(
                f"Content-Type '{info['content_type']}' (must be video/mp4)"
            )
        if info["location"]:
            problems.append(f"redirects to {info['location']} (must be direct)")
        if info["first_bytes"] <= 0:
            problems.append("empty body")

        if problems:
            print("\nFAIL - Meta/TikTok would reject this URL:")
            for p in problems:
                print(f"  * {p}")
            if "redirects" in " ".join(problems):
                print(
                    "\nNextcloud fix: this is the /download or /s/ link. The "
                    "pipeline uses /public.php/dav/files/<token>/<key>; make sure "
                    "link sharing is enabled and no password is set on the share."
                )
            return 3

        print("\nPASS - this URL is directly fetchable and correctly typed.")
        if args.keep:
            print(f"kept for testing: {url}")
        return 0
    except Exception as exc:
        print(f"\nFAIL: {type(exc).__name__}: {str(exc)[:500]}")
        return 1
    finally:
        if not args.keep and url:
            try:
                storage.delete(key)
                print("probe deleted from storage")
            except Exception as exc:
                print(f"warning: could not delete probe: {str(exc)[:200]}")
        if tmp and tmp.exists():
            tmp.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
