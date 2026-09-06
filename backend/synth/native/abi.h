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
// PROPAGATION sys-int: 0 GPS/QZSS, 1 Galileo, 2 BeiDou MEO/IGSO, 3 BeiDou GEO.
// Selects mu / omega_e_dot / f_rel and (sys==3) the GEO reference-frame
// rotation. This enum is SEPARATE from the code-generation `sys` int used by
// later signal tasks -- do not unify them. synth_sat_state is
// synth_sat_state_sys(e, 0, ...).
void synth_sat_state_sys(const KeplerEph *e, int sys, double t_gps,
                         double *pos3, double *vel3, double *clk1);
// GLONASS/SBAS broadcast state: an ECEF state vector plus a frame-constant
// luni-solar acceleration in the rotating PZ-90.11 frame at the broadcast
// epoch. Field order frozen -- _lib.py mirrors it exactly. x_m/y_m/z_m are
// metres, vx..vz m/s, ax..az m/s^2, tau/gamma seconds and s/s, toe_ref the
// broadcast epoch (seconds of week).
typedef struct {
    double x_m, y_m, z_m, vx, vy, vz, ax, ay, az, tau, gamma, toe_ref;
} GloEph;
// Propagate `e` to `t_gps` with the GLONASS ICD "simplified equations of
// motion" RK4 integrator (~60 s steps, final partial step for the remainder;
// dt_total == 0 returns the broadcast state unchanged). Writes ECEF position
// (m) to pos3[0..2], ECEF velocity (m/s) to vel3[0..2], SV clock correction
// (s) = -tau + gamma*dt_total to clk1[0].
void synth_glonass_state(const GloEph *e, double t_gps,
                         double *pos3, double *vel3, double *clk1);
// Debug shim: fills iq_interleaved[0..2*n-1] with I,Q,I,Q,... samples of a
// unit-amplitude complex carrier at freq_hz for sample rate fs, from a single
// phase-continuous NCO starting at phase 0.
void synth_debug_carrier(double freq_hz, double fs, int n, float *iq_interleaved);
// Debug shim: fills out[0..n-1] with nav symbols ({+1,-1}) at 50 Hz nav-bit rate.
// mode: 0=Zero (always +1), 1=KnownFrame (repeats bits[0..nbits-1]).
void synth_debug_nav(int mode, const int8_t *bits, int nbits,
                     double fs, int n, int8_t *out);
// Debug shim: fills out[0..n-1] with the BOC(1,1) square sub-carrier sign sequence
// ({+1,-1}) at frequency sub_hz for sample rate fs.
void synth_debug_boc(double sub_hz, double fs, int n, int8_t *out);
// Debug shim: zeroes iq[0..2*n-1], then synthesizes a single satellite
// (gain 1, nav mode Zero) into it via mix_block from absolute sample 0.
// iq holds I,Q,I,Q,... interleaved (2*n floats).
void synth_debug_one_sv(const int8_t *code, double code_rate, double code_phase0,
                        double code_doppler, double carrier_freq, double fs,
                        int n, float *iq);
// Debug shim: like synth_debug_one_sv but from an arbitrary absolute sample
// index `sample0` via gs::mix_block. Zeroes iq[0..2*n-1] first. Lets a test
// compare a single-shot span against the same span built from disjoint ranges.
void synth_debug_mix_range(const int8_t *code, double code_rate,
                           double code_phase0, double code_doppler,
                           double carrier_freq, double fs, uint64_t sample0,
                           int n, float *iq);
// Debug shim: one SV through gs::mix_block_parallel with an explicit thread
// count (mix_block_parallel zeroes iq itself). Lets a test compare nthreads=1
// against nthreads>1 over the same span.
void synth_debug_mix_parallel(const int8_t *code, double code_rate,
                              double code_phase0, double code_doppler,
                              double carrier_freq, double fs, uint64_t sample0,
                              int n, int nthreads, float *iq);
