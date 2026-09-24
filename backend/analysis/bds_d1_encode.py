"""SP-D: BeiDou B1I D1 / D2 navigation message encoder (BDS-SIS-ICD-B1I-3.0).

* 50 bps (D1, MEO/IGSO) or 500 bps (D2, GEO) NRZ.
* Subframe = 10 words x 30 bits = 300 bits, 6 s (D1) / 0.6 s (D2).
* Word 1 carries 26 information bits: preamble ``11100010010`` (11), Rev (4),
  FraID (3), SOW MSBs (8). The first 15 are sent raw; FraID + SOW MSBs form
  one BCH(15,11,1) codeword.
* Words 2..10 carry 22 information bits each as two BCH(15,11,1) codewords,
  bit-interleaved ``a0 b0 a1 b1 ... a14 b14``. Word 2 starts with the 12
  SOW LSBs.
* BCH(15,11,1): systematic, generator ``g(x) = x^4 + x + 1``.

Over the 224 information bits of a subframe the ICD's fields are contiguous
in broadcast order (a field that straddles a word boundary simply continues
in the next word's information bits), so each subframe / page is built as
one info-bit list and then cut into words. Field order and widths follow
the ICD tables 5-4..5-10 and match RTKLIB's ``decode_bds_d1`` /
``decode_bds_d2`` bit positions (checked in ``tests/test_bds_d1_encode``).

D1: subframes 1-3 carry the broadcast record (with Klobuchar alpha/beta in
subframe 1); subframes 4/5 carry page numbers 1..24 with empty almanac.
D2: subframe 1 pages 1..10 carry the record split across pages as the ICD
does; subframe 5 carries its page number 1..120 with an empty payload,
subframes 2..4 are empty. All
subframes of a D2 frame carry the frame's SOW (3 s grid).
"""
from __future__ import annotations

import math

import numpy as np

PREAMBLE = [1, 1, 1, 0, 0, 0, 1, 0, 0, 1, 0]      # 0x712 >> ... 11 bits
SYM_RATE_D1 = 50.0
SYM_RATE_D2 = 500.0
_SC = math.pi
_BCH_G = 0b10011                                   # x^4 + x + 1


def _f(x) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(v) or math.isinf(v) else v


def bits_of(value: int, n: int) -> list[int]:
    v = int(value) & ((1 << n) - 1)
    return [(v >> (n - 1 - i)) & 1 for i in range(n)]


def twos(value, scale, n) -> list[int]:
    q = int(round(_f(value) / scale))
    lo, hi = -(1 << (n - 1)), (1 << (n - 1)) - 1
    return bits_of(max(lo, min(hi, q)), n)


def uns(value, scale, n) -> list[int]:
    q = int(round(_f(value) / scale))
    return bits_of(max(0, min((1 << n) - 1, q)), n)


# --------------------------------------------------------------------------
# BCH(15, 11, 1)
# --------------------------------------------------------------------------
def bch_encode(info11: list[int]) -> list[int]:
    """Systematic BCH(15,11,1): 11 info bits -> 15 bits (info + 4 parity)."""
    assert len(info11) == 11
    reg = 0
    for b in info11:
        top = (reg >> 3) & 1
        reg = ((reg << 1) & 0xF)
        if top ^ (b & 1):
            reg ^= (_BCH_G & 0xF)
    return list(info11) + bits_of(reg, 4)


def bch_decode(word15: list[int]) -> list[int]:
    """Return the 11 info bits (systematic prefix). Clean channel: parity is
    not used to correct, only the prefix is taken."""
    return list(word15[:11])


def interleave2(a15: list[int], b15: list[int]) -> list[int]:
    out = []
    for i in range(15):
        out.append(a15[i])
        out.append(b15[i])
    return out


def deinterleave2(word30: list[int]) -> tuple[list[int], list[int]]:
    return word30[0::2], word30[1::2]


# --------------------------------------------------------------------------
# subframe assembly
# --------------------------------------------------------------------------
_INFO_BITS = 224          # 26 (word 1) + 9 x 22
_HDR_BITS = 38            # preamble, Rev, FraID, SOW (20)


def _sc(eph, k):
    return _f(eph.get(k)) / _SC


def _toe(eph):
    return uns(eph.get("toe"), 8.0, 17)


def _iono_bits(iono) -> list[int]:
    al, be = (list(iono[0]) + [0.0] * 4)[:4], (list(iono[1]) + [0.0] * 4)[:4]
    b: list[int] = []
    for v, sc in zip(al, (2 ** -30, 2 ** -27, 2 ** -24, 2 ** -24)):
        b += twos(v, sc, 8)
    for v, sc in zip(be, (2 ** 11, 2 ** 14, 2 ** 16, 2 ** 16)):
        b += twos(v, sc, 8)
    return b


