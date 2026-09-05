// backend/synth/native/ca_code.cpp
#include "abi.h"
#include <array>
#include <utility>

namespace {
// G2 phase-select tap pairs (1-indexed register stages) for PRN 1..32,
// IS-GPS-200 Table 3-Ia.
constexpr std::array<std::pair<int, int>, 32> kG2Taps = {{
    {2, 6}, {3, 7}, {4, 8}, {5, 9}, {1, 9}, {2, 10}, {1, 8}, {2, 9}, {3, 10}, {2, 3},
    {3, 4}, {5, 6}, {6, 7}, {7, 8}, {8, 9}, {9, 10}, {1, 4}, {2, 5}, {3, 6}, {4, 7},
    {5, 8}, {6, 9}, {1, 3}, {4, 6}, {5, 7}, {6, 8}, {7, 9}, {8, 10}, {1, 6}, {2, 7},
    {3, 8}, {4, 9},
}};
}  // namespace

int synth_ca_code(int prn, int8_t *out, int n) {
    if (prn < 1 || prn > 32 || n < 1023 || out == nullptr) return -1;
    int g1[11], g2[11];
    for (int i = 1; i <= 10; ++i) g1[i] = g2[i] = 1;
    const auto [t0, t1] = kG2Taps[prn - 1];
    for (int k = 0; k < 1023; ++k) {
        int g2i = g2[t0] ^ g2[t1];
        int chip = g1[10] ^ g2i;                 // 0/1
        out[k] = chip ? -1 : 1;                   // 0 -> +1, 1 -> -1
        int f1 = g1[3] ^ g1[10];
        int f2 = g2[2] ^ g2[3] ^ g2[6] ^ g2[8] ^ g2[9] ^ g2[10];
        for (int i = 10; i > 1; --i) { g1[i] = g1[i - 1]; g2[i] = g2[i - 1]; }
        g1[1] = f1; g2[1] = f2;
    }
    return 0;
}
