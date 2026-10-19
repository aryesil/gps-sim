"""Galileo E1 I/NAV navigation message decoder.

The inverse of backend.analysis.inav_encode.
Implements deinterleaving, rate-1/2 convolutional decoding, page framing,
CRC-24Q verification, word type 0-5 extraction, and Keplerian ephemeris
reconstruction from a CRC-valid set of words 1, 2, 3, 4.
"""
from __future__ import annotations

import math
from typing import Any, Sequence

import numpy as np

from backend.analysis import _inav_layout as L
from backend.analysis import _viterbi
from backend.analysis._bit_utils import (
    deinterleave_block,
    normalize_symbols,
    parity_bit,
    unpack_field,
    unpack_int,
    unpack_uint,
)
from backend.analysis._crc import check_crc24q, crc24q
from backend.analysis._demod_utils import derotate_bpsk_carrier

_G1 = _viterbi._G1
_G2 = _viterbi._G2
_PI = math.pi
_NXT = _viterbi._NXT_INV
_OUT = _viterbi._OUT_INV

_parity = parity_bit
_u = unpack_uint
_s = unpack_int
_field = unpack_field


def deinterleave_30x8(sym: Sequence[int] | np.ndarray) -> list[int]:
    """Blok ters-serpiştirici (ICD 4.1.4): 8 satır x 30 sütun; verici sütun sütun
    yazıp satır satır okur, alıcı out[c*8+r] = in[r*30+c] ile geri alır."""
    return deinterleave_block(sym, rows=8, cols=30)


def viterbi_free_decode(coded: Sequence[int] | np.ndarray) -> list[int]:
    """Exact inverse of inav_encode.conv_encode for clean channel from initial state 0."""
    return _viterbi.viterbi_free_decode(coded, invert_g2=True)


def viterbi_decode(coded: Sequence[int] | np.ndarray, *, end_state: int | None = 0) -> list[int]:
    """Hard-decision Viterbi decoder for rate-1/2 K=7 convolutional code with G2 inverted.
    Known starting state is 0, default ending state is 0 (zero-tail kısıtlaması).
    """
    return _viterbi.viterbi_decode(coded, invert_g2=True, known_start_state=0, end_state=end_state)


def decode_word_fields(wtype: int, word_bits: list[int]) -> dict[str, Any]:
    """Decode 128-bit word into named fields using the layout table."""
    out: dict[str, Any] = {}
    if wtype not in L.LAYOUT:
        return out
    off = 0
    for name, kind, scale, n in L.LAYOUT[wtype]:
        if not name.startswith("_"):
            out[name] = _field(word_bits, off, kind, scale, n)
        off += n
    return out


def _try_bits(even_bits: list[int], odd_bits: list[int]) -> dict[str, Any] | None:
    """I/NAV çift/tek bit dizisinden alanları ve CRC doğruluğunu değerlendirir."""
    if len(even_bits) < 120 or len(odd_bits) < 120:
        return None
    if even_bits[0] != 0 or odd_bits[0] != 1:
        return None

    data_k = even_bits[2:114]
    data_j = odd_bits[2:18]
    reserved_1 = odd_bits[18:58]
    sar = odd_bits[58:80]
    spare = odd_bits[80:82]
    crc_rx = odd_bits[82:106]

    crc_in = [0, 0] + data_k + [1, 0] + data_j + reserved_1 + sar + spare
    crc_calc = crc24q(crc_in)
    crc_ok = (crc_calc == crc_rx)

    word_bits = data_k + data_j
    wtype = _u(word_bits, 0, 6)

    return {
        "type": wtype,
        "crc_ok": crc_ok,
        "word_bits": word_bits,
        "osnma": reserved_1,
        "page_bits": even_bits[:120] + odd_bits[:120],
        "fields": decode_word_fields(wtype, word_bits) if crc_ok else {},
    }


def decode_page(even_syms: list[int], odd_syms: list[int]) -> dict[str, Any] | None:
    """Decode a 500-symbol nominal page from even and odd 250-symbol page parts."""
    if len(even_syms) != L.HALF_PAGE_SYMS or len(odd_syms) != L.HALF_PAGE_SYMS:
        return None

    # Deinterleave 240 symbols
    even_coded = deinterleave_30x8(even_syms[L.SYNC_SYMS:])
    odd_coded = deinterleave_30x8(odd_syms[L.SYNC_SYMS:])

    # 1. Hızlı yol: gürültüsüz kanal için viterbi_free_decode
    even_bits = viterbi_free_decode(even_coded)
    odd_bits = viterbi_free_decode(odd_coded)
    res = _try_bits(even_bits, odd_bits)
    if res is not None and res["crc_ok"]:
        return res

    # 2. Fallback: CRC başarısız ise tam Viterbi çözücü (known_start_state=0, end_state=0 ile zero-tail)
    even_bits_v = _viterbi.viterbi_decode(even_coded, invert_g2=True, known_start_state=0, end_state=0)
    odd_bits_v = _viterbi.viterbi_decode(odd_coded, invert_g2=True, known_start_state=0, end_state=0)
    res_v = _try_bits(even_bits_v, odd_bits_v)
    if res_v is not None and res_v["crc_ok"]:
        return res_v

    return res_v if res_v is not None else res


