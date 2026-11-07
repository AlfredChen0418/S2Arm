#!/bin/bash
# 门禁只看：android/native/screen_metrics.c android/native/screen_metrics.h android/tests/screen_widen_test.c runtime/tests/t_widen.c runtime/src/widen.c runtime/src/patch.c runtime/src/*.h runtime/include/*
# 安卓宽屏取屏幕尺寸的宿主单测（约 3 秒）：bash tests/android_screen_host_test.sh
#   用宿主编译器带 -D__ANDROID__ 编生产源码 screen_metrics.c + widen.c，对着冻结的 HostApi 表面尺寸跑，不要设备；NDK 链接另由 ctest-link 钩子核。
#   （11-01 由 android/test.sh 挪进合入前层：那边属合入后层，f7b55aeb8 在 __ANDROID__ 下包含 bionic 头、Mac / Linux 宿主都编不过，
#    合入前没拦住、贡献者自检才发现）
set -u; H=$(cd "$(dirname "$0")/.." && pwd); T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
A=$H/android; CC=${CC:-cc}
ldflag=-Wl,--gc-sections; [ "$(uname -s)" != Darwin ] || ldflag=-Wl,-dead_strip
libm=; [ "$(uname -s)" != Linux ] || libm=-lm   # Linux 上 widen.c 的 floorf 要 libm
"$CC" -std=gnu11 -D__ANDROID__ -ffunction-sections -fdata-sections -I "$H/runtime/include" -I "$H/runtime/src" \
    "$A/tests/screen_widen_test.c" "$H/runtime/src/widen.c" "$H/runtime/src/patch.c" "$A/native/screen_metrics.c" "$ldflag" -o "$T/screen-widen-test" \
    || { echo "【红】screen-widen-test 编不过"; exit 1; }
o1=$("$T/screen-widen-test" 2>&1) || { echo "$o1" | tail -5; echo "【红】screen-widen-test 跑不过"; exit 1; }
"$CC" -std=gnu11 -D__ANDROID__ -I "$H/runtime/include" -I "$H/runtime/src" \
    "$H/runtime/tests/t_widen.c" "$H/runtime/src/widen.c" "$H/runtime/src/patch.c" "$A/native/screen_metrics.c" -o "$T/widen-loader-test" ${libm:+"$libm"} \
    || { echo "【红】widen-loader-test 编不过"; exit 1; }
o2=$("$T/widen-loader-test" 2>&1) || { echo "$o2" | tail -5; echo "【红】widen-loader-test 跑不过"; exit 1; }
echo "$o1" | tail -1; echo "$o2" | tail -1
echo "安卓宽屏屏幕尺寸宿主单测：两步编过跑过"