def _clock_hdr(eph, week) -> list[int]:
    """SatH1, AODC, URAI, WN, toc, TGD1, TGD2 (common to D1 SF1 and D2 p1)."""
    return (bits_of(int(_f(eph.get("health", 0))) & 1, 1)
            + bits_of(int(_f(eph.get("iodc", 0))) & 0x1F, 5)
            + bits_of(0, 4)
            + bits_of(int(week) & 0x1FFF, 13)
            + uns(eph.get("toc"), 8.0, 17)
            + twos(eph.get("tgd", 0.0), 1e-10, 10)
            + twos(eph.get("tgd2", 0.0), 1e-10, 10))


def _d1_payload(sf_id, eph, week, iono, pnum) -> list[int]:
    """Information bits 38..223 of a D1 subframe (186 bits)."""
    if sf_id == 1:
        return (_clock_hdr(eph, week) + _iono_bits(iono)
                + twos(eph.get("af2"), 2 ** -66, 11)
                + twos(eph.get("af0"), 2 ** -33, 24)
                + twos(eph.get("af1"), 2 ** -50, 22)
                + bits_of(int(_f(eph.get("iode", 0))) & 0x1F, 5))
    if sf_id == 2:
        return (twos(_sc(eph, "delta_n"), 2 ** -43, 16)
                + twos(eph.get("cuc"), 2 ** -31, 18)
                + twos(_sc(eph, "m0"), 2 ** -31, 32)
                + uns(eph.get("e"), 2 ** -33, 32)
                + twos(eph.get("cus"), 2 ** -31, 18)
                + twos(eph.get("crc"), 2 ** -6, 18)
                + twos(eph.get("crs"), 2 ** -6, 18)
                + uns(eph.get("sqrtA"), 2 ** -19, 32)
                + _toe(eph)[:2])
    if sf_id == 3:
        return (_toe(eph)[2:]
                + twos(_sc(eph, "i0"), 2 ** -31, 32)
                + twos(eph.get("cic"), 2 ** -31, 18)
                + twos(_sc(eph, "omega_dot"), 2 ** -43, 24)
                + twos(eph.get("cis"), 2 ** -31, 18)
                + twos(_sc(eph, "idot"), 2 ** -43, 14)
                + twos(_sc(eph, "omega0"), 2 ** -31, 32)
                + twos(_sc(eph, "omega"), 2 ** -31, 32))
    return [0] + bits_of(pnum, 7)                     # SF4/5: Rev, Pnum


def _d2_sf1_page(page, eph, week, iono) -> list[int]:
    """Information bits 38.. of D2 subframe 1 page ``page`` (Pnum first)."""
    a0 = twos(eph.get("af0"), 2 ** -33, 24)
    a1 = twos(eph.get("af1"), 2 ** -50, 22)
    cuc = twos(eph.get("cuc"), 2 ** -31, 18)
    e = uns(eph.get("e"), 2 ** -33, 32)
    cic = twos(eph.get("cic"), 2 ** -31, 18)
    i0 = twos(_sc(eph, "i0"), 2 ** -31, 32)
    omd = twos(_sc(eph, "omega_dot"), 2 ** -43, 24)
    om = twos(_sc(eph, "omega"), 2 ** -31, 32)
    body = {
        1: _clock_hdr(eph, week),
        2: _iono_bits(iono),
        3: [0] * 38 + a0 + a1[:4],
        4: (a1[4:] + twos(eph.get("af2"), 2 ** -66, 11)
            + bits_of(int(_f(eph.get("iode", 0))) & 0x1F, 5)
            + twos(_sc(eph, "delta_n"), 2 ** -43, 16) + cuc[:14]),
        5: (cuc[14:] + twos(_sc(eph, "m0"), 2 ** -31, 32)
            + twos(eph.get("cus"), 2 ** -31, 18) + e[:10]),
        6: e[10:] + uns(eph.get("sqrtA"), 2 ** -19, 32) + cic[:10],
        7: (cic[10:] + twos(eph.get("cis"), 2 ** -31, 18) + _toe(eph)
            + i0[:21]),
        8: (i0[21:] + twos(eph.get("crc"), 2 ** -6, 18)
            + twos(eph.get("crs"), 2 ** -6, 18) + omd[:19]),
        9: omd[19:] + twos(_sc(eph, "omega0"), 2 ** -31, 32) + om[:27],
        10: om[27:] + twos(_sc(eph, "idot"), 2 ** -43, 14),
    }[page]
    return bits_of(page, 4) + body


