"""Publishes the WHIP recipes, and every file they download, to WHIP's CDN: a Cloudflare R2 bucket behind a custom
domain. CI runs it (.github/workflows/cdn.yml), one step per command, and each step only runs if the one before passed:

    build     zip each resource at its tag in recipe/manifest.toml, each zip at most max_part_mib. A resource whose
              build is already in the bucket (releases/<name>/<tag>/index.json) is not built again.
    upload    upload the new builds to releases/<name>/<tag>/. Those keys are immutable: an upload never replaces an
              object that is already there, and fails if what is there is different.
    verify    download every zip back through the public CDN URL, as txAdmin will, and check its size, sha256,
              headers, and that it finishes within txAdmin's 150 seconds at min_mbit.
    render    write the CDN recipes for a release into dist/recipes/, from verified builds only (promote does this
              too; CI runs it first to check them with txAdmin's parser rules).
    promote   make the CDN recipes, publish them under recipes/<release>/ and check them through the CDN, then copy
              them to the stable recipes/*.yaml, record the release in recipes/releases.json and purge the cache. If
              the stable recipes don't check out, it goes back to the release before.
    rollback  point recipes/*.yaml at an earlier release from releases.json (a copy, nothing is rebuilt), and check.
    releases  list the releases in releases.json.
    serve     serve a dir: store over HTTP the way the CDN serves the bucket, to try all of this on one computer.

A store is "s3:<bucket>" (R2's S3 API through the aws CLI; the endpoint comes from --endpoint or R2_ACCOUNT_ID, the
keys from R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY) or "dir:<folder>" (a local folder, for testing).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))  # python -I leaves this folder off the path
import recipes  # noqa: E402
import release_zips  # noqa: E402

IMMUTABLE = "public, max-age=31536000, immutable"
SHORT = "public, max-age=60"  # the stable recipes and releases.json: a rollback shows within a minute, even unpurged
ZIP_TYPE = "application/zip"
YAML_TYPE = "text/plain; charset=utf-8"  # txAdmin only reads the text; text/plain also shows in a browser
JSON_TYPE = "application/json"
DOWNLOAD_SECONDS = 150  # txAdmin's limit for one download_file (got's request timeout), which a recipe can't change
BUDGET = 0.9  # a part must finish in this share of DOWNLOAD_SECONDS at min_mbit, for TLS, latency and retries
RECIPE_SECONDS = 4.5  # txAdmin gives up loading a recipe URL after this
RECIPE_MAX_AGE = 300  # the longest Cache-Control max-age the stable recipes may have, or a rollback is slow to show
USER_AGENT = "txAdmin 8 (WHIP CDN check)"  # txAdmin's requests say "txAdmin <version>": a rule blocking it shows here
SAFE = re.compile(r"^[A-Za-z0-9._-]+$")
RELEASES = "recipes/releases.json"


class StoreError(RuntimeError):
    pass


class Exists(StoreError):
    """A create-only write found something at the key."""


@dataclass(frozen=True)
class Meta:
    size: int
    sha256: str


class Store(Protocol):
    def head(self, key: str) -> Meta | None: ...
    def get(self, key: str) -> bytes | None: ...
    def put(self, key: str, body: Path, content_type: str, cache_control: str, sha256: str, overwrite: bool) -> None: ...


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


class DirStore:
    """A folder that acts like the bucket. Each object's headers are kept in .meta/<key>.json."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        if key.startswith("/") or ".." in key.split("/"):
            raise StoreError(f"bad key {key}")
        return self.root / key

    def meta(self, key: str) -> dict | None:
        path = self.root / ".meta" / f"{key}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def head(self, key: str) -> Meta | None:
        meta = self.meta(key)
        return Meta(meta["size"], meta["sha256"]) if meta and self._path(key).exists() else None

    def get(self, key: str) -> bytes | None:
        path = self._path(key)
        return path.read_bytes() if path.exists() else None

    def put(self, key: str, body: Path, content_type: str, cache_control: str, sha256: str, overwrite: bool) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = body.read_bytes()
        try:
            with path.open("wb" if overwrite else "xb") as f:
                f.write(data)
        except FileExistsError:
            raise Exists(key) from None
        meta = self.root / ".meta" / f"{key}.json"
        meta.parent.mkdir(parents=True, exist_ok=True)
        meta.write_text(json.dumps({"size": len(data), "sha256": sha256, "content_type": content_type,
                                    "cache_control": cache_control}))


