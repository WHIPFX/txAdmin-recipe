"""python -m unittest discover -s tools/tests"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import release_zips  # noqa: E402


def files(*paths: str) -> list[tuple[str, str, int]]:
    return [(p, f"sha-{p}", 10) for p in paths]


class GameSets(unittest.TestCase):
    def test_each_game_leaves_out_the_other_games_folder(self) -> None:
        sets = release_zips.game_sets(files("fxmanifest.lua", "stream/a.ydr", "stream/b/c.ytyp",
                                            "stream_enhanced/a.ydr", "stream_enhanced/b/c.ytyp", "data/x.meta"))
        self.assertEqual([f[0] for f in sets["legacy"]],
                         ["fxmanifest.lua", "stream/a.ydr", "stream/b/c.ytyp", "data/x.meta"])
        self.assertEqual([f[0] for f in sets["enhanced"]],
                         ["fxmanifest.lua", "stream_enhanced/a.ydr", "stream_enhanced/b/c.ytyp", "data/x.meta"])

    def test_a_resource_without_both_folders_has_one_set(self) -> None:
        self.assertIsNone(release_zips.game_sets(files("fxmanifest.lua", "stream/a.ydr")))
        self.assertIsNone(release_zips.game_sets(files("fxmanifest.lua", "img/a.webp")))

    def test_folders_that_differ_are_refused(self) -> None:
        with self.assertRaises(SystemExit):
            release_zips.game_sets(files("stream/a.ydr", "stream/b.ydr", "stream_enhanced/a.ydr"))


class ZipSet(unittest.TestCase):
    def test_names(self) -> None:
        self.assertEqual(release_zips.zip_set("whip_x", "whip_x-3.zip"), "")
        self.assertEqual(release_zips.zip_set("whip_x", "whip_x-legacy-1.zip"), "legacy")
        self.assertEqual(release_zips.zip_set("whip_x", "whip_x-enhanced-12.zip"), "enhanced")
        for other in ("whip_x.zip", "whip_x-other-1.zip", "whip_xy-1.zip", "whip_x-1.zip.bak"):
            self.assertIsNone(release_zips.zip_set("whip_x", other), other)


if __name__ == "__main__":
    unittest.main()
