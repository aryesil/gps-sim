"""ROM images for the FPGA GNSS synthesizer: spreading codes and sin/cos table.

Code ROM bit mapping: bit value 1 = chip +1, 0 = chip -1 (the engine int8
buffers use +1/-1, so ROM bit = (chip > 0)). Chip 0 sits in bit 0 of word 0;
chip k in bit (k % 32) of word k // 32. Unused tail bits of the last word are 0.

IS-GPS-200 notation: the ICD tabulates C/A chips as register bits, where
logic 1 corresponds to chip -1 in the native generator. So GPS PRN 1 starts
1100100000 in ICD bits, which is ROM bits 0011011111.

PRN ranges: GPS C/A 1..32, SBAS L1 (same C/A code family, PRN 120..150) at
ROM index 33..63 of the C/A kind (see ``ca_index``), Galileo E1B and E1C 1..50 (the native generator
accepts up to 50; the engine plan does use PRNs above 36), BeiDou B1I 1..63.
"""
from __future__ import annotations

import math
import pathlib

import numpy as np

from backend.synth import _lib

KIND_CA = 0
KIND_E1B = 1
KIND_E1C = 2
KIND_B1I = 3

# kind -> (native synth_code system id, code length in chips, prn_max)
_KINDS = {
    KIND_CA: (0, 1023, 63),
    KIND_E1B: (5, 4092, 50),
    KIND_E1C: (6, 4092, 50),
    KIND_B1I: (3, 2046, 63),
}

QUARTER = 1024
FULL = 4096
AMPLITUDE = 32767


SBAS_PRN_FIRST = 120           # SBAS L1 PRN 120..150 -> C/A ROM index 33..63
SBAS_IDX_FIRST = 33
SBAS_PRN_LAST = SBAS_PRN_FIRST + 63 - SBAS_IDX_FIRST


def ca_index(sys_code: int, prn: int) -> int:
    """C/A ROM index (the PRN field of a command record) of an engine SV:
    GPS (sys 0) PRN 1..32 as is, SBAS (sys 2) PRN 120..150 mapped to 33..63."""
    if sys_code == 0 and 1 <= prn <= 32:
        return prn
    if sys_code == 2 and SBAS_PRN_FIRST <= prn <= SBAS_PRN_LAST:
        return prn - SBAS_PRN_FIRST + SBAS_IDX_FIRST
    raise ValueError(f"no C/A ROM code for sys {sys_code} prn {prn}")


def code_len(kind: int) -> int:
    return _KINDS[kind][1]


def _n_words(kind: int) -> int:
    return (_KINDS[kind][1] + 31) // 32


def code_chips(kind: int, prn: int) -> np.ndarray:
    """Primary code as int8 +1/-1 from the native generator."""
    sysc, n, pmax = _KINDS[kind]
    if not 1 <= prn <= pmax:
        raise ValueError(f"PRN {prn} out of range 1..{pmax} for kind {kind}")
    if kind == KIND_CA and prn >= SBAS_IDX_FIRST:
        sysc, prn = 2, prn - SBAS_IDX_FIRST + SBAS_PRN_FIRST       # SBAS L1
    prim, _ = _lib.code(sysc, prn, n, 0)
    return prim


def code_rom_words(kind: int, prn: int) -> np.ndarray:
    chips = code_chips(kind, prn)
    nw = _n_words(kind)
    bits = np.zeros(nw * 32, np.uint32)
    bits[:chips.size] = (chips > 0)
    w = bits.reshape(nw, 32) << np.arange(32, dtype=np.uint32)
    return np.bitwise_or.reduce(w, axis=1).astype(np.uint32)


def rom_layout() -> dict:
    """(kind, prn) -> (base_word, n_words), packed in kind then PRN order."""
    out = {}
    base = 0
    for kind in (KIND_CA, KIND_E1B, KIND_E1C, KIND_B1I):
        nw = _n_words(kind)
        for prn in range(1, _KINDS[kind][2] + 1):
            out[(kind, prn)] = (base, nw)
            base += nw
    return out


def all_rom_words() -> np.ndarray:
    """Whole code ROM image in rom_layout() order."""
    lay = rom_layout()
    img = np.zeros(sum(n for _, n in lay.values()), np.uint32)
    for (kind, prn), (base, nw) in lay.items():
        img[base:base + nw] = code_rom_words(kind, prn)
    return img


def sincos_table() -> tuple:
    """1024-entry quarter-wave (cos, sin), int16 Q1.15, amplitude 32767."""
    a = 2.0 * math.pi * np.arange(QUARTER) / FULL
    c = np.round(AMPLITUDE * np.cos(a)).astype(np.int16)
    s = np.round(AMPLITUDE * np.sin(a)).astype(np.int16)
    return c, s


_TABLE = sincos_table()


def table_value(idx: int) -> tuple:
    """(cos, sin) for a 4096-entry phase index.

    Exact symmetric extension of the quarter wave: with q = idx // 1024 and
    r = idx % 1024, the angle q*90deg + r*step gives (c, s) -> (-s, c) for q=1,
    (-c, -s) for q=2, (s, -c) for q=3, where c, s are the stored entries r.
    """
    idx = int(idx) & (FULL - 1)
    c, s = _TABLE
    q, r = divmod(idx, QUARTER)
    c0, s0 = int(c[r]), int(s[r])
    if q == 0:
        return c0, s0
    if q == 1:
        return -s0, c0
    if q == 2:
        return -c0, -s0
    return s0, -c0


def write_mem(path, words, width: int = 32) -> None:
    """Vivado $readmemh text: one zero-padded hex word per line."""
    digits = (width + 3) // 4
    mask = (1 << width) - 1
    lines = [f"{int(w) & mask:0{digits}x}" for w in np.asarray(words).ravel()]
    pathlib.Path(path).write_text("\n".join(lines) + "\n")
