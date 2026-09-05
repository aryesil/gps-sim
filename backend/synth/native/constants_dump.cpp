#include "abi.h"
#include "constants.hpp"
void synth_constants(double *out, int n) {
    if (n < 9) return;
    out[0] = gs::kL1Hz;      out[1] = gs::kCaChipHz;  out[2] = gs::kCaCodeLen;
    out[3] = gs::kNavBitHz;  out[4] = gs::kMu;        out[5] = gs::kOmegaEDot;
    out[6] = gs::kC;         out[7] = gs::kFRel;      out[8] = gs::kGpsUtcLeap;
}
