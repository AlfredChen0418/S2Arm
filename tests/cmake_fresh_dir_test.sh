#!/usr/bin/env bash
# 固定 CMake 构建目录在源码搬目录后要能重新配置（tests/rewrite_c_gate_test.sh fresh_cmake_dir；11-09 列车门禁红过一次）
# 门禁覆盖：tests/rewrite_c_gate_test.sh tests/cmake_fresh_dir_test.sh
# 期望按 CMake 行为手写：缓存记着旧源码路径时直接配置失败（负对照）；先过 fresh_cmake_dir 再配置成功，且缓存里记成新路径
set -u
H=$(cd "$(dirname "$0")/.." && pwd)
command -v cmake > /dev/null || { echo "🟡 没有 cmake"; exit 75; }
eval "$(sed -n '/^fresh_cmake_dir() {/p' "$H/tests/rewrite_c_gate_test.sh")"
type fresh_cmake_dir > /dev/null 2>&1 || { echo "🔴 rewrite_c_gate_test.sh 里找不到 fresh_cmake_dir"; exit 1; }
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
mkdir -p "$T/runtime/x"; printf 'cmake_minimum_required(VERSION 3.18)\nproject(x NONE)\n' > "$T/runtime/x/CMakeLists.txt"
B="$T/build"; bad=0
ck() { if eval "$2"; then echo "通 $1"; else echo "不通 $1"; bad=1; fi; }
cmake -S "$T/runtime/x" -B "$B" > /dev/null 2>&1; ck "首次配置成功" '[ -f "$B/CMakeCache.txt" ]'
mv "$T/runtime" "$T/native"   # 源码搬目录（同一工作树换了目录布局的分支）
cmake -S "$T/native/x" -B "$B" > "$T/neg.log" 2>&1; ck "负对照：不清缓存直接配置失败（CMake 报源码不符）" '! [ $? = 0 ] && grep -q "does not match the source" "$T/neg.log"'
fresh_cmake_dir "$T/native/x" "$B"
cmake -S "$T/native/x" -B "$B" > /dev/null 2>&1; ck "过 fresh_cmake_dir 后配置成功" '[ $? = 0 ]'
ck "缓存记成新源码路径" 'grep -qxF "CMAKE_HOME_DIRECTORY:INTERNAL=$T/native/x" "$B/CMakeCache.txt"'
fresh_cmake_dir "$T/native/x" "$B"; ck "路径一致时不删缓存" '[ -f "$B/CMakeCache.txt" ]'
[ $bad = 0 ] && echo "cmake_fresh_dir 单测全过" || { echo "cmake_fresh_dir 单测有不通"; exit 1; }
