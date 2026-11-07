#!/usr/bin/env python3
"""改写器规模（#251 EA SPORTS FC 27 改写 main 超过 600 秒被停）：REW=<C 改写器> python3 tests/rewrite_scale_test.py
一个函数里成千上万条 br x18 时，原先每条都把整个函数重扫好几遍（跳转目标表每次重算且不释放、写寄存器计数、外部跳入、汇合点），
耗时随条数平方涨：本机 M 系列上 4000 张跳转表 38 秒、8000 条认不出的 br x18 23 秒，1 万多张就撞 600 秒上限。
合成模块（多段 .eh_frame），期望值按输入手写、不跑被测实现；时限按「线性实现百分之一秒级、平方实现几十秒」取 15 秒，留足慢机器余量。"""
import os, re, struct, subprocess, sys, tempfile, time

REW = os.environ['REW']
RET, NOP = 0xd65f03c0, 0xd503201f
LIMIT = 15
bad = 0


def chk(name, want, got):
    global bad
    ok = want == got; bad += not ok; print(f'{"通" if ok else "不通"} {name}：期望 {want!r} → 真结果 {got!r}')


def nso(path, code, funcs):
    """code 从 0x100 起；funcs = [(起点, 长度)] 各一条 FDE（.eh_frame_hdr 表按起点排序）"""
    tsz = (0x100 + 4 * len(code) + 0xfff) & ~0xfff
    text = bytearray(tsz); struct.pack_into('<II', text, 0, 0x14000040, tsz); struct.pack_into('<%dI' % len(code), text, 0x100, *code)
    nf = len(funcs); EH = 0x100; TB = EH + 12; EF = (TB + 8 * nf + 15) & ~15
    rosz = (EF + 0x18 + 0x10 * nf + 0x10 + 0xfff) & ~0xfff
    ro = bytearray(rosz)
    struct.pack_into('<4sIIIii', ro, 0, b'MOD0', 0, 0, 0, EH, EH + 0x20)
    struct.pack_into('<BBBBiI', ro, EH, 1, 0x1B, 0x03, 0x3B, (tsz + EF) - (tsz + EH + 4), nf)
    ro[EF:EF + 0x18] = struct.pack('<II', 0x14, 0) + bytes([1]) + b'zR\0' + bytes([4, 0x78, 30, 1, 0x1B]) + bytes(7)
    for k, (fa, fl) in enumerate(funcs):
        f = tsz + EF + 0x18 + 0x10 * k
        struct.pack_into('<IIii', ro, EF + 0x18 + 0x10 * k, 0x0c, (f + 4) - (tsz + EF), fa - (f + 8), fl)
        struct.pack_into('<ii', ro, TB + 8 * k, fa - (tsz + EH), f - (tsz + EH))
    segs = ((0, bytes(text)), (tsz, bytes(ro)), (tsz + rosz, bytes(0x1000)))
    h = bytearray(0x100); h[:4] = b'NSO0'
    for k, (mo, seg) in enumerate(segs):
        struct.pack_into('<III', h, 0x10 + 0x10 * k, 0x100 + sum(len(s[1]) for s in segs[:k]), mo, len(seg)); struct.pack_into('<I', h, 0x60 + 4 * k, len(seg))
    h[0x40:0x50] = bytes(range(16))
    open(path, 'wb').write(bytes(h) + b''.join(s[1] for s in segs))


def run(code, funcs):
    with tempfile.TemporaryDirectory() as t:
        src = os.path.join(t, 'main'); nso(src, code, funcs)
        t0 = time.monotonic()
        r = subprocess.run([REW, src, os.path.join(t, 'o'), 'main'], capture_output=True, text=True, timeout=600, env=dict(os.environ, S2A_TARGET='ios'))
        m = re.search(r'PFRW mod=main need=(\d+) gaps=(\d+)', r.stdout)
        return r.returncode, (int(m[1]), int(m[2])) if m else None, time.monotonic() - t0


# ① 一个函数 4000 张相对跳转表：adrp x9 / add x9 / cmp w10,#1 / b.hi 出块 / ldrsw x18,[x9,x10,lsl#2] / add x18,x9,x18 / br x18。
#    表在本页（全是指令字，第 0 项目标出函数）→ 每张都认不出：每块 3 处碰 x18（ldrsw、add、br），缺口 = 每张 1 处
N = 4000
blk = [0x90000009, 0x91000129, 0x7100055f, 0x54000088, 0xb8aa7932, 0x8b120132, 0xd61f0240]
code = blk * N + [RET]
rc, stat, sec = run(code, [(0x100, 4 * len(code))])
chk(f'① {N} 张跳转表：退出 3、需改写 {3 * N} 处、缺口 {N} 处', (3, (3 * N, N)), (rc, stat))
chk(f'① {LIMIT} 秒内跑完（实测 {sec:.2f} 秒）', True, sec < LIMIT)

# ② 一个函数 8000 条认不出的 br x18（mov x18,x1 / br x18）：每条走完全部识别兜底（相对表 / 目标集 / 开放表 / adr 表 / 借 x16-x17 重试）
N = 8000
code = [0xaa0103f2, 0xd61f0240] * N + [RET]
rc, stat, sec = run(code, [(0x100, 4 * len(code))])
chk(f'② {N} 条 br x18：退出 3、需改写 {2 * N} 处、缺口 {N} 处', (3, (2 * N, N)), (rc, stat))
chk(f'② {LIMIT} 秒内跑完（实测 {sec:.2f} 秒）', True, sec < LIMIT)

sys.exit(1 if bad else 0)
