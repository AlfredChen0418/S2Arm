# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 S2Arm contributors
"""Cleanup must unlink entries without following them into player data."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import Failure
import library


class CleanupLinkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="s2arm synthetic cleanup ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.c = SimpleNamespace(library=self.root / "library", work_root=self.root / "work")
        self.gid = "0100000000000000"
        self.output = self.c.library / "iPhone" / self.gid
        self.output.mkdir(parents=True)

    def payload(self, parent):
        parent.mkdir(parents=True, exist_ok=True)
        path = parent / "synthetic.txt"
        path.write_bytes(b"player data must survive")
        return path

    def test_prune_unlinks_intermediates_without_deleting_their_targets(self):
        for retain in (True, False):
            for target in (self.c.library / "saves" / self.gid, self.root / "outside"):
                with self.subTest(retain_compiler=retain, target=target.name):
                    payload = self.payload(target)
                    link = self.output / "intermediate"
                    link.unlink(missing_ok=True)
                    link.symlink_to(target, target_is_directory=True)
                    library.prune_build(self.c, self.gid, retain_compiler=retain)
                    self.assertEqual(payload.read_bytes(), b"player data must survive")
                    self.assertFalse(link.is_symlink())

    def test_unlinks_file_and_dangling_links_without_following_them(self):
        payload = self.payload(self.c.library / "saves" / self.gid)
        for target in (payload, self.root / "missing"):
            with self.subTest(target=target.name):
                link = self.output / "scratch"
                link.unlink(missing_ok=True)
                payload.write_bytes(b"player data must survive")
                link.symlink_to(target)
                library._rm(self.c, self.c.library, link)
                self.assertFalse(link.is_symlink())
                self.assertEqual(payload.read_bytes(), b"player data must survive")

    def test_refuses_a_symlink_in_an_ancestor(self):
        payload = self.payload(self.c.library / "saves" / self.gid)
        link = self.output / "intermediate"
        link.symlink_to(payload.parent, target_is_directory=True)
        with self.assertRaises(Failure):
            library._rm(self.c, self.c.library, link / payload.name)
        self.assertEqual(payload.read_bytes(), b"player data must survive")

    def test_refuses_root_and_outside_entries_but_removes_ordinary_scratch(self):
        outside = self.payload(self.root / "outside")
        for path in (self.c.library, outside):
            with self.subTest(path=path.name), self.assertRaises(Failure):
                library._rm(self.c, self.c.library, path)
        scratch = self.payload(self.output / "scratch")
        library._rm(self.c, self.c.library, scratch.parent)
        self.assertFalse(scratch.parent.exists())
        self.assertEqual(outside.read_bytes(), b"player data must survive")

    @unittest.skipUnless(os.name == "nt", "Windows junction requires Windows")
    def test_junction_is_removed_without_deleting_its_target(self):
        payload = self.payload(self.c.library / "saves" / self.gid)
        junction = self.output / "intermediate"
        subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(payload.parent)],
                       check=True, capture_output=True)
        with self.assertRaises(Failure):
            library._rm(self.c, self.c.library, junction / payload.name)
        library.prune_build(self.c, self.gid)
        self.assertFalse(junction.exists())
        self.assertEqual(payload.read_bytes(), b"player data must survive")


if __name__ == "__main__":
    unittest.main()
