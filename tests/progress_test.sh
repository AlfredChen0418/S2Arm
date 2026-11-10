#!/bin/bash
# 出包进度协议单测（tools/progress.sh / tools/progress.py / pack/jobs/core.py stage_progress，约 1 秒）：bash tests/progress_test.sh
# 期望（按协议手算）：
#   ① prog_show：直连库 1000 秒起 0/100、1060 秒 30/100，通用 5/5 已做完，刷新各阶段 1100 秒起 4/9；现在 1120 →
#      「直连库 30/100（30%），本段已用 2 分 0 秒，预计还剩 4 分 40 秒（120 × 70 ÷ 30 = 280）；刷新各阶段 4/9（44%），本段已用 20 秒」（总数 9 < 20 是阶段计数，各段耗时差几个数量级，不估），
#      做完的阶段不列；没文件 / 全做完 → 不打
#   ② Python 写端：Ticker('x', 3, every=100) → 起头写 0/3，中间两步限频不写，最后一步必写 3/3；没给 S2A_PROGRESS → 不写
#   ③ 启动器读端 stage_progress：两行完整 + 半行没写完 → 按最后一个完整行发一次 progress（done 7、total 9、unit 项），读到位置停在半行之前
set -u; H=$(cd "$(dirname "$0")/.." && pwd); T=$(mktemp -d); bad=0; trap 'rm -rf "$T"' EXIT
ck() { if eval "$2"; then echo "通 $1"; else echo "不通 $1"; bad=1; fi; }
printf '1000\t直连库\t0\t100\n1000\t通用\t5\t5\n1060\t直连库\t30\t100\n1100\t刷新各阶段\t4\t9\n' > "$T/p.tsv"
o1=$(bash "$H/tools/progress.sh" show "$T/p.tsv" 1120); o1b=$(bash "$H/tools/progress.sh" show "$T/没有" 1120)
printf '1000\t通用\t5\t5\n' > "$T/done.tsv"; o1c=$(bash "$H/tools/progress.sh" show "$T/done.tsv" 1120)
ck "① prog_show：「${o1}」" '[ "$o1" = "直连库 30/100（30%），本段已用 2 分 0 秒，预计还剩 4 分 40 秒；刷新各阶段 4/9（44%），本段已用 20 秒" ] && [ -z "$o1b" ] && [ -z "$o1c" ]'
o2=$(cd "$H/tools" && S2A_PROGRESS=$T/py.tsv python3 -c "from progress import Ticker
t = Ticker('x', 3, every=100)
for _ in range(3): t.step()" && cut -f2- "$T/py.tsv" | tr '\t\n' ' |'); (cd "$H/tools" && env -u S2A_PROGRESS python3 -c "from progress import Ticker; Ticker('y', 2).step(2)")
ck "② Python 写端：「${o2}」；没给 S2A_PROGRESS 不写" '[ "$o2" = "x 0 3|x 3 3|" ]'
printf '1\t出包\t3\t9\n2\t出包\t7\t9\n3\t出包\t8' > "$T/j.tsv"
o3=$(cd "$H/pack/jobs" && python3 -c "
import core
class C(core.Context):
    def __init__(self): self.engine = core.Path(core.__file__).resolve().parents[2]
    def emit(self, **k): print(k['done'], k['total'], k['unit'], k['message'])
print(core.Context.stage_progress(C(), '$T/j.tsv', 0))" 2>&1)
ck "③ 启动器读端：「$(tr '\n' '|' <<< "$o3")」（期望一次 7 9 项、位置 $(printf '1\t出包\t3\t9\n2\t出包\t7\t9\n' | wc -c | tr -d ' ')）" '[ "$o3" = "7 9 项 出包 7/9
$(printf "1\t出包\t3\t9\n2\t出包\t7\t9\n" | wc -c | tr -d " ")" ]'
# ④ 多个计数器交替写（10-26 维护者：着色器总数与编译数量要实时看得到）：期望（手算）counters = 直连 50/100 每秒 5.0（50 项 / 10 秒）、通用 10/45455 速度未知（只有一行）；
#    刷新 3/10 是阶段计数（总数 < 20）不列；done / total = 最近更新的逐项计数（通用 10/45455）
printf '1000\t直连\t0\t100\n1001\t刷新\t3\t10\n1010\t直连\t50\t100\n1012\t通用\t10\t45455\n' > "$T/m.tsv"
o4=$(cd "$H/pack/jobs" && python3 -c "
import core
class C(core.Context):
    def __init__(self): self.engine = core.Path(core.__file__).resolve().parents[2]
    def emit(self, **k): print(k['done'], k['total'], [(c['name'], c['done'], c['total'], c['rate']) for c in k['counters']])
core.Context.stage_progress(C(), '$T/m.tsv', 0)" 2>&1)
ck "④ 多计数器：「${o4}」" '[ "$o4" = "10 45455 [('"'"'直连'"'"', 50, 100, 5.0), ('"'"'通用'"'"', 10, 45455, None)]" ]'
[ $bad = 0 ] && echo "progress 单测全过" || { echo "progress 单测有不通"; exit 1; }