class S3Store:
    """An R2 bucket through the aws CLI. The keys only ever reach the CLI through its environment."""

    def __init__(self, bucket: str, endpoint: str) -> None:
        self.bucket, self.endpoint = bucket, endpoint
        self.env = dict(os.environ)
        for aws, r2 in (("AWS_ACCESS_KEY_ID", "R2_ACCESS_KEY_ID"), ("AWS_SECRET_ACCESS_KEY", "R2_SECRET_ACCESS_KEY")):
            if os.environ.get(r2):
                self.env[aws] = os.environ[r2]
        self.env.setdefault("AWS_DEFAULT_REGION", "auto")  # R2 has one region, "auto"
        # Newer aws CLIs add checksums R2 doesn't take on every request: only send them when an API needs one
        self.env.setdefault("AWS_REQUEST_CHECKSUM_CALCULATION", "when_required")
        self.env.setdefault("AWS_RESPONSE_CHECKSUM_VALIDATION", "when_required")

    def aws(self, *args: str) -> subprocess.CompletedProcess[str]:
        cmd = ["aws", "s3api", *args, "--bucket", self.bucket, "--endpoint-url", self.endpoint, "--output", "json"]
        return subprocess.run(cmd, capture_output=True, text=True, env=self.env)

    @staticmethod
    def missing(r: subprocess.CompletedProcess[str]) -> bool:
        return r.returncode != 0 and any(s in r.stderr for s in ("(404)", "Not Found", "NoSuchKey"))

    def head(self, key: str) -> Meta | None:
        r = self.aws("head-object", "--key", key)
        if self.missing(r):
            return None
        if r.returncode:
            raise StoreError(f"head {key}: {r.stderr.strip()}")
        out = json.loads(r.stdout)
        return Meta(int(out["ContentLength"]), out.get("Metadata", {}).get("sha256", ""))

    def get(self, key: str) -> bytes | None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "object"
            r = self.aws("get-object", "--key", key, str(out))
            if self.missing(r):
                return None
            if r.returncode:
                raise StoreError(f"get {key}: {r.stderr.strip()}")
            return out.read_bytes()

    def put(self, key: str, body: Path, content_type: str, cache_control: str, sha256: str, overwrite: bool) -> None:
        args = ["put-object", "--key", key, "--body", str(body), "--content-type", content_type,
                "--cache-control", cache_control, "--metadata", f"sha256={sha256}"]
        if not overwrite:
            args += ["--if-none-match", "*"]  # R2 commits it only if the key is free, atomically
        r = self.aws(*args)
        if r.returncode and ("PreconditionFailed" in r.stderr or "(412)" in r.stderr):
            raise Exists(key)
        if r.returncode:
            raise StoreError(f"put {key}: {r.stderr.strip()}")


def open_store(spec: str, endpoint: str = "") -> Store:
    kind, _, where = spec.partition(":")
    if kind == "dir" and where:
        return DirStore(Path(where))
    if kind == "s3" and where:
        account = os.environ.get("R2_ACCOUNT_ID", "")
        endpoint = endpoint or (f"https://{account}.r2.cloudflarestorage.com" if account else "")
        if not endpoint:
            raise SystemExit("an s3: store needs --endpoint or R2_ACCOUNT_ID")
        return S3Store(where, endpoint)
    raise SystemExit(f"unknown store {spec!r}: use s3:<bucket> or dir:<folder>")


def put_bytes(store: Store, key: str, data: bytes, content_type: str, cache_control: str, overwrite: bool) -> None:
    """overwrite=False is a create-only put_new; True replaces what is there (only the stable recipe paths)."""
    with tempfile.TemporaryDirectory() as tmp:
        body = Path(tmp) / "body"
        body.write_bytes(data)
        sha = hashlib.sha256(data).hexdigest()
        if overwrite:
            store.put(key, body, content_type, cache_control, sha, overwrite=True)
        else:
            put_new(store, key, body, content_type, cache_control, sha)


