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
#include <vector>

int synth_run(const char *path, const RunSpec *rs, const SvSpec *specs, int nsv,
              void (*progress)(double, void *), void *user) {
    if (!path || !rs || (nsv > 0 && !specs)) return -1;
    std::FILE *f = std::fopen(path, "wb");
    if (!f) return -1;

    std::vector<gs::SvChannel> ch(static_cast<size_t>(nsv));
    for (int i = 0; i < nsv; ++i) {
        ch[i].code = specs[i].code;
        ch[i].code_len = 1023;
        ch[i].code_rate_hz = 1.023e6;
        ch[i].code_phase0_chips = specs[i].code_phase0_chips;
        ch[i].code_doppler_hz = specs[i].code_doppler_hz;
        ch[i].carrier_freq_hz = specs[i].carrier_freq_hz;
        ch[i].carrier_phase0_rad = specs[i].carrier_phase0_rad;
        ch[i].nav.mode = static_cast<gs::NavMode>(specs[i].nav_mode);
        ch[i].nav.bits = specs[i].nav_bits;
        ch[i].nav.nbits = specs[i].nav_nbits;
        ch[i].gain = specs[i].gain;
        ch[i].sys = specs[i].sys;
        ch[i].sub_carrier_hz = specs[i].sub_carrier_hz;
        ch[i].sec_code = specs[i].sec_code;
        ch[i].sec_len = specs[i].sec_len;
        ch[i].sec_rate_hz = specs[i].sec_rate_hz;
    }
    // Static per-SV gain, before any per-block fading is folded in.
    std::vector<float> base_gain(static_cast<size_t>(nsv));
    for (int i = 0; i < nsv; ++i) base_gain[i] = specs[i].gain;

    const int blk = rs->block_samples > 0 ? rs->block_samples : 65536;
    std::vector<float> fbuf(static_cast<size_t>(2 * blk));
    std::vector<int16_t> qbuf16(static_cast<size_t>(2 * blk));
    std::vector<int8_t> qbuf8(static_cast<size_t>(2 * blk));

    // Composite-level scale: aim for ~1/4 of full scale, divided by sqrt(nsv)
    // so a full constellation does not clip.
    const float fs_full =
        (rs->quant == 0) ? 127.0f : (rs->quant == 1 ? 2047.0f : 32767.0f);
    const float scale = (nsv > 0)
        ? (0.25f * fs_full / static_cast<float>(std::sqrt((double)nsv)))
        : fs_full;

    uint64_t done = 0;
    int rc = 0;
    while (done < rs->total_samples) {
        const int n = static_cast<int>(
            std::min<uint64_t>(static_cast<uint64_t>(blk),
                               rs->total_samples - done));
        // Recompute deterministic per-SV fading at the block midpoint every
        // block (do not hoist -- it is time-varying). Off model => factor 1.0f.
        const double block_mid_t_seconds =
            (static_cast<double>(done) + n / 2.0) / rs->fs;
        for (int i = 0; i < nsv; ++i) {
            const float fade = gs::fading_gain_linear(
                &specs[i].fading, specs[i].prn, block_mid_t_seconds);
            ch[i].gain = base_gain[i] * fade;
        }
        gs::mix_block_parallel(ch.data(), nsv, rs->fs, done, n, fbuf.data(),
                               rs->nthreads);
        void *q = (rs->quant == 0) ? static_cast<void *>(qbuf8.data())
                                   : static_cast<void *>(qbuf16.data());
        gs::quantize_block(fbuf.data(), 2 * n, rs->quant, scale, q);
        const size_t esz = (rs->quant == 0) ? 1u : 2u;
        if (std::fwrite(q, esz, static_cast<size_t>(2 * n), f) !=
            static_cast<size_t>(2 * n)) {
            rc = -2;
            break;
        }
        done += n;
        if (progress)
            progress(static_cast<double>(done) /
                         static_cast<double>(rs->total_samples),
                     user);
    }
    std::fclose(f);
    return rc;
}
