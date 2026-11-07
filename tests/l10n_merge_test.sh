#!/bin/bash
# 门禁覆盖：tools/l10n_merge.py .gitattributes
# 翻译表合并器单测（11-09：#221 #238 都往 Localizable.json 文件尾追加，先合一支另一支就冲突退回）：临时仓库登记合并器，两支各改依次合进 main。
# 期望（按规则手写）：
#   ① 条目表两支各在尾部追加一条 → 两次合并都不冲突，结果是合法 JSON：原条目在前、先合的、后合的依次在后，各一份
#   ② 键值表两支各加一个键、一支改了另一个键 → 不冲突，三处改动都在
#   ③ 两支把同一条英文改成不同的 → 仍冲突（留给人）
#   ④ 一支删了一条、另一支没动它 → 删掉
set -u; H=$(cd "$(dirname "$0")/.." && pwd); T=$(mktemp -d); trap 'rm -rf "$T"' EXIT; bad=0
ck() { if eval "$2"; then echo "通 $1"; else echo "不通 $1"; bad=1; fi; }
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.invalid GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.invalid GIT_CONFIG_GLOBAL=/dev/null
R=$T/r; git init -q -b main $R; cd $R
git config merge.s2a-l10n.driver "python3 $H/tools/l10n_merge.py %O %A %B %P"
printf 'L.json merge=s2a-l10n\nK.json merge=s2a-l10n\n' > .gitattributes
ent() { printf ' {\n  "source": "%s",\n  "en": "%s"\n }' "$1" "$2"; }
lst() { local s='[' sep=''; while [ $# -gt 0 ]; do s="$s$sep"$'\n'"$(ent "$1" "$2")"; sep=','; shift 2; done; printf '%s\n]\n' "$s"; }
lst 甲 A 乙 B > L.json; printf '{\n  "k1": "一",\n  "k2": "二"\n}\n' > K.json; git add -A; git commit -qm base
git checkout -q -b b1; lst 甲 A 乙 B 丙 C > L.json; printf '{\n  "k1": "一",\n  "k2": "二",\n  "k3": "三"\n}\n' > K.json; git commit -qam b1
git checkout -q main; git checkout -q -b b2; lst 甲 A 乙 B 丁 D > L.json; printf '{\n  "k1": "壹",\n  "k2": "二",\n  "k4": "四"\n}\n' > K.json; git commit -qam b2
git checkout -q main; git merge -q --no-edit b1 > /dev/null 2>&1; m1=$?; git merge -q --no-edit b2 > /dev/null 2>&1; m2=$?
ck "① 条目表两支尾部追加：不冲突（rc ${m1} / ${m2}），合法 JSON，顺序 甲乙丙丁" '[ $m1 = 0 ] && [ $m2 = 0 ] && [ "$(python3 -c "import json;print(\"\".join(e[\"source\"] for e in json.load(open(\"L.json\"))))")" = 甲乙丙丁 ]'
ck "② 键值表：k1 改成壹、k3 k4 都在" '[ "$(python3 -c "import json;d=json.load(open(\"K.json\"));print(d[\"k1\"],d[\"k3\"],d[\"k4\"])")" = "壹 三 四" ]'
git checkout -q -b c1 main; lst 甲 A1 乙 B 丙 C 丁 D > L.json; git commit -qam c1
git checkout -q -b c2 main; lst 甲 A2 乙 B 丙 C 丁 D > L.json; git commit -qam c2
git checkout -q main; git merge -q --no-edit c1 > /dev/null 2>&1; git merge -q --no-edit c2 > /dev/null 2>&1; m3=$?; git merge --abort 2> /dev/null
ck "③ 同一条两边改成不同 → 仍冲突（rc ${m3}）" '[ $m3 != 0 ]'
git checkout -q -b d1 main; lst 甲 A1 丙 C 丁 D > L.json; git commit -qam d1
git checkout -q -b d2 main; lst 甲 A1 乙 B 丙 C 丁 D 戊 E > L.json; git commit -qam d2
git checkout -q main; git merge -q --no-edit d1 > /dev/null 2>&1; git merge -q --no-edit d2 > /dev/null 2>&1; m4=$?
ck "④ 一支删乙、另一支加戊 → 不冲突（rc ${m4}），乙没了、戊在" '[ $m4 = 0 ] && [ "$(python3 -c "import json;print(\"\".join(e[\"source\"] for e in json.load(open(\"L.json\"))))")" = 甲丙丁戊 ]'
[ $bad = 0 ] && echo "l10n_merge 单测全过"; exit $bad
