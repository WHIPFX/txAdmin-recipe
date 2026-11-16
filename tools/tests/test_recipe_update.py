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


def changed(before: str, after: str) -> list[tuple[str, str]]:
    return [(a, b) for a, b in zip(before.split("\n"), after.split("\n")) if a != b]


class Move(unittest.TestCase):
    def test_a_source_resource_gets_only_its_tag(self) -> None:
        after = recipe_update.move(TEXT, "whip_core", "v1.2.0")
        self.assertEqual(changed(TEXT, after), [('tag = "v1.1.0"', 'tag = "v1.2.0"')])
        self.assertEqual(len(after), len(TEXT))

    def test_a_zip_resource_gets_its_tag_and_parts(self) -> None:
        after = recipe_update.move(TEXT, "whip_gunny", "v2.1.0", 5)
        self.assertEqual(changed(TEXT, after), [('tag = "v2.0.1"', 'tag = "v2.1.0"'), ("parts = 4", "parts = 5")])

    def test_only_that_resource_moves_even_when_others_share_its_tag(self) -> None:
        after = recipe_update.move(TEXT, "whip_media_props", "v1.1.0", 2)
        m = load(after)
        tags = {r.name: (r.tag, r.parts) for r in m.resources}
        self.assertEqual(tags["whip_media_props"], ("v1.1.0", 2))
        self.assertEqual(tags["whip_media_previews"], ("v1.0.0", 3))
        self.assertEqual(tags["whip_media_clothing_male"], ("v1.0.0", 4))

    def test_unknown_resources_and_missing_fields_are_refused(self) -> None:
        with self.assertRaises(recipe_update.Refused):
            recipe_update.move(TEXT, "whip_nope", "v1.0.0")
        with self.assertRaises(recipe_update.Refused):
            recipe_update.move(TEXT, "whip_core", "v1.2.0", 3)  # a source resource has no parts

    def test_the_recipes_made_after_a_move_install_the_new_release(self) -> None:
        m = load(recipe_update.move(TEXT, "whip_gunny", "v2.1.0", 5))
        text = recipes.render(m, "enhanced", "full")
        base = "https://github.com/WHIPFX/whip_gunny/releases/download"
        for i in range(1, 6):
            self.assertIn(f"    url: {base}/v2.1.0/whip_gunny-{i}.zip\n", text)
        self.assertNotIn("v2.0.1", text)
        self.assertIn("  # whip_gunny v2.1.0, as its release's zips", text)


class Resources(unittest.TestCase):
    def test_it_moves_every_whipfx_resource_but_oxmysql(self) -> None:
        found = recipe_update.resources(recipes.load_manifest())
        self.assertNotIn("bob74_ipl", found)  # not WHIPFX's
        self.assertNotIn("oxmysql", found)  # a release build of WHIPFX's fork: by hand
        self.assertEqual(found["whip_core"], ("ref", "v1.1.0"))
        self.assertEqual(found["whip_gunny"], ("zips", "v2.0.1"))
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
