#!/bin/bash
# 出包工具在 GNU / Linux 上的可移植性单测（PATH 垫片模拟：GNU 语义的 stat、没有 sysctl / vm_stat；约 10 秒）：bash tests/linux_port_test.sh
#   grokbot 审计 #824 F-pack-stat-bsd / F-pack-sysctl-jobs。期望（按规则手写，不跑被测实现推）：
#   ① GNU stat 下 tests/srcver_test.sh 全过（审计复现：四条全红）
#   ② GNU stat 下 src_ids 的键是「路径 大小 修改时间」：同路径改成 100 字节 → 键里大小是 100、重新认包；没变再认 → 命中缓存不再调 nx_unpack
#   ③ stat 整个读不出 → src_ids 报红、退出非 0、不交出行（不把空键当键）
#   ⑤ 没有 sysctl：s2a_cores 退出 0，S2A_JOBS = 本机核数（os.cpu_count），重烘路数 = min(核数, ⌊⌊物理内存 GB⌋ × 3/4⌋ × 2/3)
#   ⑥ 核数读不出（getconf 坏）：s2a_cores 报红、退出非 0（不导出空的 S2A_JOBS 让改写阶段不限并发）
set -u; H=$(cd "$(dirname "$0")/.." && pwd); T=$(mktemp -d); trap 'rm -rf "$T"' EXIT; bad=0
ck() { if eval "$2"; then echo "通 $1"; else echo "不通 $1"; bad=1; fi; }
PY=$(command -v python3)
mkdir -p "$T/gnu" "$T/nostat" "$T/nogetconf"
# GNU stat 的子集：-c 格式（%s 大小、%Y 修改秒、%n 名字）；-f 是「文件系统统计」开关、不带格式参数（BSD 的格式串被当成文件名 → 退出 1，
#   读得到的文件照样打一段统计，空闲块数随时变）；-L 跟软链
cat > "$T/gnu/stat" <<EOF
#!$PY
import os, sys, time
a, fmt, fs, deref, files = sys.argv[1:], None, False, False, []
while a:
    x = a.pop(0)
    if x == '-c': fmt = a.pop(0)
    elif x == '-f': fs = True
    elif x == '-L': deref = True
    elif x.startswith('-') and len(x) > 1: sys.exit(f'stat: invalid option -- {x}')
    else: files.append(x)
rc = 0
for f in files:
    try: s = os.stat(f) if deref else os.lstat(f)
    except OSError: print(f"stat: cannot stat '{f}': No such file or directory", file=sys.stderr); rc = 1; continue
    if fs: print(f'  File: "{f}"\n    ID: 1 Namelen: 255 Type: ext2/ext3\nBlocks: Total: 999 Free: {time.time_ns() % 997} Available: 1'); continue
    if fmt is None: print(f'  File: {f}\n  Size: {s.st_size}'); continue
    n = s.st_mtime_ns
    print(fmt.replace('%s', str(s.st_size)).replace('%.9Y', f'{n // 10**9}.{n % 10**9:09d}').replace('%Y', str(int(s.st_mtime))).replace('%n', f))
