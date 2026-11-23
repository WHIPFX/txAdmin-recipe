"""python -m unittest discover -s tools/tests"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import recipe_update  # noqa: E402
import recipes  # noqa: E402

TEXT = recipes.MANIFEST.read_text(encoding="utf-8")
# What the manifest installs now (it moves with every release), and a tag none of them has.
NOW = {r.name: r for r in recipes.load_manifest().resources}
NEW = "v99.0.0"


def changed(before: str, after: str) -> list[tuple[str, str]]:
    return [(a, b) for a, b in zip(before.split("\n"), after.split("\n")) if a != b]


class Move(unittest.TestCase):
    def test_a_source_resource_gets_only_its_tag(self) -> None:
        was = NOW["whip_core"].tag
        after = recipe_update.move(TEXT, "whip_core", NEW)
        self.assertEqual(changed(TEXT, after), [(f'tag = "{was}"', f'tag = "{NEW}"')])
        self.assertEqual(len(after) - len(TEXT), len(NEW) - len(was))

    def test_a_zip_resource_gets_its_tag_and_parts(self) -> None:
        g = NOW["whip_gunny"]
        after = recipe_update.move(TEXT, "whip_gunny", NEW, g.parts + 1)
        self.assertEqual(changed(TEXT, after),
                         [(f'tag = "{g.tag}"', f'tag = "{NEW}"'), (f"parts = {g.parts}", f"parts = {g.parts + 1}")])

    def test_only_that_resource_moves_even_when_others_share_its_tag(self) -> None:
        after = recipe_update.move(TEXT, "whip_media_props", NEW, 2)
        m = load(after)
        tags = {r.name: (r.tag, r.parts) for r in m.resources}
        self.assertEqual(tags["whip_media_props"], (NEW, 2))
        for other in ("whip_media_previews", "whip_media_clothing_male"):
            self.assertEqual(tags[other], (NOW[other].tag, NOW[other].parts))

    def test_unknown_resources_and_missing_fields_are_refused(self) -> None:
        with self.assertRaises(recipe_update.Refused):
            recipe_update.move(TEXT, "whip_nope", "v1.0.0")
        with self.assertRaises(recipe_update.Refused):
            recipe_update.move(TEXT, "whip_core", NEW, 3)  # a source resource has no parts

    def test_the_recipes_made_after_a_move_install_the_new_release(self) -> None:
        parts = NOW["whip_gunny"].parts + 1
        m = load(recipe_update.move(TEXT, "whip_gunny", NEW, parts))
        text = recipes.render(m, "enhanced", "full")
        base = "https://github.com/WHIPFX/whip_gunny/releases/download"
        for i in range(1, parts + 1):
            self.assertIn(f"    url: {base}/{NEW}/whip_gunny-{i}.zip\n", text)
        self.assertNotIn(f"{base}/{NOW['whip_gunny'].tag}/", text)
        self.assertIn(f"  # whip_gunny {NEW}, as its release's zips", text)


class Resources(unittest.TestCase):
    def test_it_moves_every_whipfx_resource_but_oxmysql(self) -> None:
        found = recipe_update.resources(recipes.load_manifest())
        self.assertNotIn("bob74_ipl", found)  # not WHIPFX's
        self.assertNotIn("oxmysql", found)  # a release build of WHIPFX's fork: by hand
        self.assertEqual(found["whip_core"], ("ref", NOW["whip_core"].tag))
        self.assertEqual(found["whip_gunny"], ("zips", NOW["whip_gunny"].tag))
        self.assertEqual(len(found), len(recipes.load_manifest().resources) - 2)


def load(text: str) -> recipes.Manifest:
    """The manifest from this text, with the real server.cfg files next to it."""
    tmp = Path(tempfile.mkdtemp())
    try:
        for cfg in recipes.MANIFEST.parent.glob("server.*.cfg"):
            shutil.copy(cfg, tmp / cfg.name)
        (tmp / "manifest.toml").write_text(text, encoding="utf-8")
        return recipes.load_manifest(tmp / "manifest.toml")
    finally:
        shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
