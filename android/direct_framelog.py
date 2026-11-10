#!/usr/bin/env python3
# 直连逐帧帧时间解析：把 debug.s2a.frame_log=1 一局末的 logcat 分片转储拼回、
# 校验完整性后算全窗统计（p50/p95/p99、1% low、>50ms/>100ms、最长帧），
# 并与同一份日志里采样的 `frame timing(近16帧...)` 聚合行逐窗对拍。
#
# 用法：python3 android/direct_framelog.py <logcat.txt> [<logcat2.txt> ...]
#       adb logcat -d | python3 android/direct_framelog.py -
#
# 线格式（native/nvn/nvn_vk_frame_log.c 约定，logcat 时间戳/PID 前缀可带）：
#   direct frame-log meta: v=2 clock=... unit=us endian=le rec=16 cap=N recorded=R \
#       presents=P dropped=D first=F bytes=B chunks=C
#   direct frame-log[i/C]:<base64 分片>           i = 1..C，各恰好一次且按序
#   direct frame-log end: frames=R dropped=D sha256=<载荷十六进制>
# 载荷（v2）= R 条 16B LE 记录（最旧→最新，全局上屏序号 F..F+R-1）：
#   u64 ts_us（CLOCK_BOOTTIME）+ u32 fence_wait_us + u32 fence_n
# 支持 v2/16B 与 v3/24B 与 v4/32B 与 v5/40B；v3 增加 acquire/present 墙钟微秒，
# v4 增加 gpu_us/gpu_seq（GPU 忙时 + 结算帧序号），v5 增加 disp_us/disp_seq
# （actualPresentTime 相邻帧间隔 + 间隔所属较新帧号），UINT32_MAX 表示未采集/无新读数。
import base64
import hashlib
from pathlib import Path
import re
import struct
import sys

META_RE = re.compile(
    r"direct frame-log meta: v=(\d+) clock=(\S+) unit=(\S+) endian=(\S+)"
    r"(?: rec=(\d+))? cap=(\d+) recorded=(\d+) presents=(\d+) dropped=(\d+) "
    r"first=(\d+) bytes=(\d+) chunks=(\d+)")
PART_RE = re.compile(r"direct frame-log\[(\d+)/(\d+)\]:(\S*)")
END_RE = re.compile(r"direct frame-log end: frames=(\d+) dropped=(\d+) sha256=([0-9a-fA-F]+)")
AGG_RE = re.compile(
    r"frame timing\(近(\d+)帧, frame=(\d+)\): gap=([\d.]+)ms p50=([\d.]+) "
    r"p95=([\d.]+) max=([\d.]+) \(n=(\d+)\)")


class FrameLogError(Exception):
    pass


def parse_preferred(file_path, log_text):
    """文件存在则只认文件并严验；不存在时用 logcat，损坏文件不静默降级。"""
    path = Path(file_path) if file_path else None
    if path is not None and path.exists():
        caps = parse(path.read_text(encoding="utf-8", errors="strict"))
        if not caps:
            raise FrameLogError("逐帧文件没有完整块；整段未证")
        return caps, "file"
    caps = parse(log_text)
    if not caps:
        raise FrameLogError("无逐帧文件或完整 logcat 块；整段未证")
    return caps, "logcat"


def _num(s):
    return int(s)


