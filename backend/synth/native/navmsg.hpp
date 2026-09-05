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
};
inline int8_t nav_symbol(const NavSource &s, double t_s) {
    if (s.mode == NavMode::Zero || s.nbits <= 0) return 1;
    long idx = static_cast<long>(std::floor(t_s * kNavBitHz));
    idx %= s.nbits;
    if (idx < 0) idx += s.nbits;
    return s.bits[idx] >= 0 ? 1 : -1;
}
}  // namespace gs
