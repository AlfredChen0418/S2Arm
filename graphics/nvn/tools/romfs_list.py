#!/usr/bin/env python3
"""RomFS 清单：仓库里唯一遍历 RomFS 目录的地方（11-03 维护者根修；tools/romfs_walk_lint.py 守住，别处出现 RomFS 目录遍历即红）。

起因：一键出包里 6–8 个步骤各自从头遍历 RomFS、逐个打开文件读头（火纹 Switch 2 版 27.4 万个文件：配置推导单次 337 秒、
GLSLC 判「要不要」82 秒、能力清单 326 秒）。现在：
  - 目录盘点（scandir + lstat，只取文件信息，不打开文件）只有这里一份实现：scan() / walk() / listdir()；
  - 文件头（前 32 字节）只读一次：heads() 按「内容身份」把读头结果落盘成清单，同一份 RomFS 的各版本根、各阶段、各进程按身份命中就不再读盘。

清单（接口见 graphics/nvn/tools/README.profile.md「RomFS 清单」）：
  位置：<清单目录>/<标签>-<身份>.json。清单目录 = 环境变量 S2A_ROMFS_WALK（给了就用；off = 不落盘），否则 <产物根的上一级>/.romfs-walk
        （roots.sh 版本根 → <S2A_ROOTS>/.romfs-walk，各版本、各阶段共用）；标签 = 产物根目录名 @ 之前（同一款各版本共用一个前缀，只留最近两份）。
  身份：全部条目按相对路径（/ 分隔）排序，每条「相对路径 \\0 类型 \\0 大小:修改时间纳秒（文件才带）」喂 sha256（前缀 FORMAT），取前 32 位。
        不含 inode / ctime / 目录时间：clonefile 克隆的版本根身份不变；阶段改了 RomFS 文件 → 大小或修改时间变 → 身份变 → 重读。
        同 make / rsync 快速比对的假设：内容变了修改时间必变。盘点有任何错误（目录打不开、lstat 失败）→ 没有身份，不读也不写清单。
  格式：UTF-8 JSON（ensure_ascii）{"format": FORMAT, "identity": 身份, "head": 32, "entries": [[相对路径, 类型位, 大小, 修改时间纳秒, 头十六进制|null, 读错误|null], …]}
        类型位：1 = 软链、2 = 目录（不跟随软链）、4 = 普通文件（跟随软链）、8 = 目录（跟随软链，os.walk 的分法）；修改时间取 lstat；
        大小：读到头的普通文件取打开后的大小（跟随软链），其余取 lstat；
        头 / 读错误只给类型位含 4 的条目：头 = 打开（跟随软链）读到的前 32 字节；读错误 = [errno, strerror, filename]。
        读：格式、身份、条目集合与类型位全对上才用，否则当没有（重读并重写）；写：临时文件 + os.replace 原子替换；写不了不报错（只是不提速）。
  不进任何键：读清单与现读逐字节相同（tests/romfs_list_test.py 钉住）；清单在仓库外、产物根外，出包读入记录（tools/stage_deps.py）不收。

walk() 与 os.walk(top)（topdown、不跟随软链目录、onerror 同义）逐项相同：每层（目录路径, 子目录名, 其余名）同 scandir 顺序，调用方原地改子目录名单照样生效。
与 os.walk 的差别只有一条：盘点在开始迭代时一次做完（os.walk 边走边盘点），迭代途中 RomFS 被改动看不到——出包期 RomFS 只在阶段的工作副本里改，读的一方不与写的一方并发。
每次调用都现盘点（不跨调用缓存）：同一进程里先后两次盘点照样能看出中间的改动（resource_input_key.py 前后两次盘点比对靠这一点）。
跨平台：只用 os.scandir / os.replace / os.path.join，相对路径一律 /；Windows 照用。
用法（给 shell 阶段）：romfs_list.py has-ext <目录> <后缀>…   任一条目名（不分大小写）以其中一个后缀结尾 → 退 0，否则退 1（同 find -iname '*<后缀>' -print -quit）
                      romfs_list.py sizes <目录> <输出>       普通文件（lstat）每个一行「字节数\\t相对路径」，给 RomFS 索引（services/romfs_index --list，免得它再枚举、逐个打开）
                      romfs_list.py files0 <目录>            普通文件（lstat，不跟随软链；同 find <目录> -type f）每个一行「<目录>/<相对路径>\\0」，scandir 顺序；有读不开的目录退 1（同 find）
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

FORMAT = "s2arm-romfs-walk-v2"
# 读头字节数：profile_guess 魔数（≤ 8）、GLSLC 判 NRO（+0x10..0x14）、SPIR-V 魔数（4）、zstd 字典号帧头（18）都在内
HEAD = 32
# 同标签只留最近两份（产物根只留现行 + 上一版）
KEEP = 2
SYMLINK, DIR, FILE, WALKDIR = 1, 2, 4, 8
_AUTO = object()


class Entry:
    """一个条目：rel = 相对路径（/ 分隔）；name；path = 盘点时的完整路径（os.path.join 拼）；kind = 类型位；
    lstat = 本次盘点的 os.lstat 结果（失败为 None，失败原因在 lstat_error）；size / head / error 只在 heads() 之后、只给普通文件"""
    __slots__ = ("rel", "name", "path", "kind", "lstat", "lstat_error", "size", "head", "error")

    def __init__(self, rel, name, path, kind, st, st_error):
        self.rel, self.name, self.path, self.kind, self.lstat, self.lstat_error = rel, name, path, kind, st, st_error
        self.size = self.head = self.error = None

    symlink = property(lambda self: bool(self.kind & SYMLINK))
    # 跟随软链后是普通文件（Path.is_file / DirEntry.is_file 的分法）
    file = property(lambda self: bool(self.kind & FILE))
    # 跟随软链后是目录（os.walk 归进子目录名单的分法）
    walkdir = property(lambda self: bool(self.kind & WALKDIR))
    # 不跟随软链的目录（会进去盘点）
    realdir = property(lambda self: bool(self.kind & DIR))

    def read(self):
        """(大小, 前 32 字节)；读失败的错误在用到时才抛（与原先「哪一项检查先碰到就由哪一项报」同序）"""
        if self.error is not None:
            raise self.error
        return self.size, self.head


class Tree:
    """一次盘点的结果：top = 调用方给的目录（原样，拼路径同 os.walk）；entries = 全部条目（盘点顺序）；
    children = {目录相对路径（根为 ""）: [该目录下的条目，scandir 顺序]}；errors = {目录相对路径: 打开该目录的 OSError}"""

    def __init__(self, top):
        self.top = os.fspath(top)
        self.entries, self.children, self.errors = [], {}, {}
        self.identity = None
        # 读头之后为 True
        self.complete = False

    def files(self):
        return [e for e in self.entries if e.file]

    def walk(self, onerror=None):
        """同 os.walk(self.top)（topdown=True, followlinks=False）"""
        stack = [(self.top, "")]
        while stack:
            top, rel = stack.pop()
            if rel in self.errors:
                if onerror is not None:
                    onerror(self.errors[rel])
                continue
            kids = self.children.get(rel, [])
            dirs = [e.name for e in kids if e.walkdir]
            nondirs = [e.name for e in kids if not e.walkdir]
            yield top, dirs, nondirs
            byname = {e.name: e for e in kids}
            prefix = rel + "/" if rel else ""
            # 调用方可能原地改过 dirs（排序 / 剪枝）：按改后的名单进，软链目录不进（同 os.walk 的 islink 判）
            for d in reversed(dirs):
                e = byname.get(d)
                if e is not None and not e.symlink:
                    stack.append((os.path.join(top, d), prefix + d))


def scan(top, stat_entries=True):
    """盘点一棵目录（不打开任何文件）：每层 scandir、每个条目 lstat 一次。打不开的目录记进 errors 并跳过（os.walk 同样跳过）。
    stat_entries=False：只要名字与类型（walk 用；类型取目录项自带的 d_type，不逐个 lstat），没有 lstat、没有身份"""
    tree = Tree(top)
    pending = [(tree.top, "")]
    ok = True
    while pending:
        folder, rel = pending.pop()
        kids = []
        try:
            with os.scandir(folder) as it:
                for c in it:
                    kids.append(c)
        except OSError as err:
            tree.errors[rel] = err
            ok = False
            continue
        prefix = rel + "/" if rel else ""
        out = tree.children[rel] = []
        for c in kids:
            kind = 0
            try:
                if c.is_symlink():
                    kind |= SYMLINK
            except OSError:
                pass
            try:
                if c.is_dir(follow_symlinks=False):
                    kind |= DIR
            except OSError:
                pass
            try:
                if c.is_dir():
                    kind |= WALKDIR
            except OSError:
                pass
            try:
                if c.is_file():
                    kind |= FILE
            except OSError:
                pass
            if not kind & DIR and (c.name == ".DS_Store" or c.name.startswith("._")):
                continue   # macOS 垃圾（同 pack/pack.py：不进资源包，也就不能进索引，否则游戏枚举到就读不到）
            st = st_err = None
            if stat_entries:
                try:
                    st = c.stat(follow_symlinks=False)
                except OSError as err:
                    st_err, ok = err, False
            e = Entry(prefix + c.name, c.name, c.path, kind, st, st_err)
            out.append(e)
            tree.entries.append(e)
            if kind & DIR:
                pending.append((c.path, e.rel))
    if ok and stat_entries:
        h = hashlib.sha256(FORMAT.encode())
        for e in sorted(tree.entries, key=lambda e: e.rel.encode("utf-8", "surrogateescape")):
            sig = f"{e.lstat.st_size}:{e.lstat.st_mtime_ns}" if stat.S_ISREG(e.lstat.st_mode) else ""
            h.update(f"{e.rel}\0{e.kind}\0{sig}\n".encode("utf-8", "surrogateescape"))
        tree.identity = h.hexdigest()[:32]
    return tree


def walk(top, onerror=None):
    """os.walk(top) 的替身（RomFS 目录遍历一律经这里）；只取名字与类型，不逐个 lstat"""
    return scan(top, stat_entries=False).walk(onerror)


def listdir(top):
    """os.listdir(top) 的替身：只列 RomFS 某一层（不进子目录），也归这里，好让 RomFS 目录读取只有一处"""
    return os.listdir(top)


def default_listing(top):
    """清单目录：S2A_ROMFS_WALK（off = 不落盘）> <产物根的上一级>/.romfs-walk（产物根 = top 的上一级，同 direct_profile_stage.py 缺省）。
    只给名为 romfs 的目录落盘：追加内容 aoc/<序号> 等别的目录的上两级是产物根本身（缺省位置会写进产物根里），标签也会各款撞名，不落盘（照常现读）"""
    if os.path.basename(os.path.normpath(os.path.abspath(top))) != "romfs":
        return None
    env = os.environ.get("S2A_ROMFS_WALK")
    if env:
        return None if env == "off" else Path(env)
    return Path(os.path.abspath(top)).parent.resolve().parent / ".romfs-walk"


def _listing_path(listing, top, identity):
    tag = Path(os.path.abspath(top)).parent.name.split("@")[0] or "romfs"
    return Path(listing) / f"{tag}-{identity}.json"


def _save(path, tree):
    rows = []
    for e in tree.entries:
        st = e.lstat
        rows.append([e.rel, e.kind, e.size if e.size is not None else st.st_size, st.st_mtime_ns,
                     e.head.hex() if e.head is not None else None,
                     [e.error.errno, e.error.strerror, e.error.filename] if e.error is not None else None])
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".walk-", dir=path.parent)
        try:
            # ensure_ascii：非 UTF-8 文件名的代理字符原样往返
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                json.dump({"format": FORMAT, "identity": tree.identity, "head": HEAD, "entries": rows}, f, separators=(",", ":"))
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        tag = path.name[:-len(tree.identity) - 6]
        mine = re.compile(re.escape(tag) + r"-[0-9a-f]{32}\.json")
        old = sorted((p for p in path.parent.iterdir() if p != path and mine.fullmatch(p.name)),
                     key=lambda p: p.stat().st_mtime, reverse=True)
        for p in old[KEEP - 1:]:
            p.unlink(missing_ok=True)
    except OSError:
        # 清单只是提速：写不了照常出结果
        pass


def _load(path, tree):
    """清单里的（大小、头、读错误）填进本次盘点的文件条目；对不上返回 False（调用方重读）"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not (isinstance(data, dict) and data.get("format") == FORMAT and data.get("identity") == tree.identity
                and data.get("head") == HEAD):
            return False
        rows = {r[0]: r for r in data["entries"]}
        if len(rows) != len(tree.entries):
            return False
        filled = []
        for e in tree.entries:
            rel, kind, size, mtime, head, error = rows[e.rel]
            if kind != e.kind:
                return False
            if e.file:
                filled.append((e, size if head is not None else None, bytes.fromhex(head) if head is not None else None,
                               OSError(*error) if error is not None else None))
        for e, size, head, error in filled:
            e.size, e.head, e.error = size, head, error
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _read_heads(entries):
    for e in entries:
        try:
            with open(e.path, "rb") as f:
                e.head = f.read(HEAD)
                e.size = os.fstat(f.fileno()).st_size
        except OSError as err:
            e.error = err


