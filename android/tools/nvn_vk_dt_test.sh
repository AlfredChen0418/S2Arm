#!/usr/bin/env bash
# B2 VK_GOOGLE_display_timing（宿主，确定性）：支持→挂 id/回读真值；不支持→明确
# unavailable；乱序→按 pid 对号；丢帧→UNKNOWN 不冒名；环覆/迟到/detach 复位。
# 天真实现（按到达顺序回填、拿 CPU now 冒充）会在断言处红。
set -euo pipefail
repo="$(cd "$(dirname "$0")/../.." && pwd)"
android="$repo/android"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
"${CC:-cc}" -std=gnu11 -O1 -Wall -Wextra -Werror -Wno-unused-parameter -pthread \
  -DS2A_DT_TEST=1 \
  -I "$android/tests/vkhost" -I "$repo/graphics/nvn/third_party/vulkan-headers/include" \
  -I "$android/native" -I "$android/native/nvn" \
  -o "$tmp/t" "$android/tests/nvn_vk_dt_test.c" "$android/native/nvn/nvn_vk_display_timing.c"
"$tmp/t"