def put_new(store: Store, key: str, body: Path, content_type: str, cache_control: str, sha256: str) -> bool:
    """Create-only upload. True if uploaded, False if the same file was already there; fails if a different one is."""
    size = body.stat().st_size

    def same() -> bool:
        there = store.head(key)
        if there is None:
            return False
        if there != Meta(size, sha256):
            raise StoreError(f"{key} is already in the bucket with different content ({there.size} bytes, sha256 "
                             f"{there.sha256 or 'unknown'}): refusing to replace it. Release a new tag instead.")
        return True

    if same():
        return False
    try:
        store.put(key, body, content_type, cache_control, sha256, overwrite=False)
    except Exists:
        if not same():  # it went again between the two calls
            raise
        return False
    return True


# ---- Messages -------------------------------------------------------------------------------------------------------
def say(msg: str) -> None:
    print(msg, flush=True)


def notice(msg: str, level: str = "notice") -> None:
    print(f"::{level}::{msg}" if os.environ.get("GITHUB_ACTIONS") else f"{level}: {msg}", flush=True)


def fail(msg: str) -> SystemExit:
    notice(msg, "error")
    return SystemExit(1)


# ---- build ----------------------------------------------------------------------------------------------------------
def prefix(name: str, tag: str) -> str:
    if not SAFE.match(name) or not SAFE.match(tag):
        raise ValueError(f"{name} {tag}: names and tags may only use letters, digits, '.', '_' and '-'")
    return f"releases/{name}/{tag}/"


def rmtree(path: Path) -> None:
    def retry(fn: Callable, p: str, *_: object) -> None:  # git makes its objects read-only, which stops Windows
        os.chmod(p, 0o666)
        fn(p)
    shutil.rmtree(path, **({"onexc": retry} if sys.version_info >= (3, 12) else {"onerror": retry}))


def pack_repo(url: str, tag: str, name: str, out: Path, max_bytes: int) -> list[Path]:
    """The same zips release_zips.py makes: every file the tag tracks, split by size. The same files give the same
    zips on the same system, but another zlib or OS gives other bytes, so each part's key has its sha256 in it."""
    work = Path(tempfile.mkdtemp(prefix=f"{name}-"))
    try:
        src = work / "src"
        release_zips.run("git", "clone", "--quiet", "--no-checkout", "--depth", "1", "--branch", tag, url, str(src))
        files = []
        for entry in filter(None, release_zips.run("git", "ls-tree", "-r", "-l", "-z", "HEAD", cwd=src).split("\0")):
            meta, path = entry.split("\t", 1)
            _, kind, sha, size = meta.split()
            if kind == "blob":
                files.append((path, sha, int(size)))
        blobs = release_zips.Blobs(src)
        paths = []
        try:
            for i, part in enumerate(release_zips.split(files, max_bytes), 1):
                paths.append(out / f"{name}-{i}.zip")
                release_zips.write_zip(str(paths[-1]), blobs, part)
        finally:
            blobs.close()
        return paths
    finally:
        rmtree(work)


