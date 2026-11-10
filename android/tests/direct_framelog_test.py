#!/usr/bin/env python3
"""direct_framelog.py 解析器单测（宿主，确定性，无设备）。

正例：python 侧按同一格式合成一份转储，解析后统计值逐项核对；
C 实现对拍：真实 nvn_vk_frame_log.c 转出的转储喂给解析器。
负控：缺分片 / 乱序 / 重复 / 哈希不符 / 计数不自洽 / 截断 / 非法 base64。
"""
import base64
import hashlib
import os
import re
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import direct_framelog as dfl  # noqa: E402

ANDROID = Path(__file__).resolve().parents[1]
N = 0


def _ok(name):
    global N
    N += 1
    print(f"  case {name:<40} ok")


def synth(ts, first=0, dropped=0, clock="CLOCK_BOOTTIME", cap=32768, fences=None):
    """python 侧镜像编码（v2）：ts = 保留的 µs 戳（最旧→最新）；
    fences = 每记录 (fence_wait_us, fence_n)，默认全 0。返回 logcat 文本。"""
    if fences is None:
        fences = [(0, 0)] * len(ts)
    blob = b"".join(struct.pack("<QII", t, fu, fn)
                    for t, (fu, fn) in zip(ts, fences))
    b64 = base64.b64encode(blob).decode()
    parts = [b64[i:i + 700] for i in range(0, len(b64), 700)] or []
    presents = first + len(ts)
    lines = [("direct frame-log meta: v=2 clock=%s unit=us endian=le rec=16 cap=%d "
              "recorded=%d presents=%d dropped=%d first=%d bytes=%d chunks=%d")
             % (clock, cap, len(ts), presents, dropped, first, len(blob), len(parts))]
    lines += ["direct frame-log[%d/%d]:%s" % (i + 1, len(parts), p)
              for i, p in enumerate(parts)]
    lines.append("direct frame-log end: frames=%d dropped=%d sha256=%s"
                 % (len(ts), dropped, hashlib.sha256(blob).hexdigest()))
    return "\n".join(lines) + "\n"


def expect_fail(name, text, needle):
    try:
        dfl.parse(text)
    except dfl.FrameLogError as e:
        assert needle in str(e), f"{name}: 错误 {e} 不含 {needle!r}"
        _ok(name)
        return
    raise AssertionError(f"{name}: 应报红却通过了")


