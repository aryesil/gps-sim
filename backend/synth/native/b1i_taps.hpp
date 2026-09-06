// backend/synth/native/b1i_taps.hpp
//
// Published constant tables for the BeiDou B1I ranging code, per
// BDS-SIS-ICD-B1I v3.0 (2019), "Ranging Code" section and Table 5-6
// "Phase assignment of G2 sequence". PRN 1..37 are identical to
// BDS-SIS-ICD-B1I v2.0 Table 4-2; PRN 38..63 are the v3.0 extension, which
// adds an optional third G2 tap (kB1iG2Tap3, 0 = unused). Cross-checked
// chip-for-chip against GNSS-SDR
// `src/algorithms/libs/beidou_b1i_signal_replica.cc` (phase1/phase2/phase3
// arrays). TABLES ONLY -- no LFSR / DSP code lives here.
#pragma once
#include <cstdint>

namespace b1i_taps {

// Ranging code length (chips) and the reset period of the G1/G2 registers.
constexpr int kCodeLen = 2046;

// G2 phase-select taps: 1-indexed register stages (stage 1 = newest bit shifted
// in, stage 11 = oldest / output stage). The PRN-specific G2 output is
// stage[t1] XOR stage[t2] XOR (t3 ? stage[t3] : 0).  Index by prn-1, prn 1..63.
constexpr int8_t kB1iG2Tap1[63] = {
    1, 1, 1, 1, 1, 1, 1, 1, 2, 3, 3, 3, 3, 3, 3, 3, 4, 4, 4, 4, 4, 4,
    5, 5, 5, 5, 5, 6, 6, 6, 6, 8, 8, 8, 9, 9, 10,
    2, 3, 3, 3, 3, 3, 4, 4, 5, 5, 5, 5, 6, 8, 9, 9, 3, 5, 7, 4, 4, 5, 5, 5, 5, 6,
};
constexpr int8_t kB1iG2Tap2[63] = {
    3, 4, 5, 6, 8, 9, 10, 11, 7, 4, 5, 6, 8, 9, 10, 11, 5, 6, 8, 9, 10, 11,
    6, 8, 9, 10, 11, 8, 9, 10, 11, 9, 10, 11, 10, 11, 11,
    7, 4, 6, 8, 10, 11, 5, 9, 6, 8, 10, 11, 9, 9, 10, 11, 7, 7, 9, 5, 9, 6, 8, 10, 11, 9,
};
constexpr int8_t kB1iG2Tap3[63] = {
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 3, 3, 3, 3, 3, 3, 3,
};

// Neumann-Hoffman secondary code (20 chips, 1 kHz), BDS-SIS-ICD-B1I v3.0
// section on the NH-modulated D1 NAV. Modulated onto MEO/IGSO ranging codes;
// GEO satellites use a flat +1 secondary.
constexpr int8_t kNH20[20] = {
    -1, -1, -1, -1, -1, 1, -1, -1, 1, 1,
    -1, 1, -1, 1, -1, -1, 1, 1, 1, -1,
};

// GEO PRN mask: bit (prn-1) set for C01..C05 and C59..C63 (per Table 4-2
// "GEO satellite" rows and the ICD 3.0 numbering extension).
constexpr uint64_t kB1iGeoMask =
    0x1FULL | (0x1FULL << 58);

inline bool is_geo(int prn) {
    return prn >= 1 && prn <= 63 && ((kB1iGeoMask >> (prn - 1)) & 1ULL);
}

}  // namespace b1i_taps
