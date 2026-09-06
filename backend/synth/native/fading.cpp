// backend/synth/native/fading.cpp
#include "fading.hpp"
#include "abi.h"

#include <cmath>

namespace {
// splitmix64 finalizer
inline uint64_t mix(uint64_t x) {
    x += 0x9E3779B97F4A7C15ULL;
    x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9ULL;
    x = (x ^ (x >> 27)) * 0x94D049BB133111EBULL;
    return x ^ (x >> 31);
}
inline double u01(uint64_t h) { return (h >> 11) * (1.0 / 9007199254740992.0); }

// Standard normal at a coherence knot via Box-Muller from two hashed uniforms.
// Keyed on (seed, prn, knot) only -> reproducible regardless of caller cadence.
inline double gauss(uint64_t seed, int prn, long knot) {
    uint64_t base = mix(seed ^ (static_cast<uint64_t>(prn) << 40)
                        ^ (static_cast<uint64_t>(knot) * 0x100000001B3ULL));
    double u1 = u01(mix(base)) + 1e-12;
    double u2 = u01(mix(base ^ 0xABCDEFULL));
    return std::sqrt(-2.0 * std::log(u1)) * std::cos(2.0 * M_PI * u2);
}

// Smoothstep interpolation between adjacent knots blends two independent unit
// normals, so the time-averaged variance of the blend is
// mean_{f in [0,1)} [ (1-w)^2 + w^2 ]  with  w = f*f*(3-2f)  ==  26/35.
// Without correction the realised dB std would sit at sqrt(26/35)*sigma_db,
// biased low. kSmoothVarComp = sqrt(35/26) restores the requested std.
constexpr double kSmoothVarComp = 1.1602387022306428;  // sqrt(35/26)
}  // namespace

namespace gs {
float fading_gain_linear(const FadingCfg *c, int prn, double t_s) {
    if (!c || c->model == 0 || c->sigma_db <= 0.0 || c->coherence_s <= 0.0)
        return 1.0f;
    double x = t_s / c->coherence_s;
    long k0 = static_cast<long>(std::floor(x));
    double frac = x - static_cast<double>(k0);
    double g0 = gauss(c->seed, prn, k0);
    double g1 = gauss(c->seed, prn, k0 + 1);
    double w = frac * frac * (3.0 - 2.0 * frac);  // smoothstep, C1 continuity
    double g = g0 * (1.0 - w) + g1 * w;
    double gain_db = c->sigma_db * kSmoothVarComp * g;
    return static_cast<float>(std::pow(10.0, gain_db / 20.0));
}
}  // namespace gs

// C-linkage shim so the symbol is loadable via ctypes as `fading_gain_linear`.
extern "C" float fading_gain_linear(const FadingCfg *c, int prn, double t_s) {
    return gs::fading_gain_linear(c, prn, t_s);
}
