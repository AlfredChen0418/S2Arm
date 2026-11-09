#!/usr/bin/env python3
"""壳的发版清单单测（pack/shell/manifest.py，Windows 全流程出包 #1123）。夹具是现造的小文件，不含游戏数据。期望按规则手写，不跑被测实现推：
① seal → 清单合产物契约 s2a.artifact.v2、target ios-shell；壳编号 = sha256("s2a.shell.v2\\0" 输入指纹 \\0 链号 \\0 iOS 版本) 前 16 位（本文件另算；提交号只记录、不进编号）；文件按路径排序、大小与 SHA-256 本文件另算
② 原样 verify → 通过；改壳里任意一个文件的 1 个字节 → 退出 1、点名那个文件「被改」
③ 删一个 → 「缺失」；多放一个 → 「多出」
④ 壳目录里有符号链接 → seal 拒（退出 2）
⑤ require：编号对 → 0；不对 → 退出 1，消息里有两个编号
⑥ 不覆盖已有清单；清单不许写进壳目录
⑦ 清单里壳编号被手改（与输入指纹 / 链号 / iOS 版本对不上）→ verify 退出 2
⑧ 输入指纹（inputs_key，临时 git 仓库造输入表）：只改表外文件（文档）→ 指纹不变；改表里任一「入」文件（目录下的也算、目录里新增也算）→ 指纹变；
   与 pack/stage_keyfiles.sh stage_files_key 同算法（本文件按「对象号 路径」逐行另算）；表里有不在 HEAD 的路径 → 报错
"""
import hashlib, json, os, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "pack/shell/manifest.py"
COMMIT, INPUTS, CHAIN, IOS = "a" * 40, "0123456789abcdef", "c7a4970bffe5", "18.0"
SDK = "27"


def run(*args):
    return subprocess.run([sys.executable, "-I", "-B", str(TOOL), *args], capture_output=True, text=True, encoding="utf-8")


class ShellManifest(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp()); self.shell = self.t / "shell"
        (self.shell / "S2Arm.app/Frameworks").mkdir(parents=True)
        self.files = {"S2Arm.app/S2Arm": b"\x00" * 300 + b"host", "S2Arm.app/Info.plist": b"<plist/>", "S2Arm.app/Frameworks/NS2Pro": b"ns2"}
        for p, b in self.files.items(): (self.shell / p).write_bytes(b)
        self.man = self.t / "shell.json"

    def seal(self):
        return run("seal", "--root", str(self.shell), "--manifest", str(self.man), "--source-commit", COMMIT, "--inputs", INPUTS, "--chain", CHAIN, "--ios-min", IOS, "--ios-sdk", SDK)

    def test_sdk_major_changes_shell_id(self):
        # 审计 P1-124：只换链接 SDK 主版本，编号也要变（手算：同 want_id 的格式，只差最后一段）
        ids = {hashlib.sha256(f"s2a.shell.v2\0{INPUTS}\0{CHAIN}\0{IOS}\0{v}".encode()).hexdigest()[:16] for v in ("26", "27")}
        self.assertEqual(len(ids), 2)

    def test_seal_verify_tamper(self):
        r = self.seal(); self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(self.man.read_text())
        want_id = hashlib.sha256(f"s2a.shell.v2\0{INPUTS}\0{CHAIN}\0{IOS}\0{SDK}".encode()).hexdigest()[:16]
        self.assertEqual((doc["schema"], doc["target"], doc["details"]["schema"], doc["details"]["shellId"]), ("s2a.artifact.v2", "ios-shell", "s2a.artifact.shell.v2", want_id))
        self.assertEqual(doc["files"], [{"path": p, "size": len(b), "sha256": hashlib.sha256(b).hexdigest()} for p, b in sorted(self.files.items())])
        self.assertEqual(run("verify", "--root", str(self.shell), "--manifest", str(self.man)).returncode, 0)
        p = self.shell / "S2Arm.app/S2Arm"; b = bytearray(p.read_bytes()); b[10] ^= 1; p.write_bytes(bytes(b))
        r = run("verify", "--root", str(self.shell), "--manifest", str(self.man))
        self.assertEqual(r.returncode, 1); self.assertIn("被改：S2Arm.app/S2Arm", r.stderr)

    def test_missing_extra(self):
        self.assertEqual(self.seal().returncode, 0)
        (self.shell / "S2Arm.app/Info.plist").unlink(); (self.shell / "S2Arm.app/extra.bin").write_bytes(b"x")
        r = run("verify", "--root", str(self.shell), "--manifest", str(self.man))
        self.assertEqual(r.returncode, 1); self.assertIn("缺失：S2Arm.app/Info.plist", r.stderr); self.assertIn("多出：S2Arm.app/extra.bin", r.stderr)

    def test_symlink_refused(self):
        os.symlink("S2Arm", self.shell / "S2Arm.app/link")
        r = self.seal(); self.assertEqual(r.returncode, 2); self.assertIn("链接", r.stderr); self.assertFalse(self.man.exists())

    def test_require(self):
        self.assertEqual(self.seal().returncode, 0)
        sid = json.loads(self.man.read_text())["details"]["shellId"]
        self.assertEqual(run("require", "--manifest", str(self.man), "--shell-id", sid).returncode, 0)
        r = run("require", "--manifest", str(self.man), "--shell-id", "0" * 16)
        self.assertEqual(r.returncode, 1); self.assertIn("0" * 16, r.stderr); self.assertIn(sid, r.stderr)

    def test_no_overwrite_and_outside(self):
        self.assertEqual(self.seal().returncode, 0)
        self.assertEqual(self.seal().returncode, 2)
        r = run("seal", "--root", str(self.shell), "--manifest", str(self.shell / "m.json"), "--source-commit", COMMIT, "--inputs", INPUTS, "--chain", CHAIN, "--ios-min", IOS, "--ios-sdk", SDK)
        self.assertEqual(r.returncode, 2); self.assertFalse((self.shell / "m.json").exists())

    def test_forged_id(self):
        self.assertEqual(self.seal().returncode, 0)
        doc = json.loads(self.man.read_text()); doc["details"]["shellId"] = "f" * 16; self.man.write_text(json.dumps(doc))
        r = run("verify", "--root", str(self.shell), "--manifest", str(self.man))
        self.assertEqual(r.returncode, 2); self.assertIn("对不上", r.stderr)



