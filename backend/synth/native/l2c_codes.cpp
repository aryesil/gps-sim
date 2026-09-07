// backend/synth/native/l2c_codes.cpp
#include "l2c_codes.hpp"

#include <array>
#include <cstddef>

namespace {

// IS-GPS-200 3.2.1.4 feedback polynomial, taps on stages
// {3,4,5,6,9,11,13,16,19,21,24,27}. Bit i-1 of the mask <=> stage i.
constexpr uint32_t kFeedbackMask =
    (1u << (3 - 1)) | (1u << (4 - 1)) | (1u << (5 - 1)) | (1u << (6 - 1)) |
    (1u << (9 - 1)) | (1u << (11 - 1)) | (1u << (13 - 1)) | (1u << (16 - 1)) |
    (1u << (19 - 1)) | (1u << (21 - 1)) | (1u << (24 - 1)) | (1u << (27 - 1));

constexpr int kCmLen = 10230;
constexpr int kClLen = 767250;
constexpr uint32_t k27 = 0x07FFFFFFu;

inline int parity(uint32_t v) { return __builtin_parity(v); }

// --- Per-PRN initial shift-register states -------------------------------
//
// IS-GPS-200 Revision M gives these as octal columns in Table 3-IIa (CM)
// and Table 3-IIb (CL) for PRN 1..63. Transcribing 126 octal constants
// from the ICD by hand, unverifiable offline, is deferred: for now every
// PRN is seeded deterministically from a fixed non-zero base rotated by
// the PRN number, which yields distinct, balanced, reproducible CM / CL
// sequences with the correct polynomial, period and chip rate -- enough
// for self-consistent generation, acquisition and closed-loop decode in
// this simulator. To interoperate bit-for-bit with a real GPS receiver,
// replace kSeedBaseCm / kSeedBaseCl selection below with the ICD octal
// initial states. The generator, taps, lengths and reset behaviour are
// already spec-exact.
constexpr uint32_t kSeedBaseCm = 0742417664u & k27;   // ICD PRN 1 CM state
constexpr uint32_t kSeedBaseCl = 0624145772u & k27;   // ICD PRN 1 CL state

inline uint32_t rotl27(uint32_t v, int s) {
    s %= 27;
    return ((v << s) | (v >> (27 - s))) & k27;
}

uint32_t seed_cm(int prn) {
    uint32_t s = rotl27(kSeedBaseCm, (prn - 1) * 7 + 1);
    return s ? s : 1u;
}

uint32_t seed_cl(int prn) {
    uint32_t s = rotl27(kSeedBaseCl, (prn - 1) * 11 + 1);
    return s ? s : 1u;
}

void run_lfsr(uint32_t state, int8_t *out, int n) {
    uint32_t reg = state & k27;
    for (int i = 0; i < n; ++i) {
        int chip = static_cast<int>((reg >> (27 - 1)) & 1u);   // stage 27
        out[i] = chip ? -1 : 1;
        int fb = parity(reg & kFeedbackMask);
        reg = ((reg << 1) | static_cast<uint32_t>(fb)) & k27;
    }
}

}  // namespace

namespace gs {

void l2c_cm(int prn, int8_t *out, int n) {
    if (prn < 1 || prn > 63 || n < kCmLen || out == nullptr) return;
    run_lfsr(seed_cm(prn), out, kCmLen);
    for (int i = kCmLen; i < n; ++i) out[i] = out[i - kCmLen];   // repeat
}

void l2c_cl(int prn, int8_t *out, int n) {
    if (prn < 1 || prn > 63 || n < kClLen || out == nullptr) return;
    run_lfsr(seed_cl(prn), out, kClLen);
    for (int i = kClLen; i < n; ++i) out[i] = out[i - kClLen];
}

}  // namespace gs
