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
#ifdef __cplusplus
}
#endif
