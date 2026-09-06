from __future__ import annotations

import ctypes
import pathlib
import sys

import numpy as np

ABI_VERSION = 14
_NATIVE_DIR = pathlib.Path(__file__).parent / "native"
_EXT = "dylib" if sys.platform == "darwin" else "so"
LIB_PATH = _NATIVE_DIR / f"libgnsssynth.{_EXT}"
_BUILD_HINT = f"make -C backend/synth/native   # produces {LIB_PATH.name}"

_CACHED: ctypes.CDLL | None = None

c_double = ctypes.c_double


class KeplerEph(ctypes.Structure):
    _fields_ = [(name, ctypes.c_double) for name in (
        "sqrtA e m0 delta_n omega omega0 omega_dot i0 idot cuc cus crc crs "
        "cic cis toe toc af0 af1 af2 _pad".split())]


class FadingCfg(ctypes.Structure):
    # Field order MUST match `FadingCfg` in native/fading.hpp exactly.
    _fields_ = [
        ("model", ctypes.c_int),         # 0 = off, 1 = lognormal
        ("sigma_db", ctypes.c_double),
        ("coherence_s", ctypes.c_double),
        ("seed", ctypes.c_uint64),
    ]


class SvSpec(ctypes.Structure):
    # Field order MUST match `SvSpec` in native/abi.h exactly.
    _fields_ = [
        ("code", ctypes.POINTER(ctypes.c_int8)),
        ("carrier_freq_hz", ctypes.c_double),
        ("carrier_phase0_rad", ctypes.c_double),
        ("code_phase0_chips", ctypes.c_double),
        ("code_doppler_hz", ctypes.c_double),
        ("nav_mode", ctypes.c_int),
        ("nav_bits", ctypes.POINTER(ctypes.c_int8)),
        ("nav_nbits", ctypes.c_int),
        ("gain", ctypes.c_float),
        ("prn", ctypes.c_int),
        ("fading", FadingCfg),
        # Task 10 -- appended after the frozen Phase-1 layout.
        ("sys", ctypes.c_int),
        ("sub_carrier_hz", ctypes.c_double),
        ("sec_code", ctypes.POINTER(ctypes.c_int8)),
        ("sec_len", ctypes.c_int),
        ("sec_rate_hz", ctypes.c_double),
    ]


class RunSpec(ctypes.Structure):
    # Field order MUST match `RunSpec` in native/abi.h exactly.
    _fields_ = [
        ("fs", ctypes.c_double),
        ("quant", ctypes.c_int),
        ("dither", ctypes.c_int),
        ("total_samples", ctypes.c_uint64),
        ("block_samples", ctypes.c_int),
        ("nthreads", ctypes.c_int),
    ]


_PROGRESS_CB = ctypes.CFUNCTYPE(None, ctypes.c_double, ctypes.c_void_p)


def _bind_run(lib: ctypes.CDLL) -> None:
    lib.synth_run.restype = ctypes.c_int
    lib.synth_run.argtypes = [
        ctypes.c_char_p, ctypes.POINTER(RunSpec), ctypes.POINTER(SvSpec),
        ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
    lib.synth_ca_code.restype = ctypes.c_int
    lib.synth_ca_code.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_int8),
                                  ctypes.c_int]
    lib.synth_code.restype = ctypes.c_int
    lib.synth_code.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int8), ctypes.c_int,
        ctypes.POINTER(ctypes.c_int8), ctypes.c_int]


def _bind_sat_state(lib: ctypes.CDLL) -> None:
    _dp = ctypes.POINTER(ctypes.c_double)
    lib.synth_sat_state.restype = None
    lib.synth_sat_state.argtypes = [ctypes.POINTER(KeplerEph), ctypes.c_double,
                                    _dp, _dp, _dp]
    lib.synth_sat_state_sys.restype = None
    lib.synth_sat_state_sys.argtypes = [ctypes.POINTER(KeplerEph), ctypes.c_int,
                                        ctypes.c_double, _dp, _dp, _dp]


def sat_state_sys(eph_struct: "KeplerEph", sys_int: int, t: float):
    """Propagate a KeplerEph via the native engine for a given PROPAGATION
    sys-int (0 GPS/QZSS, 1 Galileo, 2 BeiDou MEO/IGSO, 3 BeiDou GEO -- a
    SEPARATE enum from the code-generation `sys` int). Returns
    ``(pos3, vel3, clk)`` with pos/vel as 3-tuples of floats."""
    lib = load_lib()
    pos = (ctypes.c_double * 3)()
    vel = (ctypes.c_double * 3)()
    clk = ctypes.c_double()
    lib.synth_sat_state_sys(ctypes.byref(eph_struct), int(sys_int),
                            ctypes.c_double(t), pos, vel, ctypes.byref(clk))
    return tuple(pos), tuple(vel), clk.value


