"""python -m unittest discover -s tools/tests"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import recipes  # noqa: E402


def fake_indexes(m: recipes.Manifest) -> dict[str, dict]:
    return {r.name: {"resource": r.name, "tag": r.tag, "parts": [
        {"name": f"{r.name}-{i}.zip", "key": f"releases/{r.name}/{r.tag}/{r.name}-{i}.zip", "size": 1_000_000 * i,
         "sha256": f"{i:064x}"} for i in (1, 2)]} for r in m.resources}


class Committed(unittest.TestCase):
    def test_the_committed_recipes_match_the_manifest(self) -> None:
        self.assertEqual(recipes.main(["--check"]), 0, "run python tools/recipes.py and commit the recipes")

    def test_check_fails_when_a_recipe_is_out_of_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(recipes.main(["--out", tmp]), 0)
            stale = Path(tmp) / "whip-legacy.yaml"
            stale.write_bytes(stale.read_bytes().replace(b"v1.1.0", b"v1.0.9"))
            self.assertEqual(recipes.main(["--check", "--out", tmp]), 1)


class Recipes(unittest.TestCase):
    m = recipes.load_manifest()

    def test_every_resource_but_the_picture_packs_and_the_scripts_to_add_them(self) -> None:
        self.assertTrue(any(r.pictures for r in self.m.resources))
        for platform in self.m.platforms:
            text = recipes.render(self.m, platform)
            for r in self.m.resources:
                self.assertEqual(f"dest: {r.dest}\n" in text, not r.pictures, r.name)
            self.assertIn("file: ./get-whip-media.ps1", text)
            self.assertIn("file: ./get-whip-media.sh", text)

    def test_the_media_scripts_get_every_picture_pack_part(self) -> None:
        text = recipes.render(self.m, "legacy")
        for r in self.m.resources:
            if r.pictures:
                for p in recipes.github_parts(r):
                    self.assertIn(f"get '{p.url}' '{r.dest.removeprefix('./')}' 0 ''", text)
                    self.assertIn(f",@('{p.url}', '{r.dest.removeprefix('./')}', 0, '')", text)

    def test_the_platforms_differ_only_in_their_header_and_server_cfg(self) -> None:
        def tasks(text: str) -> str:
            return text.split("\ntasks:\n", 1)[1].split("  - action: write_file\n    file: ./server.cfg", 1)[0]
        self.assertEqual(tasks(recipes.render(self.m, "legacy")), tasks(recipes.render(self.m, "enhanced")))

    def test_recipes_are_lf_only_and_names_fit_txadmin(self) -> None:
        for name, text in recipes.render_all(self.m).items():
            self.assertNotIn("\r", text, name)
            self.assertTrue(text.endswith("\n"), name)
            title = re.search(r"^name: (.+)$", text, re.M)
            self.assertIsNotNone(title)
            self.assertLessEqual(len(title.group(1)), 24, name)  # type: ignore[union-attr]


class Cdn(unittest.TestCase):
    m = recipes.load_manifest()
    base = "https://cdn.example.com"

    def test_cdn_recipes_never_touch_github(self) -> None:
        # The point of them: a server that can't reach GitHub (ENETUNREACH) installs from the CDN alone
        for name, text in recipes.render_all(self.m, "cdn", indexes=fake_indexes(self.m), base=self.base,
                                             release_id="r1").items():
            tasks = text.split("\ntasks:\n", 1)[1]
            self.assertNotIn("download_github", tasks, name)
            for url in re.findall(r"(https?://[^\s'\"]+)", tasks):
                if "portal.cfx.re" not in url:
                    self.assertTrue(url.startswith(self.base + "/releases/"), f"{name}: {url}")
            self.assertIn("release r1", text)

    def test_cdn_recipes_download_every_part_and_unzip_it_with_the_long_timeout(self) -> None:
        text = recipes.render(self.m, "enhanced", "cdn", indexes=fake_indexes(self.m), base=self.base)
        installed = [r for r in self.m.resources if not r.pictures]
        for r in installed:
            for i in (1, 2):
                url = f"{self.base}/releases/{r.name}/{r.tag}/{r.name}-{i}.zip"
                self.assertIn(f"    url: {url}\n    path: ./tmp/{r.name}-{i}.zip\n\n  - action: unzip\n"
                              f"    src: ./tmp/{r.name}-{i}.zip\n    dest: {r.dest}\n    timeoutSeconds: 1800\n", text)
        # 3 MB per resource in fake_indexes
        self.assertIn(f"Downloads {3 * len(installed)} MB from WHIP's CDN.", " ".join(text.split()))

    def test_every_description_fits_txadmins_256_characters(self) -> None:
        big = fake_indexes(self.m)
        for index in big.values():
            for part in index["parts"]:
                part["size"] = 40 * 1048576  # thousands of MB: the longest the size can get
        for source, kw in (("github", {}), ("cdn", {"indexes": big, "base": self.base, "release_id": "r1"})):
            for name, text in recipes.render_all(self.m, source, **kw).items():  # type: ignore[arg-type]
                desc = " ".join(text.split("description: >-\n", 1)[1].split("\n\n", 1)[0].split())
                self.assertLessEqual(len(desc), 256, f"{source} {name}: {desc}")

    def test_the_cdn_media_scripts_check_sizes_and_sha256s(self) -> None:
        text = recipes.render(self.m, "legacy", "cdn", indexes=fake_indexes(self.m), base=self.base)
        sha = f"{2:064x}"
        self.assertIn(f"/whip_media_props-2.zip' 'resources/[assets]/[media]/whip_media_props' 2000000 '{sha}'", text)

    def test_a_cdn_recipe_needs_a_base_and_matching_indexes(self) -> None:
        with self.assertRaises(ValueError):
            recipes.render(self.m, "legacy", "cdn", indexes=fake_indexes(self.m))
        wrong = fake_indexes(self.m)
        wrong["whip_core"]["tag"] = "v0.0.1"
        with self.assertRaises(ValueError):
            recipes.render(self.m, "legacy", "cdn", indexes=wrong, base=self.base)


class Validation(unittest.TestCase):
    m = recipes.load_manifest()

    def test_bad_manifests_are_refused(self) -> None:
        core = self.m.sections[0].resources[0]
        for bad in (replace(core, github="release-zips", parts=0), replace(core, github="release-asset"),
                    replace(core, dest="./server.cfg"), replace(core, dest="./resources/../x")):
            section = replace(self.m.sections[0], resources=(bad,))
            with self.assertRaises(ValueError, msg=bad):
                recipes.validate(replace(self.m, sections=(section,)))
        twice = replace(self.m.sections[0], resources=(core, core))
        with self.assertRaises(ValueError):
            recipes.validate(replace(self.m, sections=(twice,)))


if __name__ == "__main__":
    unittest.main()
