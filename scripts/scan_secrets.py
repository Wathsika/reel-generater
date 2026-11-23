#!/usr/bin/env python3
"""Block a commit if a known secret value is about to be published.

Runs in CI right after `git add -A` and before `git commit`. Two layers:

  1. Known-value scan - every secret this workflow can see (API keys, tokens,
     app password, the token store JSON) is searched for verbatim in the
     staged files. Catches a future code change that writes a token into a
     state file.
  2. Pattern scan - well-known credential shapes (ghp_, github_pat_, sk-,
     AKIA..., private keys) are caught even when the value is not in this
     run's environment.

Exit 1 on any hit, so nothing is committed. The value itself is never
printed - only the file, the variable name, and the line number.

Usage:
    python scripts/scan_secrets.py            # scan staged files (CI)
    python scripts/scan_secrets.py --all      # scan the working tree
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys

# Environment variables that hold real credentials (not URLs/buckets/names).
SECRET_ENV = [
    "GROQ_API_KEY",
    "GEMINI_API_KEY",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
    "NC_APP_PASSWORD",
    "IG_ACCESS_TOKEN",
    "IG_APP_SECRET",
    "FB_PAGE_TOKEN",
    "TT_CLIENT_KEY",
    "TT_CLIENT_SECRET",
    "SECRETS_ADMIN_PAT",
]

# Never treat these as secrets even if they show up in a secret variable.
BORING = {"true", "false", "null", "none", "clips", "nextcloud", "s3", "r2", "auto"}

PATTERNS = [
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "GitHub fine-grained PAT"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"), "GitHub token"),
    (re.compile(r"gsk_[A-Za-z0-9]{20,}"), "Groq API key"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "OpenAI-style key"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key id"),
    (re.compile(r"AIza[0-9A-Za-z\-_]{35}"), "Google API key"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "Slack token"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
]

MAX_BYTES = 4 * 1024 * 1024
SKIP_DIRS = (".git/", ".venv/", "venv/", "node_modules/", "work/", "out/")


def _leaves(obj):
    """Every string inside a (possibly nested) secret JSON value."""
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _leaves(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _leaves(v)
    elif isinstance(obj, str):
        yield obj


def collect_values() -> dict[str, str]:
    """name -> secret value, ignoring blanks and short/obvious non-secrets."""
    found: dict[str, str] = {}
    for name in SECRET_ENV:
        value = os.environ.get(name) or ""
        if len(value) >= 12 and value.strip().lower() not in BORING:
            found[name] = value

    raw = os.environ.get("STATE_TOKENS") or ""
    if raw:
        try:
            for i, leaf in enumerate(_leaves(json.loads(raw))):
                if len(leaf) >= 12 and leaf.strip().lower() not in BORING:
                    found[f"STATE_TOKENS[{i}]"] = leaf
        except json.JSONDecodeError:
            if len(raw) >= 12:
                found["STATE_TOKENS"] = raw
    return found


def staged_files() -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True, text=True, check=True,
    ).stdout
    return [f for f in out.splitlines() if f.strip()]


def worktree_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        capture_output=True, text=True, check=True,
    ).stdout
    return [f for f in out.splitlines() if f.strip()]


def read_text(path: pathlib.Path) -> str | None:
    if any(part in path.as_posix() for part in SKIP_DIRS):
        return None
    try:
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:8192]:          # binary
        return None
    return data.decode("utf-8", errors="ignore")


def scan(files: list[str], values: dict[str, str]) -> int:
    hits: list[tuple[str, str, int]] = []

    for name in files:
        path = pathlib.Path(name)
        text = read_text(path)
        if text is None:
            continue
        lines = text.splitlines()

        for label, secret in values.items():
            if secret not in text:
                continue
            for lineno, line in enumerate(lines, 1):
                if secret in line:
                    hits.append((name, label, lineno))
                    break

        for pattern, label in PATTERNS:
            for lineno, line in enumerate(lines, 1):
                if pattern.search(line):
                    hits.append((name, label, lineno))

    if not hits:
        print(f"secret scan: clean ({len(files)} file(s) checked, "
              f"{len(values)} known value(s))")
        return 0

    print("SECRET SCAN FAILED - refusing to commit.\n", file=sys.stderr)
    seen = set()
    for name, label, lineno in hits:
        key = (name, label, lineno)
        if key in seen:
            continue
        seen.add(key)
        print(f"  {name}:{lineno}  <- {label}", file=sys.stderr)
    print(
        "\nRemove the value from that file. If it was already pushed, rotate "
        "the credential in GitHub Settings -> Secrets, then purge it from git "
        "history. Values are never printed on purpose.",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fail if a secret is staged for commit")
    ap.add_argument("--all", action="store_true", help="scan the whole working tree")
    args = ap.parse_args(argv)

    try:
        files = worktree_files() if args.all else staged_files()
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"secret scan: could not list files: {exc}", file=sys.stderr)
        return 2

    if not files:
        print("secret scan: nothing staged")
        return 0
    return scan(files, collect_values())


if __name__ == "__main__":
    sys.exit(main())
