# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 S2Arm contributors
"""Synthetic preflight probes keep Metal and Xcode status text consistent."""
from pathlib import Path
from unittest.mock import patch

from jobcase import JobCase
import install
import ipa
import toolchain


class ToolchainMessageTests(JobCase):
    def preflight(self, *, has_xcode=True, metal=(True, "Apple metal version fixture"), sdk=True):
        developer = self.root / "synthetic-Xcode/Contents/Developer"
        xcodebuild = developer / "usr/bin/xcodebuild"
        xcodebuild.parent.mkdir(parents=True, exist_ok=True)
        if has_xcode:
            xcodebuild.touch()
        else:
            xcodebuild.unlink(missing_ok=True)
        self.c.r["developerDir"] = str(developer)

        def probe(c, args, timeout=15):
            if "metal" in args:
                return metal
            if "--show-sdk-path" in args:
                return sdk, "synthetic-sdk" if sdk else ""
            return True, "10.0.403"

        with patch.object(toolchain, "probe", side_effect=probe), \
             patch.object(toolchain, "ios_platform", return_value=(None, "", "synthetic", None)), \
             patch.object(install, "xcode_account", return_value=(False, [])), \
             patch.object(toolchain, "reader", return_value=Path("fixture.dll")), \
             patch.object(toolchain, "input_interface", return_value=(True, "fixture")), \
             patch.object(ipa, "dotnet_sdk", return_value=(developer, developer / "dotnet")), \
             patch("platform.mac_ver", return_value=("26.0", (), "")):
            return {row["name"]: row for row in toolchain.preflight(self.c)["checks"]}

    def test_success_messages_match_success_states(self):
        rows = self.preflight()
        for name, expected in (("Metal 编译组件", "Xcode 的 Metal 编译组件已就绪。"),
                               ("Xcode", "已找到完整的 Xcode。")):
            with self.subTest(name=name):
                row = rows[name]
                self.assertTrue(row["ok"])
                self.assertEqual(row["message"], expected)
                self.assertEqual(row["help"], expected)
                self.assertEqual(set(row), {"name", "ok", "message", "requiredFor", "group", "action", "help"})
                self.assertEqual(row["requiredFor"], ["prepare"])
                self.assertEqual(row["group"], "system")
        self.assertEqual(rows["Metal 编译组件"]["action"], "downloadMetal")
        self.assertEqual(rows["Xcode"]["action"], "appStoreXcode")

    def test_failure_messages_and_diagnostics_unchanged(self):
        cases = [
            (dict(metal=(False, "license agreement not accepted")), "Metal 编译组件",
             "Xcode 还没完成首次设置：点「打开 Xcode」，同意许可后回来点重新检查。", "openXcode", "xcode_setup_required"),
            (dict(metal=(False, "component unavailable")), "Metal 编译组件",
             "缺 Xcode 的 Metal 编译组件：点「一键下载」自动安装（约 1 GB），下完自动重新检查。", "downloadMetal", "metal_unavailable"),
            (dict(has_xcode=False, metal=(False, "not found")), "Metal 编译组件",
             "准备游戏需要 Xcode 的 Metal 编译组件；先安装 Xcode。", None, "xcode_unavailable"),
            (dict(has_xcode=False, sdk=False), "Xcode",
             "没找到完整的 Xcode：点「去 App Store 安装」，装好打开一次后回来点重新检查；S2Arm 会自动找到它。", "appStoreXcode", "xcode_unavailable"),
            (dict(sdk=False), "Xcode",
             "没找到完整的 Xcode：点「去 App Store 安装」，装好打开一次后回来点重新检查；S2Arm 会自动找到它。", "appStoreXcode", "xcode_unavailable"),
        ]
        for options, name, message, action, code in cases:
            with self.subTest(options=options, name=name):
                row = self.preflight(**options)[name]
                self.assertFalse(row["ok"])
                self.assertEqual(row["message"], message)
                self.assertEqual(row["help"], message)
                self.assertEqual(row["action"], action)
                self.assertEqual(row["diagnostic"]["code"], code)
                self.assertEqual(row["requiredFor"], ["prepare"])
                self.assertEqual(row["group"], "system")
                self.assertEqual(set(row), {"name", "ok", "message", "requiredFor", "group", "action", "help", "diagnostic"})
