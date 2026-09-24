// backend/synth/native/l5_codes.cpp
#include "l5_codes.hpp"

namespace {

constexpr int kLen = 10230;
constexpr uint32_t k13 = 0x1FFFu;         // 13-bit register mask
constexpr int kXaResetChip = 8190;        // XA short-cycle reset point

// XA feedback taps: stages {9, 10, 12, 13}. Bit i-1 <=> stage i.
constexpr uint32_t kXaMask =
    (1u << (9 - 1)) | (1u << (10 - 1)) | (1u << (12 - 1)) | (1u << (13 - 1));

// XB feedback taps: stages {1, 3, 4, 6, 7, 8, 12, 13}.
constexpr uint32_t kXbMask =
    (1u << (1 - 1)) | (1u << (3 - 1)) | (1u << (4 - 1)) | (1u << (6 - 1)) |
    (1u << (7 - 1)) | (1u << (8 - 1)) | (1u << (12 - 1)) | (1u << (13 - 1));

inline int parity(uint32_t v) { return __builtin_parity(v); }

// Per-PRN XB "code advance" of IS-GPS-705 Table 3-Ia/3-Ib (the ICD's XB
// initial state is the all-ones XB register advanced by this many chips).
// Transcribed from PocketSDR sdr_code.c; identical to GNSS-SDR
// GPS_L5I_INIT_REG / GPS_L5Q_INIT_REG.
// IS-GPS-705 Table 3-Ia: XB code advance (chips) for I5, PRN 1..210.
constexpr uint16_t kI5XbAdvance[210] = {
     266,  365,  804, 1138, 1509, 1559, 1756, 2084, 2170, 2303, 2527, 2687,
    2930, 3471, 3940, 4132, 4332, 4924, 5343, 5443, 5641, 5816, 5898, 5918,
    5955, 6243, 6345, 6477, 6518, 6875, 7168, 7187, 7329, 7577, 7720, 7777,
    8057, 5358, 3550, 3412,  819, 4608, 3698,  962, 3001, 4441, 4937, 3717,
    4730, 7291, 2279, 7613, 5723, 7030, 1475, 2593, 2904, 2056, 2757, 3756,
    6205, 5053, 6437, 7789, 2311, 7432, 5155, 1593, 5841, 5014, 1545, 3016,
    4875, 2119,  229, 7634, 1406, 4506, 1819, 7580, 5446, 6053, 7958, 5267,
    2956, 3544, 1277, 2996, 1758, 3360, 2718, 3754, 7440, 2781, 6756, 7314,
     208, 5252,  696,  527, 1399, 5879, 6868,  217, 7681, 3788, 1337, 2424,
    4243, 5686, 1955, 4791,  492, 1518, 6566, 5349,  506,  113, 1953, 2797,
     934, 3023, 3632, 1330, 4909, 4867, 1183, 3990, 6217, 1224, 1733, 2319,
    3928, 2380,  841, 5049, 7027, 1197, 7208, 8000,  152, 6762, 3745, 4723,
    5502, 4796,  123, 8142, 5091, 7875,  330, 5272, 4912,  374, 2045, 6616,
    6321, 7605, 2570, 2419, 1234, 1922, 4317, 5110,  825,  958, 1089, 7813,
    6058, 7703, 6702, 1714, 6371, 2281, 1986, 6282, 3201, 3760, 1056, 6233,
    1150, 2823, 6250,  645, 2401, 1639, 2946, 7091,  923, 7045, 6493, 1706,
    5836,  926, 6086,  950, 5905, 3240, 6675, 3197, 1555, 3589, 4555, 5671,
    6948, 4664, 2086, 5950, 5521, 1515,
};
// IS-GPS-705 Table 3-Ia: XB code advance (chips) for Q5, PRN 1..210.
constexpr uint16_t kQ5XbAdvance[210] = {
    1701,  323, 5292, 2020, 5429, 7136, 1041, 5947, 4315,  148,  535, 1939,
    5206, 5910, 3595, 5135, 6082, 6990, 3546, 1523, 4548, 4484, 1893, 3961,
    7106, 5299, 4660,  276, 4389, 3783, 1591, 1601,  749, 1387, 1661, 3210,
     708, 4226, 5604, 6375, 3056, 1772, 3662, 4401, 5218, 2838, 6913, 1685,
    1194, 6963, 5001, 6694,  991, 7489, 2441,  639, 2097, 2498, 6470, 2399,
     242, 3768, 1186, 5246, 4259, 5907, 3870, 3262, 7387, 3069, 2999, 7993,
    7849, 4157, 5031, 5986, 4833, 5739, 7846,  898, 2022, 7446, 6404,  155,
    7862, 7795, 6121, 4840, 6585,  429, 6020,  200, 1664, 1499, 7298, 1305,
    7323, 7544, 4438, 2485, 3387, 7319, 1853, 5781, 1874, 7555, 2132, 6441,
    6722, 1192, 2588, 2188,  297, 1540, 4138, 5231, 4789,  659,  871, 6837,
    1393, 7383,  611, 4920, 5416, 1611, 2474,  118, 1382, 1092, 7950, 7223,
    1769, 4721, 1252, 5147, 2165, 7897, 4054, 3498, 6571, 2858, 8126, 7017,
    1901,  181, 1114, 5195, 7479, 4186, 3904, 7128, 1396, 4513, 5967, 2580,
    2575, 7961, 2598, 4508, 2090, 3685, 7748,  684,  913, 5558, 2894, 5858,
    6432, 3813, 3573, 7523, 5280, 3376, 7424, 2918, 5793, 1747, 7079, 2921,
    2490, 4119, 3373,  977,  681, 4273, 5419, 5626, 1266, 5804, 2414, 6444,
    4757,  427, 5452, 5182, 6606, 6531, 4268, 3115, 6835,  862, 4856, 2765,
      37, 1943, 7977, 2512, 4451, 4071,
};

// One 1 ms code: XA (all-ones seed, short-cycled at 8190) XOR XB advanced by
// `adv` chips. Both GNSS-SDR and PocketSDR index the advanced XB as
// XB[(n + adv) mod 10230] over a 10230-chip XB run from all-ones; the same
// convention is kept here so the chips match those receivers exactly.
void gen_one(int adv, int8_t *out) {
    int8_t xb_seq[kLen];
    uint32_t xb = k13;
    for (int i = 0; i < kLen; ++i) {
        xb_seq[i] = static_cast<int8_t>((xb >> (13 - 1)) & 1u);
        const int f = parity(xb & kXbMask);
        xb = ((xb << 1) | static_cast<uint32_t>(f)) & k13;
    }
    uint32_t xa = k13;
    for (int i = 0; i < kLen; ++i) {
        const int xa_chip = static_cast<int>((xa >> (13 - 1)) & 1u);
        const int xb_chip = xb_seq[(i + adv) % kLen];
        out[i] = (xa_chip ^ xb_chip) ? -1 : 1;
        const int xaf = parity(xa & kXaMask);
        xa = ((xa << 1) | static_cast<uint32_t>(xaf)) & k13;
        if (i + 1 == kXaResetChip) xa = k13;                     // short cycle
    }
}

}  // namespace

namespace gs {

void l5_i(int prn, int8_t *out, int n) {
    if (prn < 1 || prn > 210 || n < kLen || out == nullptr) return;
    gen_one(kI5XbAdvance[prn - 1], out);
    for (int i = kLen; i < n; ++i) out[i] = out[i - kLen];       // repeat
}

void l5_q(int prn, int8_t *out, int n) {
    if (prn < 1 || prn > 210 || n < kLen || out == nullptr) return;
    gen_one(kQ5XbAdvance[prn - 1], out);
    for (int i = kLen; i < n; ++i) out[i] = out[i - kLen];
}

}  // namespace gs