def build_subframe(sf_id: int, payload: list[int], sow: int) -> list[int]:
    """One 300-bit subframe from its information bits 38..223."""
    sow20 = bits_of(int(sow) % 604800, 20)
    info = (PREAMBLE + [0, 0, 0, 0] + bits_of(sf_id, 3) + sow20
            + (list(payload) + [0] * _INFO_BITS)[:_INFO_BITS - _HDR_BITS])
    assert len(info) == _INFO_BITS
    words = [info[:15] + bch_encode(info[15:26])]
    for w in range(9):
        o = 26 + w * 22
        words.append(interleave2(bch_encode(info[o:o + 11]),
                                 bch_encode(info[o + 11:o + 22])))
    return [bit for wd in words for bit in wd]


def d1_subframe(sf_id, eph, sow, week, iono=((0,) * 4, (0,) * 4)):
    pnum = (int(sow) // 30) % 24 + 1
    return build_subframe(sf_id, _d1_payload(sf_id, eph, week, iono, pnum),
                          sow)


def d2_subframe(sf_id, eph, frame_sow, week, iono=((0,) * 4, (0,) * 4)):
    frame = int(frame_sow) // 3
    if sf_id == 1:
        payload = _d2_sf1_page(frame % 10 + 1, eph, week, iono)
    elif sf_id == 5:
        payload = [0] + bits_of(frame % 120 + 1, 7)
    else:
        payload = []                                  # empty SF2..4
    return build_subframe(sf_id, payload, frame_sow)


def nav_stream(eph: dict, week: int, tow0_sow: float, duration_s: float,
               *, d2: bool = False, eph_by_prn=None,
               iono=None) -> tuple[np.ndarray, float]:
    """Deterministic D1 (or D2) symbol stream, ``int8`` {-1,+1}, starting at
    the subframe (D1, 6 s grid) or frame (D2, 3 s grid) boundary at or
    before ``tow0_sow`` (BDT). Subframe / page numbers follow BDT, so a
    stream started later carries the same symbols at the same time.
    ``iono`` is Klobuchar ``(alpha[4], beta[4])``.
    """
    iono = iono or ((0.0,) * 4, (0.0,) * 4)
    rate = SYM_RATE_D2 if d2 else SYM_RATE_D1
    grid = 3 if d2 else 6
    sow0 = int(float(tow0_sow) // grid) * grid
    syms: list[int] = []
    if d2:
        nfr = int(math.ceil((math.ceil(duration_s) + 30) / 3.0))
        for i in range(nfr):
            fs = (sow0 + 3 * i) % 604800
            for sf in range(1, 6):
                syms.extend(d2_subframe(sf, eph, fs, week, iono))
    else:
        nsf = int(math.ceil((math.ceil(duration_s) + 30) / 6.0))
        for i in range(nsf):
            sow = (sow0 + 6 * i) % 604800
            syms.extend(d1_subframe((sow // 6) % 5 + 1, eph, sow, week, iono))
    a = np.asarray(syms, dtype=np.int8)
    return np.where(a > 0, np.int8(1), np.int8(-1)).astype(np.int8), rate


# --------------------------------------------------------------------------
# clean-channel decode
# --------------------------------------------------------------------------
def icd_order(sf300: list[int]) -> list[int]:
    """Transmitted subframe -> ICD bit order (per word: info bits, then the
    two parity nibbles), the layout RTKLIB's decoders index into."""
    out = list(sf300[:30])
    for w in range(1, 10):
        a, b = deinterleave2(sf300[w * 30:(w + 1) * 30])
        out += a[:11] + b[:11] + a[11:] + b[11:]
    return out


def bch_ok(word15: list[int]) -> bool:
    return bch_encode(list(word15[:11])) == list(word15)


def decode_subframe(sf300: list[int]) -> dict:
    """``{"fra_id", "sow", "info", "bch_ok"}`` where ``info`` is the 224
    information bits (header included)."""
    words = [sf300[i * 30:(i + 1) * 30] for i in range(10)]
    assert words[0][:11] == PREAMBLE, "preamble mismatch"
    ok = bch_ok(words[0][15:30])
    info = list(words[0][:26])
    for w in words[1:]:
        a, b = deinterleave2(w)
        ok = ok and bch_ok(a) and bch_ok(b)
        info += bch_decode(a) + bch_decode(b)
    u = lambda lo, n: int("".join(map(str, info[lo:lo + n])), 2)
    return {"fra_id": u(15, 3), "sow": u(18, 20), "info": info, "bch_ok": ok}


def decode_frame(stream: np.ndarray) -> dict[int, list[int]]:
    """Map subframe id -> its 224 information bits, scanning for the
    preamble (first BCH-valid occurrence of each id)."""
    bits = (np.asarray(stream) > 0).astype(int).tolist()
    out: dict[int, list[int]] = {}
    i = 0
    while i + 300 <= len(bits):
        if bits[i:i + 11] == PREAMBLE:
            d = decode_subframe(bits[i:i + 300])
            if d["bch_ok"]:
                out.setdefault(d["fra_id"], d["info"])
                i += 300
                continue
        i += 1
    return out
