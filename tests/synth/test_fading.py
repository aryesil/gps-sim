"""ABI 28 land-mobile-satellite channel (backend/synth/native/fading.cpp):
ChaCha20 randomness, three-state Loo statistics per environment/elevation,
Jakes Doppler spread, band sharing, and the mixer's complex-gain knots."""
import ctypes

import numpy as np
import pytest

from backend.synth import _lib, fading

_KEY_A = bytes(range(32)).hex()
_KEY_B = bytes(range(1, 33)).hex()
L1, L5 = 1575.42e6, 1176.45e6


def _lib_f():
    lib = _lib.load_lib()
    _lib.bind_fading(lib)
    return lib


def _cfg(d, sysc="G", carrier=L1, el=45.0):
    c = _lib.FadingCfg()
    fading.FadingConfig.from_dict(d).for_run().fill(c, sysc, carrier, el)
    return c


def _gain(d, prn, t, **kw):
    out = (ctypes.c_double * 2)()
    _lib_f().fading_gain_complex(ctypes.byref(_cfg(d, **kw)), prn, float(t), out)
    return complex(out[0], out[1])


def _series(d, prn, t0, dt, n, **kw):
    out = (ctypes.c_double * (2 * n))()
    _lib_f().fading_gain_series(ctypes.byref(_cfg(d, **kw)), prn, t0, dt, n, out)
    a = np.frombuffer(out, dtype=np.float64)
    return a[0::2] + 1j * a[1::2]


def _comp(d, prn, t, **kw):
    out = (ctypes.c_double * 8)()
    _lib_f().fading_components(ctypes.byref(_cfg(d, **kw)), prn, float(t), out)
    return list(out)


def _params(d, **kw):
    return fading.params(fading.FadingConfig.from_dict(d), **{
        {"carrier": "carrier_hz", "el": "el_deg"}.get(k, k): v
        for k, v in kw.items()})


def _seeded(env="suburban", speed=10.0, seed=3):
    return {"model": "seeded", "environment": env, "speed_mps": speed,
            "seed": seed}


def _keyed(key=_KEY_A, env="suburban", speed=10.0):
    return {"model": "keyed", "environment": env, "speed_mps": speed,
            "key": key}


def _j0(x):
    th = np.linspace(0.0, np.pi, 20001)
    return np.trapezoid(np.cos(x * np.sin(th)), th) / np.pi


# ---- plumbing ----------------------------------------------------------------

def test_abi_version_matches_library():
    assert _lib.load_lib().synth_abi_version() == _lib.ABI_VERSION == 28


def test_chacha20_rfc8439_block_vector():
    # RFC 8439 section 2.3.2
    out = (ctypes.c_uint8 * 64)()
    nonce = bytes.fromhex("000000090000004a00000000")
    _lib_f().fading_chacha20_block(bytes(range(32)), 1, nonce, out)
    assert bytes(out).hex() == (
        "10f1e7e4d13b5915500fdd1fa32071c4c7d1f4c733c068030422aa9ac3d46c4e"
        "d2826446079faa0914c2d705d98b02a2b5129cd1de164eb9cbd083e8a2503c4e")


def test_off_model_is_unity():
    assert _gain({"model": "off"}, 5, 3.3) == 1.0


def test_config_validation_and_legacy_name():
    for bad in ({"model": "weird"}, {"model": "seeded", "environment": "moon"},
                {"model": "seeded", "speed_mps": -1},
                {"model": "keyed", "key": "zz"},
                {"model": "keyed", "key": "00" * 16}):
        with pytest.raises(ValueError):
            fading.FadingConfig.from_dict(bad)
    legacy = fading.FadingConfig.from_dict(
        {"model": "lognormal", "sigma_db": 3.0, "coherence_s": 2.0, "seed": 4})
    assert legacy.model == "seeded" and legacy.seed == 4


def test_keyed_run_keys():
    given = fading.FadingConfig.from_dict(_keyed())
    assert given.for_run().key == bytes(range(32))
    blank = fading.FadingConfig.from_dict(_keyed(key=None))
    k1, k2 = blank.for_run().key, blank.for_run().key
    assert len(k1) == 32 and k1 != k2
    assert blank.for_run().key_id() != given.key_id()
    with pytest.raises(ValueError):
        blank.fill(_lib.FadingCfg(), "G", L1, 45.0)


def test_seeded_is_keyed_under_the_derived_key():
    # key = ChaCha20(zero key, counter 0, nonce = seed LE || "SEED")[:32]
    out = (ctypes.c_uint8 * 64)()
    seed = 0x1234_5678_9ABC
    _lib_f().fading_chacha20_block(bytes(32), 0,
                                   seed.to_bytes(8, "little") + b"SEED", out)
    key = bytes(out)[:32].hex()
    for t in (0.0, 1.7, 55.5):
        assert _gain(_seeded(seed=seed), 9, t) == _gain(_keyed(key), 9, t)


# ---- determinism / random access ----------------------------------------------

def test_sequential_walk_equals_fresh_evaluation():
    # The engine walks knots forward with block caches; any single evaluation
    # must give the identical value (IQ independent of fs / block size).
    d = _seeded(env="urban")
    dt = _params(d)["dt_knot_s"]
    ser = _series(d, 7, 0.0, dt, 4000)
    for i in (0, 1, 999, 2500, 3999):
        assert ser[i] == _gain(d, 7, i * dt)


