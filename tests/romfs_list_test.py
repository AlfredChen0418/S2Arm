#!/usr/bin/env python3
"""RomFS 清单单测（graphics/nvn/tools/romfs_list.py，约 1 秒）：python3 tests/romfs_list_test.py（tests/romfs_walk_lint_test.sh 调）
期望一律由标准库 / 系统命令独立算出（os.walk、find、open().read(32)），不复述实现：
  ① walk() 与 os.walk 逐项相同：嵌套 / 点目录 / 空目录 / 软链文件 / 软链目录（列进子目录名单但不进）/ 断链 / 打不开的目录（onerror 拿到同一个错误）/ 调用方原地排序、剪枝子目录
  ② heads()：每个普通文件的（大小、前 32 字节）与 open().read(32) 相同；读不开的文件记错误、用到时抛同类错误
  ③ 清单：首次写一份 <标签>-<32 位身份>.json；同身份再来从清单取、不读盘；只改内容不改大小与修改时间 = 身份不变（清单假设，同 make）；
     修改时间变 = 身份变 → 重读、另写一份；同标签只留 2 份；S2A_ROMFS_WALK=off 不写；不叫 romfs 的目录不落盘（追加内容等）
  ④ 命令行：files0 与 find -type f -print0 同集合；has-ext 与 find -iname … -print -quit 同真假；sizes 与 lstat 大小同
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
TOOL = HERE.parent / "graphics" / "nvn" / "tools" / "romfs_list.py"
sys.path.insert(0, str(TOOL.parent))
import romfs_list


def build(romfs):
    files = {"a/b/c.bin": b"BNSH" + bytes(40), "a/d.txt": b"x", "A.pak": b"PAK", "e": b"", ".hidden/f": b"SARC" * 20,
             "z/y/x/w.nro": bytes(16) + b"NRO0" + bytes(8), "big": bytes(range(256)) * 4}
    for rel, raw in files.items():
        (romfs / rel).parent.mkdir(parents=True, exist_ok=True)
        (romfs / rel).write_bytes(raw)
    (romfs / "empty").mkdir()
    (romfs / "link_file").symlink_to(romfs / "a/d.txt")
    (romfs / "link_dir").symlink_to(romfs / "a")
    (romfs / "broken").symlink_to(romfs / "nowhere")
    return files


class Walk(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.root = self.base / "t0100@现行"
        self.romfs = self.root / "romfs"
        self.romfs.mkdir(parents=True)
        self.files = build(self.romfs)
        os.environ.pop("S2A_ROMFS_WALK", None)

    def tearDown(self):
        for d, ds, _ in os.walk(self.base):
            for x in ds:
                os.chmod(os.path.join(d, x), 0o755)
        shutil.rmtree(self.base)

    def test_walk_same_as_os_walk(self):
        for top in (str(self.romfs), str(self.romfs) + "/", self.romfs):
            self.assertEqual(list(romfs_list.walk(top)), list(os.walk(top)))
        # 调用方原地改子目录名单（排序 / 剪枝）同样生效
        def pruned(w):
            out = []
            for d, ds, fs in w:
                ds.sort(reverse=True)
                ds[:] = [x for x in ds if x != "z"]
                out.append((d, list(ds), sorted(fs)))
            return out
        self.assertEqual(pruned(romfs_list.walk(self.romfs)), pruned(os.walk(self.romfs)))

    @unittest.skipIf(os.geteuid() == 0, "root 读得开 000 目录")
    def test_unreadable_dir_onerror(self):
        locked = self.romfs / "a" / "b"
        os.chmod(locked, 0)
        mine, ref = [], []
        got = list(romfs_list.walk(self.romfs, onerror=mine.append))
        self.assertEqual(got, list(os.walk(self.romfs, onerror=ref.append)))
        self.assertEqual([(type(e), e.errno, e.filename) for e in mine], [(type(e), e.errno, e.filename) for e in ref])
        # 盘点不全 = 没有身份（不读也不写清单）
        self.assertIsNone(romfs_list.scan(self.romfs).identity)

    def test_heads(self):
        tree = romfs_list.heads(self.romfs, None)
        expect = {}
        for d, _, fs in os.walk(self.romfs):
            for f in fs:
                p = os.path.join(d, f)
                rel = os.path.relpath(p, self.romfs).replace(os.sep, "/")
                if os.path.isfile(p):
                    with open(p, "rb") as h:
                        expect[rel] = (os.path.getsize(p), h.read(32))
        self.assertEqual({e.rel: e.read() for e in tree.files()}, expect)

    @unittest.skipIf(os.geteuid() == 0, "root 读得开 000 文件")
    def test_read_error_roundtrip(self):
        os.chmod(self.romfs / "e", 0)
        lists = self.base / "walks"
        # 第一次现读写清单，第二次从清单取：错误类别与 errno 不变
        for _ in range(2):
            e = next(e for e in romfs_list.heads(self.romfs, lists).files() if e.rel == "e")
            with self.assertRaises(PermissionError) as cm:
                e.read()
            self.assertEqual(cm.exception.errno, 13)
        os.chmod(self.romfs / "e", 0o644)

    def test_listing(self):
        lists = self.base / "walks"
        first = romfs_list.heads(self.romfs, lists)
        names = sorted(p.name for p in lists.iterdir())
        self.assertEqual(len(names), 1)
        self.assertRegex(names[0], r"^t0100-[0-9a-f]{32}\.json$")
        # 同身份：从清单取，不读盘
        real = romfs_list._read_heads
        romfs_list._read_heads = lambda entries: (_ for _ in ()).throw(AssertionError("不该读盘"))
        try:
            again = romfs_list.heads(self.romfs, lists)
        finally:
            romfs_list._read_heads = real
        self.assertEqual({e.rel: e.read() for e in again.files()}, {e.rel: e.read() for e in first.files()})
        # 只改内容、大小与修改时间不变 = 身份不变：仍是清单里的旧头（同 make / rsync 的快速比对假设）
        target = self.romfs / "a/d.txt"
        st = target.stat()
        target.write_bytes(b"y")
        os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual(next(e for e in romfs_list.heads(self.romfs, lists).files() if e.rel == "a/d.txt").read()[1], b"x")
        # 修改时间变 = 身份变：重读、另写一份
        os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 1))
        self.assertEqual(next(e for e in romfs_list.heads(self.romfs, lists).files() if e.rel == "a/d.txt").read()[1], b"y")
        self.assertEqual(len(list(lists.iterdir())), 2)
        os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 2))
        romfs_list.heads(self.romfs, lists)
        # 同标签只留 2 份
        self.assertEqual(len(list(lists.iterdir())), 2)

    def test_default_listing(self):
        os.environ["S2A_ROMFS_WALK"] = "off"
        romfs_list.heads(self.romfs)
        self.assertFalse((self.base / ".romfs-walk").exists())
        del os.environ["S2A_ROMFS_WALK"]
        romfs_list.heads(self.romfs)
        # 缺省：<产物根上一级>/.romfs-walk
        self.assertEqual(len(list((self.base / ".romfs-walk").iterdir())), 1)
        aoc = self.root / "aoc" / "0"
        aoc.mkdir(parents=True)
        (aoc / "x").write_bytes(b"1")
        romfs_list.heads(aoc)
        # 不叫 romfs 的目录不落盘（缺省位置会落进产物根里）
        self.assertFalse((self.root / ".romfs-walk").exists())
        self.assertEqual(len(list((self.base / ".romfs-walk").iterdir())), 1)

    def test_cli(self):
        run = lambda *a: subprocess.run([sys.executable, str(TOOL), *a], capture_output=True)
        got = run("files0", str(self.romfs))
        ref = subprocess.run(["find", str(self.romfs), "-type", "f", "-print0"], capture_output=True)
        self.assertEqual(got.returncode, 0)
        self.assertEqual(sorted(got.stdout.split(b"\0")), sorted(ref.stdout.split(b"\0")))
        for exts in ((".pak",), (".PAK",), (".utoc", ".pkz"), (".nro",), (".none",)):
            pat = []
            for x in exts:
                pat += (["-o"] if pat else []) + ["-iname", "*" + x]
            ref = subprocess.run(["find", str(self.romfs), "(", *pat, ")", "-print", "-quit"], capture_output=True).stdout
            self.assertEqual(run("has-ext", str(self.romfs), *exts).returncode == 0, bool(ref), exts)
        out = self.base / "sizes.tsv"
        self.assertEqual(run("sizes", str(self.romfs), str(out)).returncode, 0)
        rows = {}
        for line in out.read_text().splitlines():
            n, rel = line.split("\t")
            rows[rel] = int(n)
        expect = {os.path.relpath(os.path.join(d, f), self.romfs): os.lstat(os.path.join(d, f)).st_size
                  for d, _, fs in os.walk(self.romfs) for f in fs if os.path.isfile(os.path.join(d, f)) and not os.path.islink(os.path.join(d, f))}
        self.assertEqual(rows, expect)
        # 同 find：读不开报错
        self.assertNotEqual(run("files0", str(self.base / "nope")).returncode, 0)


class HostJunk(unittest.TestCase):   # macOS 垃圾进索引却不进资源包：游戏枚举到就读不到（万缕千丝 Movie/.DS_Store，播片后闪退）
    def test_junk_is_not_inventoried_but_game_dotfiles_are(self):
        with tempfile.TemporaryDirectory() as d:
            t = Path(d); (t / "Movie").mkdir(); (t / "._dir").mkdir()
            for rel in (".DS_Store", "Movie/.DS_Store", "Movie/._a.mp4", "Movie/a.mp4", ".build.config", "._dir/b.bin"): (t / rel).write_bytes(b"x")
            want = sorted([".build.config", "._dir/b.bin", "Movie/a.mp4"])
            self.assertEqual(sorted(e.rel for e in romfs_list.scan(t).files()), want)
            walked = sorted(os.path.relpath(os.path.join(a, f), t).replace(os.sep, "/") for a, _, fs in romfs_list.walk(str(t)) for f in fs)
            self.assertEqual(walked, want)
            out = t.parent / (t.name + ".sizes")
            try:
                self.assertEqual(romfs_list.main(["sizes", str(t), str(out)]), 0)
                self.assertEqual(sorted(l.split("\t")[1] for l in out.read_text(encoding="utf-8").splitlines()), want)
            finally:
                out.unlink(missing_ok=True)


class CrossTagReuse(unittest.TestCase):   # 11-06：同身份、别的标签的清单也认（解包目录读过的头，播种搬进产物根后不再冷读）
    def test_same_identity_other_tag_loads_without_reading(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t); listing = t / "walk"
            a = t / "extract" / "romfs"; (a / "d").mkdir(parents=True)
            (a / "d/x.bin").write_bytes(b"HEADX" + bytes(100)); (a / "y.bin").write_bytes(b"HEADY")
            romfs_list.heads(a, listing)                       # 解包目录：读头、落清单（标签 extract）
            b = t / "roots" / "t0100@seed" / "romfs"; b.parent.mkdir(parents=True)
            os.rename(a, b)                                     # 播种整目录搬：同一批文件 → 同身份
            with patch.object(romfs_list, "_read_heads", side_effect=AssertionError("不该再读头")):
                tree = romfs_list.heads(b, listing)             # 产物根：标签 t0100，本标签没有清单 → 认同身份的 extract 清单
            got = {e.rel: e.read() for e in tree.files()}
            self.assertEqual(got, {"d/x.bin": (105, b"HEADX" + bytes(27)), "y.bin": (5, b"HEADY")})

    def test_different_identity_is_not_reused(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t); listing = t / "walk"
            a = t / "extract" / "romfs"; a.mkdir(parents=True); (a / "y.bin").write_bytes(b"HEADY")
            romfs_list.heads(a, listing)
            b = t / "roots" / "t0100@seed" / "romfs"; b.mkdir(parents=True); (b / "y.bin").write_bytes(b"OTHER!")   # 内容 / 大小不同
            tree = romfs_list.heads(b, listing)
            self.assertEqual({e.rel: e.read() for e in tree.files()}, {"y.bin": (6, b"OTHER!")})


if __name__ == "__main__":
    unittest.main()
