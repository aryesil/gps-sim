"""Clean-channel NavIC (IRNSS) L5-SPS decoder -- the inverse of
``backend.analysis.navic_encode``.

16-bit 0xEB90 sync-word scan (uncoded, searched directly in the hard
symbol stream), 73x8 block de-interleave, rate-1/2 K=7 Viterbi-free
decode (fresh register per subframe -- NavIC's tail bits make it a block
code, not continuous like CNAV), CRC-24Q check, subframe 1/2 field
extraction via the shared ``_navic_layout`` table, and Keplerian
ephemeris reconstruction.

``demod_symbols`` pulls the 50 sym/s symbol stream out of a NavIC L5-SPS
IQ capture: the 1023-chip/1.023 Mcps primary code has a 1 ms period, 20
of which make one nav symbol (20 ms) -- structurally the same folding as
GPS L1 C/A, but (unlike L1 C/A's known-at-acquisition Z-count framing)
the 20-period symbol boundary is not resolved by acquisition alone, so it
is found the same data-blind way ``cnav_decode``/``glo_str_decode`` do:
lock Doppler first (misalignment-insensitive, since each hypothesis
squares 1 ms sub-chunks before summing across the symbol), then pick the
boundary hypothesis whose data-wiped correlation concentrates best.
"""
from __future__ import annotations

import math

import numpy as np

from backend.analysis import _navic_layout as L
from backend.analysis._crc import crc24q

_G1 = 0o171
_G2 = 0o133
_SC = math.pi                                # semicircle -> radian


def _parity(x: int) -> int:
    return bin(x).count("1") & 1


def viterbi_free_decode(sym) -> list[int]:
    """Exact inverse of navic_encode._Conv on a clean channel (from reg
    0) -- one call decodes exactly one subframe's 584 symbols (the coder
    is reset to 0 at the start of every subframe, so there is no
    cross-subframe state to carry here either)."""
    reg = 0
    out: list[int] = []
    sym = [int(s) & 1 for s in sym]
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


def _deinterleave(symbols) -> list[int]:
    """Inverse of navic_encode._interleave: undo the 73x8 write-columns /
    read-rows block interleave."""
    t2 = np.asarray(list(symbols), dtype=np.int64).reshape(
        L.INTERLEAVE_ROWS, L.INTERLEAVE_COLS)      # (8, 73), row-major == readout order
    return t2.T.reshape(-1).tolist()                # (73, 8) row-major == write order


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


def _decode_payload(sf_id: int, payload: list[int]) -> dict:
    out: dict = {}
    off = 0
    for name, kind, scale, nbits in L.LAYOUT[sf_id]:
        out[name] = _field(payload, off, kind, scale, nbits)
        off += nbits
    return out


_SYNC_BITS_MSB = [(L.SYNC_WORD >> (L.SYNC_BITS - 1 - i)) & 1
                  for i in range(L.SYNC_BITS)]


def find_subframes(sym01) -> list[dict]:
    """Scan a hard-decision {0,1} symbol sequence for sync-word-framed,
    CRC-valid NavIC subframes. Handles a globally inverted stream (BPSK
    sign ambiguity). Returns one dict per hit: ``offset`` (into the input
    sequence, at the sync word), ``inverted``, ``subframe_id``, ``tow``,
    ``fields`` (decoded Table 11/12 dict)."""
    sym01 = [int(x) & 1 for x in sym01]
    step = L.SYNC_BITS + L.FEC_SYMBOLS
    hits: list[dict] = []
    for flip in (0, 1):
        stream = [x ^ flip for x in sym01]
        i = 0
        n = len(stream)
        while i + step <= n:
            if stream[i:i + L.SYNC_BITS] != _SYNC_BITS_MSB:
                i += 1
                continue
            coded = _deinterleave(stream[i + L.SYNC_BITS:i + step])
            body = viterbi_free_decode(coded)
            if len(body) != L.SUBFRAME_BITS:
                i += 1
                continue
            msg = body[:L.HEADER_BITS + L.DATA_BITS]
            if crc24q(msg) != body[L.HEADER_BITS + L.DATA_BITS:
                                   L.HEADER_BITS + L.DATA_BITS + L.CRC_BITS]:
                i += 1
                continue
            sfid_code = _u(body, 27, 2)
            sf_id = {0b00: 1, 0b01: 2}.get(sfid_code)
            if sf_id is None or sf_id not in L.LAYOUT:
                i += 1
                continue
            payload = msg[L.HEADER_BITS:]
            hits.append({
                "offset": i, "inverted": bool(flip),
                "subframe_id": sf_id, "tow": _u(body, 8, 17),
                "fields": _decode_payload(sf_id, payload),
            })
            i += step
        if hits:
            return hits
    return []


