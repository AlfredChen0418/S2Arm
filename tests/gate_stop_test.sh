#!/bin/bash
# 门禁覆盖：tools/gate_stop.sh tools/gate.sh
# 只停本树的门禁（tools/gate_stop.sh；11-09 起因：按进程名 pkill 门禁误伤别的会话与列车）：bash tests/gate_stop_test.sh
#   期望按规则手写：A、B 两棵假工作树各跑一份「写 pid 文件 + 带一个子进程」的假 tools/gate.sh；停 A → A 的门禁与子进程都不在、pid 文件删了，B 两个都还在；
#   pid 文件指向不是门禁的进程 → 退 1、不动它；pid 文件残留（进程已不在）→ 退 0、清掉文件；没在跑 → 退 0
set -u; H=$(cd "$(dirname "$0")/.." && pwd); T=$(mktemp -d); bad=0; PIDS=
trap 'for q in $PIDS; do kill -KILL "$q" 2> /dev/null; done; rm -rf "$T"' EXIT
ck() { if eval "$2"; then echo "通 $1"; else echo "不通 $1"; bad=1; fi; }
mk() { mkdir -p "$T/$1/tools" "$T/$1/build"; git init -q "$T/$1"; printf '#!/bin/bash\necho $$ > .git/s2a_gate.pid; trap "exit 143" TERM\nsleep 300 & echo $! > build/child.pid; wait\n' > "$T/$1/tools/gate.sh"
  (cd "$T/$1" && exec bash tools/gate.sh) & PIDS="$PIDS $!"
  for _ in $(seq 50); do [ -s "$T/$1/build/child.pid" ] && break; sleep 0.1; done; }
mk A; mk B
ga=$(cat $T/A/.git/s2a_gate.pid); ca=$(cat $T/A/build/child.pid); gb=$(cat $T/B/.git/s2a_gate.pid); cb=$(cat $T/B/build/child.pid)
o=$(GATE_STOP_WAIT_S=3 bash "$H/tools/gate_stop.sh" "$T/A"); r=$?
ck "停 A：退 0、A 的门禁和子进程都不在、pid 文件删了（rc=${r}：${o}）" '[ $r = 0 ] && ! kill -0 $ga 2> /dev/null && ! kill -0 $ca 2> /dev/null && [ ! -e $T/A/.git/s2a_gate.pid ]'
ck "B 不受影响：门禁与子进程都还在" 'kill -0 $gb 2> /dev/null && kill -0 $cb 2> /dev/null'
sleep 300 & other=$!; PIDS="$PIDS $other"; echo $other > $T/B/.git/s2a_gate.pid
o=$(bash "$H/tools/gate_stop.sh" "$T/B"); r=$?
ck "pid 文件指向不是门禁的进程 → 退 1、不动它（rc=${r}）" '[ $r = 1 ] && kill -0 $other 2> /dev/null'
kill -KILL $other 2> /dev/null; wait $other 2> /dev/null
o=$(bash "$H/tools/gate_stop.sh" "$T/B"); r=$?
ck "pid 文件残留（进程已不在）→ 退 0、清掉文件（rc=${r}）" '[ $r = 0 ] && [ ! -e $T/B/.git/s2a_gate.pid ]'
mkdir -p $T/C; git init -q $T/C; o=$(bash "$H/tools/gate_stop.sh" "$T/C"); r=$?
ck "没在跑的树 → 退 0（rc=${r}）" '[ $r = 0 ]'
[ $bad = 0 ] && echo "gate_stop 单测全过" || { echo "gate_stop 单测有不通"; exit 1; }
