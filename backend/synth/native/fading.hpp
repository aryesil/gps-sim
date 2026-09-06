// backend/synth/native/fading.hpp
#pragma once
#include <cstdint>
namespace gs {
struct FadingCfg {
    int model;            // 0 = off, 1 = lognormal
    double sigma_db;
    double coherence_s;
    uint64_t seed;
};
// Deterministic per-SV fading gain (linear). Depends only on
// (seed, prn, floor(t_s / coherence_s)) plus smoothstep interpolation to the
// next knot -- independent of fs, block size and thread count.
float fading_gain_linear(const FadingCfg *c, int prn, double t_s);
}  // namespace gs
