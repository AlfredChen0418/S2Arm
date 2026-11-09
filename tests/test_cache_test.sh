#!/bin/bash
# 门禁只看：tools/gate.sh tools/test_cache.py tools/gate_route.py tools/gate_decl.py tools/idle_timeout.sh tools/safe_killpg.sh
# 门禁单测结果缓存（tools/test_cache.py + tools/gate.sh 的「单测」步）：假仓库三支单测，判据是各单测自己记的「跑了几次」计数，不看耗时。约 60 秒。
# 假仓库：foo_test 跑 tools/foo.sh（路由覆盖面点名）；bar_test 头里「# 门禁输入：data/」读 data/x.txt；red_test 头里「# 门禁输入：flags/」、flags/state 写「红」时红；tools/other.sh 谁都不用。
# 期望（按规则手写，不跑被测实现先算）：
#   ① 第一次：三支各跑 1 次、绿；② 第二次：三支都不跑（计数不变），行「🟢 单测 foo_test（缓存命中）0 秒：输入与工具链没变，沿用上次的绿」
#   ③ 改 tools/foo.sh 一个字节：只 foo 再跑；④ 改不相干的 tools/other.sh：都命中；⑤ 改 data/x.txt（门禁输入声明）：只 bar 再跑
#   ⑥ 工具链指纹变（tools/toolchain.env 改一行）：三支都再跑；⑦ red_test 红了不记：连跑两次都真跑、门禁红；转绿后跑一次、再下一次命中
#   ⑧ GATE_TEST_CACHE=0：都真跑；⑨ GATE_HOST=gate-host（S2A_REMOTE=local 本机模拟）：远端那份同样用缓存（远端假家目录下），第二次［gate-host］命中不跑；⑨b 带 GATE_TEST_CACHE=0 远端也关；⑨c 远端同步阶段黄字进门禁日志与末行
# 反证：S2A_GATE_SH=<旧版 gate.sh> 跑本单测应红（旧版没有缓存：② 计数涨）
set -u; H=$(cd "$(dirname "$0")/.." && pwd); T=$(cd "$(mktemp -d)" && pwd -P); bad=0; GS=${S2A_GATE_SH:-$H/tools/gate.sh}
ck() { if eval "$2"; then echo "通 $1"; else echo "不通 $1"; bad=1; fi; }
trap '[ -n "${KEEP:-}" ] || rm -rf "$T"' EXIT
unset GATE_TIER GATE_CHANGED GATE_PLAN GATE_OUT GATE_HOST GATE_REV GATE_REMOTE_NAME GATE_SKIP GATE_ONLY GATE_HOOK_S GATE_LAYER GATE_TEST_CACHE GATE_RERUN GATE_LIST
export S2A_REMOTE_FREE_GB=999 GATE_NO_LOADQ=1 S2A_RUN_ROOT=$T/run
export S2A_REMOTE=local S2A_REMOTE_ROOT=$T/rr S2A_REMOTE_LOGS=$T/logs HOME=$T/home S2A_TRAIN_NO_POST=1
mkdir -p $T/home $T/repo/tools $T/repo/tests $T/repo/data $T/repo/flags; cd $T/repo || exit 1; mkdir -p $T/home/.s2arm/run/s2arm/bin && echo '# 测试用局槽仲裁副本' > $T/home/.s2arm/run/s2arm/bin/mac_slot-now.sh   # 维护方工具 本机缺副本不派（维护方工具）
git init -q; git config user.email t@t; git config user.name t
cp "$GS" tools/gate.sh; cp $H/tools/test_cache.py $H/tools/gate_route.py $H/tools/gate_decl.py $H/tools/idle_timeout.sh $H/tools/safe_killpg.sh tools/
echo build/ > .gitignore; echo 'X=1' > tools/toolchain.env; echo ': # 单测桩' > tools/toolchain.sh
echo 'echo foo 过' > tools/foo.sh; echo 'echo 不相干' > tools/other.sh; echo 原数据 > data/x.txt; echo 绿 > flags/state
cat > tests/foo_test.sh <<EOF
#!/bin/bash
echo run >> $T/cnt_foo; bash tools/foo.sh
EOF
cat > tests/bar_test.sh <<EOF
#!/bin/bash
# 门禁输入：data/
echo run >> $T/cnt_bar; cat data/x.txt
EOF
cat > tests/red_test.sh <<EOF
#!/bin/bash
# 门禁输入：flags/
echo run >> $T/cnt_red; grep -q 红 flags/state && { echo red 红; exit 1; }; echo red 绿
EOF
git add -A; git commit -qm 1
cm() { git add -A; git commit -qm "$1"; }
n() { grep -c . "$T/cnt_$1" 2> /dev/null || echo 0; }
cnt() { echo "$(n foo) $(n bar) $(n red)"; }
g() { bash tools/gate.sh > "$T/$1" 2>&1; echo $?; }