def bind_fading(lib: ctypes.CDLL) -> None:
    lib.fading_gain_linear.restype = ctypes.c_float
    lib.fading_gain_linear.argtypes = [
        ctypes.POINTER(FadingCfg), ctypes.c_int, ctypes.c_double]


class NativeEngineUnavailable(RuntimeError):
    pass


def load_lib() -> ctypes.CDLL:
    global _CACHED
    if _CACHED is not None:
        return _CACHED
    if not LIB_PATH.exists():
        raise NativeEngineUnavailable(
            f"native engine library not built: {LIB_PATH} missing. Build it with:\n    {_BUILD_HINT}")
    try:
        lib = ctypes.CDLL(str(LIB_PATH))
    except OSError as e:  # pragma: no cover - platform loader failure
        raise NativeEngineUnavailable(f"failed to load {LIB_PATH}: {e}\n    rebuild: {_BUILD_HINT}")
    lib.synth_abi_version.restype = ctypes.c_int
    lib.synth_abi_version.argtypes = []
    got = lib.synth_abi_version()
    if got != ABI_VERSION:
        raise NativeEngineUnavailable(
            f"ABI mismatch: library reports {got}, code expects {ABI_VERSION}. Rebuild:\n    {_BUILD_HINT}")
    _bind_sat_state(lib)
    _bind_run(lib)
    bind_fading(lib)
    _CACHED = lib
    return lib


def native_constants() -> dict[str, float]:
    lib = load_lib()
    lib.synth_constants.restype = None
    lib.synth_constants.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_int]
    buf = (ctypes.c_double * 9)()
    lib.synth_constants(buf, 9)
    keys = ["l1_hz", "ca_chip_hz", "ca_code_len", "nav_bit_hz", "mu",
            "omega_e_dot", "c", "f_rel", "gps_utc_leap"]
    return dict(zip(keys, list(buf)))


def ca_code(prn: int) -> list[int]:
    lib = load_lib()
    lib.synth_ca_code.restype = ctypes.c_int
    lib.synth_ca_code.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_int8), ctypes.c_int]
    buf = (ctypes.c_int8 * 1023)()
    if lib.synth_ca_code(prn, buf, 1023) != 0:
        raise ValueError(f"bad prn {prn}")
    return list(buf)


def code(sys: int, prn: int, prim_len: int, sec_len: int = 0):
    """L1-group code generator over the native ``synth_code`` symbol.

    ``sys`` is the CODE-GEN enum (0 GPS, 1 QZSS, 2 SBAS, 3 BeiDou B1I,
    4 GLONASS G1) -- SEPARATE from the propagation sys int. Returns
    ``(primary, secondary)`` as ``np.int8`` arrays of ``{-1, +1}`` chips;
    ``secondary`` is ``None`` when ``sec_len <= 0``.
    """
    lib = load_lib()
    _p = ctypes.POINTER(ctypes.c_int8)
    primary = np.zeros(int(prim_len), np.int8)
    secondary = np.zeros(int(sec_len), np.int8) if sec_len > 0 else None
    sec_ptr = secondary.ctypes.data_as(_p) if secondary is not None else None
    rc = lib.synth_code(int(sys), int(prn),
                        primary.ctypes.data_as(_p), int(prim_len),
                        sec_ptr, int(sec_len))
    if rc != 0:
        raise ValueError(f"synth_code failed: sys={sys} prn={prn}")
    return primary, (secondary if sec_len > 0 else None)


def debug_boc(sub_hz: float, fs: float, n: int) -> np.ndarray:
    """BOC(1,1) square sub-carrier debug shim. Fills an n-element buffer with
    the {+1,-1} sign sequence of a BOC(1,1) sub-carrier at sub_hz Hz for sample
    rate fs. Returns np.int8 array."""
    lib = load_lib()
    lib.synth_debug_boc.restype = None
    lib.synth_debug_boc.argtypes = [ctypes.c_double, ctypes.c_double,
                                    ctypes.c_int, ctypes.POINTER(ctypes.c_int8)]
    out = np.zeros(int(n), np.int8)
    lib.synth_debug_boc(ctypes.c_double(sub_hz), ctypes.c_double(fs),
                        ctypes.c_int(int(n)), out.ctypes.data_as(ctypes.POINTER(ctypes.c_int8)))
    return out


_I8 = ctypes.POINTER(ctypes.c_int8)
_F32 = ctypes.POINTER(ctypes.c_float)


