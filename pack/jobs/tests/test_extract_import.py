"""解包目录导入单测（pack/jobs/library.py import_games 支持 dump / extract / loose 三种解包目录，11-08）。"""
import unittest
from pathlib import Path

from jobcase import JobCase, synthetic_npdm, synthetic_nacp


class ExtractImportTests(JobCase):
    """三种解包目录导入：dump（Switch 2 导出）、extract（S2Arm 解包）、loose（通用解包），都不要 prod.keys"""

    def make_dump(self, root, tid, name="DumpGame"):
        """Switch 2 导出目录：base/exefs/main.npdm + main，可选 ctl/control.nacp + icon.jpg"""
        (root / "base" / "exefs").mkdir(parents=True)
        (root / "base" / "exefs" / "main.npdm").write_bytes(synthetic_npdm(tid))
        (root / "base" / "exefs" / "main").write_bytes(b"\x01" * 500)
        (root / "base" / "romfs").mkdir(parents=True)
        (root / "base" / "romfs" / "data.bin").write_bytes(b"BASE_DATA" * 100)
        # 可选控制分区
        if name != "DumpGame":
            (root / "ctl").mkdir(parents=True)
            (root / "ctl" / "control.nacp").write_bytes(synthetic_nacp(name, "1.0.0"))
            (root / "ctl" / "icon.jpg").write_bytes(b"DUMP_ICON")
        return root

    def make_extract(self, root, tid, name="ExtractGame", name_zh="提取游戏"):
        """S2Arm 解包目录：exefs/main + main.npdm，unpack.tsv"""
        (root / "exefs").mkdir(parents=True)
        (root / "exefs" / "main").write_bytes(b"\x02" * 400)
        (root / "exefs" / "main.npdm").write_bytes(synthetic_npdm(tid))
        (root / "romfs").mkdir(parents=True)
        (root / "romfs" / "data.bin").write_bytes(b"EXTRACT_DATA" * 80)
        tsv = f"title_id\t{tid:016X}\nname\t{name}\nname_zh\t{name_zh}\ndisplay_version\t2.1.0\n"
        (root / "unpack.tsv").write_text(tsv, encoding="utf-8")
        # 可选封面
        (root / "exefs" / "icon.jpg").write_bytes(b"EXTRACT_ICON")
        return root

    def make_loose(self, root, tid, name="LooseGame"):
        """通用解包目录：exefs/main + main.npdm + control.nacp，romfs/ 目录，没有 unpack.tsv"""
        (root / "exefs").mkdir(parents=True)
        (root / "exefs" / "main").write_bytes(b"\x03" * 600)
        (root / "exefs" / "main.npdm").write_bytes(synthetic_npdm(tid))
        (root / "exefs" / "control.nacp").write_bytes(synthetic_nacp(name, "3.0.0"))
        (root / "exefs" / "icon.jpg").write_bytes(b"LOOSE_ICON")
        (root / "romfs").mkdir(parents=True)
        (root / "romfs" / "data.bin").write_bytes(b"LOOSE_DATA" * 90)
        return root

    def test_dump_import_without_keys(self):
        # Switch 2 导出目录（有 base/exefs/main.npdm）：不要密钥能导入，format=dump，程序号从 main.npdm 读，
        # 有 ctl/control.nacp 用它，没有用目录名；封面取 ctl/icon.jpg
        tid = 0x0100123456789000
        dump = self.make_dump(self.root / "dump1", tid, "Zelda NS2")
        result, done = self.worker("import", paths=[str(dump)])
        self.assertEqual(done["state"], "succeeded", result.stderr + str(done.get("diagnostics")))
        [game] = self.c.catalog()["games"]
        [pkg] = game["packages"]
        self.assertEqual((game["id"], game["name"], pkg["type"], pkg["format"], pkg["version"]),
                         (f"{tid:016X}", "Zelda NS2", "base", "dump", 0))
        self.assertEqual(pkg["path"], str(dump.resolve()))
        self.assertTrue(pkg["size"] > 0)  # 目录内文件总字节
        self.assertEqual((self.c.library / "games" / game["id"] / "cover.jpg").read_bytes(), b"DUMP_ICON")
        # 没有控制分区：标题取目录名
        dump2 = self.make_dump(self.root / "RawDump", 0x0100AABBCCDD0000)
        (self.c.library / "catalog.json").write_text('{"schema": 1, "games": []}')
        result, done = self.worker("import", paths=[str(dump2)])
        self.assertEqual(done["state"], "succeeded", result.stderr)
        self.assertEqual(self.c.catalog()["games"][0]["name"], "RawDump")

    def test_extract_import_without_keys(self):
        # S2Arm 解包目录（有 exefs/main 与 unpack.tsv）：不要密钥能导入，format=extract，元数据取 unpack.tsv（优先中文名）
        tid = 0x01002468ACE00000
        extract = self.make_extract(self.root / "extract1", tid, "Mario", "马力欧")
        result, done = self.worker("import", paths=[str(extract)])
        self.assertEqual(done["state"], "succeeded", result.stderr)
        [game] = self.c.catalog()["games"]
        [pkg] = game["packages"]
        self.assertEqual((game["id"], game["name"], pkg["type"], pkg["format"], pkg["display"]),
                         (f"{tid:016X}", "马力欧", "base", "extract", "2.1.0"))
        self.assertEqual(pkg["path"], str(extract.resolve()))
        self.assertEqual((self.c.library / "games" / game["id"] / "cover.jpg").read_bytes(), b"EXTRACT_ICON")

    def test_loose_folder_reaches_ns2_dump(self):
        # 玩家 #9：已解包目录出包走 extract_folder 的 loose 分支，用到 tempfile（曾漏 import，NameError）。手算期望：起 ns2_dump.py 一次，第一个位置参数 = 临时目录，里面 base 是指向源目录的链接
        import os
        import tempfile as tf
        from types import SimpleNamespace
        from unittest import mock
        import build
        with tf.TemporaryDirectory() as d:
            root = Path(d); src = root / "src"; src.mkdir(); (root / "priv").mkdir(); calls = []
            def run(args, log, env):
                stage = Path(args[4])
                calls.append((Path(args[3]).name, os.path.islink(stage / "base"), os.path.realpath(stage / "base")))
            c = SimpleNamespace(private=root / "priv", engine=root, run=run)
            with mock.patch.object(build, "source_path", return_value=src), mock.patch.object(build, "reader_program", return_value="r"):
                build.extract_folder(c, dict(name="G"), [dict(format="loose")], root / "x", {}, root)
            self.assertEqual(calls, [("ns2_dump.py", True, os.path.realpath(src))])

    def test_loose_import_without_keys(self):
        # 通用解包目录（有 exefs/main、exefs/main.npdm 和 romfs/ 目录，没有 unpack.tsv）：不要密钥能导入，format=loose，
        # 程序号从 main.npdm 读，名称取 exefs/control.nacp 或目录名
        tid = 0x0100FFEEDDCCBB00
        loose = self.make_loose(self.root / "loose1", tid, "Splatoon")
        result, done = self.worker("import", paths=[str(loose)])
        self.assertEqual(done["state"], "succeeded", result.stderr)
        [game] = self.c.catalog()["games"]
        [pkg] = game["packages"]
        self.assertEqual((game["id"], game["name"], pkg["type"], pkg["format"]),
                         (f"{tid:016X}", "Splatoon", "base", "loose"))
        self.assertEqual((self.c.library / "games" / game["id"] / "cover.jpg").read_bytes(), b"LOOSE_ICON")

    def test_extract_dir_in_subfolder(self):
        # 解包目录嵌在子目录里：扫描时能找到，认到就不再往里走
        folder = self.root / "downloads"
        self.make_dump(folder / "sub1" / "game_dump", 0x0100111111110000, "Sub1Game")
        self.make_extract(folder / "sub2" / "game_extract", 0x0100222222220000, "Sub2Game", "子游戏2")
        result, done = self.worker("import", paths=[str(folder)])
        self.assertEqual(done["state"], "succeeded", result.stderr)
        games = sorted(self.c.catalog()["games"], key=lambda g: g["id"])
        self.assertEqual(len(games), 2)
        self.assertEqual((games[0]["name"], games[0]["packages"][0]["format"]), ("Sub1Game", "dump"))
        self.assertEqual((games[1]["name"], games[1]["packages"][0]["format"]), ("子游戏2", "extract"))

    def test_nsp_and_extract_dir_without_keys_rejected(self):
        # 同一文件夹里 NSP + 解包目录并存，没有密钥：整单 keys_missing（NSP 要密钥）
        self.install_reader_fixture()
        folder = self.root / "mixed"
        nsp = folder / "game.nsp"; nsp.parent.mkdir(); nsp.write_bytes(b"synthetic game fixture")
        self.make_dump(folder / "dumped", 0x0100AAAAAAA00000)
        result, done = self.worker("import", paths=[str(folder)])
        self.assert_failure(done, "keys_missing")

    def test_only_extract_dir_without_keys_succeeds(self):
        # 只有解包目录、没有 NSP：没有密钥照样成功，密钥状态不动（没用到密钥）
        self.install_reader_fixture()
        dump = self.make_dump(self.root / "only_dump", 0x0100BBBBBBBB0000)
        result, done = self.worker("import", paths=[str(dump)])
        self.assertEqual(done["state"], "succeeded", result.stderr)
        self.assertFalse((self.c.private / "keys/status.json").exists())


if __name__ == "__main__":
    unittest.main()
