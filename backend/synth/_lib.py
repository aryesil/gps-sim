from __future__ import annotations

import ctypes
import pathlib
import sys

ABI_VERSION = 11
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
