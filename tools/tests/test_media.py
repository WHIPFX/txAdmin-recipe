"""python -m unittest discover -s tools/tests"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import media  # noqa: E402
import recipes  # noqa: E402


class Memory:
    """A bucket in a dict, keeping every put."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str, str]] = {}
        self.puts: list[str] = []

    def etags(self, prefix: str) -> dict[str, str]:
        return {k: hashlib.md5(v[0]).hexdigest() for k, v in self.objects.items() if k.startswith(prefix)}

    def get(self, key: str) -> bytes | None:
        o = self.objects.get(key)
        return o[0] if o else None

    def put(self, key: str, data: bytes, content_type: str, cache_control: str) -> None:
        self.objects[key] = (data, content_type, cache_control)
        self.puts.append(key)


PACKS = [r for r in recipes.load_manifest().resources if r.pictures]


def manifest(tags: dict[str, str]) -> recipes.Manifest:
    """The real manifest with some picture packs at other tags."""
    m = recipes.load_manifest()
    sections = tuple(recipes.Section(s.title, s.note, tuple(
        recipes.Resource(**{**r.__dict__, "tag": tags.get(r.name, r.tag)}) for r in s.resources)) for s in m.sections)
    return recipes.Manifest(m.version, m.author, m.max_part_mib, m.min_mbit, m.platforms, sections, m.cfg)


def pack(root: Path, files: dict[str, bytes]) -> None:
    for path, data in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(data)


class Publish(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Memory()
        self.props = "whip_media_props"
        for r in PACKS:
            pack(self.tmp / "v1" / r.name, {f"img/{r.name}/a.webp": b"a-" + r.name.encode(), "fxmanifest.lua": b"x",
                                            "img/notes.yml": b"y"})
        pack(self.tmp / "v1" / self.props, {"video/emotes/Dances/Wave.webm": b"wave"})

    def run_publish(self, m: recipes.Manifest, work: str, force: bool = False) -> str:
        out = StringIO()
        with redirect_stdout(out):
            code = media.publish(self.store, m, force, False, self.tmp / work)
        self.assertEqual(code, 0)
        return out.getvalue()

    def index(self) -> dict:
        return json.loads(self.store.get(media.INDEX))

    def test_the_first_publish_uploads_every_picture_and_lists_them(self) -> None:
        self.run_publish(recipes.load_manifest(), "v1")
        idx = self.index()
        self.assertEqual(sorted(idx["packs"]), sorted(r.name for r in PACKS))
        self.assertEqual(idx["packs"][self.props]["files"], {"img/whip_media_props": ["a.webp"],
                                                             "video/emotes/dances": ["wave.webm"]})   # lower case
        data, ctype, cache = self.store.objects["media/video/emotes/dances/wave.webm"]
        self.assertEqual((data, ctype, cache), (b"wave", "video/webm", media.FOREVER))
        self.assertEqual(self.store.objects[media.INDEX][2], media.SHORT)
        self.assertNotIn("media/fxmanifest.lua", self.store.objects)   # only img/ and video/, only media files
        self.assertNotIn("media/img/notes.yml", self.store.objects)

    def test_a_pack_already_at_its_tag_is_not_gone_through_again(self) -> None:
        self.run_publish(recipes.load_manifest(), "v1")
        self.store.puts.clear()
        self.assertIn("Nothing to publish", self.run_publish(recipes.load_manifest(), "v1"))
        self.assertEqual(self.store.puts, [])

    def test_a_release_uploads_only_what_changed_and_deletes_nothing(self) -> None:
        self.run_publish(recipes.load_manifest(), "v1")
        self.store.puts.clear()
        pack(self.tmp / "v2" / self.props, {f"img/{self.props}/a.webp": b"a-" + self.props.encode(),   # the same
                                            f"img/{self.props}/b.webp": b"new"})                       # new
        self.run_publish(manifest({self.props: "v9.0.0"}), "v2")
        self.assertEqual(sorted(self.store.puts), sorted([f"media/img/{self.props}/b.webp", media.INDEX]))
        self.assertIn("media/video/emotes/dances/wave.webm", self.store.objects)   # gone from the release, kept here
        idx = self.index()
        self.assertEqual(idx["packs"][self.props]["version"], "v9.0.0")
        self.assertEqual(idx["packs"][self.props]["files"], {f"img/{self.props}": ["a.webp", "b.webp"]})
        other = next(r for r in PACKS if r.name != self.props)
        self.assertEqual(idx["packs"][other.name]["version"], other.tag)   # the others as they were

    def test_a_changed_picture_is_uploaded_again(self) -> None:
        self.run_publish(recipes.load_manifest(), "v1")
        self.store.puts.clear()
        pack(self.tmp / "v2" / self.props, {f"img/{self.props}/a.webp": b"redrawn"})
        self.run_publish(manifest({self.props: "v9.0.0"}), "v2")
        self.assertIn(f"media/img/{self.props}/a.webp", self.store.puts)
        self.assertEqual(self.store.objects[f"media/img/{self.props}/a.webp"][0], b"redrawn")

    def test_only_the_picture_packs(self) -> None:
        names = {r.name for r in PACKS}
        self.assertEqual(names, {"whip_media_clothing_male", "whip_media_clothing_female", "whip_media_props",
                                 "whip_media_previews"})


if __name__ == "__main__":
    unittest.main()
