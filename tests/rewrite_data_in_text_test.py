#!/usr/bin/env python3
"""代码段内嵌数据（#108）：REW=<C 改写器> python3 tests/rewrite_data_in_text_test.py
合成模块：一段 .eh_frame 覆盖的真实函数，后面接数据（hvc/smc/x18 假指令）；期望值按输入手写，不跑被测实现。"""
import importlib.util, os, struct, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('corpus', ROOT / 'tests/rewrite_corpus/run.py'); corpus = importlib.util.module_from_spec(spec); spec.loader.exec_module(corpus); corpus.REW = os.environ['REW']
SVC = 0xd4000021; TPIDRRO = 0xd53bd060; RET = 0xd65f03c0; NOP = 0xd503201f
HVC, SMC, DCPS = 0xd46f9025 | 2, 0xd42507b7 | 3 | 0, 0xd4add116 | 1   # 异常类：hvc / smc / dcps 形态
LDR_X18 = 0x58000012   # ldr x18, #0：动到平台寄存器的假指令
bad = 0


def chk(name, want, got):
    global bad
    ok = want == got; bad += not ok; print(f'{"通" if ok else "不通"} {name}：期望 {want!r} → 真结果 {got!r}')


def run(words, fde_len, tsz):
    with tempfile.TemporaryDirectory() as t:
        src = os.path.join(t, 'main'); corpus.nso(words, {}, fde_len, src, tsz=tsz)
        rc, gaps, _, out = corpus.rewrite(src, os.path.join(t, 'o'), 'ios')
        p = os.path.join(t, 'o', 'main.patched.bin')
        data = open(p, 'rb').read() if os.path.isfile(p) else b''
        sites = open(os.path.join(t, 'o', 'main.sites.tsv')).read() if os.path.isfile(os.path.join(t, 'o', 'main.sites.tsv')) else ''
        return rc, gaps, out, data, sites


FUNC = [TPIDRRO, SVC, NOP, RET]                     # 0x100：有 FDE 的真实函数（读线程指针、svc）
FLEN = len(FUNC) * 4
DATA_AT = 0x100 + FLEN
junk = ([HVC, SMC, DCPS, LDR_X18] * 200)            # 800 个字：足够的「不可能是代码」证据
TSZ = 0x4000
words = FUNC + junk
rc, gaps, out, data, sites = run(words, FLEN, TSZ)
chk('① 有内嵌数据：全部缺口排除、退出 0', (0, []), (rc, gaps))
chk('① 打出判定日志', True, '内嵌数据判定：启用' in out)
chk('① 真实函数里的 svc 仍改写', True, data[:4] != b'' and struct.unpack_from('<I', data, 0x100 + 4)[0] != SVC)
chk('① 数据一个字节都没动', struct.pack('<%dI' % len(junk), *junk), data[DATA_AT:DATA_AT + len(junk) * 4])

# ② 数据区里被分支指到的代码（闭包）：FDE 函数 b 到无 FDE 的一段，段里的 svc 要改写；其后的数据不算
STUB_AT = DATA_AT + 16
b = 0x14000000 | ((STUB_AT - (0x100 + 8)) // 4)
func2 = [TPIDRRO, NOP, b, RET]
stub = [SVC, RET]
gap_words = [NOP] * 0
words2 = func2 + [NOP, NOP, NOP, NOP] + stub + junk
rc, gaps, out, data, _ = run(words2, len(func2) * 4, TSZ)
chk('② 分支闭包：退出 0、无缺口', (0, []), (rc, gaps))
chk('② 闭包段里的 svc 改写了', True, struct.unpack_from('<I', data, STUB_AT)[0] != SVC)

# ③ 证据不足（只有几条 hvc）：不启用，缺口照报
rc, gaps, out, _, _ = run(FUNC + [HVC] * 8, FLEN, TSZ)
chk('③ 证据不足不启用，缺口照报', (3, 8), (rc, len(gaps)))
chk('③ 打出不启用', True, '内嵌数据判定：不启用' in out)

# ④ 异常类指令在函数范围内：范围不可信，不排除
inside = FUNC + junk
rc, gaps, out, _, _ = run(inside, len(inside) * 4, TSZ)
chk('④ 都在函数范围内：不启用、退出 3', (3, True), (rc, len(gaps) >= 600))
chk('④ 打出范围外只有 0 条', True, '范围外只有 0 条' in out)

# ⑤ 没有这类指令：不进判定，无相关日志（产物逐字节不变的前提）
rc, gaps, out, _, _ = run(FUNC, FLEN, TSZ)
chk('⑤ 普通模块不进判定', (0, [], False), (rc, gaps, '内嵌数据判定' in out))

# ⑥ 超大代码段（288 MB）：远处的真实代码只靠分支闭包发现，要能改写到补丁岛
FAR = 0x6000000   # 96 MB：B 指令够得着，函数体在 FDE 外
TSZ6 = 0x12000000
with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, 'main')
    b6 = 0x14000000 | ((FAR - (0x100 + 8)) // 4)
    func6 = [TPIDRRO, NOP, b6, RET]
    corpus.nso(func6, {}, len(func6) * 4, src, tsz=TSZ6)
    with open(src, 'r+b') as f:
        f.seek(0x100 + 0x1000); f.write(struct.pack('<%dI' % len(junk), *junk))          # 数据：足够的证据
        f.seek(0x100 + FAR); f.write(struct.pack('<2I', SVC, RET))                        # 远处真实代码
        f.seek(0x100 + TSZ6 - 0x10000); f.write(struct.pack('<%dI' % len(junk), *junk))   # 代码段尾部也有数据
    rc, gaps, out, *_ = corpus.rewrite(src, os.path.join(t, 'o'), 'ios')
    data = open(os.path.join(t, 'o', 'main.patched.bin'), 'rb').read() if rc == 0 else b''
    chk('⑥ 超大代码段：退出 0、无缺口', (0, []), (rc, gaps))
    chk('⑥ 远处真实代码改写了', True, bool(data) and struct.unpack_from('<I', data, FAR)[0] != SVC)
    chk('⑥ 尾部数据没动', struct.pack('<4I', *junk[:4]), data[TSZ6 - 0x10000:TSZ6 - 0x10000 + 16] if data else b'')

print(f'{"✓" if not bad else "✗"} rewrite_data_in_text：{bad} 不通')
sys.exit(1 if bad else 0)
