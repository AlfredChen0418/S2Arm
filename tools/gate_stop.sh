#!/bin/bash
# 只停本工作树的门禁（11-09：一条线按进程名 pkill -f "tools/gate.sh" 停自己的预跑，连别的会话的门禁预跑和列车本机步骤一起杀了）：
#   bash tools/gate_stop.sh <工作树>
#   读该工作树 git 目录里的 s2a_gate.pid（tools/gate.sh 开跑时写、退出时删），核对该进程确是 tools/gate.sh、工作目录就是这棵树，
#   先快照它的全部后代，给门禁主进程发 TERM（gate.sh 的 TERM 处理会转给各步的 timeout 进程组），等最多 GATE_STOP_WAIT_S（缺省 10）秒，
#   快照里还活着的逐个 KILL。别的树、列车的进程一个都不碰。退出码：0 = 停了 / 本来就没在跑，1 = pid 对不上（不动），2 = 用法错
set -u
T=$(cd "${1:?用法：tools/gate_stop.sh <工作树>}" 2> /dev/null && pwd -P) || { echo "用法：tools/gate_stop.sh <工作树>（目录不存在：${1}）"; exit 2; }
GD=$(git -C "$T" rev-parse --absolute-git-dir 2> /dev/null) || { echo "用法：tools/gate_stop.sh <工作树>（${T} 不是 git 工作树）"; exit 2; }
PF=$GD/s2a_gate.pid
[ -f "$PF" ] || { echo "本树没有在跑的门禁（没有 ${PF}）"; exit 0; }
p=$(cat "$PF" 2> /dev/null); case $p in ''|*[!0-9]*) echo "【红】${PF} 内容不是 pid，不动"; exit 1;; esac
kill -0 "$p" 2> /dev/null || { rm -f "$PF"; echo "门禁 pid ${p} 已不在，清掉残留的 pid 文件"; exit 0; }
c=$(ps -ww -o command= -p "$p" 2> /dev/null)
case $c in *tools/gate.sh*) ;; *) echo "【红】pid ${p} 不是门禁（${c:0:80}），不动"; exit 1;; esac
cwd=$(lsof -a -p "$p" -d cwd -Fn 2> /dev/null | sed -n 's/^n//p' | head -1)
[ -n "$cwd" ] && [ "$(cd "$cwd" 2> /dev/null && pwd -P)" = "$T" ] || { echo "【红】pid ${p} 的工作目录「${cwd:-读不到}」不是 ${T}，不动"; exit 1; }
desc() { local x; echo "$1"; for x in $(pgrep -P "$1"); do desc "$x"; done; }
snap=$(desc "$p" | tr '\n' ' ')   # 先快照再发信号：主进程一退，后代会过继给 launchd、再也认不出来
kill -TERM "$p" 2> /dev/null
alive=; for _ in $(seq $(( ${GATE_STOP_WAIT_S:-10} * 5 ))); do alive=; for q in $snap; do kill -0 "$q" 2> /dev/null && alive="$alive $q"; done; [ -z "$alive" ] && break; sleep 0.2; done
[ -z "$alive" ] || kill -KILL $alive 2> /dev/null
[ "$(cat "$PF" 2> /dev/null)" = "$p" ] && rm -f "$PF"
echo "已停 ${T} 的门禁（pid ${p}，后代 $(( $(wc -w <<< "$snap") - 1 )) 个$([ -n "$alive" ] && echo "，$(wc -w <<< "$alive" | tr -d " ") 个超时后强停")）"