def test_independent_across_keys_prns_systems_and_seeds():
    ts = (0.0, 0.5, 60)
    a = _series(_keyed(), 5, *ts)
    for other in (_series(_keyed(_KEY_B), 5, *ts),
                  _series(_keyed(), 6, *ts),
                  _series(_keyed(), 5, *ts, sysc="E"),
                  _series(_seeded(seed=4), 5, *ts)):
        assert not np.allclose(a, other)
    assert np.array_equal(a, _series(_keyed(), 5, *ts))


def test_blockage_shared_across_bands_diffuse_is_not():
    d = _seeded(env="urban")
    for t in (0.3, 7.7, 41.0):
        c1, c5 = _comp(d, 12, t, carrier=L1), _comp(d, 12, t, carrier=L5)
        assert c1[:3] == c5[:3]           # state z, shadowing x, direct amp
        assert c1[3:5] != c5[3:5]         # diffuse multipath


def test_gain_is_continuous():
    d = _seeded(env="urban", speed=10.0)
    dt = _params(d)["dt_knot_s"] / 8
    g = _series(d, 3, 0.0, dt, 40000)
    assert np.abs(np.diff(g)).max() < 0.1


# ---- physics -----------------------------------------------------------------

def test_doppler_spread_is_speed_over_wavelength():
    for v, car in ((1.4, L1), (20.0, L1), (20.0, L5)):
        assert _params(_seeded(speed=v), carrier=car)["doppler_hz"] == \
            pytest.approx(v * car / 299792458.0)
    assert _params(_seeded(speed=0.0))["doppler_hz"] == pytest.approx(0.01)


def test_diffuse_autocorrelation_is_jakes_j0():
    lags = (2, 4, 8, 40)
    acc = {k: [] for k in lags}
    for prn in range(1, 25):
        d = _seeded(env="urban", seed=prn)
        fd = _params(d)["doppler_hz"]
        w = np.array([complex(*_comp(d, prn, i / (20 * fd))[3:5])
                      for i in range(1500)])
        for k in lags:
            acc[k].append(np.mean(w[k:] * np.conj(w[:-k])).real
                          / np.mean(abs(w) ** 2))
    for k in lags:
        assert np.mean(acc[k]) == pytest.approx(_j0(2 * np.pi * k / 20),
                                                abs=0.04), k


def test_open_sky_is_weak_rician():
    g_db = 20 * np.log10(abs(_series(_seeded(env="open"), 5, 0.0, 0.01, 60000,
                                     el=60.0)))
    assert abs(np.mean(g_db)) < 0.3
    assert np.std(g_db) < 1.0
    assert np.min(g_db) > -6.0


def test_urban_low_elevation_is_mostly_blocked():
    lo = 20 * np.log10(abs(_series(_seeded(env="urban"), 5, 0.0, 0.01, 60000,
                                   el=15.0)))
    hi = 20 * np.log10(abs(_series(_seeded(env="urban"), 5, 0.0, 0.01, 60000,
                                   el=75.0)))
    assert np.mean(lo < -10.0) > 0.3
    assert np.mean(hi < -10.0) < np.mean(lo < -10.0)
    assert np.median(hi) > np.median(lo) + 3.0


def test_state_occupancy_matches_probabilities():
    d = _seeded(env="rural", speed=10.0)
    p = _params(d, el=30.0)
    z = np.array([_comp(d, prn, t, el=30.0)[0]
                  for prn in range(1, 21) for t in np.arange(0.0, 300.0, 1.0)])
    assert np.mean(z < p["z_los"]) == pytest.approx(p["p_los"], abs=0.07)
    assert np.mean(z >= p["z_shadow"]) == pytest.approx(
        1.0 - p["p_los"] - p["p_shadow"], abs=0.05)


def test_static_receiver_changes_slowly():
    fast = _series(_seeded(env="urban", speed=20.0), 4, 0.0, 0.05, 400)
    slow = _series(_seeded(env="urban", speed=0.0), 4, 0.0, 0.05, 400)
    assert np.std(np.diff(abs(slow))) < 0.1 * np.std(np.diff(abs(fast)))


# ---- mixer complex-gain knots --------------------------------------------------

def test_mixer_applies_interpolated_complex_gain():
    code = np.array(_lib.ca_code(3), dtype=np.int8)
    # fs / chip rate non-integer and a fractional code phase keep samples
    # off chip edges, where per-chunk time rounding can flip a chip.
    fs, n, s0 = 2.5e6, 6000, 123_456
    dt, j0 = 1e-4, int(s0 / 2.5e6 / 1e-4)
    rng = np.random.default_rng(2)
    knots = rng.normal(size=40) + 1j * rng.normal(size=40)
    base = _lib.debug_mix_gain(code, 1.023e6, 11.37, 1500.0, fs, s0, n,
                               j0, dt, [1.0 + 0j, 1.0 + 0j])
    got = _lib.debug_mix_gain(code, 1.023e6, 11.37, 1500.0, fs, s0, n,
                              j0, dt, knots)
    got4 = _lib.debug_mix_gain(code, 1.023e6, 11.37, 1500.0, fs, s0, n,
                               j0, dt, knots, nthreads=4)
    t = (s0 + np.arange(n)) / fs
    x = t / dt - j0
    j = np.clip(np.floor(x).astype(int), 0, knots.size - 2)
    f = np.clip(x - j, 0.0, 1.0)
    g = knots[j] + f * (knots[j + 1] - knots[j])
    assert np.allclose(got, base * g, atol=1e-4)
    assert np.max(np.abs(got - got4)) < 1e-2   # NCO seeded per chunk
