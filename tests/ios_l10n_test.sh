#!/bin/bash
# iOS 宿主界面本地化（#316）：游戏内菜单 / 触屏布局 / 金手指 / 社区补丁页走 S2AL 字符串表，iOS「设置」App 的 S2Arm 页英文原文 + 简体中文翻译。约 0 秒，不编译
# 门禁只看：ios/vpad.m ios/vpad_settings.m ios/vpad_layout.m ios/vpad_cheats.m ios/vpad_shell.m ios/cpatch_settings.m ios/cpatch_model.m ios/s2a_l10n.h ios/l10n/en.lproj/S2ArmHost.strings ios/l10n/zh-Hans.lproj/S2ArmHost.strings ios/Settings.bundle/Root.plist ios/Settings.bundle/zh-Hans.lproj/Root.strings tests/ios_l10n_test.py
# 门禁覆盖：ios/vpad.m ios/vpad_settings.m ios/vpad_layout.m ios/vpad_cheats.m ios/vpad_shell.m ios/cpatch_settings.m ios/cpatch_model.m ios/s2a_l10n.h ios/l10n/en.lproj/S2ArmHost.strings ios/l10n/zh-Hans.lproj/S2ArmHost.strings ios/Settings.bundle/Root.plist ios/Settings.bundle/zh-Hans.lproj/Root.strings tests/ios_l10n_test.py
# 期望与断言见 tests/ios_l10n_test.py 文件头（① 无显示用中文字面量 ② 键齐全无孤儿 ③ 占位符一致 ④ 设置页翻译一一对应 ⑤ plutil -lint）
set -eu
H=$(cd "$(dirname "$0")/.." && pwd)
PY=${S2A_PYTHON:-${S2A_PY:-python3}}
exec "$PY" "$H/tests/ios_l10n_test.py"
