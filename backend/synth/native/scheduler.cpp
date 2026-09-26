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

WorkerPool::WorkerPool(int nthreads) {
    if (nthreads <= 0)
        nthreads = static_cast<int>(std::thread::hardware_concurrency());
    if (nthreads < 1) nthreads = 1;
    for (int i = 1; i < nthreads; ++i) workers_.emplace_back([this] { loop(); });
}

WorkerPool::~WorkerPool() {
    {
        std::lock_guard<std::mutex> lk(m_);
        stop_ = true;
    }
    cv_.notify_all();
    for (auto &w : workers_) w.join();
}

void WorkerPool::loop() {
    uint64_t seen = 0;
    std::unique_lock<std::mutex> lk(m_);
    for (;;) {
        cv_.wait(lk, [&] { return stop_ || gen_ != seen; });
        if (stop_) return;
        seen = gen_;
        while (next_ < ntasks_) {
            const int i = next_++;
            lk.unlock();
            (*fn_)(i);
            lk.lock();
            if (--pending_ == 0) done_cv_.notify_all();
        }
    }
}

void WorkerPool::run(int ntasks, const std::function<void(int)> &fn) {
    if (ntasks <= 0) return;
    std::unique_lock<std::mutex> lk(m_);
    fn_ = &fn;
    ntasks_ = ntasks;
    next_ = 0;
    pending_ = ntasks;
    ++gen_;
    cv_.notify_all();
    while (next_ < ntasks_) {           // the caller works too
        const int i = next_++;
        lk.unlock();
        fn(i);
        lk.lock();
        --pending_;
    }
    done_cv_.wait(lk, [&] { return pending_ == 0; });
    fn_ = nullptr;
    ntasks_ = 0;
}

}  // namespace gs
