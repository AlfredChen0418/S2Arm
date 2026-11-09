#!/bin/bash
# Mac-only frontend checks; no game files, device access or network installation.
set -euo pipefail
R=$(cd "$(dirname "$0")/../.." && pwd)
cd "$R"
source "$R/tools/toolchain.sh"   # 门禁用统一工具链（tools/toolchain.env）；S2A_DEVELOPER_DIR 可覆盖
export CLANG_MODULE_CACHE_PATH="$R/build/macos/clang-cache"
export SWIFTPM_MODULECACHE_OVERRIDE="$R/build/macos/swift-cache"
mkdir -p build/macos/tests
python3 -B -m unittest discover -s pack/jobs/tests -v
python3 -B -m unittest discover -s launchers/macos/Tests -p 'test_*.py' -v
python3 -B -m unittest discover -s launchers/macos/gui_jobs/tests -v
swift_args=(--disable-sandbox --cache-path "$R/build/macos/spm-cache" --package-path launchers/macos --scratch-path build/macos/spm)
swift test "${swift_args[@]}" list > build/macos/tests/swift-discovery.log
swift_rc=0
swift test "${swift_args[@]}" > build/macos/tests/swift-execution.log 2>&1 || swift_rc=$?
cat build/macos/tests/swift-execution.log
python3 launchers/macos/check_xctest.py build/macos/tests/swift-discovery.log build/macos/tests/swift-execution.log
[ "$swift_rc" = 0 ] || exit "$swift_rc"
xcrun clang -fobjc-arc -O2 -Wall -Wextra -Wno-unused-parameter -Iruntime/include -Iios \
  launchers/macos/Tests/input_test.m runtime/src/input_config.c runtime/src/pad.c ios/input_config.m -o build/macos/tests/input \
  -framework AppKit -framework CoreGraphics -framework GameController -framework QuartzCore
build/macos/tests/input
# 系统键盘桥接 Mac 端（textinput_mac.m + runtime/src/text_input.c）：真起 AppKit 表单，程序代填字 / 点按钮核回调
xcrun clang -fobjc-arc -O2 -Wall -Wextra -Wno-unused-parameter -Iruntime/include -Iios \
  launchers/macos/Tests/textinput_test.m -o build/macos/tests/textinput -framework AppKit
build/macos/tests/textinput
