"""Frozen command format of the FPGA GNSS synthesis engine.

All records are streams of little-endian uint32 words.  Field bit positions
are given inside a record viewed as one little-endian integer (bit 0 = LSB of
word 0); per-word positions follow directly.

Record header word (first word of every record)
    [31:24] magic   = 0xA5   (CMD_MAGIC = 0xA5000000 is the magic-only word)
    [23:16] type    = 1 CFG, 2 BLOCK
    [15:0]  length  = total record length in words, header included

CFG record (6 words)
    w0 header (length 6)
    w1 nch          channels in use (0..MAX_CH)
    w2 fs_hz        sample rate, Hz (uint32)
    w3 shift        output right-shift added to the fixed one (uint32)
    w4 start_block  low 32 bits
    w5 start_block  high 32 bits

BLOCK record (4 + 10*nch words)
    w0 header (length 4 + 10*nch)
    w1 block_idx low 32 bits   (absolute block index, block k = samples
    w2 block_idx high 32 bits   [k*BLOCK, (k+1)*BLOCK) after the start)
    w3 nch
    then nch channel records of exactly 10 words (40 bytes) each.

Channel record (10 words = 320 bits, no spare bits)
    word  bits     field
    w0    [5:0]    ch            channel index 0..63
    w0    [11:6]   prn           0..63
    w0    [13:12]  kind          0 CA, 1 E1B, 2 E1C, 3 B1I
    w0    [14]     valid
    w0    [15]     boc           0/1; boc=0 -> plain code, cboc is ignored
                                 and coef[0] is the amplitude
    w0    [17:16]  cboc          0 -> 0 (plain BOC, coef[0]), 1 -> +1, 2 -> -1
                                 (CBOC phase sign; 3 illegal).  Only used when
                                 boc=1
    w0    [22:18]  sign_idx0     starting bit index (0..31) into sign_bits.
                                 The bit used at a sample is
                                 (sign_idx0 + sign_phase // (code_len << 40))
                                 mod 32, where sign_phase is sign_phase0 +
                                 k*code_inc for sample k of the block and
                                 code_len is fixed by kind (C/A 1023,
                                 E1B/E1C 4092, B1I 2046).  The host (cmdgen)
                                 always sends sign_idx0 = 0.
    w0    [31:23]  code_inc[8:0]
    w1    [31:0]   carr_inc      int32, two's complement, units 2**-32 cycle
    w2    [31:0]   carr_phase0   uint32
    w3    [31:0]   sign_bits     bit i = sign of period i; bit value 1 means
                                 +1 chip/polarity, 0 means -1
    w4    [31:0]   code_inc[40:9]   (code_inc is 43 bits: 3 integer + 40
    w5    [1:0]    code_inc[42:41]   fractional bits, chips per sample)
    w5    [31:2]   code_phase0[29:0]   (52 bits = 12 int + 40 frac chips,
    w6    [21:0]   code_phase0[51:30]    value < code_len << 40)
    w6    [31:22]  sign_phase0[9:0]    (52 bits, same format)
    w7    [31:0]   sign_phase0[41:10]
    w8    [9:0]    sign_phase0[51:42]
    w8    [27:10]  coef[0]   plain,   signed 18 bit, 6 fractional bits
    w8    [31:28]  coef[1][3:0]       g*(A+B)
    w9    [13:0]   coef[1][17:4]
    w9    [31:14]  coef[2]            g*(A-B)

Exact bit offsets are the table _CH_FIELDS below (authoritative).

code_len is not transmitted: it is fixed by kind (CA 1023, E1B/E1C 4092,
B1I 2046).  The PL datapath keeps 56-bit phase registers
(widths.CODE_PHASE_BITS); the wire carries the narrower fields because
three 56-bit fields plus the rest do not fit the 40-byte budget.

Resync: a reader scans word by word for a header with the magic byte, a known
type, a plausible length and, for BLOCK, nch consistent with length; anything
else is skipped one word at a time.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from backend.fpga import widths as W

MAGIC_BYTE = 0xA5
CMD_MAGIC = MAGIC_BYTE << 24
TYPE_CFG = 1
TYPE_BLOCK = 2
CFG_WORDS = 6
BLOCK_HDR_WORDS = 4
CH_WORDS = 10

KIND_CA, KIND_E1B, KIND_E1C, KIND_B1I = 0, 1, 2, 3
CODE_LEN = {KIND_CA: 1023, KIND_E1B: 4092, KIND_E1C: 4092, KIND_B1I: 2046}

# name: (bit offset, width); code_inc is split in two pieces.
_CH_FIELDS = {
    "ch": (0, 6), "prn": (6, 6), "kind": (12, 2), "valid": (14, 1),
    "boc": (15, 1), "cboc": (16, 2), "sign_idx0": (18, 5),
    "code_inc_lo": (23, 9), "carr_inc": (32, 32), "carr_phase0": (64, 32),
    "sign_bits": (96, 32), "code_inc_hi": (128, 34),
    "code_phase0": (162, W.CODE_PHASE_WIRE_BITS),
    "sign_phase0": (214, W.CODE_PHASE_WIRE_BITS),
    "coef0": (266, W.COEF_BITS), "coef1": (284, W.COEF_BITS),
    "coef2": (302, W.COEF_BITS),
}
assert _CH_FIELDS["coef2"][0] + W.COEF_BITS == CH_WORDS * 32
assert 9 + 34 == W.CODE_INC_WIRE_BITS


@dataclass
class ChannelBlock:
    ch: int
    prn: int
    kind: int
    code_len: int
    valid: bool
    carr_inc: int
    carr_phase0: int
    code_inc: int
    code_phase0: int
    sign_phase0: int
    sign_idx0: int
    sign_bits: int
    boc: int
    cboc: int
    coef: tuple


@dataclass
class CfgCommand:
    nch: int
    fs_hz: int
    shift: int
    start_block: int


def _chk(name, v, lo, hi):
    if not lo <= v <= hi:
        raise ValueError(f"{name}={v} outside [{lo}, {hi}]")
    return int(v)


def _hdr(typ, nwords):
    return CMD_MAGIC | (typ << 16) | nwords


def pack_cfg(c: CfgCommand) -> bytes:
    _chk("nch", c.nch, 0, W.MAX_CH)
    _chk("fs_hz", c.fs_hz, 0, 2**32 - 1)
    _chk("shift", c.shift, 0, 2**32 - 1)
    _chk("start_block", c.start_block, 0, 2**64 - 1)
    return struct.pack("<6I", _hdr(TYPE_CFG, CFG_WORDS), c.nch, c.fs_hz,
                       c.shift, c.start_block & 0xFFFFFFFF, c.start_block >> 32)


def _pack_ch(c: ChannelBlock) -> bytes:
    if c.kind not in CODE_LEN:
        raise ValueError(f"bad kind {c.kind}")
    if c.code_len != CODE_LEN[c.kind]:
        raise ValueError(f"code_len {c.code_len} != {CODE_LEN[c.kind]} for kind {c.kind}")
    lim = c.code_len << W.CODE_FRAC_BITS
    cb = {0: 0, 1: 1, -1: 2}.get(c.cboc)
    if cb is None:
        raise ValueError(f"bad cboc {c.cboc}")
    v = {
        "ch": _chk("ch", c.ch, 0, W.MAX_CH - 1),
        "prn": _chk("prn", c.prn, 0, 63),
        "kind": c.kind,
        "valid": int(bool(c.valid)),
        "boc": _chk("boc", c.boc, 0, 1),
        "cboc": cb,
        "sign_idx0": _chk("sign_idx0", c.sign_idx0, 0, W.SIGN_BITS - 1),
        "carr_inc": _chk("carr_inc", c.carr_inc, -2**31, 2**31 - 1) & 0xFFFFFFFF,
        "carr_phase0": _chk("carr_phase0", c.carr_phase0, 0, 2**32 - 1),
        "sign_bits": _chk("sign_bits", c.sign_bits, 0, 2**32 - 1),
        "code_phase0": _chk("code_phase0", c.code_phase0, 0, lim - 1),
        "sign_phase0": _chk("sign_phase0", c.sign_phase0, 0, lim - 1),
    }
    ci = _chk("code_inc", c.code_inc, 0, (1 << W.CODE_INC_WIRE_BITS) - 1)
    v["code_inc_lo"] = ci & 0x1FF
    v["code_inc_hi"] = ci >> 9
    cmax = 1 << (W.COEF_BITS - 1)
    for i in range(3):
        v[f"coef{i}"] = _chk(f"coef[{i}]", c.coef[i], -cmax, cmax - 1) & ((1 << W.COEF_BITS) - 1)
    acc = 0
    for name, (off, wid) in _CH_FIELDS.items():
        acc |= v[name] << off
    return acc.to_bytes(CH_WORDS * 4, "little")


def _unpack_ch(raw: bytes) -> ChannelBlock:
    acc = int.from_bytes(raw, "little")

    def f(name):
        off, wid = _CH_FIELDS[name]
        return (acc >> off) & ((1 << wid) - 1)

    def sf(name):
        x = f(name)
        wid = _CH_FIELDS[name][1]
        return x - (1 << wid) if x >> (wid - 1) else x

    kind = f("kind")
    cboc = {0: 0, 1: 1, 2: -1}.get(f("cboc"))
    if cboc is None:
        raise ValueError("illegal cboc code 3")
    return ChannelBlock(
        ch=f("ch"), prn=f("prn"), kind=kind, code_len=CODE_LEN[kind],
        valid=bool(f("valid")), carr_inc=sf("carr_inc"),
        carr_phase0=f("carr_phase0"),
        code_inc=f("code_inc_lo") | (f("code_inc_hi") << 9),
        code_phase0=f("code_phase0"), sign_phase0=f("sign_phase0"),
        sign_idx0=f("sign_idx0"), sign_bits=f("sign_bits"),
        boc=f("boc"), cboc=cboc, coef=(sf("coef0"), sf("coef1"), sf("coef2")))


def pack_block(block_idx: int, chans) -> bytes:
    _chk("block_idx", block_idx, 0, 2**64 - 1)
    _chk("nch", len(chans), 0, W.MAX_CH)
    n = len(chans)
    head = struct.pack("<4I", _hdr(TYPE_BLOCK, BLOCK_HDR_WORDS + CH_WORDS * n),
                       block_idx & 0xFFFFFFFF, block_idx >> 32, n)
    return head + b"".join(_pack_ch(c) for c in chans)


def unpack_stream(data: bytes) -> list:
    """Decode records; returns CfgCommand and (block_idx, [ChannelBlock]) items.

    Words that do not start a valid record are skipped (resync).
    """
    nw = len(data) // 4
    words = struct.unpack(f"<{nw}I", data[:nw * 4])
    out = []
    i = 0
    while i < nw:
        h = words[i]
        typ, ln = (h >> 16) & 0xFF, h & 0xFFFF
        if (h >> 24) != MAGIC_BYTE or i + ln > nw:
            i += 1
            continue
        if typ == TYPE_CFG and ln == CFG_WORDS and words[i + 1] <= W.MAX_CH:
            out.append(CfgCommand(words[i + 1], words[i + 2], words[i + 3],
                                  words[i + 4] | (words[i + 5] << 32)))
            i += ln
        elif (typ == TYPE_BLOCK and ln >= BLOCK_HDR_WORDS
              and words[i + 3] <= W.MAX_CH
              and ln == BLOCK_HDR_WORDS + CH_WORDS * words[i + 3]):
            n = words[i + 3]
            base = i + BLOCK_HDR_WORDS
            try:
                chans = [_unpack_ch(struct.pack(
                    f"<{CH_WORDS}I",
                    *words[base + CH_WORDS * k: base + CH_WORDS * (k + 1)]))
                    for k in range(n)]
            except ValueError:
                i += 1
                continue
            out.append((words[i + 1] | (words[i + 2] << 32), chans))
            i += ln
        else:
            i += 1
    return out
