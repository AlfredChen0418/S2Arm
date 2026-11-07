#!/bin/bash
# 管线分片本机 + gate-host 并行编单测（graphics/tools/shbake/pso_dist.sh）：S2A_REMOTE=local 本机模拟远端（远端根 / 假家目录 / 日志全在临时目录，不碰 gate-host），假编译器。退出 0 = 全过
# 假编译器：查分片脚本里每个函数库路径都存在、远端只认远端假家目录下的（远端要用改写后的路径、库要先传过去），睡 0.5 秒，产物 = 管线描述按键排序的 JSON（与库路径无关），
#   在 $HOME/calls 记一行（本机 HOME = 临时 home，远端 HOME = 远端假家目录）→ 两边各编了几片直接数。期望产物由本单测自己按同一变换从输入算，不经被测脚本。
# 期望（16 片，本机 PJ 2、远端 PJ 2）：
#   ① gate-host 可用：16 片产物齐、内容 = 期望；两边都编了（远端 ≥ 1、本机 ≥ 1）且合计 16 次；「分工」行 gate-host 片数 = 远端编译次数；16 个缓存项
#   ② 同输入再来（新产物目录）：16 片全命中、两边 0 次编译、产物同 ①
#   ③ S2A_PSO_REMOTE=off：远端 0 次、本机 16 次
#   ④ 远端工具链不同（维护方工具 退 125）：全在本机、产物齐对
#   ⑤ 工作树有未提交改动（维护方工具 只跑提交态）：全在本机、产物齐对
#   ⑥ 远端编译全失败：失败的片退回本机重编，产物齐对、没有 .fail 标记
#   ⑦ 远端服务一开编就整组被 SIGKILL（不留 .fail、维护方工具 退出）：在途片退回本机，远端 0 次，产物齐对，日志报「远端服务退了」
#   ⑫ PSO_NTJ=5：本机与远端编译器每次调用都带 -j 5
#   ⑬ 1500 片本机独编（S2A_PSO_REMOTE=off）、最后一片慢：每片恰好 1 次、收尾不逐片 fork（basename 0 次）、有进度行
# 反证（提交前手工做过）：送片前不改写库路径 → ① 远端全失败退回、远端 0 次（① 红）；release 改空且本机领片进程不收回死送片进程的领标记 → ⑦ 卡到 120 秒上限（⑦ 红）
set -u; H=$(cd "$(dirname "$0")/.." && pwd); RT=$(cd "$H/../../.." && pwd); T=$(mktemp -d "${TMPDIR:-/tmp}/t_pso_dist.XXXXXX"); bad=0
# 本测试漏下的进程：维护方工具 / pso_dist.sh 里当前目录或命令行在本次 mktemp 目录下的（按唯一路径认，不按名字杀；lsof 报的是 /private/var…，按子串比）
TR=$(cd "$T" && pwd -P)   # 规范化：TMPDIR 以 / 结尾时 $T 含「//」，lsof 报的 cwd 是 /private/var/…，不规范化 cwd 匹配永远不中（画面着色器线审查）
left() { local p c; for p in $(pgrep -f 'tools/remote.sh|pso_dist.sh (run|serve)'); do c=$(lsof -a -p $p -d cwd -Fn 2> /dev/null | sed -n 's/^n//p')
  case "$c" in "$TR"/*) echo $p; continue;; esac; case "$(ps -o command= -p $p)" in *"$T"*) echo $p;; esac; done; }
# 收尾先 TERM 漏下的，免得不通时孤儿留在门禁机器上排 / 占满核槽
trap 'kill $(left) 2> /dev/null; [ -n "${KEEP:-}" ] || rm -rf "$T"' EXIT
# 判 ✗ 时打现场（10-21 画面·着色器线：⑥⑦ 偶发多出约 60 秒、疑似上一轮孤儿 维护方工具 占着模拟槽，失败现场已删、没坐实）：
#   最近一轮（LR，go 记下）日志尾 10 行、模拟远端满核槽的持有者 pid 与命令行、本轮 维护方工具 日志里的「排队」行
scene() { echo "    | 现场：第 ${LR:-?} 轮日志尾"; tail -10 "$T/${LR:-x}.log" 2> /dev/null | sed 's/^/    |   /'
  for l in "$T"/logs/slot.*; do [ -d "$l" ] || continue; p=$(cat "$l/pid" 2> /dev/null); echo "    | 槽 ${l##*/}：持有者 ${p:-无}（$([ -n "$p" ] && ps -o command= -p "$p" 2> /dev/null | cut -c1-120 || echo 已不在)）"; done
  grep -h '排队' $(ls -t "$T"/logs/pso-*/*.log 2> /dev/null | head -3) 2> /dev/null | tail -5 | sed 's/^/    | remote：/'; }
ck() { if eval "$2"; then echo "✓ $1"; else echo "✗ $1"; scene; bad=1; fi; }
export S2A_REMOTE=local S2A_REMOTE_ROOT=$T/rr S2A_REMOTE_LOGS=$T/logs HOME=$T/home S2A_REMOTE_WAIT_S=60 S2A_REMOTE_MIN_FREE_GB=0   # 假远端就是本机临时目录：派活前的远端余量闸（缺省 30 G）量的是本机真盘，盘紧时 ①⑥⑦ 误红（11-09 守望 3 次，可用 15–29 G）；本单测不测余量闸
mkdir -p "$T/home" "$T/repo/tools/bin" "$T/repo/graphics/tools/shbake"; cd "$T/repo" || exit 1
mkdir -p "$T/home/.s2arm/run/s2arm/bin"; echo '# 测试用局槽仲裁副本' > "$T/home/.s2arm/run/s2arm/bin/mac_slot-now.sh"   # 维护方工具 本机缺副本不派（维护方工具）
git init -q; git config user.email t@t; git config user.name t
cp "$RT/tools/safe_killpg.sh" "$RT/tools/idle_timeout.sh" tools/; REM=${S2A_PRIVATE_TOOLS:+$S2A_PRIVATE_TOOLS/remote.sh}
if [ -n "$REM" ] && [ -f "$REM" ]; then cp "$REM" tools/remote.sh; cp "$S2A_PRIVATE_TOOLS/disk_low.sh" tools/; else REM=
echo "－ 未覆盖：远端分发 ①②④⑤⑥⑦⑩⑫（远端脚本 tools/remote.sh 是维护方私仓工具；设 S2A_PRIVATE_TOOLS 才测）"; fi
 cp "$RT/tools/bin/timeout" tools/bin/; cp "$H/pso_dist.sh" "$H/pso_chunk.sh" "$H/pso_isolate.py" graphics/tools/shbake/; git add -A; git commit -qm 1
cat > "$T/fake_nt" <<'EOF'
#!/bin/bash
case $HOME in */rr/home) touch "${HOME%/rr/home}/remote_tried";; esac   # 远端（假家目录）编了一片：落标记，本机 wait_nt 据此开工
o=; i=; while [ $# -gt 0 ]; do [ "$1" = -o ] && o=$2; [ "$1" = -N ] && i=$2; shift; done
python3 - "$i" "$o" <<'P' || exit 1
import json, os, sys
j = json.load(open(sys.argv[1]))
assert all(os.path.exists(l['path']) for l in j['libraries']), '库路径不存在'
# 远端（HOME = 远端假家目录）只认自己家目录下的库：本机模拟时本机路径也存在，不这样查就测不出「送片前没改写库路径」
assert not os.environ['HOME'].endswith('/rr/home') or all(l['path'].startswith(os.environ['HOME'] + '/') for l in j['libraries']), '远端用了本机库路径'
json.dump(j['pipeline_descriptors'], open(sys.argv[2], 'w'), sort_keys=True)
P
sleep 0.5; echo "$(basename "$i")" >> "$HOME/calls"
EOF
printf '#!/bin/bash\ntouch "%s/remote_tried"; exit 1\n' "$T" > "$T/fail_nt"
# ①⑥⑦ 测的是「远端真编了 / 试过、失败 / 被杀后退回本机」：本机编译器先等远端真试过一片（最多 60 秒，只等一次）再编，免得负载高时远端服务还没起来本机就领完、测不到退回路径（10-21 列车前复跑 3 例偶发不通）
printf '#!/bin/bash\nfor i in $(seq 1 600); do [ -e "%s/remote_tried" ] && break; sleep 0.1; done; touch "%s/remote_tried"\nexec "%s/fake_nt" "$@"\n' "$T" "$T" "$T" > "$T/wait_nt"   # 只等一次：超时也落标记，后面的片不再等
cat > "$T/die_nt" <<EOF
#!/bin/bash
touch "$T/remote_tried"
# 父进程是 timeout（自成一组），再上一层是远端领片进程，它与 serve 同组：整组杀 = 服务没了（timeout 与编译器自成一组，不在其内）
# 10-21 两次本机全部用户进程被 SIGKILL（08:40、09:21）的根因：领片进程已退出时 \$sp 是 launchd（pid 1），
# 它的进程组是 1，kill -KILL -- -1 = 杀掉本用户全部进程。只杀本测试临时目录里起的进程组，其余情况报错退出、不杀
sp=\$(ps -o ppid= -p \$PPID | tr -d ' '); pg=\$(ps -o pgid= -p "\$sp" 2> /dev/null | tr -d ' ')
# 经全仓唯一入口 tools/safe_killpg.sh：拒绝组号非数字 / ≤ 1 / 自己的组，组长命令行须含本测试临时目录（tests/killpg_lint_test.sh 禁止绕开它直接 kill -- -<组>）
. "$RT/tools/safe_killpg.sh"; safe_killpg KILL "\$pg" "$T" || exit 1; sleep 5
EOF
chmod +x "$T/fake_nt" "$T/fail_nt" "$T/die_nt" "$T/wait_nt"
W=$T/w; mkdir -p "$W" "$T/lib"; echo lib > "$T/lib/gal.0.metallib"; echo lib > "$T/lib/gal.1.metallib"
for n in $(seq 0 15); do
  printf '{"libraries":[{"label":"gal0","path":"%s/gal.0.metallib"},{"label":"gal1","path":"%s/gal.1.metallib"}],"pipeline_descriptors":{"compute_pipeline_descriptors":[{"n":%d,"pad":"%0*d"}]}}' "$T/o0" "$T/o0" $n $((n * 40 + 1)) 0 > "$W/pso.$n.mtl4-json"
  echo "k$n" > "$W/pso.$n.key"
done
ls "$W"/pso.*.mtl4-json > "$W/list"
exp() { python3 -c 'import json,sys;print(json.dumps(json.load(open(sys.argv[1]))["pipeline_descriptors"],sort_keys=True),end="")' "$W/pso.$1.mtl4-json"; }
export PSO_NT=$T/fake_nt PSO_REMOTE_NT=$T/fake_nt PSO_A=g PSO_PLAT=ios PSO_M=27 PSO_SV=27.0 PSO_SP=/x PSO_NTV=v1 PSO_SIG=s PSO_PC=$T/pc PSO_W=$W PSO_PJ=2 PSO_MJ=2
go() {   # go <标签> [环境…]：新产物目录（库放进去，分片脚本的库路径指向 o0 → 每次把库同步到 o0）跑一轮，回显远端 / 本机编译次数
  rm -rf "$T/o0" "$W"/*.hit "$W"/*.fail "$T/home/calls" "$T/rr/home/calls"; mkdir -p "$T/o0"; cp "$T"/lib/gal.*.metallib "$T/o0/"
  # 每轮用时与退出码进本单测日志（10-21 ⑥⑦ 负载 80 以上偶发双红、现场抓不到：再红时看是哪轮慢、是否撞 120 秒上限）
  LR=$1; g0=$SECONDS; (cd "$T" && timeout 120 env PSO_O="$T/o0" "${@:2}" bash "$T/repo/graphics/tools/shbake/pso_dist.sh" run "$W/list") > "$T/$1.log" 2>&1; grc=$?; echo "  [计时] 第 $1 轮 $((SECONDS - g0)) 秒、退出码 ${grc}（124 = 撞上限）"   # 当前目录不在仓库里（出包流程不保证）
  RC=$(cat "$T/rr/home/calls" 2> /dev/null | grep -c .); LC=$(cat "$T/home/calls" 2> /dev/null | grep -c .)
  # 跑完不留进程（10-21：feeder 杀的是 rs 函数的外层子 shell，维护方工具 成孤儿继续排 / 占 gate-host 满核槽，每次重烘漏一个，gate-host 一下午编 0 片）
  for i in $(seq 1 50); do [ -z "$(left)" ] && break; sleep 0.2; done   # 远端子进程靠 stdin 断开感知、再收组，要 2–4 秒：按事件等，上限约 10 秒
  n=$(left | grep -c .); [ "$n" -gt 0 ] && LEFT="$LEFT ${1}:${n}（$(for p in $(left); do ps -o ppid=,command= -p $p | cut -c1-80; done | tr '\n' ';')）"
}
LEFT=
allok() { local n; for n in $(seq 0 15); do [ "$(cat "$T/o0/gal_pso.g.ios27.$n.bin" 2> /dev/null)" = "$(exp $n)" ] || return 1; done; }
if [ -n "$REM" ]; then
rm -f "$T/remote_tried"; go 1 PSO_NT=$T/wait_nt; MS=$(grep -o 'gate-host [0-9]* 片' "$T/1.log" | grep -o '[0-9]*')
ck "① gate-host 可用：产物齐且对、两边都编（远端 ${RC} / 本机 ${LC}）、合计 16、分工行 gate-host ${MS:-?} 片 = 远端次数、缓存 $(find "$T/pc" -name '*.bin' | grep -c .) 项" \
  'allok && [ $RC -ge 1 ] && [ $LC -ge 1 ] && [ $((RC + LC)) = 16 ] && [ "${MS:-x}" = $RC ] && [ "$(find "$T/pc" -name "*.bin" | grep -c .)" = 16 ]'
go 2; ck "② 同输入再来：全命中（命中标记 $(ls "$W"/*.hit 2> /dev/null | grep -c .)）、两边 0 次（远端 ${RC} / 本机 ${LC}）、产物同 ①" 'allok && [ $RC = 0 ] && [ $LC = 0 ] && [ "$(ls "$W"/*.hit | grep -c .)" = 16 ]'
rm -rf "$T/pc"
fi
go 3 S2A_PSO_REMOTE=off; ck "③ S2A_PSO_REMOTE=off：远端 ${RC}、本机 ${LC}" 'allok && [ $RC = 0 ] && [ $LC = 16 ]'
if [ -n "$REM" ]; then
rm -rf "$T/pc"; mkdir -p "$T/rr/tools/bin"; printf '#!/bin/sh\necho 9.9.9\n' > "$T/rr/tools/bin/bash"; chmod +x "$T/rr/tools/bin/bash"
go 4; rm -rf "$T/rr/tools"; ck "④ 远端工具链不同：全在本机（远端 ${RC} / 本机 ${LC}）、产物齐对" 'allok && [ $RC = 0 ] && [ $LC = 16 ] && grep -qE "远端服务没起来|远端服务起来前本机已全部领完" "$T/4.log"'
rm -rf "$T/pc"; echo x >> tools/remote.sh
go 5; git checkout -q tools/remote.sh; ck "⑤ 未提交改动：全在本机（远端 ${RC} / 本机 ${LC}）、产物齐对" 'allok && [ $RC = 0 ] && [ $LC = 16 ]'
rm -rf "$T/pc"
rm -f "$T/remote_tried"; go 6 PSO_NT=$T/wait_nt PSO_REMOTE_NT=$T/fail_nt; ck "⑥ 远端编译全失败：退回本机重编（本机 ${LC}）、产物齐对、没有 .fail" 'allok && [ $LC = 16 ] && ! ls "$W"/*.fail > /dev/null 2>&1 && grep -q "编失败" "$T/6.log"'
rm -rf "$T/pc"
rm -f "$T/remote_tried"; go 7 PSO_NT=$T/wait_nt PSO_REMOTE_NT=$T/die_nt; ck "⑦ 远端服务中途被杀：在途退回本机（远端 ${RC} / 本机 ${LC}）、产物齐对、日志报远端服务退了" 'allok && [ $RC = 0 ] && [ $LC = 16 ] && grep -q "远端服务退了" "$T/7.log"'
fi
# ⑧ 一片 6 条里第 3 条让编译器崩（pso_isolate.py）：期望隔离出它、其余 5 条照收进归档（内容 = 去掉第 3 条的描述）、.bad 点名 s3、没有 .fail
# ⑨ 一片 20 条全编不过：期望坏的超过上限 8 就停、按整片失败（.fail、没有归档、没有 .bad），试编次数 < 整片二分 2×20−1 = 39
iso() {   # iso <片号> <条数> <编译器>：单编一片（不经分发），回显本机编译次数 IC
  local k; rm -f "$W"/pso_g.ios27."$1".* "$T/o0/gal_pso.g.ios27.$1.bin" "$T/home/calls"; mkdir -p "$T/o0"; cp "$T"/lib/gal.*.metallib "$T/o0/"
  { printf '{"libraries":[{"label":"gal0","path":"%s/gal.0.metallib"}],"function_descriptors":{"library_function_descriptors":[' "$T/o0"
    for k in $(seq 0 $(($2 - 1))); do [ $k = 0 ] || printf ','; printf '{"label":"L%d","name":"s%d","library":"gal0"}' $k $k; done
    printf ']},"pipeline_descriptors":{"render_pipeline_descriptors":[],"compute_pipeline_descriptors":['
    for k in $(seq 0 $(($2 - 1))); do [ $k = 0 ] || printf ','; printf '{"compute_function_descriptor":"fnd:L%d","n":%d}' $k $k; done
    printf ']}}'; } > "$W/pso.$1.mtl4-json"
  rm -f "$W/pso.$1.key"; (cd "$T" && PSO_O="$T/o0" PSO_NT="$3" timeout 120 bash "$T/repo/graphics/tools/shbake/pso_chunk.sh" "$W/pso.$1.mtl4-json") > "$T/iso$1.log" 2>&1
  IC=$(cat "$T/home/calls" 2> /dev/null | grep -c .)
}
printf '#!/bin/bash\no=; i=; a=("$@"); while [ $# -gt 0 ]; do [ "$1" = -N ] && i=$2; shift; done\ngrep -q %s "$i" && exit 1; exec "%s/fake_nt" "${a[@]}"\n' "'\"fnd:L3\"'" "$T" > "$T/bad3_nt"
printf '#!/bin/bash\necho x >> "$HOME/calls"; exit 1\n' > "$T/allbad_nt"; chmod +x "$T/bad3_nt" "$T/allbad_nt"
iso 20 6 "$T/bad3_nt"; E8=$(python3 -c 'import json,sys;j=json.load(open(sys.argv[1]));d=j["pipeline_descriptors"];d["compute_pipeline_descriptors"]=[x for x in d["compute_pipeline_descriptors"] if x["n"]!=3];print(json.dumps(d,sort_keys=True),end="")' "$W/pso.20.mtl4-json")
ck "⑧ 一片 6 条第 3 条致崩：隔离出 s3、其余 5 条照收、没有 .fail（试编 ${IC} 次）" '[ "$(cat "$T/o0/gal_pso.g.ios27.20.bin" 2> /dev/null)" = "$E8" ] && [ "$(cat "$W/pso_g.ios27.20.bad" 2> /dev/null)" = s3 ] && [ ! -e "$W/pso_g.ios27.20.fail" ]'
iso 21 20 "$T/allbad_nt"
ck "⑨ 一片 20 条全编不过：超上限即停、按整片失败（.fail、无归档、无 .bad），试编 ${IC} 次 < 39" '[ -e "$W/pso_g.ios27.21.fail" ] && [ ! -e "$T/o0/gal_pso.g.ios27.21.bin" ] && [ ! -e "$W/pso_g.ios27.21.bad" ] && [ $IC -lt 39 ] && [ $IC -ge 9 ]'
# ⑩ 远端满核槽被占（别的远端任务拿着）、本机先全领完：feeder 收掉还在排队的 维护方工具（10-21 漏下的孤儿在 gate-host 槽前排了一小时）
if [ -n "$REM" ]; then
rm -rf "$T/pc"; mkdir -p "$T/logs/slot.local.1"; echo $$ > "$T/logs/slot.local.1/pid"   # 清产物缓存：前几例已填满，不清全命中、本机 0 次
go 10; rm -rf "$T/logs/slot.local.1"; ck "⑩ 远端槽被占、本机先领完：全在本机（远端 ${RC} / 本机 ${LC}）、产物齐对、日志报本机已全部领完" 'allok && [ $RC = 0 ] && [ $LC = 16 ] && grep -q "远端服务起来前本机已全部领完" "$T/10.log"'
# ⑫ PSO_NTJ=5（编译线程数，进缓存键）：本机与远端编译器都拿到 -j 5（远端经 维护方工具 --env 带过去；--put 按本机键入缓存，两边线程数不同缓存就错）
printf '#!/bin/bash\necho "$*" >> "$HOME/args"; exec "%s/fake_nt" "$@"\n' "$T" > "$T/jr_nt"; printf '#!/bin/bash\necho "$*" >> "$HOME/args"; exec "%s/wait_nt" "$@"\n' "$T" > "$T/jrw_nt"; chmod +x "$T/jr_nt" "$T/jrw_nt"
rm -rf "$T/pc" "$T/remote_tried" "$T/home/args" "$T/rr/home/args"; go 12 PSO_NTJ=5 PSO_NT=$T/jrw_nt PSO_REMOTE_NT=$T/jr_nt
JA=$(cat "$T/home/args" "$T/rr/home/args" 2> /dev/null | grep -c .); JR=$(grep -c -- ' -j 5 ' "$T/rr/home/args" 2> /dev/null); JL=$(grep -c -- ' -j 5 ' "$T/home/args" 2> /dev/null)
ck "⑫ PSO_NTJ=5：远端 ${RC} 片调用里带 -j 5 的 ${JR:-0}、本机 ${LC} 片里 ${JL:-0}，产物齐对" 'allok && [ $RC -ge 1 ] && [ "${JR:-0}" = $RC ] && [ "${JL:-0}" = $LC ] && [ $JA = 16 ]'
fi
# ⑬ 大清单本机独编收尾（10-25 #266 M5 Max 6357 片：送片进程不在时空转的工人每轮逐个 basename + cat 全部领标记，一轮十几分钟，看门狗 600 秒杀阶段）：
#   1500 片、S2A_PSO_REMOTE=off、本机 4 个工人，最小的一片编 4 秒（别的工人先扫完一轮进入收尾）。PATH 最前放 basename 垫片记调用次数。
#   期望（按规则手写）：每片恰好编 1 次（1500）、产物 1500 个；收尾 basename 调用 0 次（旧实现每轮 1500 次）；日志有「进度：… 做完 1500 / 1500 片」
B=$T/big; mkdir -p "$B/w" "$B/bin" "$B/home"; printf '#!/bin/bash\necho x >> %s/basename.calls\nexec /usr/bin/basename "$@"\n' "$B" > "$B/bin/basename"; chmod +x "$B/bin/basename"
cat > "$B/quick_nt" <<EOF2
#!/bin/bash
o=; i=; while [ \$# -gt 0 ]; do [ "\$1" = -o ] && o=\$2; [ "\$1" = -N ] && i=\$2; shift; done
case \$i in *.0.mtl4-json) sleep 4;; esac
echo x > "\$o"; echo "\$(/usr/bin/basename "\$i")" >> "$B/home/calls"
EOF2
chmod +x "$B/quick_nt"
python3 - "$B/w" "$T/o0" <<'PY'
import sys
for n in range(1500):   # 片 0 最小（排在最后编）：大的先编（LPT）按分片脚本大小降序
    pad = 'x' * (0 if n == 0 else 50 + n % 7)
    open(f'{sys.argv[1]}/pso.{n}.mtl4-json', 'w').write('{"libraries":[{"label":"gal0","path":"%s/gal.0.metallib"}],"pipeline_descriptors":{"compute_pipeline_descriptors":[{"n":%d,"pad":"%s"}]}}' % (sys.argv[2], n, pad))
PY
find "$B/w" -name 'pso.*.mtl4-json' | LC_ALL=C sort > "$B/list"; rm -rf "$T/o0"; mkdir -p "$T/o0"; cp "$T"/lib/gal.*.metallib "$T/o0/"
g0=$SECONDS; (cd "$T" && timeout 300 env PATH="$B/bin:$PATH" HOME="$B/home" PSO_O="$T/o0" PSO_W="$B/w" PSO_PC="$B/pc" PSO_NT="$B/quick_nt" PSO_PJ=4 PSO_PROGRESS_S=2 S2A_PSO_REMOTE=off \
  bash "$T/repo/graphics/tools/shbake/pso_dist.sh" run "$B/list" 1500) > "$T/13.log" 2>&1; brc=$?
BC=$(grep -c . "$B/basename.calls" 2> /dev/null); BC=${BC:-0}; NC=$(grep -c . "$B/home/calls" 2> /dev/null); NU=$(sort -u "$B/home/calls" 2> /dev/null | grep -c .)
NB=0; for f in "$T"/o0/gal_pso.g.ios27.*.bin; do [ -e "$f" ] && NB=$((NB + 1)); done
ck "⑬ 1500 片本机独编收尾：退出 ${brc}、编 ${NC} 次（不同片 ${NU}）、产物 ${NB}、收尾 basename ${BC} 次、用时 $((SECONDS - g0)) 秒、有进度行" \
  '[ $brc = 0 ] && [ $NC = 1500 ] && [ $NU = 1500 ] && [ $NB = 1500 ] && [ $BC = 0 ] && grep -q "进度：.*做完 1500 / 1500 片" "$T/13.log"'
ck "⑪ 每轮跑完无残留进程（还引用本测试临时目录的进程：${LEFT:-无}）" '[ -z "$LEFT" ]'
[ $bad = 0 ] && echo "pso_dist 单测全过" || { echo "pso_dist 单测有不通（KEEP=1 留现场 ${T}）"; exit 1; }