class ParserTest(unittest.TestCase):
    def test_v3_grouped_metrics_and_v2_compatibility(self):
        payload = struct.pack("<QIIII", 1000000, 3500, 2, 900, 1200)
        b64 = base64.b64encode(payload).decode()
        text = ("direct frame-log meta: v=3 clock=CLOCK_BOOTTIME unit=us endian=le rec=24 "
                "cap=32768 recorded=1 presents=1 dropped=0 first=0 bytes=24 chunks=1\n"
                f"direct frame-log[1/1]:{b64}\n"
                f"direct frame-log end: frames=1 dropped=0 sha256={hashlib.sha256(payload).hexdigest()}\n")
        cap = dfl.parse(text)[0]
        self.assertEqual((cap["fence_us"], cap["fence_n"], cap["acquire_us"], cap["present_us"]),
                         ([3500], [2], [900], [1200]))
        old = dfl.parse(synth([1000]))[0]
        self.assertEqual(old["fence_us"], [0])
        self.assertIsNone(old["acquire_us"])
        self.assertIsNone(old["present_us"])
        with self.assertRaises(dfl.FrameLogError):
            dfl.parse(text.replace("rec=24", "rec=16"))

    def test_v4_gpu_fields(self):
        payload = struct.pack("<QIIIIII", 1000000, 3500, 2, 900, 1200, 8250, 7)
        b64 = base64.b64encode(payload).decode()
        text = ("direct frame-log meta: v=4 clock=CLOCK_BOOTTIME unit=us endian=le rec=32 "
                "cap=32768 recorded=1 presents=1 dropped=0 first=0 bytes=32 chunks=1\n"
                f"direct frame-log[1/1]:{b64}\n"
                f"direct frame-log end: frames=1 dropped=0 sha256={hashlib.sha256(payload).hexdigest()}\n")
        cap = dfl.parse(text)[0]
        self.assertEqual((cap["gpu_us"], cap["gpu_seq"]), ([8250], [7]))
        # UINT32_MAX = 无新结算帧/未采集 → None，不当 0 用
        payload2 = struct.pack("<QIIIIII", 2000000, 0, 0, 0, 0, 0xffffffff, 0xffffffff)
        b64b = base64.b64encode(payload2).decode()
        text2 = text.replace(b64, b64b).replace(hashlib.sha256(payload).hexdigest(),
                                                hashlib.sha256(payload2).hexdigest())
        cap2 = dfl.parse(text2)[0]
        self.assertIsNone(cap2["gpu_us"][0])
        self.assertIsNone(cap2["gpu_seq"][0])
        with self.assertRaises(dfl.FrameLogError):
            dfl.parse(text.replace("rec=32", "rec=24"))

    def test_v5_disp_fields(self):
        payload = struct.pack("<QIIIIIIII", 1000000, 3500, 2, 900, 1200, 8250, 7, 16667, 11)
        b64 = base64.b64encode(payload).decode()
        text = ("direct frame-log meta: v=5 clock=CLOCK_BOOTTIME unit=us endian=le rec=40 "
                "cap=32768 recorded=1 presents=1 dropped=0 first=0 bytes=40 chunks=1\n"
                f"direct frame-log[1/1]:{b64}\n"
                f"direct frame-log end: frames=1 dropped=0 sha256={hashlib.sha256(payload).hexdigest()}\n")
        cap = dfl.parse(text)[0]
        self.assertEqual((cap["gpu_us"], cap["gpu_seq"]), ([8250], [7]))
        self.assertEqual((cap["disp_us"], cap["disp_seq"]), ([16667], [11]))
        # UINT32_MAX = 无新确认对/未采集 → None，不当 0 用
        payload2 = struct.pack("<QIIIIIIII", 2000000, 0, 0, 0, 0,
                               0xffffffff, 0xffffffff, 0xffffffff, 0xffffffff)
        b64b = base64.b64encode(payload2).decode()
        text2 = text.replace(b64, b64b).replace(hashlib.sha256(payload).hexdigest(),
                                                hashlib.sha256(payload2).hexdigest())
        cap2 = dfl.parse(text2)[0]
        self.assertIsNone(cap2["disp_us"][0])
        self.assertIsNone(cap2["disp_seq"][0])
        with self.assertRaises(dfl.FrameLogError):
            dfl.parse(text.replace("rec=40", "rec=32"))

    def test_file_priority_and_fallback(self):
        log = synth([1000, 2000, 3000])
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'frame.log'
            caps, source = dfl.parse_preferred(path, log)
            self.assertEqual(source, 'logcat')
            path.write_text(synth([4000, 6000]))
            caps, source = dfl.parse_preferred(path, log)
            self.assertEqual((source, caps[0]['ts']), ('file', [4000, 6000]))
            path.write_text('broken')
            with self.assertRaisesRegex(dfl.FrameLogError, '未证'):
                dfl.parse_preferred(path, log)

    def test_all(self):
        # ---- 正例：200 帧 16.6ms 间隔 + 两次卡顿（平移后续戳保持单调）----
        ts = [1_000_000 + i * 16_666 for i in range(200)]
        for i in range(50, 200):
            ts[i] += 60_000     # i=50 处一次 60ms 卡顿（进入间隔 76.7ms）
        for i in range(150, 200):
            ts[i] += 120_000    # i=150 处再一次 120ms 卡顿（进入间隔 136.7ms）
        fences = [(0, 0)] * 200
        fences[10] = (3500, 2)      # 帧 10：2 次等待共 3.5ms
        fences[11] = (900, 1)       # 帧 11：1 次 0.9ms
        fences[90] = (42000, 3)     # 帧 90：3 次共 42ms
        log = synth(ts, fences=fences)
        cap = dfl.parse(log)[0]
        a = dfl.analyze(cap)
        assert a["frames"] == 200 and a["intervals"] == 199
        assert a["gt50"] == 2 and a["gt100"] == 1
        assert abs(a["p50_ms"] - 16.666) < 0.01
        assert abs(a["longest_ms"] - (16.666 + 120.0)) < 0.01
        assert a["longest_frame"] == 150
        assert a["nonmono"] == 0
        fz = a["fence"]
        assert fz["waits"] == 6 and abs(fz["wait_ms"] - 46.4) < 0.01
        assert fz["frames_with"] == 3
        assert abs(fz["longest_ms"] - 42.0) < 0.01 and fz["longest_frame"] == 90
        _ok("positive_stats")

        # 对拍：给 frame=48 的窗口（帧 33..48，gap 都是 16.666ms）造一行聚合日志，
        # 解析器按同一 nearest-rank 应算回完全一致。
        agg = ("frame timing(近16帧, frame=48): gap=16.7ms p50=16.7 p95=16.7 "
               "max=16.7 (n=16) exec=1.0ms present=0.5ms other=15.2ms "
               "commands=3 fps=60.0\n")
        rows = dfl.crosscheck(cap, log + agg)
        assert len(rows) == 1 and rows[0]["verdict"] == "一致", rows
        _ok("crosscheck_match")

        # 偏差注入：max 多报 0.4ms → 应判偏差
        agg_bad = agg.replace("max=16.7", "max=17.1")
        rows = dfl.crosscheck(cap, log + agg_bad)
        assert rows[0]["verdict"] == "偏差", rows
        _ok("crosscheck_detects_drift")

        # 聚合窗跨出记录范围 → 不可比，不算失败
        agg_far = agg.replace("frame=48", "frame=999")
        rows = dfl.crosscheck(cap, log + agg_far)
        assert rows[0]["verdict"] == "不可比", rows
        _ok("crosscheck_out_of_range")

        # ---- 丢帧（环满）：first>0 的块合法，统计照常，dropped 可见 ----
        ts2 = [5_000 + i * 16_000 for i in range(100)]
        cap2 = dfl.parse(synth(ts2, first=230, dropped=230))[0]
        a2 = dfl.analyze(cap2)
        assert a2["frames"] == 100 and a2["meta"]["dropped"] == 230
        assert a2["meta"]["first"] == 230
        _ok("dropped_prefix_visible")

        # ---- 重复时间戳：不致命，nonmono 计数可见（解析层不判合法性）----
        ts3 = [1000, 2000, 2000, 3000, 4000]
        cap3 = dfl.parse(synth(ts3))[0]
        assert dfl.analyze(cap3)["nonmono"] == 1
        _ok("duplicate_ts_counted")

        # ---- 负控：缺一片 / 乱序 / 重复 ----
        lines = log.splitlines(keepends=True)
        part_i = next(i for i, l in enumerate(lines) if "[2/" in l)
        expect_fail("missing_chunk", "".join(l for i, l in enumerate(lines) if i != part_i), "缺分片 [2]")
        reordered = lines[:]
        reordered[part_i], reordered[part_i + 1] = reordered[part_i + 1], reordered[part_i]
        expect_fail("reordered_chunk", "".join(reordered), "缺分片 [2]")
        dup = lines[:part_i + 1] + [lines[part_i]] + lines[part_i + 1:]
        expect_fail("duplicate_chunk", "".join(dup), "乱序")

        # ---- 负控：sha256 不符 / meta-end 计数不符 / 载荷长度不符 ----
        bad_hash = re.sub(r"sha256=[0-9a-f]+", "sha256=" + "0" * 64, log)
        expect_fail("hash_mismatch", bad_hash, "sha256")
        bad_end = re.sub(r"end: frames=200", "end: frames=199", log)
        expect_fail("end_count_mismatch", bad_end, "meta 不符")
        bad_b64 = re.sub(r"(frame-log\[1/\d+\]:).", r"\1", log, count=1)  # 吞一个 b64 字符
        expect_fail("payload_len_mismatch", bad_b64, "base64")
        # meta bytes 与载荷不符（把 meta 改小）：解码长度对不上 → 载荷错
        bad_meta = re.sub(r"bytes=\d+", "bytes=1500", log)
        expect_fail("meta_bytes_lie", bad_meta, "载荷")

        # ---- 负控：v=1 一律拒收（main 上从未发出过 v1，兼容分支已删）----
        v1 = re.sub(r"meta: v=2", "meta: v=1", log)
        expect_fail("v1_rejected", v1, "不支持")

        # ---- 负控：v2 缺 rec / rec 不符 / 版本未知 ----
        no_rec = re.sub(r"endian=le rec=16", "endian=le", log)
        expect_fail("v2_missing_rec", no_rec, "rec")
        bad_rec = re.sub(r"rec=16", "rec=8", log)
        expect_fail("v2_rec_mismatch", bad_rec, "rec")
        bad_v = re.sub(r"meta: v=2", "meta: v=6", log)
        expect_fail("unsupported_version", bad_v, "不支持")

        # ---- 负控：计数不自洽（溢出构造）----
        # first+recorded != presents（seq 回绕/被改）→ 拒绝
        over = re.sub(r"presents=200", "presents=250", log)
        expect_fail("counter_inconsistent", over, "不自洽")
        # dropped != first（meta 与 end 同步改，只对不上 first）→ 拒绝
        ddrop = synth(ts2, first=230, dropped=230).replace("dropped=230", "dropped=237")
        expect_fail("dropped_first_mismatch", ddrop, "不自洽")

        # ---- 负控：无 end 截断 / 分片先于 meta / 空日志 ----
        no_end = "\n".join(l for l in log.splitlines() if "end:" not in l)
        expect_fail("truncated_dump", no_end, "截断")
        part_line = next(l for l in log.splitlines() if "[1/" in l)
        expect_fail("part_before_meta", part_line + "\n", "先于 meta")
        assert dfl.parse("noise\nno blocks\n") == []
        _ok("no_blocks_empty_ok")

        # ---- CLI：正常块 rc=0，无块/坏块 rc=1 ----
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            f.write(log + agg)
            path = f.name
        try:
            r = subprocess.run([sys.executable, str(ANDROID / "direct_framelog.py"), path],
                               capture_output=True, text=True)
            assert r.returncode == 0, r.stderr + r.stdout
            assert "p50=" in r.stdout and "1%low=" in r.stdout and "一致 1" in r.stdout
        finally:
            os.unlink(path)
        _ok("cli_report")


