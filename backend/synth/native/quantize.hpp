// backend/synth/native/quantize.hpp
#pragma once
namespace gs {
void quantize_block(const float *iq, int n2, int quant, float scale, void *out);
}
