#!/bin/bash
# 门禁覆盖：tools/run_token.sh tools/gate.sh tools/flock_hold.py
# 本机预跑整份令牌（tools/run_token.sh，11-09 维护方：几条线同时本机预跑把负载冲到 200 多）：bash tests/run_token_test.sh，约 20 秒
#   期望按规则手写（令牌 2 个）：三份假仓库门禁同时起，各自唯一的单测睡 3 秒并登记「在跑」→ 同时在跑最多 2 份、第三份打「本机预跑排队中」、三份都绿；
#   2 个令牌被占时列车起的门禁（GATE_NO_LOADQ=1）不排队、立刻跑；包装用法 bash tools/run_token.sh <命令> 同样限 2 份；持有者被 kill -9 → 令牌随即放出
set -u; H=$(cd "$(dirname "$0")/.." && pwd); T=$(mktemp -d); PIDS=; bad=0
trap 'for q in $PIDS; do kill -KILL "$q" 2> /dev/null; done; rm -rf "$T"' EXIT
ck() { if eval "$2"; then echo "通 $1"; else echo "不通 $1"; bad=1; fi; }
unset GATE_IN_STEP GATE_NO_LOADQ GATE_LAYER GATE_RERUN GATE_ONLY GATE_PLAN
export GATE_RUN_TOKEN_DIR=$T/tok GATE_RUN_PAR=2 GATE_RUN_POLL_S=0.2 GATE_TOKEN_DIR=$T/steptok
# probe：登记在跑 → 记下此刻同时在跑几份（取最大）→ 睡 → 撤登记
PROBE='mkdir -p "$RT_DIR/on"; : > "$RT_DIR/on/$$"; n=$(ls "$RT_DIR/on" | wc -l | tr -d " "); echo "$n" >> "$RT_DIR/peak"; sleep 3; rm -f "$RT_DIR/on/$$"'
mk() { mkdir -p "$T/$1/tools" "$T/$1/tests"; cp "$H/tools/gate.sh" "$H/tools/gate_plan.sh" "$H/tools/run_token.sh" "$H/tools/flock_hold.py" "$T/$1/tools/"
  printf '#!/bin/bash\n%s\n' "$PROBE" > "$T/$1/tests/probe_test.sh"; git init -q "$T/$1"; }
g() { (cd "$T/$1" && env "${@:2}" RT_DIR=$T S2A_GATE_RUNTOK_TEST=1 S2A_GATE_CPU_IDLE_MIN=0 GATE_HOST=local GATE_TIER=tool GATE_CHANGED=tools/x.sh GATE_TEST_CACHE=0 \
  timeout 120 bash tools/gate.sh > "$T/$1.out" 2>&1; echo $? > "$T/$1.rc"); }
peak() { sort -n "$T/peak" 2> /dev/null | tail -1; }
for r in a b c d; do mk $r; done
g a & p1=$!; g b & p2=$!; g c & p3=$!; wait $p1 $p2 $p3
q=$(grep -l "本机预跑排队中" "$T"/a.out "$T"/b.out "$T"/c.out 2> /dev/null | wc -l | tr -d ' ')
ck "三份同时起：同时在跑最多 2 份（峰值 $(peak)）" '[ "$(peak)" = 2 ]'
ck "第三份排队：恰好 1 份打「本机预跑排队中」（${q} 份）" '[ "$q" = 1 ]'
ck "三份都绿（rc $(cat $T/a.rc) $(cat $T/b.rc) $(cat $T/c.rc)）" '[ "$(cat $T/a.rc)$(cat $T/b.rc)$(cat $T/c.rc)" = 000 ]'
# 列车插队：两个令牌被包装用法占住（睡 10 秒），列车门禁不排
rm -f "$T/peak"; for i in 1 2; do bash "$H/tools/run_token.sh" bash -c "sleep 10; : > $T/rel.$i" & PIDS="$PIDS $!"; done; sleep 1
t0=$SECONDS; g d GATE_NO_LOADQ=1; dt=$((SECONDS - t0))
ck "令牌被占时列车门禁（GATE_NO_LOADQ=1）不排队：绿、没打排队、${dt} 秒 < 9" '[ "$(cat $T/d.rc)" = 0 ] && ! grep -q 排队中 "$T/d.out" && [ $dt -lt 9 ]'
# 包装用法：令牌仍被占 → 第三份排队，等前面放了才跑
o=$(bash "$H/tools/run_token.sh" bash -c "ls $T/rel.* 2> /dev/null | wc -l | tr -d ' '" 2>&1); r=$?
ck "包装用法：令牌全占时排队、持有者放了才跑（rc=${r}）" '[ $r = 0 ] && [[ "$o" == *本机预跑排队中* ]] && [ "$(tail -1 <<< "$o")" != 0 ]'
# kill -9 持有者 → 令牌放出
bash "$H/tools/run_token.sh" sleep 30 & k1=$!; bash "$H/tools/run_token.sh" sleep 30 & k2=$!; PIDS="$PIDS $k1 $k2"; sleep 1
kill -KILL $k1; wait $k1 2> /dev/null; sleep 0.5
t0=$SECONDS; o=$(bash "$H/tools/run_token.sh" true 2>&1); r=$?; dt=$((SECONDS - t0))
ck "持有者被 kill -9 → 令牌随即放出，新的一份不用等（rc=${r}，${dt} 秒）" '[ $r = 0 ] && [ $dt -lt 3 ] && [[ "$o" != *排队中* ]]'
[ $bad = 0 ] && echo "本机预跑令牌单测全过" || { echo "本机预跑令牌单测有不通"; for f in "$T"/*.out; do echo "== $f"; tail -5 "$f"; done; exit 1; }
