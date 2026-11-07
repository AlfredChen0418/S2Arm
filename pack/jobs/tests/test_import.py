"""导入游戏（pack/jobs/library.py import_games）单测（从 test_error_recovery.py 拆出，10-26；夹具 jobcase.JobCase）。"""
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from jobcase import JobCase, synthetic_nacp, synthetic_npdm, synthetic_nup

APP = 0x0400486024DEC000   # 时之笛的程序号（合成容器，内容是测试字节）


class DigestTests(unittest.TestCase):   # 11-06：导入的包身份 = 原件整份 SHA-256；Windows 走系统 CNG，结果必须与 hashlib 逐位相同
    def test_digest_equals_hashlib_sha256_and_reports_progress(self):
        import hashlib, os, tempfile
        import core
        with tempfile.TemporaryDirectory() as t:
            for n in (0, 1, (4 << 20) - 1, 4 << 20, (4 << 20) * 2 + 12345):   # 空文件、块边界前后、多块加零头
                p = Path(t) / f"f{n}"; data = os.urandom(n); p.write_bytes(data)
                seen = []
                self.assertEqual(core.digest(p, lambda d, total: seen.append((d, total))), hashlib.sha256(data).hexdigest(), n)
                if n: self.assertEqual(seen[-1], (n, n))