def parse(text):
    """logcat 文本 -> [Capture]。不完整的块（缺分片/重复/序号错/哈希不符）报红。"""
    captures = []
    meta = None
    parts = None  # dict idx -> payload
    for lineno, raw in enumerate(text.splitlines(), 1):
        m = META_RE.search(raw)
        if m:
            if meta is not None:
                raise FrameLogError(
                    f"line {lineno}: 新 meta 出现但上一块未 end（转储被截断）")
            meta = {"v": _num(m[1]), "clock": m[2], "unit": m[3], "endian": m[4],
                    "rec": _num(m[5]) if m[5] else None,
                    "cap": _num(m[6]), "recorded": _num(m[7]), "presents": _num(m[8]),
                    "dropped": _num(m[9]), "first": _num(m[10]), "bytes": _num(m[11]),
                    "chunks": _num(m[12]), "line": lineno}
            parts = {}
            continue
        m = PART_RE.search(raw)
        if m:
            if meta is None:
                raise FrameLogError(f"line {lineno}: 分片先于 meta 出现")
            i, n, payload = int(m[1]), int(m[2]), m[3]
            if n != meta["chunks"]:
                raise FrameLogError(f"line {lineno}: 分片总数 {n} 与 meta chunks={meta['chunks']} 不符")
            if i < 1 or i > n:
                raise FrameLogError(f"line {lineno}: 分片序号 {i} 越界 1..{n}")
            if i != len(parts) + 1:
                expected = len(parts) + 1
                if i > expected:
                    raise FrameLogError(f"line {lineno}: 缺分片 {list(range(expected, i))}；整段未证")
                raise FrameLogError(f"line {lineno}: 分片乱序/重复：期望第 {expected} 片实得第 {i} 片；整段未证")
            parts[i] = payload
            continue
        m = END_RE.search(raw)
        if m:
            if meta is None:
                raise FrameLogError(f"line {lineno}: end 先于 meta 出现")
            got = sorted(parts)
            want = list(range(1, meta["chunks"] + 1))
            missing = [i for i in want if i not in parts]
            if missing:
                raise FrameLogError(
                    f"块@line {meta['line']}: 缺分片 {missing}；整段未证")
            b64 = "".join(parts[i] for i in want)
            try:
                blob = base64.b64decode(b64, validate=True)
            except Exception as e:
                raise FrameLogError(f"块@line {meta['line']}: base64 非法: {e}")
            if len(blob) != meta["bytes"]:
                raise FrameLogError(
                    f"块@line {meta['line']}: 载荷 {len(blob)}B != meta bytes={meta['bytes']}")
            if int(m[1]) != meta["recorded"] or int(m[2]) != meta["dropped"]:
                raise FrameLogError(
                    f"块@line {meta['line']}: end 行 frames/dropped 与 meta 不符")
            if m[3].lower() != hashlib.sha256(blob).hexdigest():
                raise FrameLogError(f"块@line {meta['line']}: sha256 不符，载荷被改动/截断")
            if meta["v"] not in (2, 3, 4, 5) or meta["unit"] != "us" or meta["endian"] != "le":
                raise FrameLogError(f"块@line {meta['line']}: 不支持的 v/unit/endian（只认 v=2,3,4,5/us/le）")
            expected_rec = {2: 16, 3: 24, 4: 32, 5: 40}[meta["v"]]
            if meta["rec"] != expected_rec:
                raise FrameLogError(
                    f"块@line {meta['line']}: v={meta['v']} 记录必须 rec={expected_rec}（实得 rec={meta['rec']}）")
            if meta["recorded"] * expected_rec != meta["bytes"]:
                raise FrameLogError(f"块@line {meta['line']}: bytes != recorded*rec")
            if meta["recorded"] > meta["cap"] or meta["recorded"] > meta["presents"]:
                raise FrameLogError(f"块@line {meta['line']}: recorded 越界")
            if meta["first"] + meta["recorded"] != meta["presents"]:
                raise FrameLogError(
                    f"块@line {meta['line']}: first+recorded != presents（计数不自洽）")
            if meta["dropped"] != meta["first"]:
                raise FrameLogError(
                    f"块@line {meta['line']}: dropped != first（丢弃计数不自洽）")
            n = meta["recorded"]
            fmt, width = {2: ("QII", 3), 3: ("QIIII", 5), 4: ("QIIIIII", 7),
                          5: ("QIIIIIIII", 9)}[meta["v"]]
            flat = struct.unpack("<" + fmt * n, blob) if n else ()
            ts = [flat[i * width] for i in range(n)]
            fence_us = [flat[i * width + 1] for i in range(n)]
            fence_n = [flat[i * width + 2] for i in range(n)]
            acquire_us = [None if flat[i * width + 3] == 0xffffffff else flat[i * width + 3] for i in range(n)] if width >= 5 else None
            present_us = [None if flat[i * width + 4] == 0xffffffff else flat[i * width + 4] for i in range(n)] if width >= 5 else None
            gpu_us = [None if flat[i * width + 5] == 0xffffffff else flat[i * width + 5] for i in range(n)] if width >= 7 else None
            gpu_seq = [None if flat[i * width + 6] == 0xffffffff else flat[i * width + 6] for i in range(n)] if width >= 7 else None
            disp_us = [None if flat[i * width + 7] == 0xffffffff else flat[i * width + 7] for i in range(n)] if width == 9 else None
            disp_seq = [None if flat[i * width + 8] == 0xffffffff else flat[i * width + 8] for i in range(n)] if width == 9 else None
            captures.append({"meta": meta, "ts": ts, "fence_us": fence_us,
                             "fence_n": fence_n, "acquire_us": acquire_us,
                             "present_us": present_us, "gpu_us": gpu_us,
                             "gpu_seq": gpu_seq, "disp_us": disp_us,
                             "disp_seq": disp_seq, "line": meta["line"]})
            meta = None
            parts = None
            continue
    if meta is not None:
        missing = [i for i in range(1, meta["chunks"] + 1) if i not in parts]
        detail = f"，缺分片 {missing}" if missing else ""
        raise FrameLogError(f"块@line {meta['line']}: 文件结束仍无 end（转储被截断）{detail}；整段未证")
    return captures


