from __future__ import annotations

import ctypes
import pathlib
import sys

import numpy as np

ABI_VERSION = 17
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


class GloEph(ctypes.Structure):
    # Field order MUST match `GloEph` in native/abi.h exactly (frozen).
    _fields_ = [(name, ctypes.c_double) for name in (
        "x_m y_m z_m vx vy vz ax ay az tau gamma toe_ref".split())]


def glo_struct(record: dict) -> "GloEph":
    """Build a `GloEph` from a raw-parsed GLONASS/SBAS broadcast dict (mirrors
    `engine.kepler_struct`). Optional luni-solar accel fields default to 0.0."""
    s = GloEph()
    for k in ("x_m", "y_m", "z_m", "vx", "vy", "vz", "tau", "gamma", "toe_ref"):
        setattr(s, k, float(record[k]))
    for k in ("ax", "ay", "az"):
        setattr(s, k, float(record.get(k, 0.0)))
    return s


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
        # Task 16b -- per-SV primary code geometry for the full-run path.
        ("code_len", ctypes.c_int),
        ("chip_rate_hz", ctypes.c_double),
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


class BandSpec(ctypes.Structure):
    # Field order MUST match `BandSpec` in native/abi.h exactly (frozen).
    _fields_ = [
        ("out_path", ctypes.c_char_p),
        ("fs", ctypes.c_double),
        ("quant", ctypes.c_int),
        ("dither", ctypes.c_int),
        ("total_samples", ctypes.c_uint64),
        ("block_samples", ctypes.c_int),
        ("nthreads", ctypes.c_int),
        ("svs", ctypes.POINTER(SvSpec)),
        ("nsv", ctypes.c_int),
    ]


_PROGRESS_CB = ctypes.CFUNCTYPE(None, ctypes.c_double, ctypes.c_void_p)


def one_sv_spec(code, carrier_hz=0.0, code_phase0=0.0, code_doppler=0.0,
                gain=1.0, prn=1):
    """Return a filled `SvSpec` for a single channel: nav_mode 0 (zero), fading
    off, and the five Task-10 fields zero/None. `code` is a 1023-entry int8
    array of {-1,+1} chips; the caller must keep it alive for the run (this
    helper stashes a contiguous int8 copy on the returned struct as
    ``._code_keep`` so the pointer stays valid)."""
    s = SvSpec()
    kept = np.ascontiguousarray(code, dtype=np.int8)
    s.code = kept.ctypes.data_as(ctypes.POINTER(ctypes.c_int8))
    s._code_keep = kept  # keep the buffer alive as long as the struct lives
    s.carrier_freq_hz = float(carrier_hz)
    s.carrier_phase0_rad = 0.0
    s.code_phase0_chips = float(code_phase0)
    s.code_doppler_hz = float(code_doppler)
    s.nav_mode = 0
    s.nav_bits = None
    s.nav_nbits = 0
    s.gain = float(gain)
    s.prn = int(prn)
    s.fading = FadingCfg(0, 0.0, 0.0, 0)
    s.sys = 0
    s.sub_carrier_hz = 0.0
    s.sec_code = None
    s.sec_len = 0
    s.sec_rate_hz = 0.0
    s.code_len = 1023
    s.chip_rate_hz = 1.023e6
    return s


def fill_band(bandspec, path, fs, quant, total_samples, sv_list,
              block_samples=65536, nthreads=0):
    """Populate `bandspec` (a `_lib.BandSpec`) in place from `sv_list` (a list of
    `_lib.SvSpec`). A `(SvSpec * n)` array is built and both it and the SvSpec
    code buffers are stashed on the struct (``._svs_keep`` / ``._sv_list``) so
    they are not garbage-collected while the C side holds the pointer. Returns
    `bandspec`."""
    n = len(sv_list)
    arr = (SvSpec * n)(*sv_list)
    bandspec.out_path = str(path).encode()
    bandspec.fs = float(fs)
    bandspec.quant = int(quant)
    bandspec.dither = 0
    bandspec.total_samples = int(total_samples)
    bandspec.block_samples = int(block_samples)
    bandspec.nthreads = int(nthreads)
    bandspec.svs = ctypes.cast(arr, ctypes.POINTER(SvSpec))
    bandspec.nsv = n
    bandspec._svs_keep = arr        # keep the (SvSpec*n) array alive
    bandspec._sv_list = list(sv_list)  # keep the source structs (code bufs) alive
    return bandspec


def run_bands(bands, progress_cb=None) -> int:
    """Run `synth_run_bands` over a list of `_lib.BandSpec`. Returns the C rc
    (0 on success, first non-zero per-band rc otherwise)."""
    lib = load_lib()
    n = len(bands)
    arr = (BandSpec * n)(*bands)
    _cb_keep = _PROGRESS_CB(progress_cb) if progress_cb is not None else None
    cb = ctypes.cast(_cb_keep, ctypes.c_void_p) if _cb_keep is not None else None
    return int(lib.synth_run_bands(arr, ctypes.c_int(n), cb, None))


def _bind_run(lib: ctypes.CDLL) -> None:
    lib.synth_run.restype = ctypes.c_int
    lib.synth_run.argtypes = [
        ctypes.c_char_p, ctypes.POINTER(RunSpec), ctypes.POINTER(SvSpec),
        ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
    lib.synth_run_bands.restype = ctypes.c_int
    lib.synth_run_bands.argtypes = [
        ctypes.POINTER(BandSpec), ctypes.c_int, ctypes.c_void_p,
        ctypes.c_void_p]
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
    lib.synth_glonass_state.restype = None
    lib.synth_glonass_state.argtypes = [ctypes.POINTER(GloEph), ctypes.c_double,
                                        _dp, _dp, _dp]


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


def glonass_state(glo_struct: "GloEph", t: float):
    """Propagate a GloEph via the native GLONASS PZ-90 RK4 integrator. Returns
    ``(pos3, vel3, clk)`` with pos/vel as 3-tuples of floats."""
    lib = load_lib()
    pos = (ctypes.c_double * 3)()
    vel = (ctypes.c_double * 3)()
    clk = ctypes.c_double()
    lib.synth_glonass_state(ctypes.byref(glo_struct), ctypes.c_double(t),
                            pos, vel, ctypes.byref(clk))
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
