"""Clean-channel CNAV decoder -- the inverse of backend.analysis.cnav_encode.

Convolutional Viterbi-free decode from state 0, 0x8B framing, CRC-24Q
check, message 10 / 11 / 30 / 33 field extraction via the shared
``_cnav_layout`` table, and Keplerian-ephemeris reconstruction from a
CRC-valid 10 + 11 (+ 30) set.

``demod_symbols`` pulls the 50 sym/s convolutional symbol stream out of a
GPS L2C IQ capture: the CM code period is exactly one symbol (20 ms), so
this is a plain prompt correlation with a carrier wipe, no 20:1 bit/code
folding.
"""
from __future__ import annotations

import math

import numpy as np

from backend.analysis import _cnav_layout as L
from backend.analysis._crc import crc24q

_PREAMBLE = 0x8B
_G1 = 0o171
_G2 = 0o133
_PI = math.pi


def _parity(x: int) -> int:
    return bin(x).count("1") & 1


def viterbi_free_decode(sym) -> list[int]:
    """Exact inverse of cnav_encode._Conv on a clean channel (from reg 0)."""
    reg = 0
    out: list[int] = []
    sym = list(int(s) & 1 for s in sym)
    for i in range(0, len(sym) - 1, 2):
        s0, s1 = sym[i], sym[i + 1]
        for guess in (0, 1):
            r = ((reg << 1) | guess) & 0x7F
            if _parity(r & _G1) == s0 and (_parity(r & _G2) ^ 1) == s1:
                out.append(guess)
                reg = r
                break
        else:
            out.append(0)
            reg = (reg << 1) & 0x7F
    return out


def _viterbi_tables():
    nxt = np.zeros((64, 2), dtype=np.int64)
    out = np.zeros((64, 2, 2), dtype=np.int64)
    for st in range(64):
        for b in (0, 1):
            r = ((st << 1) | b) & 0x7F
            nxt[st, b] = r & 0x3F
            out[st, b] = (_parity(r & _G1), _parity(r & _G2) ^ 1)
    return nxt, out


_NXT, _OUT = _viterbi_tables()


def viterbi_decode(sym) -> list[int]:
    """Hard-decision Viterbi for the K=7 CNAV code (same convention as
    cnav_encode._Conv). Unlike :func:`viterbi_free_decode` it needs no known
    starting state: a capture begins mid-stream with the continuous encoder
    in an unknown state, so every state starts at metric 0. ``sym`` must
    start on a symbol-pair boundary; callers try both pair offsets."""
    s = np.asarray([int(x) & 1 for x in sym], dtype=np.int64)
    n = s.size // 2
    if n == 0:
        return []
    pairs = s[:2 * n].reshape(n, 2)
    metric = np.zeros(64)
    # predecessor bookkeeping: for each step, best (prev state, bit) per state
    prev = np.zeros((n, 64), dtype=np.int64)
    bit = np.zeros((n, 64), dtype=np.int8)
    src = np.repeat(np.arange(64), 2)
    bsrc = np.tile([0, 1], 64)
    dst = _NXT[src, bsrc]
    for k in range(n):
        cost = (metric[src]
                + (_OUT[src, bsrc, 0] != pairs[k, 0])
                + (_OUT[src, bsrc, 1] != pairs[k, 1]))
        order = np.lexsort((cost, dst))            # by dst, then cost
        first = np.ones(order.size, dtype=bool)
        first[1:] = dst[order][1:] != dst[order][:-1]
        win = order[first]
        new = np.full(64, np.inf)
        new[dst[win]] = cost[win]
        prev[k, dst[win]] = src[win]
        bit[k, dst[win]] = bsrc[win]
        metric = new
    st = int(np.argmin(metric))
    bits = np.zeros(n, dtype=np.int8)
    for k in range(n - 1, -1, -1):
        bits[k] = bit[k, st]
        st = int(prev[k, st])
    return bits.tolist()


def _u(bits, lo, n):
    v = 0
    for b in bits[lo:lo + n]:
        v = (v << 1) | (int(b) & 1)
    return v


def _field(bits, lo, kind, scale, n):
    v = _u(bits, lo, n)
    if kind == "s" and (v & (1 << (n - 1))):
        v -= 1 << n
    return v * scale