def reconstruct_ephemeris(hits: list[dict]) -> dict:
    """Build a GPS-Keplerian-compatible ephemeris dict from CRC-valid
    subframe 1 + 2 hits (last-seen-wins per subframe id, matching
    cnav_decode's own by-type-dict convention)."""
    by_id = {h["subframe_id"]: h["fields"] for h in hits}
    if not ({1, 2} <= set(by_id)):
        raise ValueError("need NavIC subframes 1 and 2")
    f1, f2 = by_id[1], by_id[2]
    rec = {"system": "I"}
    rec["gps_week"] = int(f1["wn"])
    rec["af0"] = f1["af0"]
    rec["af1"] = f1["af1"]
    rec["af2"] = f1["af2"]
    rec["toc"] = f1["toc"]
    rec["tgd"] = f1["tgd"]
    rec["delta_n"] = f1["delta_n"] * _SC
    rec["idot"] = f1["idot"] * _SC
    for k in ("cuc", "cus", "cic", "cis", "crc", "crs"):
        rec[k] = f1[k]
    rec["m0"] = f2["m0"] * _SC
    rec["toe"] = f2["toe"]
    rec["e"] = f2["e"]
    rec["sqrtA"] = f2["sqrt_a"]
    rec["omega0"] = f2["omega0"] * _SC
    rec["omega"] = f2["omega"] * _SC
    rec["omega_dot"] = f2["omega_dot"] * _SC
    rec["i0"] = f2["i0"] * _SC
    return rec


# --- IQ -> hard nav symbols --------------------------------------------

_NAVIC_LEN = 1023
_NAVIC_CHIP_HZ = 1.023e6
_SYM_S = 0.02                                   # one nav symbol, 50 sym/s
_L5_CTR_HZ = 1_176_450_000.0
_SUBCHUNKS = 20


def _concentration(corr):
    """Fraction of correlator energy on the real axis after the best
    constant-phase rotation -- ~1 for a locked BPSK stream, ~0.5 for a
    spinning one. Data-blind (corr**2 cancels the +/-1 symbol)."""
    c2 = np.sum(corr ** 2)
    aligned = corr * np.exp(-1j * 0.5 * np.angle(c2))
    return float(np.sum(np.real(aligned) ** 2) / np.sum(np.abs(corr) ** 2))