def _pct(sorted_xs, q):
    """线性插值分位（type-7）；q∈[0,100]。空样本返回 None。"""
    n = len(sorted_xs)
    if not n:
        return None
    if n == 1:
        return sorted_xs[0]
    h = (n - 1) * q / 100.0
    lo = int(h)
    hi = min(lo + 1, n - 1)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (h - lo)


def analyze(cap):
    """Capture -> 报告 dict。间隔 = 相邻上屏时间戳差（µs），样本数 = recorded-1。"""
    ts = cap["ts"]
    meta = cap["meta"]
    diffs = [ts[i] - ts[i - 1] for i in range(1, len(ts))]
    nonmono = sum(1 for i in range(1, len(ts)) if ts[i] <= ts[i - 1])
    s = sorted(diffs)
    worst = s[len(s) - max(1, len(s) // 100):] if s else []
    longest_i = diffs.index(max(diffs)) if diffs else -1
    return {
        "meta": meta,
        "frames": len(ts),
        "intervals": len(diffs),
        "nonmono": nonmono,
        "mean_ms": sum(diffs) / len(diffs) / 1000.0 if diffs else None,
        "p50_ms": _pct(s, 50) / 1000.0 if s else None,
        "p95_ms": _pct(s, 95) / 1000.0 if s else None,
        "p99_ms": _pct(s, 99) / 1000.0 if s else None,
        "low1_ms": sum(worst) / len(worst) / 1000.0 if worst else None,
        "gt50": sum(1 for d in diffs if d > 50000),
        "gt100": sum(1 for d in diffs if d > 100000),
        "longest_ms": max(diffs) / 1000.0 if diffs else None,
        # 最长间隔的全局上屏序号（meta.first 起的 0 基）：i+1 = 间隔右端帧
        "longest_frame": meta["first"] + longest_i + 1 if diffs else None,
        "fence": _fence_stats(cap),
        "acquire": _timing_stats(cap.get("acquire_us")),
        "present": _timing_stats(cap.get("present_us")),
    }


def _timing_stats(values):
    if values is None:
        return None
    actual = [x for x in values if x is not None]
    if not actual:
        return None
    ordered = sorted(actual)
    return {"samples": len(actual), "missing": len(values) - len(actual),
            "total_ms": sum(actual) / 1000, "p95_ms": _pct(ordered, 95) / 1000,
            "max_ms": max(actual) / 1000}


def _fence_stats(cap):
    """每帧 fence 等待列：总次数/总时长/有等待的帧数/单帧最长。"""
    fus, fn = cap["fence_us"], cap["fence_n"]
    total_n = sum(fn)
    total_us = sum(fus)
    frames_with = sum(1 for x in fn if x)
    longest_us = max(fus) if fus else 0
    return {"waits": total_n, "wait_ms": total_us / 1000.0,
            "frames_with": frames_with, "longest_ms": longest_us / 1000.0,
            # 单帧最长等待的全局上屏序号
            "longest_frame": cap["meta"]["first"] + (fus.index(longest_us) if fus else 0)}


def _nrank(sorted_xs, i):
    return sorted_xs[max(0, min(i, len(sorted_xs) - 1))]


def crosscheck(cap, text):
    """与同日志里的 `frame timing(近16帧, frame=F)` 聚合行逐窗对拍。
    聚合窗 = 全局上屏序号 F-n+1..F 的 n 个 gap 样本；本记录全局序号 i（0基）
    对应 stats frame=i+1，gap_i = ts[i]-ts[i-1]。返回逐行结论。"""
    ts, first = cap["ts"], cap["meta"]["first"]
    rows = []
    for m in AGG_RE.finditer(text):
        w, f, gap, p50, p95, mx, n = (int(m[1]), int(m[2]), float(m[3]),
                                      float(m[4]), float(m[5]), float(m[6]), int(m[7]))
        # stats 帧号 f 是 1 基 present 计数；记录 0 基序号 j 对应 stats 帧 first+j+1。
        # 窗内 n 个 gap 样本属于 stats 帧 f-n+1..f，第 g 帧的 gap = ts[g-1-first]-ts[g-2-first]，
        # 右端记录序号 i = f-n-first+k（k=0..n-1），且首个样本还需要 ts[i-1]。
        idx = [f - n - first + k for k in range(n)]
        if idx[0] < 1 or idx[-1] >= len(ts):
            rows.append({"frame": f, "verdict": "不可比",
                         "why": f"窗口落在记录范围外（first={first}, kept={len(ts)}）"})
            continue
        samples = [(ts[i] - ts[i - 1]) / 1000.0 for i in idx]
        samples.sort()
        mean = sum(samples) / n
        # 与 C 侧同式 nearest-rank：i50=(n+1)/2-1，i95=(n*95+99)/100-1（再夹到 n-1）。
        cp50 = _nrank(samples, (n + 1) // 2 - 1)
        cp95 = _nrank(samples, (n * 95 + 99) // 100 - 1)
        cmax = samples[-1]
        tol = 0.15  # 聚合行按 %.1f 打；记录是 µs 整型，容差 0.15ms
        ok = (abs(mean - gap) <= tol and abs(cp50 - p50) <= tol
              and abs(cp95 - p95) <= tol and abs(cmax - mx) <= tol)
        rows.append({"frame": f, "n": n,
                     "verdict": "一致" if ok else "偏差",
                     "ours": (mean, cp50, cp95, cmax), "log": (gap, p50, p95, mx)})
    return rows


def report(cap, text):
    a = analyze(cap)
    m = a["meta"]
    out = []
    out.append(f"块@line {m['line']}: clock={m['clock']} unit=us endian=le")
    out.append(f"  上屏 {a['frames']} 次（全局序号 {m['first']}..{m['first'] + a['frames'] - 1}，"
               f"presents={m['presents']} dropped={m['dropped']}）")
    if not a["intervals"]:
        out.append("  间隔样本 0：只有 1 帧，统计不可得")
        return "\n".join(out)
    out.append(f"  间隔样本 {a['intervals']}：均值 {a['mean_ms']:.1f}ms "
               f"p50={a['p50_ms']:.1f} p95={a['p95_ms']:.1f} p99={a['p99_ms']:.1f} "
               f"1%low={a['low1_ms']:.1f} 最长={a['longest_ms']:.1f}ms(帧#{a['longest_frame']})")
    out.append(f"  >50ms: {a['gt50']} 次  >100ms: {a['gt100']} 次  "
               f"非递增戳: {a['nonmono']} 处")
    fz = a["fence"]
    if fz["waits"]:
        out.append(f"  fence 等待: {fz['waits']} 次 共 {fz['wait_ms']:.1f}ms "
                   f"（{fz['frames_with']} 帧有等待；单帧最长 "
                   f"{fz['longest_ms']:.1f}ms @帧#{fz['longest_frame']}）")
    else:
        out.append("  fence 等待: 0 次（无等待帧或 generic 路径未打点）")
    rows = crosscheck(cap, text)
    if rows:
        same = sum(1 for r in rows if r["verdict"] == "一致")
        diff = [r for r in rows if r["verdict"] == "偏差"]
        skip = sum(1 for r in rows if r["verdict"] == "不可比")
        out.append(f"  对拍 近16帧聚合行 {len(rows)} 条：一致 {same}，偏差 {len(diff)}，"
                   f"不可比 {skip}（落在环外）")
        for r in diff[:5]:
            o, l = r["ours"], r["log"]
            out.append(f"    偏差 frame={r['frame']} n={r['n']}: "
                       f"记录 gap={o[0]:.1f} p50={o[1]:.1f} p95={o[2]:.1f} max={o[3]:.1f} "
                       f"vs 聚合 {l[0]:.1f}/{l[1]:.1f}/{l[2]:.1f}/{l[3]:.1f}")
    else:
        out.append("  对拍：日志内无 `frame timing(近16帧...)` 聚合行可比对")
    return "\n".join(out)


def main(argv):
    if len(argv) < 2:
        sys.stderr.write(__doc__)
        return 2
    text = ""
    for path in argv[1:]:
        with (sys.stdin if path == "-" else open(path, encoding="utf-8", errors="replace")) as f:
            text += f.read()
    try:
        caps = parse(text)
    except FrameLogError as e:
        print(f"FAIL {e}")
        return 1
    if not caps:
        print("FAIL 日志内无 direct frame-log 块（开关没开？局末没到 shutdown？）")
        return 1
    rc = 0
    for i, cap in enumerate(caps):
        print(f"== 直连逐帧帧时间 #{i + 1}/{len(caps)} ==")
        print(report(cap, text))
        rows = crosscheck(cap, text)
        if any(r["verdict"] == "偏差" for r in rows):
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
