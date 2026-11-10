#!/bin/bash
# 出包并行度按同机在跑道数（tools/cores.sh s2a_lanes，11-01）：不传 S2A_LANES 的入口（普查 / 快测 / 手动出包）不再各当整台是自己的。约 2 秒：bash tests/cores_lanes_test.sh
# 期望值只用本机核数 nc（getconf）手算，不调被测函数推：
#   ① 另有 2 道活着（各自 source cores.sh 算过、还没退）→ 第 3 道 S2A_JOBS = nc / 3
#   ② 那 2 道退出后 → 本道 S2A_JOBS = nc（死掉的登记被删）
#   ③ 传了 S2A_LANES=5、只有本道在跑 → nc / 5（取两者大的）
#   ④ 调用方给了 S2A_JOBS=3 → 原样 3、不登记（嵌套入口继承导出值，不重复算道数）
#   ⑤ 别人再忙也不扣核（11-06）：假 iostat 报 0% 空闲 → 仍 = 核数，且根本不去采样（假 iostat 被调就留记号）
#   ⑥ 降优先级：入口进程 nice = max(10, 进来时的 nice)；S2A_NICE=0 → 不动（= 进来时的 nice）
#   ⑨ 8 核、另 1 道在跑 → 核数 ÷ 道数 4
# 门禁输入：tools/cores.sh tests/cores_lanes_test.sh pack/refresh.sh
set -u; H=$(cd "$(dirname "$0")/.." && pwd); D=$(mktemp -d "${TMPDIR:-/tmp}/s2a-lanes-XXXXXX"); trap 'kill $P1 $P2 2> /dev/null; rm -rf "$D"' EXIT
nc=$(getconf _NPROCESSORS_ONLN); bad=0
ck() { if [ "$1" = "$2" ]; then echo "通 $3：期望 $2 → 真结果 $1"; else echo "不通 $3：期望 $2 → 真结果 $1"; bad=$((bad + 1)); fi; }
run() { env -u S2A_JOBS -u S2A_CPU_BAKE -u S2A_LANES S2A_LANE_DIR="$D" S2A_MEM_TOTAL_GB=1024 S2A_CORES_QUIET=1 "$@" bash -c "source '$H/tools/cores.sh'; s2a_cores; echo \$S2A_JOBS"; }
hold() { env -u S2A_JOBS -u S2A_CPU_BAKE -u S2A_LANES S2A_LANE_DIR="$D" S2A_MEM_TOTAL_GB=1024 S2A_CORES_QUIET=1 bash -c "source '$H/tools/cores.sh'; s2a_cores; exec sleep 60" & }
hold; P1=$!; hold; P2=$!
for i in $(seq 1 50); do [ "$(ls "$D" | wc -l)" -ge 2 ] && break; sleep 0.1; done
ck "$(run)" "$(( nc / 3 > 0 ? nc / 3 : 1 ))" "① 另 2 道在跑"
kill $P1 $P2; wait $P1 $P2 2> /dev/null
ck "$(run)" "$nc" "② 那 2 道退了"
ck "$([ -e "$D/$P1" ] || [ -e "$D/$P2" ] && echo 还在 || echo 已删)" "已删" "② 退出的那 2 道的登记被删（下一次数道数时顺手删）"
ck "$(run env S2A_LANES=5)" "$(( nc / 5 > 0 ? nc / 5 : 1 ))" "③ S2A_LANES=5 只有本道"
n0=$(ls "$D" | wc -l | tr -d ' ')
ck "$(run env S2A_JOBS=3)" "3" "④ 给了 S2A_JOBS"
ck "$(ls "$D" | wc -l | tr -d ' ')" "$n0" "④ 给了 S2A_JOBS 不登记"
# ⑤ 别人再忙也不扣核、不采样（11-06 出包速度线：玩家机后台一忙整单只剩一半核）：假 iostat 报 0% 空闲，被调就写记号
S=$(mktemp -d "${TMPDIR:-/tmp}/s2a-iostat-XXXXXX")
printf '#!/bin/sh\ntouch "%s/called"\necho "    KB/t  tps  MB/s  us sy id"\necho "   10.00    1  0.01  50 50 0"\n' "$S" > "$S/iostat"; chmod +x "$S/iostat"
ck "$(run env PATH="$S:$PATH" S2A_NCPU=8)" "8" "⑤ 8 核、别人占满 CPU → 仍 8 路"
ck "$([ -e "$S/called" ] && echo 采了 || echo 没采)" "没采" "⑤ 入口不再采样别人的占用"; rm -rf "$S"
# ⑥ 降优先级：期望 = max(10, 进来时的 nice)（本测试进程的 nice 手读，不调被测函数）
n0=$(ps -o nice= -p $$ | tr -d ' '); want=$(( n0 > 10 ? n0 : 10 ))
nice_of() { env -u S2A_JOBS -u S2A_CPU_BAKE -u S2A_LANES S2A_LANE_DIR="$D" S2A_MEM_TOTAL_GB=1024 S2A_CORES_QUIET=1 "$@" bash -c "source '$H/tools/cores.sh'; s2a_cores; ps -o nice= -p \$\$ | tr -d ' '"; }
ck "$(nice_of)" "$want" "⑥ 入口进程降到 nice $want"
ck "$(nice_of env S2A_NICE=0)" "$n0" "⑥ S2A_NICE=0 不动"
# ⑨ 8 核、另 1 道在跑 → 核数 ÷ 道数 = 4
hold; P3=$!; for i in $(seq 1 50); do [ -e "$D/$P3" ] && break; sleep 0.1; done
ck "$(run env S2A_NCPU=8)" "4" "⑨ 8 核、另 1 道在跑 → 4"; kill $P3; wait $P3 2> /dev/null
# ⑩ 已进跑局的道不摊核（11-02 普查本机：同机 9–11 单多数在排 / 跑 Mac 局，冷翻译单只分到 2 核）：另 2 道在跑、其中 1 道登记标 mac → 只算本道 + 1 道 → nc ÷ 2
hold; P4=$!; hold; P5=$!; for i in $(seq 1 50); do [ -e "$D/$P4" ] && [ -e "$D/$P5" ] && break; sleep 0.1; done
echo mac > "$D/$P5"
ck "$(run)" "$(( nc / 2 > 0 ? nc / 2 : 1 ))" "⑩ 另 2 道在跑、其中 1 道已进跑局（登记 mac）→ 只摊出包中的 → 核数 ÷ 2"; kill $P4 $P5; wait $P4 $P5 2> /dev/null
# ⑫ 直连库编译路数 S2A_DIRECT_J（11-04 玩家路径 J）= min(S2A_JOBS, 1 + (物理 × 0.75 ÷ 道数 − 1740 − 1065) ÷ 420 MB)，下限 1；期望手算：
#   8 GB / 8 核单道：可用 6144 MB → 1 + 3339 ÷ 420 = 1 + 7 = 8 → 8 路（重活 S2A_CPU_BAKE 仍 = 8 × 3/4 × 2/3 = 4，原先直连库也用它 = 4 路）
#   16 GB / 10 核：12288 → 1 + 22 = 23 → 封顶核数 10；4 GB / 8 核：3072 → 1 + 0 = 1；8 GB / 8 核、S2A_LANES=2：可用 3072 → 1，且 ≤ 核数 ÷ 2 = 4
dj() { env -u S2A_JOBS -u S2A_CPU_BAKE -u S2A_DIRECT_J -u S2A_LANES S2A_LANE_DIR="$D" S2A_CORES_QUIET=1 "$@" bash -c "source '$H/tools/cores.sh'; s2a_cores; echo \$S2A_DIRECT_J/\$S2A_CPU_BAKE"; }
ck "$(dj env S2A_NCPU=8 S2A_MEM_TOTAL_GB=8)" "8/4" "⑫ 8 GB / 8 核：直连库 8 路、重活 4 路"
ck "$(dj env S2A_NCPU=10 S2A_MEM_TOTAL_GB=16)" "10/8" "⑫ 16 GB / 10 核：直连库封顶核数 10、重活 8"
ck "$(dj env S2A_NCPU=8 S2A_MEM_TOTAL_GB=4)" "1/2" "⑫ 4 GB / 8 核：直连库 1 路（下限）、重活 2"
ck "$(dj env S2A_NCPU=8 S2A_MEM_TOTAL_GB=8 S2A_LANES=2)" "1/2" "⑫ 8 GB / 8 核两道：每道可用 3 GB → 直连库 1 路"
ck "$(dj env S2A_NCPU=8 S2A_MEM_TOTAL_GB=8 S2A_DIRECT_J=3)" "3/4" "⑫ 调用方给了 S2A_DIRECT_J=3 → 原样"
# ⑬ 接线：pack/refresh.sh 两处出直连库都按 J > S2A_DIRECT_J > S2A_CPU_BAKE 取路数
ck "$(grep -c 'CT=${J:-${S2A_DIRECT_J:-$S2A_CPU_BAKE}}' "$H/pack/refresh.sh")" "2" "⑬ refresh.sh 两处直连库路数取 S2A_DIRECT_J"
# ⑭ 读物理内存不靠 PATH 上的 python3（11-10 Intel Mac 实走：anaconda 3.9 的 os.sysconf 不认 SC_PHYS_PAGES，整单停在并行度）：
#   PATH 最前放一个必失败的假 python3、不给 S2A_MEM_TOTAL_GB → 仍算得出；期望 GB 用系统自带读法手算（macOS sysctl / Linux /proc/meminfo）
F=$(mktemp -d "${TMPDIR:-/tmp}/s2a-fakepy-XXXXXX")
printf '#!/bin/sh\necho "ValueError: unrecognized configuration name" >&2\nexit 1\n' > "$F/python3"; chmod +x "$F/python3"
if [ "$(uname -s)" = Darwin ]; then want_gb=$(( $(sysctl -n hw.memsize) >> 30 )); else want_gb=$(( $(awk '/^MemTotal:/ { print $2; exit }' /proc/meminfo) >> 20 )); fi
mem() { env -u S2A_JOBS -u S2A_CPU_BAKE -u S2A_DIRECT_J -u S2A_LANES -u S2A_MEM_TOTAL_GB -u S2A_PYTHON PATH="$F:$PATH" S2A_LANE_DIR="$D" S2A_CORES_QUIET=1 S2A_NCPU=8 \
  bash -c "source '$H/tools/cores.sh'; s2a_cores > /dev/null 2>&1 && echo \$(( (\$S2A_CPU_BAKE > 0) )) || echo 失败"; }
ck "$(mem)" "1" "⑭ PATH 上的 python3 坏了（anaconda 3.9 那种），不给 S2A_MEM_TOTAL_GB 也算得出并行度（本机 ${want_gb} GB）"; rm -rf "$F"
# ⑮ 入口接线（#447 评审）：一键出包选定 Python 后把它所在目录放到 PATH 最前，出包链各 .sh 的裸 python3 都落到同一个解释器
ck "$(grep -c '^export S2A_PYTHON=$PY S2A_PY=$PY PATH="$(dirname "$PY"):$PATH"$' "$H/pack/oneclick/oneclick.sh")" "1" "⑮ oneclick.sh 把选定的 Python 目录放到 PATH 最前"
echo "同机道数并行度单测：$([ $bad = 0 ] && echo 全通 || echo "$bad 项不通")"; exit $(( bad > 0 ))