def _demod(iq, fs, code, *, dopp_hz, code_phase_chips):
    """NavIC nav-symbol tracker: 1023-chip/1.023 Mcps primary, 20
    primary periods per 50 sps nav symbol, no secondary code."""
    iq = np.asarray(iq)
    if not np.iscomplexobj(iq):
        iq = iq.astype(np.complex64)
    code = np.asarray(code, dtype=np.float64)

    npp = int(round(fs * _SYM_S))
    spp = int(round(fs * (_NAVIC_LEN / _NAVIC_CHIP_HZ)))   # one primary period
    if npp <= 0 or iq.size // npp < 4:
        return np.zeros(0, dtype=np.int8)
    m = _SUBCHUNKS
    while npp % m:
        m -= 1

    cp0 = (_NAVIC_LEN - float(code_phase_chips)) % _NAVIC_LEN
    n_per_sym = int(round(npp / max(spp, 1)))

    def _build(off_samp, nsym_cap=None):
        base = iq[off_samp:]
        nsym = base.size // npp
        if nsym_cap is not None:
            nsym = min(nsym, nsym_cap)
        tt = np.arange(nsym * npp, dtype=np.float64) / fs
        seg = base[:nsym * npp]

        def _subcorr(dopp0, drift=0.0):
            ramp = dopp0 * tt + 0.5 * drift * tt * tt
            wipe = np.exp(-2j * np.pi * ramp)
            cph = cp0 + _NAVIC_CHIP_HZ * tt + (_NAVIC_CHIP_HZ / _L5_CTR_HZ) * ramp
            idx = (np.floor(cph).astype(np.int64)) % _NAVIC_LEN
            rep = code[idx]
            prod = (seg * wipe * rep).reshape(nsym, m, npp // m)
            return prod.sum(axis=2)

        return nsym, tt, _subcorr

    # --- 0. Doppler lock first, at an arbitrary (off=0) symbol grouping:
    # every hypothesis squares 1 ms sub-chunks before summing across the
    # 20 ms symbol window, so a data-sign transition at one of the other
    # 19 possible boundaries cannot corrupt this stage (same reasoning as
    # glo_str_decode's meander-boundary-after-Doppler-lock ordering).
    nsym, tt, _subcorr = _build(0)
    if nsym < 4:
        return np.zeros(0, dtype=np.int8)
    d = float(dopp_hz)
    pn = min(nsym, 200)
    # Pull-in runs on a capped ``pn``-symbol prefix, not the full capture:
    # _subcorr's cost scales with the whole array it was built over, and
    # these 9 iterations only ever look at the first ``pn`` rows of its
    # result, so building a separate small closure here (instead of
    # slicing the full-capture one after computing it) turns a capture-
    # sized cost into a fixed ~200-symbol one on every iteration.
    _, _, _subcorr_pn = _build(0, nsym_cap=pn)
    for _ in range(3):
        sq = (_subcorr_pn(d).reshape(-1)) ** 2
        fr = np.fft.fftfreq(sq.size, d=_SYM_S / m)
        step = float(fr[np.argmax(np.abs(np.fft.fft(sq)))]) / 2.0
        d += step
        if abs(step) < 1.0:
            break
    for _ in range(6):
        corr = _subcorr_pn(d).sum(axis=1)
        slope = np.polyfit(np.arange(pn, dtype=np.float64),
                           np.unwrap(np.angle(corr ** 2)), 1)[0] / 2.0
        dfr = slope / (2.0 * np.pi * _SYM_S)
        d += dfr
        if abs(dfr) < 0.02:
            break

    B = 50

    def _block_freqs(p):
        nbk = max(1, p.shape[0] // B)
        out = np.zeros(nbk)
        for k in range(nbk):
            sq = (p[k * B:(k + 1) * B].reshape(-1)) ** 2
            fr = np.fft.fftfreq(sq.size, d=_SYM_S / m)
            out[k] = float(fr[np.argmax(np.abs(np.fft.fft(sq)))]) / 2.0
        return out, nbk

    p_full = _subcorr(d)
    fblk, nb = _block_freqs(p_full)
    drift = 0.0
    if nb > 3:
        bt_s = (np.arange(nb) + 0.5) * B * _SYM_S
        c1, c0 = np.polyfit(bt_s, fblk, 1)
        d += float(c0)
        drift = float(c1)

    # --- 1. now that Doppler (+ drift) is locked, search the 20-period
    # symbol boundary by data-blind concentration of a short prefix.
    def _decode_at(off):
        """Full step-2 decode (residual-phase polyfit + hard decision)
        for one boundary hypothesis."""
        nsym, _tt, subcorr = _build(off * spp)
        if nsym < 4:
            return None
        p_full = subcorr(d, drift)
        si = np.arange(nsym, dtype=np.float64)
        corr = p_full.sum(axis=1)
        deg = min(4, nsym - 1) if nsym >= 5 else 1
        coef = np.polyfit(si, np.unwrap(np.angle(corr ** 2)), deg) / 2.0
        corr = corr * np.exp(-1j * np.polyval(coef, si))
        corr = corr * np.exp(-1j * 0.5 * np.angle(np.sum(corr ** 2)))
        return (np.real(corr) < 0).astype(np.int8)

    if n_per_sym > 1:
        scored = []
        for off in range(n_per_sym):
            ns, _tt, _sc = _build(off * spp, nsym_cap=60)
            if ns < 8:
                continue
            sc = _concentration(_sc(d, drift).sum(axis=1))
            scored.append((sc, off))
        scored.sort(key=lambda t: -t[0])
        # Concentration alone can rank a boundary whose data-bit
        # transition falls inside the correlation window above a clean
        # one -- measured directly on two of five SVs in this suite's
        # own fixture, where the top-concentration offset decoded only
        # a partial/no subframe while a lower-ranked one decoded both
        # cleanly. Confirm each candidate (best-first) against the real
        # sync-word/CRC check -- both subframes 1 and 2, since a single
        # decoded subframe is exactly the partial-corruption case this
        # is guarding against, not a valid lock -- rather than trusting
        # the score blindly. If none validate fully, fall back to
        # whichever candidate decoded the most subframes (best-first on
        # ties), e.g. a genuinely weak SV that never gets both.
        sym, best_partial, best_partial_n = None, None, -1
        for _sc, off in scored:
            cand = _decode_at(off)
            if cand is None:
                continue
            hits = find_subframes(cand.tolist())
            sfids = {h["subframe_id"] for h in hits}
            if {1, 2} <= sfids:
                sym = cand
                break
            if len(sfids) > best_partial_n:
                best_partial, best_partial_n = cand, len(sfids)
        if sym is None:
            sym = best_partial if best_partial is not None else (
                _decode_at(scored[0][1]) if scored else None)
    else:
        sym = _decode_at(0)

    if sym is None:
        return np.zeros(0, dtype=np.int8)
    return sym


def demod_symbols(iq, fs, prn, *, dopp_hz=None, code_phase_chips=None):
    """Hard {0,1} nav symbols (50 sym/s) from a NavIC L5-SPS capture."""
    from backend.analysis import band_acquire
    from backend.synth import _lib

    iq = np.asarray(iq)
    if not np.iscomplexobj(iq):
        iq = iq.astype(np.complex64)
    elif iq.dtype == np.complex128:
        iq = iq.astype(np.complex64)
    code = _lib.code_navic(int(prn)).astype(np.float64)
    if dopp_hz is None or code_phase_chips is None:
        a = band_acquire.acquire(iq, fs, code, chip_hz=_NAVIC_CHIP_HZ,
                                 code_len=_NAVIC_LEN, dopp_step=100.0)
        dopp_hz = a["doppler_hz"] if dopp_hz is None else dopp_hz
        code_phase_chips = (a["code_phase_chips"] if code_phase_chips is None
                            else code_phase_chips)
    return _demod(iq, fs, code, dopp_hz=dopp_hz,
                 code_phase_chips=code_phase_chips)