// Task 10 debug shims: like the three above but taking the five new SvSpec
// fields (sys / sub_carrier_hz / sec_code / sec_len / sec_rate_hz) so tests can
// exercise the BOC sign + secondary-code XOR path. Passing 0/nullptr for the
// five reproduces the Phase-1 shims exactly.
void synth_debug_one_sv_ex(const int8_t *code, double code_rate,
                           double code_phase0, double code_doppler,
                           double carrier_freq, double fs, int n,
                           int sys, double sub_carrier_hz,
                           const int8_t *sec_code, int sec_len,
                           double sec_rate_hz, float *iq);
void synth_debug_mix_range_ex(const int8_t *code, double code_rate,
                              double code_phase0, double code_doppler,
                              double carrier_freq, double fs, uint64_t sample0,
                              int n, int sys, double sub_carrier_hz,
                              const int8_t *sec_code, int sec_len,
                              double sec_rate_hz, float *iq);
void synth_debug_mix_parallel_ex(const int8_t *code, double code_rate,
                                 double code_phase0, double code_doppler,
                                 double carrier_freq, double fs,
                                 uint64_t sample0, int n, int nthreads,
                                 int sys, double sub_carrier_hz,
                                 const int8_t *sec_code, int sec_len,
                                 double sec_rate_hz, float *iq);
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
    // Task 10 -- appended after the frozen Phase-1 layout. Defaults 0/nullptr
    // leave the mixer byte-identical to Phase 1 (BOC + secondary both off).
    int    sys;                // CODE-GEN sys int (0 GPS/QZSS/SBAS, 1 Galileo E1,
                               // 2 BeiDou B1I, ...); informational for the mixer
    double sub_carrier_hz;     // > 0 => apply BOC(1,1) square sub-carrier sign
    const int8_t *sec_code;    // secondary code chips {-1,+1}, sec_len entries
    int    sec_len;            // > 0 => apply secondary-code XOR
    double sec_rate_hz;        // secondary chip rate (Hz)
    // Task 16b -- per-SV code geometry for the full-run path. 0 / 0.0 keep the
    // Phase-1 GPS defaults (1023 chips @ 1.023 Mcps) so debug/test shims that
    // zero-init SvSpec stay byte-identical.
    int    code_len;           // primary code length in chips; 0 => 1023
    double chip_rate_hz;       // primary chip rate (Hz); 0.0 => 1.023e6
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
// One band == one interleaved-IQ output file. Field order frozen -- _lib.py
// mirrors it exactly.
typedef struct {
    const char *out_path;      // one file per band
    double   fs;
    int      quant;            // 0 int8, 1 int12, 2 int16 (same as RunSpec)
    int      dither;
    uint64_t total_samples;
    int      block_samples;
    int      nthreads;
    const SvSpec *svs;         // this band's channels
    int      nsv;
} BandSpec;
// Streams one interleaved-IQ file per band (bands[0..nband-1]). Progress is
// forwarded per band as fraction 0..1 over that band's own sample count; with
// nband == 1 this is identical to synth_run's Phase-1 progress semantics.
// Returns the first non-zero per-band rc, else 0.
int synth_run_bands(const BandSpec *bands, int nband,
                    void (*progress)(double, void *), void *user);
// L1-group code generator. CODE-GEN sys int (SEPARATE from the propagation sys
// int of synth_sat_state_sys): 0 GPS, 1 QZSS, 2 SBAS, 3 BeiDou B1I, 4 GLONASS
// G1. Fills primary[0..prim_len-1] with {-1,+1} chips (prim_len must be >= the
// code length for that system). secondary is filled with {-1,+1} only when
// sec_len > 0; GPS/QZSS/SBAS L1 have no secondary code. Returns 0 on success,
// -1 on bad args / unsupported system. synth_ca_code(prn,out,n) ==
// synth_code(0, prn, out, n, nullptr, 0).
int synth_code(int sys, int prn, int8_t *primary, int prim_len,
               int8_t *secondary, int sec_len);
#ifdef __cplusplus
}
#endif
