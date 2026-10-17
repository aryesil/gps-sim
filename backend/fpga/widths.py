"""Frozen fixed-point widths of the FPGA GNSS synthesis engine.

Single source of truth for the host model and the RTL parameters.
"""
CARR_PHASE_BITS = 32
LUT_BITS = 12
LUT_ROUND = 1 << 19
CODE_FRAC_BITS = 40
CODE_PHASE_BITS = 56
COEF_FRAC_BITS = 6
COEF_BITS = 18
TABLE_BITS = 16
OUT_BITS = 16
MAX_CH = 64
BLOCK = 65536
SIGN_BITS = 32

# Wire-format field widths (cmdfmt). The PL datapath keeps CODE_PHASE_BITS
# (56) for phase/increment registers; the command carries narrower fields that
# the PL zero-extends: phases are < code_len << 40 <= 4092 << 40 < 2**52, and
# the increment is < 8 chips/sample (3 integer + 40 fractional bits).
CODE_INC_WIRE_BITS = 43
CODE_PHASE_WIRE_BITS = 52
