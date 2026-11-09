"""Windows 短根：出包工作区用短路径，避开 MAX_PATH。
<盘>:\\S2ArmTmp\\<工作目录摘要>\\ 下放 t（TMP / TEMP）和 w0、w1…（指向各工作区的目录联接）；oneclick_win 之后所有路径都走联接。
摘要只看工作目录，同一款每次得到同一个短路径：阶段键里的路径不变，联接也一直有效（产物里记的路径不失效），不随进程结束删除。
不同工作目录摘要不同，互不串；同一工作目录由作业锁保证同时只有一单。
环境变量 S2A_SHORT_ROOTS（长路径<TAB>短路径，一行一对）给 winlong：os.path.realpath / Path.resolve 把联接展开成长路径后再映射回短路径，子进程同样生效。
卷不是 NTFS、盘根建不了目录时返回 None，调用方照旧用长路径（打一行黄字，不静默）。"""
from __future__ import annotations

import hashlib
import ntpath
import os
import sys

from .winlong import parse_table, to_short   # noqa: F401  映射规则放 winlong：子进程的 sitecustomize 按文件路径加载它，不依赖 pack 包

BASE = "S2ArmTmp"
ENV = "S2A_SHORT_ROOTS"
TEMP_ENV = "S2A_SHORT_TEMP"


def short_base(path):
    """工作目录 → 它的短根（只算路径，不碰盘）"""
    full = ntpath.abspath(str(path))
    return ntpath.join(ntpath.splitdrive(full)[0] + "\\", BASE, hashlib.sha256(ntpath.normcase(full).encode("utf-8", "surrogatepass")).hexdigest()[:16])


def to_long(path, table):
    """短路径 → 真实长路径（目录联接的目标要写长路径）"""
    text = str(path)
    low = ntpath.normcase(text)
    for _, long, short in table:
        key = ntpath.normcase(short)
        if low == key or low.startswith(key + "\\"):
            return long + text[len(short):]
    return text


def table_now():
    return parse_table(os.environ.get(ENV, ""))


def _is_ntfs(path):
    import ctypes as C
    k = C.WinDLL("kernel32", use_last_error=True)
    fs = C.create_unicode_buffer(64)
    return bool(k.GetVolumeInformationW(ntpath.splitdrive(path)[0] + "\\", None, 0, None, None, None, fs, 64)) and fs.value.upper() == "NTFS"


def _is_junction(path):
    try:
        return os.path.isjunction(path)
    except (AttributeError, OSError):
        return False


def _link(link, target):
    import _winapi
    if os.path.lexists(link):
        if _is_junction(link) and ntpath.normcase(_read_target(link)) == ntpath.normcase(target):
            return
        os.rmdir(link)   # 只删联接本身，不碰目标
    _winapi.CreateJunction(target, link)


def _read_target(link):
    target = os.readlink(link)
    return target[4:] if target.startswith("\\\\?\\") else target


def sweep(root):
    """清掉目标已不在的残留：只 rmdir 联接本身，临时目录整个删"""
    import shutil
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        base = ntpath.join(root, name)
        try:
            links = [ntpath.join(base, n) for n in os.listdir(base) if _is_junction(ntpath.join(base, n))]
        except OSError:
            continue
        if links and all(os.path.isdir(_read_target(l)) for l in links):
            continue
        for link in links:
            os.rmdir(link)
        shutil.rmtree(ntpath.join(base, "t"), ignore_errors=True)
        try:
            os.rmdir(base)
        except OSError:
            pass


def enter(out, *others):
    """给工作目录 out 和其它工作区建短根；→ {原路径: 短路径}，建不了 → None。
    设 S2A_SHORT_ROOTS（子进程继承）与 S2A_SHORT_TEMP"""
    full = ntpath.abspath(str(out))
    os.makedirs(full, exist_ok=True)
    base = short_base(full)
    try:
        if not _is_ntfs(full):
            print("【黄】工作目录所在卷不是 NTFS，建不了目录联接，仍用长路径", file=sys.stderr)
            return None
        root = ntpath.dirname(base)
        os.makedirs(root, exist_ok=True)
        sweep(root)
        os.makedirs(ntpath.join(base, "t"), exist_ok=True)
        mapping, rows = {}, []
        for i, path in enumerate([full, *[ntpath.abspath(str(p)) for p in others if p]]):
            link = ntpath.join(base, f"w{i}")
            os.makedirs(path, exist_ok=True)
            _link(link, path)
            mapping[path] = link
            rows.append(f"{path}\t{link}")
    except OSError as error:
        print(f"【黄】建不了短路径工作区（{error}），仍用长路径", file=sys.stderr)
        return None
    os.environ[ENV] = "\n".join(rows)
    os.environ[TEMP_ENV] = ntpath.join(base, "t")
    return mapping
