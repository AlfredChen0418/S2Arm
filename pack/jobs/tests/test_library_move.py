"""换游戏库：打开已有的、认领旧产物、搬家（同盘 / 跨盘）和失败回滚。"""
import hashlib
import json
import os
import sys
import tempfile
import unittest
import unittest.mock as um
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import library
from core import Failure

GID = "0100000000010000"


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


class MoveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.private = self.root / "private"; self.private.mkdir()

    def ctx(self, library_path, **request):
        notes = []
        c = SimpleNamespace(library=library_path, private=self.private, engine=self.root / "engine", r=request, begin=lambda *a, **k: None, check=lambda: None,
                            progress=lambda *a: None, redact=lambda s: s, diagnostic=lambda m, **k: notes.append(m), notes=notes)
        return c

    def old(self, name="old"):
        lib = self.root / name; (lib / "saves" / GID).mkdir(parents=True); (lib / "iPhone" / GID / "t资源包").mkdir(parents=True); (lib / "games" / GID).mkdir(parents=True)
        (lib / ".s2arm-library.json").write_text(json.dumps({"schema": 1, "id": "library-id"}))
        (lib / "catalog.json").write_text(json.dumps({"schema": 1, "games": [{"id": GID, "name": "甲", "display": "", "packages": []}]}))
        (lib / "saves" / GID / "save.bin").write_bytes(b"save-data-1234")
        (lib / "iPhone" / GID / "t.ipa").write_bytes(b"ipa" * 1000)
        (lib / "iPhone" / GID / "t资源包" / "romfs.img").write_bytes(b"pack" * 100)
        (lib / ".s2arm-work" / "cache" / "air").mkdir(parents=True); (lib / ".s2arm-work" / "cache" / "air" / "shader.bin").write_bytes(b"shader")
        (lib / ".execution.lock").write_text(""); (lib / ".catalog.lock").write_text("")
        return lib

    def run_setup(self, target, **request):
        with um.patch.object(library, "require_apfs"), um.patch.object(library, "require_local"):
            return library.setup(self.ctx(target, **request))

    def assert_moved(self, old, new):
        self.assertEqual(sha(new / "saves" / GID / "save.bin"), hashlib.sha256(b"save-data-1234").hexdigest())
        self.assertTrue((new / "iPhone" / GID / "t.ipa").is_file())
        self.assertTrue((new / "iPhone" / GID / "t资源包" / "romfs.img").is_file())
        self.assertTrue((new / ".s2arm-work" / "cache" / "air" / "shader.bin").is_file())
        self.assertEqual(json.loads((new / "catalog.json").read_text())["games"][0]["id"], GID)
        self.assertFalse((old / "iPhone").exists()); self.assertFalse((new / ".s2arm-moving").exists())

    def assert_untouched(self, old, new):
        self.assertEqual((old / "saves" / GID / "save.bin").read_bytes(), b"save-data-1234")
        self.assertTrue((old / "iPhone" / GID / "t.ipa").is_file()); self.assertTrue((old / ".s2arm-library.json").is_file())
        self.assertFalse((new / ".s2arm-library.json").exists()); self.assertFalse((new / ".s2arm-moving").exists())

    def test_opens_existing_library(self):
        lib = self.old("lib")
        r = self.run_setup(lib)
        self.assertEqual((r["outcome"], r["libraryId"]), ("opened", "library-id"))

    def test_empty_folder_without_move_is_new_and_empty(self):
        r = self.run_setup(self.root / "fresh")
        self.assertEqual(r["outcome"], "created")
        self.assertEqual(json.loads((self.root / "fresh/catalog.json").read_text())["games"], [])

    def test_adopts_copied_products_with_their_catalog(self):
        lib = self.old("copied"); (lib / ".s2arm-library.json").unlink()
        r = self.run_setup(lib)
        self.assertEqual(r["outcome"], "adopted"); self.assertTrue(r["libraryId"])
        self.assertEqual(json.loads((lib / "catalog.json").read_text())["games"][0]["name"], "甲")

    def test_adopts_products_without_a_catalog(self):
        lib = self.old("copied"); (lib / ".s2arm-library.json").unlink(); (lib / "catalog.json").unlink()
        self.assertEqual(self.run_setup(lib)["outcome"], "adopted")
        games = json.loads((lib / "catalog.json").read_text())["games"]
        self.assertEqual([g["id"] for g in games], [GID]); self.assertEqual(games[0]["name"], GID)

    def test_unrelated_folder_is_refused_and_left_alone(self):
        d = self.root / "docs"; d.mkdir(); (d / "a.txt").write_text("x")
        with self.assertRaises(Failure): self.run_setup(d)
        self.assertEqual([p.name for p in d.iterdir()], ["a.txt"])

    def test_same_volume_move(self):
        old, new = self.old(), self.root / "new"
        r = self.run_setup(new, moveFrom=str(old))
        self.assertEqual((r["outcome"], r["libraryId"]), ("moved", "library-id"))
        self.assert_moved(old, new)
        self.assertFalse(old.exists())

    def test_cross_volume_move(self):
        old, new = self.old(), self.root / "new"
        with um.patch.object(library, "_same_volume", return_value=False):
            r = self.run_setup(new, moveFrom=str(old))
        self.assertEqual(r["outcome"], "moved"); self.assert_moved(old, new); self.assertFalse(old.exists())

    def test_cross_volume_failed_verification_rolls_back(self):
        old, new = self.old(), self.root / "new"
        real = library._verify_copy
        def corrupt(src, dst, strict):
            if src.name == "saves": (dst / GID / "save.bin").write_bytes(b"damaged-data-!")
            real(src, dst, strict)
        with um.patch.object(library, "_same_volume", return_value=False), um.patch.object(library, "_verify_copy", corrupt), self.assertRaises(Failure) as e:
            self.run_setup(new, moveFrom=str(old))
        self.assertEqual(e.exception.code, "library_move_failed"); self.assert_untouched(old, new)

    def test_interrupted_rename_rolls_back(self):
        old, new = self.old(), self.root / "new"
        real, calls = os.rename, [0]
        def flaky(a, b):
            calls[0] += 1
            if calls[0] == 3: raise OSError(28, "no space")
            real(a, b)
        with um.patch.object(library.os, "rename", flaky), self.assertRaises(OSError):
            self.run_setup(new, moveFrom=str(old))
        self.assert_untouched(old, new)
        self.assertEqual(sorted(p.name for p in new.iterdir()), [])

    def test_not_enough_space_changes_nothing(self):
        old, new = self.old(), self.root / "new"
        usage = SimpleNamespace(free=0, total=1, used=1)
        with um.patch.object(library, "_same_volume", return_value=False), um.patch.object(library.shutil, "disk_usage", return_value=usage), self.assertRaises(Failure) as e:
            self.run_setup(new, moveFrom=str(old))
        self.assertEqual(e.exception.code, "disk_full"); self.assert_untouched(old, new)

    def test_refuses_a_target_inside_the_library(self):
        old = self.old()
        with self.assertRaises(Failure): self.run_setup(old / "inner", moveFrom=str(old))
        self.assertTrue((old / "iPhone" / GID / "t.ipa").is_file())

    def test_default_library_brings_private_caches_along(self):
        old = self.private / "lib"; old.parent.mkdir(exist_ok=True)
        src = self.old(); os.rename(src, old); new = self.root / "new"
        (self.private / "cache" / "pso").mkdir(parents=True); (self.private / "cache" / "pso" / "p.bin").write_bytes(b"pso")
        import shutil; shutil.rmtree(old / ".s2arm-work")
        self.run_setup(new, moveFrom=str(old))
        self.assertTrue((new / ".s2arm-work" / "cache" / "pso" / "p.bin").is_file())
        self.assertFalse((self.private / "cache").exists())

    def test_absolute_links_follow_the_move(self):
        old, new = self.old(), self.root / "new"
        os.symlink(old / "iPhone" / GID / "t.ipa", old / "iPhone" / GID / "current.ipa")
        self.run_setup(new, moveFrom=str(old))
        self.assertEqual(os.readlink(new / "iPhone" / GID / "current.ipa").removeprefix("\\\\?\\"), str(new / "iPhone" / GID / "t.ipa"))
        self.assertTrue((new / "iPhone" / GID / "current.ipa").is_file())


if __name__ == "__main__":
    unittest.main()