def _decode_payload(msg_type: int, payload: list[int]) -> dict:
    out: dict = {}
    off = 0
    for name, kind, scale, nbits in L.LAYOUT[msg_type]:
        out[name] = _field(payload, off, kind, scale, nbits)
        off += nbits
    return out


def decode_messages(sym01) -> list[dict]:
    sym01 = list(int(x) & 1 for x in sym01)
    # A capture starts mid-stream: the symbol-pair phase and the carrier
    # sign are both unknown, so try all four.
    for flip, pair_off in ((0, 0), (0, 1), (1, 0), (1, 1)):
        stream = [x ^ flip for x in sym01[pair_off:]]
        bits = viterbi_decode(stream)
        msgs: list[dict] = []
        i = 0
        while i + L.MSG_BITS <= len(bits):
            if _u(bits, i, 8) == _PREAMBLE:
                m = bits[i:i + L.MSG_BITS]
                if crc24q(m[:L.MSG_BITS - L.CRC_BITS]) == m[L.MSG_BITS - L.CRC_BITS:]:
                    mtype = _u(m, 14, 6)
                    payload = m[L.HEADER_BITS:L.MSG_BITS - L.CRC_BITS]
                    msgs.append({
                        "type": mtype,
                        "prn": _u(m, 8, 6),
                        "tow_6s": _u(m, 20, 17),
                        "alert": m[37],
                        "crc_ok": True,
                        "fields": (_decode_payload(mtype, payload)
                                   if mtype in L.LAYOUT else {}),
                    })
                    i += L.MSG_BITS
                    continue
            i += 1
        if msgs:
            return msgs
    return []


def reconstruct_ephemeris(msgs: list[dict]) -> dict:
    by_type: dict[int, dict] = {}
    for m in msgs:
        if m.get("crc_ok") and m["type"] in (10, 11, 30, 33):
            by_type[m["type"]] = m["fields"]
    if not ({10, 11} <= set(by_type)):
        raise ValueError("need CRC-valid CNAV messages 10 and 11")
    f10, f11 = by_type[10], by_type[11]
    f30 = by_type.get(30, {})
    rec = {"system": "G"}
    rec["sqrtA"] = math.sqrt(max(f10["dA"] + L.A_REF, 1.0))
    rec["e"] = f10["e"]
    rec["m0"] = f10["m0"] * _PI
    rec["delta_n"] = f10["delta_n"] * _PI
    rec["omega"] = f10["omega"] * _PI
    rec["toe"] = f11["toe"] or f10["toe"]
    rec["omega0"] = f11["omega0"] * _PI
    rec["i0"] = f11["i0"] * _PI
    rec["omega_dot"] = (f11["d_omega_dot"] + L.OMEGA_DOT_REF) * _PI
    rec["idot"] = f11["idot"] * _PI
    for k in ("cuc", "cus", "crc", "crs", "cic", "cis"):
        rec[k] = f11[k]
    rec["toc"] = f30.get("toc", rec["toe"])
    rec["af0"] = f30.get("af0", 0.0)
    rec["af1"] = f30.get("af1", 0.0)
    rec["af2"] = f30.get("af2", 0.0)
    rec["tgd"] = f30.get("tgd", 0.0)
    rec["isc_l2c"] = f30.get("isc_l2c", 0.0)
    rec["isc_l5i5"] = f30.get("isc_l5i5", 0.0)
    rec["isc_l5q5"] = f30.get("isc_l5q5", 0.0)
    rec["gps_week"] = int(f10.get("wn", 0))
    return rec


# --- IQ -> convolutional symbols ---------------------------------------------

_SYM_S = 0.02                           # one CNAV symbol (50 sym/s), both bands
_L2_CTR_HZ = 1_227_600_000.0
_L5_CTR_HZ = 1_176_450_000.0
_L5_CHIP_HZ = 10.23e6
_L5_LEN = 10230
# GPS L5 I5 Neuman-Hoffman secondary NH10 (IS-GPS-200 3.3.2.4), {+1,-1}.
_NH10 = np.array([1 if b == "0" else -1 for b in "0000110101"], dtype=np.float64)


# Sub-symbol partials per symbol. The coarse Doppler pull-in squares them
# (cancelling data and secondary-code signs), so the residual it can find is
# +/- _SUBCHUNKS / (4 * sym_s): 80 over a 20 ms symbol is 0.25 ms partials
# and +/- 1 kHz, which covers a 1 ms acquisition's Doppler error. That error
# reaches several hundred Hz on L5, because a 1 ms acquisition block
# straddles an NH10 sign flip.
_SUBCHUNKS = 80


