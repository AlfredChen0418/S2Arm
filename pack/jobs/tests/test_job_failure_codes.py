"""#774：真实子进程退出后的具体错误、装机分类及玩家恢复码闭包；全为合成输入。"""
import ast
import contextlib
import importlib.util
import io
import subprocess
import json
import re
from pathlib import Path
import plistlib
import shlex
import sys
import os
import unittest
from unittest.mock import patch
import zipfile

from jobcase import JobCase, JOBS, ROOT
from core import Context, Failure, Cancelled, atomic
from errors import RECOVERY
import build
import ipa
import install
import library


def oneclick_stages(source):
    # lap 的真实调用；复用解包路径会重复解包名，末尾无参数 lap 只收计时。
    return tuple(dict.fromkeys(re.findall(r"(?m)^\s*lap\s+([^;\s]+)", source)))


REAL_ONECLICK_STAGES = oneclick_stages((ROOT / "pack/oneclick/oneclick.sh").read_text())


class PackageFailures(JobCase):
    def setUp(self):
        super().setUp()
        self.engine = self.root / "engine"
        script = self.engine / "pack/oneclick/oneclick.sh"
        script.parent.mkdir(parents=True)
        self.script = script
        self.fonts = self.root / "fonts"; self.fonts.mkdir()
        source = self.c.library / "sources/base.nsp"
        source.parent.mkdir(exist_ok=True); source.write_bytes(b"synthetic container")
        pkg = dict(titleId="0100000000000000", baseId="0100000000000000", type="base", version=0,
                   path="sources/base.nsp", sha256="a" * 64, guestIsa="aarch64", size=19)
        atomic(self.c.library / "catalog.json", dict(schema=1, games=[dict(id="0100000000000000", name="Fixture", packages=[pkg])]))
        self.c.keys.parent.mkdir(parents=True, exist_ok=True)
        self.c.keys.write_text("header_key = " + "0" * 64 + "\n")
        self.c.engine = self.engine
        self.c.r.update(gameId="0100000000000000")
        self.extraction = self.root / "shared-extract"; self.extraction.mkdir()   # 共用解包目录（build.ensure_extract）桩成夹具，真解由 Mac 打包单测覆盖

    def script_result(self, phase, message="synthetic reason", ok=False, refresh_lines=None):
        self.assertIn(phase, REAL_ONECLICK_STAGES)
        events = []
        for real in REAL_ONECLICK_STAGES:
            events.append(dict(ev="stage", name=real))
            events.append(dict(ev="stage_end", name=real, ok=False if real == phase else True))
            if real == phase: break
        events.append(dict(ev="result", ok=ok, msg=message))
        self.script.write_text("#!/bin/bash\nwhile [ $# -gt 0 ]; do case \"$1\" in --events) E=$2; shift 2;; --out) O=$2; shift 2;; *) shift;; esac; done\n"
                               + ("mkdir -p \"$O/logs\"\nprintf '%s\\n' " + shlex.quote("\n".join(refresh_lines)) + ' > "$O/logs/refresh.log"\n' if refresh_lines is not None else "")
                               + "printf '%s\\n' " + " ".join(shlex.quote(json.dumps(e, ensure_ascii=False)) for e in events) + ' > "$E"\n'
                               + "echo 'synthetic tool stderr' >&2\nexit 17\n")

    def build(self):
        with patch.object(ipa, "dotnet_sdk", return_value=(self.root, Path(sys.executable))), \
             patch.object(ipa, "resolve_target", return_value={"gpuArch": "applegpu_g18p", "os": 27}), \
             patch.object(ipa, "_iml_device_hint"), patch.object(self.c, "fonts_dir", return_value=self.fonts), \
             patch.object(library, "check_pack_space", return_value=None), \
             patch.object(build, "ensure_extract", return_value=self.extraction), patch.object(build, "seed_from_sibling", return_value=False):
            return ipa.build(self.c)

    def test_oneclick_failures_keep_reason_exit_code_and_one_diagnostic(self):
        # 独立手写预期，不从被测分类表生成；调用真实 c.run / bash / JSONL 读取。
        expected = {"认游戏": "package_failed", "解包": "content_invalid", "播种": "resource_preparation_failed",
                    "刷新各阶段": "package_failed", "出包": "package_failed", "交付": "package_failed",
                    "安卓出包": "package_failed", "安装与导入": "package_failed"}
        for phase in REAL_ONECLICK_STAGES:
            code = expected[phase]
            with self.subTest(phase=phase):
                self.c.job["diagnostics"].clear()
                self.script_result(phase)
                with self.assertRaises(Failure) as got:
                    self.build()
                self.assertEqual(got.exception.code, code)
                self.assertIn("synthetic reason", got.exception.internal)
                self.c.report_failure(got.exception)
                ds = self.c.job["diagnostics"]
                self.assertEqual(len(ds), 1)
                self.assertEqual(ds[0]["exitCode"], 17)
                self.assertEqual(ds[0]["code"], code)
                # 11-07 维护者：阶段名 / 原因原文不上屏，只进 detail（复制错误摘要）
                self.assertNotIn("synthetic reason", ds[0]["summary"])
                self.assertNotIn("（" + phase + "）", ds[0]["summary"])
                self.assertIn("synthetic reason", ds[0]["detail"])

    def test_real_packaging_substeps_from_producer_prefixes(self):
        # 这些是 pack/ipa.sh 与它调的收尾 pack/ipa_tail.py（11-04 Windows 出包线从 ipa.sh 搬出）的 fail 前缀，不是新增或虚构的 lap 阶段。
        source = (ROOT / "pack/ipa.sh").read_text() + (ROOT / "pack/ipa_tail.py").read_text()
        for detail, code in (("签名失败（profile missing）", "signing_failed"),
                             ("自签失败（codesign error）", "signing_failed"),
                             ("签名核实失败：权限不符", "signing_failed"),
                             ("出壳失败（编译器报签名失败字段）", "compilation_failed"),
                             ("预编壳组装失败（写外置镜像包失败）", "compilation_failed"),
                             ("组装没产出 fixture.app", "compilation_failed"),
                             ("资源包失败（缺文件）", "resource_preparation_failed"),
                             ("未知失败（detail内含签名失败）", "package_failed")):
            with self.subTest(detail=detail):
                self.c.job["diagnostics"].clear()
                if code != "package_failed": self.assertIn(detail.split("（")[0].split("：")[0].split(" fixture")[0], source)
                reason = "出包失败：【红】fixture 出包：" + detail
                self.script_result("出包", reason)
                with self.assertRaises(Failure) as got: self.build()
                self.assertEqual(got.exception.code, code)
                self.assertIn(reason, got.exception.internal)
                self.c.report_failure(got.exception)
                self.assertEqual(len(self.c.job["diagnostics"]), 1)
                self.assertEqual(self.c.job["diagnostics"][-1]["exitCode"], 17)

        # iml_check.py 和 verify.py 的真实报错再被 ipa.sh.fail 包一层红标记。
        for detail, literal, producer, expected in (
                ("【红】签名里没有「提高内存上限」权限（synthetic）", "【红】签名里没有「提高内存上限」权限", "pack/iml_check.py", "signing_failed"),
                ("【红】签名含私有 JIT 键 ['synthetic']，出包拒绝", "【红】签名含私有 JIT 键", "pack/iml_check.py", "signing_failed"),
                ("🔴 资源包核对 x：SHA 不一致", "🔴 资源包核对", "pack/verify.py", "resource_preparation_failed")):
            with self.subTest(producer=producer, detail=detail):
                self.assertIn(literal, (ROOT / producer).read_text())
                self.c.job["diagnostics"].clear()
                reason = "出包失败：【红】fixture 出包：" + detail
                self.script_result("出包", reason)
                with self.assertRaises(Failure) as got: self.build()
                self.assertEqual(got.exception.code, expected)
                self.assertIn(reason, got.exception.internal)
                self.c.report_failure(got.exception)
                self.assertEqual(len(self.c.job["diagnostics"]), 1)
                self.assertEqual(self.c.job["diagnostics"][-1]["exitCode"], 17)

    def test_actual_producer_errors_feed_real_job(self):
        # 运行真实 IML 读取及判定；夹具为维护方生成的不含私密信息的 ad hoc 签名。
        spec = importlib.util.spec_from_file_location("job_fixture_iml", ROOT / "pack/iml_check.py")
        iml = importlib.util.module_from_spec(spec); spec.loader.exec_module(iml)
        errors = []
        app = self.root / "Probe.app"; app.mkdir()
        (app / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleExecutable": "Probe", "CFBundleIdentifier": "org.s2arm.synthetic"}))
        for fixture in ("iml_missing", "iml_banned"):
            (app / "Probe").write_bytes((ROOT / "tests/fixtures/iml_signed" / fixture).read_bytes())
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = iml._check_app(str(app))
            self.assertEqual(code, 1)
            errors.append((output.getvalue().strip(), "signing_failed"))
        # 真实资源核对脚本读取合成的错误版本manifest，自行产生原始红行。
        bad = self.root / "invalid-pack"; bad.mkdir(); (bad / "manifest.json").write_text('{"version": 1}')
        src = self.root / "empty-source"; src.mkdir()
        checked = subprocess.run([sys.executable, str(ROOT / "pack/verify.py"), str(bad), "romfs=" + str(src)], capture_output=True, text=True, timeout=10)
        self.assertEqual(checked.returncode, 1, checked.stderr)
        self.assertTrue(checked.stdout.startswith("🔴 资源包核对"))
        errors.append((checked.stdout.strip(), "resource_preparation_failed"))
        for raw, expected in errors:
            with self.subTest(raw=raw):
                reason = "出包失败：【红】fixture 出包：" + raw
                self.script_result("出包", reason)
                with self.assertRaises(Failure) as got: self.build()
                self.assertEqual(got.exception.code, expected)
                self.assertIn(raw, got.exception.internal)

    def test_real_refresh_log_restores_filtered_stop_reason(self):
        cases = (("【红】fixture 刷新停在直连配置推导（rc=1）：推导内部异常", "configuration_failed"),
                 ("【红】fixture 刷新停在 rewrite 阶段（rc=1），现行 = fixture", "rewrite_failed"),
                 ("【红】fixture 刷新停在抽取阶段（rc=1）：缺少文件", "shader_extract_failed"),
                 ("【红】fixture 刷新停在 sysdata 阶段（rc=1）", "package_failed"))
        for red, code in cases:
            with self.subTest(code=code):
                self.script_result("刷新各阶段", "刷新停在：filtered detail（logs/refresh.log）", refresh_lines=[red, "计时行不是根因"])
                with self.assertRaises(Failure) as got: self.build()
                self.assertEqual(got.exception.code, code)
                self.assertIn("filtered detail", got.exception.internal)
                self.assertIn(red, got.exception.internal)

    def test_refresh_log_cannot_reuse_stale_or_nonterminal_red(self):
        log = self.c.library / "iPhone/0100000000000000/logs/refresh.log"; log.parent.mkdir(parents=True)
        log.write_text("【红】fixture 刷新停在直连配置推导（rc=1）：旧错误\n")
        self.script_result("刷新各阶段", "本轮未知错误")
        with self.assertRaises(Failure) as got: self.build()
        self.assertEqual(got.exception.code, "package_failed")
        self.assertNotIn("旧错误", got.exception.internal)
        self.script_result("刷新各阶段", "本轮最终错误", refresh_lines=["【红】fixture 刷新停在直连配置推导（rc=1）：早期行", "【红】fixture 刷新停在 sysdata 阶段（rc=2）"])
        with self.assertRaises(Failure) as got: self.build()
        self.assertEqual(got.exception.code, "package_failed")
        self.assertNotIn("早期行", got.exception.internal)

    def test_nonzero_with_success_result_is_still_failure(self):
        self.script_result("出包", "must not claim successful delivery", ok=True)
        with self.assertRaises(Failure) as got:
            self.build()
        self.assertEqual(got.exception.code, "package_failed")
        self.assertNotIn("must not claim successful delivery", got.exception.internal)

    def test_no_event_preserves_unknown_reason_and_control_failures(self):
        self.script.write_text("#!/bin/bash\necho 'actual compiler failure' >&2\nexit 19\n")
        with self.assertRaises(Failure) as got:
            self.build()
        self.assertEqual(got.exception.code, "package_failed")
        self.assertIn("actual compiler failure", got.exception.internal)
        for code in ("stage_timeout", "tool_missing"):
            with patch.object(self.c, "run", side_effect=Failure("keep me", code=code)):
                with self.assertRaises(Failure) as got:
                    self.build()
                self.assertEqual((got.exception.code, str(got.exception)), (code, "keep me"))
        with patch.object(self.c, "run", side_effect=Cancelled()):
            with self.assertRaises(Cancelled): self.build()

    def test_result_without_stage_and_zero_exit_false_result(self):
        self.script_result("解包", "specific reason without stage")
        script = self.script.read_text()
        # 任务正文的最小夹具：只有 result，没有阶段记录；未知阶段也必须带真实原因。
        lines = [json.dumps(dict(ev="result", ok=False, msg="specific reason without stage"))]
        self.script.write_text("#!/bin/bash\nwhile [ $# -gt 0 ]; do if [ \"$1\" = --events ]; then E=$2; break; fi; shift; done\n"
                               + "printf '%s\\n' " + shlex.quote(lines[0]) + ' > "$E"\nexit 1\n')
        with self.assertRaises(Failure) as got: self.build()
        self.assertEqual(got.exception.code, "package_failed")
        self.assertIn("specific reason without stage", got.exception.internal)
        self.script.write_text(script.replace("exit 17", "exit 0"))
        with self.assertRaises(Failure) as got: self.build()
        self.assertEqual(got.exception.code, "content_invalid")
        self.assertIn("specific reason without stage", got.exception.internal)

    def test_completed_stage_is_not_later_failure_cause(self):
        events = self.root / "events.jsonl"
        for ok, expected in ((True, "package_failed"), (False, "content_invalid")):
            events.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in [
                dict(ev="stage", name="解包"), dict(ev="stage_end", name="解包", ok=ok),
                dict(ev="result", ok=False, msg="subsequent real reason")]))
            got = ipa.oneclick_failure(events, None, None)
            self.assertEqual(got.code, expected)
            self.assertIn("subsequent real reason", got.internal)

    def test_worker_reports_specific_failed_result(self):
        self.script_result("解包", "bad synthetic container")
        entry = self.root / "worker.py"
        entry.write_text("import sys\nfrom pathlib import Path\nsys.path.insert(0, " + repr(str(JOBS)) + ")\n"
                         "import main, ipa, library\nfrom core import Context\n"
                         "ipa.dotnet_sdk = lambda: (Path(sys.prefix), Path(sys.executable))\n"
                         "ipa.resolve_target = lambda c, required=True: {'gpuArch': 'applegpu_g18p', 'os': 27}\n"
                         "ipa._iml_device_hint = lambda *a: None\n"
                         "library.check_pack_space = lambda *a, **kw: None\n"
                         "import build\nbuild.ensure_extract = lambda *a: Path(" + repr(str(self.extraction)) + ")\nbuild.seed_from_sibling = lambda *a: False\n"
                         "Context.fonts_dir = lambda c: Path(" + repr(str(self.fonts)) + ")\n"
                         "raise SystemExit(main.main())\n")
        result, terminal = self.worker("ipa", entrypoint=entry, engineDir=str(self.engine), gameId="0100000000000000")
        self.assertEqual(result.returncode, 1, result.stderr)
        diagnostic = self.assert_failure(terminal, "content_invalid")
        self.assertIn("bad synthetic container", diagnostic["detail"])
        self.assertEqual(diagnostic["summary"], "文件内容不对：文件结构校验没通过，可能没下完整。")
        self.assertEqual(diagnostic["exitCode"], 17)
        self.assertEqual(len(terminal["diagnostics"]), 1)


