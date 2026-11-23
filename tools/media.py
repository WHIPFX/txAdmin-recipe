"""Publishes WHIP's picture packs to WHIP's CDN (media.whipfx.net), so whip_core can load a picture when a screen
shows it instead of every server installing the packs and every player downloading them on joining.

    python tools/media.py publish [--force] [--dry-run]

For each resource recipe/manifest.toml marks `pictures = true`, at its tag:

- Every file under img/ and video/ goes to media/<its path> in the bucket, but only when the bucket has no copy or a
  different one (R2 gives a file's MD5 as its ETag). Each is cached for a year: whip_core asks for it with
  ?v=<the pack's tag>, so a release is a new address. One copy of each picture, whatever the number of releases.
- Nothing is deleted, so a server on an older whip_core never loses a picture.
- media/index.json then says each pack's tag and its files (cached 60 seconds). whip_core reads it to know which
  pictures exist and which ?v= to ask for.

A pack already at its tag in the index is skipped (--force goes through every pack again). Afterwards a few of the
new files and the index are fetched back through the CDN and checked.

Environment: R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET (the cdn environment's secrets), and
WHIP_CDN_BASE (https://media.whipfx.net) for the check through the CDN.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))
import recipes  # noqa: E402

PREFIX = "media/"
INDEX = PREFIX + "index.json"
DIRS = ("img", "video")
TYPES = {
    ".webp": "image/webp", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".svg": "image/svg+xml", ".webm": "video/webm", ".mp4": "video/mp4",
}
FOREVER = "public, max-age=31536000, immutable"
SHORT = "public, max-age=60"
UA = "WHIP media publisher"


class Store(Protocol):
    def etags(self, prefix: str) -> dict[str, str]: ...
    def get(self, key: str) -> bytes | None: ...
    def put(self, key: str, data: bytes, content_type: str, cache_control: str) -> None: ...


class R2:
    """The bucket, through R2's S3 API."""

    def __init__(self) -> None:
        import boto3
        from botocore.config import Config

        self.bucket = os.environ["R2_BUCKET"]
        self.s3 = boto3.client(
            "s3",
            endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            region_name="auto",
            config=Config(request_checksum_calculation="when_required", response_checksum_validation="when_required",
                          max_pool_connections=32, retries={"max_attempts": 6, "mode": "standard"}),
        )

    def etags(self, prefix: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            for o in page.get("Contents", []):
                out[o["Key"]] = o["ETag"].strip('"')
        return out

    def get(self, key: str) -> bytes | None:
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except self.s3.exceptions.NoSuchKey:
            return None

    def put(self, key: str, data: bytes, content_type: str, cache_control: str) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type, CacheControl=cache_control)


def files_of(root: Path) -> dict[str, Path]:
    """The pack's pictures and videos: 'img/props/x.webp' -> its file."""
    out: dict[str, Path] = {}
    for d in DIRS:
        for f in sorted((root / d).rglob("*")):
            if f.is_file() and f.suffix.lower() in TYPES:
                out[f.relative_to(root).as_posix()] = f
    return out


def grouped(paths: list[str]) -> dict[str, list[str]]:
    """{'img/props': ['a.webp', ...]}: the index lists names by folder, so the folders aren't repeated 60,000 times."""
    out: dict[str, list[str]] = {}
    for p in sorted(paths):
        d, _, name = p.rpartition("/")
        out.setdefault(d, []).append(name)
    return out


def fetch_pack(repo: str, tag: str, into: Path) -> None:
    subprocess.run(["git", "-c", "advice.detachedHead=false", "clone", "--quiet", "--depth", "1", "--branch", tag,
                    f"https://github.com/{repo}.git", str(into)], check=True)