def build(m: recipes.Manifest, store: Store, dist: Path, only: set[str], tag_override: str,
          git_base: str, asset_base: str) -> None:
    (dist / "index").mkdir(parents=True, exist_ok=True)
    max_bytes = int(m.max_part_mib * 1024 * 1024)
    for r in m.resources:
        if only and r.name not in only:
            continue
        tag = tag_override or r.tag
        key = prefix(r.name, tag) + "index.json"
        there = store.get(key)
        if there is not None:
            (dist / "index" / f"{r.name}.json").write_bytes(there)
            say(f"{r.name} {tag}: already in the bucket, not built again")
            continue
        out = dist / "zips" / r.name / tag
        out.mkdir(parents=True, exist_ok=True)
        if r.github == "release-asset":  # a release build (oxmysql's), mirrored as it is
            paths = [out / r.asset]
            with urllib.request.urlopen(f"{asset_base}/{r.repo}/releases/download/{tag}/{r.asset}", timeout=300) as resp:
                paths[0].write_bytes(resp.read())
        else:
            paths = pack_repo(f"{git_base}/{r.repo}.git", tag, r.name, out, max_bytes)
        parts = []
        for p in paths:
            sha = sha256_file(p)
            # releases/<name>/<tag>/<sha256, 16 digits>/<zip>: a rebuild that comes out different never meets the old
            parts.append({"name": p.name, "key": f"{prefix(r.name, tag)}{sha[:16]}/{p.name}", "size": p.stat().st_size,
                          "sha256": sha})
        index = {"resource": r.name, "repo": r.repo, "tag": tag, "max_part_bytes": max_bytes, "parts": parts,
                 "built": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        (dist / "index" / f"{r.name}.json").write_text(json.dumps(index, indent=1))
        (out / "index.json").write_text(json.dumps(index, indent=1))
        say(f"{r.name} {tag}: built {len(parts)} zip(s), {sum(p['size'] for p in parts) / 1048576:.1f} MiB")


def upload(store: Store, dist: Path) -> None:
    for built in sorted((dist / "zips").glob("*/*/index.json")):
        index = json.loads(built.read_text())
        for part in index["parts"]:
            new = put_new(store, part["key"], built.parent / part["name"], ZIP_TYPE, IMMUTABLE, part["sha256"])
            say(f"{'uploaded' if new else 'already there'}  {part['key']}  {part['size'] / 1048576:.1f} MiB")
        # The index goes last: once it's there, every part is. If another run's build of the same tag got there
        # first, that one stands, and this run's recipes use it: an index is never replaced.
        key = prefix(index["resource"], index["tag"]) + "index.json"
        try:
            new = put_new(store, key, built, JSON_TYPE, IMMUTABLE, sha256_file(built))
            say(f"{'uploaded' if new else 'already there'}  {key}")
        except StoreError:
            there = store.get(key)
            if there is None:
                raise
            (dist / "index" / f"{index['resource']}.json").write_bytes(there)
            notice(f"{key} was uploaded by another run while this one built: using that build (this run's zips for "
                   f"{index['resource']} {index['tag']} are left unused)", "warning")


# ---- verify ---------------------------------------------------------------------------------------------------------
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None  # a redirect is an error here (a login page, a moved bucket): urllib raises it as HTTPError


@dataclass
class Fetched:
    status: int
    headers: dict[str, str]
    size: int
    sha256: str
    seconds: float
    body: bytes = b""


def fetch(url: str, keep: bool = False, timeout: float = DOWNLOAD_SECONDS) -> Fetched:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    opener = urllib.request.build_opener(NoRedirect)
    start = time.monotonic()
    try:
        resp = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        return Fetched(e.code, {k.lower(): v for k, v in e.headers.items()}, 0, "", time.monotonic() - start)
    with resp:
        h, size, kept = hashlib.sha256(), 0, []
        while chunk := resp.read(1 << 20):
            h.update(chunk)
            size += len(chunk)
            if keep:
                kept.append(chunk)
        return Fetched(resp.status, {k.lower(): v for k, v in resp.headers.items()}, size, h.hexdigest(),
                       time.monotonic() - start, b"".join(kept))


def max_age(cache_control: str) -> int | None:
    found = re.search(r"(?:^|[,\s])(?:s-)?max-age=(\d+)", cache_control)
    return int(found.group(1)) if found else None


def check(url: str, size: int, sha256: str, kind: str, min_mbit: float, got: Fetched | None = None) -> list[str]:
    """What is wrong with the object at url (nothing: []). kind is zip, recipe (stable) or pinned (a release's copy)."""
    got = got or fetch(url)
    problems = []
    if 300 <= got.status < 400:
        return [f"redirects ({got.status}) to {got.headers.get('location', '?')}: txAdmin must get the file itself"]
    if got.status != 200:
        return [f"HTTP {got.status}"]
    if "cf-mitigated" in got.headers:
        problems.append(f"Cloudflare challenged the request (cf-mitigated: {got.headers['cf-mitigated']})")
    ctype = got.headers.get("content-type", "")
    if "text/html" in ctype:
        problems.append(f"content-type is {ctype}: a web page, not the file")
    elif kind == "zip" and not ctype.startswith(ZIP_TYPE):
        problems.append(f"content-type is {ctype or 'missing'}, not {ZIP_TYPE}")
    elif kind != "zip" and not ctype.startswith("text/"):
        problems.append(f"content-type is {ctype or 'missing'}, not text")
    if "content-length" in got.headers and int(got.headers["content-length"]) != size:
        problems.append(f"content-length is {got.headers['content-length']}, not {size}")
    if got.size != size or got.sha256 != sha256:
        problems.append(f"got {got.size} bytes with sha256 {got.sha256}, not {size} with {sha256}")
    cache = got.headers.get("cache-control", "")
    if kind == "recipe" and (max_age(cache) is None or max_age(cache) > RECIPE_MAX_AGE):
        problems.append(f"cache-control is {cache or 'missing'}: the stable recipes need max-age {RECIPE_MAX_AGE} or less")
    if kind == "zip":
        need = size * 8 / (min_mbit * 1_000_000)
        if need > DOWNLOAD_SECONDS * BUDGET:
            problems.append(f"needs {need:.0f} s at {min_mbit:g} Mbit/s, over {DOWNLOAD_SECONDS * BUDGET:.0f} s "
                            f"({BUDGET:.0%} of txAdmin's {DOWNLOAD_SECONDS} s): make the parts smaller")
        if got.seconds > DOWNLOAD_SECONDS:
            problems.append(f"took {got.seconds:.0f} s to download here, over txAdmin's {DOWNLOAD_SECONDS} s")
    elif got.seconds > RECIPE_SECONDS:
        problems.append(f"took {got.seconds:.1f} s: txAdmin gives up loading a recipe after {RECIPE_SECONDS} s")
    return problems


def report(url: str, size: int, got: Fetched, min_mbit: float, problems: list[str]) -> None:
    speed = size * 8 / max(got.seconds, 1e-6) / 1_000_000
    at_min = size * 8 / (min_mbit * 1_000_000)
    say(f"{'ok  ' if not problems else 'FAIL'}  {url}  {size / 1048576:.1f} MiB in {got.seconds:.2f} s "
        f"({speed:,.0f} Mbit/s here; {at_min:.0f} s at {min_mbit:g} Mbit/s)")
    for p in problems:
        say(f"        {p}")


def verify(m: recipes.Manifest, dist: Path, base: str, only: set[str]) -> None:
    verified = {}
    failures = 0
    for path in sorted((dist / "index").glob("*.json")):
        index = json.loads(path.read_text())
        if only and index["resource"] not in only:
            continue
        for part in index["parts"]:
            url = f"{base}/{part['key']}"
            got = fetch(url)
            problems = check(url, part["size"], part["sha256"], "zip", m.min_mbit, got)
            report(url, part["size"], got, m.min_mbit, problems)
            failures += bool(problems)
            if not problems:
                verified[part["key"]] = part["sha256"]
    if failures:
        raise fail(f"{failures} file(s) failed the check through {base}: nothing was promoted")
    (dist / "verified.json").write_text(json.dumps({"base": base, "objects": verified}, indent=1))
    say(f"All {len(verified)} file(s) check out through {base}.")


# ---- promote and rollback -------------------------------------------------------------------------------------------
def load_releases(store: Store) -> dict:
    raw = store.get(RELEASES)
    return json.loads(raw) if raw else {"current": None, "releases": [], "history": []}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def purge(urls: list[str]) -> None:
    token, zone = os.environ.get("CF_API_TOKEN", ""), os.environ.get("CF_ZONE_ID", "")
    if not token or not zone:
        notice(f"CF_API_TOKEN or CF_ZONE_ID is not set, so the cache was not purged: the stable recipes have "
               f"'{SHORT}', so every edge has the new ones within a minute.")
        return
    req = urllib.request.Request(f"https://api.cloudflare.com/client/v4/zones/{zone}/purge_cache", method="POST",
                                 data=json.dumps({"files": urls}).encode(),
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            ok = json.loads(resp.read()).get("success")
    except urllib.error.HTTPError as e:
        ok, resp_text = False, e.read().decode(errors="replace")[:300]
        notice(f"Cloudflare's purge API answered {e.code}: {resp_text}", "warning")
    if not ok:
        notice("The cache purge failed: the stable recipes still change within a minute (their max-age).", "warning")
    else:
        say(f"Purged {len(urls)} URL(s) from Cloudflare's cache")


def settle(urls: dict[str, Meta], min_mbit: float, wait: float) -> list[str]:
    """Check the stable recipes until they match, for up to wait seconds (an unpurged edge can hold the old ones)."""
    deadline = time.monotonic() + wait
    while True:
        problems = []
        for url, meta in urls.items():
            got = fetch(url, timeout=30)
            found = check(url, meta.size, meta.sha256, "recipe", min_mbit, got)
            problems += [f"{url}: {p}" for p in found]
        if not problems or time.monotonic() > deadline:
            return problems
        say(f"  not there yet ({len(problems)} problem(s)), checking again in 10 s")
        time.sleep(10)


def point_stable(store: Store, files: dict[str, bytes]) -> None:
    for name, data in files.items():
        put_bytes(store, f"recipes/{name}", data, YAML_TYPE, SHORT, overwrite=True)
        say(f"recipes/{name} now has {hashlib.sha256(data).hexdigest()[:12]}")


def save_releases(store: Store, releases: dict) -> None:
    put_bytes(store, RELEASES, json.dumps(releases, indent=1).encode(), JSON_TYPE, SHORT, overwrite=True)


def switch(store: Store, base: str, releases: dict, to: str, action: str, min_mbit: float, wait: float) -> list[str]:
    """Point the stable recipes at release `to` (already in the bucket), record it, purge, and check through the CDN."""
    entry = next((r for r in releases["releases"] if r["id"] == to), None)
    if entry is None:
        raise fail(f"there is no release {to}. Releases: {', '.join(r['id'] for r in releases['releases']) or 'none'}")
    files = {}
    for name, meta in entry["recipes"].items():
        data = store.get(f"recipes/{to}/{name}")
        if data is None or hashlib.sha256(data).hexdigest() != meta["sha256"]:
            raise fail(f"recipes/{to}/{name} is missing or changed: not switching to {to}")
        files[name] = data
    point_stable(store, files)
    releases["history"].append({"time": now(), "action": action, "from": releases["current"], "to": to})
    releases["current"] = to
    save_releases(store, releases)
    purge([f"{base}/recipes/{name}" for name in files] + [f"{base}/{RELEASES}"])
    return settle({f"{base}/recipes/{n}": Meta(m["size"], m["sha256"]) for n, m in entry["recipes"].items()},
                  min_mbit, wait)


def is_stale(commit: str, current_commit: str, repo: Path) -> bool:
    """Is this run's commit older than the one the CDN already serves? (Runs can finish out of order.)"""
    if not commit or not current_commit or commit == current_commit:
        return False
    r = subprocess.run(["git", "merge-base", "--is-ancestor", commit, current_commit], cwd=repo, capture_output=True)
    return r.returncode == 0


def render(m: recipes.Manifest, dist: Path, base: str, release_id: str) -> dict[str, bytes]:
    """The CDN recipes for this release, from the verified builds. Also written to dist/recipes/, for CI to check."""
    if not SAFE.match(release_id):
        raise fail(f"bad release id {release_id}")
    verified_path = dist / "verified.json"
    verified = json.loads(verified_path.read_text())["objects"] if verified_path.exists() else {}
    indexes = {}
    for r in m.resources:
        path = dist / "index" / f"{r.name}.json"
        if not path.exists():
            raise fail(f"no build of {r.name}: run build, upload and verify first")
        indexes[r.name] = json.loads(path.read_text())
        for part in indexes[r.name]["parts"]:
            if verified.get(part["key"]) != part["sha256"]:
                raise fail(f"{part['key']} has not passed verify: nothing was promoted")

    files = {name: text.encode() for name, text in recipes.render_all(
        m, "cdn", indexes=indexes, base=base, release_id=release_id).items()}
    for name, data in files.items():  # every download in every recipe is a file verify passed
        for url in re.findall(r"^    url: (\S+)$", data.decode(), re.M):
            if verified.get(url.removeprefix(f"{base}/")) is None:
                raise fail(f"{name} downloads {url}, which verify did not check")
    (dist / "recipes").mkdir(exist_ok=True)
    for name, data in files.items():
        (dist / "recipes" / name).write_bytes(data)
    return files


def promote(m: recipes.Manifest, store: Store, dist: Path, base: str, release_id: str, commit: str, repo: Path,
            wait: float) -> None:
    files = render(m, dist, base, release_id)
    releases = load_releases(store)
    if any(r["id"] == release_id for r in releases["releases"]):
        raise fail(f"release {release_id} already exists")
    current = next((r for r in releases["releases"] if r["id"] == releases["current"]), None)
    if current and is_stale(commit, current.get("commit", ""), repo):
        notice(f"The CDN already serves {current['id']}, made from a newer commit ({current['commit'][:7]}): not "
               "promoting this older one.")
        return
    indexes = {r.name: json.loads((dist / "index" / f"{r.name}.json").read_text()) for r in m.resources}
    entry = {"id": release_id, "time": now(), "commit": commit, "base": base,
             "resources": {r.name: {"tag": indexes[r.name]["tag"], "parts": [
                 {k: p[k] for k in ("key", "size", "sha256")} for p in indexes[r.name]["parts"]]} for r in m.resources},
             "recipes": {name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                         for name, data in files.items()}}

    # 1. The release's own copy: immutable, and checked through the CDN before anyone is pointed at it
    for name, data in files.items():
        put_bytes(store, f"recipes/{release_id}/{name}", data, YAML_TYPE, IMMUTABLE, overwrite=False)
    put_bytes(store, f"recipes/{release_id}/release.json", json.dumps(entry, indent=1).encode(), JSON_TYPE,
              IMMUTABLE, overwrite=False)
    problems = []
    for name, meta in entry["recipes"].items():
        url = f"{base}/recipes/{release_id}/{name}"
        got = fetch(url, timeout=30)
        found = check(url, meta["size"], meta["sha256"], "pinned", m.min_mbit, got)
        report(url, meta["size"], got, m.min_mbit, found)
        problems += found
    if problems:
        raise fail(f"release {release_id}'s recipes don't check out through {base}: the stable recipes were not changed")

    # 2. Point the stable recipes at it; if they don't check out, go back
    releases["releases"].append(entry)
    previous = releases["current"]
    problems = switch(store, base, releases, release_id, "promote", m.min_mbit, wait)
    if problems:
        for p in problems:
            say(f"FAIL  {p}")
        if previous:
            notice(f"The stable recipes didn't check out: going back to {previous}", "warning")
            back = switch(store, base, releases, previous, "auto-rollback", m.min_mbit, wait)
            if back:
                raise fail(f"the rollback to {previous} doesn't check out either: {back[0]}")
        raise fail(f"release {release_id} failed its last check" + (f", so the CDN is back on {previous}" if previous else ""))
    say(f"Release {release_id} is live: {base}/recipes/<recipe>.yaml")


def rollback(store: Store, base: str, to: str, min_mbit: float, wait: float) -> None:
    releases = load_releases(store)
    if releases["current"] == to:
        notice(f"{to} is already the current release: checking it again")
    problems = switch(store, base, releases, to, "rollback", min_mbit, wait)
    if problems:
        for p in problems:
            say(f"FAIL  {p}")
        raise fail(f"the stable recipes point at {to} but don't check out through {base}")
    say(f"The stable recipes are back on {to}")


def list_releases(store: Store) -> None:
    releases = load_releases(store)
    for r in releases["releases"]:
        tags = ", ".join(f"{name} {v['tag']}" for name, v in r["resources"].items())
        say(f"{'*' if r['id'] == releases['current'] else ' '} {r['id']}  {r['time']}  {r['commit'][:7]}  {tags}")
    if not releases["releases"]:
        say("No releases yet")


# ---- serve (testing) ------------------------------------------------------------------------------------------------
def make_server(root: Path, port: int) -> http.server.ThreadingHTTPServer:
    store = DirStore(root)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            key = self.path.split("?", 1)[0].lstrip("/")
            meta = store.meta(key) if key and ".." not in key.split("/") else None
            data = store.get(key) if meta else None
            if data is None or meta is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", meta["content_type"])
            self.send_header("Cache-Control", meta["cache_control"])
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: object) -> None:
            pass

    return http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)


