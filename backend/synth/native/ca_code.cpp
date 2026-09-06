// backend/synth/native/ca_code.cpp
#include "abi.h"
#include <array>
#include <utility>
// QZSS L1C/A (PRN 193..202) and SBAS L1 (PRN 120..158) reuse the GPS Gold
// construction with a published per-PRN G2 code-phase delay. Tables only --
// see the header for the IS-QZSS-PNT / RTCA DO-229 citations.
#include "qzss_sbas_taps.hpp"

namespace {
// G2 phase-select tap pairs (1-indexed register stages) for PRN 1..32,
// IS-GPS-200 Table 3-Ia.
constexpr std::array<std::pair<int, int>, 32> kG2Taps = {{
    {2, 6}, {3, 7}, {4, 8}, {5, 9}, {1, 9}, {2, 10}, {1, 8}, {2, 9}, {3, 10}, {2, 3},
    {3, 4}, {5, 6}, {6, 7}, {7, 8}, {8, 9}, {9, 10}, {1, 4}, {2, 5}, {3, 6}, {4, 7},
    {5, 8}, {6, 9}, {1, 3}, {4, 6}, {5, 7}, {6, 8}, {7, 9}, {8, 10}, {1, 6}, {2, 7},
    {3, 8}, {4, 9},
}};

// Advance the GPS G1/G2 Fibonacci LFSRs one chip. Stage arrays are 1-indexed
// (g[1] newest, g[10] oldest / output stage). Shared by every L1-group code.
inline void step_g1g2(int *g1, int *g2) {
    int f1 = g1[3] ^ g1[10];
    int f2 = g2[2] ^ g2[3] ^ g2[6] ^ g2[8] ^ g2[9] ^ g2[10];
    for (int i = 10; i > 1; --i) { g1[i] = g1[i - 1]; g2[i] = g2[i - 1]; }
    g1[1] = f1; g2[1] = f2;
}

// GPS-form Gold code: G2 phase selected by a stage tap pair (t0, t1).
void gps_gold_taps(int t0, int t1, int8_t *out) {
    int g1[11], g2[11];
    for (int i = 1; i <= 10; ++i) g1[i] = g2[i] = 1;
    for (int k = 0; k < 1023; ++k) {
        int chip = g1[10] ^ g2[t0] ^ g2[t1];     // 0/1
        out[k] = chip ? -1 : 1;                   // 0 -> +1, 1 -> -1
        step_g1g2(g1, g2);
    }
}

// QZSS/SBAS-form Gold code: G2 sequence cyclically delayed by g2_delay chips.
void gps_gold_g2delay(int g2_delay, int8_t *out) {
    int g1[11], g2[11];
    for (int i = 1; i <= 10; ++i) g1[i] = g2[i] = 1;
    int g1seq[1023], g2seq[1023];
    for (int k = 0; k < 1023; ++k) {
        g1seq[k] = g1[10];
        g2seq[k] = g2[10];
        step_g1g2(g1, g2);
    }
    for (int k = 0; k < 1023; ++k) {
        int chip = g1seq[k] ^ g2seq[(k + g2_delay) % 1023];
        out[k] = chip ? -1 : 1;
    }
}
}  // namespace

// CODE-GEN sys int (distinct from the propagation sys int in synth_sat_state_sys):
//   0 GPS, 1 QZSS, 2 SBAS, 3 BeiDou B1I (Task 7), 4 GLONASS G1 (Task 12).
// Fills `primary` with {-1,+1} chips; `prim_len` must be >= the code length.
// `secondary` is filled only when `sec_len > 0` -- GPS/QZSS/SBAS L1 have no
// secondary code, so it is left untouched. Returns 0 ok, -1 bad args/unsupported.
extern "C" int synth_code(int sys, int prn, int8_t *primary, int prim_len,
                          int8_t *secondary, int sec_len) {
    (void)secondary;
    (void)sec_len;
    if (primary == nullptr || prim_len < 1023) return -1;
    switch (sys) {
    case 0:  // GPS L1 C/A
        if (prn < 1 || prn > 32) return -1;
        gps_gold_taps(kG2Taps[prn - 1].first, kG2Taps[prn - 1].second, primary);
        return 0;
    case 1:  // QZSS L1 C/A
        if (prn < 193 || prn > 202) return -1;
        gps_gold_g2delay(kQzssG2Delay[prn - 193], primary);
        return 0;
    case 2:  // SBAS L1
        if (prn < 120 || prn > 158) return -1;
        gps_gold_g2delay(kSbasG2Delay[prn - 120], primary);
        return 0;
    case 3:  // BeiDou B1I  -- Task 7
    case 4:  // GLONASS G1  -- Task 12
    default:
        return -1;
    }
}

int synth_ca_code(int prn, int8_t *out, int n) {
    return synth_code(0, prn, out, n, nullptr, 0);
}
