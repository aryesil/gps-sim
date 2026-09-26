// backend/synth/native/fading.hpp
#pragma once
#include <cstdint>
namespace gs {
struct FadingCfg {
    int model;            // 0 = off, 1 = lognormal (seeded), 2 = keyed
    double sigma_db;
    double coherence_s;
    uint64_t seed;        // lognormal only
    // ABI 27 -- keyed model. The per-SV process is drawn from ChaCha20
    // (RFC 8439) keyed with `key`, so without the key its values cannot be
    // predicted from any amount of observed IQ. `domain` separates
    // constellations that reuse PRN numbers (ASCII system letter).
    uint8_t key[32];
    int domain;
};
// Deterministic per-SV fading gain (linear).
//   lognormal: depends only on (seed, prn, floor(t_s / coherence_s)) plus
//              smoothstep interpolation to the next knot.
//   keyed:     depends only on (key, domain, prn, t_s); knots are jittered
//              and each SV gets its own grid phase and spacing.
// Both are independent of fs, block size and thread count.
float fading_gain_linear(const FadingCfg *c, int prn, double t_s);
// RFC 8439 ChaCha20 block function (exposed for test vectors).
void chacha20_block(const uint8_t key[32], uint32_t counter,
                    const uint8_t nonce[12], uint8_t out[64]);
}  // namespace gs