class ImportTests(JobCase):
    def test_complete_metadata_does_not_hide_truncation(self):
        self.import_keys()
        self.install_reader_fixture()
        source = self.root / "source.nsp"
        source.write_bytes(b"truncated fixture")
        _, terminal = self.worker("import", paths=[str(source)])
        self.assert_failure(terminal, "content_truncated")
        self.assertEqual(self.c.catalog()["games"], [])

    def test_multi_content_file_with_truncated_content_is_rejected_whole(self):
        # 有意：导入把一个文件当一个整体，被截断的内容之后出包会缺东西，所以整个文件拒绝，
        # 不留下半个包。解包流程仍只跳过被截断的内容（行为不变，见 tests/nx_unpack_fail_test.sh）。
        self.import_keys()
        self.install_reader_fixture()
        source = self.root / "combined.nsp"
        source.write_bytes(b"multi truncated fixture")
        _, terminal = self.worker("import", paths=[str(source)])
        self.assert_failure(terminal, "content_truncated")
        self.assertEqual(self.c.catalog()["games"], [])

    def test_truncated_key_candidate_is_not_evidence_against_keys(self):
        self.import_keys()
        self.install_reader_fixture()
        source = self.root / "source.nsp"
        source.write_bytes(b"complete fixture")
        _, terminal = self.worker("import", paths=[str(source)])
        self.assertEqual(terminal["state"], "succeeded")
        source.write_bytes(b"truncated fixture")
        _, terminal = self.worker("keys", path=str(self.c.keys))
        self.assertEqual(terminal["state"], "succeeded")
        self.assertFalse(terminal["result"]["verified"])

    def test_ticket_missing_key_candidate_is_not_evidence_against_keys(self):
        # #242：库里候选游戏缺票据，同一份格式有效的密钥重新导入要成功、记未验证；再导这款游戏时照报缺票据
        self.import_keys()
        self.install_reader_fixture()
        source = self.root / "source.nsp"
        source.write_bytes(b"complete fixture")
        _, terminal = self.worker("import", paths=[str(source)])
        self.assertEqual(terminal["state"], "succeeded")
        source.write_bytes(b"ticket fixture")
        _, terminal = self.worker("keys", path=str(self.c.keys))
        self.assertEqual(terminal["state"], "succeeded")
        self.assertFalse(terminal["result"]["verified"])
        _, terminal = self.worker("import", paths=[str(source)])
        self.assert_failure(terminal, "ticket_missing")

    def test_missing_keys_outdated_keys_and_missing_ticket_are_distinct(self):
        self.install_reader_fixture()
        source = self.root / "source.nsp"
        source.write_bytes(b"outdated fixture")
        _, terminal = self.worker("import", paths=[str(source)])
        self.assert_failure(terminal, "keys_missing")
        self.import_keys()
        _, terminal = self.worker("import", paths=[str(source)])
        self.assert_failure(terminal, "keys_outdated")
        source.write_bytes(b"ticket fixture")
        _, terminal = self.worker("import", paths=[str(source)])
        diagnostic = self.assert_failure(terminal, "ticket_missing")
        self.assertNotIn("prod.keys", diagnostic["summary"])
        self.assertNotIn("重新导出", diagnostic["suggestion"])

    def test_import_folder_follows_symlinked_files_and_folders(self):
        # 原件挪走、原位置留软链（10-26 维护者把 Downloads 原件挪外置盘）：文件夹里指向文件的软链、软链目录都认；软链成环不转圈
        self.import_keys(); self.install_reader_fixture()
        real = self.root / "moved"; (real / "sub").mkdir(parents=True)
        (real / "source.nsp").write_bytes(b"synthetic game fixture")
        (real / "sub" / "other.nsp").write_bytes(b"synthetic game fixture 2")
        picked = self.root / "downloads"; picked.mkdir()
        (picked / "source.nsp").symlink_to(real / "source.nsp")
        (picked / "sub").symlink_to(real / "sub")
        (picked / "loop").symlink_to(picked)
        result, done = self.worker("import", paths=[str(picked)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(done["state"], "succeeded")
        # 原件原地只读（11-02）：不复制进游戏库，清单记原件的绝对路径（软链所在的路径，出包时照样跟到目标）
        self.assertFalse((self.c.library / "sources").exists())
        paths = sorted(p["path"] for g in self.c.catalog()["games"] for p in g["packages"])
        self.assertEqual(paths, sorted({str(picked.resolve() / "source.nsp"), str(picked.resolve() / "sub" / "other.nsp")}))
        self.assertEqual(sorted(Path(p).read_bytes() for p in paths), [b"synthetic game fixture", b"synthetic game fixture 2"])

    def test_readd_original_after_managed_copy_removed(self):
        # beta.3 及以前复制进游戏库的托管副本：副本还在时重放原件照旧用副本；「删除托管 NSP 副本」之后重放原件，改记成原件位置（否则这款一直缺文件）
        import json
        self.import_keys(); self.install_reader_fixture()
        original = self.root / "drive" / "game.nsp"; original.parent.mkdir(); original.write_bytes(b"synthetic game fixture")
        def run():
            result, done = self.worker("import", paths=[str(original)])
            self.assertEqual(done["state"], "succeeded", result.stderr)
            return [p["path"] for g in self.c.catalog()["games"] for p in g["packages"]]
        [path] = run()
        managed = self.c.library / "sources" / (Path(path).name + ".managed")
        managed.parent.mkdir(); managed.write_bytes(original.read_bytes())
        catalog = json.loads((self.c.library / "catalog.json").read_text())
        for g in catalog["games"]:
            for p in g["packages"]:
                p["path"] = str(managed.relative_to(self.c.library))
        (self.c.library / "catalog.json").write_text(json.dumps(catalog))
        self.assertEqual(run(), [str(managed.relative_to(self.c.library))])
        managed.unlink()
        self.assertEqual(run(), [str(original.resolve())])


class NupImportTests(JobCase):
    """Switch 2 NUP 容器导入（11-07）：pack/ns2_nup.py 认（不看扩展名）、不要 prod.keys；Switch 1 原件照旧要密钥、走读取器"""

    def nup(self, path, nacp=True):
        path.parent.mkdir(parents=True, exist_ok=True)
        synthetic_nup(path, APP, [
            (0, APP, 0, {"main": b"\x01" * 300, "main.npdm": synthetic_npdm(APP)}, {"a.bin": b"base"}, {}),
            (1, APP | 0x800, 131072, {"main": b"\x02" * 500, "main.npdm": synthetic_npdm(APP)}, {"a.bin": b"upd"},
             {2: synthetic_nacp("Ocarina T", "1.2.0"), 3: b"JPGICON"} if nacp else {}),
            (2, APP + 0x1001, 0, None, {"d.bin": b"DLC"}, {})])
        return path

    def test_nup_imported_without_prod_keys(self):
        # ① 没导 prod.keys、读取器也没装：.nup 照样收进库——本体 / 最新更新 / DLC 各一条、同一原件、format=nup；
        #   标题与显示版本取容器里的 NACP，封面取容器里的图标；密钥状态不动（没用到密钥）
        src = self.nup(self.root / "drive" / "Zelda T [0400486024dec000][v131072].nup")
        self.assertFalse(self.c.keys.exists())
        result, done = self.worker("import", paths=[str(src)])
        self.assertEqual(done["state"], "succeeded", result.stderr + str(done.get("diagnostics")))
        [game] = self.c.catalog()["games"]
        rows = sorted((p["type"], p["titleId"], p["version"], p["path"], p.get("format"), p["display"]) for p in game["packages"])
        self.assertEqual(rows, sorted([("base", "0400486024DEC000", 0, str(src.resolve()), "nup", "1.2.0"),
                                       ("update", "0400486024DEC800", 131072, str(src.resolve()), "nup", "1.2.0"),
                                       ("dlc", "0400486024DED001", 0, str(src.resolve()), "nup", "1.2.0")]))
        self.assertEqual((game["id"], game["name"]), ("0400486024DEC000", "Ocarina T"))
        self.assertEqual((self.c.library / "games/0400486024DEC000/cover.jpg").read_bytes(), b"JPGICON")
        self.assertFalse((self.c.private / "keys/status.json").exists())
        # 没带控制分区：标题取文件名去掉「 [程序号][版本]」（同一键出包）
        bare = self.nup(self.root / "drive2" / "Bare Name [0400486024dec000].nup", nacp=False)
        (self.c.library / "catalog.json").write_text('{"schema": 1, "games": []}')
        result, done = self.worker("import", paths=[str(bare)])
        self.assertEqual(done["state"], "succeeded", result.stderr)
        self.assertEqual(self.c.catalog()["games"][0]["name"], "Bare Name")

    def test_fake_nup_rejected(self):
        # ② 扩展名是 .nup 但不是 NUP（文件头不是 NUP 魔数）：直接选它照旧报「不支持的类型」；文件夹里混着真假两个 → 只收真的
        fake = self.root / "drive" / "fake.nup"; fake.parent.mkdir(); fake.write_bytes(b"not a NUP container at all")
        result, done = self.worker("import", paths=[str(fake)])
        self.assert_failure(done, "input_type_unsupported")
        real = self.nup(self.root / "drive" / "real.nup")
        result, done = self.worker("import", paths=[str(self.root / "drive")])
        self.assertEqual(done["state"], "succeeded", result.stderr + str(done.get("diagnostics")))
        self.assertEqual({p["path"] for g in self.c.catalog()["games"] for p in g["packages"]}, {str(real.resolve())})

    def test_switch1_nsp_import_unchanged(self):
        # ④ Switch 1 NSP：没有密钥照旧 keys_missing；有密钥走读取器（--inspect 的元数据原样记，不带 format），密钥状态记已验证
        import json
        nsp = self.root / "drive" / "game.nsp"; nsp.parent.mkdir(); nsp.write_bytes(b"synthetic game fixture")
        result, done = self.worker("import", paths=[str(nsp)])
        self.assert_failure(done, "keys_missing")
        self.import_keys(); self.install_reader_fixture()
        result, done = self.worker("import", paths=[str(nsp)])
        self.assertEqual(done["state"], "succeeded", result.stderr)
        [game] = self.c.catalog()["games"]
        [pkg] = game["packages"]
        self.assertEqual((game["id"], pkg["type"], pkg["path"], "format" in pkg, pkg["guestIsa"]),
                         ("0100000000000000", "base", str(nsp.resolve()), False, "aarch64"))
        self.assertTrue(json.loads((self.c.private / "keys/status.json").read_text())["verified"])


if __name__ == "__main__":
    unittest.main()
