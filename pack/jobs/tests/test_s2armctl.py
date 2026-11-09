import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]


def load():
    spec = importlib.util.spec_from_file_location("s2armctl_under_test", ROOT / "launchers/macos/s2armctl.py")
    module = importlib.util.module_from_spec(spec)
    with patch.object(sys, "argv", ["s2armctl"]):
        spec.loader.exec_module(module)
    return module


class WaitOp(unittest.TestCase):
    def test_returns_only_after_the_launcher_is_idle(self):
        """作业记录已结束但启动器还在收尾时不能返回：下一条命令会撞 409。"""
        ctl = load()
        done = {"id": "new", "operation": "keys", "state": "completed", "message": "ok", "started": 1}
        states = [dict(tasks=[], games=[]), dict(tasks=[done], games=[], busy=True), dict(tasks=[done], games=[], busy=True), dict(tasks=[done], games=[], busy=False), dict(tasks=[done], games=[], busy=False)]
        seen = []

        def call(method, path, *a, **k):
            seen.append(len(seen))
            return states[min(len(seen) - 1, len(states) - 1)]
        with patch.object(ctl, "call", call), patch.object(ctl, "brief", return_value=""), patch.object(ctl.time, "sleep", lambda s: None), patch("builtins.print"):
            ctl.wait_op("keys", set())
        self.assertGreaterEqual(len(seen), 4)   # 读到 busy=False 之后才结束

    def test_older_launchers_without_the_field_do_not_hang(self):
        ctl = load()
        done = {"id": "new", "operation": "keys", "state": "completed", "message": "ok", "started": 1}
        with patch.object(ctl, "call", return_value=dict(tasks=[done], games=[])), patch.object(ctl, "brief", return_value=""), patch.object(ctl.time, "sleep", lambda s: self.fail("不该等待")), patch("builtins.print"):
            ctl.wait_op("keys", set())


if __name__ == "__main__":
    unittest.main()
