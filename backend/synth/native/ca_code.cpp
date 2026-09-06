// backend/synth/native/ca_code.cpp
#include "abi.h"
#include <array>
#include <utility>
// QZSS L1C/A (PRN 193..202) and SBAS L1 (PRN 120..158) reuse the GPS Gold
// construction with a published per-PRN G2 code-phase delay. Tables only --
// see the header for the IS-QZSS-PNT / RTCA DO-229 citations.
#include "qzss_sbas_taps.hpp"
// BeiDou B1I (Task 7) reuses only the published G2 phase-assignment table and
// the Neumann-Hoffman secondary -- tables only, see the header for the
// BDS-SIS-ICD-B1I v3.0 citation.
#include "b1i_taps.hpp"

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
// BeiDou B1I ranging code: two 11-bit Fibonacci LFSRs (G1, G2), both reset to
// 01010101010 every 2046 chips. G1(x) = 1 + x + x^7 + x^8 + x^9 + x^10 + x^11,
// G2(x) = 1 + x + x^2 + x^3 + x^4 + x^5 + x^8 + x^9 + x^11 (BDS-SIS-ICD-B1I
// v3.0 sec 4.3). The PRN-specific G2 output is the XOR of the two (or three,
// for PRN 38..63) tapped stages in b1i_taps. Stage array r[0..10] is oldest
// (output) to newest (feedback in). Matches GNSS-SDR beidou_b1i_signal_replica.
void b1i_ranging(int t1, int t2, int t3, int8_t *out) {
    int r1[11], r2[11];
    for (int i = 0; i < 11; ++i) r1[i] = r2[i] = i & 1;   // 01010101010
    for (int k = 0; k < b1i_taps::kCodeLen; ++k) {
        int g1 = r1[0];
        int g2 = r2[11 - t1] ^ r2[11 - t2] ^ (t3 ? r2[11 - t3] : 0);
        out[k] = (g1 ^ g2) ? int8_t(-1) : int8_t(1);   // bit 0 -> +1, 1 -> -1
        int f1 = r1[0] ^ r1[1] ^ r1[2] ^ r1[3] ^ r1[4] ^ r1[10];
        int f2 = r2[0] ^ r2[2] ^ r2[3] ^ r2[6] ^ r2[7] ^ r2[8] ^ r2[9] ^ r2[10];
        for (int i = 0; i < 10; ++i) { r1[i] = r1[i + 1]; r2[i] = r2[i + 1]; }
        r1[10] = f1; r2[10] = f2;
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
    case 3: {  // BeiDou B1I  -- Task 7
        if (prn < 1 || prn > 63 || prim_len < b1i_taps::kCodeLen) return -1;
        const int i = prn - 1;
        b1i_ranging(b1i_taps::kB1iG2Tap1[i], b1i_taps::kB1iG2Tap2[i],
                    b1i_taps::kB1iG2Tap3[i], primary);
        if (secondary && sec_len >= 20) {
            const bool geo = b1i_taps::is_geo(prn);
            for (int c = 0; c < 20; ++c)
                secondary[c] = geo ? int8_t(1) : b1i_taps::kNH20[c];
        }
        return 0;
    }
    case 4:  // GLONASS G1  -- Task 12
    default:
        return -1;
    }
}

int synth_ca_code(int prn, int8_t *out, int n) {
    return synth_code(0, prn, out, n, nullptr, 0);
}
