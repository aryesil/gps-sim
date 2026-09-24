// backend/synth/native/l2c_codes.hpp
// GPS L2C civil ranging codes: CM (10230 chips, 20 ms) and CL (767250
// chips, 1.5 s). Both are produced by the 27-stage modular shift register
// of IS-GPS-200 3.2.1.4 with polynomial
//   1 + x^3 + x^4 + x^5 + x^6 + x^9 + x^11 + x^13 + x^16 + x^19 + x^21
//     + x^24 + x^27
// truncated at the CM / CL period, seeded from the ICD's per-PRN initial
// states (Table 3-IIa, PRN 1..63 and 159..210). Internal C++ helpers --
// NOT part of the extern "C" ABI.
#pragma once
#include <cstdint>

namespace gs {
bool l2c_prn_valid(int prn);                // PRN 1..63 or 159..210
void l2c_cm(int prn, int8_t *out, int n);   // n >= 10230
void l2c_cl(int prn, int8_t *out, int n);   // n >= 767250
}  // namespace gs
