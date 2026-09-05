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
#ifdef __cplusplus
}
#endif
