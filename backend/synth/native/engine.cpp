// backend/synth/native/engine.cpp
#include "abi.h"
#include "fading.hpp"
#include "mixer.hpp"
#include "quantize.hpp"
#include "scheduler.hpp"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <thread>
#include <vector>

// Streams one interleaved-IQ file for a single band. This is the former
// synth_run body, with fs/quant/dither/total_samples/block_samples/nthreads/
// svs/nsv/out_path sourced from the BandSpec instead of RunSpec + args.
static int run_one_band(const BandSpec &b,
                        void (*progress)(double, void *), void *user) {
    const SvSpec *specs = b.svs;
    const int nsv = b.nsv;
    if (!b.out_path || (nsv > 0 && !specs)) return -1;
    std::FILE *f = std::fopen(b.out_path, "wb");
    if (!f) return -1;

    std::vector<gs::SvChannel> ch(static_cast<size_t>(nsv));
    for (int i = 0; i < nsv; ++i) {
        ch[i].code = specs[i].code;
        ch[i].code_len = specs[i].code_len > 0 ? specs[i].code_len : 1023;
        ch[i].code_rate_hz =
            specs[i].chip_rate_hz > 0.0 ? specs[i].chip_rate_hz : 1.023e6;
        ch[i].code_phase0_chips = specs[i].code_phase0_chips;
        ch[i].code_doppler_hz = specs[i].code_doppler_hz;
        ch[i].carrier_freq_hz = specs[i].carrier_freq_hz;
        ch[i].carrier_phase0_rad = specs[i].carrier_phase0_rad;
        ch[i].nav.mode = static_cast<gs::NavMode>(specs[i].nav_mode);
        ch[i].nav.bits = specs[i].nav_bits;
        ch[i].nav.nbits = specs[i].nav_nbits;
        ch[i].nav.sym_rate_hz = specs[i].nav_sym_rate_hz;
        ch[i].gain = specs[i].gain;
        ch[i].sys = specs[i].sys;
        ch[i].sub_carrier_hz = specs[i].sub_carrier_hz;
        ch[i].sec_code = specs[i].sec_code;
        ch[i].sec_len = specs[i].sec_len;
        ch[i].sec_rate_hz = specs[i].sec_rate_hz;
        ch[i].traj_nknots = specs[i].traj_nknots;
        ch[i].traj_knot_samples = specs[i].traj_knot_samples;
        ch[i].traj_carr_freq = specs[i].traj_carr_freq;
        ch[i].traj_carr_phase = specs[i].traj_carr_phase;
        ch[i].traj_code_rate = specs[i].traj_code_rate;
        ch[i].traj_code_phase = specs[i].traj_code_phase;
        ch[i].tx_time_valid = specs[i].tx_time_valid;
        ch[i].tx_chips_offset = specs[i].tx_chips_offset;
        ch[i].cboc = specs[i].cboc;
    }
    // Per-SV channel process (fading model 0 = off => no gain knots).
    std::vector<gs::ChannelProcess> chan(static_cast<size_t>(nsv));
    std::vector<std::vector<float>> knots(static_cast<size_t>(nsv));
    for (int i = 0; i < nsv; ++i)
        if (specs[i].fading.model != 0)
            chan[i] = gs::ChannelProcess(specs[i].fading, specs[i].prn);

    const int blk = b.block_samples > 0 ? b.block_samples : 65536;
    std::vector<float> fbuf(static_cast<size_t>(2 * blk));
    const size_t esz = (b.quant == 0) ? 1u : 2u;
    // Two output buffers: the writer thread drains one while the workers
    // fill the other, so disk writes overlap the mixing.
    std::vector<uint8_t> qbuf[2] = {
        std::vector<uint8_t>(static_cast<size_t>(2 * blk) * esz),
        std::vector<uint8_t>(static_cast<size_t>(2 * blk) * esz)};

    // Composite-level scale: aim for ~1/4 of full scale, divided by sqrt(nsv)
    // so a full constellation does not clip.
    const float fs_full =
        (b.quant == 0) ? 127.0f : (b.quant == 1 ? 2047.0f : 32767.0f);
    const float scale = (nsv > 0)
        ? (0.25f * fs_full / static_cast<float>(std::sqrt((double)nsv)))
        : fs_full;

    gs::WorkerPool pool(b.nthreads);
    const int nthreads = pool.size();
    std::thread writer;
    bool write_ok = true;
    size_t write_n = 0;

    uint64_t done = 0;
    int rc = 0;
    int cur = 0;
    while (done < b.total_samples) {
        const int n = static_cast<int>(
            std::min<uint64_t>(static_cast<uint64_t>(blk),
                               b.total_samples - done));
        // Complex channel gain on the model's knot grid, covering every
        // sample of this block; the mixer interpolates between knots. Knot
        // values depend only on (config, prn, knot time), so the IQ does not
        // depend on fs, block size or thread count.
        const double t_first = static_cast<double>(done) / b.fs;
        const double t_last = static_cast<double>(done + n - 1) / b.fs;
        for (int i = 0; i < nsv; ++i) {
            if (!chan[i].enabled()) continue;
            const double dt = chan[i].params().dt_knot_s;
            const int64_t j0 = static_cast<int64_t>(std::floor(t_first / dt));
            const int64_t j1 = static_cast<int64_t>(std::floor(t_last / dt)) + 1;
            const int nk = static_cast<int>(j1 - j0 + 1);
            knots[i].resize(static_cast<size_t>(2 * nk));
            for (int j = 0; j < nk; ++j) {
                const std::complex<double> g =
                    chan[i].gain(static_cast<double>(j0 + j) * dt);
                knots[i][2 * j] = static_cast<float>(g.real());
                knots[i][2 * j + 1] = static_cast<float>(g.imag());
            }
            ch[i].gain_nknots = nk;
            ch[i].gain_knot_j0 = j0;
            ch[i].gain_knot_dt = dt;
            ch[i].gain_knots = knots[i].data();
        }
        // Mix and quantise in parallel chunks. mix_block accumulates, so
        // each chunk zeroes its slice first; the output does not depend on
        // the chunking (the mixer is absolute-sample seeded).
        const int nchunk = (n < 4096) ? 1 : nthreads;
        const int chunk = (n + nchunk - 1) / nchunk;
        uint8_t *q = qbuf[cur].data();
        pool.run(nchunk, [&](int t) {
            const int lo = t * chunk;
            const int hi = std::min(n, lo + chunk);
            if (lo >= hi) return;
            float *iq = fbuf.data() + 2 * lo;
            std::fill(iq, iq + 2 * (hi - lo), 0.0f);
            gs::mix_block(ch.data(), nsv, b.fs, done + static_cast<uint64_t>(lo),
                          hi - lo, iq);
            gs::quantize_block(iq, 2 * (hi - lo), b.quant, scale,
                               q + static_cast<size_t>(2 * lo) * esz);
        });
        if (writer.joinable()) {
            writer.join();
            if (!write_ok) {
                rc = -2;
                break;
            }
        }
        write_n = static_cast<size_t>(2 * n);
        writer = std::thread([&, q, write_n] {
            write_ok = std::fwrite(q, esz, write_n, f) == write_n;
        });
        cur ^= 1;
        done += n;
        if (progress)
            progress(static_cast<double>(done) /
                         static_cast<double>(b.total_samples),
                     user);
    }
    if (writer.joinable()) {
        writer.join();
        if (!write_ok && rc == 0) rc = -2;
    }
    std::fclose(f);
    return rc;
}

int synth_run_bands(const BandSpec *bands, int nband,
                    void (*progress)(double, void *), void *user) {
    if (!bands || nband < 0) return -1;
    for (int i = 0; i < nband; ++i) {
        const int rc = run_one_band(bands[i], progress, user);
        if (rc != 0) return rc;
    }
    return 0;
}

int synth_run(const char *path, const RunSpec *rs, const SvSpec *specs, int nsv,
              void (*progress)(double, void *), void *user) {
    if (!path || !rs || (nsv > 0 && !specs)) return -1;
    BandSpec b;
    b.out_path = path;
    b.fs = rs->fs;
    b.quant = rs->quant;
    b.dither = rs->dither;
    b.total_samples = rs->total_samples;
    b.block_samples = rs->block_samples;
    b.nthreads = rs->nthreads;
    b.svs = specs;
    b.nsv = nsv;
    return synth_run_bands(&b, 1, progress, user);
}
