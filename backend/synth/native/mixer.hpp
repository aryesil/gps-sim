// backend/synth/native/mixer.hpp
#pragma once
#include "navmsg.hpp"
#include <cstdint>
namespace gs {
struct SvChannel {
    const int8_t *code = nullptr;
    int code_len = 1023;
    double code_rate_hz = 1.023e6;
    double code_phase0_chips = 0.0;
    double code_doppler_hz = 0.0;
    double carrier_freq_hz = 0.0;
    double carrier_phase0_rad = 0.0;
    NavSource nav{};
    float gain = 1.0f;
    // Task 10 -- appended. Defaults keep mix_block byte-identical to Phase 1.
    int sys = 0;
    double sub_carrier_hz = 0.0;      // > 0 => BOC(1,1) square sub-carrier sign
    const int8_t *sec_code = nullptr; // secondary code chips {-1,+1}
    int sec_len = 0;                  // > 0 => secondary-code XOR
    double sec_rate_hz = 0.0;         // secondary chip rate (Hz)
};
void mix_block(const SvChannel *__restrict svs, int nsv, double fs,
               uint64_t sample0, int n, float *__restrict iq);
}  // namespace gs
