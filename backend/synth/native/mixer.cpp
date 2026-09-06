// backend/synth/native/mixer.cpp
#include "mixer.hpp"
#include "nco.hpp"
#include <cmath>

namespace gs {

#if defined(__x86_64__) && defined(__GNUC__) && !defined(__APPLE__)
__attribute__((target_clones("default", "sse4.2", "avx2", "avx2,fma")))
#endif
void mix_block(const SvChannel *__restrict svs, int nsv, double fs,
               uint64_t sample0, int n, float *__restrict iq) {
    const double dt = 1.0 / fs;
    for (int s = 0; s < nsv; ++s) {
        const SvChannel &sv = svs[s];
        // Carrier NCO seeded so that absolute sample `sample0` has the exact
        // starting phase (continuity across blocks).
        Nco carr;
        carr.set_freq(sv.carrier_freq_hz, fs);
        double abs_t0 = sample0 * dt;
        double ph0 = sv.carrier_phase0_rad
                     + 2.0 * M_PI * sv.carrier_freq_hz * abs_t0;
        ph0 = std::fmod(ph0, 2.0 * M_PI);
        if (ph0 < 0) ph0 += 2.0 * M_PI;
        carr.phase = static_cast<uint32_t>(ph0 / (2.0 * M_PI) * 4294967296.0);

        const double chip_rate = sv.code_rate_hz + sv.code_doppler_hz;
        const double g = sv.gain;
        const int L = sv.code_len;
        const int8_t *__restrict code = sv.code;
        const NavSource nav = sv.nav;
        // Code-phase convention: geometry.observables reports the epoch delay
        // rho/c in chips, and inspector.acquire / inspector.compare treat that
        // value as the correlation lag directly (gps-sdr-sim convention). The
        // correlation lag is the complement of the chip index seeded at
        // sample 0, so seed L - (code_phase0_chips mod L) here.
        double eff = std::fmod(sv.code_phase0_chips, static_cast<double>(L));
        if (eff < 0) eff += L;
        eff = L - eff;
        if (eff >= L) eff -= L;

        // BOC(1,1): the sub-carrier is code-phase-locked -- two half-chips per
        // code chip. Deriving the sign from the running code phase `cp` (which
        // already carries code Doppler and is absolute-sample-seeded) keeps the
        // sub-carrier coherent with the code across Doppler, streaming blocks and
        // parallel chunks (final review B1 discipline). An independent nominal-
        // rate NCO slipped ~code_doppler chips/s vs the code.
        const bool use_boc = sv.sub_carrier_hz > 0.0;
        const int8_t *__restrict sec = sv.sec_code;
        const int sec_len = sv.sec_len;

        for (int k = 0; k < n; ++k) {
            double t = abs_t0 + k * dt;
            // `t` is already absolute (abs_t0 + k*dt); advancing by absolute `t`
            // keeps the code NCO continuous across streaming blocks and across
            // parallel chunks. Using (t - abs_t0) here dropped the absolute term
            // and restarted the code phase at `eff` every call (final review B1).
            double cp = eff + chip_rate * t;
            long ci = static_cast<long>(cp) % L;
            if (ci < 0) ci += L;
            float chip = static_cast<float>(code[ci]);
            if (sec_len > 0) {
                // One secondary chip spans exactly one primary code period, so
                // index by elapsed primary periods derived from the absolute
                // code phase `cp` -- phase-continuous across streaming blocks and
                // parallel chunks, with transitions aligned to code-period edges.
                long period =
                    static_cast<long>(std::floor(cp / static_cast<double>(L)));
                long si = period % sec_len;
                if (si < 0) si += sec_len;
                chip *= static_cast<float>(sec[si]);
            }
            if (use_boc) {
                long hc = static_cast<long>(std::floor(2.0 * cp));
                if (hc & 1L) chip = -chip;
            }
            float navsym = static_cast<float>(nav_symbol(nav, t));
            std::complex<float> c = carr.next();
            float d = g * chip * navsym;
            iq[2 * k]     += d * c.real();
            iq[2 * k + 1] += d * c.imag();
        }
    }
}

}  // namespace gs

#include "abi.h"
#include "scheduler.hpp"

