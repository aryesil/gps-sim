import numpy as np
import pytest

from backend.synth import fading, _lib


def _gain(cfg_dict, prn, ts):
    lib = _lib.load_lib()
    _lib.bind_fading(lib)
    c = _lib.FadingCfg()
    c.model = 1 if cfg_dict["model"] == "lognormal" else 0
    c.sigma_db = cfg_dict.get("sigma_db", 0.0)
    c.coherence_s = cfg_dict.get("coherence_s", 1.0)
    c.seed = cfg_dict.get("seed", 0)
    return float(lib.fading_gain_linear(_lib.ctypes.byref(c), prn, ts))


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
    assert lib.synth_abi_version() == _lib.ABI_VERSION == 11
