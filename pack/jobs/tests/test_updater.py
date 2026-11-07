import hashlib
import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ed25519  # noqa: E402
import updater  # noqa: E402

TEST_SECRET = bytes(range(32))


def signed(version, files, secret=TEST_SECRET):
    raw = json.dumps({"schema": 1, "version": version, "files": files}).encode()
    return raw, ed25519.sign(secret, raw)


def tree(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.install = self.root / "apps/S2Arm"
        (self.install / "Engine").mkdir(parents=True)
        (self.install / "S2Arm.exe").write_text("old")
        self.private = self.root / "data"
        for name, body in (("DefaultLibrary/games/a/cover.jpg", "game"), ("keys/prod.keys", "k"), ("saves/backup.zip", "save"), ("jobs/t.json", "{}"), ("cache/x", "c")):
            path = self.private / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        (self.private / "updates").mkdir()
        self.staged = self.root / "staged"
        (self.staged / "Engine").mkdir(parents=True)
        (self.staged / "S2Arm.exe").write_text("new")

    def data(self):
        return {k: v for k, v in tree(self.private).items() if not k.startswith("updates")}


class ApplyTests(Base):
    def run_apply(self, launch, **options):
        return updater.apply(self.staged, self.install, 2147483646, self.private, launch=launch, health_seconds=options.pop("health", 1), **options)

    def healthy(self, install, args):
        (self.private / "updates/health.ok").write_text("ok")

    def test_success_replaces_install_and_keeps_every_user_file(self):
        before = self.data()
        self.assertEqual(self.run_apply(self.healthy), 0)
        self.assertEqual((self.install / "S2Arm.exe").read_text(), "new")
        self.assertFalse(self.install.with_name("S2Arm.s2arm-old").exists())
        self.assertEqual(self.data(), before)

    def test_read_only_files_do_not_leave_the_old_copy_behind(self):
        locked = self.install / "Engine/pack"; locked.write_text("x"); locked.chmod(0o444)
        self.assertEqual(self.run_apply(self.healthy), 0)
        self.assertFalse(self.install.with_name("S2Arm.s2arm-old").exists())

    def test_new_version_that_never_starts_is_rolled_back(self):
        before = self.data(); launches = []
        self.assertEqual(self.run_apply(lambda i, a: launches.append((i / "S2Arm.exe").read_text())), 1)
        self.assertEqual((self.install / "S2Arm.exe").read_text(), "old")
        self.assertEqual(launches, ["new", "old"])
        self.assertEqual(json.loads((self.private / "updates/last_failure.json").read_text())["code"], "update_rolled_back")
        self.assertEqual(self.data(), before)

    def test_swap_failure_keeps_old_version(self):
        real, calls = os.rename, []

        def flaky(source, target):
            calls.append(target)
            if str(target) == str(self.install) and len(calls) == 2:
                raise PermissionError(13, "locked")
            real(source, target)
        launches = []
        with patch.object(updater.os, "rename", flaky), patch.object(updater.time, "sleep", lambda s: None), patch.object(updater.time, "monotonic", side_effect=iter(range(0, 10**6, 100)).__next__):
            self.assertEqual(self.run_apply(lambda i, a: launches.append((i / "S2Arm.exe").read_text())), 1)
        self.assertEqual((self.install / "S2Arm.exe").read_text(), "old")
        self.assertEqual(launches, ["old"])

    def test_app_that_does_not_exit_is_left_alone(self):
        result = updater.apply(self.staged, self.install, os.getpid(), self.private, launch=self.fail, exit_seconds=0)
        self.assertEqual(result, 1)
        self.assertEqual((self.install / "S2Arm.exe").read_text(), "old")

    def test_retry_after_interrupted_first_rename_installs_new_version(self):
        before = self.data()
        backup = self.install.with_name("S2Arm.s2arm-old")
        # swap() interrupted after install -> backup, before new -> install.
        os.rename(self.install, backup)
        self.assertEqual(self.run_apply(self.healthy), 0)
        self.assertEqual((self.install / "S2Arm.exe").read_text(), "new")
        self.assertFalse(backup.exists())
        self.assertEqual(self.data(), before)

    def test_retry_with_missing_staged_files_restores_the_old_install(self):
        before = self.data()
        backup = self.install.with_name("S2Arm.s2arm-old")
        os.rename(self.install, backup)
        launches = []
        self.staged = self.root / "missing-staged"
        self.assertEqual(self.run_apply(lambda i, a: launches.append((i / "S2Arm.exe").read_text())), 1)
        self.assertEqual((self.install / "S2Arm.exe").read_text(), "old")
        self.assertEqual(launches, ["old"])
        self.assertEqual(self.data(), before)

    def test_failed_interrupted_install_recovery_preserves_the_backup(self):
        backup = self.install.with_name("S2Arm.s2arm-old")
        os.rename(self.install, backup)
        with patch.object(updater, "retry_rename", side_effect=PermissionError(13, "locked")):
            self.assertEqual(self.run_apply(lambda *_: None), 1)
        self.assertEqual((backup / "S2Arm.exe").read_text(), "old")

    def test_retry_after_partial_new_copy_cleans_it_and_installs_normally(self):
        backup = self.install.with_name("S2Arm.s2arm-old")
        os.rename(self.install, backup)
        partial = self.install.with_name("S2Arm.s2arm-new")
        partial.mkdir()
        (partial / "incomplete").write_text("synthetic interrupted copy")
        self.assertEqual(self.run_apply(self.healthy), 0)
        self.assertEqual((self.install / "S2Arm.exe").read_text(), "new")
        self.assertFalse((self.install / "incomplete").exists())
        self.assertFalse(partial.exists())

    def unconfirmed_previous_update(self):
        """上一次 apply 已把安装目录换成新版、旧版留在副本，没等到 health.ok 就中断（审计 P1-147）"""
        backup = self.install.with_name("S2Arm.s2arm-old")
        os.rename(self.install, backup)
        (self.install / "Engine").mkdir(parents=True)
        (self.install / "S2Arm.exe").write_text("unconfirmed")
        return backup

    def test_failed_retry_after_unconfirmed_update_keeps_the_known_good_install(self):
        self.unconfirmed_previous_update()
        self.staged = self.root / "missing-staged"   # 这次替换必然失败
        launches = []
        self.assertEqual(self.run_apply(lambda i, a: launches.append((i / "S2Arm.exe").read_text())), 1)
        failure = json.loads((self.private / "updates/last_failure.json").read_text())
        self.assertEqual(failure["code"], "update_swap_failed")
        # 失败句说「已保留旧版」：盘上必须真有确认过能用的那一版
        self.assertEqual((self.install / "S2Arm.exe").read_text(), "old")
        self.assertEqual(launches, ["old"])

    def test_new_version_failing_after_unconfirmed_update_rolls_back_to_the_known_good_install(self):
        backup = self.unconfirmed_previous_update()
        launches = []
        self.assertEqual(self.run_apply(lambda i, a: launches.append((i / "S2Arm.exe").read_text())), 1)
        self.assertEqual((self.install / "S2Arm.exe").read_text(), "old")
        self.assertEqual(launches, ["new", "old"])
        self.assertFalse(backup.exists())

    def test_confirmed_previous_update_becomes_the_rollback_target(self):
        self.unconfirmed_previous_update()
        (self.private / "updates/health.ok").write_text("ok")   # 上次换上的版本后来起来过
        launches = []
        self.assertEqual(self.run_apply(lambda i, a: launches.append((i / "S2Arm.exe").read_text())), 1)
        self.assertEqual((self.install / "S2Arm.exe").read_text(), "unconfirmed")
        self.assertEqual(launches, ["new", "unconfirmed"])


class Handler(http.server.BaseHTTPRequestHandler):
    routes = {}
    hits = []

    def do_GET(self):
        self.hits.append(self.path)
        body = self.routes.get(self.path)
        self.send_response(200 if body is not None else 404); self.end_headers()
        self.wfile.write(body or b"")

    def log_message(self, *a):
        pass


class FlakyHandler(http.server.BaseHTTPRequestHandler):
    """按脚本回应：每项是一次请求给多少字节（None = 全给）、支不支持续传"""
    body = b""
    plan = []
    ranges = []

    def do_GET(self):
        header = self.headers.get("Range")
        start = int(header[6:-1]) if header else 0
        type(self).ranges.append(start)
        limit, resume = type(self).plan.pop(0) if type(self).plan else (None, True)
        if not resume:
            start = 0
        data = self.body[start:]
        self.send_response(206 if start else 200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data if limit is None else data[:limit])
        self.wfile.flush()
        self.connection.close()

    def log_message(self, *a):
        pass


class DownloadTests(Base):
    def setUp(self):
        super().setUp()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FlakyHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        FlakyHandler.body = bytes(range(256)) * 4096
        FlakyHandler.ranges = []
        self.target = self.root / "package"
        for name, value in (("trusted", lambda url: True), ("emit", lambda **k: None)):
            p = patch.object(updater, name, value); p.start(); self.addCleanup(p.stop)
        p = patch.object(updater, "RETRY_SECONDS", 0); p.start(); self.addCleanup(p.stop)

    def get(self):
        updater.download(f"http://127.0.0.1:{self.server.server_port}/p", self.target, len(FlakyHandler.body))

    def test_dropped_connection_resumes_where_it_stopped(self):
        FlakyHandler.plan = [(1 << 18, True), (1 << 18, True), (None, True)]
        self.get()
        self.assertEqual(self.target.read_bytes(), FlakyHandler.body)
        self.assertEqual(FlakyHandler.ranges, [0, 1 << 18, 1 << 19])

    def test_server_without_resume_starts_over(self):
        FlakyHandler.plan = [(1 << 18, True), (None, False)]
        self.get()
        self.assertEqual(self.target.read_bytes(), FlakyHandler.body)

    def test_gives_up_when_nothing_arrives(self):
        FlakyHandler.plan = [(0, True)] * updater.DOWNLOAD_ATTEMPTS
        with self.assertRaises(updater.UpdateError) as caught:
            self.get()
        self.assertEqual(caught.exception.code, "update_download_failed")
        self.assertFalse(self.target.exists())

    def test_retry_in_a_new_process_resumes_from_the_saved_part(self):
        """跨进程重试（审计 P2-220）：上次进程留下的 .part 接着下，不从头来"""
        Path(str(self.target) + ".part").write_bytes(FlakyHandler.body[:1 << 18])
        FlakyHandler.plan = [(None, True)]
        self.get()
        self.assertEqual(self.target.read_bytes(), FlakyHandler.body)
        self.assertEqual(FlakyHandler.ranges, [1 << 18])

    def test_part_longer_than_the_manifest_is_discarded(self):
        Path(str(self.target) + ".part").write_bytes(b"x" * (len(FlakyHandler.body) + 1))
        FlakyHandler.plan = [(None, True)]
        self.get()
        self.assertEqual(self.target.read_bytes(), FlakyHandler.body)
        self.assertEqual(FlakyHandler.ranges, [0])

    def test_slow_progress_keeps_going_past_the_attempt_limit(self):
        FlakyHandler.plan = [(1 << 16, True)] * (updater.DOWNLOAD_ATTEMPTS + 3) + [(None, True)]
        self.get()
        self.assertEqual(self.target.read_bytes(), FlakyHandler.body)


class MacUnpackTests(Base):
    def test_failed_attach_reports_why_and_mounts_outside_the_data_folder(self):
        calls = []
        def run(cmd, **k):
            calls.append(cmd)
            return type("R", (), {"returncode": 1, "stderr": b"hdiutil: attach failed - Permission denied"})
        dest = self.root / "unpack"; dest.mkdir()
        with patch.object(updater.subprocess, "run", run), self.assertRaises(updater.UpdateError) as caught:
            updater.extract_mac(self.root / "x.dmg", dest)
        self.assertEqual(caught.exception.code, "update_unpack_failed")
        self.assertIn("Permission denied", caught.exception.detail)
        mount = Path(calls[0][calls[0].index("-mountpoint") + 1])
        self.assertFalse(mount.is_relative_to(self.root)); self.assertFalse(mount.exists())

    def test_failure_reason_reaches_the_log(self):
        args = type("A", (), {"private_root": str(self.root / "p")})
        updater.record_failure(args, "update_unpack_failed", "打不开", "hdiutil: attach failed")
        text = (self.root / "p/logs/update.log").read_text(encoding="utf-8")
        self.assertIn("update_unpack_failed", text); self.assertIn("hdiutil: attach failed", text)


class PrepareTests(Base):
    def setUp(self):
        super().setUp()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        patcher = patch.dict(os.environ, {"S2A_UPDATE_API": self.base + "/latest", "S2A_UPDATE_PUBKEY": ed25519.public_key(TEST_SECRET).hex()})
        patcher.start(); self.addCleanup(patcher.stop)
        package = self.root / "p.zip"
        with zipfile.ZipFile(package, "w") as z:
            z.writestr("S2Arm/S2Arm.exe", "new"); z.writestr("S2Arm/Engine/e", "e"); z.writestr("S2Arm/Runtime/python/python.exe", "py")
            z.writestr("S2Arm/Runtime/python/Lib/site-packages/big", "x"); z.writestr("S2Arm/Runtime/python/Lib/os.py", "os")
        self.zip = package.read_bytes()
        self.publish(hashlib.sha256(self.zip).hexdigest())

    def publish(self, digest, tag="v9.9.9", secret=TEST_SECRET, version="9.9.9"):
        name = "S2Arm-windows-x64.zip"
        raw, signature = signed(version, {name: {"sha256": digest, "size": len(self.zip)}}, secret)
        Handler.routes = {"/latest": json.dumps({"tag_name": tag, "assets": [
            {"name": name, "size": len(self.zip), "browser_download_url": self.base + "/pkg"},
            {"name": updater.MANIFEST, "size": 80, "browser_download_url": self.base + "/m"},
            {"name": updater.SIGNATURE, "size": 64, "browser_download_url": self.base + "/s"}]}).encode(),
            "/pkg": self.zip, "/m": raw, "/s": signature}

    def prepare(self, protect=()):
        with patch("builtins.print") as out, patch.object(updater.subprocess, "run", return_value=type("R", (), {"returncode": 0, "stdout": ""})()):
            updater.prepare("windows", "0.1.1", self.private, self.install, list(protect) + [self.private])
        return json.loads(out.call_args.args[0])

    def test_ready_leaves_install_untouched_and_runner_has_no_site_packages(self):
        before = tree(self.install)
        ready = self.prepare()
        self.assertEqual(ready["state"], "ready")
        self.assertEqual(tree(self.install), before)
        self.assertEqual((Path(ready["staged"]) / "S2Arm.exe").read_text(), "new")
        self.assertTrue(Path(ready["python"]).is_file())
        self.assertFalse(list(Path(ready["python"]).parent.rglob("site-packages")))

    def test_retry_keeps_the_partial_download_of_the_same_version(self):
        work = self.private / "updates/9.9.9"
        (work / "unpack").mkdir(parents=True)
        (work / "unpack/stale").write_text("x")
        part = work / "S2Arm-windows-x64.zip.part"
        part.write_bytes(self.zip[:100])
        seen = []
        with patch.object(updater, "download", side_effect=lambda url, target, size: seen.append(Path(str(target) + ".part").read_bytes()) or Path(target).write_bytes(self.zip)):
            ready = self.prepare()
        self.assertEqual(ready["state"], "ready")
        self.assertEqual(seen, [self.zip[:100]])   # prepare 没删已下的字节，交给 download 续传
        self.assertFalse((work / "unpack/stale").exists())

    def test_checksum_mismatch_deletes_download_and_keeps_old(self):
        self.publish("0" * 64); before = tree(self.install)
        with self.assertRaises(updater.UpdateError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, "update_checksum_mismatch")
        self.assertEqual(tree(self.install), before)
        self.assertFalse(list((self.private / "updates").rglob("S2Arm-windows-x64.zip")))

    def test_runs_the_way_the_launcher_starts_it(self):
        """两端都用 python -I 直接跑 updater.py：同目录的模块要自己能找到。"""
        self.publish(hashlib.sha256(self.zip).hexdigest())
        run = __import__("subprocess").run([sys.executable, "-B", "-I", updater.__file__, "check", "--platform", "windows", "--current", "0.1.0"], capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue(json.loads(run.stdout)["available"])

    def test_bad_signature_downloads_nothing(self):
        self.publish(hashlib.sha256(self.zip).hexdigest(), secret=bytes([7]) * 32)
        Handler.hits = []
        with self.assertRaises(updater.UpdateError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, "update_bad_signature")
        self.assertNotIn("/pkg", Handler.hits)

    def test_manifest_for_another_version_is_refused(self):
        self.publish(hashlib.sha256(self.zip).hexdigest(), version="9.9.8")
        with self.assertRaises(updater.UpdateError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, "update_bad_signature")

    def test_oversized_download_is_stopped(self):
        name = "S2Arm-windows-x64.zip"
        raw, signature = signed("9.9.9", {name: {"sha256": hashlib.sha256(self.zip).hexdigest(), "size": 10}})
        self.publish(hashlib.sha256(self.zip).hexdigest())
        Handler.routes["/m"], Handler.routes["/s"] = raw, signature
        with self.assertRaises(updater.UpdateError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, "update_checksum_mismatch")

    def test_disk_full_stops_before_download(self):
        with patch.object(updater.shutil, "disk_usage", return_value=type("U", (), {"free": 1})()), patch.object(updater, "download", side_effect=AssertionError):
            with self.assertRaises(updater.UpdateError) as caught:
                self.prepare()
        self.assertEqual(caught.exception.code, "update_disk_full")

    def test_network_failure_is_reported_by_main_and_keeps_old(self):
        before = tree(self.install)
        Handler.routes["/pkg"] = None
        with patch("builtins.print") as out:
            code = updater.main(["prepare", "--platform", "windows", "--current", "0.1.1", "--private-root", str(self.private), "--install", str(self.install)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.call_args.args[0])["state"], "failed")
        self.assertEqual(tree(self.install), before)

    def test_data_inside_install_is_refused(self):
        inside = self.install / "MyLibrary"; inside.mkdir()
        with self.assertRaises(updater.UpdateError) as caught:
            self.prepare([inside])
        self.assertEqual(caught.exception.code, "update_data_inside_install")

    def test_zip_slip_is_refused(self):
        evil = self.root / "evil.zip"
        with zipfile.ZipFile(evil, "w") as z:
            z.writestr("../../escape", "x")
        target = self.root / "out"; target.mkdir()
        with self.assertRaises(updater.UpdateError):
            updater.extract_windows(evil, target)
        self.assertFalse((self.root / "escape").exists())


class DeltaTests(Base):
    def setUp(self):
        super().setUp()
        self.old = self.root / "old"; self.new = self.root / "new"
        for root, body in ((self.old, "v1"), (self.new, "v2")):
            (root / "Engine").mkdir(parents=True); (root / "same").write_text("same")
            (root / "tool").write_text(body); (root / "Engine/data").write_text(body * 1000)
        (self.old / "gone").write_text("x")
        (self.new / "added").write_text("new file")
        if os.name != "nt":
            (self.new / "tool").chmod(0o755); (self.old / "tool").chmod(0o644)
            os.symlink("tool", self.new / "alias")
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        patcher = patch.dict(os.environ, {"S2A_UPDATE_API": self.base + "/latest", "S2A_UPDATE_PUBKEY": ed25519.public_key(TEST_SECRET).hex()}); patcher.start(); self.addCleanup(patcher.stop)
        self.delta = updater.build_delta(self.old, self.new, "windows", "0.1.1", "9.9.9", self.root)
        full = self.root / "full.zip"
        with zipfile.ZipFile(full, "w") as z:
            z.writestr("S2Arm/S2Arm.exe", "full"); z.writestr("S2Arm/Engine/e", "e"); z.writestr("S2Arm/Runtime/python/python.exe", "py")
        self.full = full.read_bytes()
        self.hits = Handler.hits = []

    def publish(self, delta, in_manifest=True):
        names = {"full": "S2Arm-windows-x64.zip", "delta": self.delta.name}
        files = {names["full"]: {"sha256": hashlib.sha256(self.full).hexdigest(), "size": len(self.full)}}
        if in_manifest:
            files[names["delta"]] = {"sha256": hashlib.sha256(delta).hexdigest(), "size": len(delta)}
        raw, signature = signed("9.9.9", files)
        Handler.routes = {"/latest": json.dumps({"tag_name": "v9.9.9", "assets": [
            {"name": names["full"], "size": 1, "browser_download_url": self.base + "/full"},
            {"name": names["delta"], "size": 1, "browser_download_url": self.base + "/delta"},
            {"name": updater.MANIFEST, "size": 9, "browser_download_url": self.base + "/m"},
            {"name": updater.SIGNATURE, "size": 9, "browser_download_url": self.base + "/s"}]}).encode(),
            "/full": self.full, "/delta": delta, "/m": raw, "/s": signature}

    def prepare(self):
        shutil_rmtree = __import__("shutil").rmtree
        shutil_rmtree(self.install); __import__("shutil").copytree(self.old, self.install, symlinks=True)
        # 差分打完的目录里要有 Runtime/python 才能备 runner：清单里没有，打桩跳过
        with patch("builtins.print") as out, patch.object(updater, "runner_python", return_value=self.root / "py"), patch.object(updater.subprocess, "run", return_value=type("R", (), {"returncode": 0, "stdout": ""})()):
            updater.prepare("windows", "0.1.1", self.private, self.install, [self.private])
        return [json.loads(c.args[0]) for c in out.call_args_list]

    def test_delta_applies_to_a_copy_and_matches_the_new_tree_exactly(self):
        out = self.root / "out"; out.mkdir()
        staged = updater.apply_delta(self.delta, "windows", "0.1.1", "9.9.9", self.old, out)
        self.assertEqual(updater.file_manifest(staged), updater.file_manifest(self.new))
        self.assertTrue((self.old / "gone").exists())   # 原目录不动

    def test_only_changed_files_are_packed(self):
        with zipfile.ZipFile(self.delta) as z:
            self.assertNotIn("files/same", z.namelist())
            self.assertIn("files/tool", z.namelist())

    def test_prepare_uses_delta_and_skips_the_full_package(self):
        self.publish(self.delta.read_bytes()); before = self.data()
        events = self.prepare()
        self.assertEqual(events[-1]["via"], "delta")
        self.assertNotIn("/full", Handler.hits)
        self.assertEqual(updater.file_manifest(events[-1]["staged"]), updater.file_manifest(self.new))
        self.assertEqual(self.data(), before)

    def test_corrupt_delta_falls_back_to_full(self):
        self.publish(b"not a zip")
        events = self.prepare()
        self.assertIn("deltaFailed", [e["state"] for e in events])
        self.assertEqual(events[-1]["via"], "full")
        self.assertEqual((Path(events[-1]["staged"]) / "S2Arm.exe").read_text(), "full")

    def test_delta_outside_the_signed_manifest_is_ignored(self):
        self.publish(self.delta.read_bytes(), in_manifest=False)
        events = self.prepare()
        self.assertEqual(events[-1]["via"], "full")
        self.assertNotIn("/delta", Handler.hits)

    def test_changed_install_falls_back_to_full(self):
        out = self.root / "out"; out.mkdir()
        (self.old / "same").write_text("edited by something")
        with self.assertRaises(updater.UpdateError):
            updater.apply_delta(self.delta, "windows", "0.1.1", "9.9.9", self.old, out)

    def test_paths_and_links_cannot_leave_the_app(self):
        for target in ({"../evil": {"sha256": "0"}}, {"a/../../evil": {"sha256": "0"}}, {"/etc/x": {"sha256": "0"}}, {"a\\b": {"sha256": "0"}}, {"C:/x": {"sha256": "0"}},
                       {"l": {"link": "/etc/passwd"}}, {"l": {"link": "../../x"}}, {"a/l": {"link": "../../x"}}):
            with self.subTest(target=target), self.assertRaises(updater.UpdateError):
                updater.check_target(target)
        updater.check_target({"Contents/Frameworks/P.framework/Python": {"link": "Versions/Current/Python"}, "a/b": {"link": "../c"}, "a/f": {"sha256": "0"}})

    def test_release_tools_import_and_reference_real_names(self):
        sys.path.insert(0, str(Path(updater.__file__).resolve().parents[2] / "tools"))
        import make_update_delta, sign_update_manifest
        self.assertEqual(make_update_delta.SUMS, "SHA256SUMS.txt")
        (self.old / "S2Arm-windows-x64.zip").write_text("pkg"); (self.old / "S2Arm-mac-arm64-delta-0.1.1.zip").write_text("d"); (self.old / "other.txt").write_text("no")
        files = json.loads(sign_update_manifest.build(self.old, "9.9.9"))["files"]
        self.assertEqual(sorted(files), ["S2Arm-mac-arm64-delta-0.1.1.zip", "S2Arm-windows-x64.zip"])

    def test_delta_for_another_version_is_refused(self):
        out = self.root / "out"; out.mkdir()
        with self.assertRaises(updater.UpdateError):
            updater.apply_delta(self.delta, "windows", "0.1.0", "9.9.9", self.old, out)


class RunnerTests(unittest.TestCase):
    def test_apply_needs_no_network_modules(self):
        """updates/runner 的 Python 在 Mac 上没有 ssl：导入 updater 不能带进来。"""
        code = "import sys; sys.path.insert(0, %r); import updater; print([m for m in ('ssl', 'urllib.request', 'zipfile', '_hashlib') if m in sys.modules])" % str(Path(updater.__file__).parent)
        self.assertEqual(__import__("subprocess").run([sys.executable, "-I", "-c", code], capture_output=True, text=True).stdout.strip(), "[]")


class Ed25519Tests(unittest.TestCase):
    """RFC 8032 第 7.1 节的测试向量，期望值来自 RFC，不依赖被测实现。"""
    def test_rfc_vectors(self):
        for secret, public, message, signature in (
            ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60", "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
             "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
            ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb", "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
             "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00")):
            sk, pk, msg, sig = map(bytes.fromhex, (secret, public, message, signature))
            self.assertEqual(ed25519.public_key(sk), pk)
            self.assertEqual(ed25519.sign(sk, msg), sig)
            self.assertTrue(ed25519.verify(pk, msg, sig))

    def test_tampering_is_rejected(self):
        pk = ed25519.public_key(TEST_SECRET); sig = ed25519.sign(TEST_SECRET, b"manifest")
        self.assertFalse(ed25519.verify(pk, b"manifesT", sig))
        self.assertFalse(ed25519.verify(pk, b"manifest", sig[:-1] + bytes([sig[-1] ^ 1])))
        self.assertFalse(ed25519.verify(pk, b"manifest", sig[:32] + (int.from_bytes(sig[32:], "little") + ed25519.L).to_bytes(32, "little")))   # 不规范的 S
        self.assertFalse(ed25519.verify(pk, b"manifest", sig[:10]))


class VersionTests(Base):
    def release(self, tag):
        names = (updater.ASSETS["mac"], updater.MANIFEST, updater.SIGNATURE)
        version = tag[1:] if tag.count(".") == 2 and "-" not in tag else "0.0.0"
        raw, signature = signed(version, {updater.ASSETS["mac"]: {"sha256": "0" * 64, "size": 1}})
        release = {"tag_name": tag, "assets": [{"name": n, "size": 1, "browser_download_url": "https://github.com/" + n} for n in names]}
        answers = {updater.API: json.dumps(release).encode(), "https://github.com/" + updater.MANIFEST: raw, "https://github.com/" + updater.SIGNATURE: signature}

        class Response(__import__("io").BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass
        return patch.object(updater, "open_url", side_effect=lambda url, timeout: Response(answers[url]))

    def test_only_a_strictly_higher_numeric_version_is_offered(self):
        key = ed25519.public_key(TEST_SECRET).hex()
        for tag, current, available in (("v0.1.2", "0.1.1", True), ("v0.10.0", "0.9.9", True), ("v0.1.1", "0.1.1", False), ("v0.1.0", "0.1.1", False), ("v0.2.0-beta", "0.1.1", False), ("v0.2.0", "", False)):
            with self.subTest(tag=tag, current=current), self.release(tag), patch.object(updater, "PUBLIC_KEY", key), patch.dict(os.environ, {"S2A_UPDATE_API": ""}):
                self.assertEqual(updater.latest("mac", current)["available"], available)

    def test_official_build_ignores_the_test_key(self):
        with patch.dict(os.environ, {"S2A_UPDATE_API": "", "S2A_UPDATE_PUBKEY": "00" * 32}):
            self.assertEqual(updater.public_key().hex(), updater.PUBLIC_KEY)

    def test_official_build_refuses_other_hosts(self):
        with patch.dict(os.environ, {"S2A_UPDATE_API": ""}):
            self.assertFalse(updater.trusted("https://evil.example/x"))
            self.assertFalse(updater.trusted("http://github.com/x"))
            self.assertTrue(updater.trusted("https://github.com/x"))


if __name__ == "__main__":
    unittest.main()
