// backend/synth/native/galileo_e5a.cpp
#include "galileo_e5a_codes.hpp"
#include "codes_mem.hpp"

namespace {
constexpr int kLen = 10230;

void copy_fill(const int8_t *mem, int8_t *out, int n) {
    for (int i = 0; i < kLen; ++i) out[i] = mem[i];
    for (int i = kLen; i < n; ++i) out[i] = out[i - kLen];   // repeat, as l5_codes.cpp
}
}  // namespace

namespace gs {

void e5a_i(int prn, int8_t *out, int n) {
    if (out == nullptr || n < kLen) return;
    const int8_t *mem = mem_e5ai(prn);
    if (mem == nullptr) return;
    copy_fill(mem, out, n);
}

void e5a_q(int prn, int8_t *out, int n) {
    if (out == nullptr || n < kLen) return;
    const int8_t *mem = mem_e5aq(prn);
    if (mem == nullptr) return;
    copy_fill(mem, out, n);
}

}  // namespace gs