def heads(top, listing=_AUTO, tree=None):
    """盘点 + 每个普通文件的（大小、前 32 字节）：有对得上身份的清单就从清单取（不读盘），否则并行读头并写清单。
    listing：清单目录；缺省见 default_listing；None = 不落盘（只在本次调用里用）。tree：已盘点的结果（同一进程里复用，调用方保证期间没改）"""
    tree = tree if tree is not None else scan(top)
    if tree.complete:
        return tree
    if listing is _AUTO:
        listing = default_listing(top)
    path = _listing_path(listing, top, tree.identity) if listing is not None and tree.identity else None
    # 本标签没有：同目录下别的标签同身份的清单也认（身份 = 相对路径 + 类型 + 大小 + 修改时间纳秒，同身份即同一批文件；
    #   11-06 出包速度线：解包目录读过的头，播种搬进产物根后 caps 再冷读一遍，王国之泪 30.5 万个文件Windows 出包机约 119 秒）
    alt = sorted(p for p in Path(listing).glob(f"*-{tree.identity}.json") if p != path) if path and not path.exists() and Path(listing).is_dir() else []
    if not (path and (_load(path, tree) or any(_load(p, tree) for p in alt))):
        files = tree.files()
        # 读头是磁盘随机读（外置 SSD 单线程约 1.8k 个/秒），线程多只排队等盘、几乎不占核：16 路约 8k 个/秒（11-03 火纹实测，32 路只再快约 5%）
        workers = max(1, min(16, 2 * (os.cpu_count() or 1)))
        chunks = [files[i::workers * 8] for i in range(workers * 8)]
        with ThreadPoolExecutor(workers) as pool:
            list(pool.map(_read_heads, chunks))
        if path:
            _save(path, tree)
    tree.complete = True
    return tree


