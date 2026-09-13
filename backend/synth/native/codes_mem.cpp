// backend/synth/native/codes_mem.cpp
#include "codes_mem.hpp"

extern const int8_t kE1B[50][4092];
extern const int8_t kE1C[50][4092];
extern const int8_t kCS25[25];
extern const int8_t kE5AI[50][10230];
extern const int8_t kE5AQ[50][10230];

const int8_t *mem_e1b(int prn) {
    return (prn >= 1 && prn <= 50) ? kE1B[prn - 1] : nullptr;
}
const int8_t *mem_e1c(int prn) {
    return (prn >= 1 && prn <= 50) ? kE1C[prn - 1] : nullptr;
}
const int8_t *cs25() { return kCS25; }

const int8_t *mem_e5ai(int prn) {
    return (prn >= 1 && prn <= 50) ? kE5AI[prn - 1] : nullptr;
}
const int8_t *mem_e5aq(int prn) {
    return (prn >= 1 && prn <= 50) ? kE5AQ[prn - 1] : nullptr;
}
