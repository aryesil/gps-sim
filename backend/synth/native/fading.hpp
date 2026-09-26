// backend/synth/native/fading.hpp
#pragma once
#include <complex>
#include <cstdint>
#include <vector>
namespace gs {
// ABI 28 -- land-mobile-satellite (LMS) channel per SV and signal.
//
// The complex gain multiplying the satellite's line-of-sight signal is a
// three-state Loo model:
//   h(t) = 10^((mu + Sigma*x(t))/20) + 10^(MP/20) * w(t)
// * a slow Gaussian state process z(t), thresholded by elevation-dependent
//   probabilities into line-of-sight / shadowed / blocked (with a short
//   blend across each threshold), picks (mu, Sigma, MP);
// * x(t) is exponentially correlated unit shadowing of the direct path;
// * w(t) is unit-power complex diffuse multipath with a Jakes (classical)
//   Doppler spectrum of spread f_D = speed / wavelength (sum of sinusoids).
// All three advance with the distance the receiver has travelled (a constant
// speed, or a piecewise-linear distance profile from a waypoint route), plus
// a slow time term for the satellite's own motion, so a stopped receiver
// fades slowly and a moving one at the rate its speed sets.
// z and x depend only on (key, system, prn), so every band of one satellite
// sees the same blockage and shadowing; w is independent per carrier.
// All three are drawn from ChaCha20 and evaluated by random access, so a
// gain depends only on (config, prn, t) -- never on fs, block size or thread
// count.
struct FadingCfg {
    int model;            // 0 = off, 1 = seeded, 2 = keyed
    int env;              // 0 open sky, 1 rural/tree, 2 suburban, 3 urban
    double speed_mps;     // constant receiver speed (used when motion_n == 0)
    double carrier_hz;    // this signal's carrier (diffuse Doppler spread)
    double el_deg;        // satellite elevation (state probabilities)
    uint64_t seed;        // seeded model: key = ChaCha20(0-key, nonce=seed)
    uint8_t key[32];      // keyed model: 256-bit ChaCha20 key
    int domain;           // ASCII system letter (systems reuse PRN numbers)
    // Distance travelled (m) at times motion_t (s, increasing), linearly
    // interpolated and held beyond the ends. motion_n >= 2 replaces
    // speed_mps. The arrays are copied when the channel is built.
    int motion_n;
    const double *motion_t;
    const double *motion_d;
};

struct FadingParams {
    double doppler_hz;    // f_D of the diffuse part at the top speed
    double dt_state_s;    // state process grid at the top speed (tau / 32)
    double dt_shadow_s;   // shadowing process grid at the top speed (tau / 32)
    double dt_diffuse_s;  // 1 / (32 f_D): knot spacing that resolves w(t)
    double dt_knot_s;     // gain-knot spacing the engine hands the mixer
    double p_los, p_shadow;   // state probabilities at this elevation
    double z_los, z_shadow;   // state thresholds on z
};

// Exponentially correlated unit Gaussian process on a uniform time grid:
// AR(1) taps over ChaCha20 innovations. Keeps a window of ChaCha20 blocks so
// the forward walk of a synthesis run draws every block once; any other
// access order still returns the same values.
class GridProcess {
public:
    void init(const uint8_t key[32], int domain, int purpose, int prn,
              double dt);
    // Linear interpolation of the grid process at coordinate u (grid step
    // dt; u must not decrease between calls for the cache to help).
    double at(double u);
private:
    double value(int64_t m);
    double normal(int64_t q);
    uint8_t key_[32] = {};
    uint32_t n0_ = 0;
    double dt_ = 1.0;
    int64_t b0_ = 0;
    std::vector<double> buf_;
    int64_t last_m_ = INT64_MIN;
    double last_v0_ = 0.0, last_v1_ = 0.0;
};

class ChannelProcess {
public:
    ChannelProcess() = default;
    ChannelProcess(const FadingCfg &c, int prn);
    bool enabled() const { return on_; }
    const FadingParams &params() const { return p_; }
    std::complex<double> gain(double t_s);
    // out: z, x_shadow, direct amplitude, diffuse re, diffuse im (unit
    // power), mu_db, sigma_db, mp_db.
    void components(double t_s, double out[8]);
private:
    void state_params(double z, double &mu, double &sig, double &mp) const;
    std::complex<double> diffuse(double t_s) const;
    double distance(double t_s) const;
    bool on_ = false;
    double speed_ = 0.0;           // constant speed when there is no profile
    std::vector<double> mt_, md_;  // distance profile
    double lambda_ = 0.19;         // carrier wavelength (m)
    double d_state_ = 1.0, d_shadow_ = 1.0;   // correlation distances (m)
    FadingParams p_{};
    double st_[3][3] = {};   // [state][mu, sigma, mp] in dB
    GridProcess state_, shadow_;
    double sos_c_[32] = {};   // diffuse arrival-angle cosines
    double sos_ph_[32] = {};  // diffuse sinusoid phases (rad)
};

// RFC 8439 ChaCha20 block function (exposed for test vectors).
void chacha20_block(const uint8_t key[32], uint32_t counter,
                    const uint8_t nonce[12], uint8_t out[64]);
}  // namespace gs
