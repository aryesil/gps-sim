// backend/synth/native/beidou_b2a_codes.hpp
#pragma once
#include <cstdint>
namespace gs {
// BeiDou B2a data component (10230 chips, {-1,+1}). prn 1..63.
void b2a_d(int prn, int8_t *out, int n);
// BeiDou B2a pilot component (10230 chips, {-1,+1}). prn 1..63.
void b2a_p(int prn, int8_t *out, int n);
}  // namespace gs
