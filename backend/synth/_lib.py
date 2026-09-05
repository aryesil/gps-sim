from __future__ import annotations

import ctypes
import pathlib
import sys

ABI_VERSION = 4
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


def _bind_sat_state(lib: ctypes.CDLL) -> None:
    lib.synth_sat_state.restype = None
    lib.synth_sat_state.argtypes = [ctypes.POINTER(KeplerEph), ctypes.c_double,
                                    ctypes.POINTER(ctypes.c_double),
                                    ctypes.POINTER(ctypes.c_double),
                                    ctypes.POINTER(ctypes.c_double)]


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
