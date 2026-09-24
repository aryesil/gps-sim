from __future__ import annotations

import ctypes
import pathlib
import sys

import numpy as np

ABI_VERSION = 26
_NATIVE_DIR = pathlib.Path(__file__).parent / "native"
if sys.platform == "darwin":
    _EXT = "dylib"
elif sys.platform == "win32":
    _EXT = "dll"
else:
    _EXT = "so"
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
        # SP-B -- per-block trajectory knots (traj_nknots 0 => Phase-1 path).
        ("traj_nknots", ctypes.c_int),
        ("traj_knot_samples", ctypes.c_uint64),
        ("traj_carr_freq", ctypes.POINTER(ctypes.c_double)),
        ("traj_carr_phase", ctypes.POINTER(ctypes.c_double)),
        ("traj_code_rate", ctypes.POINTER(ctypes.c_double)),
        ("traj_code_phase", ctypes.POINTER(ctypes.c_double)),
        # SP-D -- nav-message symbol rate (0.0 => 50 Hz GPS LNAV).
        ("nav_sym_rate_hz", ctypes.c_double),
        # ABI 25 -- transmit-time modulation clock (see abi.h).
        ("tx_time_valid", ctypes.c_int),
        ("tx_chips_offset", ctypes.c_double),
        # ABI 26 -- Galileo E1 CBOC(6,1,1/11) sign (0 = BOC(1,1)).
        ("cboc", ctypes.c_int),
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
    s.traj_nknots = 0
    s.traj_knot_samples = 0
    s.traj_carr_freq = None
    s.traj_carr_phase = None
    s.traj_code_rate = None
    s.traj_code_phase = None
    s.nav_sym_rate_hz = 0.0
    s.tx_time_valid = 0
    s.tx_chips_offset = 0.0
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
    lib.synth_code_l2c.restype = ctypes.c_int
    lib.synth_code_l2c.argtypes = [
        ctypes.c_int, ctypes.POINTER(ctypes.c_int8), ctypes.c_int,
        ctypes.POINTER(ctypes.c_int8), ctypes.c_int]
    lib.synth_code_l5.restype = ctypes.c_int
    lib.synth_code_l5.argtypes = [
        ctypes.c_int, ctypes.POINTER(ctypes.c_int8), ctypes.c_int,
        ctypes.POINTER(ctypes.c_int8), ctypes.c_int]
    lib.synth_code_e5a.restype = ctypes.c_int
    lib.synth_code_e5a.argtypes = [
        ctypes.c_int, ctypes.POINTER(ctypes.c_int8), ctypes.c_int,
        ctypes.POINTER(ctypes.c_int8), ctypes.c_int]
    lib.synth_code_b2a.restype = ctypes.c_int
    lib.synth_code_b2a.argtypes = [
        ctypes.c_int, ctypes.POINTER(ctypes.c_int8), ctypes.c_int,
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
    5 Galileo E1B, 6 Galileo E1C, 7 NavIC L5-SPS) -- SEPARATE from the
    propagation sys int. There is no native GLONASS branch (any other value
    returns rc -1); GLONASS G1 is generated in Python by
    ``engine._glo_g1_code``. Returns
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


def code_l2c(prn: int):
    """(cm[10230], cl[767250]) int8 {-1,+1} arrays for GPS L2C PRN `prn`."""
    lib = load_lib()
    _p = ctypes.POINTER(ctypes.c_int8)
    cm = np.zeros(10230, np.int8)
    cl = np.zeros(767250, np.int8)
    rc = lib.synth_code_l2c(int(prn), cm.ctypes.data_as(_p), 10230,
                            cl.ctypes.data_as(_p), 767250)
    if rc != 0:
        raise ValueError(f"synth_code_l2c rejected prn {prn}")
    return cm, cl


def code_l5(prn: int):
    """(i5[10230], q5[10230]) int8 {-1,+1} arrays for GPS / QZSS L5 PRN `prn`.

    I5 carries the CNAV data component; Q5 is the dataless pilot. The
    Neuman-Hoffman secondaries (NH10 on I5, NH20 on Q5) are applied by the
    mixer via SvSpec.sec_code, not here."""
    lib = load_lib()
    _p = ctypes.POINTER(ctypes.c_int8)
    i5 = np.zeros(10230, np.int8)
    q5 = np.zeros(10230, np.int8)
    rc = lib.synth_code_l5(int(prn), i5.ctypes.data_as(_p), 10230,
                           q5.ctypes.data_as(_p), 10230)
    if rc != 0:
        raise ValueError(f"synth_code_l5 rejected prn {prn}")
    return i5, q5