class MacFailures(JobCase):
    def test_signing_and_allowed_nonzero_refresh(self):
        self.c.operation = "build"
        signing = self.root / "codesign"
        signing.write_text("#!/bin/sh\n echo 'synthetic signature failure' >&2\nexit 4\n"); signing.chmod(0o755)
        self.c.milestone = "host"
        with self.assertRaises(Failure) as got: self.c.run([str(signing)])
        self.assertEqual(got.exception.code, "signing_failed")
        self.assertIn("synthetic signature failure", got.exception.internal)
        refresh = self.root / "refresh.sh"; refresh.write_text("exit 3\n")
        self.c.job["diagnostics"].clear()
        self.c.run(["bash", refresh], ok_codes=(0, 3))
        self.assertEqual(self.c.last_rc, 3)
        self.assertEqual(self.c.job["diagnostics"], [])

    def test_build_commands_have_specific_codes_other_jobs_unchanged(self):
        self.c.operation = "build"
        refresh = self.root / "pack/refresh.sh"; refresh.parent.mkdir()
        refresh.write_text("echo 'synthetic Metal compiler error' >&2\nexit 17\n")
        import milestones
        milestones.plan(self.c, 0, "build_dual")
        milestones.enter(self.c, "refresh")
        # 刷新没有终止红行（判不出停在哪个子阶段）：不猜，归 package_failed（11-07 起没有通用着色器子阶段可借）
        with self.assertRaises(Failure) as got:
            self.c.run(["bash", refresh])
        self.assertEqual(got.exception.code, "package_failed")
        self.assertIn("synthetic Metal compiler error", got.exception.internal)
        # build.prepare 每次 refresh 前进入 refresh；未知的旧 host 标签也不得误归编译。
        self.c.milestone = "host"
        with self.assertRaises(Failure) as got: self.c.run(["bash", refresh])
        self.assertEqual(got.exception.code, "package_failed")
        # 残留 milestone 不应把任意后续命令误叫成编译失败。
        self.c.milestone = "host"
        with self.assertRaises(Failure) as got: self.c.run([sys.executable, "-c", "raise SystemExit(7)"])
        self.assertEqual(got.exception.code, "package_failed")
        self.c.operation = "import"
        with self.assertRaises(Failure) as got: self.c.run(["bash", refresh])
        self.assertEqual(got.exception.code, "tool_exit")


