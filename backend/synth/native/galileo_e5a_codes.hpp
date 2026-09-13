// backend/synth/native/galileo_e5a_codes.hpp
// Galileo E5a-I (data) and E5a-Q (pilot) civil ranging codes: fixed
// 10230-chip memory codes at 10.23 Mcps (1 ms primary period), Galileo OS
// SIS ICD Annex C. NOT LFSR-generated -- the chip tables are compiled in
// from tools/gen_galileo_e5a.py (galileo_e5a_codes.cpp), the same
// memory-code approach as Galileo E1-B/E1-C (galileo_e1_codes.cpp).
// Internal C++ helpers -- NOT part of the extern "C" ABI.
#pragma once
#include <cstdint>

namespace gs {
void e5a_i(int prn, int8_t *out, int n);   // n >= 10230
void e5a_q(int prn, int8_t *out, int n);   // n >= 10230
}  // namespace gs
