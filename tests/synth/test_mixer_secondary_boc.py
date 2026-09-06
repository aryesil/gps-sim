"""Task 10: SvSpec gains sys / sub-carrier / secondary; the mixer applies a
BOC(1,1) sign and a secondary-code XOR in its inner loop.

The first two tests come from the task brief. The third is the mandatory
block/chunk-coherence test (final review class "B1"): with BOC *and* a real
secondary code enabled, a span synthesized in one shot must equal the same span
built from disjoint sub-ranges, and nthreads=1 must equal nthreads=4.
"""
import numpy as np

from backend import config
from backend.synth import _lib


def test_secondary_and_boc_off_matches_phase1():
    code = _lib.code(0, 5, 1023)[0].astype(np.int8)
    n = 20000
    a = _lib.debug_one_sv(code, config.CA_CHIP_HZ, 512.0, 0.0, 1000.0,
                          2_600_000.0, n)                      # Phase-1 shim
    b = _lib.debug_one_sv_ex(code, config.CA_CHIP_HZ, 512.0, 0.0, 1000.0,
                             2_600_000.0, n, sys=0, sub_hz=0.0,
                             sec=None, sec_len=0, sec_rate=0.0)
    assert np.max(np.abs(a - b)) < 1e-4


def test_secondary_flips_sign_at_secondary_rate():
    code = _lib.code(0, 5, 1023)[0].astype(np.int8)
    sec = np.array([1, -1], np.int8)
    n = 52000
    out = _lib.debug_one_sv_ex(code, config.CA_CHIP_HZ, 0.0, 0.0, 0.0,
                               2_600_000.0, n, sys=0, sub_hz=0.0,
                               sec=sec, sec_len=2, sec_rate=1000.0)
    # first 1 ms uses sec[0]=+1, second 1 ms uses sec[1]=-1 -> I-branch flips
    i0 = out[0:2*2600:2]
    i1 = out[2*2600:2*5200:2]
    assert np.sign(np.mean(i0 * i1)) <= 0


def test_boc_plus_secondary_block_chunk_coherence():
    code = _lib.code(6, 1, 4092, 25)[0].astype(np.int8)
    sec = _lib.code(6, 1, 4092, 25)[1].astype(np.int8)
    assert sec is not None and len(sec) == 25
    fs = 2_600_000.0
    n, m = 120_000, 47_000
    sub_hz = 1.023e6
    sec_rate = 250.0
    code_dopp = 3.0

    def rng(sample0, count):
        return _lib.debug_mix_range_ex(code, config.CA_CHIP_HZ, 128.0, code_dopp,
                                       777.0, fs, sample0, count, sys=1,
                                       sub_hz=sub_hz, sec=sec, sec_len=25,
                                       sec_rate=sec_rate)

    single = rng(0, n)
    split = np.concatenate([rng(0, m), rng(m, n - m)])
    assert np.max(np.abs(single - split)) < 1e-2

    p1 = _lib.debug_mix_parallel_ex(code, config.CA_CHIP_HZ, 128.0, code_dopp,
                                    777.0, fs, 0, n, 1, sys=1, sub_hz=sub_hz,
                                    sec=sec, sec_len=25, sec_rate=sec_rate)
    p4 = _lib.debug_mix_parallel_ex(code, config.CA_CHIP_HZ, 128.0, code_dopp,
                                    777.0, fs, 0, n, 4, sys=1, sub_hz=sub_hz,
                                    sec=sec, sec_len=25, sec_rate=sec_rate)
    assert np.max(np.abs(p1 - p4)) < 1e-2