# ---- main -----------------------------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["build", "upload", "verify", "render", "promote", "rollback", "releases", "serve"])
    ap.add_argument("--manifest", type=Path, default=recipes.MANIFEST)
    ap.add_argument("--store", default="", help="s3:<bucket> or dir:<folder>")
    ap.add_argument("--endpoint", default="", help="the S3 API endpoint (default: R2's, from R2_ACCOUNT_ID)")
    ap.add_argument("--base", default=os.environ.get("WHIP_CDN_BASE", ""), help="the CDN's public URL (WHIP_CDN_BASE)")
    ap.add_argument("--dist", type=Path, default=Path("dist"), help="the work folder the steps share")
    ap.add_argument("--only", default="", help="build, upload and verify only these resources (comma separated)")
    ap.add_argument("--tag", default="", help="with one --only resource: build this tag, not the manifest's")
    ap.add_argument("--release-id", default="", help="promote: the new release's id")
    ap.add_argument("--to", default="", help="rollback: the release to go back to")
    ap.add_argument("--commit", default=os.environ.get("GITHUB_SHA", ""), help="promote: the commit it comes from")
    ap.add_argument("--wait", type=float, default=180, help="how long the stable recipes may take to show, in seconds")
    ap.add_argument("--git-base", default="https://github.com")
    ap.add_argument("--asset-base", default="https://github.com")
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args(argv)

    m = recipes.load_manifest(args.manifest)
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    unknown = only - {r.name for r in m.resources}
    if unknown:
        raise SystemExit(f"not in {args.manifest.name}: {', '.join(sorted(unknown))}")
    if args.tag and len(only) != 1:
        raise SystemExit("--tag needs exactly one --only resource")
    base = args.base.rstrip("/")
    local = ("http://127.0.0.1:", "http://localhost:")  # the serve command and wrangler's local R2, for testing
    if args.command in ("verify", "render", "promote", "rollback") and not base.startswith(("https://", *local)):
        raise SystemExit("--base (or WHIP_CDN_BASE) must be the CDN's https:// URL")
    if args.command == "serve":
        server = make_server(Path(args.store.removeprefix("dir:")), args.port)
        say(f"Serving {args.store} on http://127.0.0.1:{args.port} (Ctrl+C stops it)")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0
    store = open_store(args.store, args.endpoint) if args.command not in ("verify", "render") else None
    if args.command == "build":
        build(m, store, args.dist, only, args.tag, args.git_base.rstrip("/"), args.asset_base.rstrip("/"))
    elif args.command == "upload":
        upload(store, args.dist)
    elif args.command == "verify":
        verify(m, args.dist, base, only)
    elif args.command == "render":
        for name in render(m, args.dist, base, args.release_id):
            say(f"wrote {args.dist / 'recipes' / name}")
    elif args.command == "promote":
        if only:
            raise SystemExit("promote publishes every resource: no --only")
        promote(m, store, args.dist, base, args.release_id, args.commit, Path.cwd(), args.wait)
    elif args.command == "rollback":
        rollback(store, base, args.to, m.min_mbit, args.wait)
    elif args.command == "releases":
        list_releases(store)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except StoreError as e:
        notice(str(e), "error")
        sys.exit(1)
