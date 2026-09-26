import numpy as np
import pytest

from backend.synth import fading, _lib


def _gain(cfg_dict, prn, ts, sysc="G"):
    lib = _lib.load_lib()
    _lib.bind_fading(lib)
    c = _lib.FadingCfg()
    fading.FadingConfig.from_dict(cfg_dict).fill(c, sysc)
    return float(lib.fading_gain_linear(_lib.ctypes.byref(c), prn, ts))


def _trace_db(cfg_dict, prn, ts, sysc="G"):
    return np.array([20 * np.log10(_gain(cfg_dict, prn, float(t), sysc))
                     for t in ts])


def test_off_model_is_unity():
    for prn in (1, 15, 30):
        assert _gain({"model": "off"}, prn, 3.3) == pytest.approx(1.0)


def test_deterministic_same_inputs_same_output():
    d = {"model": "lognormal", "sigma_db": 3.0, "coherence_s": 2.0, "seed": 42}
    assert _gain(d, 7, 5.0) == _gain(d, 7, 5.0)


def test_prn_independence():
    d = {"model": "lognormal", "sigma_db": 3.0, "coherence_s": 2.0, "seed": 42}
    assert _gain(d, 7, 5.0) != _gain(d, 8, 5.0)


def test_sigma_matches_requested_over_long_run():
    d = {"model": "lognormal", "sigma_db": 2.5, "coherence_s": 0.5, "seed": 1}
    ts = np.arange(0, 2000, 0.5) + 0.123
    g_db = np.array([20 * np.log10(_gain(d, 5, float(t))) for t in ts])
    assert abs(g_db.std() - 2.5) < 0.5
    assert abs(g_db.mean()) < 0.5     # zero-mean in dB


def test_config_validation():
    with pytest.raises(ValueError):
        fading.FadingConfig.from_dict({"model": "weird"})
    with pytest.raises(ValueError):
        fading.FadingConfig.from_dict({"model": "lognormal", "coherence_s": 0})


def test_abi_version_matches_library():
    lib = _lib.load_lib()
    assert lib.synth_abi_version() == _lib.ABI_VERSION == 27


# ---- lognormal must be unchanged by ABI 27 ---------------------------------

_M64 = (1 << 64) - 1


def _mix(x):
    x = (x + 0x9E3779B97F4A7C15) & _M64
    x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & _M64
    x = ((x ^ (x >> 27)) * 0x94D049BB133111EB) & _M64
    return x ^ (x >> 31)


def _ref_lognormal_db(seed, prn, t, sigma, coh):
    def g(k):
        base = _mix(seed ^ (prn << 40) ^ ((k * 0x100000001B3) & _M64))
        u1 = (_mix(base) >> 11) / 9007199254740992.0 + 1e-12
        u2 = (_mix(base ^ 0xABCDEF) >> 11) / 9007199254740992.0
        return np.sqrt(-2 * np.log(u1)) * np.cos(2 * np.pi * u2)
    x = t / coh
    k0 = int(np.floor(x))
    f = x - k0
    w = f * f * (3 - 2 * f)
    return sigma * 1.1602387022306428 * (g(k0) * (1 - w) + g(k0 + 1) * w)


def test_lognormal_matches_reference_formula():
    d = {"model": "lognormal", "sigma_db": 3.0, "coherence_s": 2.0, "seed": 42}
    for prn, t in ((7, 5.0), (1, 0.3), (30, 123.4)):
        got = 20 * np.log10(_gain(d, prn, t))
        assert got == pytest.approx(_ref_lognormal_db(42, prn, t, 3.0, 2.0),
                                    abs=1e-4)


# ---- keyed model -----------------------------------------------------------

_KEY_A = bytes(range(32)).hex()
_KEY_B = bytes(range(1, 33)).hex()


def _keyed(key=_KEY_A, sigma=3.0, coh=1.0):
    return {"model": "keyed", "sigma_db": sigma, "coherence_s": coh, "key": key}


