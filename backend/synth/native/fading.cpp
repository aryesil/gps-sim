// backend/synth/native/fading.cpp
#include "fading.hpp"
#include "abi.h"

#include <algorithm>
#include <cmath>
#include <cstring>

namespace {
constexpr double kC = 299792458.0;

// ---- ChaCha20 --------------------------------------------------------------

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

// ---- model tables ------------------------------------------------------------
//
// Representative L-band land-mobile-satellite values in the spirit of the
// Perez-Fontan / ITU-R P.681 three-state model: per state the Loo direct-path
// mean and spread (dB) and the diffuse multipath power (dB, relative to an
// unobstructed direct path), the state and shadowing correlation distances,
// and the line-of-sight / shadowed probabilities at 10, 30, 50, 70, 90 deg
// elevation (blocked takes the rest). They are not a reproduction of any one
// published measurement campaign.
struct EnvPreset {
    double st[3][3];          // [LOS, shadowed, blocked][mu, sigma, mp]
    double d_state_m, d_shadow_m;
    double p_los[5], p_sh[5];
};
constexpr EnvPreset kEnv[4] = {
    // open sky
    {{{0.0, 0.3, -22.0}, {-4.0, 1.5, -18.0}, {-15.0, 2.0, -18.0}}, 30.0, 5.0,
     {0.97, 0.99, 1.0, 1.0, 1.0}, {0.03, 0.01, 0.0, 0.0, 0.0}},
    // rural / tree-shadowed
    {{{0.0, 0.8, -18.0}, {-7.0, 2.5, -15.0}, {-18.0, 3.0, -15.0}}, 12.0, 3.0,
     {0.55, 0.70, 0.80, 0.88, 0.92}, {0.35, 0.25, 0.17, 0.10, 0.07}},
    // suburban
    {{{0.0, 1.0, -16.0}, {-8.0, 3.0, -14.0}, {-20.0, 3.0, -14.0}}, 15.0, 3.0,
     {0.45, 0.65, 0.78, 0.87, 0.92}, {0.30, 0.22, 0.15, 0.09, 0.06}},
    // urban
    {{{0.0, 1.0, -14.0}, {-9.0, 3.0, -12.0}, {-22.0, 3.0, -13.0}}, 20.0, 2.0,
     {0.15, 0.35, 0.55, 0.72, 0.80}, {0.25, 0.30, 0.25, 0.18, 0.12}},
};
// A static receiver still decorrelates as the satellite moves across the sky;
// these bound the correlation times and the Doppler spread at low speed.
constexpr double kTauStateMax = 300.0;
constexpr double kTauShadowMax = 60.0;
constexpr double kDopplerMin = 0.01;
constexpr double kKnotMax = 0.05;
// Width (in z) of the blend across a state threshold, so a state change is a
// short transition instead of a step.
constexpr double kStateBlend = 0.2;

// Grid points per correlation time / per Doppler period.
constexpr int kPerTau = 32;
constexpr int kArTaps = 192;          // AR(1) truncated at rho^192 = e^-6
constexpr int kSos = 32;              // diffuse: sinusoids per (SV, carrier)

constexpr int kPurposeState = 0;
constexpr int kPurposeShadow = 1;
constexpr int kPurposeDiffuse = 2;

// Unit-energy exponential (AR(1)) taps: x_m = sum_j a_j w_{m-j}.
const double *ar_taps() {
    static const std::vector<double> t = [] {
        std::vector<double> a(kArTaps);
        const double rho = std::exp(-1.0 / kPerTau);
        double e = 0.0;
        for (int j = 0; j < kArTaps; ++j) {
            a[j] = std::pow(rho, j);
            e += a[j] * a[j];
        }
        const double s = 1.0 / std::sqrt(e);
        for (int j = 0; j < kArTaps; ++j) a[j] *= s;
        return a;
    }();
    return t.data();
}

// Acklam's inverse standard normal CDF (relative error < 1.2e-9).
double inv_norm(double p) {
    static const double a[] = {-3.969683028665376e+01, 2.209460984245205e+02,
                               -2.759285104469687e+02, 1.383577518672690e+02,
                               -3.066479806614716e+01, 2.506628277459239e+00};
    static const double b[] = {-5.447609879822406e+01, 1.615858368580409e+02,
                               -1.556989798598866e+02, 6.680131188771972e+01,
                               -1.328068155288572e+01};
    static const double c[] = {-7.784894002430293e-03, -3.223964580411365e-01,
                               -2.400758277161838e+00, -2.549732539343734e+00,
                               4.374664141464968e+00, 2.938163982698783e+00};
    static const double d[] = {7.784695709041462e-03, 3.224671290700398e-01,
                               2.445134137142996e+00, 3.754408661907416e+00};
    const double pl = 0.02425;
    if (p < pl || p > 1.0 - pl) {
        const double q = std::sqrt(-2.0 * std::log(p < pl ? p : 1.0 - p));
        const double v = (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) /
                         ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0);
        return p < pl ? v : -v;
    }
    const double q = p - 0.5, r = q * q;
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q /
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0);
}

