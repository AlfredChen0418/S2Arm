"""游戏库布局（pack/jobs/layout.py）单测：命名规则、旧布局迁移、中断续做。期望值按设计文档的规则手写，不跑被测实现推导。"""
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from jobcase import JobCase, OsView   # OsView：只给被测模块看 os.name = "nt"，不改全进程（7d265148）

import layout
from core import atomic

A, B, C = "0100000000010000", "0100000000020000", "0100000000030000"


class FolderNameTests(unittest.TestCase):
    def test_rules(self):
        cases = [
            (("The Legend of Zelda: Tears of the Kingdom", A, ()), "The Legend of Zelda Tears of the Kingdom"),   # 冒号删掉、空格合一
            (("  Pokémon™  Scarlet. ", A, ()), "Pokmon Scarlet"),                                                   # 非 ASCII 删、首尾空格和点去掉
            (("塞尔达传说", A, ()), A),                                                                               # 没有可用字符 → 游戏号
            (("CON", A, ()), A), (("nul.txt", A, ()), A),                                                           # Windows 保留名
            (("x" * 60, A, ()), "x" * 48),                                                                           # 最长 48
            (("Hades", B, ("hades",)), "Hades [0000]"),                                                              # 重名（不分大小写）→ 加号后 4 位
            (("Hades", "01000000000ABCD0", ("Hades",)), "Hades [BCD0]"),
            (("", A.lower(), ()), A),                                                                                # 游戏号统一大写
        ]
        for args, want in cases:
            with self.subTest(args=args):
                self.assertEqual(layout.folder_name(*args), want)