def publish_pack(store: Store, name: str, tag: str, root: Path, have: dict[str, str], dry_run: bool) -> tuple[dict, list[str]]:
    """Upload what differs. Returns the pack's index entry and the paths uploaded."""
    files = files_of(root)
    todo = [(path, f) for path, f in files.items()
            if have.get(PREFIX + path) != hashlib.md5(f.read_bytes()).hexdigest()]
    print(f"{name} {tag}: {len(files)} files, {len(todo)} new or changed")
    if not dry_run and todo:
        def up(item: tuple[str, Path]) -> None:
            path, f = item
            store.put(PREFIX + path, f.read_bytes(), TYPES[f.suffix.lower()], FOREVER)

        with ThreadPoolExecutor(16) as pool:
            for i, _ in enumerate(pool.map(up, todo), 1):
                if i % 2000 == 0:
                    print(f"  {i}/{len(todo)} uploaded")
    return {"version": tag, "files": grouped(list(files))}, [p for p, _ in todo]


def check(base: str, index: dict, uploaded: dict[str, list[str]]) -> list[str]:
    """Fetch the index and a few new files back through the CDN, as a player's game will. Returns what's wrong."""
    def get(url: str) -> tuple[int, str, bytes]:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.headers.get("Content-Type", ""), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type", ""), b""

    problems = []
    status, _, body = get(f"{base}/{INDEX}?check={int(time.time())}")   # a fresh address: not the 60 s cache
    if status != 200:
        problems.append(f"{INDEX}: HTTP {status}")
    else:
        live = json.loads(body)
        for name, entry in index["packs"].items():
            if live.get("packs", {}).get(name, {}).get("version") != entry["version"]:
                problems.append(f"{INDEX} doesn't have {name} {entry['version']}")
    for name, paths in uploaded.items():
        version = index["packs"][name]["version"]
        for path in paths[:: max(1, len(paths) // 5)][:5]:
            status, ctype, _ = get(f"{base}/{PREFIX}{path}?v={version}")
            want = TYPES[Path(path).suffix.lower()]
            if status != 200 or not ctype.startswith(want):
                problems.append(f"{path}: HTTP {status}, {ctype or 'no Content-Type'} (want {want})")
    return problems


def publish(store: Store, m: recipes.Manifest, force: bool, dry_run: bool, workdir: Path, base: str = "") -> int:
    raw = store.get(INDEX)
    index = json.loads(raw) if raw else {"packs": {}}
    packs = [r for r in m.resources if r.pictures]
    due = [r for r in packs if force or index["packs"].get(r.name, {}).get("version") != r.tag]
    for r in packs:
        if r not in due:
            print(f"{r.name} {r.tag}: already on the CDN")
    if not due:
        print("Nothing to publish.")
        return 0

    have = store.etags(PREFIX)
    uploaded: dict[str, list[str]] = {}
    for r in due:
        root = workdir / r.name
        if not root.exists():
            fetch_pack(r.repo, r.tag, root)
        index["packs"][r.name], uploaded[r.name] = publish_pack(store, r.name, r.tag, root, have, dry_run)

    index["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if dry_run:
        print("Dry run: nothing uploaded, the index left as it was.")
        return 0
    store.put(INDEX, json.dumps(index, separators=(",", ":")).encode(), "application/json", SHORT)
    listed = ", ".join(f"{n} {e['version']}" for n, e in sorted(index["packs"].items()))
    print(f"{INDEX}: {listed}")

    if base:
        problems = check(base, index, uploaded)
        for p in problems:
            print(f"FAIL {p}")
        if problems:
            return 1
        print(f"Checked through {base}.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    pub = sub.add_parser("publish", help="publish every picture pack not yet on the CDN at its manifest tag")
    pub.add_argument("--force", action="store_true", help="go through every pack, even one already at its tag")
    pub.add_argument("--dry-run", action="store_true", help="say what would upload, change nothing")
    args = p.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        return publish(R2(), recipes.load_manifest(), args.force, args.dry_run, Path(tmp),
                       os.environ.get("WHIP_CDN_BASE", "").rstrip("/"))


if __name__ == "__main__":
    sys.exit(main())
