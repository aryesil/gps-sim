// backend/synth/native/abi.h
#pragma once
#include <stdint.h>
#ifdef __cplusplus
#include "fading.hpp"
using gs::FadingCfg;
#else
typedef struct {
    int model;
    double sigma_db;
    double coherence_s;
    uint64_t seed;
} FadingCfg;
#endif
#ifdef __cplusplus
extern "C" {
#endif
int synth_abi_version(void);
// Deterministic per-SV fading gain (linear), C-linkage shim over
// gs::fading_gain_linear. Depends only on (seed, prn, floor(t_s/coherence_s))
// plus smoothstep interpolation to the next knot.
float fading_gain_linear(const FadingCfg *c, int prn, double t_s);
// Fills out[0..8] with: l1_hz, ca_chip_hz, ca_code_len, nav_bit_hz, mu,
// omega_e_dot, c, f_rel, gps_utc_leap. `n` must be >= 9.
void synth_constants(double *out, int n);
// Writes 1023 values in {-1,+1} to out[0..1022] for the L1 C/A code of `prn`
// (1..32). Chip bit 0 -> +1, 1 -> -1. Returns 0 on success, -1 on bad prn /
// n < 1023 / null out.
int synth_ca_code(int prn, int8_t *out, int n);
// Broadcast Keplerian ephemeris: 21 doubles, layout frozen. `_pad` keeps the
// struct an even number of doubles; set it to 0.0.
typedef struct {
    double sqrtA, e, m0, delta_n, omega, omega0, omega_dot;
    double i0, idot, cuc, cus, crc, crs, cic, cis;
    double toe, toc, af0, af1, af2, _pad;
} KeplerEph;
// Propagate `e` to `t_gps` (GPS seconds of week). Writes ECEF position (m) to
// pos3[0..2], ECEF velocity (m/s, 0.5 s central difference on the orbit plane)
// to vel3[0..2], and the SV clock correction (s) to clk1[0].
void synth_sat_state(const KeplerEph *e, double t_gps,
                     double *pos3, double *vel3, double *clk1);
// Debug shim: fills iq_interleaved[0..2*n-1] with I,Q,I,Q,... samples of a
// unit-amplitude complex carrier at freq_hz for sample rate fs, from a single
// phase-continuous NCO starting at phase 0.
void synth_debug_carrier(double freq_hz, double fs, int n, float *iq_interleaved);
// Debug shim: fills out[0..n-1] with nav symbols ({+1,-1}) at 50 Hz nav-bit rate.
// mode: 0=Zero (always +1), 1=KnownFrame (repeats bits[0..nbits-1]).
void synth_debug_nav(int mode, const int8_t *bits, int nbits,
                     double fs, int n, int8_t *out);
// Debug shim: zeroes iq[0..2*n-1], then synthesizes a single satellite
// (gain 1, nav mode Zero) into it via mix_block from absolute sample 0.
// iq holds I,Q,I,Q,... interleaved (2*n floats).
void synth_debug_one_sv(const int8_t *code, double code_rate, double code_phase0,
                        double code_doppler, double carrier_freq, double fs,
                        int n, float *iq);
// Per-SV channel spec for a full run. `code` points at 1023 int8 values in
// {-1,+1}, owned by the caller for the whole synth_run call. Field order is
// frozen -- _lib.py mirrors it exactly.
typedef struct {
    const int8_t *code;        // 1023 int8, owned by caller
    double carrier_freq_hz;    // carrier Doppler at t=0 (complex baseband; L1 is the recorder LO)
    double carrier_phase0_rad;
    double code_phase0_chips;
    double code_doppler_hz;    // constant over the run (Phase 1 approximation)
    int    nav_mode;           // 0 zero, 1 known_frame
    const int8_t *nav_bits;    // may be NULL when nav_mode == 0
    int    nav_nbits;
    float  gain;               // static per-SV gain
    int    prn;                // 1..32; keys deterministic per-SV models
    FadingCfg fading;          // deterministic per-SV fading (model 0 = off)
} SvSpec;
// Whole-run spec. Field order frozen -- _lib.py mirrors it exactly.
typedef struct {
    double   fs;
    int      quant;            // 0 int8, 1 int12, 2 int16
    int      dither;           // 0/1 (reserved, unused in Phase 1)
    uint64_t total_samples;
    int      block_samples;    // e.g. 65536
    int      nthreads;         // 0 = hardware_concurrency
} RunSpec;
// Streams interleaved I,Q samples to `path` (int8 or int16 words per `quant`).
// Calls progress(fraction, user) after every block when non-NULL.
// Returns 0 on success, negative on error.
int synth_run(const char *path, const RunSpec *rs,
              const SvSpec *svs, int nsv,
              void (*progress)(double, void *), void *user);
#ifdef __cplusplus
}
#endif