def _as_i8(arr):
    if arr is None:
        return None
    a = np.ascontiguousarray(arr, dtype=np.int8)
    return a.ctypes.data_as(_I8), a  # keep `a` alive in caller


def debug_one_sv(code, code_rate, code_phase0, code_doppler, carrier_freq, fs, n):
    """Phase-1 single-SV mixer shim. Returns the interleaved I,Q,I,Q,... span
    (float32, length 2n)."""
    lib = load_lib()
    lib.synth_debug_one_sv.restype = None
    lib.synth_debug_one_sv.argtypes = [
        _I8, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double,
        ctypes.c_double, ctypes.c_int, _F32]
    cptr, _keep = _as_i8(code)
    out = (ctypes.c_float * (2 * int(n)))()
    lib.synth_debug_one_sv(cptr, code_rate, code_phase0, code_doppler,
                           carrier_freq, fs, int(n), out)
    return np.array(list(out), dtype=np.float32)


def debug_one_sv_ex(code, code_rate, code_phase0, code_doppler, carrier_freq, fs,
                    n, *, sys=0, sub_hz=0.0, sec=None, sec_len=0, sec_rate=0.0):
    """Task-10 single-SV mixer shim with sys / BOC sub-carrier / secondary code.
    Starts at absolute sample 0. Returns the interleaved span (float32, 2n)."""
    lib = load_lib()
    lib.synth_debug_one_sv_ex.restype = None
    lib.synth_debug_one_sv_ex.argtypes = [
        _I8, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double,
        ctypes.c_double, ctypes.c_int, ctypes.c_int, ctypes.c_double,
        _I8, ctypes.c_int, ctypes.c_double, _F32]
    cptr, _kc = _as_i8(code)
    sptr, _ks = _as_i8(sec) if sec is not None else (None, None)
    out = (ctypes.c_float * (2 * int(n)))()
    lib.synth_debug_one_sv_ex(cptr, code_rate, code_phase0, code_doppler,
                              carrier_freq, fs, int(n), int(sys), sub_hz,
                              sptr, int(sec_len), sec_rate, out)
    return np.array(list(out), dtype=np.float32)


def debug_mix_range_ex(code, code_rate, code_phase0, code_doppler, carrier_freq,
                       fs, sample0, n, *, sys=0, sub_hz=0.0, sec=None, sec_len=0,
                       sec_rate=0.0):
    """Task-10 mixer shim over gs::mix_block from an arbitrary absolute sample
    index, with the five new fields. Returns complex64, length n."""
    lib = load_lib()
    lib.synth_debug_mix_range_ex.restype = None
    lib.synth_debug_mix_range_ex.argtypes = [
        _I8, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double,
        ctypes.c_double, ctypes.c_uint64, ctypes.c_int, ctypes.c_int,
        ctypes.c_double, _I8, ctypes.c_int, ctypes.c_double, _F32]
    cptr, _kc = _as_i8(code)
    sptr, _ks = _as_i8(sec) if sec is not None else (None, None)
    out = (ctypes.c_float * (2 * int(n)))()
    lib.synth_debug_mix_range_ex(cptr, code_rate, code_phase0, code_doppler,
                                 carrier_freq, fs, int(sample0), int(n),
                                 int(sys), sub_hz, sptr, int(sec_len), sec_rate,
                                 out)
    a = np.array(list(out), dtype=np.float32)
    return a[0::2] + 1j * a[1::2]


def debug_mix_parallel_ex(code, code_rate, code_phase0, code_doppler,
                          carrier_freq, fs, sample0, n, nthreads, *, sys=0,
                          sub_hz=0.0, sec=None, sec_len=0, sec_rate=0.0):
    """Task-10 mixer shim over gs::mix_block_parallel with an explicit thread
    count and the five new fields. Returns complex64, length n."""
    lib = load_lib()
    lib.synth_debug_mix_parallel_ex.restype = None
    lib.synth_debug_mix_parallel_ex.argtypes = [
        _I8, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double,
        ctypes.c_double, ctypes.c_uint64, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_double, _I8, ctypes.c_int, ctypes.c_double, _F32]
    cptr, _kc = _as_i8(code)
    sptr, _ks = _as_i8(sec) if sec is not None else (None, None)
    out = (ctypes.c_float * (2 * int(n)))()
    lib.synth_debug_mix_parallel_ex(cptr, code_rate, code_phase0, code_doppler,
                                    carrier_freq, fs, int(sample0), int(n),
                                    int(nthreads), int(sys), sub_hz, sptr,
                                    int(sec_len), sec_rate, out)
    a = np.array(list(out), dtype=np.float32)
    return a[0::2] + 1j * a[1::2]
