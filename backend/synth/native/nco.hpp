// backend/synth/native/nco.hpp
#pragma once
#include <array>
#include <complex>
#include <cmath>
#include <cstdint>

namespace gs {

inline const std::array<std::complex<float>, 4096> &expLut() {
    static const auto lut = [] {
        std::array<std::complex<float>, 4096> t{};
        for (int i = 0; i < 4096; ++i) {
            double a = 2.0 * M_PI * i / 4096.0;
            t[i] = {static_cast<float>(std::cos(a)), static_cast<float>(std::sin(a))};
        }
        return t;
    }();
    return lut;
}

// 32-bit phase accumulator; top 12 bits index the 4096-entry LUT.
// The same struct is reused as the code NCO at a different frequency.
struct Nco {
    uint32_t phase = 0;
    uint32_t inc = 0;
    void set_freq(double hz, double fs) {
        // Wraps negative frequencies correctly via unsigned modular arithmetic.
        double frac = hz / fs;
        frac -= std::floor(frac);
        inc = static_cast<uint32_t>(frac * 4294967296.0 + 0.5);
    }
    inline std::complex<float> next() {
        std::complex<float> v = expLut()[phase >> 20];
        phase += inc;
        return v;
    }
};

}  // namespace gs