class MigrateTests(JobCase):
    def old_library(self):
        """v0.1.7 布局：A 有 Mac 现行 + 上一版运行版本、出包缓存、存档、着色器缓存、键位；B 只有封面和存档；C 名字全中文"""
        L = self.c.library
        (L / ".s2arm-layout.json").unlink(missing_ok=True)  # 删掉新库标记，模拟旧布局
        atomic(L / "catalog.json", dict(schema=1, games=[dict(id=A, name="Super Game: Deluxe"), dict(id=B, name="Other"), dict(id=C, name="中文游戏")]))
        rel = L / "games" / A / "releases"
        for name, host in (("k1-old", b"old host"), ("k2-cur", b"current host")):
            exe = rel / name / "S2ArmGame.app/Contents/MacOS/s2arm-host"; exe.parent.mkdir(parents=True); exe.write_bytes(host)
            (rel / name / "romfs").mkdir(); (rel / name / "romfs/data.bin").write_bytes(name.encode())
            atomic(rel / name / "manifest.json", dict(schema=1, files={"romfs/data.bin": [1, "x"], "S2ArmGame.app/Contents/MacOS/s2arm-host": [1, "y"]}))
        (L / "games" / A / "current").symlink_to("releases/k2-cur")
        (L / "games" / A / "cover.jpg").write_bytes(b"jpgA")
        (L / "games" / A / "custom.json").write_text('{"name":"我的"}')
        (L / "cache" / A / "key").mkdir(parents=True); (L / "cache" / A / "key/x").write_text("c")
        (L / "saves" / A / "bis").mkdir(parents=True); (L / "saves" / A / "bis/s.dat").write_text("saveA")
        (L / "save-backups" / A).mkdir(parents=True)
        (L / "runtime-cache" / A / "shaders").mkdir(parents=True)
        (L / "mappings").mkdir(); (L / "mappings" / (A + ".json")).write_text('{"k":1}')
        (L / "games" / B).mkdir(parents=True); (L / "games" / B / "cover.jpg").write_bytes(b"jpgB")
        (L / "saves" / B).mkdir(parents=True); (L / "saves" / B / "s").write_text("saveB")
        (L / "saves" / C).mkdir(parents=True)
        return L

    def test_migrates_per_game_and_splits_mac_host(self):
        L = self.old_library()
        self.assertTrue(layout.migrate(self.c))
        a, b, c = L / "games/Super Game Deluxe", L / "games/Other", L / ("games/" + C)
        self.assertEqual({p.name for p in (L / "games").iterdir()}, {a.name, b.name, c.name})
        for d, gid in ((a, A), (b, B), (c, C)):
            self.assertEqual(json.loads((d / "game.json").read_text())["id"], gid)
        self.assertEqual((a / "image/mac/romfs/data.bin").read_bytes(), b"k2-cur")      # 现行那份变成镜像
        self.assertFalse((a / "image/mac/S2ArmGame.app").exists())                     # 宿主拆出去了
        sha = hashlib.sha256(b"current host").hexdigest()[:12]
        self.assertEqual((L / "hosts/mac" / sha / "S2ArmGame.app/Contents/MacOS/s2arm-host").read_bytes(), b"current host")
        self.assertEqual(json.loads((a / "game.json").read_text())["hostMac"], sha)
        self.assertEqual(json.loads((a / "image/mac/manifest.json").read_text())["files"], {"romfs/data.bin": [1, "x"]})
        self.assertFalse((a / "releases").exists()); self.assertFalse((a / "current").exists())   # 上一版删掉
        self.assertEqual((a / "build/key/x").read_text(), "c")
        self.assertEqual((a / "saves/bis/s.dat").read_text(), "saveA")
        self.assertTrue((a / "save-backups").is_dir()); self.assertTrue((a / "shader-cache/shaders").is_dir())
        self.assertEqual((a / "input.json").read_text(), '{"k":1}')
        self.assertEqual((a / "cover.jpg").read_bytes(), b"jpgA"); self.assertEqual((a / "custom.json").read_text(), '{"name":"我的"}')
        self.assertEqual((b / "saves/s").read_text(), "saveB"); self.assertEqual((b / "cover.jpg").read_bytes(), b"jpgB")
        for gone in ("cache", "saves", "save-backups", "runtime-cache", "mappings"):
            self.assertFalse((L / gone).exists(), gone)
        self.assertEqual(json.loads((L / ".s2arm-layout.json").read_text())["version"], layout.VERSION)
        self.assertFalse(layout.migrate(self.c))                                        # 再跑：已迁完，什么都不动

    def test_chinese_only_name_takes_english_from_nacp(self):
        """旧清单只有中文名（C「中文游戏」）：到 iPhone 资源包里的 control.nacp 读美式英语标题做文件夹名；读不到才用游戏号"""
        L = self.old_library()
        nacp = bytearray(0x4000); title = "Chinese Game: Intl".encode(); nacp[:len(title)] = title   # ApplicationTitle[0] 美式英语
        pack = L / "iPhone" / C / "t资源包" / "ctl"; pack.mkdir(parents=True); (pack / "control.nacp").write_bytes(bytes(nacp))
        layout.migrate(self.c)
        self.assertEqual(json.loads((L / "games" / "Chinese Game Intl" / "game.json").read_text())["id"], C)

    def test_interrupted_migration_resumes(self):
        L = self.old_library()
        real = layout._split_release
        def boom(c, gid, d):
            if gid == B:
                raise OSError("断电")
            return real(c, gid, d)
        with patch.object(layout, "_split_release", side_effect=boom), self.assertRaises(OSError):
            layout.migrate(self.c)
        self.assertEqual(json.loads((L / ".s2arm-layout.json").read_text()), dict(version=1, games=[A]))
        self.c._layout_idx = None
        layout.migrate(self.c)
        self.assertEqual((L / "games/Other/saves/s").read_text(), "saveB")
        self.assertEqual((L / "games/Super Game Deluxe/saves/bis/s.dat").read_text(), "saveA")
        self.assertEqual(json.loads((L / ".s2arm-layout.json").read_text())["version"], layout.VERSION)

    def test_game_dir_found_by_game_json_not_folder_name(self):
        L = self.old_library(); layout.migrate(self.c)
        os.rename(L / "games/Other", L / "games/玩家改的名字")
        self.c._layout_idx = None
        self.assertEqual(layout.game_dir(self.c, B), L / "games/玩家改的名字")
        d = layout.game_dir(self.c, "0100000000040000", english="Other")   # 新款撞了别人原来的名字也行（按现存文件夹判重）
        self.assertEqual(d.name, "Other")


