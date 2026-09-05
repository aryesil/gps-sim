// backend/synth/native/mixer.cpp
#include "mixer.hpp"
#include "nco.hpp"
#include <cmath>

namespace gs {

void mix_block(const SvChannel *svs, int nsv, double fs,
               uint64_t sample0, int n, float *iq) {
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
        for (int k = 0; k < n; ++k) {
            double t = abs_t0 + k * dt;
            double cp = sv.code_phase0_chips + chip_rate * (t - abs_t0);
            long ci = static_cast<long>(cp) % L;
            if (ci < 0) ci += L;
            float chip = static_cast<float>(sv.code[ci]);
            float nav = static_cast<float>(nav_symbol(sv.nav, t));
            std::complex<float> c = carr.next();
            float d = g * chip * nav;
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
