#!/usr/bin/env python3
"""pack/ns2_nup.py 单测：按 NUP v1 规格（a55uka/nup SPEC.md）在测试里拼合成容器，期望值是写进去的内容本身，不跑被测实现反推。

① 本体 + 两版更新 + 1 个 DLC：只展开本体与最新更新（v131072），旧版 v65536 不写；文件逐字节相同；zstd 存的文件解得出；
   最新更新带控制分区 → updates/131072/control.nacp 与 icon_AmericanEnglish.dat 是写进去的字节；DLC 落 dlc/<标题号>/0/romfs
② 接 pack/ns2_dump.py：出的解包目录 exefs/main.npdm 程序号 = 合成值，ctl/control.nacp = 原件（不是合成件），romfs 取最新更新
③ 同一容器再跑：复用（打「NUP 没变」），输出目录 inode 不变
④ 没有本体（更新带 standalone_update）：打黄字，最新更新同时写成 base/
⑤ 改坏数据区一个字节 → 报红（SHA-256 对不上），不留输出
⑥ --detect：.nup 文件、含一个 .nup 的文件夹 → 退出 0；普通文件、含两个 .nup 的文件夹 → 退出 1
"""
import hashlib
import os
import shutil
import struct
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
NUP = os.path.join(ROOT, 'pack', 'ns2_nup.py')
DUMP = os.path.join(ROOT, 'pack', 'ns2_dump.py')
APP = 0x0400486024DEC000
SUPER = struct.Struct('<4sHHQQ32sQQII16s32s')
TOC = struct.Struct('<4sIQQQQQ')
PKG = struct.Struct('<QIIIIBBHIQI20x')
DIR = struct.Struct('<IIIIQ')
FILE = struct.Struct('<IIQQQ32sBBH12x')
TLV = struct.Struct('<HHI')
NO_DIR = 0xFFFFFFFF

try:
    from compression import zstd as Z

    def zc(b):
        return Z.compress(b)
except ImportError:
    import zstandard

    def zc(b):
        return zstandard.ZstdCompressor().compress(b)


def npdm(pid):   # 最小 META：0x70 处 ACI0 偏移 / 大小，ACI0+0x10 程序号
    b = bytearray(0x100)
    b[:4] = b'META'
    struct.pack_into('<II', b, 0x70, 0x80, 0x40)
    b[0x80:0x84] = b'ACI0'
    struct.pack_into('<Q', b, 0x90, pid)
    return bytes(b)