class Upgrade3Tests(JobCase):
    """2 → 3（_upgrade_3）：旧 iPhone/<号>/ 与 iPhone/container/ 迁进每款 image/ios、默认位置根 S2Arm.ipa、devices/legacy.json；中途被杀能续。期望按 layout 头注与迁移说明手写"""

    def v2_library(self):
        """v2 库：A 有容器件 + 资源包 + 壳号 + 单款 ipa；B 只有 ipa + 现行产物根 + 资源包（老玩家，没存过容器件）；
        C 已从游戏库删掉（清单里没有、也没有游戏文件夹），iPhone/C/ 还留着；旧容器 iPhone/container 装着 A、B"""
        L = self.c.library
        atomic(L / "catalog.json", dict(schema=1, games=[dict(id=A, name="Alpha"), dict(id=B, name="Beta")]))
        atomic(L / ".s2arm-layout.json", dict(version=2, games=[A, B]))
        for gid in (A, B):
            layout.game_dir(self.c, gid)
        a = L / "iPhone" / A
        (a / "container-kit").mkdir(parents=True); (a / "container-kit/main.npdm").write_bytes(b"META-A"); (a / "container-kit/main.fixups.tsv").write_text("")
        (a / "ta资源包").mkdir(); (a / "ta资源包/manifest.json").write_text('{"index_sha256": "pa"}')
        (a / "shell.id").write_text("SIDA\n"); (a / "ta.ipa").write_bytes(b"ipa")
        (a / "logs").mkdir(); (a / "logs/ipa.log").write_text("old ipa log")   # 出包日志：释放空间的保留项，迁移要留着
        b = L / "iPhone" / B; root = b / "roots/tb@pkg-1"
        (root / "romfs").mkdir(parents=True); (root / "romfs/huge.bin").write_bytes(b"x" * 100)
        (root / "main.npdm").write_bytes(b"META-B"); (root / "main.fixups.tsv").write_text(""); (root / "build.log").write_text("log")
        (b / "roots/tb@现行").symlink_to("tb@pkg-1")
        (b / "tb资源包").mkdir(); (b / "tb资源包/manifest.json").write_text('{"index_sha256": "pb"}')
        (b / "shell.id").write_text("SIDB"); (b / "tb.ipa").write_bytes(b"ipa")
        (L / "iPhone" / C / "tc资源包").mkdir(parents=True); (L / "iPhone" / C / "tc.ipa").write_bytes(b"ipa")
        box = L / "iPhone/container"; (box / "S2Arm.app").mkdir(parents=True)
        (box / "S2Arm.app/s2a_catalogue.json").write_text(json.dumps({"schema": 1, "shell_id": "SIDA", "games": [{"title_id": A}, {"title_id": B}]}))
        (box / "S2Arm.ipa").write_bytes(b"phone ipa"); (box / "bundle.txt").write_text("io.s2arm.games.team1\n")
        atomic(layout.path(self.c, A, "ios-install.json"), dict(at=100, free=True, device="甲机"))
        atomic(layout.path(self.c, B, "ios-install.json"), dict(at=200, free=False, device="乙机"))
        return L

    def check_migrated(self, L):
        import library
        ga, gb = layout.find_game(self.c, A), layout.find_game(self.c, B)
        self.assertEqual((ga / "image/ios/kit/main.npdm").read_bytes(), b"META-A")
        self.assertEqual((ga / "build/ios/logs/ipa.log").read_text(), "old ipa log")   # 旧出包日志搬到新的出包工作目录，没随 iPhone/ 删掉
        self.assertEqual(json.loads((ga / "image/ios/pack/manifest.json").read_text())["index_sha256"], "pa")
        self.assertEqual(json.loads((ga / "game.json").read_text())["ios"], dict(shellId="SIDA", pack="pa"))
        # B 没有容器件：先从现行产物根存一份（只要组装读的文件，不带 RomFS / 日志），再随 iPhone/ 删掉产物根
        kit = gb / "image/ios/kit"
        self.assertEqual(sorted(str(q.relative_to(kit)) for q in kit.rglob("*")), ["main.fixups.tsv", "main.npdm"])
        self.assertEqual(json.loads((gb / "game.json").read_text())["ios"], dict(shellId="SIDB", pack="pb"))
        self.assertTrue(library.iphone_ready(self.c, A)); self.assertTrue(library.iphone_ready(self.c, B))
        self.assertIsNone(layout.find_game(self.c, C)); self.assertFalse(any((L / "games").glob("*" + C + "*")))   # 已删的款不新建文件夹
        self.assertEqual((L / "S2Arm.ipa").read_bytes(), b"phone ipa")
        self.assertEqual(json.loads((L / "devices/legacy.json").read_text()),
                         dict(schema=1, games={A: {}, B: {}}, shellId="SIDA", bundleId="io.s2arm.games.team1", failedShells=[],
                              signed=dict(at=200, free=False, device="乙机")))   # 各款已装记录取最新一次并进来
        self.assertFalse(layout.path(self.c, A, "ios-install.json", create=False).exists())
        self.assertFalse(layout.path(self.c, B, "ios-install.json", create=False).exists())
        self.assertFalse((L / "iPhone").exists())   # 单款 ipa、产物根、底包都能重建：随 iPhone/ 删掉
        self.assertEqual(json.loads((L / ".s2arm-layout.json").read_text()), dict(version=layout.VERSION, games=[A, B], iphone=[A, B, C]))   # 3 之后接着迁到当前版本（_upgrade_4）
        # 第一台装机的设备认领迁移来的记录
        self.assertEqual(layout.load_device(self.c, "phone-1")["games"], {A: {}, B: {}})
        self.assertFalse((L / "devices/legacy.json").exists()); self.assertTrue((L / "devices/phone-1.json").is_file())
        self.assertEqual(layout.install_record(self.c, B), dict(at=200, free=False, device="乙机"))

    def test_upgrade_moves_iphone_outputs_into_each_game(self):
        L = self.v2_library()
        self.assertTrue(layout.migrate(self.c))
        self.check_migrated(L)
        self.assertFalse(layout.migrate(self.c))   # 再跑：已迁完

    def test_upgrade_interrupted_resumes(self):
        """B 写 game.json 时被杀：标记里只记了 A（version 仍是 2），iPhone/ 与旧容器都还在；再跑接着做完，结果与一次做完相同"""
        L = self.v2_library()
        real = layout.update_info
        def boom(c, gid, **fields):
            if gid.upper() == B and "ios" in fields:
                raise OSError("断电")
            return real(c, gid, **fields)
        with patch.object(layout, "update_info", side_effect=boom), self.assertRaises(OSError):
            layout.migrate(self.c)
        self.assertEqual(json.loads((L / ".s2arm-layout.json").read_text()), dict(version=2, games=[A, B], iphone=[A]))
        self.assertTrue((L / "iPhone/container/S2Arm.ipa").is_file()); self.assertFalse((L / "devices").exists())
        self.c._layout_idx = None
        self.assertTrue(layout.migrate(self.c))
        self.check_migrated(L)

    def test_iphone_ready_needs_kit_npdm_and_pack_manifest(self):
        """⑥ iphone_ready：容器件 main.npdm 在且非空、有改写产物表、资源包清单在才是 True；缺任一项 False"""
        import library
        from jobcase import ios_fixture
        atomic(self.c.library / "catalog.json", dict(schema=1, games=[dict(id=A, name="Alpha")]))
        self.assertFalse(library.iphone_ready(self.c, A))   # 没出过 iPhone 版（连文件夹都没有）
        kit, pack = ios_fixture(self.c, A)
        self.assertTrue(library.iphone_ready(self.c, A))
        cases = (("缺 main.npdm", lambda: (kit / "main.npdm").unlink()), ("main.npdm 是空的", lambda: (kit / "main.npdm").write_bytes(b"")),
                 ("缺资源包清单", lambda: (pack / "manifest.json").unlink()), ("缺改写产物表", lambda: (kit / "main.fixups.tsv").unlink()))
        for name, breaks in cases:
            with self.subTest(name):
                ios_fixture(self.c, A); breaks()
                self.assertFalse(library.iphone_ready(self.c, A))


