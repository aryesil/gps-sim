// backend/synth/native/scheduler.cpp
#include "scheduler.hpp"
#include "mixer.hpp"
#include <algorithm>
#include <thread>
#include <vector>

namespace gs {

void mix_block_parallel(const SvChannel *svs, int nsv, double fs,
                        uint64_t sample0, int n, float *iq, int nthreads) {
    if (nthreads <= 0)
        nthreads = static_cast<int>(std::thread::hardware_concurrency());
    if (nthreads < 1) nthreads = 1;

    // mix_block is always-accumulate; the caller must zero the buffer first.
    for (int i = 0; i < 2 * n; ++i) iq[i] = 0.0f;

    if (nthreads == 1 || n < 4096) {
        mix_block(svs, nsv, fs, sample0, n, iq);
        return;
    }

    const int chunk = (n + nthreads - 1) / nthreads;
    std::vector<std::thread> pool;
    pool.reserve(static_cast<size_t>(nthreads));
    for (int t = 0; t < nthreads; ++t) {
        const int lo = t * chunk;
        const int hi = std::min(n, lo + chunk);
        if (lo >= hi) break;
        pool.emplace_back([=] {
            mix_block(svs, nsv, fs, sample0 + static_cast<uint64_t>(lo),
                      hi - lo, iq + 2 * lo);
        });
    }
    for (auto &th : pool) th.join();
}

}  // namespace gs