class CRoundTripTest(unittest.TestCase):
    """真实 C 实现的转储 → 解析器拼回：时间戳逐点一致，统计一致。"""

    def test_c_dump_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            harness = Path(td) / "h.c"
            harness.write_text(r"""
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include "nvn/nvn_vk_frame_log.c"
// macOS Mach-O 不留未定义弱符号：宿主对拍不带 GPU 时间戳 / display timing → 未采集（UINT32_MAX）。
int nvn_vk_first_draw_gpu_last(double *ms, int *passes, uint64_t *frame) {
    (void)ms; (void)passes; (void)frame; return 0;
}
int nvn_vk_dt_last_gap(uint64_t *frame, uint64_t *gap_ns) {
    (void)frame; (void)gap_ns; return 0;
}
static void out(void *c, const char *l) { (void)c; puts(l); }
int main(void) {
    struct s2a_frame_log fl; s2a_fl_reset(&fl);
    // 30000 帧：16.6ms 基线 + 尖峰；超出环容？32768 够用，也验满环边沿。
    for (uint32_t i = 0; i < 30000; i++) {
        uint64_t t = 3000000ull + i * 16666ull;
        if (i >= 12345) t += 88000;       // i=12345 一次 88ms 卡顿
        if (i >= 23456) t += 150000;      // i=23456 再一次 150ms 卡顿
        // fence 账：i=5000 → 2 次共 3ms；i=25000 → 1 次 700µs（其余帧无等待）
        if (i == 5000) { s2a_fl_fence(&fl, 1500000); s2a_fl_fence(&fl, 1500000); }
        if (i == 25000) s2a_fl_fence(&fl, 700000);
        s2a_fl_note(&fl, t);
    }
    // 再灌 5000 帧把前面挤掉 → dropped=first=35000-32768=2232
    for (uint32_t i = 30000; i < 35000; i++) {
        uint64_t t = 3000000ull + i * 16666ull;
        if (i >= 12345) t += 88000;
        if (i >= 23456) t += 150000;
        if (i == 30010) s2a_fl_fence(&fl, 9000000);   // 9ms 一次
        s2a_fl_note(&fl, t);
    }
    s2a_fl_dump(&fl, out, NULL);
    return 0;
}
""")
            exe = Path(td) / "h"
            cc = os.environ.get("CC", "cc")
            subprocess.run([cc, "-std=gnu11", "-O1", "-Wall", "-Wextra", "-Werror",
                            "-I", str(ANDROID / "native"),
                            "-I", str(ANDROID / "native/nvn"),
                            "-I", str(ANDROID / "native/direct"),
                            "-o", str(exe), str(harness),
                            str(ANDROID / "native/direct/sha256.c")], check=True)
            out = subprocess.run([str(exe)], capture_output=True, text=True,
                                 check=True).stdout
        cap = dfl.parse(out)[0]
        m = cap["meta"]
        assert m["recorded"] == 32768 and m["presents"] == 35000
        assert m["dropped"] == 2232 and m["first"] == 2232

        def want(g):
            return (3000000 + g * 16666 + (88000 if g >= 12345 else 0)
                    + (150000 if g >= 23456 else 0))
        ts = cap["ts"]
        for j in (0, 1, 32766, 32767):
            g = 2232 + j                     # 第 j 个保留戳 = 全局第 2232+j 帧
            assert ts[j] == want(g), (j, ts[j], want(g))
        a = dfl.analyze(cap)
        assert a["frames"] == 32768 and a["intervals"] == 32767
        # 两次卡顿都在保留区（12345/23456 > 2232）：进入间隔 104.7ms / 166.7ms
        assert a["gt50"] == 2 and a["gt100"] == 2
        assert abs(a["p50_ms"] - 16.666) < 0.01
        assert abs(a["longest_ms"] - 166.666) < 0.01
        assert a["nonmono"] == 0
        # fence 字段逐点核对（v2）：i=5000/25000/30010 各被注一次
        fus, fn = cap["fence_us"], cap["fence_n"]
        assert fus is not None and fn is not None
        assert fus[5000 - 2232] == 3000 and fn[5000 - 2232] == 2
        assert fus[25000 - 2232] == 700 and fn[25000 - 2232] == 1
        assert fus[30010 - 2232] == 9000 and fn[30010 - 2232] == 1
        fz = a["fence"]
        assert fz["waits"] == 4 and abs(fz["wait_ms"] - 12.7) < 0.01
        assert fz["frames_with"] == 3
        assert abs(fz["longest_ms"] - 9.0) < 0.01 and fz["longest_frame"] == 30010
        _ok("c_dump_roundtrip_full_ring")


if __name__ == "__main__":
    unittest.main(verbosity=0)
    # unittest.main 已退出；统计靠 _ok 的输出。