class LocationTests(JobCase):
    """游戏库结构 v3：多个存放位置 + 索引表。期望按设计文档的规则手写"""
    def setUp(self):
        super().setUp()
        atomic(self.c.library / "catalog.json", dict(schema=1, games=[dict(id=A, name="Alpha"), dict(id=B, name="Beta")]))
        self.disk = self.root / "移动硬盘" / "S2Arm"

    def fresh(self):
        self.c._layout_idx = None   # 新作业进程：索引从磁盘读
        return self.c

    def test_game_in_second_location_found_through_index(self):
        d = layout.game_dir(self.c, A, location=self.disk)
        self.assertEqual(d, self.disk / "games" / "Alpha")
        self.assertEqual(json.loads((self.disk / ".s2arm-location.json").read_text())["schema"], 1)
        self.assertEqual(layout.state(self.fresh(), A), ("ok", d))
        self.assertEqual(layout.root_of(self.c, A), self.disk)
        layout.game_dir(self.c, B)   # 没指定位置、没用过别的位置 → 默认位置
        self.assertEqual(layout.find_game(self.fresh(), B), self.c.library / "games" / "Beta")

    def test_renamed_folder_offline_location_and_missing_folder(self):
        d = layout.game_dir(self.c, A, location=self.disk)
        os.rename(d, d.with_name("改过的名字"))
        self.assertEqual(layout.state(self.fresh(), A), ("ok", self.disk / "games" / "改过的名字"))   # 扫这个位置认回来
        os.rename(self.disk, self.disk.with_name("拔掉了"))
        self.assertEqual(layout.state(self.fresh(), A), ("offline", self.disk))
        with self.assertRaises(Exception):
            layout.game_dir(self.c, A)                                                             # 位置没连上不新建
        os.rename(self.disk.with_name("拔掉了"), self.disk)
        os.rename(self.disk / "games" / "改过的名字", self.root / "挪走了")
        self.assertEqual(layout.state(self.fresh(), A), ("missing", self.disk))
        with self.assertRaises(Exception):
            layout.relink(self.c, B, self.root / "挪走了")                                         # 游戏号对不上：拒
        back = layout.relink(self.c, A, self.root / "挪走了")                                      # 在 games 外：搬回原来的位置
        self.assertEqual(back, self.disk / "games" / "挪走了"); self.assertFalse((self.root / "挪走了").exists())
        self.assertEqual(layout.state(self.fresh(), A), ("ok", back))
        (self.c.library / "games").mkdir(exist_ok=True)
        os.rename(back, self.c.library / "games" / "Alpha 副本")
        layout.relink(self.c, A, self.c.library / "games" / "Alpha 副本")                         # 已在某个位置的 games 下：原地认
        self.assertEqual(layout.state(self.fresh(), A), ("ok", self.c.library / "games" / "Alpha 副本"))

    def test_move_same_volume_and_cross_volume_with_check(self):
        d = layout.game_dir(self.c, A)
        (d / "saves").mkdir(); (d / "saves" / "s.dat").write_text("存档")
        moved = layout.move_game(self.c, A, self.disk)                                            # 同卷：改名
        self.assertEqual((moved / "saves" / "s.dat").read_text(), "存档"); self.assertFalse(d.exists())
        self.assertEqual(layout.find_game(self.fresh(), A), moved)
        with patch.object(layout, "_same_volume", return_value=False):                            # 跨卷：拷、核对、删旧
            back = layout.move_game(self.c, A, self.c.library)
        self.assertEqual((back / "saves" / "s.dat").read_text(), "存档"); self.assertFalse(moved.exists())
        self.assertEqual(layout.find_game(self.fresh(), A), back)

    def test_cross_volume_move_keeps_links_as_links(self):
        """跨卷移动不把链接展开（Windows junction 同理，Windows 实机另验）：产物根的「现行」这类相对链接原样；指向文件夹内部的绝对链接改指新位置"""
        d = layout.game_dir(self.c, A)
        root = d / "build" / "k" / "roots"; (root / "t@pkg").mkdir(parents=True); (root / "t@pkg" / "main.npdm").write_bytes(b"n")
        (root / "t@现行").symlink_to("t@pkg")                                   # 相对
        (d / "abs-link").symlink_to(root / "t@pkg")                            # 绝对、指向文件夹内部
        with patch.object(layout, "_same_volume", return_value=False):
            moved = layout.move_game(self.c, A, self.disk)
        r = moved / "build" / "k" / "roots"
        self.assertTrue((r / "t@现行").is_symlink()); self.assertEqual(os.readlink(r / "t@现行"), "t@pkg")
        self.assertTrue((moved / "abs-link").is_symlink()); self.assertEqual(Path(os.readlink(moved / "abs-link")), r / "t@pkg")
        self.assertEqual((r / "t@现行" / "main.npdm").read_bytes(), b"n")

    def test_last_used_location_is_default_for_new_games(self):
        layout.use_location(self.c, self.disk)
        self.assertEqual(layout.game_dir(self.fresh(), B).parent.parent, self.disk)