class InstallFailures(JobCase):
    def test_install_rc_four_causes_and_unknown(self):
        import diag
        gid = "0100000000000000"
        out = self.c.library / "iPhone" / gid; out.mkdir(parents=True)
        archive = out / "test.ipa"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("Payload/Test.app/Info.plist", plistlib.dumps({"CFBundleIdentifier": "test"}))
            z.writestr("Payload/Test.app/S2Arm", b"bin")
        (out / "test资源包").mkdir()   # 有资源包才装（审计 18:00 P1-58：没有资源包先报 pack_missing、不走到装机）
        self.c.r.update(gameId=gid)
        import ipa as packer
        def fake_assemble(c, g, root=None, dest=None):
            return archive, [gid], [gid]
        def unpack(args, **kw):
            with zipfile.ZipFile(archive) as z: z.extractall(args[-1])
            import subprocess
            return subprocess.CompletedProcess(args, 0, "", "")
        cases = (("The maximum number of apps for free development profiles has been reached", "free_app_limit"),
                 ("Developer Mode is disabled on this device", "developer_mode_required"),
                 ("The pairing record is invalid. Please pair the device again", "device_pairing_required"),
                 ("The provisioning profile has expired", "provisioning_expired"),
                 ("synthetic unknown installation error", "device_install_failed"))
        for raw, code in cases:
            with self.subTest(code=code), patch.object(install, "xcode_teams", return_value=[dict(id="TESTTEAM", name="Test", free=True)]), \
                 patch.object(install, "phone", return_value=dict(id="fixture", name="Synthetic")), \
                 patch.object(install, "udid_of", return_value=""), patch.object(install, "pull_seen"), \
                 patch.object(install, "backup_saves"), patch.object(packer, "assemble", side_effect=fake_assemble), \
                 patch.object(self.c, "run"), \
                 patch.object(install.subprocess, "run", side_effect=unpack), \
                 patch.object(diag, "devicectl", return_value=(9, {}, raw)):
                with self.assertRaises(Failure) as got: install.install(self.c)
                self.assertEqual(got.exception.code, code)
                self.assertIn(raw, str(got.exception))

    def test_resource_transfer_preserves_control_failures(self):
        pack = self.root / "pack"; pack.mkdir(); (pack / "manifest.json").write_text('{"version": 2, "index_sha256": "x"}')
        for code in ("stage_timeout", "tool_missing"):
            with patch.object(self.c, "run", side_effect=Failure("specific original", code=code)):
                with self.assertRaises(Failure) as got: install.push_pack(self.c, "fixture", "fixture.app", pack)
                self.assertEqual((got.exception.code, str(got.exception)), (code, "specific original"))
        def failed(*args, **kw):
            log = Path(args[1]); log.parent.mkdir(parents=True, exist_ok=True); log.write_text("synthetic cable disconnect\n")
            raise Failure("generic", code="tool_exit")
        with patch.object(self.c, "run", side_effect=failed):
            with self.assertRaises(Failure) as got: install.push_pack(self.c, "fixture", "fixture.app", pack)
        self.assertEqual(got.exception.code, "resource_transfer_failed")
        self.assertIn("synthetic cable disconnect", str(got.exception))

    def test_classification_does_not_guess_from_unrelated_expiry_or_maximum(self):
        for raw, expected in (("Your session has expired", "xcode_account_missing"),
                              ("reached the maximum number of retries", "signing_failed"),
                              ("Developer Mode is enabled; network failed", "signing_failed"),
                              ("certificate has expired", "signing_failed"),
                              ("provisioning profile has a valid expiration date", "signing_failed")):
            with self.subTest(raw=raw): self.assertEqual(install.classify(raw), expected)
        self.assertNotIn("AltStore", RECOVERY["free_app_limit"])
        self.assertIn("7", RECOVERY["free_app_limit"])


