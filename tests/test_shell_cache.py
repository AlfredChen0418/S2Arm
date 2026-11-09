#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 S2Arm contributors
"""Offline shell-cache contract tests; original bytes, independent identities and hashes."""
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import io
import json
import multiprocessing
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    from pack.shell import cache
except ImportError:
    cache = None
from pack.shell import manifest

BASE = "https://shell.invalid/releases"
PAYLOAD = {"S2Arm.app/host": b"original host\0\x01", "S2Arm.app/中文 图标.dat": b"icon", "empty": b""}


def fixture(commit="a" * 40):
    # Concatenation keeps the NUL delimiter distinct from the leading digit.
    inputs = hashlib.sha256(commit.encode()).hexdigest()[:16]   # 夹具：每个「提交」一份不同的输入指纹
    sid = hashlib.sha256(("s2a.shell.v2\0" + inputs + "\0abc12\0" + "18.0\0" + "27").encode()).hexdigest()[:16]
    return {"schema": "s2a.artifact.v2", "target": "ios-shell",
            "details": {"schema": "s2a.artifact.shell.v2", "shellId": sid, "inputs": inputs,
                        "sourceCommit": commit, "chain": "abc12", "iosMin": "18.0", "iosSdk": "27"},
            "files": [{"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                      for name, data in sorted(PAYLOAD.items())]}


def process_worker(dest, sid, responses, barrier, output):
    """Real spawned processes, actual filesystem publish; only transport is replaced."""
    publish = cache._publish
    def synchronize(*args):
        barrier.wait(timeout=15)
        return publish(*args)
    try:
        with patch.object(cache, "_publish", side_effect=synchronize), \
             patch.object(cache, "_open_url", side_effect=lambda url: io.BytesIO(responses[url])), \
             patch.object(socket, "create_connection", side_effect=AssertionError("no network")):
            result = cache.ensure(sid, dest, BASE)
            output.put((result.hit, str(result.root), None))
    except Exception as error:
        output.put((None, None, repr(error)))
        raise


class ShellCache(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(cache, "required pack.shell.cache downloader is absent")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name)
        self.dest = self.parent / "cache"
        self.doc = fixture()
        self.sid = self.doc["details"]["shellId"]
        self.calls = []
        self.responses = {}
        self.add_release(self.doc)
        self.network = patch.object(socket, "create_connection", side_effect=AssertionError("tests must not use network"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.reader = patch.object(cache, "_open_url", side_effect=self.read)
        self.reader.start()
        self.addCleanup(self.reader.stop)

    def add_release(self, doc):
        sid = doc["details"]["shellId"]
        self.responses[f"{BASE}/{sid}/manifest.json"] = json.dumps(doc, ensure_ascii=False).encode()
        for name, data in PAYLOAD.items():
            # Independent literal expected encoding for the only non-ASCII fixture path.
            name = name.replace("中文 图标.dat", "%E4%B8%AD%E6%96%87%20%E5%9B%BE%E6%A0%87.dat")
            self.responses[f"{BASE}/{sid}/files/{name}"] = data

    def read(self, url):
        self.calls.append(url)
        data = self.responses[url]
        if isinstance(data, Exception):
            raise data
        return io.BytesIO(data)

    def ensure(self, sid=None, offline=False, base=BASE):
        return cache.ensure(sid or self.sid, self.dest, base, offline)

    def test_download_exact_bytes_and_atomic_layout(self):
        result = self.ensure()
        self.assertEqual(result.root, (self.dest / self.sid / "root").resolve())
        self.assertEqual(result.manifest, (self.dest / self.sid / "manifest.json").resolve())
        self.assertFalse(result.hit)
        self.assertEqual(len(self.calls), 4)
        for name, data in PAYLOAD.items():
            self.assertEqual((result.root / name).read_bytes(), data)
        self.assertEqual(manifest.verify(result.root, result.manifest), self.doc)
        self.assertEqual(set(self.dest.iterdir()), {self.dest / self.sid})

    def test_download_through_parent_alias(self):
        # Like macOS /var -> /private/var: an ancestor alias, not a linked cache.
        target = self.parent / "real-parent"
        target.mkdir()
        alias = self.parent / "parent-alias"
        try:
            alias.symlink_to(target, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"parent alias creation unavailable: {error}")
        self.dest = alias / "cache"
        self.test_download_exact_bytes_and_atomic_layout()

    def test_reuse_no_download_for_both_app_modes(self):
        single = self.ensure()
        self.calls.clear()
        multi = self.ensure()
        self.assertTrue(multi.hit)
        self.assertEqual(single.root, multi.root)
        self.assertEqual(self.calls, [])

    def test_switch_version_preserves_other(self):
        a = self.ensure()
        before = a.manifest.read_bytes()
        bdoc = fixture("b" * 40)
        self.add_release(bdoc)
        b = self.ensure(bdoc["details"]["shellId"])
        self.assertNotEqual(a.root, b.root)
        self.assertEqual(a.manifest.read_bytes(), before)
        self.assertEqual(manifest.verify(a.root, a.manifest), self.doc)

    def test_offline_hit_and_missing_version(self):
        self.ensure()
        self.calls.clear()
        self.assertTrue(self.ensure(offline=True, base=None).hit)
        with self.assertRaisesRegex(cache.CacheError, "0" * 16):
            self.ensure("0" * 16, offline=True)
        self.assertEqual(self.calls, [])

    def test_tamper_missing_extra_never_redownload(self):
        result = self.ensure()
        host = result.root / "S2Arm.app/host"
        self.calls.clear()
        for operation in ("tamper", "missing", "extra"):
            with self.subTest(operation=operation):
                host.write_bytes(PAYLOAD["S2Arm.app/host"])
                extra = result.root / "extra"
                extra.unlink(missing_ok=True)
                if operation == "tamper":
                    host.write_bytes(b"X" + host.read_bytes()[1:])
                elif operation == "missing":
                    host.unlink()
                else:
                    extra.write_bytes(b"x")
                with self.assertRaisesRegex(cache.CacheError, "host|extra"):
                    self.ensure()
                self.assertEqual(self.calls, [])

    def test_incomplete_existing_entry_not_overwritten(self):
        entry = self.dest / self.sid
        entry.mkdir(parents=True)
        with self.assertRaises(cache.CacheError):
            self.ensure()
        self.assertEqual(list(entry.iterdir()), [])
        self.assertEqual(self.calls, [])

    def test_extra_entry_metadata_rejected(self):
        result = self.ensure()
        (result.root.parent / "extra").write_bytes(b"x")
        self.calls.clear()
        with self.assertRaises(cache.CacheError):
            self.ensure()
        self.assertEqual(self.calls, [])

    def test_wrong_manifest_id_before_payload(self):
        self.responses[f"{BASE}/{self.sid}/manifest.json"] = json.dumps(fixture("b" * 40)).encode()
        with self.assertRaisesRegex(cache.CacheError, self.sid):
            self.ensure()
        self.assertEqual(self.calls, [f"{BASE}/{self.sid}/manifest.json"])
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_short_long_wrong_hash_and_io_error_leave_no_version(self):
        url = f"{BASE}/{self.sid}/files/S2Arm.app/host"
        for response in (b"x", PAYLOAD["S2Arm.app/host"] + b"x", b"X" + PAYLOAD["S2Arm.app/host"][1:], OSError("interrupted")):
            with self.subTest(response=repr(response)):
                self.responses[url] = response
                with self.assertRaisesRegex(cache.CacheError, self.sid):
                    self.ensure()
                self.assertEqual(list(self.dest.iterdir()), [])

    def test_manifest_fetch_failure_identifies_missing_version(self):
        self.responses[f"{BASE}/{self.sid}/manifest.json"] = OSError("offline")
        with self.assertRaisesRegex(cache.CacheError, self.sid):
            self.ensure()
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_malformed_duplicate_and_oversized_manifest(self):
        for blob in (b"{", b'{"schema":1,"schema":2}', b"x" * (cache.MAX_MANIFEST + 1)):
            with self.subTest(size=len(blob)):
                self.responses[f"{BASE}/{self.sid}/manifest.json"] = blob
                with self.assertRaises(cache.CacheError):
                    self.ensure()
                self.assertEqual(list(self.dest.iterdir()), [])

    def test_manifest_paths_refused_before_any_payload(self):
        paths = ["../escape", "/absolute", "a\\b", "C:stream", "CON.txt", "aux",
                 "a.", "a ", "a//b", "a/./b", "a/", "a?b", "a\x01b"]
        for name in paths:
            with self.subTest(path=name):
                doc = copy.deepcopy(self.doc)
                doc["files"][0]["path"] = name
                self.responses[f"{BASE}/{self.sid}/manifest.json"] = json.dumps(doc).encode()
                self.calls.clear()
                with self.assertRaises(cache.CacheError):
                    self.ensure()
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(list(self.dest.iterdir()), [])
        self.assertFalse((self.parent / "escape").exists())

    def test_parent_case_collisions_and_file_directory_conflict(self):
        for names in (("Dir/a", "dir/b"), ("A", "a"), ("a", "a/b")):
            with self.subTest(names=names):
                doc = copy.deepcopy(self.doc)
                doc["files"] = [dict(self.doc["files"][0], path=name) for name in names]
                self.responses[f"{BASE}/{self.sid}/manifest.json"] = json.dumps(doc).encode()
                self.calls.clear()
                with self.assertRaises(cache.CacheError):
                    self.ensure()
                self.assertEqual(len(self.calls), 1)

    def test_size_bool_and_duplicate_file_rejected(self):
        for bad in ("bool", "duplicate"):
            doc = copy.deepcopy(self.doc)
            if bad == "bool":
                doc["files"][0]["size"] = True
            else:
                doc["files"].append(doc["files"][0])
            self.responses[f"{BASE}/{self.sid}/manifest.json"] = json.dumps(doc).encode()
            with self.assertRaises(cache.CacheError):
                self.ensure()

    def test_transport_policy_and_url_encoding(self):
        for base in ("file:///nonexistent-s2a-shell-root", "relative/dir", "http://remote.invalid/x", "http://127.0.0.2/x", "https://user:pass@host/x", "https://host/x?q=1", "https://host/x#frag"):
            with self.subTest(base=base), self.assertRaises(cache.CacheError):
                self.ensure(base=base)
        self.assertEqual(self.calls, [])
        self.ensure()
        self.assertIn(f"{BASE}/{self.sid}/files/S2Arm.app/%E4%B8%AD%E6%96%87%20%E5%9B%BE%E6%A0%87.dat", self.calls)

    def test_local_directory_source(self):
        """本机发布目录（内部链路：出壳后按发布布局放目录、ssh 同步到 Windows）：绝对路径与 file:// 都取得到、字节同；目录里文件被改 → 拒收、不入缓存"""
        pub = self.parent / "pub"
        (pub / self.sid).mkdir(parents=True)
        (pub / self.sid / "manifest.json").write_bytes(json.dumps(self.doc, ensure_ascii=False).encode())
        for name, data in PAYLOAD.items():
            f = pub / self.sid / "files" / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(data)
        real = self.reader.temp_original
        with patch.object(cache, "_open_url", side_effect=real):
            for i, base in enumerate((str(pub), pub.as_uri())):
                with self.subTest(base=base):
                    r = cache.ensure(self.sid, self.parent / f"local-{i}", base)
                    self.assertFalse(r.hit)
                    for name, data in PAYLOAD.items():
                        self.assertEqual((r.root / name).read_bytes(), data)
            (pub / self.sid / "files" / "empty").write_bytes(b"tampered")
            with self.assertRaises(cache.CacheError):
                cache.ensure(self.sid, self.parent / "local-bad", str(pub))
            self.assertFalse((self.parent / "local-bad" / self.sid).exists())
        self.assertEqual(self.calls, [])

    def test_publish_then_ensure_round_trip(self):
        """出壳 → publish 摆发布布局 → ensure 从发布目录取：字节同；同编号再发布是核对不覆盖；壳被改过 → 拒绝发布"""
        shell = self.parent / "shell"
        for name, data in PAYLOAD.items():
            f = shell / "root" / name; f.parent.mkdir(parents=True, exist_ok=True); f.write_bytes(data)
        doc = manifest.seal(shell / "root", shell / "manifest.json", "b" * 40, "1" * 16, "abc12", "18.0", "27")
        sid = doc["details"]["shellId"]
        pub = self.parent / "pub"
        self.assertEqual(cache.publish(shell, pub), sid)
        self.assertEqual(cache.publish(shell, pub), sid)   # 已发布：核对后返回
        with patch.object(cache, "_open_url", side_effect=self.reader.temp_original):
            r = cache.ensure(sid, self.parent / "rt", str(pub))
        for name, data in PAYLOAD.items():
            self.assertEqual((r.root / name).read_bytes(), data)
        (shell / "root" / "empty").write_bytes(b"x")
        with self.assertRaises(manifest.ShellError):
            cache.publish(shell, self.parent / "pub2")
        self.assertFalse((self.parent / "pub2" / sid).exists())

    def test_loopback_http_allowed_without_real_network(self):
        for index, base in enumerate(("http://127.0.0.1:8899/x", "http://localhost:8899/x", "http://[::1]:8899/x")):
            with self.subTest(base=base):
                root = self.parent / f"loopback-{index}"
                with patch.object(cache, "_open_url", side_effect=lambda url: io.BytesIO(self.responses[url.replace(base, BASE)])):
                    self.assertFalse(cache.ensure(self.sid, root, base).hit)

    def test_redirects_cannot_leave_publication_root(self):
        policy = cache._SameRootRedirect(BASE)
        from urllib.request import Request
        self.assertIsNotNone(policy.redirect_request(Request(BASE + "/a"), None, 302, "", {}, BASE + "/b"))
        for url in ("http://remote.invalid/a", "https://other.invalid/a", "https://shell.invalid/elsewhere",
                    BASE + "/../escape", BASE + "/%2e%2e/escape", BASE + "/x%5cy"):
            with self.subTest(url=url), self.assertRaises(cache.CacheError):
                policy.redirect_request(Request(BASE + "/a"), None, 302, "", {}, url)

    def test_invalid_shell_ids_never_access_network(self):
        for sid in ("../bad", "A" * 16, "a" * 15, "a" * 17, "a/b", ""):
            with self.subTest(sid=sid), self.assertRaises(cache.CacheError):
                cache.ensure(sid, self.dest, BASE)
        self.assertEqual(self.calls, [])

    def test_cache_root_symlink_rejected(self):
        target = self.parent / "outside"
        target.mkdir()
        try:
            self.dest.symlink_to(target, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"symlink creation unavailable: {error}")
        with self.assertRaises(cache.CacheError):
            self.ensure()
        self.assertEqual(list(target.iterdir()), [])

    def test_cached_payload_and_manifest_links_rejected(self):
        result = self.ensure()
        outside = self.parent / "outside"
        outside.write_bytes(b"unchanged")
        for path in (result.root / "S2Arm.app/host", result.manifest):
            original = path.read_bytes()
            path.unlink()
            try:
                path.symlink_to(outside)
            except OSError as error:
                path.write_bytes(original)
                self.skipTest(f"symlink creation unavailable: {error}")
            with self.assertRaises(cache.CacheError):
                self.ensure()
            path.unlink()
            path.write_bytes(original)
        self.assertEqual(outside.read_bytes(), b"unchanged")

    @unittest.skipUnless(os.name == "nt", "actual Windows junction probe")
    def test_windows_junction_rejected(self):
        target = self.parent / "outside"
        target.mkdir()
        result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(self.dest), str(target)], capture_output=True)
        if result.returncode:
            self.skipTest("Windows junction creation unavailable")
        with self.assertRaises(cache.CacheError):
            self.ensure()
        self.assertEqual(list(target.iterdir()), [])

    def test_concurrent_publish_uses_verified_winner(self):
        barrier = threading.Barrier(2)
        original = cache._publish
        def publish(*args):
            barrier.wait(timeout=5)
            return original(*args)
        with patch.object(cache, "_publish", side_effect=publish), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.ensure(), range(2)))
        self.assertEqual(sum(not item.hit for item in results), 1)
        self.assertEqual(results[0].root, results[1].root)
        self.assertEqual(manifest.verify(results[0].root, results[0].manifest), self.doc)
        self.assertEqual(set(self.dest.iterdir()), {self.dest / self.sid})

    def test_concurrent_existing_bad_winner_not_overwritten(self):
        def publish(stage, entry, sid):
            entry.mkdir()
            (entry / "sentinel").write_bytes(b"preserve")
            return original(stage, entry, sid)
        original = cache._publish
        with patch.object(cache, "_publish", side_effect=publish), self.assertRaises(cache.CacheError):
            self.ensure()
        self.assertEqual((self.dest / self.sid / "sentinel").read_bytes(), b"preserve")

    def test_real_spawned_process_publish(self):
        context = multiprocessing.get_context("spawn")
        output, barrier = context.Queue(), context.Barrier(2)
        workers = [context.Process(target=process_worker, args=(self.dest, self.sid, self.responses, barrier, output))
                   for _ in range(2)]
        try:
            for worker in workers:
                worker.start()
            results = [output.get(timeout=25) for _ in workers]
            for worker in workers:
                worker.join(timeout=10)
                self.assertEqual(worker.exitcode, 0)
            self.assertEqual([item[2] for item in results], [None, None])
            self.assertEqual(sum(not item[0] for item in results), 1)
            self.assertEqual(results[0][1], results[1][1])
            self.assertEqual(manifest.verify(self.dest / self.sid / "root", self.dest / self.sid / "manifest.json"), self.doc)
        finally:
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                    worker.join(timeout=5)
            output.close()
            output.join_thread()

    def test_cli_offline_json_utf8_and_no_network(self):
        self.ensure()
        renamed = self.parent / "缓存 空格"
        self.dest.rename(renamed)
        tool = ROOT / "pack/shell/cache.py"
        result = subprocess.run([sys.executable, "-I", "-X", "utf8=0", "-B", str(tool), "--shell-id", self.sid,
                                 "--cache-dir", str(renamed), "--offline", "--json"], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = json.loads(result.stdout.decode("utf-8"))
        self.assertEqual(doc["shellId"], self.sid)
        self.assertTrue(doc["hit"])
        self.assertIn("缓存 空格", doc["root"])
        missing = subprocess.run([sys.executable, "-I", "-X", "utf8=0", "-B", str(tool), "--shell-id", "0" * 16,
                                  "--cache-dir", str(renamed), "--offline"], capture_output=True)
        self.assertEqual(missing.returncode, 1)
        self.assertIn("0" * 16, missing.stderr.decode("utf-8"))

    def test_default_cache_root_configuration(self):
        with patch.dict(os.environ, {"S2A_SHELL_CACHE": str(self.dest)}):
            self.assertEqual(cache.default_cache_dir(), self.dest)
        with patch.dict(os.environ, {"S2A_RUN_ROOT": str(self.parent)}, clear=True):
            self.assertEqual(cache.default_cache_dir(), self.parent / "s2arm/cache/shell")


if __name__ == "__main__":
    unittest.main()
