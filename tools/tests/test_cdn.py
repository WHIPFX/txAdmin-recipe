"""python -m unittest discover -s tools/tests

The Pipeline test runs build, upload, verify, promote and rollback end to end: against git repos and a release
asset made in a temporary folder, a dir: store, and that store served over HTTP the way the CDN serves the bucket.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cdn  # noqa: E402
import recipes  # noqa: E402

ZIP = {"content-type": "application/zip", "cache-control": cdn.IMMUTABLE}


def fetched(body: bytes, headers: dict[str, str], status: int = 200, seconds: float = 0.1) -> cdn.Fetched:
    return cdn.Fetched(status, headers, len(body), hashlib.sha256(body).hexdigest(), seconds)


class Check(unittest.TestCase):
    body = b"x" * 1000
    sha = hashlib.sha256(body).hexdigest()

    def problems(self, got: cdn.Fetched, kind: str = "zip", size: int = 1000, mbit: float = 3) -> list[str]:
        return cdn.check("https://cdn.example.com/x", size, self.sha, kind, mbit, got)

    def test_a_good_zip_passes(self) -> None:
        self.assertEqual(self.problems(fetched(self.body, ZIP)), [])

    def test_redirects_errors_and_challenges_fail(self) -> None:
        self.assertIn("redirects", self.problems(fetched(b"", {"location": "https://login"}, 302))[0])
        self.assertEqual(self.problems(fetched(b"", {}, 404)), ["HTTP 404"])
        self.assertTrue(any("challenged" in p for p in self.problems(fetched(self.body, {**ZIP, "cf-mitigated": "challenge"}))))

    def test_a_web_page_or_the_wrong_type_fails(self) -> None:
        self.assertTrue(any("web page" in p for p in self.problems(fetched(self.body, {"content-type": "text/html"}))))
        self.assertTrue(any("not application/zip" in p for p in self.problems(fetched(self.body, {"content-type": "application/octet-stream"}))))

    def test_wrong_bytes_fail(self) -> None:
        self.assertTrue(any("sha256" in p for p in self.problems(fetched(b"y" * 1000, ZIP))))
        self.assertTrue(any("content-length" in p for p in self.problems(fetched(self.body, {**ZIP, "content-length": "999"}))))

    def test_a_part_too_big_for_150_seconds_at_min_mbit_fails(self) -> None:
        # 40 MiB at 3 Mbit/s is 112 s: inside 90% of 150 s. At 2 Mbit/s it's 168 s: not
        size = 40 * 1024 * 1024
        big = cdn.Fetched(200, ZIP, size, self.sha, 1)
        self.assertFalse(any("Mbit/s" in p for p in self.problems(big, size=size, mbit=3)))
        self.assertTrue(any("make the parts smaller" in p for p in self.problems(big, size=size, mbit=2)))
        self.assertTrue(any("over txAdmin's 150 s" in p for p in self.problems(fetched(self.body, ZIP, seconds=151))))

    def test_stable_recipes_need_a_short_cache_and_must_load_in_time(self) -> None:
        text = {"content-type": cdn.YAML_TYPE}
        self.assertEqual(self.problems(fetched(self.body, {**text, "cache-control": cdn.SHORT}), "recipe"), [])
        self.assertTrue(self.problems(fetched(self.body, {**text, "cache-control": cdn.IMMUTABLE}), "recipe"))
        self.assertTrue(self.problems(fetched(self.body, text), "recipe"))
        self.assertEqual(self.problems(fetched(self.body, {**text, "cache-control": cdn.IMMUTABLE}), "pinned"), [])
        self.assertTrue(any("4.5 s" in p for p in self.problems(fetched(self.body, {**text, "cache-control": cdn.SHORT}, seconds=5), "recipe")))

    def test_max_age(self) -> None:
        self.assertEqual(cdn.max_age("public, max-age=60"), 60)
        self.assertEqual(cdn.max_age("s-maxage=10"), None)
        self.assertEqual(cdn.max_age("no-store"), None)


class PutNew(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.store = cdn.DirStore(self.tmp / "bucket")
        self.file = self.tmp / "a.zip"
        self.file.write_bytes(b"one")
        self.sha = cdn.sha256_file(self.file)

    def tearDown(self) -> None:
        cdn.rmtree(self.tmp)

    def test_uploads_once_skips_the_same_file_and_refuses_a_different_one(self) -> None:
        self.assertTrue(cdn.put_new(self.store, "releases/a/v1/a-1.zip", self.file, cdn.ZIP_TYPE, cdn.IMMUTABLE, self.sha))
        self.assertFalse(cdn.put_new(self.store, "releases/a/v1/a-1.zip", self.file, cdn.ZIP_TYPE, cdn.IMMUTABLE, self.sha))
        self.file.write_bytes(b"two")
        with self.assertRaisesRegex(cdn.StoreError, "refusing to replace"):
            cdn.put_new(self.store, "releases/a/v1/a-1.zip", self.file, cdn.ZIP_TYPE, cdn.IMMUTABLE, cdn.sha256_file(self.file))
        self.assertEqual(self.store.get("releases/a/v1/a-1.zip"), b"one")

    def test_a_race_the_store_catches_is_checked_again(self) -> None:
        class Racy(cdn.DirStore):
            def put(inner, *args: object, **kwargs: object) -> None:  # noqa: N805
                cdn.DirStore.put(inner, *args, **kwargs)  # someone else's write lands first
                raise cdn.Exists("k")
        self.assertFalse(cdn.put_new(Racy(self.tmp / "racy"), "k", self.file, cdn.ZIP_TYPE, cdn.IMMUTABLE, self.sha))

    def test_bad_keys_and_tags_are_refused(self) -> None:
        with self.assertRaises(cdn.StoreError):
            self.store.get("../outside")
        with self.assertRaises(ValueError):
            cdn.prefix("whip_core", "v1/../../x")


class S3(unittest.TestCase):
    def run_with(self, returncode: int, stderr: str = "", stdout: str = "") -> mock.MagicMock:
        return mock.MagicMock(return_value=subprocess.CompletedProcess([], returncode, stdout, stderr))

    def store(self) -> cdn.S3Store:
        with mock.patch.dict(os.environ, {"R2_ACCESS_KEY_ID": "id", "R2_SECRET_ACCESS_KEY": "secret"}):
            return cdn.S3Store("bucket", "https://acct.r2.cloudflarestorage.com")

    def test_create_only_puts_send_if_none_match_and_keep_keys_out_of_the_command(self) -> None:
        s3 = self.store()
        run = self.run_with(0)
        with mock.patch("subprocess.run", run):
            s3.put("k", Path("f"), cdn.ZIP_TYPE, cdn.IMMUTABLE, "abc", overwrite=False)
        cmd, env = run.call_args.args[0], run.call_args.kwargs["env"]
        self.assertEqual(cmd[cmd.index("--if-none-match") + 1], "*")
        self.assertIn("sha256=abc", cmd)
        self.assertEqual(cmd[cmd.index("--endpoint-url") + 1], "https://acct.r2.cloudflarestorage.com")
        self.assertNotIn("secret", " ".join(cmd))
        self.assertEqual((env["AWS_ACCESS_KEY_ID"], env["AWS_SECRET_ACCESS_KEY"]), ("id", "secret"))
        with mock.patch("subprocess.run", run):
            s3.put("k", Path("f"), cdn.YAML_TYPE, cdn.SHORT, "abc", overwrite=True)
        self.assertNotIn("--if-none-match", run.call_args.args[0])

    def test_412_is_exists_and_404_is_missing(self) -> None:
        s3 = self.store()
        with mock.patch("subprocess.run", self.run_with(254, "An error occurred (PreconditionFailed) ... (412)")):
            with self.assertRaises(cdn.Exists):
                s3.put("k", Path("f"), cdn.ZIP_TYPE, cdn.IMMUTABLE, "abc", overwrite=False)
        with mock.patch("subprocess.run", self.run_with(254, "An error occurred (404) when calling the HeadObject operation: Not Found")):
            self.assertIsNone(s3.head("k"))
        with mock.patch("subprocess.run", self.run_with(0, stdout='{"ContentLength": 3, "Metadata": {"sha256": "abc"}}')):
            self.assertEqual(s3.head("k"), cdn.Meta(3, "abc"))
        with mock.patch("subprocess.run", self.run_with(255, "Could not connect to the endpoint URL")):
            with self.assertRaises(cdn.StoreError):
                s3.head("k")


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=cwd, check=True,
                   capture_output=True)


class Pipeline(unittest.TestCase):
    """build, upload, verify, promote, a second release, rollback, and the failures that must stop it."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.bucket = self.tmp / "bucket"
        self.store = cdn.DirStore(self.bucket)
        self.server = cdn.make_server(self.bucket, 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        # Two resources as git repos, and oxmysql's release build as a file
        self.git_base = self.tmp / "git"
        self.repo("whip_core", {"fxmanifest.lua": b"fx_version 'cerulean'\n"}, "v1.0.0")
        self.repo("whip_media_props", {f"img/{i}.webp": os.urandom(30_000) for i in range(6)}, "v1.0.0")
        asset = self.tmp / "assets" / "WHIPFX" / "oxmysql" / "releases" / "download" / "v2" / "oxmysql.zip"
        asset.parent.mkdir(parents=True)
        with zipfile.ZipFile(asset, "w") as z:
            z.writestr("oxmysql/fxmanifest.lua", "fx_version 'cerulean'\n")
        self.asset_base = asset.parents[5].as_uri()
        self.manifest = self.write_manifest("v1.0.0")

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        cdn.rmtree(self.tmp)

    def repo(self, name: str, files: dict[str, bytes], tag: str) -> None:
        work = self.tmp / "work" / name
        if not work.exists():
            work.mkdir(parents=True)
            git("init", "-q", "-b", "main", cwd=work)
        for path, data in files.items():
            (work / path).parent.mkdir(parents=True, exist_ok=True)
            (work / path).write_bytes(data)
        git("add", "-A", cwd=work)
        git("commit", "-q", "-m", tag, cwd=work)
        git("tag", tag, cwd=work)
        bare = self.git_base / "WHIPFX" / f"{name}.git"
        if bare.exists():
            git("push", "-q", "--tags", str(bare), "main", cwd=work)
        else:
            bare.parent.mkdir(parents=True, exist_ok=True)
            git("clone", "-q", "--bare", str(work), str(bare), cwd=self.tmp)

    def write_manifest(self, core_tag: str) -> Path:
        d = self.tmp / "recipe"
        d.mkdir(exist_ok=True)
        for p in ("legacy", "enhanced"):
            (d / f"server.{p}.cfg").write_text(f"# {p}\nensure whip_core\n")
        (d / "manifest.toml").write_text(f"""
[recipe]
version = "1.0.0"
author = "WHIPFX"
max_part_mib = 0.05
min_mbit = 3
[platform.legacy]
title = "Legacy"
name = "WHIP"
lite_name = "WHIP Lite"
server_cfg = "server.legacy.cfg"
[platform.enhanced]
title = "Enhanced"
name = "WHIP (Enhanced)"
lite_name = "WHIP Lite (Enhanced)"
server_cfg = "server.enhanced.cfg"
[[section]]
title = "WHIP"
[[section.resource]]
name = "whip_core"
repo = "WHIPFX/whip_core"
tag = "{core_tag}"
dest = "./resources/whip_core"
github = "source"
[[section.resource]]
name = "whip_media_props"
repo = "WHIPFX/whip_media_props"
tag = "v1.0.0"
dest = "./resources/[assets]/[media]/whip_media_props"
github = "release-zips"
parts = 4
heavy = true
[[section.resource]]
name = "oxmysql"
repo = "WHIPFX/oxmysql"
tag = "v2"
dest = "./resources/[ox]"
github = "release-asset"
asset = "oxmysql.zip"
""")
        return d / "manifest.toml"

    def cdn(self, *args: str) -> None:
        cdn.main([args[0], "--manifest", str(self.manifest), "--store", f"dir:{self.bucket}", "--base", self.base,
                  "--dist", str(self.dist), "--git-base", self.git_base.as_uri(), "--asset-base", self.asset_base,
                  "--wait", "0", *args[1:]])

    def publish(self, release_id: str) -> None:
        self.dist = self.tmp / f"dist-{release_id}"
        for step in ("build", "upload", "verify"):
            self.cdn(step)
        self.cdn("promote", "--release-id", release_id)

    def stable(self, name: str = "whip-legacy.yaml") -> str:
        return cdn.fetch(f"{self.base}/recipes/{name}", keep=True).body.decode()

    def releases(self) -> dict:
        return json.loads(cdn.fetch(f"{self.base}/{cdn.RELEASES}", keep=True).body)

    def test_publish_a_second_release_and_roll_back(self) -> None:
        self.publish("r1")
        r1 = self.stable()
        self.assertIn("release r1", r1)
        # whip_media_props is split into parts of at most 0.05 MiB; oxmysql is mirrored as it is
        props = json.loads(self.store.get("releases/whip_media_props/v1.0.0/index.json"))
        self.assertGreater(len(props["parts"]), 1)
        self.assertTrue(all(p["size"] <= 0.05 * 1048576 for p in props["parts"]))
        ox = json.loads(self.store.get("releases/oxmysql/v2/index.json"))["parts"][0]
        self.assertEqual(ox["key"], f"releases/oxmysql/v2/{ox['sha256'][:16]}/oxmysql.zip")
        self.assertIn(f"url: {self.base}/{ox['key']}", r1)
        lite = self.stable("whip-enhanced-lite.yaml")
        self.assertNotIn("dest: ./resources/[assets]/[media]/whip_media_props", lite)
        self.assertIn(props["parts"][0]["sha256"], lite)  # the media scripts check what they download
        self.assertNotIn("github.com", r1.split("\ntasks:\n")[1])

        # A new tag of one resource: only it is built; the rest come from the bucket as they are
        self.repo("whip_core", {"client.lua": b"print('v2')\n"}, "v1.1.0")
        self.manifest = self.write_manifest("v1.1.0")
        with mock.patch("cdn.pack_repo", wraps=cdn.pack_repo) as packed:
            self.publish("r2")
        self.assertEqual([c.args[2] for c in packed.call_args_list], ["whip_core"])
        core = json.loads(self.store.get("releases/whip_core/v1.1.0/index.json"))["parts"][0]["key"]
        self.assertIn(f"url: {self.base}/{core}", self.stable())
        self.assertEqual(self.releases()["current"], "r2")

        # Rollback copies r1's recipes back: nothing is rebuilt
        self.cdn("rollback", "--to", "r1")
        self.assertEqual(self.stable(), r1)
        rel = self.releases()
        self.assertEqual(rel["current"], "r1")
        self.assertEqual([(h["action"], h["to"]) for h in rel["history"]],
                         [("promote", "r1"), ("promote", "r2"), ("rollback", "r1")])
        with self.assertRaises(SystemExit):
            self.cdn("rollback", "--to", "r9")

    def test_nothing_is_promoted_without_a_passing_verify(self) -> None:
        self.dist = self.tmp / "dist"
        self.cdn("build")
        self.cdn("upload")
        with self.assertRaises(SystemExit):
            self.cdn("promote", "--release-id", "r1")  # verify never ran
        # A file the CDN serves that isn't what was uploaded fails verify
        key = json.loads(self.store.get("releases/whip_core/v1.0.0/index.json"))["parts"][0]["key"]
        (self.bucket / key).write_bytes(b"not the zip")
        with self.assertRaises(SystemExit):
            self.cdn("verify")
        self.assertFalse((self.dist / "verified.json").exists())
        self.assertIsNone(self.store.get(cdn.RELEASES))

    def test_two_builds_of_one_tag_that_differ_never_replace_each_other(self) -> None:
        # Two runs build the same tag at once, on systems whose zlib gives other bytes. The first upload stands; the
        # second keeps its zips under their own sha256 keys and adopts the first's index, replacing nothing.
        self.dist = self.tmp / "first"
        self.cdn("build", "--only", "whip_core")
        first = self.dist
        self.dist = self.tmp / "second"
        self.cdn("build", "--only", "whip_core")
        built = next((self.dist / "zips" / "whip_core").glob("*/whip_core-1.zip"))
        built.write_bytes(b"rebuilt differently")
        index = built.parent / "index.json"
        data = json.loads(index.read_text())
        sha = cdn.sha256_file(built)
        data["parts"][0].update(size=built.stat().st_size, sha256=sha, key=f"releases/whip_core/v1.0.0/{sha[:16]}/whip_core-1.zip")
        index.write_text(json.dumps(data))
        (self.dist / "index" / "whip_core.json").write_text(json.dumps(data))
        self.dist = first
        self.cdn("upload")
        winner = self.store.get("releases/whip_core/v1.0.0/index.json")
        self.dist = self.tmp / "second"
        self.cdn("upload")
        self.assertEqual(self.store.get("releases/whip_core/v1.0.0/index.json"), winner)
        self.assertEqual((self.dist / "index" / "whip_core.json").read_bytes(), winner)
        original = json.loads(winner)["parts"][0]
        self.assertEqual(cdn.sha256_file(self.bucket / original["key"]), original["sha256"])

    def test_a_promote_whose_stable_recipes_fail_goes_back_to_the_last_release(self) -> None:
        self.publish("r1")
        r1 = self.stable()
        self.repo("whip_core", {"client.lua": b"print('v2')\n"}, "v1.1.0")
        self.manifest = self.write_manifest("v1.1.0")
        # The first check (of r2) fails, the second (of r1, after going back) passes
        with mock.patch("cdn.settle", side_effect=[["recipes/whip-legacy.yaml: HTTP 503"], []]):
            with self.assertRaises(SystemExit):
                self.publish("r2")
        self.assertEqual(self.stable(), r1)
        rel = self.releases()
        self.assertEqual(rel["current"], "r1")
        self.assertEqual(rel["history"][-1]["action"], "auto-rollback")

    def test_an_older_commit_does_not_replace_a_newer_release(self) -> None:
        repo = self.tmp / "work" / "whip_core"
        old = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
        self.repo("whip_core", {"b.lua": b"--\n"}, "v1.0.1")
        new = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
        self.assertTrue(cdn.is_stale(old, new, repo))
        self.assertFalse(cdn.is_stale(new, old, repo))
        self.assertFalse(cdn.is_stale(new, new, repo))


if __name__ == "__main__":
    unittest.main()
