// backend/synth/native/scheduler.hpp
#pragma once
#include "mixer.hpp"
#include <cstdint>
namespace gs {
// Pre-zeroes the whole 2*n float buffer, then splits [0,n) into `nthreads`
// disjoint sample chunks, each running mix_block on its own thread.
// nthreads <= 0 -> hardware_concurrency(); n < 4096 or nthreads == 1 ->
// single mix_block after the zero.
void mix_block_parallel(const SvChannel *svs, int nsv, double fs,
                        uint64_t sample0, int n, float *iq, int nthreads);
}  // namespace gs
