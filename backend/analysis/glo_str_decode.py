"""GLONASS G1/G2 noisy-IQ string demodulator.

Acquires the shared 511-chip m-sequence (common to every FDMA slot) at a
given channel offset, carrier-tracks the 100 sym/s meander stream the same
way :mod:`backend.analysis.cnav_decode` tracks CNAV (chirp Doppler + drift
+ residual-phase polyfit), then hunts for Hamming-valid GLONASS string
framing. Sync uses the KX Hamming check
(:func:`backend.analysis.glo_str_encode.hamming_check`) rather than the
30-chip time mark: the string start (one of 100 bit-pair slots per 2 s
string) and the 2-way meander parity (which symbol of each bit-pair is
"first") are searched jointly, picking whichever combination yields the
most Hamming-valid strings.

Ephemeris reconstruction is :func:`glo_str_encode.reconstruct_ephemeris`.
The state vector is referenced to the broadcast t_b (string 2); the
receiver resolves it with :func:`glo_str_encode.toe_ref_from_tb`.
"""
from __future__ import annotations

import numpy as np

from backend.analysis import glo_str_encode as G

_CODE_LEN = 511
_CHIP_HZ = 511_000.0
_SYM_S = 0.01                      # one meander symbol (100 sym/s)
_PERIODS_PER_SYM = 10              # code periods (1 ms each) per symbol
_SUBCHUNKS = 10


def _concentration(corr):
    """Fraction of correlator energy on the real axis after the best
    constant-phase rotation -- ~1 for a locked stream, ~0.5 for a spinning
    one. Data-blind (corr**2 cancels the +/-1 symbol)."""
    c2 = np.sum(corr ** 2)
    aligned = corr * np.exp(-1j * 0.5 * np.angle(c2))
    return float(np.sum(np.real(aligned) ** 2) / np.sum(np.abs(corr) ** 2))


