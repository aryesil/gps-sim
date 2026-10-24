# SPDX-License-Identifier: MIT
"""Small GPS L1 C/A software receiver used to measure the SDR's own oscillator error.

Independent of the simulator backend: only numpy is required. GNSS-SDR was read as an
architecture reference only; no code is copied.
"""
L1_HZ = 1_575_420_000.0
CHIP_HZ = 1_023_000.0
CODE_LEN = 1023
C = 299_792_458.0