static gs::SvChannel debug_sv(const int8_t *code, double code_rate,
                              double code_phase0, double code_doppler,
                              double carrier_freq, int sys = 0,
                              double sub_carrier_hz = 0.0,
                              const int8_t *sec_code = nullptr, int sec_len = 0,
                              double sec_rate_hz = 0.0) {
    gs::SvChannel sv;
    sv.code = code;
    sv.code_len = 1023;
    sv.code_rate_hz = code_rate;
    sv.code_phase0_chips = code_phase0;
    sv.code_doppler_hz = code_doppler;
    sv.carrier_freq_hz = carrier_freq;
    sv.gain = 1.0f;
    sv.sys = sys;
    sv.sub_carrier_hz = sub_carrier_hz;
    sv.sec_code = sec_code;
    sv.sec_len = sec_len;
    sv.sec_rate_hz = sec_rate_hz;
    return sv;
}

void synth_debug_one_sv(const int8_t *code, double code_rate, double code_phase0,
                        double code_doppler, double carrier_freq, double fs,
                        int n, float *iq) {
    for (int k = 0; k < 2 * n; ++k) iq[k] = 0.0f;
    gs::SvChannel sv = debug_sv(code, code_rate, code_phase0, code_doppler,
                                carrier_freq);
    gs::mix_block(&sv, 1, fs, 0, n, iq);
}

// Debug shim: one SV through gs::mix_block from an arbitrary absolute sample
// index, so tests can compare a single-shot run against a split-range run.
// Zeroes iq[0..2n) (mix_block is accumulate-only), then accumulates.
void synth_debug_mix_range(const int8_t *code, double code_rate,
                           double code_phase0, double code_doppler,
                           double carrier_freq, double fs, uint64_t sample0,
                           int n, float *iq) {
    for (int k = 0; k < 2 * n; ++k) iq[k] = 0.0f;
    gs::SvChannel sv = debug_sv(code, code_rate, code_phase0, code_doppler,
                                carrier_freq);
    gs::mix_block(&sv, 1, fs, sample0, n, iq);
}

// Debug shim: one SV through gs::mix_block_parallel (which zeroes iq itself),
// so tests can compare nthreads=1 against nthreads>1 over the same span.
void synth_debug_mix_parallel(const int8_t *code, double code_rate,
                              double code_phase0, double code_doppler,
                              double carrier_freq, double fs, uint64_t sample0,
                              int n, int nthreads, float *iq) {
    gs::SvChannel sv = debug_sv(code, code_rate, code_phase0, code_doppler,
                                carrier_freq);
    gs::mix_block_parallel(&sv, 1, fs, sample0, n, iq, nthreads);
}

// --- Task 10 _ex shims: expose sys / sub_carrier_hz / secondary code ---------

void synth_debug_one_sv_ex(const int8_t *code, double code_rate,
                           double code_phase0, double code_doppler,
                           double carrier_freq, double fs, int n,
                           int sys, double sub_carrier_hz,
                           const int8_t *sec_code, int sec_len,
                           double sec_rate_hz, float *iq) {
    for (int k = 0; k < 2 * n; ++k) iq[k] = 0.0f;
    gs::SvChannel sv = debug_sv(code, code_rate, code_phase0, code_doppler,
                                carrier_freq, sys, sub_carrier_hz, sec_code,
                                sec_len, sec_rate_hz);
    gs::mix_block(&sv, 1, fs, 0, n, iq);
}

void synth_debug_mix_range_ex(const int8_t *code, double code_rate,
                              double code_phase0, double code_doppler,
                              double carrier_freq, double fs, uint64_t sample0,
                              int n, int sys, double sub_carrier_hz,
                              const int8_t *sec_code, int sec_len,
                              double sec_rate_hz, float *iq) {
    for (int k = 0; k < 2 * n; ++k) iq[k] = 0.0f;
    gs::SvChannel sv = debug_sv(code, code_rate, code_phase0, code_doppler,
                                carrier_freq, sys, sub_carrier_hz, sec_code,
                                sec_len, sec_rate_hz);
    gs::mix_block(&sv, 1, fs, sample0, n, iq);
}

void synth_debug_mix_parallel_ex(const int8_t *code, double code_rate,
                                 double code_phase0, double code_doppler,
                                 double carrier_freq, double fs,
                                 uint64_t sample0, int n, int nthreads,
                                 int sys, double sub_carrier_hz,
                                 const int8_t *sec_code, int sec_len,
                                 double sec_rate_hz, float *iq) {
    gs::SvChannel sv = debug_sv(code, code_rate, code_phase0, code_doppler,
                                carrier_freq, sys, sub_carrier_hz, sec_code,
                                sec_len, sec_rate_hz);
    gs::mix_block_parallel(&sv, 1, fs, sample0, n, iq, nthreads);
}
