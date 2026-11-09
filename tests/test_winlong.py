#!/usr/bin/env python3
"""Windows 长路径（pack/orch/winlong.py）。纯函数在所有平台测；真实文件操作只在 Windows 上跑"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pack.orch import winlong
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import redact


class LongPathTest(unittest.TestCase):
    def test_short_paths_untouched(self):
        self.assertEqual(winlong.long_path("C:\\a\\b.txt"), "C:\\a\\b.txt")
        self.assertEqual(winlong.long_path(7), 7)     # 文件描述符
        self.assertIsNone(winlong.long_path(None))

    def test_long_paths_prefixed(self):
        path = "C:\\" + "a\\" * 130 + "f.txt"
        self.assertGreater(len(path), winlong.LIMIT)
        self.assertEqual(winlong.long_path(path), "\\\\?\\" + path)
        self.assertEqual(winlong.long_path("\\\\?\\" + path), "\\\\?\\" + path)   # 不重复加
        unc = "\\\\srv\\share\\" + "a\\" * 130 + "f"
        self.assertEqual(winlong.long_path(unc), "\\\\?\\UNC\\srv\\share\\" + "a\\" * 130 + "f")

    def test_redact_keeps_windows_path_length(self):
        out = redact.redact_text(r"[WinError 206] : 'C:\Users\example\a\b.txt'")
        self.assertIn("[PRIVATE_PATH len=", out)
        self.assertNotIn("example", out.split("PRIVATE_PATH")[1])
        self.assertEqual(redact.redact_text("/Users/example/a.txt"), "[PRIVATE_PATH]")   # 非 Windows 路径不变


@unittest.skipUnless(os.name == "nt", "真实文件操作只在 Windows 上跑")
class RealFilesTest(unittest.TestCase):
    def test_operations_past_260(self):
        winlong.install()
        root = tempfile.mkdtemp()
        deep = Path(root, *["d" * 30] * 10)
        self.addCleanup(shutil.rmtree, winlong.long_path(root), True)
        deep.mkdir(parents=True)
        f = deep / "file.txt"
        self.assertGreater(len(str(f)), 260)
        f.write_text("x", encoding="utf-8")
        self.assertTrue(f.exists() and f.is_file() and os.path.isfile(f))
        self.assertEqual(f.stat().st_size, 1)
        self.assertEqual([p.name for p in deep.iterdir()], ["file.txt"])
        copied = deep / "copy.txt"
        shutil.copy2(f, copied)
        self.assertEqual(copied.read_text(encoding="utf-8"), "x")
        moved = deep / "moved.txt"
        os.replace(copied, moved)
        self.assertEqual(sorted(n for _, _, names in os.walk(root) for n in names), ["file.txt", "moved.txt"])


if __name__ == "__main__":
    unittest.main()
