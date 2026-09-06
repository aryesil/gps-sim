#pragma once
#include "constants.hpp"
#include <cmath>
#include <cstdint>
namespace gs {
enum class NavMode { Zero = 0, KnownFrame = 1 };
struct NavSource {
    NavMode mode = NavMode::Zero;
    const int8_t *bits = nullptr;
    int nbits = 0;
    double sym_rate_hz = 0.0;   // 0 => kNavBitHz (GPS LNAV 50 Hz)
};
inline int8_t nav_symbol(const NavSource &s, double t_s) {
    if (s.mode == NavMode::Zero || s.nbits <= 0) return 1;
    const double rate = s.sym_rate_hz > 0.0 ? s.sym_rate_hz : kNavBitHz;
    long idx = static_cast<long>(std::floor(t_s * rate));
    idx %= s.nbits;
    if (idx < 0) idx += s.nbits;
    return s.bits[idx] >= 0 ? 1 : -1;
}
}  // namespace gs
