"""Pure-Python NIST P-256 ECDSA (RFC 6979 deterministic signing), SEC1 codec, PEM."""
from __future__ import annotations

import base64
import hashlib
import hmac

P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
G = (0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
     0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5)

_OID_EC_PUBKEY = bytes.fromhex("06072a8648ce3d0201")
_OID_P256 = bytes.fromhex("06082a8648ce3d030107")


def _dbl(p):
    x, y, z = p
    if not y:
        return (0, 1, 0)
    zz = z * z % P
    m = 3 * (x - zz) * (x + zz) % P
    s = 4 * x * y * y % P
    x3 = (m * m - 2 * s) % P
    y3 = (m * (s - x3) - 8 * pow(y, 4, P)) % P
    return (x3, y3, 2 * y * z % P)


def _add(p, q):
    if not p[2]:
        return q
    if not q[2]:
        return p
    x1, y1, z1 = p
    x2, y2, z2 = q
    z1z1 = z1 * z1 % P
    z2z2 = z2 * z2 % P
    u1 = x1 * z2z2 % P
    u2 = x2 * z1z1 % P
    s1 = y1 * z2 * z2z2 % P
    s2 = y2 * z1 * z1z1 % P
    if u1 == u2:
        return _dbl(p) if s1 == s2 else (0, 1, 0)
    h = (u2 - u1) % P
    r = (s2 - s1) % P
    hh = h * h % P
    hhh = h * hh % P
    v = u1 * hh % P
    x3 = (r * r - hhh - 2 * v) % P
    y3 = (r * (v - x3) - s1 * hhh) % P
    return (x3, y3, h * z1 * z2 % P)


def _mul(k, pt):
    r = (0, 1, 0)
    q = (pt[0], pt[1], 1)
    while k:
        if k & 1:
            r = _add(r, q)
        q = _dbl(q)
        k >>= 1
    return r


def _affine(p):
    if not p[2]:
        raise ValueError("point at infinity")
    zi = pow(p[2], -1, P)
    zi2 = zi * zi % P
    return (p[0] * zi2 % P, p[1] * zi2 * zi % P)


def pubkey(d: int) -> tuple[int, int]:
    if not 1 <= d < N:
        raise ValueError("private key out of range")
    return _affine(_mul(d, G))


def _k_rfc6979(d: int, h1: bytes) -> int:
    x = d.to_bytes(32, "big")
    h = (int.from_bytes(h1, "big") % N).to_bytes(32, "big")
    v = b"\x01" * 32
    k = b"\x00" * 32
    k = hmac.new(k, v + b"\x00" + x + h, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + x + h, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        cand = int.from_bytes(v, "big")
        if 1 <= cand < N:
            return cand
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


def sign(d: int, msg: bytes) -> bytes:
    h1 = hashlib.sha256(msg).digest()
    e = int.from_bytes(h1, "big")
    k = _k_rfc6979(d, h1)
    while True:
        r = _affine(_mul(k, G))[0] % N
        s = pow(k, -1, N) * (e + r * d) % N
        if r and s:
            return r.to_bytes(32, "big") + s.to_bytes(32, "big")
        k = _k_rfc6979(d, h1 + b"\x00")


def verify(pub: tuple[int, int], msg: bytes, sig: bytes) -> bool:
    if len(sig) != 64:
        return False
    r = int.from_bytes(sig[:32], "big")
    s = int.from_bytes(sig[32:], "big")
    if not (1 <= r < N and 1 <= s < N):
        return False
    x, y = pub
    if (y * y - x * x * x + 3 * x - B) % P:
        return False
    e = int.from_bytes(hashlib.sha256(msg).digest(), "big")
    w = pow(s, -1, N)
    pt = _add(_mul(e * w % N, G), _mul(r * w % N, pub))
    if not pt[2]:
        return False
    zz = pt[2] * pt[2] % P
    return pt[0] == r * zz % P or (r + N < P and pt[0] == (r + N) * zz % P)


def compress(pub: tuple[int, int]) -> bytes:
    return bytes([2 + (pub[1] & 1)]) + pub[0].to_bytes(32, "big")


def decompress(b: bytes) -> tuple[int, int]:
    if len(b) != 33 or b[0] not in (2, 3):
        raise ValueError("bad compressed point")
    x = int.from_bytes(b[1:], "big")
    if x >= P:
        raise ValueError("x out of range")
    y = pow((x * x * x - 3 * x + B) % P, (P + 1) // 4, P)
    if y * y % P != (x * x * x - 3 * x + B) % P:
        raise ValueError("point not on curve")
    if (y & 1) != (b[0] & 1):
        y = P - y
    return (x, y)


def _tlv(tag: int, body: bytes) -> bytes:
    n = len(body)
    ln = bytes([n]) if n < 128 else bytes([0x81, n])
    return bytes([tag]) + ln + body


def _pem(label: str, der: bytes) -> str:
    b64 = base64.b64encode(der).decode()
    lines = [b64[i:i + 64] for i in range(0, len(b64), 64)]
    return f"-----BEGIN {label}-----\n" + "\n".join(lines) + f"\n-----END {label}-----\n"


def _point(pub) -> bytes:
    return b"\x04" + pub[0].to_bytes(32, "big") + pub[1].to_bytes(32, "big")


def pem_public(pub: tuple[int, int]) -> str:
    alg = _tlv(0x30, _OID_EC_PUBKEY + _OID_P256)
    bits = _tlv(0x03, b"\x00" + _point(pub))
    return _pem("PUBLIC KEY", _tlv(0x30, alg + bits))


def pem_private(d: int) -> str:
    body = (_tlv(0x02, b"\x01") + _tlv(0x04, d.to_bytes(32, "big"))
            + _tlv(0xA0, _OID_P256)
            + _tlv(0xA1, _tlv(0x03, b"\x00" + _point(pubkey(d)))))
    return _pem("EC PRIVATE KEY", _tlv(0x30, body))
