"""Galileo E1-B page splitting, subframe indexing and OSNMA test-vector loaders."""
from __future__ import annotations

from .crypto import bits_from_bytes

_WEEK_S = 604800


def split_page(b240: list[int]) -> tuple[list[int], list[int]]:
    return b240[2:114] + b240[122:138], b240[138:178]


def subframe_of(tow: int) -> tuple[int, int]:
    p = ((tow - 1) % 30) // 2
    return tow - 1 - 2 * p, p


def load_icd_csv(path, wn: int, tow0: int) -> dict[tuple[int, int], list[int]]:
    out: dict[tuple[int, int], list[int]] = {}
    t0 = wn * _WEEK_S + tow0
    with open(path, encoding="utf-8") as f:
        next(f)
        for line in f:
            line = line.strip()
            if not line:
                continue
            svid, nbits, hexs = line.split(",")
            bits = bits_from_bytes(bytes.fromhex(hexs), int(nbits))
            for k in range(len(bits) // 240):
                out[(int(svid), t0 + 2 * k)] = bits[240 * k:240 * k + 240]
    return out


def load_fixed_csv(path) -> dict[tuple[int, int], list[int]]:
    out: dict[tuple[int, int], list[int]] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            tow, wn, svid, hexs = line.split(",")
            out[(int(svid), int(wn) * _WEEK_S + int(tow))] = bits_from_bytes(
                bytes.fromhex(hexs), 240)
    return out