def decode_messages(sym01: list[int] | np.ndarray) -> list[dict[str, Any]]:
    """Scan a {0, 1} symbol stream for Galileo E1 I/NAV pages.
    Handles sync pattern searching, page part pairing, CRC-24Q checking,
    and carrier phase ambiguity (polarity flip).
    """
    sym = normalize_symbols(sym01, bpsk_sign=False)
    n = len(sym)
    sync = L.SYNC_PATTERN

    for flip in (0, 1):
        stream = [x ^ flip for x in sym]
        msgs: list[dict[str, Any]] = []
        i = 0
        while i + L.PAGE_SYMS <= n:
            # Check even part sync
            if stream[i : i + L.SYNC_SYMS] == sync:
                # Check odd part sync at i + 250
                if stream[i + L.HALF_PAGE_SYMS : i + L.HALF_PAGE_SYMS + L.SYNC_SYMS] == sync:
                    even_part = stream[i : i + L.HALF_PAGE_SYMS]
                    odd_part = stream[i + L.HALF_PAGE_SYMS : i + L.PAGE_SYMS]
                    page = decode_page(even_part, odd_part)
                    if page is not None and page["crc_ok"]:
                        # Akış içindeki sembol başlangıç indeksi (zamanlama doğrulaması için)
                        page["sym_idx"] = i
                        msgs.append(page)
                        i += L.PAGE_SYMS
                        continue
            i += 1
        if msgs:
            return msgs
    return []


def sisa_metres(idx: int | float) -> float:
    """Convert an 8-bit SISA(E1,E5b) index (OS SIS ICD Table 89) to metres.
    Inverse of :func:`backend.analysis.inav_encode.sisa_index`."""
    i = int(round(float(idx)))
    if i < 0:
        return 0.0
    if i < 50:
        return round(i * 0.01, 4)
    if i < 75:
        return round(0.50 + (i - 50) * 0.02, 4)
    if i < 100:
        return round(1.00 + (i - 75) * 0.04, 4)
    if i <= 125:
        return round(2.00 + (i - 100) * 0.16, 4)
    return float("nan")


def reconstruct_ephemeris(msgs: list[dict[str, Any]]) -> dict[str, Any]:
    """Reconstruct Galileo Keplerian ephemeris dictionary from CRC-valid I/NAV messages.
    Requires word types 1, 2, 3, 4. Word types 5 and 0 are used when available for
    GST week, TOW, and group delays.
    """
    by_type: dict[int, dict[str, Any]] = {}
    for m in msgs:
        if m.get("crc_ok") and m.get("fields"):
            wtype = m["type"]
            by_type[wtype] = m["fields"]

    required = {1, 2, 3, 4}
    if not required.issubset(by_type):
        raise ValueError(f"need CRC-valid I/NAV word types 1, 2, 3 and 4, got {sorted(by_type.keys())}")

    f1 = by_type[1]
    f2 = by_type[2]
    f3 = by_type[3]
    f4 = by_type[4]
    f5 = by_type.get(5, {})
    f0 = by_type.get(0, {})

    # I/NAV af0..af2 E1-E5b IF referanslıdır (OS SIS ICD 5.1.4).
    rec: dict[str, Any] = {"system": "E", "clk_ref": "E5B"}
    rec["prn"] = int(f4["prn"])
    rec["iodnav"] = int(f1["iodnav"])

    # Orbital parameters
    rec["sqrtA"] = float(f1["sqrtA"])
    rec["e"] = float(f1["e"])
    rec["m0"] = float(f1["m0"]) * _PI
    rec["toe"] = float(f1["toe"])

    rec["omega0"] = float(f2["omega0"]) * _PI
    rec["i0"] = float(f2["i0"]) * _PI
    rec["omega"] = float(f2["omega"]) * _PI
    rec["idot"] = float(f2["idot"]) * _PI

    rec["omega_dot"] = float(f3["omega_dot"]) * _PI
    rec["delta_n"] = float(f3["delta_n"]) * _PI
    rec["cuc"] = float(f3["cuc"])
    rec["cus"] = float(f3["cus"])
    rec["crc"] = float(f3["crc"])
    rec["crs"] = float(f3["crs"])
    if "sisa" in f3:
        rec["sisa"] = sisa_metres(f3["sisa"])

    # Harmonic corrections and clock
    rec["cic"] = float(f4["cic"])
    rec["cis"] = float(f4["cis"])
    rec["toc"] = float(f4["toc"])
    rec["af0"] = float(f4["af0"])
    rec["af1"] = float(f4["af1"])
    rec["af2"] = float(f4["af2"])

    # Group delays and timing from word 5 / word 0
    if f5:
        rec["tgd"] = float(f5.get("bgd_e1_e5b", 0.0))
        rec["tgd_e5a"] = float(f5.get("bgd_e1_e5a", rec["tgd"]))
        rec["gal_week"] = int(f5.get("week", 0))
        rec["tow"] = float(f5.get("tow", 0.0))
    elif f0:
        rec["gal_week"] = int(f0.get("week", 0))
        rec["tow"] = float(f0.get("tow", 0.0))
        rec["tgd"] = 0.0
        rec["tgd_e5a"] = 0.0
    else:
        rec["gal_week"] = 0
        rec["tow"] = 0.0
        rec["tgd"] = 0.0
        rec["tgd_e5a"] = 0.0

    return rec