class LogCauses(JobCase):
    """11-07 现场（联合开发者 Mac 启动器旷野之息）：直连库出库写盘失败只报「刷新停在 direct 阶段（rc=1）」+ package_failed。
    日志夹具按真实产出格式手写：refresh.sh 终止红行、pack/stage_direct.sh 转述 direct_lib.sh 的「🔴 出库失败 rc=…（<build.log>）」、.NET 未处理异常。"""
    GID = "01007EF00011E000"
    STOP = "【红】01007EF00011E000 刷新停在 direct 阶段（rc=1），现行 = 01007EF00011E000@ex-b3eed688"

    def mac_refresh(self, refresh_lines, milestone="refresh"):
        import milestones
        self.c.operation = "build"
        refresh = self.root / "pack/refresh.sh"; refresh.parent.mkdir(exist_ok=True)
        refresh.write_text("printf '%s\\n' " + shlex.quote("\n".join(refresh_lines)) + "\nexit 1\n")
        milestones.plan(self.c, 0, "build_dual")
        milestones.enter(self.c, milestone)
        self.c.job["diagnostics"].clear()
        with self.assertRaises(Failure) as got:
            self.c.run(["bash", refresh])
        return got.exception, self.c.job["diagnostics"][-1]

    def direct_build_log(self, body):
        # 玩家机缓存在「Application Support」下，路径带空格（Store.swift privateRoot）
        work = self.root / "Application Support/S2Arm/run/s2arm/cache/direct" / self.GID / "work.stage.4242"
        work.mkdir(parents=True)
        log = work / "build.log"; log.write_text(body)
        return log

    def direct_refresh_lines(self, build_log):
        return ["⏱ 01007EF00011E000 刷新分阶段用时（秒，并行段各记各的墙钟）：direct 812；刷新共 900 秒",
                "【红】01007EF00011E000 直连库出库失败（rc=1，[直连库] 🔴 出库失败 rc=1（%s））" % build_log,
                self.STOP]

    def test_direct_stage_dotnet_disk_full_reads_build_log(self):
        log = self.direct_build_log(
            "[直连库] 第 4/5 步 出库：41971 支，8 路并行\n"
            "Unhandled exception. System.IO.IOException: No space left on device : '/w/tmp/dsh_batch/spill.dat'\n"
            "   at Microsoft.Win32.SafeHandles.SafeFileHandle.Init(String path, FileMode mode)\n"
            "   at System.IO.Strategies.OSFileStreamStrategy.Write(ReadOnlySpan`1 buffer)\n"
            "   at S2A.Dsh.Batch.Spill.Flush()\n"
            "Traceback (most recent call last):\n"
            "subprocess.CalledProcessError: Command '['dotnet', 'dsh_batch.dll', 'build']' returned non-zero exit status 134.\n")
        error, d = self.mac_refresh(self.direct_refresh_lines(log))
        self.assertEqual((error.code, d["code"]), ("disk_full", "disk_full"))
        self.assertEqual(d["summary"], "磁盘空间不够：准备这个游戏需要更多空余空间。")
        for internal in ("rc=", "direct", self.GID, "ex-b3eed688", "【红】"):
            self.assertNotIn(internal, d["summary"])
        self.assertEqual(d["suggestion"], RECOVERY["disk_full"])
        # 原文 / 阶段 / rc / 归因依据照旧进 detail（「复制错误摘要」带走）
        for evidence in ("刷新停在 direct 阶段（rc=1）", "No space left on device", "build.log", "归因 disk_full"):
            self.assertIn(evidence, d["detail"])

    def test_chain_mismatch_from_newer_xcode_says_update_s2arm(self):
        # 11-07 联合开发者现场（beta.4 + Xcode 27.2，metal 32023.922 比锁 32023.921 新）：direct_lib.sh 把编译器提示当链号读，
        #   报「链号前后不一（c7a4970bffe5 / note:）」→ 刷新停在 direct 阶段。上屏：这版 S2Arm 不认识你的 Xcode；下一步：更新 S2Arm
        from errors import RECOVERY
        error, d = self.mac_refresh(["[直连库] 🔴 链号前后不一（c7a4970bffe5 / note:）", self.STOP])
        self.assertEqual(error.code, "engine_toolchain_newer")
        self.assertEqual(d["summary"], "这版 S2Arm 不认识你的 Xcode：你的 Xcode 比这版 S2Arm 新。")
        self.assertEqual(d["suggestion"], RECOVERY["engine_toolchain_newer"])
        for internal in ("rc=", "c7a4970bffe5", "note:", "direct"):
            self.assertNotIn(internal, d["summary"])

    def test_direct_stage_without_readable_cause_asks_for_summary(self):
        # 子日志读不到（已清 / 路径截断）：说不清是 direct 的哪一步，不猜——package_failed，下一步请玩家复制错误摘要
        error, d = self.mac_refresh(self.direct_refresh_lines(self.root / "gone/work.stage.1/build.log"))
        self.assertEqual(error.code, "package_failed")
        self.assertIn("复制错误摘要", d["suggestion"])

    def test_cause_beyond_bounded_tail_is_not_read(self):
        # 子日志只读尾 32000 字节：根因行之后再有 40 KB 输出就读不到，按阶段归（射程有界的反证）
        log = self.direct_build_log("System.IO.IOException: No space left on device\n" + ("x" * 99 + "\n") * 400)
        error, _ = self.mac_refresh(self.direct_refresh_lines(log))
        self.assertEqual(error.code, "package_failed")

    def test_python_errno_28_in_fresh_refresh_log(self):
        events = self.root / "events.jsonl"
        events.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in [
            dict(ev="stage", name="刷新各阶段"), dict(ev="stage_end", name="刷新各阶段", ok=False),
            dict(ev="result", ok=False, msg="刷新停在：【红】fixture sysdata 写不出（logs/refresh.log）")]))
        refresh = self.root / "out/logs/refresh.log"; refresh.parent.mkdir(parents=True)
        before = ipa._log_identity(refresh)
        refresh.write_text("Traceback (most recent call last):\n"
                           "  File \"pack/sysdata.py\", line 88, in write\n"
                           "OSError: [Errno 28] No space left on device: '/w/roots/fixture/sysdata.bin'\n"
                           "【红】fixture 刷新停在 sysdata 阶段（rc=1），现行 = fixture@ex-1\n")
        got = ipa.oneclick_failure(events, None, None, refresh_log=refresh, refresh_before=before)
        self.assertEqual(got.code, "disk_full")
        self.assertEqual(str(got), "磁盘空间不够：准备这个游戏需要更多空余空间。")
        self.assertIn("[Errno 28]", got.internal)
        # 同一份 refresh.log 若不是本轮写的（身份没变），不读、不归因
        got = ipa.oneclick_failure(events, None, None, refresh_log=refresh, refresh_before=ipa._log_identity(refresh))
        self.assertEqual(got.code, "package_failed")

    def test_windows_dotnet_not_enough_space(self):
        events = self.root / "events.jsonl"
        events.write_text(json.dumps(dict(ev="result", ok=False, msg="刷新停在：【红】fixture 直连库出库失败（rc=1）")))
        log = self.root / "ipa.log"
        log.write_text("Unhandled exception. System.IO.IOException: There is not enough space on the disk. : 'T:\\work\\tmp\\spill.dat'\n"
                       "   at System.IO.RandomAccess.WriteAtOffset(SafeFileHandle handle, ReadOnlySpan`1 buffer, Int64 fileOffset)\n")
        got = ipa.oneclick_failure(events, None, log)
        self.assertEqual((got.code, str(got)), ("disk_full", "磁盘空间不够：准备这个游戏需要更多空余空间。"))

    def test_metal_missing_beats_direct_stage(self):
        for line in ('xcrun: error: unable to find utility "metal", not a developer tool or in PATH',
                     "error: cannot execute tool 'metal' due to missing Metal Toolchain; use: xcodebuild -downloadComponent MetalToolchain"):
            with self.subTest(line=line):
                error, d = self.mac_refresh([line, "【红】fixture 刷新停在 direct 阶段（rc=1），现行 = fixture@ex-1"], milestone="direct")
                self.assertEqual(error.code, "metal_unavailable")
                self.assertEqual(d["summary"], "缺少 Metal 组件：编着色器要用的 Metal 组件没装上。")

    def test_other_missing_tool(self):
        error, _ = self.mac_refresh(["pack/stage_x.sh: line 9: spirv-cross: command not found", "【红】fixture 刷新停在 caps 阶段（rc=1），现行 = fixture@ex-1"])
        self.assertEqual(error.code, "tool_missing")

    def test_unrecognized_cause_asks_for_error_summary(self):
        error, d = self.mac_refresh(["【红】fixture 刷新停在 sysdata 阶段（rc=1），现行 = fixture@ex-1"])
        self.assertEqual(d["code"], "package_failed")
        self.assertEqual(d["summary"], "出包未完成。")
        self.assertIn("「复制错误摘要」", d["suggestion"])
        self.assertIn("刷新停在 sysdata 阶段", d["detail"])


