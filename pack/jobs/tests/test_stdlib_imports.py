"""pack/jobs 里用到的标准库模块都要 import（#9：build.py 的 extract_folder 用了 tempfile 却没 import，
解包目录 / Switch 2 dump 导入的游戏出包必现 NameError；原先没有任何单测走到那条分支）。静态检查：不跑游戏、不依赖机器。
射程：只抓「模块级名字是标准库模块名、整个文件里没绑定过」的用法；局部变量与第三方库不在范围内。"""
import ast
import sys
import unittest
from pathlib import Path

JOBS = Path(__file__).resolve().parents[1]


def unbound_stdlib_names(path: Path) -> dict:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bound = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            bound |= {(a.asname or a.name).split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            bound |= {a.asname or a.name for a in n.names}
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(n.name)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            bound.add(n.id)
        elif isinstance(n, ast.arg):
            bound.add(n.arg)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
    return {n.id: n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in sys.stdlib_module_names and n.id not in bound}


class StdlibImportTests(unittest.TestCase):
    def test_every_stdlib_module_used_is_imported(self):
        bad = {p.name: u for p in sorted(JOBS.glob("*.py")) if (u := unbound_stdlib_names(p))}
        self.assertEqual(bad, {}, "用了标准库模块但没 import（文件 → {模块: 行号}）")

    def test_checker_catches_the_issue_9_shape(self):
        import tempfile as _tmp
        with _tmp.TemporaryDirectory() as d:
            f = Path(d) / "x.py"
            f.write_text("import os\ndef g():\n    with tempfile.TemporaryDirectory() as s:\n        return s\n", encoding="utf-8")
            self.assertEqual(unbound_stdlib_names(f), {"tempfile": 3})


if __name__ == "__main__":
    unittest.main()