def main(argv):
    if len(argv) >= 3 and argv[0] == "has-ext":
        exts = tuple(x.lower() for x in argv[2:])
        return 0 if any(e.name.lower().endswith(exts) for e in scan(argv[1]).entries) else 1
    if len(argv) == 2 and argv[0] == "files0":
        tree = scan(argv[1])
        out = sys.stdout.buffer
        for e in tree.entries:
            if e.lstat is not None and stat.S_ISREG(e.lstat.st_mode):
                out.write(os.fsencode(tree.top) + b"/" + os.fsencode(e.rel) + b"\0")
        bad = [(rel, err) for rel, err in tree.errors.items()] + [(e.rel, e.lstat_error) for e in tree.entries if e.lstat_error is not None]
        if bad:
            rel, err = bad[0]
            print(f"romfs_list.py files0：读不开 {rel or tree.top}：{err}", file=sys.stderr)
            return 1
        return 0
    if len(argv) == 3 and argv[0] == "sizes":
        tree = scan(argv[1])
        bad = [(rel, err) for rel, err in tree.errors.items()] + [(e.rel, e.lstat_error) for e in tree.entries if e.lstat_error is not None]
        if bad:
            rel, err = bad[0]
            print(f"romfs_list.py sizes：读不开 {rel or tree.top}：{err}", file=sys.stderr)
            return 1
        with open(argv[2], "w", encoding="utf-8", errors="surrogateescape", newline="\n") as f:
            for e in tree.entries:
                if stat.S_ISREG(e.lstat.st_mode):
                    f.write(f"{e.lstat.st_size}\t{e.rel}\n")
        return 0
    print(__doc__.split("用法（给 shell 阶段）：")[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