class PlayerSummary(unittest.TestCase):
    def test_player_reason_follows_experience_and_has_english(self):
        from errors import PLAYER_REASON
        spec = {i["code"]: i for i in json.loads((ROOT / "launchers/shared/experience.v1.json").read_text())["errors"]["items"]}
        loc = {i["source"]: i.get("en") for i in json.loads((ROOT / "launchers/macos/adapter/Sources/S2ArmAppleAdapter/Resources/Localizable.json").read_text())}
        from errors import STAGE_CODES
        self.assertFalse(set(STAGE_CODES.values()) - PLAYER_REASON.keys())
        for code, text in PLAYER_REASON.items():
            with self.subTest(code=code):
                title, detail = spec[code]["title"]["zh-Hans"], spec[code]["detail"]["zh-Hans"]
                self.assertEqual(text, title + "。" if "具体原因见" in detail else title + "：" + detail)
                self.assertTrue(loc.get(text))


class RecoveryCoverage(unittest.TestCase):
    def test_real_lap_names_are_all_mapped_and_unknown_names_fail(self):
        source = (ROOT / "pack/oneclick/oneclick.sh").read_text()
        self.assertEqual(set(oneclick_stages(source)), set(ipa.ONECLICK_PHASES))
        with self.assertRaisesRegex(AssertionError, "未登记新阶段"):
            self.assertEqual(set(oneclick_stages(source + "\nlap 未登记新阶段\n")), set(ipa.ONECLICK_PHASES))

    def test_free_quota_translation_matches_chinese(self):
        loc = json.loads((ROOT / "launchers/macos/adapter/Sources/S2ArmAppleAdapter/Resources/Localizable.json").read_text())
        text = next(i["en"] for i in loc if i["source"] == RECOVERY["free_app_limit"])
        self.assertNotIn("AltStore", text)

    def test_all_literal_player_codes_have_recovery(self):
        found = set()
        for path in JOBS.glob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call): continue
                for kw in node.keywords:
                    if kw.arg == "code" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                        found.add(kw.value.value)
                if path.name == "saves.py" and isinstance(node.func, ast.Name) and node.func.id in ("fail", "report") and len(node.args) >= 3:
                    code = node.args[2]
                    if isinstance(code, ast.Constant) and isinstance(code.value, str): found.add("save_" + code.value)
        # 当前没有需豁免的内部码；控制平面返回给玩家的码也必须能指导恢复。
        self.assertEqual(found - RECOVERY.keys(), set())
        for _, code in install.SIGN_ERRORS: self.assertIn(code, RECOVERY)
        from errors import STAGE_CODES
        self.assertFalse(set(STAGE_CODES.values()) - RECOVERY.keys())
        translations = json.loads((ROOT / "launchers/macos/adapter/Sources/S2ArmAppleAdapter/Resources/Localizable.json").read_text())
        sources = {i["source"] for i in translations if i.get("en")}
        self.assertFalse({k for k, v in RECOVERY.items() if v not in sources})
        spec = json.loads((ROOT / "launchers/shared/experience.v1.json").read_text())
        self.assertEqual({i["code"] for i in spec["errors"]["items"]}, set(RECOVERY) | {"artifact_invalid"})


class FailureSummaryTest(unittest.TestCase):
    """摘要带失败事实：手算期望——红行优先、顺序不变、取满 limit 行；没有日志只有阶段"""
    def test_failure_summary_text(self):
        import tempfile as tf
        import errors
        with tf.NamedTemporaryFile("w", suffix=".log", delete=False, encoding="utf-8") as f:
            f.write("a\nb\n【红】坏了\nc\nd\ne\n")
        self.addCleanup(lambda: os.unlink(f.name))
        lines = errors.last_output_lines(f.name, limit=4)
        self.assertEqual(lines, ["【红】坏了", "c", "d", "e"])
        self.assertEqual(errors.failure_summary("解包", 1, lines),
                         "Failed stage: 解包\nExit code: 1\nLast output:\n  【红】坏了\n  c\n  d\n  e")
        self.assertEqual(errors.failure_summary("", None, []), "Failed stage: unknown")
