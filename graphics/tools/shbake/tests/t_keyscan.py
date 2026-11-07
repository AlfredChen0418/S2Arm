#!/usr/bin/env python3
# 找钥回退扫描并行化单测（graphics/tools/shbake/pak_crypto.py find_key / _scan，11-01 出包线）：约 2 秒，退出 0 = 全过。
# ⓪ 系统 AES 后端过 FIPS-197 AES-256 已知答案（Windows 上同一用例验 BCrypt 那支）。
# 构造（期望手写，不跑被测实现推期望）：随机 32 字节密钥 K，明文索引 = 挂载点串 + 填充到 16 字节整数倍，用 OpenSSL（与被测无关）AES-256-ECB 加密，哈希 = SHA-1(明文)。
#   镜像 = 2 MiB 随机字节，K 埋在 ① 中段（4 字节对齐）② 最后一个窗口（len-32）；③ 镜像里没有 K → None。
#   每例分别用 1 路（本进程顺序扫）与 8 路（fork 进程池）跑，两者都必须找到 K；整个过程的标准输出 / 错误里不能出现 K 的十六进制。
#   ⑥ 镜像里没有 K、K 只在代码构造候选 first 里且排在扫描扫完之后才轮到（first 先慢慢给 3 个假钥）：8 路也必须找到 K（11-02 小小梦魇3 漏钥）。
#   ⑦ 工作进程扫到非首段就异常退出（模拟被杀 / 崩溃；11-04、11-05 gate-host 门禁两次本单测 120 秒超时、日志全空）：8 路仍须在 30 秒内返回——
#      K 在中段 → 找到 K；镜像里没有 K → None。旧实现（multiprocessing.Pool）丢段永等，30 秒闹钟到点判不通
#   ⑧ 上面各例跑完后本进程没有未回收的子进程（11-09 根修：执行器 fork 子进程卡锁，打完「失败 0」退出时永等）
import os, sys, struct, hashlib, subprocess, io, contextlib
H = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, H)
import pak_crypto as P
fail = 0
def ck(ok, msg):
    global fail
    print(('✓ ' if ok else '✗ ') + msg); fail += 0 if ok else 1
# ⓪ 系统 AES 后端（macOS CommonCrypto / Windows BCrypt）对 FIPS-197 附录 C.3 的 AES-256 已知答案：密钥 00..1f，密文 8ea2…6089 → 明文 0011…eeff；两块连解（ECB）各自独立
ck(P.aes_ecb(bytes(range(32)), bytes.fromhex('8ea2b7ca516745bfeafc49904b496089') * 2) == bytes.fromhex('00112233445566778899aabbccddeeff') * 2, '⓪ AES-256 已知答案（FIPS-197 C.3）')
K = os.urandom(32)
plain = struct.pack('<i', 10) + b'../../../\0' + bytes(2)
plain += bytes(-len(plain) % 16)
enc = subprocess.run(['/usr/bin/openssl', 'enc', '-aes-256-ecb', '-K', K.hex(), '-nopad'], input=plain, capture_output=True, check=True).stdout
dig = hashlib.sha1(plain).digest()
out = io.StringIO()
with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
    base = bytearray(os.urandom(2 << 20))
    mid = bytearray(base); a = (len(mid) // 2) & ~3; mid[a:a + 32] = K
    tail = bytearray(base); tail[-32:] = K
    res = {}
    for name, img in (('中段', bytes(mid)), ('末窗口', bytes(tail)), ('没有', bytes(base))):
        for w in (1, 8):
            P._FOUND.clear(); res[(name, w)] = P._scan(img, enc, dig, workers=w)
    import time
    def late():
        for _ in range(3): time.sleep(0.5); yield os.urandom(32)
        yield K
    P._FOUND.clear(); res['late'] = P._scan(bytes(base), enc, dig, workers=8, first=late())
    P._FOUND.clear(); res['find_key'] = P.find_key(bytes(mid), enc, dig)
ck(res[('中段', 1)] == K and res[('中段', 8)] == K, '① 密钥在中段：1 路 / 8 路都找到')
ck(res[('末窗口', 1)] == K and res[('末窗口', 8)] == K, '② 密钥在最后一个窗口：1 路 / 8 路都找到（段尾不漏窗）')
ck(res[('没有', 1)] is None and res[('没有', 8)] is None, '③ 镜像里没有密钥：都返回 None')
ck(res['find_key'] == K and P._FOUND == [K], '④ find_key 走回退扫描找到并记进进程内缓存')
ck(res['late'] == K, '⑥ 扫描先扫完没命中：构造候选照样试完，排在后面的 K 也找到')
ck(K.hex() not in out.getvalue() and K.hex().upper() not in out.getvalue(), '⑤ 输出里没有密钥的十六进制')
import signal
class _Late(Exception): pass
def _alarm(*_): raise _Late()
real_range, parent = P._scan_range, os.getpid()
def dying(span):   # 只在工作进程里、扫到非首段就退出；父进程补扫时照常
    if os.getpid() != parent and span[0] != 0: os._exit(9)
    return real_range(span)
P._scan_range = dying; signal.signal(signal.SIGALRM, _alarm); r7 = {}
try:
    signal.alarm(30)
    with contextlib.redirect_stderr(io.StringIO()):
        for name, img in (('中段', bytes(mid)), ('没有', bytes(base))): P._FOUND.clear(); r7[name] = P._scan(img, enc, dig, workers=8)
except _Late: r7['超时'] = True
finally: signal.alarm(0); P._scan_range = real_range
ck(r7.get('中段') == K and '没有' in r7 and r7['没有'] is None and '超时' not in r7, '⑦ 工作进程异常退出：30 秒内返回，中段找到 K、没有的返回 None（不丢段永等）')
def _kids_left():   # 本进程还有没回收的子进程（活的或僵尸）
    try: os.waitpid(-1, os.WNOHANG); return True
    except ChildProcessError: return False
ck(not _kids_left(), '⑧ 返回后不留子进程：命中、扫完、工作进程异常退出各路都已杀掉并回收（11-09 gate-host 两次打完「失败 0」仍退出 124：退出时 join 卡在 fork 时锁住的子进程）')
print(f't_keyscan：失败 {fail}')
sys.exit(1 if fail else 0)
