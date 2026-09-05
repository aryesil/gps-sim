from __future__ import annotations

import ctypes
import pathlib
import sys

ABI_VERSION = 3
_NATIVE_DIR = pathlib.Path(__file__).parent / "native"
_EXT = "dylib" if sys.platform == "darwin" else "so"
LIB_PATH = _NATIVE_DIR / f"libgnsssynth.{_EXT}"
_BUILD_HINT = f"make -C backend/synth/native   # produces {LIB_PATH.name}"

_CACHED: ctypes.CDLL | None = None


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