r=$(g o1); c1=$(cnt)
ck "① 第一次：三支各跑 1 次、绿（计数 ${c1}，rc=${r}）" '[ $r = 0 ] && [ "$c1" = "1 1 1" ]'
r=$(g o2); c2=$(cnt)
ck "② 第二次：输入没变 → 三支都不跑（计数 ${c2}）、绿（rc=${r}）" '[ $r = 0 ] && [ "$c2" = "1 1 1" ]'
ck "② 命中行：「🟢 单测 foo_test（缓存命中）N 秒：输入与工具链没变，沿用上次的绿」" 'grep -qxE "🟢 单测 foo_test（缓存命中）[0-9]+ 秒：输入与工具链没变，沿用上次的绿" $T/o2'
echo 'echo foo 过!' > tools/foo.sh; cm foo; r=$(g o3); c3=$(cnt)
ck "③ 改 tools/foo.sh 一个字节 → 只 foo 再跑（计数 ${c3}，rc=${r}）" '[ $r = 0 ] && [ "$c3" = "2 1 1" ]'
echo 'echo 不相干!' > tools/other.sh; cm other; r=$(g o4); c4=$(cnt)
ck "④ 改不相干的 tools/other.sh → 都命中（计数 ${c4}，rc=${r}）" '[ $r = 0 ] && [ "$c4" = "2 1 1" ]'
echo 新数据 > data/x.txt; cm data; r=$(g o5); c5=$(cnt)
ck "⑤ 改 data/x.txt（bar 的门禁输入声明）→ 只 bar 再跑（计数 ${c5}，rc=${r}）" '[ $r = 0 ] && [ "$c5" = "2 2 1" ]'
echo 'X=2' > tools/toolchain.env; cm tc; r=$(g o6); c6=$(cnt)
ck "⑥ 工具链指纹变（toolchain.env）→ 三支都再跑（计数 ${c6}，rc=${r}）" '[ $r = 0 ] && [ "$c6" = "3 3 2" ]'
echo 红 > flags/state; cm red; r=$(g o7); r2=$(g o7b); c7=$(cnt)
ck "⑦ red_test 红了不记缓存：连跑两次都真跑、门禁红（计数 ${c7}，rc=${r} ${r2}）" '[ $r = 1 ] && [ $r2 = 1 ] && [ "$c7" = "3 3 4" ] && grep -q "^🔴 单测 red_test" $T/o7b'
echo 绿2 > flags/state; cm green; r=$(g o7c); r2=$(g o7d); c7=$(cnt)
ck "⑦ 转绿后跑一次记缓存、下一次命中（计数 ${c7}，rc=${r} ${r2}）" '[ $r = 0 ] && [ $r2 = 0 ] && [ "$c7" = "3 3 5" ] && grep -q "^🟢 单测 red_test（缓存命中）" $T/o7d'
r=$(GATE_TEST_CACHE=0 g o8); c8=$(cnt)
ck "⑧ GATE_TEST_CACHE=0 → 都真跑（计数 ${c8}，rc=${r}）" '[ $r = 0 ] && [ "$c8" = "4 4 6" ] && ! grep -q 缓存命中 $T/o8'
mkdir -p $T/priv; export S2A_PRIVATE_TOOLS=$T/priv
cat > $T/priv/remote.sh << 'REMOTESTUB'
#!/bin/bash
NM=default; REV=HEAD; PULL=(); ENVS=(); CMD=()
while [ $# -gt 0 ]; do
  case $1 in
    --name) NM=$2; shift 2;; --rev) REV=$2; shift 2;;
    --timeout|--sync) shift 2;; --clean|--keep) shift;;
    --pull) PULL+=("$2"); shift 2;; --env) ENVS+=("$2"); shift 2;;
    --) shift; CMD=("$@"); break;; *) shift;;
  esac