sys.exit(rc)
EOF
printf '#!/bin/bash\necho "sysctl: command not found" >&2; exit 127\n' > "$T/gnu/sysctl"
printf '#!/bin/bash\necho "stat: 坏了" >&2; exit 1\n' > "$T/nostat/stat"
printf '#!/bin/bash\necho "getconf: 坏了" >&2; exit 1\n' > "$T/nogetconf/getconf"
chmod +x "$T"/*/*

o=$(PATH="$T/gnu:$PATH" bash "$H/tests/srcver_test.sh" 2>&1); r=$?
ck "① GNU stat 下 srcver_test 全过（rc=${r}）" '[ $r = 0 ]' || echo "$o" | grep 不通

mkdir -p "$T/src"; printf 123456 > "$T/src/a.nsp"; touch -t 202610200100 "$T/src/a.nsp"
cat > "$T/nxu" <<EOF
#!/bin/bash
echo "\$*" >> "$T/calls"; mkdir -p "\$2"; printf 'title_id\t0100aaaa00000000\n' > "\$2/unpack.tsv"
EOF
chmod +x "$T/nxu"
ids() { PATH="$1:$PATH" S2A_NX_UNPACK=$T/nxu S2A_GAME_SRC=$T/src S2A_SRC_IDS=$T/ids.tsv bash -c "source '$H/pack/srcs.sh'; src_ids"; }
calls() { awk 'END { print NR }' "$T/calls" 2> /dev/null || echo 0; }
ids "$T/gnu" > "$T/i1"; head -c 100 /dev/zero > "$T/src/a.nsp"; touch -t 202610200100 "$T/src/a.nsp"; ids "$T/gnu" > "$T/i2"; c2=$(calls); ids "$T/gnu" > "$T/i3"; c3=$(calls)
M=$("$PY" -c 'import os,sys;n=os.stat(sys.argv[1]).st_mtime_ns;print(f"{n//10**9}.{n%10**9:09d}")' "$T/src/a.nsp")
ck "② GNU stat：键 = 路径 / 100 / ${M}（实得 $(cut -f2,3 "$T/i2" | tr '\t' /)），换文件重认（调 ${c2} 次）、不变命中缓存（再认后 ${c3} 次）" \
  '[ "$(cut -f1-4 "$T/i2" | tr '\''\t'\'' '\''|'\'')" = "$T/src/a.nsp|100|$M|0100AAAA00000000" ] && [ "$(cut -f2 "$T/i1")" = 6 ] && [ "$c2" = 2 ] && [ "$c3" = 2 ] && [ "$(grep -c . "$T/i3")" = 1 ]'
# 审计 P1-54：同一秒、同大小换内容（只有纳秒不同）也要重认。手算期望：调用次数从 2 变 3
"$PY" -c 'import os,sys;p=sys.argv[1];n=os.stat(p).st_mtime_ns;open(p,"wb").write(b"x"*100);os.utime(p,ns=(n,n+1000))' "$T/src/a.nsp"
ids "$T/gnu" > "$T/i5"; c5=$(calls)
ck "②b 同秒同大小换内容重认（调 ${c5} 次，期望 3）" '[ "$c5" = 3 ]'
ids "$T/nostat" > "$T/i4" 2> "$T/e4"; r=$?
ck "③ stat 读不出 → 报红、rc=${r} 非 0、不交出行" '[ $r != 0 ] && [ ! -s "$T/i4" ] && grep -q 读不出 "$T/e4"'

# 钉死同机道数与负载（#1247：cores.sh 从 11-01 起还看 ~/.s2a/lanes 与 1 分钟负载，门禁机读到真实状态时红时绿）：空登记目录 + 负载 0，只验「没有 sysctl 时读得到核数 / 内存」
mkdir -p "$T/lanes"
cores() { env -u S2A_JOBS -u S2A_CPU_BAKE -u S2A_MEM_TOTAL_GB -u S2A_MEM_BAKE_GB -u S2A_LANES S2A_LANE_DIR="$T/lanes" S2A_CORES_QUIET=1 PATH="$1:$PATH" bash -c "source '$H/tools/cores.sh'; s2a_cores; echo \"\$? \${S2A_JOBS:-空} \${S2A_CPU_BAKE:-空}\""; }
W=$("$PY" -c 'import os; n=os.cpu_count(); g=os.sysconf("SC_PAGE_SIZE")*os.sysconf("SC_PHYS_PAGES")//(1<<30)*3//4*2//3; print(f"0 {n} {min(n, max(g, 1))}")')
o=$(cores "$T/gnu" 2>&1)
ck "⑤ 没有 sysctl：s2a_cores 读数「${o}」= 期望「${W}」" '[ "$o" = "$W" ]'
o=$(cores "$T/nogetconf" 2> "$T/e6"); r=${o%% *}
ck "⑥ 核数读不出 → 报红、rc=${r} 非 0（${o}）" '[ "$r" != 0 ] && grep -q 读不出 "$T/e6"'

[ $bad = 0 ] && echo "Linux 可移植性单测全过" || { echo "Linux 可移植性单测有不通"; exit 1; }
