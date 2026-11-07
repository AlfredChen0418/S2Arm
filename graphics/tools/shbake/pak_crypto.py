"""UE PAK AES helpers. Keys stay in memory; no game-specific addresses or keys.

AES comes from the OS: CommonCrypto on macOS (also in the packaged launcher's
environment), bcrypt.dll on Windows (the Windows packing host); no Python package.
The index hash is FPakInfo.IndexHash (SHA-1 of the decrypted index bytes):
https://dev.epicgames.com/documentation/en-us/unreal-engine/API/Runtime/PakFile/FPakInfo
"""
import os, ctypes
import hashlib
import struct

_dec = None   # (密钥, 密文) → 明文：本机系统 AES 的 ECB 解密，首次用时按平台选


def _commoncrypto():
    cc = ctypes.CDLL('/usr/lib/libSystem.B.dylib').CCCrypt
    cc.restype = ctypes.c_int32
    cc.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                   ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p,
                   ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p,
                   ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    def dec(key, data):
        out = ctypes.create_string_buffer(len(data)); n = ctypes.c_size_t()
        if cc(1, 0, 2, key, len(key), None, data, len(data), out, len(data), ctypes.byref(n)):   # 解密、AES、ECB
            raise ValueError('AES 解密失败')
        return out.raw[:n.value]
    return dec


def _bcrypt():   # Windows 出包机（11-05 Windows 出包机普查七龙珠ZERO：pak 索引加密，原先只有 CommonCrypto，Windows 上红「系统 CommonCrypto 不可用」）
    b, V, P, U = ctypes.WinDLL('bcrypt'), ctypes.c_void_p, ctypes.POINTER, ctypes.c_ulong
    for f, args in (('BCryptOpenAlgorithmProvider', [P(V), ctypes.c_wchar_p, ctypes.c_wchar_p, U]),
                    ('BCryptSetProperty', [V, ctypes.c_wchar_p, V, U, U]),
                    ('BCryptGenerateSymmetricKey', [V, P(V), V, U, ctypes.c_char_p, U, U]),
                    ('BCryptDecrypt', [V, ctypes.c_char_p, U, V, V, U, V, U, P(U), U]),
                    ('BCryptDestroyKey', [V])):
        getattr(b, f).restype, getattr(b, f).argtypes = ctypes.c_long, args   # 返回 NTSTATUS，0 = 成功
    alg = V(); mode = ctypes.create_unicode_buffer('ChainingModeECB')
    if b.BCryptOpenAlgorithmProvider(ctypes.byref(alg), 'AES', None, 0) or b.BCryptSetProperty(alg, 'ChainingMode', mode, ctypes.sizeof(mode), 0):
        raise OSError('BCrypt AES-ECB')
    def dec(key, data):
        k = V()
        if b.BCryptGenerateSymmetricKey(alg, ctypes.byref(k), None, 0, key, len(key), 0): raise ValueError('AES 解密失败')
        try:
            out = ctypes.create_string_buffer(len(data)); n = U()
            if b.BCryptDecrypt(k, data, len(data), None, None, 0, out, len(data), ctypes.byref(n), 0): raise ValueError('AES 解密失败')   # 不去填充
            return out.raw[:n.value]
        finally: b.BCryptDestroyKey(k)
    return dec


def aes_ecb(key, data):
    """Decrypt AES-256-ECB without padding removal (PAK sizes carry padding)."""
    global _dec
    if len(key) != 32 or len(data) % 16:
        raise ValueError('AES 密钥长度或块长度无效')
    if _dec is None:
        try:
            _dec = _bcrypt() if os.name == 'nt' else _commoncrypto()
        except (OSError, AttributeError):
            raise ValueError('系统 AES 不可用（macOS CommonCrypto / Windows BCrypt）') from None
    return _dec(key, data)


def module_image(data):
    """Map NSO segments at their memory offsets for ADR(P)/literal resolution."""
    if data[:4] != b'NSO0': return data, 0
    import lz4.block
    image = bytearray(); text_size = 0
    for i, off in enumerate((0x10, 0x20, 0x30)):
        fo, mo, size = struct.unpack_from('<III', data, off)
        packed, = struct.unpack_from('<I', data, 0x60 + 4 * i)
        if fo + packed > len(data) or mo + size > (1 << 30):
            raise ValueError('NSO 段范围无效')
        raw = data[fo:fo + packed]
        if (data[12] >> i) & 1: raw = lz4.block.decompress(raw, uncompressed_size=size)
        if len(raw) != size: raise ValueError('NSO 段长度不符')
        if len(image) < mo + size: image.extend(bytes(mo + size - len(image)))
        image[mo:mo + size] = raw
        if i == 0:
            if mo != 0: raise ValueError('NSO text 起点无效')
            text_size = size
    return bytes(image), text_size


