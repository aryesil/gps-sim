// backend/synth/native/l5_codes.cpp
#include "l5_codes.hpp"

namespace {

constexpr int kLen = 10230;
constexpr uint32_t k13 = 0x1FFFu;         // 13-bit register mask
constexpr int kXaResetChip = 8190;        // XA short-cycle reset point

// XA feedback taps: stages {9, 10, 12, 13}. Bit i-1 <=> stage i.
constexpr uint32_t kXaMask =
    (1u << (9 - 1)) | (1u << (10 - 1)) | (1u << (12 - 1)) | (1u << (13 - 1));

// XB feedback taps: stages {1, 3, 4, 6, 7, 8, 12, 13}.
constexpr uint32_t kXbMask =
    (1u << (1 - 1)) | (1u << (3 - 1)) | (1u << (4 - 1)) | (1u << (6 - 1)) |
    (1u << (7 - 1)) | (1u << (8 - 1)) | (1u << (12 - 1)) | (1u << (13 - 1));

inline int parity(uint32_t v) { return __builtin_parity(v); }

inline uint32_t rotl13(uint32_t v, int s) {
    s = ((s % 13) + 13) % 13;
    return ((v << s) | (v >> (13 - s))) & k13;
}

// Per-PRN XB initial state. IS-GPS-200-M Table 3-Ia (I5) / 3-Ib (Q5) are
// not available offline: seed from a fixed non-zero base rotated by the
// PRN, distinct for the I5 and Q5 phases -- the same documented
// compromise as l2c_codes.cpp. Generator, taps, length and XA short
// cycle are already spec-exact.
constexpr uint32_t kXbBase = 0x1ACEu & k13;

uint32_t xb_seed(int prn, bool q) {
    uint32_t s = rotl13(kXbBase, (prn - 1) * (q ? 5 : 3) + (q ? 7 : 2));
    return s ? s : 1u;
}

void gen_one(uint32_t xb_state, int8_t *out) {
    uint32_t xa = k13;                 // all-ones
    uint32_t xb = xb_state & k13;
    for (int i = 0; i < kLen; ++i) {
        int xa_chip = static_cast<int>((xa >> (13 - 1)) & 1u);   // stage 13
        int xb_chip = static_cast<int>((xb >> (13 - 1)) & 1u);
        out[i] = (xa_chip ^ xb_chip) ? -1 : 1;

        int xaf = parity(xa & kXaMask);
        xa = ((xa << 1) | static_cast<uint32_t>(xaf)) & k13;
        if (i + 1 == kXaResetChip) xa = k13;                     // short cycle

        int xbf = parity(xb & kXbMask);
        xb = ((xb << 1) | static_cast<uint32_t>(xbf)) & k13;
    }
}

}  // namespace

namespace gs {

void l5_i(int prn, int8_t *out, int n) {
    if (prn < 1 || prn > 210 || n < kLen || out == nullptr) return;
    gen_one(xb_seed(prn, false), out);
    for (int i = kLen; i < n; ++i) out[i] = out[i - kLen];       // repeat
}

void l5_q(int prn, int8_t *out, int n) {
    if (prn < 1 || prn > 210 || n < kLen || out == nullptr) return;
    gen_one(xb_seed(prn, true), out);
    for (int i = kLen; i < n; ++i) out[i] = out[i - kLen];
}

}  // namespace gs
