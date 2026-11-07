"""Windows 一键出包收尾行 → 作业里程碑（pack/jobs/ipa.py win_milestone）。
#244 / #269：失败的步也打「[时刻] 步名：N 秒（rc=1）」，原先照样进下一段，直连库失败被错误摘要报成「编 App 与签名」。
用例取自玩家日志原文；期望按「成功收尾 = 进下一段、失败收尾不动」手写。"""
import unittest

import jobcase  # noqa: F401  按其他 pack/jobs 单测的做法先导入，设好模块路径

import ipa


class WinMilestoneTests(unittest.TestCase):
    def test_success_lines_advance(self):
        self.assertEqual(ipa.win_milestone("[19:54:43] 播种：11.1 秒"), "refresh")
        self.assertEqual(ipa.win_milestone("[19:56:14] 八个轻阶段：91.1 秒"), "direct")
        self.assertEqual(ipa.win_milestone("[17:37:39] 抽取 + 直连库：900.2 秒"), "ipa")
        self.assertEqual(ipa.win_milestone("[17:37:39] 抽取 + 直连库：900.2 秒（rc=0）"), "ipa")

    def test_failed_step_does_not_advance(self):   # #269 原文：失败停在「抽取 + 直连库」所属的 direct 段（编译着色器库），不进 ipa
        self.assertIsNone(ipa.win_milestone("[19:56:15] 抽取 + 直连库：1.7 秒（rc=1）"))
        self.assertIsNone(ipa.win_milestone("[03:09:42] 抽取 + 直连库：39.3 秒（rc=12）"))
        self.assertIsNone(ipa.win_milestone("[19:56:14] 八个轻阶段：91.1 秒（rc=2）"))

    def test_other_lines_ignored(self):
        self.assertIsNone(ipa.win_milestone("【红】direct_lib.log：rc=1；[直连库] 🔴 OpenGL 款要 glslangValidator"))
        self.assertIsNone(ipa.win_milestone("[19:54:31] 判游戏解码器：0.0 秒"))


if __name__ == "__main__":
    unittest.main()
