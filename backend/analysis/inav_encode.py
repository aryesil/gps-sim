"""SP-D: Galileo E1-B I/NAV navigation message encoder.

Builds a structurally spec-accurate Galileo Open Service I/NAV symbol stream
(Galileo OS SIS ICD): 2 s nominal pages, each an *even* then an *odd* 250-
symbol page part at 250 sym/s. A page part is a 10-symbol synchronisation
pattern followed by 240 FEC symbols -- rate 1/2, constraint length 7
convolutional coding (G1 = 0o171, G2 = 0o133 with G2 inverted) over a
120-bit block (114 data + 6 tail zeros), then a 30x8 block interleaver.

Implemented word types: 1-5 (ephemeris + clock + iono/BGD/GST) plus type 0
(spare/time), transmitted one per page in a repeating 6-page cycle so a
receiver recovers the full ephemeris within 12 s. Reserved / SAR fields
are transmitted as zero; SISA carries the RINEX value. A 24-bit CRC-24Q
covers the two page parts' type+data fields; :func:`check_page_crc` re-verifies it.

No Galileo receiver ships in this repo; correctness is covered by the
encoder unit tests (sync offsets, FEC reference vector, interleaver
permutation, CRC self-check) and by acquisition surviving the modulation.
"""
from __future__ import annotations

import math

import numpy as np

from backend.geometry import galileo_clock_shift_s

SYNC_PATTERN = [0, 1, 0, 1, 1, 0, 0, 0, 0, 0]   # 10 symbols, even and odd
SYM_RATE_HZ = 250.0
_PAGE_SYMS = 500                                  # 2 s nominal page
_HALF_SYMS = 250
_SC = math.pi                                     # semicircle <-> radian

# Galileo OS SIS ICD Bölüm 4.1.3: G1 = 0o171, G2 = 0o133 (G2 terslenir).
# Standart polinomlar MSB=en-yeni bit kuralındadır. Koddaki yazmaçta ((reg << 1) | b) ile
# en yeni bit LSB'de olduğundan 7-bit zaman tersi polinomlar kullanılır: G1=0o117, G2=0o155.
_G1 = 0o117
_G2 = 0o155
_CRC24Q = 0x1864CFB


# --------------------------------------------------------------------------
# bit helpers
# --------------------------------------------------------------------------
def bits_of(value: int, n: int) -> list[int]:
    """`value` as `n` big-endian bits (two's complement if negative)."""
    v = int(value) & ((1 << n) - 1)
    return [(v >> (n - 1 - i)) & 1 for i in range(n)]


def _f(x) -> float:
    """NaN/None-safe float: missing broadcast fields become 0."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(v) or math.isinf(v) else v


def twos(value: float, scale: float, n: int) -> int:
    """Quantise `value / scale` to an `n`-bit signed integer (saturating)."""
    q = int(round(_f(value) / scale))
    lo, hi = -(1 << (n - 1)), (1 << (n - 1)) - 1
    return max(lo, min(hi, q))


def uns(value: float, scale: float, n: int) -> int:
    q = int(round(_f(value) / scale))
    return max(0, min((1 << n) - 1, q))


def _crc24q(bits: list[int]) -> list[int]:
    """CRC-24Q (poly 0x1864CFB, init 0), MSB-first over `bits`."""
    # Doğrudan form CRC-24Q: reg ^= bit<<23 döngüsü M(x)*x^24 mod g(x) üretir.
    # Fazladan 24 sıfır kaydırma standardı bozar, bu yüzden trailing sıfır kaydırma uygulanmaz.
    reg = 0
    for b in bits:
        reg ^= (int(b) & 1) << 23
        reg <<= 1
        if reg & (1 << 24):
            reg ^= _CRC24Q
        reg &= 0xFFFFFF
    return bits_of(reg, 24)


# --------------------------------------------------------------------------
# FEC: rate 1/2, K=7 convolutional, G2 inverted
# --------------------------------------------------------------------------
def _parity(x: int) -> int:
    return bin(x).count("1") & 1


def conv_encode(data: list[int]) -> list[int]:
    """Rate-1/2 K=7 convolutional code, G1=0o171 then (G2=0o133 inverted).
    Encoder starts in the all-zero state; `data` must carry its own tail.
    """
    reg = 0
    out: list[int] = []
    for b in data:
        reg = ((reg << 1) | (int(b) & 1)) & 0x7F
        out.append(_parity(reg & _G1))
        out.append(_parity(reg & _G2) ^ 1)
    return out


def interleave_30x8(sym: list[int]) -> list[int]:
    """Blok serpiştirici (Galileo OS SIS ICD Bölüm 4.1.4):
    8 satır x 30 sütun matris; kodlanmış semboller SÜTUN sütun yazılır
    (sütun i = c[8i..8i+7]), SATIR satır okunur: çıkış[j*30+i] = c[i*8+j].
    Cebirsel karşılığı reshape(30, 8).T.reshape(240) (a[sütun, satır] transpozu).
    Alıcı tarafı (GNSS-SDR): out[c*8+r] = in[r*30+c]. Önceki (8, 30) reshape
    bu permütasyonun tersiydi; alıcıda hiçbir sayfanın CRC'si tutmuyordu.
    """
    assert len(sym) == 240
    a = np.asarray(sym).reshape(30, 8)
    return a.T.reshape(240).tolist()


def deinterleave_30x8(sym: list[int]) -> list[int]:
    """Ters blok serpiştirici: interleave_30x8'in tam tersi (alıcı yönü)."""
    a = np.asarray(sym).reshape(8, 30)
    return a.T.reshape(240).tolist()


