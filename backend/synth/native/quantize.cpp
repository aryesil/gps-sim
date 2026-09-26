// backend/synth/native/quantize.cpp
#include "quantize.hpp"
#include <algorithm>
#include <cmath>
#include <cstdint>

namespace gs {
// scale maps the expected composite RMS to a comfortable fraction of full
// scale; caller passes it in. clampv is the format's max magnitude.
// quant: 0 = int8 (int8_t* out), 1 = int12 (int16_t* out), 2 = int16 (int16_t* out).
// Round half away from zero, as std::lround, but inline and vectorisable:
// a float plus 0.5 is exact in double, so truncating the double sum gives
// the same integer lround would (a float sum would round 0.49999997 up).
static inline double round_away(float v) {
    const double d = static_cast<double>(v);
    return d + (d < 0.0 ? -0.5 : 0.5);
}

void quantize_block(const float *iq, int n2, int quant, float scale,
                    void *out) {
    const float clampv = (quant == 0) ? 127.0f : (quant == 1 ? 2047.0f : 32767.0f);
    if (quant == 0) {
        int8_t *o = static_cast<int8_t *>(out);
        for (int k = 0; k < n2; ++k)
            o[k] = static_cast<int8_t>(static_cast<int>(round_away(
                std::clamp(iq[k] * scale, -clampv, clampv))));
    } else {
        int16_t *o = static_cast<int16_t *>(out);
        for (int k = 0; k < n2; ++k)
            o[k] = static_cast<int16_t>(static_cast<int>(round_away(
                std::clamp(iq[k] * scale, -clampv, clampv))));
    }
}
}  // namespace gs
