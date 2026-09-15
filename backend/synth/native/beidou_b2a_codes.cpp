// backend/synth/native/beidou_b2a_codes.cpp
// BeiDou B2a data/pilot primary ranging codes (BDS-SIS-ICD-B2a-1.0 sec 5,
// Figures 5-2/5-3). Two 13-bit Fibonacci LFSRs (G1, G2) per component; G1 is
// common, seeded all-ones and reset to all-ones at chip 8190; G2 is seeded
// per-PRN from the ICD's Table 5-2 (data) / Table 5-3 (pilot) initial-phase
// values. Ported from GNSS-SDR's beidou_b2a_signal_replica.cc (PR #1093,
// branch `next`, GPL-3.0-or-later) -- taps and G2-init tables transcribed
// verbatim; this is real ICD data, not a compromise/placeholder (unlike the
// GPS L2C/L5 per-PRN seed tables elsewhere in this directory).
#include "beidou_b2a_codes.hpp"
#include <array>
#include <cstdint>

namespace {

// Register-2 initial phase s2,1 .. s2,13 for PRN 1..63 (ICD Table 5-2).
constexpr std::array<uint16_t, 63> kDataG2Init = {
    0x1025, 0x1034, 0x10AD, 0x114F, 0x1155, 0x11AE, 0x11EE, 0x11FB,
    0x1329, 0x13DA, 0x1435, 0x1444, 0x1455, 0x145B, 0x145C, 0x14A3,
    0x14F7, 0x1501, 0x153E, 0x15AB, 0x15B1, 0x1653, 0x1662, 0x1698,
    0x16B6, 0x16F2, 0x16FF, 0x1712, 0x173C, 0x17A1, 0x17C8, 0x17D4,
    0x17EB, 0x17F3, 0x1851, 0x1894, 0x18B7, 0x1911, 0x1919, 0x19AB,
    0x19B1, 0x19D2, 0x1A55, 0x1A74, 0x1ACB, 0x1B57, 0x1C34, 0x1C83,
    0x1C8B, 0x1CA3, 0x1CA8, 0x1D3B, 0x1D97, 0x1E48, 0x1E94, 0x1E99,
    0x1EDA, 0x1EF8, 0x1EFF, 0x1FB5, 0x0402, 0x1BF5, 0x03D2};

// Same for PRN 1-60; PRN 61-63 differ (ICD Table 5-3).
constexpr std::array<uint16_t, 63> kPilotG2Init = {
    0x1025, 0x1034, 0x10AD, 0x114F, 0x1155, 0x11AE, 0x11EE, 0x11FB,
    0x1329, 0x13DA, 0x1435, 0x1444, 0x1455, 0x145B, 0x145C, 0x14A3,
    0x14F7, 0x1501, 0x153E, 0x15AB, 0x15B1, 0x1653, 0x1662, 0x1698,
    0x16B6, 0x16F2, 0x16FF, 0x1712, 0x173C, 0x17A1, 0x17C8, 0x17D4,
    0x17EB, 0x17F3, 0x1851, 0x1894, 0x18B7, 0x1911, 0x1919, 0x19AB,
    0x19B1, 0x19D2, 0x1A55, 0x1A74, 0x1ACB, 0x1B57, 0x1C34, 0x1C83,
    0x1C8B, 0x1CA3, 0x1CA8, 0x1D3B, 0x1D97, 0x1E48, 0x1E94, 0x1E99,
    0x1EDA, 0x1EF8, 0x1EFF, 0x1FB5, 0x1486, 0x05F8, 0x0355};

// ICD (5-1): data g1(x)=1+x+x^5+x^11+x^13, g2(x)=1+x^3+x^5+x^9+x^11+x^12+x^13
constexpr int kDataG1Taps[4] = {0, 4, 10, 12};
constexpr int kDataG2Taps[6] = {2, 4, 8, 10, 11, 12};
// ICD (5-2): pilot g1(x)=1+x^3+x^6+x^7+x^13, g2(x)=1+x+x^5+x^7+x^8+x^12+x^13
constexpr int kPilotG1Taps[4] = {2, 5, 6, 12};
constexpr int kPilotG2Taps[6] = {0, 4, 6, 7, 11, 12};

constexpr int kCodeLen = 10230;
constexpr int kG1ResetChip = 8190;

void generate(int prn, const std::array<uint16_t, 63> &g2_init,
              const int *g1_taps, int n_g1_taps, const int *g2_taps,
              int n_g2_taps, int8_t *out, int n) {
    if (out == nullptr || n < kCodeLen) return;
    if (prn < 1 || prn > 63) return;
    const int idx = prn - 1;

    int g1[13];
    int g2[13];
    for (int i = 0; i < 13; ++i) {
        g1[i] = 1;
        g2[i] = static_cast<int>((g2_init[static_cast<size_t>(idx)] >> (12 - i)) & 1U);
    }

    for (int k = 0; k < kCodeLen; ++k) {
        int chip = g1[12] ^ g2[12];
        out[k] = chip ? int8_t(-1) : int8_t(1);
        int fb1 = 0, fb2 = 0;
        for (int t = 0; t < n_g1_taps; ++t) fb1 ^= g1[g1_taps[t]];
        for (int t = 0; t < n_g2_taps; ++t) fb2 ^= g2[g2_taps[t]];
        for (int i = 12; i > 0; --i) { g1[i] = g1[i - 1]; g2[i] = g2[i - 1]; }
        g1[0] = fb1;
        g2[0] = fb2;
        if ((k + 1) == kG1ResetChip) {
            for (int &v : g1) v = 1;
        }
    }
}

}  // namespace

namespace gs {

void b2a_d(int prn, int8_t *out, int n) {
    generate(prn, kDataG2Init, kDataG1Taps, 4, kDataG2Taps, 6, out, n);
}

void b2a_p(int prn, int8_t *out, int n) {
    generate(prn, kPilotG2Init, kPilotG1Taps, 4, kPilotG2Taps, 6, out, n);
}

}  // namespace gs
