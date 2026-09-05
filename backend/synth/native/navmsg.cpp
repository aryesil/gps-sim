#include "abi.h"
#include "navmsg.hpp"
void synth_debug_nav(int mode, const int8_t *bits, int nbits,
                     double fs, int n, int8_t *out) {
    gs::NavSource s{static_cast<gs::NavMode>(mode), bits, nbits};
    for (int k = 0; k < n; ++k) out[k] = gs::nav_symbol(s, k / fs);
}