def constructed_keys(image, text_size):
    """Collect literal bytes stored by straight-line ARM64 constant sequences.

    Handles MOVZ/N/K, ADR(P), ADD-immediate, literal/unsigned/pair loads and
    stores (including Q registers). This is candidate generation, not guest
    execution: unsupported instructions cannot authorize a key. A complete
    decrypted index must still pass SHA-1 in find_key(). No SDK names involved.
    """
    regs = [None] * 32; vectors = [None] * 32; stores = {}; seen = set()
    last = -1024
    for pc in range(0, text_size - 3, 4):
        ins, = struct.unpack_from('<I', image, pc)
        wide = (ins & 0x1f800000) == 0x12800000
        adr = (ins & 0x1f000000) == 0x10000000
        add = (ins & 0x7f000000) == 0x11000000
        literal = (ins & 0x3b000000) == 0x18000000
        unsigned = (ins & 0x3b000000) == 0x39000000
        pair = (ins & 0x3a000000) == 0x28000000
        branch = (ins & 0x7c000000) == 0x14000000 or (ins & 0xfe000000) == 0xd6000000
        if not (wide or adr or add or literal or unsigned or pair or branch): continue
        if branch or pc - last > 512:
            regs = [None] * 32; vectors = [None] * 32; stores = {}
        last = pc
        if branch: continue
        rd = ins & 31; base = (ins >> 5) & 31
        if adr:
            imm = ((ins >> 5) & 0x7ffff) << 2 | ((ins >> 29) & 3)
            if imm & (1 << 20): imm -= 1 << 21
            regs[rd] = (pc & ~4095) + (imm << 12) if ins >> 31 else pc + imm
            continue
        if add:
            imm = ((ins >> 10) & 4095) << (12 if ins & (1 << 22) else 0)
            regs[rd] = None if regs[base] is None else regs[base] + imm
            continue
        if wide:
            shift = ((ins >> 21) & 3) * 16; imm = ((ins >> 5) & 65535) << shift
            op = (ins >> 29) & 3; mask = (1 << (64 if ins >> 31 else 32)) - 1
            if op == 2: regs[rd] = imm
            elif op == 0: regs[rd] = ~imm & mask
            elif op == 3 and regs[rd] is not None: regs[rd] = (regs[rd] & ~(65535 << shift) | imm) & mask
            continue
        vector = bool(ins & (1 << 26)); values = vectors if vector else regs
        def load(reg, address, size):
            values[reg] = int.from_bytes(image[address:address + size], 'little') if address is not None and 0 <= address <= len(image) - size else None
        if literal:
            imm = (ins >> 5) & 0x7ffff
            if imm & (1 << 18): imm -= 1 << 19
            opc = ins >> 30; size = (4 << opc) if vector else (8 if opc == 1 else 4)
            load(rd, pc + imm * 4, size); continue
        if unsigned:
            size = 16 if vector and ins & (1 << 23) else 1 << (ins >> 30)
            fields = [(((ins >> 10) & 4095) * size, rd, size)]
        else:
            size = 4 << (ins >> 30) if vector else (8 if ins >> 31 else 4)
            off = (ins >> 15) & 127
            off = (off - 128 if off >= 64 else off) * size
            fields = [(off, rd, size), (off + size, (ins >> 10) & 31, size)]
        if ins & (1 << 22):
            for off, reg, size in fields: load(reg, None if regs[base] is None else regs[base] + off, size)
            continue
        slots = stores.setdefault(base, {})
        for off, reg, size in fields:
            if values[reg] is None: continue
            raw = (values[reg] & ((1 << (8 * size)) - 1)).to_bytes(size, 'little')
            slots.update((off + j, value) for j, value in enumerate(raw))
            for start in (off, off + size - 32):
                if all(start + j in slots for j in range(32)):
                    key = bytes(slots[start + j] for j in range(32))
                    if key not in seen:
                        seen.add(key); yield key


_FOUND = []   # 本进程已验证通过的密钥：一款游戏的几十个 pak 通常共用一把（11-01 出包线：无主之地3 85 个 pak 每个重扫整个 main，91 秒 × 85）


def key_fits(key, index, index_hash):   # 这把密钥解出的索引通过 SHA-1 校验
    n, = struct.unpack_from('<i', aes_ecb(key, index[:16]))
    return 0 < abs(n) <= 65536 and hashlib.sha1(aes_ecb(key, index)).digest() == index_hash


def find_key(image, index, index_hash):
    """Search aligned embedded 32-byte constants; verify against the index hash.

    No entropy heuristic: zero-containing keys and the last window are valid.
    A plausible mount point is only a prefilter; the full SHA-1 must match.
    """
    if len(index) < 16 or len(index) % 16:
        raise ValueError('加密索引长度不是 AES 块的整数倍')
    for key in _FOUND:   # 先试本进程找到过的（同一判据：索引 SHA-1 全符才算）
        if key_fits(key, index, index_hash): return key
    image, text_size = module_image(image)
    # 两路同时：进程池分段扫整个镜像（4 字节对齐的每个窗口），本进程同时试代码构造的候选（解码代码段，无主之地3 119 MB 代码段 15 秒）；谁先命中算谁
    #   （11-01 出包线：原先先构造后扫描、全串行，无主之地3 首个 pak 110 秒；密钥在镜像 79% 处，顺序扫到它 88 秒）
    key = _scan(image, index, index_hash, first=(constructed_keys(image, text_size) if text_size else ()))
    if key is not None: _FOUND.append(key)
    return key


