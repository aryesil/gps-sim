// backend/synth/native/mixer.cpp
#include "mixer.hpp"
#include "nco.hpp"
#include <cmath>

namespace gs {

#if defined(__x86_64__) && defined(__GNUC__)
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
        for (int k = 0; k < n; ++k) {
            double t = abs_t0 + k * dt;
            double cp = eff + chip_rate * (t - abs_t0);
            long ci = static_cast<long>(cp) % L;
            if (ci < 0) ci += L;
            float chip = static_cast<float>(code[ci]);
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
void synth_debug_one_sv(const int8_t *code, double code_rate, double code_phase0,
                        double code_doppler, double carrier_freq, double fs,
                        int n, float *iq) {
    for (int k = 0; k < 2 * n; ++k) iq[k] = 0.0f;
    gs::SvChannel sv;
    sv.code = code;
    sv.code_len = 1023;
    sv.code_rate_hz = code_rate;
    sv.code_phase0_chips = code_phase0;
    sv.code_doppler_hz = code_doppler;
    sv.carrier_freq_hz = carrier_freq;
    sv.gain = 1.0f;
    gs::mix_block(&sv, 1, fs, 0, n, iq);
}
