#!/bin/bash
# 本机预跑整份令牌（11-09 维护方：中午几条线同时在本机预跑，负载冲到 200 多，偶发单测被挤红、读数不可信）：
#   全机同时在跑的本机预跑不超过 GATE_RUN_PAR 份（缺省 2；按本机核数 / 内存调）。tools/gate.sh 的每步令牌只限「步」，管不住几份门禁各自把步排满；
#   CPU 空闲排队只在开跑那一刻看一眼，几份同时看见空闲就一起起。这里在整份层面互斥。
#   令牌 = 内核文件锁 $GATE_RUN_TOKEN_DIR/run.<号>（缺省 ~/.s2a/gate_tokens；tools/flock_hold.py take-any）：持有者退出（含 kill -9）锁即放，不会漏。
#   领不到就排队并打「本机预跑排队中」，不硬跑。豁免：列车起的门禁（GATE_NO_LOADQ=1，优先级最高、直接跑）、门禁步里嵌套的门禁（GATE_IN_STEP，外层已占）。
#   两种用法：tools/gate.sh source 后调 runtok_take <持有者 pid>；手工跑的单测循环等：bash tools/run_token.sh <命令…>（领到后 exec 成该命令，命令结束即放）
runtok_exempt() { [ -n "${GATE_NO_LOADQ:-}" ] || [ -n "${GATE_IN_STEP:-}" ]; }
runtok_take() {   # runtok_take <持有者 pid>：0 领到；2 持锁小进程起不来（报红，不空等）
  local me=$1 d=${GATE_RUN_TOKEN_DIR:-$HOME/.s2a/gate_tokens} n=${GATE_RUN_PAR:-2} i ls= r rc w0=$SECONDS lw= py
  py=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/flock_hold.py
  mkdir -p "$d" || return 2
  for i in $(seq 1 "$n"); do ls="$ls${ls:+:}$d/run.$i"; done
  while :; do
    r=$(python3 "$py" "$ls" "$me" take-any gate_run 2> /dev/null); rc=$?
    if [ $rc = 0 ]; then
      printf '%s\n' "$me" > "${r#*$'\t'}.holder" 2> /dev/null
      [ -n "$lw" ] && echo "本机预跑排队结束：等了 $((SECONDS - w0)) 秒" >&2
      return 0
    fi
    [ $rc = 1 ] || { echo "【红】本机预跑令牌：持锁小进程起不来（flock_hold.py 返回 ${rc}）" >&2; return 2; }
    if [ -z "$lw" ] || [ $((SECONDS - lw)) -ge 60 ]; then lw=$SECONDS; echo "本机预跑排队中：${n} 个令牌全被占（${d}），已等 $((SECONDS - w0)) 秒" >&2; fi
    sleep "${GATE_RUN_POLL_S:-2}"
  done; }
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  [ $# -gt 0 ] || { echo "用法：bash tools/run_token.sh <命令…>"; exit 1; }
  runtok_exempt || runtok_take $$ || exit 75
  exec "$@"
fi