inline double smooth01(double x) {
    if (x <= 0.0) return 0.0;
    if (x >= 1.0) return 1.0;
    return x * x * (3.0 - 2.0 * x);
}

inline int64_t floor_div(int64_t a, int64_t b) {
    int64_t q = a / b;
    if ((a % b != 0) && ((a < 0) != (b < 0))) --q;
    return q;
}

// Eight 53-bit uniforms from ChaCha20 block `b` of one stream. Nonce:
// (system << 24 | purpose << 16 | prn), b bits 0..31, b bits 32..47 | band << 16.
void block_uniforms(const uint8_t key[32], uint32_t n0, uint32_t band,
                    int64_t b, double u[8]) {
    const uint64_t bi = static_cast<uint64_t>(b);
    const uint32_t w[3] = {n0, static_cast<uint32_t>(bi),
                           static_cast<uint32_t>((bi >> 32) & 0xFFFFu) | (band << 16)};
    uint8_t nonce[12];
    for (int i = 0; i < 3; ++i)
        for (int j = 0; j < 4; ++j)
            nonce[4 * i + j] = static_cast<uint8_t>(w[i] >> (8 * j));
    uint8_t blk[64];
    gs::chacha20_block(key, 0, nonce, blk);
    for (int i = 0; i < 8; ++i) {
        const uint64_t v = static_cast<uint64_t>(le32(blk + 8 * i)) |
                           (static_cast<uint64_t>(le32(blk + 8 * i + 4)) << 32);
        u[i] = (v >> 11) * (1.0 / 9007199254740992.0);
    }
}

inline uint32_t stream_n0(int domain, int purpose, int prn) {
    return (static_cast<uint32_t>(domain & 0xFF) << 24) |
           (static_cast<uint32_t>(purpose & 0xFF) << 16) |
           (static_cast<uint32_t>(prn) & 0xFFFFu);
}

