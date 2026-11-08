"""Windows 上该用哪个 bash（issue #26）。

Win11 的 PATH 里第一个 bash 常是 System32 的 WSL 存根：没装发行版时往 stdout 写 UTF-16LE 报错、退出码非 0，
拿它跑脚本得到的输出每个字符后面跟 \\x00，写进环境变量后下一次起子进程报 ValueError: embedded null character。
顺序：S2A_BASH（发版启动器随包的 bash）→ Git for Windows 默认位置 → PATH，且 WSL 存根一律不要。
"""
from __future__ import annotations

import ntpath
import os
import shutil

GIT_BASH = r'C:\Program Files\Git\bin\bash.exe'


def is_wsl_stub(path):
    """Windows 自带的 WSL 启动器（System32 / Sysnative 下的 bash.exe，以及应用执行别名 WindowsApps\\bash.exe）"""
    parts = ntpath.normcase(ntpath.normpath(path)).split('\\')
    return parts[-1] == 'bash.exe' and len(parts) > 1 and parts[-2] in ('system32', 'sysnative', 'windowsapps')


def find_bash(env=None, nt=None):
    """→ 可用的 bash 路径；Windows 上一个都没有给 None。非 Windows 就是 PATH 上的 bash"""
    if not (os.name == 'nt' if nt is None else nt):
        return 'bash'
    env = os.environ if env is None else env
    for candidate in (env.get('S2A_BASH'), GIT_BASH, shutil.which('bash', path=env.get('PATH'))):
        if candidate and os.path.isfile(candidate) and not is_wsl_stub(candidate):
            return candidate
    return None
