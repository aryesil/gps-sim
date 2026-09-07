// backend/synth/native/l5_codes.hpp
// GPS / QZSS L5 civil ranging codes: I5 and Q5, each 10230 chips at
// 10.23 Mcps (1 ms primary period). Each chip is XA(i) XOR XBi(i) where
//   XA  is a 13-stage LFSR, polynomial 1 + x^9 + x^10 + x^12 + x^13,
//       initial state all-ones, SHORT-CYCLED: reset to all-ones after
//       8190 chips (IS-GPS-200 3.3.2.2), and
//   XBi is a 13-stage LFSR, polynomial
//       1 + x + x^3 + x^4 + x^6 + x^7 + x^8 + x^12 + x^13, with a
//       per-PRN initial state (IS-GPS-200-M Table 3-Ia for I5,
//       Table 3-Ib for Q5).
// The ICD initial-state tables are not available offline; as in
// l2c_codes.cpp the per-PRN XB states are seeded deterministically by
// rotating a fixed base, which yields distinct, balanced, reproducible
// I5 / Q5 sequences with the correct polynomials, period and chip rate.
// The generators, taps, length and XA short-cycle are spec-exact.
// Internal C++ helpers -- NOT part of the extern "C" ABI.
#pragma once
#include <cstdint>

namespace gs {
void l5_i(int prn, int8_t *out, int n);   // n >= 10230
void l5_q(int prn, int8_t *out, int n);   // n >= 10230
}  // namespace gs