# --- IQ -> I/NAV symbols -----------------------------------------------------

_E1_LEN = 4092
_E1_CHIP_HZ = 1.023e6
_E1_CTR_HZ = 1_575_420_000.0
_E1_SYM_S = 0.004  # 4 ms = 250 sym/s


def demod_symbols_e1b(
    iq,
    fs: float,
    prn: int,
    *,
    dopp_hz: float | None = None,
    code_phase_chips: float | None = None,
    centre_offset_hz: float = 0.0,
) -> np.ndarray:
    """Demodulate hard {0, 1} I/NAV symbols (250 sym/s) from Galileo E1 IQ.

    Wipes carrier Doppler and correlates against the Galileo E1-B primary code
    modulated by the BOC(1,1) subcarrier (1.023 MHz).
    """
    from backend.analysis import l1_acquire
    from backend.synth import _lib

    iq = np.asarray(iq)
    if not np.iscomplexobj(iq):
        iq = iq.astype(np.complex64)
    elif iq.dtype == np.complex128:
        iq = iq.astype(np.complex64)

    if dopp_hz is None or code_phase_chips is None:
        acq = l1_acquire.acquire_l1(
            iq, fs, "E", prn, centre_offset_hz=centre_offset_hz
        )
        if acq.get("metric_db", -99.0) < 6.0:
            return np.zeros(0, dtype=np.int8)
        dopp_hz = acq["doppler_hz"] if dopp_hz is None else dopp_hz
        code_phase_chips = (
            acq["code_phase_chips"] if code_phase_chips is None else code_phase_chips
        )

    code = _lib.code(5, int(prn), _E1_LEN, 0)[0].astype(np.float64)

    npp = int(round(fs * _E1_SYM_S))
    nsym = len(iq) // npp
    if nsym == 0:
        return np.zeros(0, dtype=np.int8)

    cp0 = (_E1_LEN - float(code_phase_chips)) % _E1_LEN
    e0 = int(math.ceil((_E1_LEN - cp0) % _E1_LEN / _E1_CHIP_HZ * fs))
    if e0 + npp * 4 > len(iq):
        e0 = 0
    base = iq[e0:]
    nsym = base.size // npp
    if nsym == 0:
        return np.zeros(0, dtype=np.int8)

    carrier_freq = centre_offset_hz + float(dopp_hz)
    rate_div_ctr = (_E1_CHIP_HZ / _E1_CTR_HZ) * float(dopp_hz)
    inv_fs = 1.0 / fs

    seg = np.empty(nsym, dtype=np.complex64)
    # Bound chunk size so peak temporary buffer never exceeds ~250k samples (< 20 MB)
    max_chunk_samples = 250_000
    chunk_sym = max(1, min(100, int(max_chunk_samples // max(1, npp))))

    for s0 in range(0, nsym, chunk_sym):
        s1 = min(s0 + chunk_sym, nsym)
        n_chk = s1 - s0
        k0 = s0 * npp
        k1 = s1 * npp
        t_chk = (np.arange(k0, k1, dtype=np.float64) + e0) * inv_fs
        wipe_chk = np.exp(-2j * np.pi * carrier_freq * t_chk).astype(np.complex64)
        cph_chk = (cp0 + _E1_CHIP_HZ * t_chk + rate_div_ctr * t_chk) % _E1_LEN
        chip_idx_chk = np.floor(cph_chk).astype(np.int32)
        sub_chk = np.where(np.floor(2.0 * cph_chk).astype(np.int32) & 1, -1.0, 1.0).astype(np.float32)
        rep_chk = code[chip_idx_chk].astype(np.float32) * sub_chk
        seg[s0:s1] = (base[k0:k1] * wipe_chk * rep_chk).reshape(n_chk, npp).sum(axis=1)

    seg = derotate_bpsk_carrier(seg, deg=2)
    return np.where(np.real(seg) > 0, np.int8(1), np.int8(0)).astype(np.int8)


demod_symbols = demod_symbols_e1b
