#!/bin/bash
# 门禁覆盖：graphics/tools/direct/dsh/batch/Build.cs graphics/tools/direct/dsh/batch/Program.cs
# 配对顶点只等自己的片元、不等整个第一波（KI-001；dsh_batch --paired-waves → Build.PairedWaves；约 1 分钟，含编 dsh_batch）：bash tests/dshb_paired_waves_test.sh
#   原先「翻译与逐键收尾」两次 Parallel.ForEach：配对的顶点着色器要等整个第一波（含无关的慢键）收尾才开始。期望按规则手写（各键睡给定毫秒，打印开始 / 结束毫秒）：
#   ① 并行 4：慢键 a 1500 ms、片元 f 100 ms、配对顶点 v（配 f）100 ms → v 在 f 结束后开始、在 a 结束前就做完（两波屏障下 v 要等到 a 结束后才开始）
#   ② 片元 f 800 ms、v 配 f → v 不早于 f 结束开始（依赖照守：v 翻译读 f 的 _b.metal）
#   ③ 同 ②，v 缓存命中（hit）→ 不等 f，f 结束前 v 已开始
#   ④ 并行 1（一个线程）：a、f、v 照样跑完，不死锁（20 秒内）
set -u; H=$(cd "$(dirname "$0")/.." && pwd); T=$(mktemp -d); trap 'rm -rf "$T"' EXIT; bad=0
ck() { if eval "$2"; then echo "通 $1"; else echo "不通 $1"; bad=1; fi; }
PY=${S2A_PY:-python3}
DSHB=$("$PY" -c "import sys; sys.path.insert(0, '$H/translator'); import dsh_batch; import shlex; print(' '.join(map(shlex.quote, dsh_batch.get()[0])))" 2> "$T/build.err") || { echo "🔴 编不出 dsh_batch：$(tail -3 "$T/build.err")"; exit 1; }
run() { $DSHB --paired-waves "$@" > "$T/out" 2> "$T/err"; }
s() { awk -v k="$1" '$1==k{print $2}' "$T/out"; }
e() { awk -v k="$1" '$1==k{print $3}' "$T/out"; }
run 4 a:1500 f:100 v:100:f
ck "① v 等 f（v 开始 $(s v) ≥ f 结束 $(e f)）、不等慢键 a（v 结束 $(e v) < a 结束 $(e a)）" '[ -n "$(s v)" ] && [ "$(s v)" -ge "$(e f)" ] && [ "$(e v)" -lt "$(e a)" ]'
run 4 f:800 b:50 v:50:f
ck "② 片元慢：v 开始 $(s v) ≥ f 结束 $(e f)" '[ -n "$(s v)" ] && [ "$(s v)" -ge "$(e f)" ]'
run 4 f:800 b:50 v:50:f:hit
ck "③ 缓存命中不等片元：v 开始 $(s v) < f 结束 $(e f)" '[ -n "$(s v)" ] && [ "$(s v)" -lt "$(e f)" ]'
if command -v timeout > /dev/null; then TO="timeout 20"; else TO=; fi
$TO $DSHB --paired-waves 1 a:50 f:50 v:50:f > "$T/out" 2> "$T/err"; rc=$?
ck "④ 并行 1 不死锁（rc=$rc，v 结束 $(e v)）" '[ $rc = 0 ] && [ -n "$(e v)" ]'
[ $bad = 0 ] && echo "dshb_paired_waves 单测全过" || { echo "dshb_paired_waves 单测有不通"; cat "$T/err" | tail -5; exit 1; }
