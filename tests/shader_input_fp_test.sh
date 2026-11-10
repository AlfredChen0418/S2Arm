#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 S2Arm contributors
# 门禁覆盖：graphics/tools/shader_pack.sh
# 门禁输入：tools/toolchain.sh tools/toolchain.env graphics/tools/shbake/key_norm.py pack/jobs/archs.py pack/pso_targets.tsv
# 原创合成夹具；真实 --key / 抽取入口，Apple 工具与 BSD stat 用协议替身，不宣称 Mac 实测。
set -euo pipefail
H=$(cd "$(dirname "$0")/.." && pwd)
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
SP=$H/graphics/tools/shader_pack.sh
mkdir -p "$T/root/romfs" "$T/root/exefs" "$T/bin" "$T/prebuilt/gal" "$T/prebuilt/shbake"
printf main > "$T/root/exefs/main"
printf a > "$T/root/romfs/space name"
printf '{}' > "$T/prebuilt/prepare.json"
for f in gal/gal_xc shbake/S2A.ShBake shbake/gpu_arch; do
  printf '#!/bin/sh\nexit 0\n' > "$T/prebuilt/$f"; chmod +x "$T/prebuilt/$f"
done
cat > "$T/bin/xcrun" <<'STUB'
#!/bin/sh
case "$*" in *--show-sdk-version*) echo 27.1;; *--version*) echo 'Apple metal version 32023.1';; *) exit 1;; esac
STUB
# macOS 的 /usr/bin/git 是校验 DEVELOPER_DIR 的 shim：假目录下整只 git 静默死（key_norm 的 git show 全空）。包装卸 DEVELOPER_DIR 再 exec 真 git；对不吃该变量的 git 无影响。
cat > "$T/bin/git" <<STUB
#!/bin/sh
exec env -u DEVELOPER_DIR "$(command -v git)" "\$@"
STUB
# 只模拟 stat 参数协议与失败输出；不替换 input_fp / key_of / 抽取缓存代码。
cat > "$T/bin/stat" <<'STUB'
#!/usr/bin/env python3
import os, sys
mode = os.environ.get('FP_STAT_MODE', 'gnu')
args = sys.argv[1:]
with open(os.environ['FP_STAT_LOG'], 'a') as f:
    f.write(args[0] + '\n')
if mode == 'broken' or (mode == 'bsd' and args[:2] != ['-f', '%N %z']):
    print('failed stat stdout must not become a fingerprint')
    print('synthetic stat failure', file=sys.stderr)
    sys.exit(1)
if args[:2] not in (['-c', '%n %s'], ['-f', '%N %z']):
    sys.exit(2)
for name in args[2:]:
    print(f'{name} {os.stat(name).st_size}')
STUB
chmod +x "$T/bin/"*
export PATH="$T/bin:$PATH" FP_STAT_LOG="$T/stat.log" LC_ALL=C
export S2A_PREBUILT_TOOLS="$T/prebuilt" S2A_TC_RESOLVED=1 DEVELOPER_DIR="$T" S2A_TC_METAL=fixture
export S2A_PSO_ARCHS=applegpu_g16p S2A_PSO_OS=27 S2A_PACK_PLAT=ios S2A_PYTHON="$(command -v python3)" J=1
key() { bash "$SP" --key "$T/root"; }
# 直接读取生产函数（不复制实现）；用手写清单算独立预期，包含原 BSD 字节格式。
sed -n '/^input_fp()/,/^}/p' "$SP" > "$T/input.sh"
# input_fp 的 romfs 文件清单经 $R/graphics/nvn/tools/romfs_list.py files0（仓库里唯一的 RomFS 遍历），在 shader_pack.sh 里由全局 R / PY 给
fp() { R=$H PY=python3 bash -c 'source "$1"; input_fp "$2"' _ "$T/input.sh" "$T/root"; }
expected=$(printf 'exefs/main 4\nromfs/space name 1\n' | shasum -a 256; printf main | shasum -a 256)
bad=0
ck() { if eval "$2"; then echo "通 $1"; else echo "不通 $1"; bad=1; fi; }
# GNU 宿主时真正使用系统 stat；其他宿主仍测 GNU 参数协议替身，明确标注。
REAL_STAT=$(PATH=${PATH#*:} command -v stat)
if "$REAL_STAT" -c '%n %s' "$T/root/exefs/main" >/dev/null 2>&1; then
  mv "$T/bin/stat" "$T/stat-stub"; echo '环境：真实 GNU stat'
else
  echo '环境：无 GNU stat，GNU 分支为协议替身'
fi
k1=$(key); printf longer > "$T/root/romfs/space name"; k2=$(key)
ck '① 同路径 romfs 长度 1→6，真实 --key 改变' '[ "$k1" != "$k2" ]'
printf a > "$T/root/romfs/space name"
[ ! -f "$T/stat-stub" ] || mv "$T/stat-stub" "$T/bin/stat"
export FP_STAT_MODE=bsd
actual=$(fp)
ck '② BSD fallback 与手写旧格式指纹逐字相同' '[ "$actual" = "$expected" ]'
: > "$FP_STAT_LOG"; k3=$(key)
ck '③ --key fallback 先 GNU 后 BSD，键与 GNU 相同' '[ "$k3" = "$k1" ] && [ "$(cat "$FP_STAT_LOG")" = "$(printf -- "-c\n-f")" ]'
mkdir -p "$T/root/aoc/1/romfs"; printf dlc > "$T/root/aoc/1/romfs/data"
k4=$(key); printf larger > "$T/root/aoc/1/romfs/data"; k5=$(key)
ck '④ DLC 长度变化改变 --key' '[ "$k4" != "$k5" ]'
printf MAIN > "$T/root/exefs/main"; k6=$(key)
ck '⑤ main 同长度内容变化改变 --key' '[ "$k5" != "$k6" ]'
k7=$(key); ck '⑥ 不变输入重复键相同' '[ "$k6" = "$k7" ]'
export FP_STAT_MODE=broken
rc=0; fp > "$T/out" 2> "$T/err" || rc=$?
ck '⑦ 两种 stat 都失败：函数非零且不输出指纹' '[ "$rc" -ne 0 ] && [ ! -s "$T/out" ]'
rc=0; key > "$T/out" 2> "$T/err" || rc=$?
ck '⑧ 两种 stat 都失败：真实 --key 非零且不输出键' '[ "$rc" -ne 0 ] && [ ! -s "$T/out" ]'
# 原创最小 NPDM 仅供入口读程序号，不含游戏数据。失败必须在抽取命令之前停止。
python3 - "$T/root/main.npdm" <<'PY'
import struct, sys
b = bytearray(0x100)
struct.pack_into('<I', b, 0x70, 0x80)
b[0x80:0x84] = b'ACI0'
struct.pack_into('<Q', b, 0x90, 1)
open(sys.argv[1], 'wb').write(b)
PY
rc=0; bash "$SP" "$T/root" "$T/bake" > "$T/out" 2> "$T/err" || rc=$?
ck '⑨ 抽取入口 stat 失败非零，未调用抽取器' '[ "$rc" -ne 0 ] && [ ! -e "$T/bake/work/extract.log" ]'
[ "$bad" = 0 ] && echo 'shader_input_fp 9/9 全过'