done
RR=${S2A_REMOTE_ROOT:?}; W=$RR/wt/$NM; GU=$RR/S2Arm.git
git init -q --bare "$GU" 2>/dev/null; SHA=$(git rev-parse "$REV")
git push -q -f "$GU" "$SHA:refs/heads/wt/$NM" 2>/dev/null
if [ -d "$W" ]; then git -C "$W" checkout -q --detach "$SHA"; git -C "$W" clean -fdq
else git -C "$GU" worktree add -q --detach "$W" "$SHA"; fi
mkdir -p "$RR/home"
for v in "${ENVS[@]}"; do [ -n "${!v+x}" ] && export "$v=${!v}"; done
[ -n "${S2A_STUB_WARN:-}" ] && echo "【黄】remote：门禁同步缺包 ${S2A_STUB_WARN}" >&2
( cd "$W" && unset S2A_RUN_ROOT && HOME="$RR/home" bash -c "${CMD[*]}" ); rc=$?
for pp in "${PULL[@]}"; do
  src=${pp%%=*}; dst=${pp#*=}
  [ "${src#~/}" = "$src" ] && src="$W/$src" || src="$RR/home/${src#~/}"
  [ -d "$src" ] && { mkdir -p "$dst"; rsync -a "$src/" "$dst/"; }; done
exit $rc
REMOTESTUB
chmod +x $T/priv/remote.sh
# ⑨ 远端那份（GATE_PLAN）：远端工作树里同样算键，缓存在远端假家目录下（本机缓存不带过去，第一次远端照跑）
r=$(GATE_HOST=gate-host g o9); r2=$(GATE_HOST=gate-host g o9b); c9=$(cnt)
ck "⑨ GATE_HOST=gate-host：第一次远端照跑、第二次［gate-host］命中不跑（计数 ${c9}，rc=${r} ${r2}）" '[ $r = 0 ] && [ $r2 = 0 ] && [ "$c9" = "5 5 7" ] && grep -qE "^🟢 单测 foo_test［gate-host］（缓存命中）[0-9]+ 秒" $T/o9b'
ck "⑨ 远端缓存落在远端假家目录（$(ls $T/rr/home/.s2arm/run/s2arm/cache/testres 2> /dev/null | grep -c .) 个键）" '[ "$(ls $T/rr/home/.s2arm/run/s2arm/cache/testres 2> /dev/null | grep -c .)" -ge 3 ]'
r=$(GATE_HOST=gate-host GATE_TEST_CACHE=0 g o9c); c9=$(cnt)
ck "⑨b GATE_HOST=gate-host + GATE_TEST_CACHE=0：开关带到远端、都真跑（计数 ${c9}，rc=${r}）" '[ $r = 0 ] && [ "$c9" = "6 6 8" ] && ! grep -q 缓存命中 $T/o9c'
# ⑨c 远端同步阶段的黄字（remote.sh 缺包）：原样进门禁日志，末行在「应跑 N 步」后点一句、仍是绿
r=$(GATE_HOST=gate-host S2A_STUB_WARN=pkg.x/1.0 g o9d)
ck "⑨c 远端同步黄字：门禁日志有原行、末行点「同步缺 1 项」且仍绿（rc=${r}）" '[ $r = 0 ] && grep -q "^【黄】remote：门禁同步缺包 pkg.x/1.0" $T/o9d && tail -1 $T/o9d | grep -q "^门禁 绿：.*应跑 [0-9]* 步；⚠ gate-host 同步缺 1 项" && ! tail -1 $T/o9c | grep -q 同步缺'
# ⑩ 读声明的单测（覆盖面含 tools/gate_route.py）：别的钩子文件头的路由声明改了 → 键变（要重跑）；普通单测键不变
#   （10-30：换链支改 core-link/gate.sh 的路由，gate_route_matrix_test 在干净 main 上命中缓存给了假绿）
mkdir -p hk; printf '#!/bin/bash\n# 门禁路由：a/\necho 钩子\n' > hk/gate.sh
printf '#!/bin/bash\npython3 tools/gate_route.py tests a/x > /dev/null\necho run\n' > tests/decl_test.sh; cm 10a
k1=$(python3 tools/test_cache.py key . tests/decl_test.sh tests/foo_test.sh | tr '\n' ' ')
sed -i '' 's|# 门禁路由：a/|# 门禁路由：b/|' hk/gate.sh; cm 10b
k2=$(python3 tools/test_cache.py key . tests/decl_test.sh tests/foo_test.sh | tr '\n' ' ')
ck "⑩ 路由声明改了：decl_test 键变、foo_test 键不变" '[ "${k1%% *}" != "${k2%% *}" ] && [ "$(cut -d" " -f2 <<< "$k1")" = "$(cut -d" " -f2 <<< "$k2")" ]'
# ⑪ 「门禁只看」只截路由、不截缓存输入：only_test 只看 tools/a.sh，a.sh 调 tools/b.py；改 b.py → 缓存键变（重跑），路由仍不选它
#   （10-30：train_test「门禁只看」漏 known_red.py，缓存一直沿用改判定前的绿）
printf 'python3 tools/b.py\n' > tools/a.sh; printf 'print(1)\n' > tools/b.py
printf '#!/bin/bash\n# 门禁只看：tools/a.sh\nbash tools/a.sh\n' > tests/only_test.sh; cm 11a
j1=$(python3 tools/test_cache.py key . tests/only_test.sh); printf 'print(2)\n' > tools/b.py; cm 11b
j2=$(python3 tools/test_cache.py key . tests/only_test.sh); rt=$(python3 tools/gate_route.py tests tools/b.py | grep -c only_test)
ck "⑪ 改「门禁只看」之外、被调用的 tools/b.py：缓存键变（${j1:0:8} → ${j2:0:8}）、路由不选 only_test（${rt}）" '[ "$j1" != "$j2" ] && [ "$rt" = 0 ]'
[ $bad = 0 ] && echo "test_cache 单测全过" || { echo "test_cache 单测有不通"; tail -15 $T/o9b; exit 1; }
