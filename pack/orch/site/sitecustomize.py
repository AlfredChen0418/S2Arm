# Windows 出包作业的子 Python 进程：装上长路径处理（pack/orch/winlong.py）。win_runtime_env 把本目录放进 PYTHONPATH
import importlib.util
import os

_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "winlong.py")
_spec = importlib.util.spec_from_file_location("s2a_winlong", _path)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
_module.install()
