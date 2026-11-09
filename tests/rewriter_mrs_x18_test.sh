#!/bin/bash
# 门禁覆盖：runtime/rewriter/rewriter.c
# mrs x18, tpidr_el0（#52）：改写器要把线程指针写进虚拟 x18 槽，不再记缺口。合成 NSO，约 5 秒：bash tests/rewriter_mrs_x18_test.sh
# 期望按 AArch64 语义手写：原位 b 到跳板；跳板存三个临时寄存器 → 取 tpidr 槽的值 → 存进 x18 槽（键页偏移 0）→ 恢复 → 跳回下一条
set -u; H=$(cd "$(dirname "$0")/.." && pwd); T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
PY=${S2A_PY:-$HOME/.s2arm/venv/bin/python3}; [ -x "$PY" ] || PY=python3
REW=${REW:-$(python3 "$H/pack/rewriter.py" c-path 2>/dev/null | tail -1)}; [ -x "$REW" ] || { echo "不通 取不到 C 改写器"; exit 1; }
mkdir -p "$T/exefs" "$T/c"
$PY - "$T/exefs/main" <<'EOF'
import struct, sys
code = [0xd53bd052, 0xd65f03c0]   # mrs x18, tpidr_el0 / ret
text = bytearray(0x1000); struct.pack_into('<II', text, 0, 0x14000040, 0x1000); struct.pack_into('<%dI' % len(code), text, 0x100, *code)
ro = bytearray(0x1000); EH, EF = 0x100, 0x200
struct.pack_into('<4sIIIii', ro, 0, b'MOD0', 0, 0, 0, EH, EH + 0x20)
struct.pack_into('<BBBBiIii', ro, EH, 1, 0x1B, 0x03, 0x3B, (0x1000 + EF) - (0x1000 + EH + 4), 1, 0x100 - (0x1000 + EH), (0x1000 + EF + 0x18) - (0x1000 + EH))
cie = struct.pack('<II', 0x14, 0) + bytes([1]) + b'zR\0' + bytes([4, 0x78, 30, 1, 0x1B]) + bytes(7)
ro[EF:EF + 0x18] = cie; f = 0x1000 + EF + 0x18
struct.pack_into('<IIii', ro, EF + 0x18, 0x10, (f + 4) - (0x1000 + EF), 0x100 - (f + 8), 4 * len(code))
text, ro, data = bytes(text), bytes(ro), bytes(0x1000)
h = bytearray(0x100); h[:4] = b'NSO0'; fo = 0x100
for k, (mo, seg) in enumerate(((0, text), (0x1000, ro), (0x2000, data))):
    struct.pack_into('<III', h, 0x10 + 0x10 * k, fo + 0x1000 * k, mo, len(seg)); struct.pack_into('<I', h, 0x60 + 4 * k, len(seg))
h[0x40:0x50] = bytes(range(16))
open(sys.argv[1], 'wb').write(bytes(h) + text + ro + data)
EOF
"$REW" "$T/exefs/main" "$T/c" main > "$T/c.log" 2>&1; rc=$?
grep -q 'gaps=0' "$T/c.log" && [ "$rc" = 0 ] || { echo "不通 期望 gaps=0、退出 0，实得 rc=${rc}：$(grep -E 'GAP|PFRW' "$T/c.log" | tail -2)"; exit 1; }
$PY - "$T/c" <<'EOF' || { echo "不通 跳板序列不符"; exit 1; }
import struct, sys
from capstone import Cs, CS_ARCH_ARM64, CS_MODE_ARM
d = sys.argv[1]; key = next(int(l.split('\t')[1], 16) for l in open(d + '/main.fixups.tsv') if l.startswith('# KEY_PAGE_OFF\t'))
b = open(d + '/main.patched.bin', 'rb').read(); md = Cs(CS_ARCH_ARM64, CS_MODE_ARM)
w, = struct.unpack_from('<I', b, 0x100); assert w >> 26 == 0b000101, hex(w)
t = 0x100 + 4 * (w & 0x3ffffff)
got = [f'{i.mnemonic} {i.op_str}' for i in md.disasm(b[t:t + 4 * 16], t)][:16]
print('\n'.join(got))
want_head = ['stp x0, x1, [sp, #-0x10]', 'stur x2, [sp, #-0x18]', 'mrs x1, tpidrro_el0']
assert got[:3] == want_head, got[:3]
# 先读 tpidr 槽（slot 1，偏移 8）到 x0，再写进 x18 槽（slot 0）
assert got[5:8] == ['add x1, x1, x2', 'ldr x0, [x1]', 'mrs x1, tpidrro_el0'], got[5:8]
assert got[10] == 'add x1, x1, x2' and got[11] == 'str x0, [x1]', got[10:12]
assert got[12:14] == ['ldur x2, [sp, #-0x18]', 'ldp x0, x1, [sp, #-0x10]'], got[12:14]
assert got[14].startswith('b #') and int(got[14].split('#')[1], 16) == 0x104, got[14]
EOF
echo "🟢 mrs x18, tpidr_el0 改写：序列符合期望，缺口 0"