# --------------------------------------------------------------------------
# word-type payloads (128 bits each incl. the 6-bit type)
# --------------------------------------------------------------------------
def _iodnav(eph: dict) -> int:
    """Galileo IODnav türetimi (10 bit, 0-1023):
    1. 'iodnav' alanı varsa öncelikli olarak kullan.
    2. 'iode' alanı varsa (bazı RINEX ayrıştırıcıları buraya yazar) kullan.
    3. İkisi de yoksa toe // 600 mod 1024 ile deterministik türet (proje içi deterministik türetme kuralı; standart kuralı değil).
    """
    for key in ("iodnav", "iode"):
        val = eph.get(key)
        if val is not None:
            try:
                v = float(val)
                if not (math.isnan(v) or math.isinf(v)):
                    return int(v) & 0x3FF
            except (TypeError, ValueError):
                pass
    toe = _f(eph.get("toe"))
    return int(toe // 600) & 0x3FF


def sisa_index(metres) -> int:
    """SISA (E1,E5b) index for a RINEX SISA value in metres (OS SIS ICD
    Table 89): 1 cm steps to 0.49 m, 2 cm to 0.99 m, 4 cm to 1.99 m, 16 cm
    to 6 m. A missing value maps to 107 (3.12 m, the nominal broadcast),
    never to 255 (NAPA), which receivers treat as unusable."""
    m = _f(metres)
    if m <= 0.0:
        return 107
    cm = m * 100.0
    if cm < 50.0:
        return int(round(cm))
    if cm < 100.0:
        return 50 + int(round((cm - 50.0) / 2.0))
    if cm < 200.0:
        return 75 + int(round((cm - 100.0) / 4.0))
    return min(125, 100 + int(round((cm - 200.0) / 16.0)))


def word_type_1(eph, tow):
    b = bits_of(1, 6) + bits_of(_iodnav(eph), 10)
    b += bits_of(int(round(_f(eph.get("toe")) / 60.0)) & 0x3FFF, 14)
    b += bits_of(twos(_f(eph.get("m0")) / _SC, 2 ** -31, 32), 32)
    b += bits_of(uns(_f(eph.get("e")), 2 ** -33, 32), 32)
    b += bits_of(uns(_f(eph.get("sqrtA")), 2 ** -19, 32), 32)
    b += [0, 0]
    return b


def word_type_2(eph, tow):
    b = bits_of(2, 6) + bits_of(_iodnav(eph), 10)
    b += bits_of(twos(_f(eph.get("omega0")) / _SC, 2 ** -31, 32), 32)
    b += bits_of(twos(_f(eph.get("i0")) / _SC, 2 ** -31, 32), 32)
    b += bits_of(twos(_f(eph.get("omega")) / _SC, 2 ** -31, 32), 32)
    b += bits_of(twos(_f(eph.get("idot")) / _SC, 2 ** -43, 14), 14)
    b += [0, 0]
    return b


def word_type_3(eph, tow):
    b = bits_of(3, 6) + bits_of(_iodnav(eph), 10)
    b += bits_of(twos(_f(eph.get("omega_dot")) / _SC, 2 ** -43, 24), 24)
    b += bits_of(twos(_f(eph.get("delta_n")) / _SC, 2 ** -43, 16), 16)
    b += bits_of(twos(_f(eph.get("cuc")), 2 ** -29, 16), 16)
    b += bits_of(twos(_f(eph.get("cus")), 2 ** -29, 16), 16)
    b += bits_of(twos(_f(eph.get("crc")), 2 ** -5, 16), 16)
    b += bits_of(twos(_f(eph.get("crs")), 2 ** -5, 16), 16)
    b += bits_of(sisa_index(eph.get("sisa")), 8)             # SISA(E1,E5b)
    return b


def word_type_4(eph, tow):
    b = bits_of(4, 6) + bits_of(_iodnav(eph), 10)
    b += bits_of(int(_f(eph.get("prn", 0))) & 0x3F, 6)
    b += bits_of(twos(_f(eph.get("cic")), 2 ** -29, 16), 16)
    b += bits_of(twos(_f(eph.get("cis")), 2 ** -29, 16), 16)
    b += bits_of(int(round(_f(eph.get("toc")) / 60.0)) & 0x3FFF, 14)
    # I/NAV af0 E1-E5b IF referanslıdır; kayıt F/NAV (E1-E5a) referanslıysa
    # -Δ ile taşı (geometry.galileo_clock_shift_s), yoksa değişmez.
    b += bits_of(twos(_f(eph.get("af0")) + galileo_clock_shift_s(eph, "E5B"), 2 ** -34, 31), 31)
    b += bits_of(twos(_f(eph.get("af1")), 2 ** -46, 21), 21)
    b += bits_of(twos(_f(eph.get("af2")), 2 ** -59, 6), 6)
    b += [0, 0]
    return b


def word_type_5(eph, tow, week):
    b = bits_of(5, 6)
    # ai0..ai2 BİLEREK 0: NeQuick-G efektif iyonizasyon düzeyi Az = ai0 + ai1*mu
    # + ai2*mu^2. Simülatör E sinyaline iyonosfer gecikmesi enjekte etmiyor
    # (engine._atmo_for, "enjekte edilen = yayından çıkarılabilen"); sıfır
    # olmayan bir Az, NeQuick-G alıcısına sinyalde bulunmayan bir gecikmeyi
    # düşürtüp pseudorange'i yanlı kılardı. Tutarlı sıfır olmayan değer ancak
    # engine'e NeQuick-G (ICD Ek. E, ~1500 satır) gömüp ai'yi aynı Az'dan
    # türeterek verilebilir; Klobuchar'dan türetmek alıcıdaki modelle
    # uyuşmaz. Bkz. tests/synth/test_engine_iono_galileo.py.
    b += bits_of(0, 11) + bits_of(0, 11) + bits_of(0, 14)      # iono a_i0..2
    b += bits_of(0, 5)                                          # region flags
    bgd_b = eph.get("tgd", 0.0)
    bgd_a = eph.get("tgd_e5a", bgd_b)
    b += bits_of(twos(bgd_a, 2 ** -32, 10), 10)                # BGD E1-E5a
    b += bits_of(twos(bgd_b, 2 ** -32, 10), 10)                # BGD E1-E5b
    b += bits_of(0, 2) + bits_of(0, 2) + [0] + [0]             # HS/DVS
    b += bits_of(int(week) & 0xFFF, 12)
    b += bits_of(int(tow) & 0xFFFFF, 20)
    b += bits_of(0, 23)
    return b


def word_type_0(eph, tow, week):
    # Time field '10': WN/TOW below are valid (OS SIS ICD Table 50);
    # '00' told receivers to ignore them.
    b = bits_of(0, 6) + bits_of(2, 2) + bits_of(0, 88)
    b += bits_of(int(week) & 0xFFF, 12)
    b += bits_of(int(tow) & 0xFFFFF, 20)
    return b


_CYCLE = [1, 2, 3, 4, 5, 0]
E1B_PAGE_PHASE_S = 1


def page_index(tow: int) -> int:
    return ((int(tow) - E1B_PAGE_PHASE_S) % 30) // 2


def word_bits(wtype: int, eph: dict, tow: int, week: int) -> list[int]:
    if wtype == 1:
        w = word_type_1(eph, tow)
    elif wtype == 2:
        w = word_type_2(eph, tow)
    elif wtype == 3:
        w = word_type_3(eph, tow)
    elif wtype == 4:
        w = word_type_4(eph, tow)
    elif wtype == 5:
        w = word_type_5(eph, tow, week)
    else:
        w = word_type_0(eph, tow, week)
    assert len(w) == 128, (wtype, len(w))
    return w


# --------------------------------------------------------------------------
# page assembly
# --------------------------------------------------------------------------
def _page_parts(wtype: int, eph: dict, tow: int, week: int,
                osnma40: list[int] | None,
                word: list[int] | None = None) -> tuple[list[int], list[int]]:
    """Pre-FEC even and odd page parts, 120 bits each."""
    if word is None:
        word = word_bits(wtype, eph, tow, week)      # 128 bits incl. type
    data_k = word[:112]                              # even part data field
    data_j = word[112:128]                           # odd part data field (16)
    r1 = [0] * 40 if osnma40 is None else list(osnma40)
    assert len(r1) == 40

    # I/NAV page part = 114 content bits + 6 tail zeros -> 120 -> FEC -> 240.
    #   even: even/odd(1) + page type(1) + data k 1..112 (112)          = 114
    #   odd : even/odd(1) + page type(1) + data k 113..128 (16)
    #         + reserved-1(40) + SAR(22) + spare(2) + CRC(24) + reserved-2(8)
    #                                                                   = 114
    # CRC-24Q covers the 196 bits preceding it, in transmission order.
    crc_in = ([0, 0] + data_k
              + [1, 0] + data_j + r1 + [0] * 22 + [0, 0])
    assert len(crc_in) == 196
    crc = _crc24q(crc_in)

    even_data = [0, 0] + data_k + [0] * 6                       # 120 bits
    odd_data = ([1, 0] + data_j + r1 + [0] * 22 + [0, 0]
                + crc + [0] * 8 + [0] * 6)                      # 120 bits
    assert len(even_data) == 120 and len(odd_data) == 120
    return even_data, odd_data


def page_bits240(wtype: int, eph: dict, tow: int, week: int,
                 osnma40: list[int] | None = None) -> list[int]:
    even_data, odd_data = _page_parts(wtype, eph, tow, week, osnma40)
    return even_data[:120] + odd_data[:120]


def build_page(wtype: int, eph: dict, tow: int, week: int,
               osnma40: list[int] | None = None,
               word: list[int] | None = None) -> list[int]:
    """One 500-symbol nominal page (even part then odd part)."""
    even_data, odd_data = _page_parts(wtype, eph, tow, week, osnma40, word)
    even = SYNC_PATTERN + interleave_30x8(conv_encode(even_data))
    odd = SYNC_PATTERN + interleave_30x8(conv_encode(odd_data))
    assert len(even) == _HALF_SYMS and len(odd) == _HALF_SYMS
    return even + odd


def check_page_crc(page_syms: list[int]) -> bool:
    """Re-verify the CRC of a page produced by :func:`build_page`."""
    even = page_syms[:_HALF_SYMS]
    odd = page_syms[_HALF_SYMS:]
    even_data = _viterbi_free_decode(deinterleave_30x8(even[10:]))
    odd_data = _viterbi_free_decode(deinterleave_30x8(odd[10:]))
    data_k = even_data[2:114]
    data_j = odd_data[2:18]
    crc_in = ([0, 0] + data_k + [1, 0] + data_j + [0] * 40 + [0] * 22 + [0, 0])
    off = 2 + 16 + 40 + 22 + 2
    return _crc24q(crc_in) == odd_data[off:off + 24]


def decode_word(page_syms: list[int]) -> tuple[int, list[int]]:
    """Recover ``(word_type, word_bits[128])`` from a clean page produced by
    :func:`build_page` -- deinterleave, undo the FEC, then splice the 112-bit
    even data field with the 16-bit odd data field. For the encoder tests and
    the QZSS-style round-trip checks; not a noise-tolerant receiver.
    """
    even = page_syms[:_HALF_SYMS]
    odd = page_syms[_HALF_SYMS:]
    even_data = _viterbi_free_decode(deinterleave_30x8(list(even[10:])))
    odd_data = _viterbi_free_decode(deinterleave_30x8(list(odd[10:])))
    word = even_data[2:114] + odd_data[2:18]
    wtype = int("".join(str(b) for b in word[:6]), 2)
    return wtype, word


def _viterbi_free_decode(coded: list[int]) -> list[int]:
    """Exact inverse of :func:`conv_encode` for a clean (noise-free) stream:
    walk the trellis deterministically using the G1 output, which for this
    code equals the newest register bit XOR older taps -- recover input bit
    by bit from the known encoder state."""
    reg = 0
    out = []
    for i in range(0, len(coded), 2):
        c1 = coded[i]
        # try input 0 and 1, pick the one whose (c1, c2) matches
        for cand in (0, 1):
            r = ((reg << 1) | cand) & 0x7F
            if _parity(r & _G1) == c1 and (_parity(r & _G2) ^ 1) == coded[i + 1]:
                out.append(cand)
                reg = r
                break
        else:                                   # pragma: no cover - clean only
            out.append(0)
            reg = (reg << 1) & 0x7F
    return out


# --------------------------------------------------------------------------
# public entry
# --------------------------------------------------------------------------
def nav_stream(eph: dict, week: int, tow0_sow: float, duration_s: float,
               eph_by_prn=None, osnma=None, prn: int | None = None) -> np.ndarray:
    """Deterministic I/NAV symbol stream, ``int8`` in ``{-1, +1}`` at 250
    sym/s. Length covers ``duration_s`` rounded up to a whole 2 s page plus a
    30 s guard, aligned so a page boundary sits on the odd-second GST grid
    (OS SIS ICD 2.0 Table 38).
    """
    npages = int(math.ceil((math.ceil(duration_s) + 30) / 2.0))
    r = int(round(tow0_sow))
    tow0 = r - ((r - E1B_PAGE_PHASE_S) % 2)
    gen = None
    if osnma is not None:
        from backend.analysis.osnma.generator import OsnmaGenerator
        gen = OsnmaGenerator(osnma)
        prn = int(prn if prn is not None else eph.get("prn", 1) or 1)
    syms: list[int] = []
    for p in range(npages):
        ab = tow0 + 2 * p
        wk = week + ab // 604800
        tow = ab % 604800
        # Word type follows GST, not the stream start: a stream started
        # later (live mode restarts it every segment) must continue the
        # same sequence instead of re-sending the first word type.
        wtype = _CYCLE[page_index(tow) % len(_CYCLE)]
        if gen is None:
            syms.extend(build_page(wtype, eph, tow, wk))
            continue
        gst_abs = wk * 604800 + tow
        e = gen.eph_for(prn, gst_abs) or eph
        word = gen.tamper_word(prn, gst_abs, wtype, word_bits(wtype, e, tow, wk))
        syms.extend(build_page(wtype, e, tow, wk, osnma40=gen.page_bits(prn, gst_abs),
                               word=word))
    a = np.asarray(syms, dtype=np.int8)
    # BPSK eşlemesi: bit 0 -> +1, bit 1 -> -1 (tüm GNSS standartları ve E1-C pilot evresiyle uyumlu)
    return np.where(a > 0, np.int8(-1), np.int8(1)).astype(np.int8)
