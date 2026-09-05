// backend/synth/native/nco_debug.cpp
#include "abi.h"
#include "nco.hpp"

void synth_debug_carrier(double freq_hz, double fs, int n, float *iq_interleaved) {
    gs::Nco nco;
    nco.set_freq(freq_hz, fs);
    for (int k = 0; k < n; ++k) {
        auto v = nco.next();
        iq_interleaved[2 * k] = v.real();
        iq_interleaved[2 * k + 1] = v.imag();
    }
}