// Seeded model: the key is one ChaCha20 block under the all-zero key with the
// seed (little-endian) and "SEED" as nonce, so both models share one path.
void seed_key(uint64_t seed, uint8_t key[32]) {
    uint8_t zero[32] = {};
    uint8_t nonce[12];
    for (int i = 0; i < 8; ++i) nonce[i] = static_cast<uint8_t>(seed >> (8 * i));
    nonce[8] = 'S'; nonce[9] = 'E'; nonce[10] = 'E'; nonce[11] = 'D';
    uint8_t blk[64];
    gs::chacha20_block(zero, 0, nonce, blk);
    std::memcpy(key, blk, 32);
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

void GridProcess::init(const uint8_t key[32], int domain, int purpose, int prn,
                       double dt) {
    std::memcpy(key_, key, 32);
    n0_ = stream_n0(domain, purpose, prn);
    dt_ = dt;
    b0_ = 0;
    buf_.clear();
    last_m_ = INT64_MIN;
}

// q-th unit normal of the stream: block b = q / 8 gives eight uniforms, i.e.
// four Box-Muller pairs.
double GridProcess::normal(int64_t q) {
    const int64_t b = floor_div(q, 8);
    const int64_t nb = static_cast<int64_t>(buf_.size() / 8);
    if (b < b0_ || b > b0_ + nb) {
        buf_.clear();
        b0_ = b;
    } else if (b == b0_ + nb && nb >= 4096) {
        buf_.erase(buf_.begin(), buf_.begin() + 8 * 2048);
        b0_ += 2048;
    }
    while (static_cast<int64_t>(buf_.size() / 8) <= b - b0_) {
        double u[8];
        block_uniforms(key_, n0_, 0,
                       b0_ + static_cast<int64_t>(buf_.size() / 8), u);
        for (int i = 0; i < 4; ++i) {
            const double r = std::sqrt(-2.0 * std::log(1.0 - u[2 * i]));
            buf_.push_back(r * std::cos(2.0 * M_PI * u[2 * i + 1]));
            buf_.push_back(r * std::sin(2.0 * M_PI * u[2 * i + 1]));
        }
    }
    return buf_[static_cast<size_t>(q - 8 * b0_)];
}

// Exponentially correlated unit-variance value at grid point m (AR(1) taps
// over innovations m-191 .. m, walked in increasing order so a forward run
// keeps hitting the block window).
double GridProcess::value(int64_t m) {
    const double *a = ar_taps();
    double x = 0.0;
    for (int64_t i = m - (kArTaps - 1); i <= m; ++i) x += a[m - i] * normal(i);
    return x;
}

double GridProcess::at(double u) {
    const double x = u / dt_;
    const double fl = std::floor(x);
    const int64_t m = static_cast<int64_t>(fl);
    if (m != last_m_) {
        if (m == last_m_ + 1) {
            last_v0_ = last_v1_;
        } else {
            last_v0_ = value(m);
        }
        last_v1_ = value(m + 1);
        last_m_ = m;
    }
    const double f = x - fl;
    return last_v0_ + f * (last_v1_ - last_v0_);
}

ChannelProcess::ChannelProcess(const FadingCfg &c, int prn) {
    if (c.model == 0) return;
    const int env = std::clamp(c.env, 0, 3);
    const EnvPreset &e = kEnv[env];
    uint8_t key[32];
    if (c.model == 1)
        seed_key(c.seed, key);
    else
        std::memcpy(key, c.key, 32);
    for (int s = 0; s < 3; ++s)
        for (int k = 0; k < 3; ++k) st_[s][k] = e.st[s][k];

    // Motion: a distance profile from a route, else a constant speed. The
    // top speed sizes the knot grid so the fastest stretch is resolved.
    double vmax = 0.0;
    if (c.motion_n >= 2 && c.motion_t && c.motion_d) {
        mt_.assign(c.motion_t, c.motion_t + c.motion_n);
        md_.assign(c.motion_d, c.motion_d + c.motion_n);
        for (int i = 1; i < c.motion_n; ++i) {
            const double dt = mt_[i] - mt_[i - 1];
            if (dt > 0.0) vmax = std::max(vmax, (md_[i] - md_[i - 1]) / dt);
        }
    } else {
        speed_ = std::max(c.speed_mps, 0.0);
        vmax = speed_;
    }
    const double carrier = c.carrier_hz > 0.0 ? c.carrier_hz : 1575.42e6;
    lambda_ = kC / carrier;
    d_state_ = e.d_state_m;
    d_shadow_ = e.d_shadow_m;
    // Decorrelation rates: distance over the correlation distance plus the
    // satellite-motion floor.
    p_.doppler_hz = vmax / lambda_ + kDopplerMin;
    p_.dt_state_s = 1.0 / (kPerTau * (vmax / d_state_ + 1.0 / kTauStateMax));
    p_.dt_shadow_s = 1.0 / (kPerTau * (vmax / d_shadow_ + 1.0 / kTauShadowMax));
    p_.dt_diffuse_s = 1.0 / (kPerTau * p_.doppler_hz);   // knot spacing only
    p_.dt_knot_s = std::min({p_.dt_state_s, p_.dt_shadow_s, p_.dt_diffuse_s, kKnotMax});

    const double el = std::clamp(c.el_deg, 10.0, 90.0);
    const double xi = (el - 10.0) / 20.0;
    const int i0 = std::min(static_cast<int>(xi), 3);
    const double fr = xi - i0;
    p_.p_los = e.p_los[i0] + fr * (e.p_los[i0 + 1] - e.p_los[i0]);
    p_.p_shadow = e.p_sh[i0] + fr * (e.p_sh[i0 + 1] - e.p_sh[i0]);
    const double lo = 1e-6, hi = 1.0 - 1e-6;
    p_.z_los = inv_norm(std::clamp(p_.p_los, lo, hi));
    p_.z_shadow = inv_norm(std::clamp(p_.p_los + p_.p_shadow, lo, hi));

    // State and shadowing run on u = distance / d_c + t / tau_max (one unit
    // = one correlation length), gridded kPerTau points per unit.
    state_.init(key, c.domain, kPurposeState, prn, 1.0 / kPerTau);
    shadow_.init(key, c.domain, kPurposeShadow, prn, 1.0 / kPerTau);
    // Diffuse multipath: sum of kSos unit phasors, arrival angle n in the
    // stratum [2 pi n / N, 2 pi (n+1) / N) and a random phase. Its ensemble
    // autocorrelation over travelled distance r is J0(2 pi r / lambda), the
    // Jakes spectrum for any speed profile. Carrier in
    // units of 1.023 MHz names the band (1540 L1, 1200 L2, 1150 L5, per
    // channel for GLONASS FDMA), so each carrier scatters independently.
    const uint32_t band =
        static_cast<uint32_t>(std::lround(carrier / 1.023e6)) & 0xFFFFu;
    const uint32_t n0 = stream_n0(c.domain, kPurposeDiffuse, prn);
    for (int b = 0; b < kSos / 4; ++b) {
        double u[8];
        block_uniforms(key, n0, band, b, u);
        for (int i = 0; i < 4; ++i) {
            const int n = 4 * b + i;
            const double alpha = 2.0 * M_PI * (n + u[2 * i]) / kSos;
            sos_c_[n] = std::cos(alpha);
            sos_ph_[n] = 2.0 * M_PI * u[2 * i + 1];
        }
    }
    on_ = true;
}

double ChannelProcess::distance(double t_s) const {
    if (mt_.empty()) return speed_ * t_s;
    if (t_s <= mt_.front()) return md_.front();
    if (t_s >= mt_.back()) return md_.back();
    const size_t i = static_cast<size_t>(
        std::upper_bound(mt_.begin(), mt_.end(), t_s) - mt_.begin()) - 1;
    const double dt = mt_[i + 1] - mt_[i];
    const double f = dt > 0.0 ? (t_s - mt_[i]) / dt : 0.0;
    return md_[i] + f * (md_[i + 1] - md_[i]);
}

// Diffuse multipath at time t: each phasor turns with the travelled distance
// in wavelengths, plus kDopplerMin cycles/s for the satellite's own motion.
std::complex<double> ChannelProcess::diffuse(double t_s) const {
    const double r = distance(t_s) / lambda_ + kDopplerMin * t_s;
    double re = 0.0, im = 0.0;
    for (int n = 0; n < kSos; ++n) {
        const double ph = 2.0 * M_PI * sos_c_[n] * r + sos_ph_[n];
        re += std::cos(ph);
        im += std::sin(ph);
    }
    const double s = 1.0 / std::sqrt(static_cast<double>(kSos));
    return {re * s, im * s};
}

void ChannelProcess::state_params(double z, double &mu, double &sig,
                                  double &mp) const {
    const double s1 = smooth01((z - p_.z_los) / kStateBlend + 0.5);
    const double s2 = smooth01((z - p_.z_shadow) / kStateBlend + 0.5);
    double v[3];
    for (int k = 0; k < 3; ++k)
        v[k] = st_[0][k] + s1 * (st_[1][k] - st_[0][k]) + s2 * (st_[2][k] - st_[1][k]);
    mu = v[0]; sig = v[1]; mp = v[2];
}

void ChannelProcess::components(double t_s, double out[8]) {
    for (int k = 0; k < 8; ++k) out[k] = 0.0;
    if (!on_) { out[2] = 1.0; return; }
    const double d = distance(t_s);
    const double z = state_.at(d / d_state_ + t_s / kTauStateMax);
    const double x = shadow_.at(d / d_shadow_ + t_s / kTauShadowMax);
    const std::complex<double> w = diffuse(t_s);
    double mu, sig, mp;
    state_params(z, mu, sig, mp);
    out[0] = z;
    out[1] = x;
    out[2] = std::pow(10.0, (mu + sig * x) / 20.0);
    out[3] = w.real();
    out[4] = w.imag();
    out[5] = mu;
    out[6] = sig;
    out[7] = mp;
}

std::complex<double> ChannelProcess::gain(double t_s) {
    if (!on_) return {1.0, 0.0};
    double o[8];
    components(t_s, o);
    const double a = std::pow(10.0, o[7] / 20.0);
    return {o[2] + a * o[3], a * o[4]};
}
}  // namespace gs