_CHUNK_SAMPLES = 1 << 22          # ~4 M samples per _demod work chunk


def _concentration(corr):
    """Fraction of correlator energy on the real axis after the best
    constant-phase rotation -- ~1 for a locked BPSK stream, ~0.5 for a
    spinning one. Data-blind (corr**2 cancels the +/-1 symbol)."""
    c2 = np.sum(corr ** 2)
    aligned = corr * np.exp(-1j * 0.5 * np.angle(c2))
    return float(np.sum(np.real(aligned) ** 2) / np.sum(np.abs(corr) ** 2))


def _demod(iq, fs, code, *, chip_hz, code_len, carrier_ctr_hz, dopp_hz,
           code_phase_chips, sec=None, sec_rate_hz=0.0, sym_s=_SYM_S):
    """Shared CNAV-style symbol tracker for L2C (CM), L5 (I5), E5a-I and
    B2a-data.

    ``code`` is the {-1,+1} primary code; one symbol spans ``sym_s`` s
    (default 20 ms / 50 sym/s -- L2C/L5/E5a; B-CNAV2 on B2a-data passes
    5 ms / 200 sym/s). ``sec`` / ``sec_rate_hz`` apply a secondary code
    (L5 NH10 at 1 kHz, B2a-data's 5-chip "00010" at 1 kHz); its unknown
    phase is searched jointly with the symbol boundary before the carrier
    is locked. The carrier is modelled as a linear-frequency chirp and the
    code rate is tied to it through ``chip_hz / carrier_ctr_hz`` so code
    Doppler and its drift are tracked across a 40 s capture.
    """
    # Keep the (large) capture in whatever complex precision the caller
    # gave -- an L5 fix reads hundreds of millions of samples, so forcing
    # complex128 here would double an already multi-GB array. complex64 is
    # ample for the tracker.
    iq = np.asarray(iq)
    if not np.iscomplexobj(iq):
        iq = iq.astype(np.complex64)
    code = np.asarray(code, dtype=np.float64)

    npp = int(round(fs * sym_s))
    spp = int(round(fs * (code_len / chip_hz)))       # one primary period
    if npp <= 0 or iq.size // npp < 4:
        return np.zeros(0, dtype=np.int8)
    m = _SUBCHUNKS
    while npp % m:
        m -= 1

    # band_acquire reports the circular correlation lag (gps-sdr-sim
    # convention); the replica's start chip is its complement.
    cp0 = (code_len - float(code_phase_chips)) % code_len

    n_per_sym = int(round(npp / max(spp, 1)))          # primary periods / symbol

    # Symbols start on a primary-code epoch (ICD), which sits at the
    # acquisition lag, not at sample 0. Start every integration window
    # there: a window opened at sample 0 straddles a symbol boundary
    # (for L2C, one symbol per code period, the whole window straddles it)
    # and sums through every data sign flip.
    e0 = int(math.ceil((code_len - cp0) % code_len / chip_hz * fs))

    def _build(off_samp, sec_roll, nsym_cap=None):
        off_samp += e0
        base = iq[off_samp:]
        nsym = base.size // npp
        if nsym_cap is not None:
            nsym = min(nsym, nsym_cap)
        # replica chip at the (shifted) window start
        c_start = (cp0 + chip_hz * off_samp / fs) % code_len
        # Symbols per chunk: an L5 capture is ~750 M samples, and building
        # the replica/wipe for all of it at once took tens of GB. Chunks
        # use absolute time, so phase and code stay continuous across them.
        per = max(1, _CHUNK_SAMPLES // npp)

        def _subcorr(dopp0, drift=0.0, nlim=None):
            n_use = nsym if nlim is None else min(nsym, nlim)
            out = np.empty((n_use, m), dtype=np.complex128)
            for k0 in range(0, n_use, per):
                k1 = min(n_use, k0 + per)
                tt = np.arange(k0 * npp, k1 * npp, dtype=np.float64) / fs
                ramp = dopp0 * tt + 0.5 * drift * tt * tt
                wipe = np.exp(-2j * np.pi * ramp)
                cph = c_start + chip_hz * tt + (chip_hz / carrier_ctr_hz) * ramp
                rep = code[np.floor(cph).astype(np.int64) % code_len]
                if sec is not None and sec_rate_hz > 0.0:
                    sidx = (np.floor(tt * sec_rate_hz).astype(np.int64)
                            + sec_roll) % sec.size
                    rep = rep * sec[sidx]
                seg = base[k0 * npp:k1 * npp]
                out[k0:k1] = (seg * wipe * rep).reshape(
                    k1 - k0, m, npp // m).sum(axis=2)
            return out

        return nsym, None, _subcorr

    d = float(dopp_hz)

    # --- 0. coarse Doppler pull-in, before anything that needs the symbol
    # boundary. Squaring the sub-symbol partials cancels the data and
    # secondary-code signs (every partial lies inside one primary period,
    # and the windows start on a code epoch), so this works at any boundary
    # hypothesis. It must come first: at the raw acquisition Doppler the
    # carrier spins through the boundary-search prefix and that search
    # picks at random.
    ns0, _tt0, _sc0 = _build(0, 0)
    if ns0 < 4:
        return np.zeros(0, dtype=np.int8)
    pn = min(ns0, 200)
    for _ in range(4):
        sq = (_sc0(d, nlim=pn).reshape(-1)) ** 2
        fr = np.fft.fftfreq(sq.size, d=sym_s / m)
        step = float(fr[np.argmax(np.abs(np.fft.fft(sq)))]) / 2.0
        d += step
        if abs(step) < 1.0:
            break

    # --- 1. secondary-code alignment and symbol boundary. Acquisition pins
    # the primary code phase only within one primary period. The secondary
    # code (NH10 on L5, CS20 on E5a, "00010" on B2a) is synchronised to the
    # symbol in the ICDs, so data signs only change on a secondary-period
    # edge. Find that edge first, from one-per-epoch partials: matching the
    # secondary code over each period sums in phase only at the true
    # alignment, whatever the data. Then pick the symbol boundary among the
    # few candidate periods by coherent per-symbol energy. (The old search
    # tied the secondary roll to the boundary guess and scored it by
    # real-axis concentration, which a wrong roll does not lower, so it
    # picked at random on most satellites.)
    S = sec.size if sec is not None else 0
    if (sec is not None and n_per_sym > 1 and n_per_sym % S == 0
            and m % n_per_sym == 0):
        pe = min(ns0, 100)
        ep = _sc0(d, nlim=pe).reshape(pe * n_per_sym,
                                      m // n_per_sym).sum(axis=1)
        j = np.arange(ep.size)
        raw = ep * sec[j % S]            # undo the roll-0 wipe _build applied
        best_a, best_e = 0, -1.0
        for a0 in range(S):
            q = (raw.size - a0) // S
            v = (raw[a0:a0 + q * S].reshape(q, S) * sec).sum(axis=1)
            e = float(np.mean(np.abs(v) ** 2))
            if e > best_e:
                best_a, best_e = a0, e
        aligned = raw * sec[(j - best_a) % S]
        best = (-1.0, best_a)
        for off in range(best_a, n_per_sym, S):
            q = (aligned.size - off) // n_per_sym
            v = aligned[off:off + q * n_per_sym].reshape(
                q, n_per_sym).sum(axis=1)
            e = float(np.mean(np.abs(v) ** 2))
            if e > best[0]:
                best = (e, off)
        off = best[1]
        sec_roll = (off - best_a) % S    # == 0: window opens on chip 0
    elif sec is not None and n_per_sym > 1:
        best = (-1.0, 0)
        for off in range(n_per_sym):
            ns, _tt, _sc = _build(off * spp, off % S, nsym_cap=60)
            if ns < 8:
                continue
            sc = float(np.mean(np.abs(_sc(d).sum(axis=1)) ** 2))
            if sc > best[0]:
                best = (sc, off)
        off = best[1]
        sec_roll = off % S
    else:
        off, sec_roll = 0, 0
    nsym, tt, _subcorr = _build(off * spp, sec_roll)
    if nsym < 4:
        return np.zeros(0, dtype=np.int8)

    # --- 1b. fine lock: a linear fit of the data-wiped phase.
    pn = min(nsym, 200)
    for _ in range(6):
        corr = _subcorr(d, nlim=pn).sum(axis=1)
        slope = np.polyfit(np.arange(pn, dtype=np.float64),
                           np.unwrap(np.angle(corr ** 2)), 1)[0] / 2.0
        dfr = slope / (2.0 * np.pi * sym_s)
        d += dfr
        if abs(dfr) < 0.02:
            break

    # --- 2. Doppler drift from the per-block residual carrier frequency,
    # then rebuild the replica as a linear-frequency chirp.
    B = 50

    def _block_freqs(p):
        nbk = max(1, p.shape[0] // B)
        out = np.zeros(nbk)
        for k in range(nbk):
            sq = (p[k * B:(k + 1) * B].reshape(-1)) ** 2
            fr = np.fft.fftfreq(sq.size, d=sym_s / m)
            out[k] = float(fr[np.argmax(np.abs(np.fft.fft(sq)))]) / 2.0
        return out, nbk

    p_full = _subcorr(d)
    fblk, nb = _block_freqs(p_full)
    if nb > 3:
        bt_s = (np.arange(nb) + 0.5) * B * sym_s
        c1, c0 = np.polyfit(bt_s, fblk, 1)
        d += float(c0)
        p_full = _subcorr(d, float(c1))

    # --- 3. residual carrier phase: low-order polyfit of the data-wiped
    # phase, de-rotated before the hard decision.
    si = np.arange(nsym, dtype=np.float64)
    corr = p_full.sum(axis=1)
    deg = min(4, nsym - 1) if nsym >= 5 else 1
    coef = np.polyfit(si, np.unwrap(np.angle(corr ** 2)), deg) / 2.0
    corr = corr * np.exp(-1j * np.polyval(coef, si))
    corr = corr * np.exp(-1j * 0.5 * np.angle(np.sum(corr ** 2)))

    return (np.real(corr) < 0).astype(np.int8)


def demod_symbols(iq, fs, prn, *, dopp_hz=None, code_phase_chips=None):
    """Hard {0,1} convolutional symbols (50 sym/s) from a GPS L2C capture."""
    from backend.analysis import band_acquire

    iq = np.asarray(iq)
    if iq.dtype != np.complex64:
        iq = iq.astype(np.complex64)
    if dopp_hz is None or code_phase_chips is None:
        a = band_acquire.acquire_l2c(iq, fs, prn)
        dopp_hz = a["doppler_hz"] if dopp_hz is None else dopp_hz
        code_phase_chips = (a["code_phase_chips"] if code_phase_chips is None
                            else code_phase_chips)
    # CM/CL TDM: track the CM slots (code phase given in CM chips).
    return _demod(iq, fs, band_acquire.l2c_cm_replica(prn),
                  chip_hz=band_acquire.L2C_TDM_CHIP_HZ,
                  code_len=band_acquire.L2C_TDM_LEN,
                  carrier_ctr_hz=_L2_CTR_HZ, dopp_hz=dopp_hz,
                  code_phase_chips=2.0 * float(code_phase_chips))


def demod_symbols_l5(iq, fs, prn, *, dopp_hz=None, code_phase_chips=None):
    """Hard {0,1} CNAV symbols (50 sym/s) from a GPS / QZSS L5 capture: the
    I5 primary at 10.23 Mcps with the NH10 secondary wiped off. Same
    message set and downstream decode as L2C."""
    from backend.analysis import band_acquire
    from backend.synth import _lib

    iq = np.asarray(iq)
    if not np.iscomplexobj(iq):
        iq = iq.astype(np.complex64)
    elif iq.dtype == np.complex128:
        iq = iq.astype(np.complex64)
    i5, _q5 = _lib.code_l5(int(prn))
    i5 = i5.astype(np.float64)
    if dopp_hz is None or code_phase_chips is None:
        a = band_acquire.acquire(iq, fs, i5, chip_hz=_L5_CHIP_HZ,
                                 code_len=_L5_LEN, dopp_hz=6000.0,
                                 dopp_step=100.0, nperiods=1)
        dopp_hz = a["doppler_hz"] if dopp_hz is None else dopp_hz
        code_phase_chips = (a["code_phase_chips"] if code_phase_chips is None
                            else code_phase_chips)
    return _demod(iq, fs, i5, chip_hz=_L5_CHIP_HZ, code_len=_L5_LEN,
                  carrier_ctr_hz=_L5_CTR_HZ, dopp_hz=dopp_hz,
                  code_phase_chips=code_phase_chips,
                  sec=_NH10, sec_rate_hz=1000.0)