class HostPruneTests(JobCase):
    """公共宿主清理（host_update.prune）：别的存放位置里的游戏在用的宿主不能删；有位置没连上就不清"""
    def test_prune_skips_files_in_games_root(self):
        import host_update

        game = layout.game_dir(self.c, A, english="Alpha")
        used, unused = "aaaaaaaaaaaa", "bbbbbbbbbbbb"
        layout.update_info(self.c, A, hostMac=used)
        hosts = layout.hosts_mac(self.c)
        for sha in (used, unused):
            (hosts / sha / host_update.APP).mkdir(parents=True)

        files = [game.parent / name for name in (".DS_Store", "notes.txt")]
        for path in files:
            path.write_bytes(b"metadata")

        host_update.prune(self.c)

        self.assertEqual({p.name for p in hosts.iterdir()}, {used})
        for path in files:
            self.assertEqual(path.read_bytes(), b"metadata")

    def test_hosts_used_in_other_locations_survive_and_offline_skips(self):
        import host_update
        atomic(self.c.library / "catalog.json", dict(schema=1, games=[dict(id=A, name="Alpha"), dict(id=B, name="Beta")]))
        disk = self.root / "盘2" / "S2Arm"
        layout.game_dir(self.c, A); layout.game_dir(self.c, B, location=disk)
        for sha in ("aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"):
            (layout.hosts_mac(self.c) / sha / "S2ArmGame.app").mkdir(parents=True)
        layout.update_info(self.c, A, hostMac="aaaaaaaaaaaa"); layout.update_info(self.c, B, hostMac="bbbbbbbbbbbb")
        os.rename(disk, disk.with_name("拔掉"))
        host_update.prune(self.c)                                                   # 盘 2 没连上：一个都不删
        self.assertEqual(sorted(p.name for p in layout.hosts_mac(self.c).iterdir()), ["aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"])
        os.rename(disk.with_name("拔掉"), disk)
        host_update.prune(self.c)                                                   # 都连上：只删没人用的 c
        self.assertEqual(sorted(p.name for p in layout.hosts_mac(self.c).iterdir()), ["aaaaaaaaaaaa", "bbbbbbbbbbbb"])


