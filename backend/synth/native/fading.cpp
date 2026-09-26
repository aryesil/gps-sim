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
// biased low. kSmoothVarComp = sqrt(35/26) restores the requested std. The
// average is per interval, so it holds for the keyed model's unequal
// (jittered) intervals too.
constexpr double kSmoothVarComp = 1.1602387022306428;  // sqrt(35/26)

inline double smooth_blend(double g0, double g1, double frac) {
    double w = frac * frac * (3.0 - 2.0 * frac);  // smoothstep, C1 continuity
    return g0 * (1.0 - w) + g1 * w;
}

// ---- keyed model -----------------------------------------------------------

inline uint32_t rotl32(uint32_t v, int c) { return (v << c) | (v >> (32 - c)); }
inline uint32_t le32(const uint8_t *p) {
    return static_cast<uint32_t>(p[0]) | (static_cast<uint32_t>(p[1]) << 8) |
           (static_cast<uint32_t>(p[2]) << 16) | (static_cast<uint32_t>(p[3]) << 24);
}
inline void qr(uint32_t &a, uint32_t &b, uint32_t &c, uint32_t &d) {
    a += b; d ^= a; d = rotl32(d, 16);
    c += d; b ^= c; b = rotl32(b, 12);
    a += b; d ^= a; d = rotl32(d, 8);
    c += d; b ^= c; b = rotl32(b, 7);
}

// Eight 53-bit uniforms in [0,1) from one ChaCha20 block. The nonce carries
// (domain, purpose, prn, knot), so every (SV, knot) pair gets its own
// independent block under the key.
struct Draw { double u[8]; };
inline Draw keyed_draw(const uint8_t key[32], int domain, int purpose,
                       int prn, int64_t knot) {
    uint8_t nonce[12];
    const uint32_t n0 = (static_cast<uint32_t>(domain & 0xFF) << 24) |
                        (static_cast<uint32_t>(purpose & 0xFF) << 16) |
                        (static_cast<uint32_t>(prn) & 0xFFFFu);
    const uint64_t k = static_cast<uint64_t>(knot);
    const uint32_t w[3] = {n0, static_cast<uint32_t>(k),
                           static_cast<uint32_t>(k >> 32)};
    for (int i = 0; i < 3; ++i)
        for (int j = 0; j < 4; ++j)
            nonce[4 * i + j] = static_cast<uint8_t>(w[i] >> (8 * j));
    uint8_t blk[64];
    gs::chacha20_block(key, 0, nonce, blk);
    Draw d;
    for (int i = 0; i < 8; ++i) {
        uint64_t v = static_cast<uint64_t>(le32(blk + 8 * i)) |
                     (static_cast<uint64_t>(le32(blk + 8 * i + 4)) << 32);
        d.u[i] = u01(v);
    }
    return d;
}

constexpr int kPurposeKnot = 0;
constexpr int kPurposeSv = 1;
// Knot k sits at (k + jitter) grid units, jitter in [-kJitter, kJitter].
// kJitter < 0.5 keeps the knots strictly increasing, so the interval holding
// any x is found within one step of floor(x).
constexpr double kJitter = 0.35;

struct Knot { double pos, g; };
inline Knot keyed_knot(const gs::FadingCfg *c, int prn, int64_t k) {
    Draw d = keyed_draw(c->key, c->domain, kPurposeKnot, prn, k);
    double u1 = d.u[1] + 1e-12;
    double g = std::sqrt(-2.0 * std::log(u1)) * std::cos(2.0 * M_PI * d.u[2]);
    return {static_cast<double>(k) + (2.0 * d.u[0] - 1.0) * kJitter, g};
}

double keyed_gain_db(const gs::FadingCfg *c, int prn, double t_s) {
    // Per-SV grid phase and spacing (0.75..1.25 x coherence_s), so the
    // satellites share no common knot grid and no knot lands on t = 0.
    Draw sv = keyed_draw(c->key, c->domain, kPurposeSv, prn, 0);
    const double spacing = c->coherence_s * (0.75 + 0.5 * sv.u[1]);
    const double x = t_s / spacing + sv.u[0];
    int64_t k = static_cast<int64_t>(std::floor(x));
    Knot a = keyed_knot(c, prn, k);
    if (x < a.pos) {
        --k;
        a = keyed_knot(c, prn, k);
    }
    Knot b = keyed_knot(c, prn, k + 1);
    if (x >= b.pos) {
        ++k;
        a = b;
        b = keyed_knot(c, prn, k + 1);
    }
    const double frac = (x - a.pos) / (b.pos - a.pos);
    return c->sigma_db * kSmoothVarComp * smooth_blend(a.g, b.g, frac);
}
}  // namespace

namespace gs {
void chacha20_block(const uint8_t key[32], uint32_t counter,
                    const uint8_t nonce[12], uint8_t out[64]) {
    uint32_t s[16] = {0x61707865u, 0x3320646eu, 0x79622d32u, 0x6b206574u};
    for (int i = 0; i < 8; ++i) s[4 + i] = le32(key + 4 * i);
    s[12] = counter;
    for (int i = 0; i < 3; ++i) s[13 + i] = le32(nonce + 4 * i);
    uint32_t x[16];
    for (int i = 0; i < 16; ++i) x[i] = s[i];
    for (int r = 0; r < 10; ++r) {
        qr(x[0], x[4], x[8], x[12]);
        qr(x[1], x[5], x[9], x[13]);
        qr(x[2], x[6], x[10], x[14]);
        qr(x[3], x[7], x[11], x[15]);
        qr(x[0], x[5], x[10], x[15]);
        qr(x[1], x[6], x[11], x[12]);
        qr(x[2], x[7], x[8], x[13]);
        qr(x[3], x[4], x[9], x[14]);
    }
    for (int i = 0; i < 16; ++i) {
        const uint32_t v = x[i] + s[i];
        for (int j = 0; j < 4; ++j)
            out[4 * i + j] = static_cast<uint8_t>(v >> (8 * j));
    }
}

float fading_gain_linear(const FadingCfg *c, int prn, double t_s) {
    if (!c || c->model == 0 || c->sigma_db <= 0.0 || c->coherence_s <= 0.0)
        return 1.0f;
    double gain_db;
    if (c->model == 2) {
        gain_db = keyed_gain_db(c, prn, t_s);
    } else {
        double x = t_s / c->coherence_s;
        long k0 = static_cast<long>(std::floor(x));
        double frac = x - static_cast<double>(k0);
        double g0 = gauss(c->seed, prn, k0);
        double g1 = gauss(c->seed, prn, k0 + 1);
        gain_db = c->sigma_db * kSmoothVarComp * smooth_blend(g0, g1, frac);
    }
    return static_cast<float>(std::pow(10.0, gain_db / 20.0));
}
}  // namespace gs

// C-linkage shims so the symbols are loadable via ctypes.
extern "C" float fading_gain_linear(const FadingCfg *c, int prn, double t_s) {
    return gs::fading_gain_linear(c, prn, t_s);
}
extern "C" void fading_chacha20_block(const uint8_t *key, uint32_t counter,
                                      const uint8_t *nonce, uint8_t *out) {
    gs::chacha20_block(key, counter, nonce, out);
}
