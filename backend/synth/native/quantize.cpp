// backend/synth/native/quantize.cpp
#include "quantize.hpp"
#include <algorithm>
#include <cmath>
#include <cstdint>

namespace gs {
// scale maps the expected composite RMS to a comfortable fraction of full
// scale; caller passes it in. clampv is the format's max magnitude.
// quant: 0 = int8 (int8_t* out), 1 = int12 (int16_t* out), 2 = int16 (int16_t* out).
void quantize_block(const float *iq, int n2, int quant, float scale,
                    void *out) {
    const float clampv = (quant == 0) ? 127.0f : (quant == 1 ? 2047.0f : 32767.0f);
    if (quant == 0) {
        int8_t *o = static_cast<int8_t *>(out);
        for (int k = 0; k < n2; ++k)
            o[k] = static_cast<int8_t>(std::lround(
                std::clamp(iq[k] * scale, -clampv, clampv)));
    } else {
        int16_t *o = static_cast<int16_t *>(out);
        for (int k = 0; k < n2; ++k)
            o[k] = static_cast<int16_t>(std::lround(
                std::clamp(iq[k] * scale, -clampv, clampv)));
    }
}
}  // namespace gs
