"""Ed25519 signatures (RFC 8032), for checking that a core release is ttycat's. See release.py.

The standard library has no Ed25519, and the app's Python carries nothing else, so this is the
RFC's own reference algorithm. Checking only handles public data. Signing runs once per release,
on a machine nobody else is timing, so it doesn't need to be constant-time either.
"""
from __future__ import annotations

import hashlib

P = 2**255 - 19
L = 2**252 + 27742317777372353535851937790883648493  # the order of the base point
D = -121665 * pow(121666, P - 2, P) % P
SQRT_M1 = pow(2, (P - 1) // 4, P)


def _hash(data: bytes) -> int:
    return int.from_bytes(hashlib.sha512(data).digest(), "little")


def _add(a, b):
    """Points in extended coordinates: x = X/Z, y = Y/Z, x*y = T/Z."""
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    pa = (y1 - x1) * (y2 - x2) % P
    pb = (y1 + x1) * (y2 + x2) % P
    pc = t1 * 2 * D * t2 % P
    pd = z1 * 2 * z2 % P
    e, f, g, h = pb - pa, pd - pc, pd + pc, pb + pa
    return (e * f % P, g * h % P, f * g % P, e * h % P)


def _times(n: int, point):
    out = (0, 1, 1, 0)
    while n > 0:
        if n & 1:
            out = _add(out, point)
        point = _add(point, point)
        n >>= 1
    return out


def _same(a, b) -> bool:
    return (a[0] * b[2] - b[0] * a[2]) % P == 0 and (a[1] * b[2] - b[1] * a[2]) % P == 0


def _x_for(y: int, sign: int) -> int | None:
    if y >= P:
        return None
    xx = (y * y - 1) * pow(D * y * y + 1, P - 2, P) % P
    if xx == 0:
        return None if sign else 0
    x = pow(xx, (P + 3) // 8, P)
    if (x * x - xx) % P:
        x = x * SQRT_M1 % P
    if (x * x - xx) % P:
        return None
    return P - x if (x & 1) != sign else x


_GY = 4 * pow(5, P - 2, P) % P
_GX = _x_for(_GY, 0)
BASE = (_GX, _GY, 1, _GX * _GY % P)


def _encode(point) -> bytes:
    x, y, z, _ = point
    zi = pow(z, P - 2, P)
    x, y = x * zi % P, y * zi % P
    return (y | (x & 1) << 255).to_bytes(32, "little")


def _decode(data: bytes):
    if len(data) != 32:
        return None
    y = int.from_bytes(data, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = _x_for(y, sign)
    return None if x is None else (x, y, 1, x * y % P)


def _expand(secret: bytes) -> tuple[int, bytes]:
    if len(secret) != 32:
        raise ValueError("an Ed25519 secret key is 32 bytes")
    h = hashlib.sha512(secret).digest()
    a = int.from_bytes(h[:32], "little") & ((1 << 254) - 8) | (1 << 254)
    return a, h[32:]


def public_key(secret: bytes) -> bytes:
    return _encode(_times(_expand(secret)[0], BASE))


def sign(secret: bytes, message: bytes) -> bytes:
    a, prefix = _expand(secret)
    public = _encode(_times(a, BASE))
    r = _hash(prefix + message) % L
    big_r = _encode(_times(r, BASE))
    s = (r + _hash(big_r + public + message) % L * a) % L
    return big_r + s.to_bytes(32, "little")


def verify(public: bytes, message: bytes, signature: bytes) -> bool:
    """True only for a signature made with the secret half of `public`, over exactly `message`."""
    if len(public) != 32 or len(signature) != 64:
        return False
    a = _decode(public)
    r = _decode(signature[:32])
    if a is None or r is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= L:
        return False
    h = _hash(signature[:32] + public + message) % L
    return _same(_times(s, BASE), _add(r, _times(h, a)))