def test_chacha20_rfc8439_block_vector():
    # RFC 8439 section 2.3.2
    lib = _lib.load_lib()
    _lib.bind_fading(lib)
    out = (_lib.ctypes.c_uint8 * 64)()
    nonce = bytes.fromhex("000000090000004a00000000")
    lib.fading_chacha20_block(bytes(range(32)), 1, nonce, out)
    assert bytes(out).hex() == (
        "10f1e7e4d13b5915500fdd1fa32071c4c7d1f4c733c068030422aa9ac3d46c4e"
        "d2826446079faa0914c2d705d98b02a2b5129cd1de164eb9cbd083e8a2503c4e")


def test_keyed_is_deterministic_under_one_key():
    assert _gain(_keyed(), 7, 5.0) == _gain(_keyed(), 7, 5.0)


def test_keyed_traces_uncorrelated_across_keys_prns_and_systems():
    ts = np.arange(0, 400, 0.25)
    a = _trace_db(_keyed(), 5, ts)
    for other in (_trace_db(_keyed(_KEY_B), 5, ts),       # other key
                  _trace_db(_keyed(), 6, ts),             # other PRN
                  _trace_db(_keyed(), 5, ts, sysc="E")):  # G5 vs E5
        assert abs(np.corrcoef(a, other)[0, 1]) < 0.2


def test_lognormal_is_the_same_for_every_system_but_keyed_is_not():
    # documents why the keyed model carries a domain tag
    d = {"model": "lognormal", "sigma_db": 3.0, "coherence_s": 1.0, "seed": 9}
    assert _gain(d, 5, 3.3, "G") == _gain(d, 5, 3.3, "E")
    assert _gain(_keyed(), 5, 3.3, "G") != _gain(_keyed(), 5, 3.3, "E")


def test_keyed_sigma_and_mean_over_long_run():
    ts = np.arange(0, 3000, 0.5) + 0.123
    g_db = _trace_db(_keyed(sigma=2.5, coh=0.5), 5, ts)
    assert abs(g_db.std() - 2.5) < 0.5
    assert abs(g_db.mean()) < 0.5


def test_keyed_is_continuous():
    ts = np.arange(0, 50, 0.001)
    g_db = _trace_db(_keyed(sigma=3.0, coh=1.0), 3, ts)
    assert np.abs(np.diff(g_db)).max() < 0.05


def test_keyed_has_no_shared_knot_grid():
    # lognormal knots sit on k*coherence_s for every SV; the keyed model's
    # per-SV phase/spacing/jitter moves them. Locate knots as the points
    # where the second difference jumps (smoothstep is C1, not C2).
    dt = 0.002
    ts = np.arange(0, 40, dt)

    def knots(d, prn):
        g = np.log(np.array([_gain(d, prn, float(t)) for t in ts]))
        d2 = np.abs(np.diff(g, 2))
        jump = np.abs(np.diff(d2))
        return ts[1:-2][jump > 20 * np.median(jump) + 1e-9]

    logn = {"model": "lognormal", "sigma_db": 3.0, "coherence_s": 1.0, "seed": 4}
    lk = knots(logn, 5)
    assert len(lk) and np.all(np.abs(lk - np.round(lk)) < 3 * dt)
    kk = knots(_keyed(), 5)
    assert len(kk) >= 10
    assert np.mean(np.abs(kk - np.round(kk)) < 3 * dt) < 0.5


def test_keyed_config_parsing_and_fresh_run_keys():
    with pytest.raises(ValueError):
        fading.FadingConfig.from_dict(_keyed(key="zz"))
    with pytest.raises(ValueError):
        fading.FadingConfig.from_dict(_keyed(key="00" * 16))
    given = fading.FadingConfig.from_dict(_keyed())
    assert given.for_run().key == bytes(range(32))
    blank = fading.FadingConfig.from_dict(_keyed(key=None))
    k1, k2 = blank.for_run().key, blank.for_run().key
    assert len(k1) == 32 and k1 != k2
    assert blank.for_run().key_id() != given.key_id()
    with pytest.raises(ValueError):
        blank.fill(_lib.FadingCfg(), "G")