class VolumeNameTests(unittest.TestCase):
    """存放位置下拉框 / 「未连接」里的盘名：Windows 按盘符（原先只认 Mac 的 /Volumes，Windows 每个盘都叫系统盘）"""
    def test_windows_and_mac_names(self):
        import library
        cases = [("C:\\S2Arm", "", "系统盘"), ("D:\\S2Arm", "Games", "Games（D:）"), ("e:\\x", "", "E 盘"), ("\\\\nas\\share\\S2Arm", "", "\\\\nas\\share")]
        for path, label, want in cases:
            with self.subTest(path=path), patch.object(library, "os", OsView()), patch.dict(os.environ, {"SystemDrive": "C:"}), \
                 patch.object(library, "_win_volume_label", return_value=label):
                self.assertEqual(library._volume(path), want)
        self.assertEqual(library._volume(os.path.join(os.sep, "Volumes", "移动硬盘", "S2Arm")), "移动硬盘")


if __name__ == "__main__":
    unittest.main()


class HealFolderNamesTests(JobCase):
    """还叫游戏号的文件夹（v0.2.0 及以前导入的中文名游戏：读取器不给英文名、拷封面时这款还没进 catalog）在作业开始时按英文名改名。
    期望按规则手写：A 有 Mac 产物 NACP「Hollow Knight: Silksong」→ 「Hollow Knight Silksong」、catalog 记 englishName、索引跟着改；
    B 中文名、没有任何产物 → 留着游戏号；C 名字本来就是英文 → 「Hades」；有任务拿着执行锁时一个都不改"""
    def setUp(self):
        super().setUp()
        L = self.c.library
        atomic(L / "catalog.json", dict(schema=1, games=[dict(id=A, name="空洞骑士：丝之歌"), dict(id=B, name="泰拉瑞亚"), dict(id=C, name="Hades")]))
        for gid in (A, B, C):
            (L / "games" / gid).mkdir(parents=True)
            atomic(L / "games" / gid / "game.json", dict(schema=1, id=gid))
            (L / "games" / gid / "cover.jpg").write_bytes(gid.encode())
        nacp = bytearray(0x4000); t = "Hollow Knight: Silksong".encode(); nacp[:len(t)] = t
        ctl = L / "games" / A / "image" / "mac" / "ctl"; ctl.mkdir(parents=True); (ctl / "control.nacp").write_bytes(bytes(nacp))

    def fresh(self):
        self.c._layout_idx = None
        return self.c

    def test_renames_when_english_name_known(self):
        L = self.c.library
        self.assertEqual(layout.heal_folder_names(self.fresh()), 2)
        self.assertEqual(sorted(p.name for p in (L / "games").iterdir()), sorted([B, "Hollow Knight Silksong", "Hades"]))
        self.assertEqual((L / "games" / "Hollow Knight Silksong" / "cover.jpg").read_bytes(), A.encode())
        self.assertEqual(layout.find_game(self.fresh(), A), L / "games" / "Hollow Knight Silksong")
        self.assertEqual(layout.find_game(self.fresh(), C), L / "games" / "Hades")
        names = {g["id"]: g.get("englishName") for g in json.loads((L / "catalog.json").read_text())["games"]}
        self.assertEqual(names, {A: "Hollow Knight: Silksong", B: None, C: "Hades"})
        self.assertEqual(layout.heal_folder_names(self.fresh()), 0)   # 再跑：只剩 B（没有英文名），不动

    def test_skips_while_a_job_holds_the_execution_lock(self):
        import filelock
        with (self.c.library / ".execution.lock").open("a") as f:
            filelock.try_lock(f)
            self.assertEqual(layout.heal_folder_names(self.fresh()), 0)
            filelock.unlock(f)
        self.assertEqual(sorted(p.name for p in (self.c.library / "games").iterdir()), sorted([A, B, C]))


    def test_rename_refused_leaves_everything_untouched(self):
        # Windows 上文件夹里有被占用的文件时改名失败：这款整款不动——文件夹、索引、catalog 的 englishName 都留原样（不能只改一半）
        real = os.replace
        def busy(a, b, *args, **kwargs):
            if Path(a).name == A:
                raise PermissionError(32, "being used by another process")
            return real(a, b, *args, **kwargs)
        with patch.object(layout.os, "replace", busy):
            self.assertEqual(layout.heal_folder_names(self.fresh()), 1)   # 只有 C（Hades）改成
        L = self.c.library
        self.assertTrue((L / "games" / A).is_dir())
        self.assertEqual(layout.find_game(self.fresh(), A), L / "games" / A)
        names = {g["id"]: g.get("englishName") for g in json.loads((L / "catalog.json").read_text())["games"]}
        self.assertEqual(names, {A: None, B: None, C: "Hades"})


