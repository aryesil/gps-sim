"""OSNMA bit helpers, TESLA step, HMAC tag and MACSEQ builders."""
from __future__ import annotations

import hashlib
import hmac

_WEEK_S = 604800


def bits_from_int(v: int, n: int) -> list[int]:
    return [(v >> (n - 1 - i)) & 1 for i in range(n)]


def int_from_bits(b: list[int]) -> int:
    v = 0
    for x in b:
        v = (v << 1) | x
    return v


def bytes_from_bits(b: list[int]) -> bytes:
    b = list(b) + [0] * ((-len(b)) % 8)
    return bytes(int_from_bits(b[i:i + 8]) for i in range(0, len(b), 8))


def bits_from_bytes(b: bytes, n: int | None = None) -> list[int]:
    out = [(byte >> (7 - k)) & 1 for byte in b for k in range(8)]
    return out if n is None else out[:n]


def gst32(gst_abs: int) -> int:
    return ((gst_abs // _WEEK_S) % 4096) << 20 | (gst_abs % _WEEK_S)


def hash_fn(hf: int):
    if hf == 0:
        return hashlib.sha256
    if hf == 2:
        return hashlib.sha3_256
    raise ValueError(f"unsupported HF {hf}")


def tesla_step(key: bytes, gst_sf_prev: int, alpha: bytes, hf: int = 0) -> bytes:
    h = hash_fn(hf)(key + gst32(gst_sf_prev).to_bytes(4, "big") + alpha).digest()
    return h[:len(key)]


def mac(key: bytes, msg_bits: list[int], mf: int = 0) -> bytes:
    if mf != 0:
        raise NotImplementedError("only HMAC-SHA-256 (MF=0) is supported")
    return hmac.new(key, bytes_from_bits(msg_bits), hashlib.sha256).digest()


def tag_message(prn_d: int | None, prn_a: int, gst_sf: int, ctr: int, nmas: int,
                navdata: list[int]) -> list[int]:
    m = [] if prn_d is None else bits_from_int(prn_d, 8)
    m += bits_from_int(prn_a, 8) + bits_from_int(gst32(gst_sf), 32)
    m += bits_from_int(ctr, 8) + bits_from_int(nmas, 2) + list(navdata)
    return m


def tag(key: bytes, prn_d: int | None, prn_a: int, gst_sf: int, ctr: int, nmas: int,
        navdata: list[int], ts_bits: int, mf: int = 0) -> int:
    d = mac(key, tag_message(prn_d, prn_a, gst_sf, ctr, nmas, navdata), mf)
    return int_from_bits(bits_from_bytes(d, ts_bits))


def macseq(key: bytes, prn_a: int, gst_sf: int, flx_infos: list[int] = (),
           mf: int = 0) -> int:
    m = bits_from_int(prn_a, 8) + bits_from_int(gst32(gst_sf), 32)
    for t in flx_infos:
        m += bits_from_int(t, 16)
    return int_from_bits(bits_from_bytes(mac(key, m, mf), 12))