// C-linkage shims so the model is loadable via ctypes.
extern "C" void fading_gain_complex(const FadingCfg *c, int prn, double t_s,
                                    double *out) {
    gs::ChannelProcess p(*c, prn);
    const std::complex<double> g = p.gain(t_s);
    out[0] = g.real();
    out[1] = g.imag();
}
extern "C" void fading_gain_series(const FadingCfg *c, int prn, double t0_s,
                                   double dt_s, int n, double *out) {
    gs::ChannelProcess p(*c, prn);
    for (int i = 0; i < n; ++i) {
        const std::complex<double> g = p.gain(t0_s + i * dt_s);
        out[2 * i] = g.real();
        out[2 * i + 1] = g.imag();
    }
}
extern "C" void fading_components(const FadingCfg *c, int prn, double t_s,
                                  double *out) {
    gs::ChannelProcess p(*c, prn);
    p.components(t_s, out);
}
extern "C" void fading_params(const FadingCfg *c, int prn, double *out) {
    gs::ChannelProcess p(*c, prn);
    const gs::FadingParams &q = p.params();
    const double v[9] = {q.doppler_hz, q.dt_state_s, q.dt_shadow_s,
                         q.dt_diffuse_s, q.dt_knot_s, q.p_los, q.p_shadow,
                         q.z_los, q.z_shadow};
    for (int i = 0; i < 9; ++i) out[i] = v[i];
}
extern "C" void fading_chacha20_block(const uint8_t *key, uint32_t counter,
                                      const uint8_t *nonce, uint8_t *out) {
    gs::chacha20_block(key, counter, nonce, out);
}
