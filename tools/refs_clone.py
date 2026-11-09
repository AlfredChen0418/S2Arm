#!/usr/bin/env python3
"""Windows ReFS 块克隆（#1100 Windows 出包，11-04 Windows 出包线）：clone_file(src, dst) → True = 块克隆成了（不占新空间，同 macOS clonefile），
False = 做不了（不是 Windows、跨卷、不是 ReFS / 开发驱动器、文件系统拒绝），调用方退回普通拷贝。dst 不能已存在。
做法：建空的 dst、设成与 src 同长（源是稀疏文件则 dst 也标稀疏），再按簇对齐分段发 FSCTL_DUPLICATE_EXTENTS_TO_FILE（每段 < 4 GiB）；
最后一段长度向上取整到簇（ReFS 允许克隆到文件尾所在簇的末尾）。失败时删掉半成品 dst。修改时间按源设（同 shutil.copy2 的时间语义）。
  refs_clone.py <源> <目标>     命令行试一次：成了退 0，做不了退 1（自检 / 排障用）
"""
import ctypes
import os
import sys

FSCTL_DUPLICATE_EXTENTS_TO_FILE = 0x00098344
FSCTL_SET_SPARSE = 0x000900C4
CHUNK = 1 << 30
_FS = {}   # 盘符 → 文件系统名（GetVolumeInformationW，每卷只问一次）


def _volume_fs(drive):
    """盘符（如 'C:'）→ 文件系统名（'NTFS' / 'ReFS' …）；问不到回 ''。开发驱动器也是 ReFS"""
    if drive not in _FS:
        from ctypes import wintypes as w
        buf = ctypes.create_unicode_buffer(64)
        ok = ctypes.WinDLL('kernel32').GetVolumeInformationW(ctypes.c_wchar_p(drive + '\\'), None, 0, None, None, None, buf, 64)
        _FS[drive] = buf.value if ok else ''
    return _FS[drive]


def clone_file(src, dst):
    if sys.platform != 'win32': return False
    from ctypes import wintypes as w
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.CreateFileW.restype = w.HANDLE
    k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
    k.DeviceIoControl.argtypes = [w.HANDLE, w.DWORD, ctypes.c_void_p, w.DWORD, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p]
    k.GetDiskFreeSpaceW.argtypes = [w.LPCWSTR, ctypes.POINTER(w.DWORD), ctypes.POINTER(w.DWORD), ctypes.POINTER(w.DWORD), ctypes.POINTER(w.DWORD)]
    INVALID = w.HANDLE(-1).value
    # 先解开目录联接 / 软链再比盘符（11-06 出包速度线：Windows 出包机作业目录 R:\…\lane\<单号> 是指到 T: 的联接，按字面盘符比，联接里外的文件判错跨卷 / 同卷，
    #   该克隆的退回整份拷贝——王国之泪「ipa 收尾」1830 秒）。目标还不存在：realpath 解开已存在的上级
    src, dst = getattr(os.path.realpath, 'long', os.path.realpath)(src), getattr(os.path.realpath, 'long', os.path.realpath)(dst)
    if os.path.splitdrive(src)[0].upper() != os.path.splitdrive(dst)[0].upper(): return False   # 跨卷不能克隆
    # 不是 ReFS 的卷（玩家 NTFS）每卷只问一次文件系统名就回 False：原先每个文件都建空目标、发 FSCTL 失败、再删目标
    #   （11-06 出包速度线：Windows 出包机 C 盘 NTFS 王国之泪资源包，每个文件白耗约 1 毫秒，30.5 万个）
    if _volume_fs(os.path.splitdrive(dst)[0].upper()).upper() != 'REFS': return False
    spc, bps, a, b = w.DWORD(), w.DWORD(), w.DWORD(), w.DWORD()
    if not k.GetDiskFreeSpaceW(os.path.splitdrive(dst)[0] + '\\', ctypes.byref(spc), ctypes.byref(bps), ctypes.byref(a), ctypes.byref(b)): return False
    cluster = spc.value * bps.value
    size = os.path.getsize(src)
    hs = k.CreateFileW(src, 0x80000000, 1, None, 3, 0x80, None)   # GENERIC_READ、共享读、OPEN_EXISTING
    if hs == INVALID: return False
    hd = INVALID; ok = False
    try:
        hd = k.CreateFileW(dst, 0xC0000000, 0, None, 1, 0x80, None)   # GENERIC_READ|WRITE、CREATE_NEW
        if hd == INVALID: return False
        n = w.DWORD()
        attrs = k.GetFileAttributesW(src)
        if attrs != 0xFFFFFFFF and attrs & 0x200 and not k.DeviceIoControl(hd, FSCTL_SET_SPARSE, None, 0, None, 0, ctypes.byref(n), None): return False
        li = ctypes.c_longlong(size)
        if not k.SetFilePointerEx(hd, li, None, 0) or not k.SetEndOfFile(hd): return False

        class DUP(ctypes.Structure):
            _fields_ = [('FileHandle', w.HANDLE), ('SourceFileOffset', ctypes.c_longlong), ('TargetFileOffset', ctypes.c_longlong), ('ByteCount', ctypes.c_longlong)]
        off, end = 0, (size + cluster - 1) // cluster * cluster
        while off < end:
            cnt = min(CHUNK, end - off)
            d = DUP(hs, off, off, cnt)
            if not k.DeviceIoControl(hd, FSCTL_DUPLICATE_EXTENTS_TO_FILE, ctypes.byref(d), ctypes.sizeof(d), None, 0, ctypes.byref(n), None): return False
            off += cnt
        ok = True
        return True
    finally:
        k.CloseHandle(hs)
        if hd != INVALID: k.CloseHandle(hd)
        if hd != INVALID and not ok:
            try: os.remove(dst)
            except OSError: pass
        if ok:
            st = os.stat(src); os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))


if __name__ == '__main__':
    if len(sys.argv) != 3: sys.exit(__doc__)
    sys.exit(0 if clone_file(sys.argv[1], sys.argv[2]) else 1)