def build(path, pkgs, corrupt=False):
    """pkgs = [(kind, title_id, version, flags, exefs{名: 字节} 或 None, romfs{相对路径: 字节} 或 None, meta{类型: 字节})]；
    romfs 里名字以 .z 结尾的文件用 zstd 存（存进容器时去掉 .z 后缀）。"""
    dirs, files, slots_of = [], [], {}   # dirs: [名字, 子项列表]；files: [名字, 原始字节, 编码]

    def mkdir(name):
        dirs.append([name, []])
        return len(dirs) - 1

    def tree(d, mapping):
        for rel, data in sorted(mapping.items()):
            parts = rel.split('/')
            cur = d
            for p in parts[:-1]:
                hit = next((x for x in dirs[cur][1] if x[0] == 'd' and dirs[x[1]][0] == p.encode()), None)
                if hit is None:
                    nd = mkdir(p.encode())
                    dirs[cur][1].append(('d', nd))
                    cur = nd
                else:
                    cur = hit[1]
            fn = parts[-1]
            codec = 1 if fn.endswith('.z') else 0
            files.append([fn[:-2].encode() if codec else fn.encode(), data, codec])
            dirs[cur][1].append(('f', len(files) - 1))
    recs = []
    for kind, tid, ver, flags, exefs, romfs, meta in pkgs:
        root = mkdir(b'')
        e = r = NO_DIR
        if exefs is not None:
            e = mkdir(b'exefs')
            dirs[root][1].append(('d', e))
            tree(e, exefs)
        if romfs is not None:
            r = mkdir(b'romfs')
            dirs[root][1].append(('d', r))
            tree(r, romfs)
        recs.append([tid, root, e, r, ver, kind, flags, meta])
    ndirs, nfiles = len(dirs), len(files)
    pool = bytearray()

    def pname(b):
        o = len(pool)
        pool.extend(b)
        return o, len(b)
    slots = []
    dir_recs = []
    for name, kids in dirs:
        o, n = pname(name)
        first = len(slots)
        for t, i in kids:
            slots.append(i if t == 'd' else ndirs + i)
        dir_recs.append((o, n, first, len(kids)))
    file_names = [pname(f[0]) for f in files]
    head = 0x80
    toc_fixed = TOC.size + len(recs) * PKG.size + ndirs * DIR.size + nfiles * FILE.size + 4 * len(slots)
    # 元数据块放在目录表后、数据区前
    metas = []
    for rec in recs:
        blob = b''.join(TLV.pack(t, 0, len(v)) + v for t, v in sorted(rec[7].items()))
        metas.append(blob)
    pool_off = toc_fixed
    toc_size = pool_off + len(pool)
    meta_off = head + toc_size
    meta_total = sum(len(m) for m in metas)
    blob_start = (meta_off + meta_total + 15) & ~15
    data = bytearray()
    frecs = []
    for (o, n), (name, raw, codec) in zip(file_names, files):
        stored = zc(raw) if codec else raw
        if raw:
            pad = (-(blob_start + len(data))) % 16
            data.extend(b'\0' * pad)
            eoff = blob_start + len(data)
            data.extend(stored)
            frecs.append((o, n, len(raw), eoff, len(stored), hashlib.sha256(raw).digest(), codec))
        else:
            frecs.append((o, n, 0, 0, 0, hashlib.sha256(b'').digest(), 0))
    toc = bytearray(TOC.pack(b'TOC\x01', len(recs), ndirs, nfiles, pool_off, len(pool), 0))
    mo = meta_off
    for rec, m in zip(recs, metas):
        tid, root, e, r, ver, kind, flags, _ = rec
        toc += PKG.pack(tid, root, e, r, ver, kind, flags, 0, 0, mo if m else 0, len(m))
        mo += len(m)
    for o, n, first, count in dir_recs:
        toc += DIR.pack(o, n, first, count, 0)
    for o, n, size, eoff, esz, sha, codec in frecs:
        toc += FILE.pack(o, n, size, eoff, esz, sha, codec, 0, 0)
    toc += struct.pack(f'<{len(slots)}I', *slots)
    toc += pool
    assert len(toc) == toc_size
    fsize = blob_start + len(data)
    sup = SUPER.pack(b'NUP\x01', 0x80, 0, head, toc_size, hashlib.sha256(toc).digest(), blob_start, fsize, len(recs), 0, b'\0' * 16, b'\0' * 32)
    body = bytearray(sup + toc + b''.join(metas))
    body += b'\0' * (blob_start - len(body))
    body += data
    if corrupt:   # 改坏最后一个原样文件的一个字节（数据区尾部）
        body[-1] ^= 0xFF
    with open(path, 'wb') as f:
        f.write(body)


def run(*a):
    return subprocess.run([PY, *a], capture_output=True, text=True)


ok = bad = 0


def chk(name, got, want):
    global ok, bad
    if got == want:
        ok += 1
        print(f'通 {name}')
    else:
        bad += 1
        print(f'不通 {name}：期望 {want!r} → 真结果 {got!r}')


def rd(p):   # 不存在返回 None（负对照下给出「不通」行，而不是异常退出）
    try:
        with open(p, 'rb') as f:
            return f.read()
    except OSError:
        return None