_SCAN = None   # (镜像, 索引, 哈希)：fork 出的扫描子进程共享这一份（写时复制，不把上百 MB 的镜像按任务序列化）


def _scan_range(span):   # 子进程：[a, b) 里起点 4 字节对齐的 32 字节窗口逐个试；→ 命中的密钥或 None（只回传给父进程，不打印）
    image, index, index_hash = _SCAN; a, b = span; previous = None
    for off in range(a, b, 4):
        key = image[off:off + 32]
        if key != previous and key_fits(key, index, index_hash): return key
        previous = key
    return None


def _scan(image, index, index_hash, workers=None, first=()):
    """全镜像回退扫描：按 4 字节对齐切成约 8×路数段，交给 fork 的进程池；本进程同时逐个试 first（代码构造的候选）。任一处命中就停、其余段作废；两路都试完没命中才返回 None。
    能命中的窗口通过整份索引的 SHA-1，正常只有一把密钥：并行与顺序扫描的结果相同。fork 不可用或只给 1 路时本进程先试 first、再顺序扫"""
    global _SCAN
    end = max(0, len(image) - 31)
    workers = workers or int(os.environ.get('S2A_KEYSCAN_J') or 0) or os.cpu_count() or 4
    step = max(4096, -(-end // (workers * 8)) // 4 * 4) if end else 4096
    spans = [(a, min(a + step, end)) for a in range(0, end, step)]
    _SCAN = (image, index, index_hash)
    try:
        if not hasattr(os, 'fork') or workers <= 1 or not spans:
            for key in first:
                if key_fits(key, index, index_hash): return key
            for sp in spans:
                k = _scan_range(sp)
                if k is not None: return k
            return None
        return _scan_forked(spans, workers, first, index, index_hash)
    finally:
        _SCAN = None


def _scan_forked(spans, workers, first, index, index_hash):
    """fork 出 workers 个子进程，第 w 个扫第 w、w+workers、… 段，每扫完一段经管道报一行「段号 密钥十六进制或 -」；本进程边收边试 first。
    父进程不起任何线程，返回前（命中、扫完或异常）一律 SIGKILL 并回收全部子进程。
    11-09 根修：原先用 concurrent.futures.ProcessPoolExecutor（fork 上下文）。它的管理线程已在跑时再 fork，子进程可能卡在 fork 时被别的线程持有的锁上，
      既不领任务也不领退出哨兵；别的子进程把段扫完、结果照对，但解释器退出时 join 这个子进程永等——gate-host 门禁 t_keyscan 打完「失败 0」仍 120 秒超时
      （11-09 00:19、01:58 两次，退出 124）。子进程异常退出（被杀 / 崩溃）时管道读到 EOF，它没报的段由本进程补扫，结果与不出事时相同"""
    import select, signal, sys
    workers = min(workers, len(spans))
    kids, pids = {}, []   # 读端 fd → [没报的段号集合, 半行缓冲]
    try:
        for w in range(workers):
            mine = range(w, len(spans), workers)
            r, wr = os.pipe()
            pid = os.fork()
            if pid == 0:   # 子进程：只算、只写管道，任何情况都 _exit（不跑父进程的 atexit / 缓冲刷新）
                try:
                    os.close(r)
                    for i in mine:
                        k = _scan_range(spans[i])
                        os.write(wr, f'{i} {k.hex() if k is not None else "-"}\n'.encode())
                        if k is not None: break
                finally:
                    os._exit(0)
            os.close(wr); pids.append(pid); kids[r] = [set(mine), b'']
        hit, lost, cand, cand_left = None, [], iter(first), True
        while kids and hit is None:
            ready = select.select(list(kids), [], [], 0 if cand_left else None)[0]
            for fd in ready:
                ent, data = kids[fd], os.read(fd, 65536)
                if not data:   # 子进程结束：正常扫完时集合已空；异常退出时剩下的段记为丢失
                    lost += [spans[i] for i in ent[0]]; os.close(fd); del kids[fd]; continue
                *lines, ent[1] = (ent[1] + data).split(b'\n')
                for ln in lines:
                    i, k = ln.split(); ent[0].discard(int(i))
                    if k != b'-' and hit is None: hit = bytes.fromhex(k.decode())
            if cand_left and hit is None:   # 每轮试一个构造候选；扫描命中才停
                key = next(cand, None)
                if key is None: cand_left = False
                elif key_fits(key, index, index_hash): return key
        if hit is not None: return hit
        for key in cand:   # 扫描扫完没命中：构造候选照样试完（11-02 小小梦魇3：密钥在第 9101 个候选）
            if key_fits(key, index, index_hash): return key
        if lost:
            print(f'【黄】找钥回退扫描：{len(lost)} 段的工作进程异常退出，本进程补扫', file=sys.stderr)
            for sp in sorted(lost):
                k = _scan_range(sp)
                if k is not None: return k
        return None
    finally:
        for fd in kids: os.close(fd)
        for pid in pids:
            try: os.kill(pid, signal.SIGKILL)
            except ProcessLookupError: pass
            try: os.waitpid(pid, 0)
            except ChildProcessError: pass
