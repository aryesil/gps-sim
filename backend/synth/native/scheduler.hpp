// backend/synth/native/scheduler.hpp
#pragma once
#include "mixer.hpp"
#include <condition_variable>
#include <cstdint>
#include <functional>
#include <mutex>
#include <thread>
#include <vector>
namespace gs {
// Pre-zeroes the whole 2*n float buffer, then splits [0,n) into `nthreads`
// disjoint sample chunks, each running mix_block on its own thread.
// nthreads <= 0 -> hardware_concurrency(); n < 4096 or nthreads == 1 ->
// single mix_block after the zero.
void mix_block_parallel(const SvChannel *svs, int nsv, double fs,
                        uint64_t sample0, int n, float *iq, int nthreads);

// Fixed set of worker threads reused for every block: spawning and joining
// nthreads std::threads per 65536-sample block cost ~13 % of a run.
class WorkerPool {
public:
    explicit WorkerPool(int nthreads);
    ~WorkerPool();
    WorkerPool(const WorkerPool &) = delete;
    WorkerPool &operator=(const WorkerPool &) = delete;
    int size() const { return static_cast<int>(workers_.size()) + 1; }
    // Runs fn(i) for i in [0, ntasks) across the workers and the calling
    // thread; returns when all are done.
    void run(int ntasks, const std::function<void(int)> &fn);

private:
    void loop();
    std::vector<std::thread> workers_;
    std::mutex m_;
    std::condition_variable cv_, done_cv_;
    const std::function<void(int)> *fn_ = nullptr;
    int ntasks_ = 0, next_ = 0, pending_ = 0;
    uint64_t gen_ = 0;
    bool stop_ = false;
};

}  // namespace gs