NACP = bytes(range(256)) * 64   # 0x4000 字节，可辨认的内容
ICON = b'\xff\xd8\xff\xe0JPGICON'
MAIN_B, MAIN_U = b'\x01' * 300, b'\x02' * 500
BIG = os.urandom(70000)
with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, 'Zelda Test [0400486024dec000][v131072].nup')
    pk = [
        (0, APP, 0, 0, {'main': MAIN_B, 'main.npdm': npdm(APP)}, {'a.bin': b'base-a', 'dir/c.bin.z': BIG, 'same.bin': b'S' * 4096}, {}),
        (1, APP | 0x800, 65536, 0, {'main': b'\x09' * 10, 'main.npdm': npdm(APP)}, {'a.bin': b'old'}, {}),
        (1, APP | 0x800, 131072, 0, {'main': MAIN_U, 'main.npdm': npdm(APP)}, {'a.bin': b'upd-a', 'dir/c.bin.z': BIG, 'same.bin': b'S' * 4096}, {2: NACP, 3: ICON}),
        (2, APP + 0x1001, 0, 0, None, {'dlc.bin': b'DLC1'}, {}),
    ]
    build(src, pk)
    out = os.path.join(t, 'dump')
    r = run(NUP, src, out)
    chk('① 退出 0', r.returncode, 0)
    chk('① 本体 exefs/main', rd(os.path.join(out, 'base/exefs/main')), MAIN_B)
    chk('① 最新更新 exefs/main', rd(os.path.join(out, 'updates/131072/exefs/main')), MAIN_U)
    chk('① 旧版 v65536 不写', os.path.exists(os.path.join(out, 'updates/65536')), False)
    chk('① zstd 文件解得出（7 万字节随机）', rd(os.path.join(out, 'updates/131072/romfs/dir/c.bin')), BIG)
    chk('① 控制分区 control.nacp', rd(os.path.join(out, 'updates/131072/control.nacp')), NACP)
    chk('① 图标', rd(os.path.join(out, 'updates/131072/icon_AmericanEnglish.dat')), ICON)
    chk('① DLC', rd(os.path.join(out, f'dlc/{APP + 0x1001:016X}/0/romfs/dlc.bin')), b'DLC1')
    # ②
    ex = os.path.join(t, 'extract')
    r2 = run(DUMP, out, ex)
    chk('② ns2_dump 退出 0', r2.returncode, 0)
    if r2.returncode == 0:
        b = rd(os.path.join(ex, 'exefs/main.npdm'))
        chk('② 程序号', struct.unpack_from('<Q', b, 0x90)[0], APP)
        chk('② ctl/control.nacp 是原件', rd(os.path.join(ex, 'ctl/control.nacp')), NACP)
        chk('② 没写合成说明', os.path.exists(os.path.join(ex, 'ctl/README.synth')), False)
        chk('② romfs 取最新更新', rd(os.path.join(ex, 'romfs/a.bin')), b'upd-a')
    else:
        print(r2.stdout[-600:], r2.stderr[-600:])
    # ③
    ino = os.stat(out).st_ino
    r3 = run(NUP, src, out)
    chk('③ 复用', ('NUP 没变' in r3.stdout, os.stat(out).st_ino), (True, ino))
    # ④
    src2 = os.path.join(t, 'nobase.nup')
    build(src2, [(1, APP | 0x800, 131072, 1, {'main': MAIN_U, 'main.npdm': npdm(APP)}, {'a.bin': b'upd-a'}, {})])
    out2 = os.path.join(t, 'dump2')
    r4 = run(NUP, src2, out2)
    chk('④ 无本体：退出 0 + 黄字', (r4.returncode, '没有本体' in r4.stdout), (0, True))
    chk('④ 无本体：base/exefs/main = 更新的', rd(os.path.join(out2, 'base/exefs/main')) if r4.returncode == 0 else None, MAIN_U)
    # ⑤
    src3 = os.path.join(t, 'bad.nup')
    build(src3, pk, corrupt=True)
    out3 = os.path.join(t, 'dump3')
    r5 = run(NUP, src3, out3)
    chk('⑤ 坏字节报红', (r5.returncode, 'SHA-256' in r5.stderr, os.path.exists(out3)), (1, True, False))
    # ⑥
    plain = os.path.join(t, 'plain.bin')
    open(plain, 'wb').write(b'not a nup')
    one = os.path.join(t, 'one')
    os.makedirs(one)
    shutil.copy(src2, os.path.join(one, 'x.nup'))
    two = os.path.join(t, 'two')
    os.makedirs(two)
    shutil.copy(src2, os.path.join(two, 'a.nup'))
    shutil.copy(src2, os.path.join(two, 'b.nup'))
    chk('⑥ --detect', [run(NUP, '--detect', p).returncode for p in (src, one, plain, two)], [0, 0, 1, 1])
    # ⑦ 写出时出错（11-06 Windows 出包机时之笛：并发出包把 T 盘写满，OSError 28 被 with 退出时关映射的 BufferError 盖掉，日志只见 BufferError）：
    #   进程内调 Nup.copy，写入对象一写就抛「磁盘已满」→ 抛出来的必须是原来的 OSError 28，映射已关；原样 / zstd 两条路都核。
    #   另：with 块里还留着导出的视图又抛了别的错 → 关映射失败不得盖掉原异常（原异常照抛、带一句关映射失败的说明）
    sys.path.insert(0, os.path.join(ROOT, 'pack'))
    import ns2_nup as N

    class Full:
        def write(self, b):
            raise OSError(28, 'No space left on device')

    def first(codec):
        with N.Nup(src) as n:
            return next(i for i, f in enumerate(n.files) if f[5] == codec and f[1])

    for codec, what in ((0, '原样'), (1, 'zstd')):
        err, nn = None, None
        try:
            with N.Nup(src) as nn:
                nn.copy(first(codec), Full())
        except BaseException as e:
            err = e
        chk(f'⑦ {what}文件写出时磁盘满：抛原异常 OSError 28（不是 BufferError）、映射已关',
            (type(err).__name__, getattr(err, 'errno', None), bool(nn and nn.mm.closed)), ('OSError', 28, True))
    err, keep = None, None
    try:
        with N.Nup(src) as nn:
            keep = memoryview(nn.mm)
            raise ValueError('with 块里的原异常')
    except BaseException as e:
        err = e
    chk('⑦ 关映射失败不盖原异常：抛 ValueError、带关映射失败的说明',
        (type(err).__name__, any('关闭' in x or 'close' in x for x in getattr(err, '__notes__', []))), ('ValueError', True))
    if keep is not None:
        keep.release()
    # ⑧ 共用入口（11-07：启动器作业层 / 共享解包阶段都走它）：--inspect 出认包行（路径 字节 修改时间 程序号）；
    #   --to-extract 一步出解包目录（同 ① ② 两步的结果），中间的 <解包目录>.ns2_dump 不留；没给 --name、没控制分区时标题 = 文件名去掉「 [程序号][版本]」
    r8 = run(NUP, '--inspect', one)
    st = os.stat(os.path.join(one, 'x.nup'))
    chk('⑧ --inspect 认包行', r8.stdout.strip().split('\t'), [os.path.abspath(os.path.join(one, 'x.nup')),str(st.st_size), f'{st.st_mtime_ns // 10**9}.{st.st_mtime_ns % 10**9:09d}', f'{APP:016X}'])
    ex8 = os.path.join(t, 'ex8')
    r8 = run(NUP, '--to-extract', src, ex8)
    chk('⑧ --to-extract 退出 0', r8.returncode, 0)
    chk('⑧ --to-extract 产物同 ① ② 两步', [rd(os.path.join(ex8, p)) for p in ('exefs/main', 'ctl/control.nacp', 'romfs/a.bin', 'aoc/1/dlc.bin')], [MAIN_U, NACP, b'upd-a', b'DLC1'])
    chk('⑧ 中间导出目录不留', sorted(n for n in os.listdir(t) if n.startswith('ex8')), ['ex8'])
    src9 = os.path.join(t, 'Bare Name [0400486024dec000].nup')
    shutil.copy(src2, src9)
    ex9 = os.path.join(t, 'ex9')
    run(NUP, '--to-extract', src9, ex9)
    chk('⑧ 没控制分区：标题取文件名', 'name\tBare Name' in (rd(os.path.join(ex9, 'unpack.tsv')) or b'').decode(), True)
    # ⑨ 共享解包阶段 pack/stages/unpack.sh（出包队列 packq 走它）：源目录里只有一个 .nup → 认包行出程序号、extract 走 ns2_nup.py --to-extract，不要 prod.keys；
    #   同输入再跑复用；源目录里是假 .nup（不是 NUP 魔数）→ 照旧报「没有原始包」
    us = os.path.join(ROOT, 'pack', 'stages', 'unpack.sh')
    env9 = dict(os.environ, S2A_NX_UNPACK='/usr/bin/true', S2A_KEYS=os.path.join(t, 'no.keys'), S2A_SRC_IDS=os.path.join(t, 'ids.tsv'), S2A_UNPACK_CACHE='off')
    def ush(*a):
        return subprocess.run(['bash', us, *a], capture_output=True, text=True, env=env9)
    r9 = ush('inspect', '--src', one)
    chk('⑨ unpack.sh inspect 认出程序号', (r9.returncode, r9.stdout.strip().split('\t')[-1:]), (0, [f'{APP:016X}']))
    x9 = os.path.join(t, 'x9')
    r9 = ush('extract', '--src', one, '--out', x9)
    chk('⑨ unpack.sh extract 退出 0', r9.returncode, 0)
    if r9.returncode:
        print(r9.stdout[-600:], r9.stderr[-600:])
    chk('⑨ unpack.sh 产物：exefs/main、unpack.tsv 程序号、阶段键', (rd(os.path.join(x9, 'exefs/main')), f'title_id\t{APP:016x}' in (rd(os.path.join(x9, 'unpack.tsv')) or b'').decode(),
                                                                  os.path.isfile(os.path.join(x9, '.stage_unpack'))), (MAIN_U, True, True))
    r9 = ush('extract', '--src', one, '--out', x9)
    chk('⑨ unpack.sh 同输入再跑复用', (r9.returncode, '复用' in r9.stdout), (0, True))
    fake = os.path.join(t, 'fake')
    os.makedirs(fake)
    open(os.path.join(fake, 'x.nup'), 'wb').write(b'not a nup')
    r9 = ush('extract', '--src', fake, '--out', os.path.join(t, 'x9f'))
    chk('⑨ unpack.sh 假 .nup 照旧报没有原始包', (r9.returncode, '没有 .xci' in r9.stderr), (1, True))

# ⑩ 非 macOS 走真拷，不碰 ctypes（#128：Windows 上 ctypes.CDLL(None) 抛 TypeError，无本体的 NUP 每次解包崩）
with tempfile.TemporaryDirectory() as t:
    from unittest import mock
    import ctypes
    sys.path.insert(0, os.path.join(ROOT, 'pack'))
    import ns2_nup
    src, dst = os.path.join(t, 'a'), os.path.join(t, 'b')
    open(src, 'wb').write(b'content')
    with mock.patch.object(ns2_nup.sys, 'platform', 'win32'), mock.patch.object(ctypes, 'CDLL', side_effect=TypeError('NoneType')) as cdll:
        got = ns2_nup._clone(src, dst)
    chk('⑩ 非 macOS：真拷、不调 ctypes', (got == dst, open(dst, 'rb').read(), cdll.called), (True, b'content', False))

print(f'{"✓" if not bad else "✗"} ns2_nup：{ok} 过 / {bad} 不过')
sys.exit(1 if bad else 0)