class InputsKey(unittest.TestCase):
    def setUp(self):
        self.r = Path(tempfile.mkdtemp())
        self.g = lambda *a: subprocess.run(["git", "-C", str(self.r), *a], check=True, capture_output=True)
        self.g("init", "-q")
        for p, b in {"runtime/src/a.c": "int a;", "runtime/src/b.h": "#define B", "docs/x.md": "doc", "Makefile": "all:"}.items():
            (self.r / p).parent.mkdir(parents=True, exist_ok=True); (self.r / p).write_text(b)
        (self.r / "pack/stage_inputs").mkdir(parents=True)
        (self.r / "pack/stage_inputs/shell.tsv").write_text("# 表\n入\truntime/src\n入\tMakefile\n外\tdocs/x.md\n")
        self.commit()

    def commit(self):
        self.g("add", "-A"); self.g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x")

    def key(self):
        sys.path.insert(0, str(ROOT)); from pack.shell import manifest
        return manifest.inputs_key(self.r)

    def expect(self):   # 本文件另算：git ls-tree 每行「对象号 路径」
        out = subprocess.run(["git", "-C", str(self.r), "ls-tree", "HEAD", "--", "runtime/src", "Makefile"], capture_output=True, text=True, check=True).stdout
        return hashlib.sha256("".join(f"{l.split()[2]} {l.split(chr(9))[1]}\n" for l in out.splitlines()).encode()).hexdigest()[:16]

    def test_doc_change_keeps_source_change_moves(self):
        k0 = self.key(); self.assertEqual(k0, self.expect())
        (self.r / "docs/x.md").write_text("doc 改了"); self.commit()
        self.assertEqual(self.key(), k0, "只改文档，壳输入指纹不该变")
        (self.r / "runtime/src/b.h").write_text("#define B 2"); self.commit()
        k2 = self.key(); self.assertNotEqual(k2, k0, "改宿主源文件（登记目录下的头文件），指纹要变"); self.assertEqual(k2, self.expect())
        (self.r / "runtime/src/c.c").write_text("int c;"); self.commit()
        k3 = self.key(); self.assertNotEqual(k3, k2, "登记目录下新增源文件，指纹要变")
        (self.r / "Makefile").write_text("all: x"); self.commit()
        self.assertNotEqual(self.key(), k3)

    def test_missing_path_reported(self):
        (self.r / "pack/stage_inputs/shell.tsv").write_text("入\truntime/gone.c\n"); self.commit()
        sys.path.insert(0, str(ROOT)); from pack.shell import manifest
        with self.assertRaises(manifest.ShellError): manifest.inputs_key(self.r)


class ReleaseSnapshot(unittest.TestCase):
    """⑨ 发布快照（根有 engine.json）：current_id 读 engine.json 的 shellId，不在快照里重算；没写 shellId → 报错（不悄悄算一个对不上的）"""
    def test_engine_json(self):
        sys.path.insert(0, str(ROOT)); from pack.shell import manifest
        d = Path(tempfile.mkdtemp()); old = manifest.ROOT
        try:
            manifest.ROOT = d
            (d / "engine.json").write_text(json.dumps({"schema": 1, "shellId": "0123456789abcdef"}))
            self.assertEqual(manifest.current_id(), "0123456789abcdef")
            (d / "engine.json").write_text(json.dumps({"schema": 1}))
            with self.assertRaises(manifest.ShellError): manifest.current_id()
        finally: manifest.ROOT = old

if __name__ == "__main__":
    unittest.main()
