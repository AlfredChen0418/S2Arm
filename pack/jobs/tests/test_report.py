import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tools"))
import report  # noqa: E402
from core import Failure  # noqa: E402


class ReportTests(unittest.TestCase):
    """期望值按摘要的约定手写：每类失败都要带上定位问题所需的事实，且不泄露路径和密钥。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.private = Path(self.tmp.name)
        (self.private / "jobs").mkdir(); (self.private / "logs").mkdir(); (self.private / "keys").mkdir()
        self.library = self.private / "lib"; self.library.mkdir()
        (self.library / "catalog.json").write_text(json.dumps({"games": [{"id": "0100", "name": "Game", "display": "1.2", "packages": [
            {"type": "base", "version": 0, "format": "", "size": 2048}, {"type": "update", "version": 65536, "format": "", "size": 1024}]}]}))

    def job(self, task, **fields):
        record = {"id": task, "operation": "ipa", "state": "failed", "stage": "解包", "message": "坏了", "started": 100.0, "ended": 160.0} | fields
        (self.private / "jobs" / (task + ".json")).write_text(json.dumps(record))
        (self.private / "jobs" / (task + ".request.json")).write_text(json.dumps({"library": str(self.library), "operation": record["operation"]}))
        return record

    def test_failed_export_carries_cause_game_and_real_log(self):
        log = self.private / "logs" / "t1.log"; log.write_text("first line\nfatal: bad nca\n")
        self.job("t1", gameId="0100", milestone="unpack", milestones=[{"id": "unpack"}, {"id": "ipa"}], milestoneStarted={"unpack": 100.0, "ipa": 130.0},
                 diagnostics=[{"code": "content_invalid", "component": "S2Arm", "stage": "解包", "summary": "文件结构校验没通过", "suggestion": "重新导入", "logPath": str(log), "exitCode": 3}])
        text = report.build(self.private, "t1", {"platform": "iPhone / iPad", "app": "0.1.0", "build": "abc", "device": "iPhone17,1", "checks": [{"name": "Metal", "message": "缺"}]})
        for expected in ("平台：iPhone / iPad", "任务号：t1", "出错阶段：unpack", "错误码：content_invalid（S2Arm，阶段 解包，退出码 3）", "文件结构校验没通过", "建议：重新导入",
                         "unpack 30 秒；ipa 30 秒", "名称：Game", "程序号：0100", "包：update v65536 NSP/XCI 1.00 KB", "原包合计：3.00 KB", "S2Arm：0.1.0（abc）", "目标设备：iPhone17,1",
                         "Metal：缺", "fatal: bad nca", "密钥：没有", "反馈："):
            self.assertIn(expected, text)

    def test_killed_job_says_there_was_no_diagnosis(self):
        self.job("t2", state="interrupted")
        text = report.build(self.private, "t2")
        self.assertIn("结果：interrupted", text)
        self.assertIn("错误码：无（作业没有写出诊断", text)
        self.assertIn("没有留下日志文件", text)

    def test_python_crash_log_is_included(self):
        (self.private / "logs" / "t3-exception.log").write_text("Traceback\nKeyError: 'x'\n")
        self.job("t3", operation="import")
        self.assertIn("KeyError: 'x'", report.build(self.private, "t3"))

    def test_detail_used_when_log_file_is_gone(self):
        self.job("t4", diagnostics=[{"code": "stage_timeout", "summary": "超时", "detail": "internal rc=9", "logPath": str(self.private / "gone.log")}])
        self.assertIn("internal rc=9", report.build(self.private, "t4"))

    def test_home_and_key_values_are_removed(self):
        secret = "ab" * 16
        (self.private / "keys/prod.keys").write_text(f"header_key = {secret}\n")
        (self.private / "logs" / "t5.log").write_text(f"path {Path.home()}/x key {secret}\n")
        self.job("t5")
        text = report.build(self.private, "t5")
        self.assertNotIn(str(Path.home()) + "/x", text)
        self.assertNotIn(secret, text)
        self.assertIn("密钥：已放入", text)

    def test_uuid_task_id_survives_redaction(self):
        task = "d0b6f440-9fc5-43d6-acdb-983d53f0ff38"
        (self.private / "logs" / (task + ".log")).write_text("boom\n")
        self.job(task)
        text = report.build(self.private, task)
        self.assertIn("任务号：" + task, text)
        self.assertIn(task + ".log", text)

    def test_long_log_keeps_the_end(self):
        (self.private / "logs" / "t6.log").write_text("start\n" + "x" * 50000 + "\nthe end\n")
        self.job("t6")
        text = report.build(self.private, "t6")
        self.assertIn("the end", text)
        self.assertNotIn("start\nxxx", text)
        self.assertLess(len(text), 20000)

    def test_newest_failed_task_is_picked_within_filters(self):
        self.job("old", gameId="0100", started=1.0); self.job("new", gameId="0100", started=2.0); self.job("mac", gameId="0100", operation="build", started=3.0)
        self.job("ok", gameId="0100", state="completed", started=4.0)
        self.assertEqual(report.find_task(self.private, "0100", ["ipa"]), "new")
        self.assertEqual(report.find_task(self.private, "0100"), "mac")
        with self.assertRaises(Failure):
            report.find_task(self.private, "9999")


if __name__ == "__main__":
    unittest.main()