def _demod(iq, fs, code, *, carrier_ctr_hz, center_hz, dopp_hz,
           code_phase_chips):
    """Hard {0,1} meander-symbol decisions (100 Hz) from a GLONASS G1/G2
    capture.

    ``center_hz`` is this slot's FDMA carrier offset from the band's
    nominal centre (the IQ baseband reference); ``band_acquire.acquire``'s
    own ``center_hz`` search excludes it from the ``dopp_hz`` it returns,
    so it is folded back in here, added to the residual Doppler in the
    wipe/ramp. ``carrier_ctr_hz`` (nominal centre + offset, the SV's true
    RF frequency) scales the code-Doppler coupling the same way
    ``cnav_decode._demod`` does for its own single-frequency bands.
    """
    iq = np.asarray(iq)
    if not np.iscomplexobj(iq):
        iq = iq.astype(np.complex64)
    code = np.asarray(code, dtype=np.float64)

    spp = int(round(fs * (_CODE_LEN / _CHIP_HZ)))        # one code period
    npp = spp * _PERIODS_PER_SYM                         # one meander symbol
    if npp <= 0 or iq.size // npp < 4:
        return np.zeros(0, dtype=np.int8)
    m = _SUBCHUNKS
    while npp % m:
        m -= 1

    # band_acquire reports the circular correlation lag (gps-sdr-sim
    # convention); the replica's start chip is its complement.
    cp0 = (_CODE_LEN - float(code_phase_chips)) % _CODE_LEN

    def _build(off_samp, nsym_cap=None):
        base = iq[off_samp:]
        nsym = base.size // npp
        if nsym_cap is not None:
            nsym = min(nsym, nsym_cap)
        tt = np.arange(nsym * npp, dtype=np.float64) / fs
        seg = base[:nsym * npp]

        def _subcorr(dopp0, drift=0.0):
            chirp = 0.5 * drift * tt * tt
            wipe = np.exp(-2j * np.pi * ((center_hz + dopp0) * tt + chirp))
            # Code-Doppler coupling scales with this SV's true (velocity
            # -induced) Doppler only. ``center_hz`` is this slot's fixed
            # FDMA design offset -- part of the RF carrier to wipe, but not
            # a rate anything is moving at -- so it must not leak into the
            # code chip rate the way it correctly does into the wipe above.
            # Folding it in here (as the wipe's ramp did before this fix)
            # left every non-zero FDMA channel's replica drifting tens of
            # code periods over a multi-second capture while channel 0
            # (center_hz == 0) happened to stay correct by coincidence.
            code_ramp = dopp0 * tt + chirp
            cph = cp0 + _CHIP_HZ * tt + (_CHIP_HZ / carrier_ctr_hz) * code_ramp
            idx = (np.floor(cph).astype(np.int64)) % _CODE_LEN
            rep = code[idx]
            prod = (seg * wipe * rep).reshape(nsym, m, npp // m)
            return prod.sum(axis=2)

        return nsym, tt, _subcorr

    # --- 0. lock Doppler on an arbitrary (off=0) grouping. Every step here
    # squares each 1 ms sub-chunk *before* summing across a symbol-length
    # window (see ``_subcorr``'s per-subchunk output and ``.reshape(-1)``
    # below), and a meander data transition can only fall on a 10-subchunk
    # (10 ms) boundary -- so no individual 1 ms subchunk ever straddles one.
    # That makes this whole stage insensitive to which of the 10 possible
    # symbol groupings we start from, so it can run before the boundary is
    # known at all.
    nsym0, tt0, _subcorr0 = _build(0)
    if nsym0 < 4:
        return np.zeros(0, dtype=np.int8)
    d = float(dopp_hz)

    # coarse squared-signal pull-in (alias-free out to +/- m/(2*_SYM_S) Hz),
    # then a linear data-wiped-phase fit.
    pn = min(nsym0, 400)
    for _ in range(3):
        sq = (_subcorr0(d)[:pn].reshape(-1)) ** 2
        fr = np.fft.fftfreq(sq.size, d=_SYM_S / m)
        step = float(fr[np.argmax(np.abs(np.fft.fft(sq)))]) / 2.0
        d += step
        if abs(step) < 1.0:
            break
    for _ in range(6):
        corr = _subcorr0(d)[:pn].reshape(-1) ** 2
        slope = np.polyfit(np.arange(corr.size, dtype=np.float64),
                           np.unwrap(np.angle(corr)), 1)[0] / 2.0
        dfr = slope / (2.0 * np.pi * (_SYM_S / m))
        d += dfr
        if abs(dfr) < 0.02:
            break

    # Doppler drift from the per-block residual carrier frequency.
    B = 50

    def _block_freqs(_subcorr, dopp0):
        p = _subcorr(dopp0)
        nbk = max(1, p.shape[0] // B)
        out = np.zeros(nbk)
        for k in range(nbk):
            sq = (p[k * B:(k + 1) * B].reshape(-1)) ** 2
            fr = np.fft.fftfreq(sq.size, d=_SYM_S / m)
            out[k] = float(fr[np.argmax(np.abs(np.fft.fft(sq)))]) / 2.0
        return out, nbk

    fblk, nb = _block_freqs(_subcorr0, d)
    drift = 0.0
    if nb > 3:
        bt_s = (np.arange(nb) + 0.5) * B * _SYM_S
        drift, c0 = np.polyfit(bt_s, fblk, 1)
        d += float(c0)

    # --- 1. NOW search the meander-symbol boundary: acquisition pins the
    # primary code phase only within one 1 ms code period, so a 10-period
    # (10 ms) integration window straddling the true symbol boundary sums
    # through a data sign flip. Score each of the 10 boundary hypotheses by
    # the data-blind concentration of a short prefix -- at the Doppler this
    # module just locked, not the raw acquisition estimate, since a
    # residual of even a few tens of Hz spins the phase through several
    # cycles over a multi-symbol prefix and swamps the concentration metric
    # for every hypothesis equally.
    best = (-1.0, 0)
    for off in range(_PERIODS_PER_SYM):
        ns, _tt, _sc = _build(off * spp, nsym_cap=80)
        if ns < 8:
            continue
        sc = _concentration(_sc(d, drift).sum(axis=1))
        if sc > best[0]:
            best = (sc, off)
    off = best[1]
    nsym, tt, _subcorr = _build(off * spp)
    if nsym < 4:
        return np.zeros(0, dtype=np.int8)

    # --- 2. rebuild the replica at the correct symbol boundary, as the
    # linear-frequency chirp the drift fit found.
    p_full = _subcorr(d, drift)

    # --- 3. residual carrier phase: low-order polyfit of the data-wiped
    # phase, de-rotated before the hard decision.
    si = np.arange(nsym, dtype=np.float64)
    corr = p_full.sum(axis=1)
    deg = min(4, nsym - 1) if nsym >= 5 else 1
    coef = np.polyfit(si, np.unwrap(np.angle(corr ** 2)), deg) / 2.0
    corr = corr * np.exp(-1j * np.polyval(coef, si))
    corr = corr * np.exp(-1j * 0.5 * np.angle(np.sum(corr ** 2)))

    return (np.real(corr) < 0).astype(np.int8)


def demod_symbols(iq, fs, *, nominal_ctr_hz, offset_hz, dopp_hz=None,
                  code_phase_chips=None):
    """Hard {0,1} meander-symbol decisions (100 Hz) from a GLONASS G1/G2
    capture at one FDMA slot.

    ``offset_hz`` is that slot's FDMA carrier offset from the band's
    nominal centre (:func:`backend.synth.signals.glo_channel_offset_hz` --
    562.5 kHz/channel on G1, 437.5 kHz on G2); ``nominal_ctr_hz`` is the
    band's own nominal centre (1602.0 MHz / 1246.0 MHz). Acquires when
    ``dopp_hz``/``code_phase_chips`` aren't supplied.
    """
    from backend.analysis import band_acquire
    from backend.synth.engine import _glo_g1_code

    iq = np.asarray(iq)
    if not np.iscomplexobj(iq):
        iq = iq.astype(np.complex64)
    code = _glo_g1_code().astype(np.float64)
    if dopp_hz is None or code_phase_chips is None:
        a = band_acquire.acquire(iq, fs, code, chip_hz=_CHIP_HZ,
                                 code_len=_CODE_LEN, center_hz=offset_hz,
                                 dopp_step=50.0, nperiods=10)
        dopp_hz = a["doppler_hz"] if dopp_hz is None else dopp_hz
        code_phase_chips = (a["code_phase_chips"] if code_phase_chips is None
                            else code_phase_chips)
    return _demod(iq, fs, code, carrier_ctr_hz=nominal_ctr_hz + offset_hz,
                  center_hz=offset_hz, dopp_hz=dopp_hz,
                  code_phase_chips=code_phase_chips)


def sync_and_decode(sym01) -> dict[int, list[int]]:
    """Map string number -> its 76 data bits (``data[:4]`` the string
    number; see ``glo_str_encode.build_string``) out of raw meander-symbol
    hard decisions.

    A string is 200 symbols: 85 relative-coded data bits as meander pairs,
    then the 30-chip time mark. Neither the 2-way meander parity nor the
    string start is known after tracking, so both are searched jointly
    (string start over the 100 bit-pair slots of one string), keeping the
    combination with the most KX-valid strings. The relative code is undone
    differentially, which also makes the result immune to a carrier-phase
    sign flip.
    """
    sym = [int(s) & 1 for s in sym01]
    per = G._STR_SYMS // 2                     # bit-pair slots per string
    best_n, best_frame = -1, {}
    for parity in (0, 1):
        bits = sym[parity::2]
        for off in range(per):
            n_ok = 0
            frame: dict[int, list[int]] = {}
            i = off
            while i + G._STR_BITS <= len(bits):
                s = G._derelative(bits[i:i + G._STR_BITS])
                if G.hamming_check(s):
                    n_ok += 1
                    data = G._string_data(s)
                    sidx = int("".join(map(str, data[:4])), 2)
                    frame.setdefault(sidx, data)
                i += per
            if n_ok > best_n:
                best_n, best_frame = n_ok, frame
    return best_frame
