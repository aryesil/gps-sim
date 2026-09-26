// backend/synth/native/mixer.cpp
#include "mixer.hpp"
#include "nco.hpp"
#include <climits>
#include <cmath>

namespace gs {

// CBOC(6,1,1/11) weights: sqrt(10/11), sqrt(1/11).
static constexpr float kCbocA = 0.95346258924559231f;
static constexpr float kCbocB = 0.30151134457776363f;

#if defined(__x86_64__) && defined(__GNUC__) && !defined(__APPLE__) && !defined(_WIN32)
// target_clones needs ifunc (ELF/Mach-O symbol resolvers); MinGW's PE-COFF
// target does not support it -- the plain function below still compiles
// with whatever -march the build uses, just without runtime CPU dispatch.
__attribute__((target_clones("default", "sse4.2", "avx2", "avx2,fma")))
#endif
void mix_block(const SvChannel *__restrict svs, int nsv, double fs,
               uint64_t sample0, int n, float *__restrict iq) {
    const double dt = 1.0 / fs;
    for (int s = 0; s < nsv; ++s) {
        const SvChannel &sv = svs[s];
        double abs_t0 = sample0 * dt;

        // SP-B: pick this block's trajectory knot. Knot j covers mixer block j;
        // within the block the NCO runs linearly from the knot's frequency and
        // accumulated phase. traj_nknots == 0 => Phase-1 constant-Doppler path,
        // which the expressions below reduce to exactly (seg_t0 == 0).
        const bool use_traj = sv.traj_nknots > 0;
        double eff_carr_freq = sv.carrier_freq_hz;
        double eff_carr_phase0 = sv.carrier_phase0_rad;   // phase at seg_t0
        double eff_code_rate = sv.code_rate_hz + sv.code_doppler_hz;
        double seg_t0 = 0.0;
        double code_phase_at_seg = 0.0;
        if (use_traj) {
            const uint64_t ks = sv.traj_knot_samples;
            int j = ks ? static_cast<int>(sample0 / ks) : 0;
            if (j < 0) j = 0;
            if (j > sv.traj_nknots - 1) j = sv.traj_nknots - 1;
            seg_t0 = static_cast<double>(static_cast<uint64_t>(j) * ks) / fs;
            eff_carr_freq = sv.traj_carr_freq[j];
            eff_carr_phase0 = sv.traj_carr_phase[j];
            eff_code_rate = sv.traj_code_rate[j];
            code_phase_at_seg = sv.traj_code_phase[j];
        }

        // Carrier NCO seeded so that absolute sample `sample0` has the exact
        // starting phase (continuity across blocks / knots).
        Nco carr;
        carr.set_freq(eff_carr_freq, fs);
        double ph0 = eff_carr_phase0
                     + 2.0 * M_PI * eff_carr_freq * (abs_t0 - seg_t0);
        ph0 = std::fmod(ph0, 2.0 * M_PI);
        if (ph0 < 0) ph0 += 2.0 * M_PI;
        carr.phase = static_cast<uint32_t>(ph0 / (2.0 * M_PI) * 4294967296.0);

        const double chip_rate = eff_code_rate;
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
        // Trajectory branch: absolute code phase at abs_t0 comes from the knot.
        const double cp_base =
            code_phase_at_seg + eff_code_rate * (abs_t0 - seg_t0);

        // BOC(1,1): the sub-carrier is code-phase-locked -- two half-chips per
        // code chip. Deriving the sign from the running code phase `cp` (which
        // already carries code Doppler and is absolute-sample-seeded) keeps the
        // sub-carrier coherent with the code across Doppler, streaming blocks and
        // parallel chunks (final review B1 discipline). An independent nominal-
        // rate NCO slipped ~code_doppler chips/s vs the code.
        const bool use_boc = sv.sub_carrier_hz > 0.0;
        const int cboc = sv.cboc;
        const int8_t *__restrict sec = sv.sec_code;
        const int sec_len = sv.sec_len;
        // Transmit-time modulation clock (ABI 25): u = cp + tx_off counts
        // primary chips since nav symbol 0 left the satellite. Chips per nav
        // symbol use the NOMINAL chip rate -- symbol/secondary edges are
        // fixed in satellite time, Doppler only stretches them in receiver
        // time (already carried by cp).
        const bool use_tx = sv.tx_time_valid != 0;
        const double tx_off = sv.tx_chips_offset;
        const double chips_per_sym =
            sv.code_rate_hz / nav_rate(nav);
        // ABI 28 channel gain: knot j covers [(j0+j)*dt, (j0+j+1)*dt).
        const bool use_gk = sv.gain_nknots >= 2 && sv.gain_knots;
        const double inv_gdt = use_gk ? 1.0 / sv.gain_knot_dt : 0.0;
        const int64_t gk_last = sv.gain_nknots - 2;

        // The code chip, secondary chip and nav symbol only change when
        // floor(cp) or floor(u) does, so they are recomputed on those edges
        // instead of every sample (the 64-bit modulos and divisions were
        // most of the per-sample cost). Exact when the code length and the
        // chips per nav symbol are integers -- every signal defined today;
        // anything else takes the per-sample path.
        const bool cache_chip =
            chips_per_sym == std::floor(chips_per_sym) && chips_per_sym > 0.0;
        int64_t last_fl = INT64_MIN, last_ufl = INT64_MIN;
        float cached = 0.0f;
        const std::complex<float> *lut = expLut().data();

        for (int k = 0; k < n; ++k) {
            double t = abs_t0 + k * dt;
            // `t` is already absolute (abs_t0 + k*dt); advancing by absolute `t`
            // keeps the code NCO continuous across streaming blocks and across
            // parallel chunks. Using (t - abs_t0) here dropped the absolute term
            // and restarted the code phase at `eff` every call (final review B1).
            double cp = use_traj ? (cp_base + chip_rate * (k * dt))
                                 : (eff + chip_rate * t);
            const double u = use_tx ? (cp + tx_off) : cp;
            const int64_t fl = static_cast<int64_t>(std::floor(cp));
            const int64_t ufl = static_cast<int64_t>(std::floor(u));
            float chip;
            if (cache_chip && fl == last_fl && ufl == last_ufl) {
                chip = cached;
            } else {
                // int64_t, not long: long is 32-bit on Windows/MinGW and the
                // absolute chip count passes 2^31 after ~210 s at 10.23 Mcps.
                int64_t ci = fl % L;
                if (ci < 0) ci += L;
                chip = static_cast<float>(code[ci]);
                if (sec_len > 0) {
                    // One secondary chip spans exactly one primary code
                    // period, so index by elapsed primary periods --
                    // phase-continuous across streaming blocks and parallel
                    // chunks, with transitions aligned to code-period edges.
                    // In transmit-time mode the period count is referenced
                    // to the nav stream, so secondary chip 0 starts every nav
                    // symbol (ICD synchronisation).
                    int64_t period = static_cast<int64_t>(
                        std::floor(u / static_cast<double>(L)));
                    int64_t si = period % sec_len;
                    if (si < 0) si += sec_len;
                    chip *= static_cast<float>(sec[si]);
                }
                if (use_tx)
                    chip *= static_cast<float>(nav_symbol_at(
                        nav, static_cast<int64_t>(std::floor(u / chips_per_sym))));
                cached = chip;
                last_fl = fl;
                last_ufl = ufl;
            }
            if (!use_tx) chip *= static_cast<float>(nav_symbol(nav, t));
            if (use_boc) {
                int64_t hc = static_cast<int64_t>(std::floor(2.0 * cp));
                if (cboc != 0) {
                    // CBOC(6,1,1/11): sc(6,1) has 12 half-periods per chip.
                    int64_t hc6 = static_cast<int64_t>(std::floor(12.0 * cp));
                    const float s11 = (hc & 1) ? -1.0f : 1.0f;
                    const float s61 = (hc6 & 1) ? -1.0f : 1.0f;
                    chip *= kCbocA * s11 + static_cast<float>(cboc) * kCbocB * s61;
                } else if (hc & 1) {
                    chip = -chip;
                }
            }
            std::complex<float> c = lut[carr.phase >> 20];
            carr.phase += carr.inc;
            if (use_gk) {
                const double x = t * inv_gdt - static_cast<double>(sv.gain_knot_j0);
                int64_t j = static_cast<int64_t>(std::floor(x));
                if (j < 0) j = 0;
                if (j > gk_last) j = gk_last;
                float f = static_cast<float>(x - static_cast<double>(j));
                f = f < 0.0f ? 0.0f : (f > 1.0f ? 1.0f : f);
                const float *gp = sv.gain_knots + 2 * j;
                const float gr = gp[0] + f * (gp[2] - gp[0]);
                const float gi = gp[1] + f * (gp[3] - gp[1]);
                c = std::complex<float>(c.real() * gr - c.imag() * gi,
                                        c.real() * gi + c.imag() * gr);
            }
            float d = g * chip;
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

// SP-B debug shim: one SV through gs::mix_block with the trajectory fields set.
void synth_debug_mix_traj(const int8_t *code, double code_rate,
                          double code_phase0, double carrier_freq, double fs,
                          uint64_t sample0, int n, int traj_nknots,
                          uint64_t traj_knot_samples,
                          const double *carr_freq, const double *carr_phase,
                          const double *code_rate_knots,
                          const double *code_phase_knots, float *iq) {
    for (int k = 0; k < 2 * n; ++k) iq[k] = 0.0f;
    gs::SvChannel sv = debug_sv(code, code_rate, code_phase0, 0.0, carrier_freq);
    sv.traj_nknots = traj_nknots;
    sv.traj_knot_samples = traj_knot_samples;
    sv.traj_carr_freq = carr_freq;
    sv.traj_carr_phase = carr_phase;
    sv.traj_code_rate = code_rate_knots;
    sv.traj_code_phase = code_phase_knots;
    gs::mix_block(&sv, 1, fs, sample0, n, iq);
}

// ABI 28 debug shim: one SV through gs::mix_block_parallel with complex
// channel-gain knots, so tests can check the per-sample interpolation.
void synth_debug_mix_gain(const int8_t *code, double code_rate,
                          double code_phase0, double carrier_freq, double fs,
                          uint64_t sample0, int n, int nthreads, int nknots,
                          int64_t knot_j0, double knot_dt, const float *knots,
                          float *iq) {
    gs::SvChannel sv = debug_sv(code, code_rate, code_phase0, 0.0, carrier_freq);
    sv.gain_nknots = nknots;
    sv.gain_knot_j0 = knot_j0;
    sv.gain_knot_dt = knot_dt;
    sv.gain_knots = knots;
    gs::mix_block_parallel(&sv, 1, fs, sample0, n, iq, nthreads);
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