class StoreMetaTests(JobCase):
    """游戏库的商店元数据（library.store_meta）：商品页一次请求取横图 + 英文标题，存进游戏库，各平台出包只读这一份。打桩替掉 banner_fetch，零联网。
    期望按规则手写：B「泰拉瑞亚」（NACP 没有英文标题、文件夹是游戏号）拿到 Terraria + 横图 → catalog 记 englishName、横图进文件夹、
    接着 heal_folder_names 改名为「Terraria」且横图跟着走；C 的商店标题是日文 → 不记；A 已有英文名和横图 → 不查；断网（D）只记 storeChecked；
    7 天内再跑不再查；S2A_BANNER_FETCH=0 不查"""
    D = "0100000000040000"

    def setUp(self):
        super().setUp()
        L = self.c.library
        atomic(L / "catalog.json", dict(schema=1, games=[dict(id=A, name="空洞骑士：丝之歌", englishName="Hollow Knight Silksong"),
                                                       dict(id=B, name="泰拉瑞亚"), dict(id=C, name="ハデス"), dict(id=self.D, name="离线游戏")]))
        for gid in (A, B, C, self.D):
            (L / "games" / gid).mkdir(parents=True)
            atomic(L / "games" / gid / "game.json", dict(schema=1, id=gid))
        (L / "games" / A / "banner.jpg").write_bytes(b"old")
        self.calls = []
        names = {B: "Terraria", C: "ハデス"}
        test = self

        class Fake:
            @staticmethod
            def fetch_banner(gid, dest):
                test.calls.append(gid)
                if gid == test.D:
                    return False, "商品页拉取失败：timed out"
                Path(dest).write_bytes(b"JPG-" + gid.encode()); return True, "1920x1080"

            @staticmethod
            def store_name(gid):
                return names.get(gid, "")
        for p in (patch.dict(os.environ, {"S2A_BANNER_FETCH": "1"}), patch("library.engine_tool", lambda c, name: Fake)):
            p.start(); self.addCleanup(p.stop)

    def fresh(self):
        self.c._layout_idx = None
        return self.c

    def catalog(self):
        return {g["id"]: g for g in json.loads((self.c.library / "catalog.json").read_text())["games"]}

    def test_fetch_records_and_rename_follows(self):
        import library
        L = self.c.library
        self.assertEqual(library.store_meta(self.fresh()), 3)
        self.assertEqual(sorted(self.calls), sorted([B, C, self.D]))                 # A 都有了：不查
        cat = self.catalog()
        self.assertEqual({g: cat[g].get("englishName") for g in cat}, {A: "Hollow Knight Silksong", B: "Terraria", C: None, self.D: None})
        self.assertTrue(all(cat[g].get("storeChecked") for g in (B, C, self.D)))
        self.assertEqual((L / "games" / B / "banner.jpg").read_bytes(), b"JPG-" + B.encode())
        self.assertFalse((L / "games" / self.D / "banner.jpg").exists())              # 断网：只记查过
        self.assertEqual((L / "games" / A / "banner.jpg").read_bytes(), b"old")
        layout.heal_folder_names(self.fresh())
        self.assertEqual(layout.find_game(self.fresh(), B), L / "games" / "Terraria")
        self.assertEqual((L / "games" / "Terraria" / "banner.jpg").read_bytes(), b"JPG-" + B.encode())
        self.calls.clear()
        self.assertEqual(library.store_meta(self.fresh()), 0)                          # 7 天内查过：不再查
        self.assertEqual(self.calls, [])

    def test_import_ids_only_and_switch_off(self):
        import library
        self.assertEqual(library.store_meta(self.fresh(), {B}), 1)                     # 导入刚加的款：只查它
        self.assertEqual(self.calls, [B])
        self.calls.clear()
        with patch.dict(os.environ, {"S2A_BANNER_FETCH": "0"}):
            self.assertEqual(library.store_meta(self.fresh(), {C}), 0)
        self.assertEqual(self.calls, [])

    def test_job_start_spawns_background_but_not_for_launch(self):
        # 作业开始：改名同步做（不联网），查商店交后台；起局连后台也不起
        import library
        spawned = []
        with patch("library.store_meta_background", lambda c, ids=None: spawned.append(c.operation)):
            self.c.operation = "launch"; layout.ensure(self.fresh())
            self.c.operation = "build"; layout.ensure(self.fresh())
        self.assertEqual(spawned, ["build"])
        self.assertEqual(self.calls, [])   # 作业本身没联网

    def test_background_entry_fetches_then_renames(self):
        # 后台子进程入口（store_meta_bg.main）：查到英文名和横图记进游戏库，接着把文件夹改名
        import store_meta_bg
        L = self.c.library
        rc = store_meta_bg.main([str(self.c.private), str(L), self.c.r.get("libraryId") or "", str(self.c.engine)])
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(self.calls), sorted([B, C, self.D]))
        self.assertEqual((L / "games" / "Terraria" / "banner.jpg").read_bytes(), b"JPG-" + B.encode())

    def test_spawn_args_and_skips(self):
        import library
        popen = []
        with patch("subprocess.Popen", lambda args, **kw: popen.append(args)):
            self.assertTrue(library.store_meta_background(self.fresh(), {B}))
            self.assertEqual(popen[-1][3:5], [str(Path(library.__file__).with_name("store_meta_bg.py")), str(self.c.private)])
            self.assertEqual(popen[-1][-1], B)                                          # 只查导入刚加的款
            self.assertFalse(library.store_meta_background(self.fresh(), {A}))          # A 都有了：不起
            with patch.dict(os.environ, {"S2A_BANNER_FETCH": "0"}):
                self.assertFalse(library.store_meta_background(self.fresh(), {B}))
        self.assertEqual(len(popen), 1)


class CoverFallbackTests(JobCase):
    """catalog 的封面三级（期望按规则手写）：游戏包自带图标 cover.jpg → 商店横图 banner.jpg → 空（界面用 S2Arm 标志）"""
    def test_cover_then_banner_then_empty(self):
        L = self.c.library
        atomic(L / "catalog.json", dict(schema=1, games=[dict(id=A, name="甲", packages=[]), dict(id=B, name="乙", packages=[]), dict(id=C, name="丙", packages=[])]))
        for gid, files in ((A, ("cover.jpg", "banner.jpg")), (B, ("banner.jpg",)), (C, ())):
            d = L / "games" / gid; d.mkdir(parents=True); atomic(d / "game.json", dict(schema=1, id=gid))
            for f in files: (d / f).write_bytes(f.encode())
        result, done = self.worker("catalog")
        self.assertEqual(done["state"], "succeeded", result.stderr)
        covers = {g["id"]: g["cover"] for g in done["result"]["games"]}
        self.assertEqual(covers, {A: str(L / "games" / A / "cover.jpg"), B: str(L / "games" / B / "banner.jpg"), C: ""})

