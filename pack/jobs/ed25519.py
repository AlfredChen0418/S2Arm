"""Ed25519（RFC 8032）的纯 Python 实现，只用标准库：随包 Python 没有 cryptography。

更新器用它验发版清单的签名；签名只在发版机上用（tools/sign_update_manifest.py）。不是常数时间实现，不要用在联网服务上。
"""
import hashlib

P = 2 ** 255 - 19
L = 2 ** 252 + 27742317777372353535851937790883648493
D = -121665 * pow(121666, P - 2, P) % P
I = pow(2, (P - 1) // 4, P)


def _h(data):
    return hashlib.sha512(data).digest()


def _x(y, sign):
    x2 = (y * y - 1) * pow(D * y * y + 1, P - 2, P) % P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P:
        x = x * I % P
    if (x * x - x2) % P:
        return None
    return P - x if x & 1 != sign else x


GY = 4 * pow(5, P - 2, P) % P
G = (_x(GY, 0), GY, 1, _x(GY, 0) * GY % P)
ZERO = (0, 1, 1, 0)


def _add(a, b):
    A = (a[1] - a[0]) * (b[1] - b[0]) % P
    B = (a[1] + a[0]) * (b[1] + b[0]) % P
    C = 2 * a[3] * b[3] * D % P
    E_ = 2 * a[2] * b[2] % P
    e, f, g, h = B - A, E_ - C, E_ + C, B + A
    return (e * f % P, g * h % P, f * g % P, e * h % P)


def _mul(s, point):
    result = ZERO
    while s:
        if s & 1:
            result = _add(result, point)
        point = _add(point, point)
        s >>= 1
    return result


def _equal(a, b):
    return (a[0] * b[2] - b[0] * a[2]) % P == 0 and (a[1] * b[2] - b[1] * a[2]) % P == 0


def _compress(point):
    z = pow(point[2], P - 2, P)
    x, y = point[0] * z % P, point[1] * z % P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(data):
    if len(data) != 32:
        return None
    y = int.from_bytes(data, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= P:
        return None
    x = _x(y, sign)
    return None if x is None else (x, y, 1, x * y % P)


def _expand(secret):
    if len(secret) != 32:
        raise ValueError("Ed25519 私钥必须是 32 字节")
    digest = _h(secret)
    a = int.from_bytes(digest[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, digest[32:]


def public_key(secret):
    return _compress(_mul(_expand(secret)[0], G))


def sign(secret, message):
    a, prefix = _expand(secret)
    public = _compress(_mul(a, G))
    r = int.from_bytes(_h(prefix + message), "little") % L
    big_r = _compress(_mul(r, G))
    k = int.from_bytes(_h(big_r + public + message), "little") % L
    return big_r + int.to_bytes((r + k * a) % L, 32, "little")


def verify(public, message, signature):
    if len(public) != 32 or len(signature) != 64:
        return False
    a, r = _decompress(public), _decompress(signature[:32])
    s = int.from_bytes(signature[32:], "little")
    if a is None or r is None or s >= L:
        return False
    k = int.from_bytes(_h(signature[:32] + public + message), "little") % L
    return _equal(_mul(s, G), _add(r, _mul(k, a)))
