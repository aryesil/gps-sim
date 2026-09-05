// backend/synth/native/abi.h
#pragma once
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
int synth_abi_version(void);
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
#ifdef __cplusplus
}
#endif
