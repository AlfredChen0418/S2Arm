"""Windows 长路径（玩家反馈 Hades II：[WinError 206] 文件名或扩展名太长）。

系统要开 LongPathsEnabled 才认 260 字符以上的普通路径，玩家机上多半没开、改它又要管理员权限。
这里把文件操作的路径参数在够长时加上 \\\\?\\ 前缀（Windows 官方的长路径写法，不需要系统设置）；短路径原样不动。
只在 Windows 生效。入口：pack.orch 包导入时、作业主程序、子 Python 进程的 sitecustomize（pack/orch/site）。
"""
from __future__ import annotations

import builtins
import io
import ntpath
import os

LIMIT = 240   # 留出目录内再拼文件名的余量；系统上限 260
_done = False


def long_path(path):
    """够长的绝对或相对路径 → \\\\?\\ 形式（UNC 用 \\\\?\\UNC\\）；其余原样返回。非路径参数（文件描述符、None、bytes）不动"""
    try:
        s = os.fspath(path)
    except TypeError:
        return path
    if not isinstance(s, str) or len(s) < LIMIT or s.startswith(("\\\\?\\", "\\\\.\\")):
        return path
    full = ntpath.abspath(s)
    return "\\\\?\\UNC\\" + full[2:] if full.startswith("\\\\") else "\\\\?\\" + full


class _Entry:
    """目录项：路径按调用方给的写法，其余照原 DirEntry（前缀只在系统调用里用，调用方拿到带前缀的路径后 relative_to 比不上，#138）"""
    __slots__ = ("_entry", "path", "name")

    def __init__(self, entry, path):
        self._entry, self.path, self.name = entry, path, entry.name

    def __getattr__(self, name):
        return getattr(self._entry, name)

    def __fspath__(self):
        return self.path


class _Scan:
    def __init__(self, entries, parent):
        self._entries, self._parent = entries, parent

    def __iter__(self):
        return self

    def __next__(self):
        entry = next(self._entries)
        return _Entry(entry, ntpath.join(self._parent, entry.name))

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self._entries.close()


def _wrap_scandir(module):
    original = module.scandir

    def scandir(path="."):
        converted = long_path(path)
        return original(path) if converted is path else _Scan(original(converted), os.fspath(path))

    scandir.__doc__ = original.__doc__
    module.scandir = scandir


def _wrap(module, name, count):
    original = getattr(module, name, None)
    if original is None:
        return
    def wrapper(*args, **kwargs):
        converted = [long_path(a) if i < count else a for i, a in enumerate(args)]
        return original(*converted, **kwargs)
    wrapper.__name__ = name
    wrapper.__doc__ = original.__doc__
    setattr(module, name, wrapper)


def install():
    global _done
    if _done or os.name != "nt":
        return
    _done = True
    for name in ("stat", "lstat", "open", "mkdir", "rmdir", "unlink", "remove", "listdir", "chmod", "utime", "readlink", "truncate", "access"):
        _wrap(os, name, 1)
    _wrap_scandir(os)
    for name in ("rename", "replace", "link", "symlink"):
        _wrap(os, name, 2)
    for name in ("exists", "lexists", "isfile", "isdir", "islink", "getsize", "getmtime", "getatime", "getctime", "isjunction"):
        _wrap(os.path, name, 1)
    _wrap(builtins, "open", 1)
    io.open = builtins.open
    import _winapi
    _wrap(_winapi, "CopyFile2", 2)
    _wrap(_winapi, "CreateJunction", 2)
