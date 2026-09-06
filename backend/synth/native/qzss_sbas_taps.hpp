// backend/synth/native/qzss_sbas_taps.hpp
//
// Published constant tables ONLY -- no DSP logic here. This is the sanctioned
// "shared published constant table" exception to the self-validating-
// architecture rule.
//
// QZSS L1 C/A (PRN 193..202) and SBAS L1 (PRN 120..158) both reuse the GPS
// L1 C/A Gold-code construction (IS-GPS-200 G1/G2 1023-chip LFSRs); each PRN
// differs only by the cyclic phase delay applied to the G2 sequence.
//
//   * QZSS G2 code-phase delays: IS-QZSS-PNT.004, Table 3.2.2-2
//     ("L1C/A code phase assignments").
//   * SBAS G2 code-phase delays: RTCA DO-229 (SBAS L1 MOPS), Appendix A
//     (also ICAO Annex 10, Vol I, Appendix B).
//
// The values below are the G2 delay-chip column of those tables and match the
// widely-used reference implementation (RTKLIB L1 C/A code-phase table).
#pragma once

namespace {

// QZSS L1C/A, PRN 193..202 -> index [prn - 193].
constexpr int kQzssG2Delay[10] = {
    339, 208, 711, 189, 263, 537, 663, 942, 173, 900,
};

// SBAS L1, PRN 120..158 -> index [prn - 120].
constexpr int kSbasG2Delay[39] = {
    145, 175,  52,  21, 237, 235, 886, 657, 634, 762,   // PRN 120..129
    355, 1012, 176, 603, 130, 359, 595,  68, 386, 797,  // PRN 130..139
    456, 499, 883, 307, 127, 211, 121, 118, 163, 628,   // PRN 140..149
    853, 484, 289, 811, 202, 1021, 463, 568, 904,       // PRN 150..158
};

}  // namespace