def code_e5a(prn: int):
    """(ei[10230], eq[10230]) int8 {-1,+1} arrays for Galileo E5a PRN `prn`.

    Fixed ICD memory codes (not LFSR-generated). E5a-I carries F/NAV data
    plus the CS20 secondary (applied by the mixer via SvSpec.sec_code, not
    here); E5a-Q is the dataless pilot, not emitted by the engine."""
    lib = load_lib()
    _p = ctypes.POINTER(ctypes.c_int8)
    ei = np.zeros(10230, np.int8)
    eq = np.zeros(10230, np.int8)
    rc = lib.synth_code_e5a(int(prn), ei.ctypes.data_as(_p), 10230,
                            eq.ctypes.data_as(_p), 10230)
    if rc != 0:
        raise ValueError(f"synth_code_e5a rejected prn {prn}")
    return ei, eq


def code_b2a(prn: int):
    """(bd[10230], bp[10230]) int8 {-1,+1} arrays for BeiDou B2a PRN `prn`
    (1..63). Real 13-bit dual-LFSR ranging codes (BDS-SIS-ICD-B2a-1.0). B2a
    data carries B-CNAV2 plus the 5-chip secondary "00010" (applied by the
    mixer via SvSpec.sec_code, not here); B2a pilot is the dataless
    100-chip-secondary component, not emitted by the engine."""
    lib = load_lib()
    _p = ctypes.POINTER(ctypes.c_int8)
    bd = np.zeros(10230, np.int8)
    bp = np.zeros(10230, np.int8)
    rc = lib.synth_code_b2a(int(prn), bd.ctypes.data_as(_p), 10230,
                            bp.ctypes.data_as(_p), 10230)
    if rc != 0:
        raise ValueError(f"synth_code_b2a rejected prn {prn}")
    return bd, bp


def code_navic(prn: int) -> np.ndarray:
    """1023-chip {-1,+1} int8 array for NavIC (IRNSS) L5-SPS PRN `prn`
    (1..14). Single ranging component -- no pilot, no secondary code."""
    primary, _ = code(7, int(prn), 1023, 0)
    return primary


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


_F64 = ctypes.POINTER(ctypes.c_double)


def _as_f64(seq):
    a = np.ascontiguousarray(seq, dtype=np.float64)
    return a.ctypes.data_as(_F64), a


def debug_mix_traj(code, code_rate, code_phase0, carrier_freq, fs, sample0, n,
                   knot_samples, carr_freq, carr_phase, code_rate_knots,
                   code_phase_knots):
    """SP-B trajectory mixer shim over gs::mix_block. The four knot sequences
    must be equal length (= traj_nknots). Returns complex64, length n."""
    lib = load_lib()
    lib.synth_debug_mix_traj.restype = None
    lib.synth_debug_mix_traj.argtypes = [
        _I8, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double,
        ctypes.c_uint64, ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
        _F64, _F64, _F64, _F64, _F32]
    cptr, _kc = _as_i8(code)
    cf, _k1 = _as_f64(carr_freq)
    cph, _k2 = _as_f64(carr_phase)
    cr, _k3 = _as_f64(code_rate_knots)
    cph2, _k4 = _as_f64(code_phase_knots)
    nk = len(_k1)
    assert len(_k2) == len(_k3) == len(_k4) == nk
    out = (ctypes.c_float * (2 * int(n)))()
    lib.synth_debug_mix_traj(cptr, code_rate, code_phase0, carrier_freq, fs,
                             int(sample0), int(n), int(nk), int(knot_samples),
                             cf, cph, cr, cph2, out)
    a = np.array(list(out), dtype=np.float32)
    return a[0::2] + 1j * a[1::2]


def attach_trajectory(spec, knot_samples, carr_freq, carr_phase,
                      code_rate, code_phase):
    """Attach SP-B per-block trajectory knots to a SvSpec. The four knot
    sequences must be equal length (one entry per mixer block). The ctypes
    arrays are stashed on the struct as ``spec._traj_keep`` so they outlive
    the run."""
    n = len(carr_freq)
    if not (len(carr_phase) == len(code_rate) == len(code_phase) == n):
        raise ValueError("trajectory knot sequences differ in length")
    cf = (ctypes.c_double * n)(*(float(x) for x in carr_freq))
    cph = (ctypes.c_double * n)(*(float(x) for x in carr_phase))
    cr = (ctypes.c_double * n)(*(float(x) for x in code_rate))
    kp = (ctypes.c_double * n)(*(float(x) for x in code_phase))
    spec.traj_nknots = n
    spec.traj_knot_samples = int(knot_samples)
    spec.traj_carr_freq = ctypes.cast(cf, _F64)
    spec.traj_carr_phase = ctypes.cast(cph, _F64)
    spec.traj_code_rate = ctypes.cast(cr, _F64)
    spec.traj_code_phase = ctypes.cast(kp, _F64)
    spec._traj_keep = (cf, cph, cr, kp)


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
