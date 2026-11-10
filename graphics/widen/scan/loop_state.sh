#!/bin/bash
# 闭环局结论三态（画幅线，10-29）：graphics/widen/scan/loop_state.sh <局目录> → 打一行「<态>\t<说明>」，态 = 过 / 不过 / 未判
#   过：verdict.tsv 非空、每行都过。
#   不过：主局到了判断点（host.log 到 MARK，或开着点集崩了——崩是点集的结论）、判出画面问题：①–⑤ 有一行不过；
#        或主局各项都过、只有⑩ / 界面局的项不过，且那一局自己起来了（对照局还要到 MARK）。没有界面路线的⑥不过照旧是不过（没证据不开）。
#   未判：基础设施失败，或没到判断点又不是画面原因（局超时、起了没崩没到 MARK）。未判不是结论：auto_table.sh 不缓存、不按工具版本冻结，下次触发重排。
# 10-29 普查：closed_loop.sh 局没起也写 verdict.tsv（行行不过），auto_table.sh 见判据非空就记「不过」并按工具版本冻结——等负载 3601 秒、构建失败、
#   链号不一致、快照失败、槽时被构建吃掉的超时，50 格里至少 8 格这样冻住。
# 基础设施信号只认现有日志里的确定行（出处）：
#   run.out「；局没起」          维护方工具 run：独占局等 CPU 空闲超时（旧措辞「出包没停或负载没降」同尾）、排队超时，退 3
#   run.out「🔴 构建失败」        runtime/run_mac.sh：make host 失败（缺 toolchain.env、nxaot 运行时包不在等）
#   run.out「【红】Mac 起局链号」  tools/mac_chain_check.py：链号不一致 / 检查失败（【黄】显式放行不算）
#   run.out 改写 / 判 GPU 驱动模块失败 / 没有改写产物   runtime/run_mac.sh 起局前的准备
#   save.txt「【红】存档：」       tools/mac_save.sh mac_save_prep（run_mac.sh 把它的输出写进 save.txt）
#   run.out「局超时（拿到槽后」   维护方工具 run：timeout 杀局（rc=124）。局长 260 秒 + 宿主墙钟 30 秒 < 槽时 295 秒，到这一步 = 起局前的改写 / 构建吃了槽时
#   局目录没有 host.log           宿主没起（以上各条没认出的兜底）
#   快照失败、产物根刷新红、重判时局目录不全 / 构建号对不上、写表失败：closed_loop.sh 自己在出判据之前（或之后）就退 75，不经这里
set -u
D=${1:?局目录}; V=$D/verdict.tsv
out() { printf '%s\t%s\n' "$1" "$2"; exit 0; }
infra() {   # infra <子局目录>：打基础设施失败的确定信号（一句）；没有就什么都不打
  local x=$1 l p
  for p in '；局没起' '🔴 构建失败' '【红】Mac 起局链号' '🔴 .* 改写 .* 失败' '判 GPU 驱动模块失败' '既没有改写产物也没有 exefs' '局超时（拿到槽后'; do
    l=$(LC_ALL=C /usr/bin/grep -a -m1 -E "$p" "$x/run.out" 2> /dev/null) && { echo "$l" | cut -c1-200; return; }
  done
  l=$(LC_ALL=C /usr/bin/grep -a -m1 '【红】存档：' "$x/save.txt" 2> /dev/null) && { echo "$l" | cut -c1-200; return; }
  [ -s "$x/host.log" ] || echo "局没起（$x 没有 host.log）"
}
marked() { LC_ALL=C /usr/bin/grep -a -q 'S2A 标记 MARK' "$1/host.log" 2> /dev/null; }
crashed() { LC_ALL=C /usr/bin/grep -a -q CRASH "$1/host.log" 2> /dev/null; }
[ -s "$V" ] || out 未判 "没出判据（verdict.tsv 空：闭环在起局前就退了）"
bad=$(LC_ALL=C awk -F'\t' '$2 != "过" { printf "%s%s", (n++ ? "、" : ""), $1 }' "$V")
[ -z "$bad" ] && out 过 全过
w=$(infra "$D"); [ -n "$w" ] && out 未判 "主局：$w"
marked "$D" || crashed "$D" || out 未判 "主局起了、没崩、没到 MARK（自动玩家没进游戏场景）"
mb=$(LC_ALL=C awk -F'\t' '$2 != "过" && $1 !~ /^(⑥|⑦|⑧|⑨|⑩)/ { printf "%s%s", (n++ ? "、" : ""), $1 }' "$V")
[ -n "$mb" ] && out 不过 "主局判出：$mb 不过"
if LC_ALL=C awk -F'\t' '$2 != "过" && $1 ~ /^⑩/ { f = 1 } END { exit !f }' "$V"; then   # ⑩ 要对照局（不开点集）同录像窗的录像
  w=$(infra "$D/base"); [ -n "$w" ] && out 未判 "对照局：$w"
  marked "$D/base" || out 未判 "对照局没到 MARK（不开点集，不是点集的问题）"
fi
if [ -e "$D/ui/run.out" ] && LC_ALL=C awk -F'\t' '$2 != "过" && $1 ~ /^(⑥|⑦|⑧|⑨)/ { f = 1 } END { exit !f }' "$V"; then   # 界面局起过（没有界面路线时不起、⑥判不过照旧）
  w=$(infra "$D/ui"); [ -n "$w" ] && out 未判 "界面局：$w"
fi
out 不过 "判出：$bad 不过"
