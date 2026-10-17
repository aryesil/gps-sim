import importlib
import os


def test_constants_have_expected_values():
    cfg = importlib.import_module("backend.config")
    assert cfg.L1_HZ == 1575.42e6
    assert cfg.CA_CHIP_HZ == 1.023e6
    assert cfg.CA_CODE_LEN == 1023
    assert cfg.NAV_BIT_HZ == 50
    assert cfg.C == 299792458.0
    assert cfg.ALLOW_TX is False
    assert cfg.DEFAULT_SAMPLE_RATE == 2.6e6


def test_l2_l5_band_centres_are_integer_multiples_of_1023():
    from backend import config
    base = 10.23e6
    assert config.L2_HZ == 120 * base
    assert config.L5_HZ == 115 * base
    assert config.L1_HZ == 154 * base


def test_precise_defaults():
    # reload: the autouse _offline_sp3 fixture points PRECISE_DIR at a tmp dir
    cfg = importlib.reload(importlib.import_module("backend.config"))
    assert cfg.PRECISE_DIR == cfg.DATA_DIR / "precise"
    assert cfg.PRECISE_DIR.is_dir()
    # Ships free, anonymous (no-login) IGS product mirrors by default; a
    # download is still only performed on an explicit request.
    assert len(cfg.PRECISE_SP3_MIRRORS) >= 2
    # All entries are HTTP(S) or anonymous FTP URLs. Plain http:// is
    # allowed only for the ESA navigation-office archive (its TLS chain does
    # not verify), ftp:// only for GFZ (the one anonymous GRECJ source).
    for m in cfg.PRECISE_SP3_MIRRORS + cfg.PRECISE_SP3_ULTRA_MIRRORS:
        assert (m.startswith("https://")
                or (m.startswith("http://") and "navigation-office.esa.int" in m)
                or (m.startswith("ftp://") and "ftp.gfz-potsdam.de" in m)), m
    assert all("cddis" not in m for m in cfg.PRECISE_SP3_MIRRORS)  # needs Earthdata login
    assert any("RAP" in m for m in cfg.PRECISE_SP3_MIRRORS)       # rapid
    assert any("FIN" in m for m in cfg.PRECISE_SP3_MIRRORS)       # final
    assert any("ULT" in m for m in cfg.PRECISE_SP3_MIRRORS)       # ultra-rapid
    assert "RAP" in cfg.PRECISE_SP3_MIRRORS[0]                    # rapid tried first
    assert "MGX" in cfg.PRECISE_SP3_MIRRORS[0]                    # multi-GNSS first
    assert len(cfg.PRECISE_SP3_ULTRA_MIRRORS) >= 1
    # ultra-rapid is the last-resort tier
    tags = [t for m in cfg.PRECISE_SP3_MIRRORS for t in ("RAP", "FIN", "ULT") if t in m]
    assert tags.index("RAP") < tags.index("FIN") < tags.index("ULT")


def test_precise_sp3_mirrors_env_can_disable(monkeypatch):
    monkeypatch.setenv("PRECISE_SP3_MIRRORS", "")
    import backend.config as cfg
    importlib.reload(cfg)
    assert cfg.PRECISE_SP3_MIRRORS == []
    importlib.reload(cfg)  # restore


def test_env_override(monkeypatch):
    monkeypatch.setenv("ALLOW_TX", "1")
    monkeypatch.setenv("DEVICE_URI", "ip:10.0.0.5")
    import backend.config as cfg
    importlib.reload(cfg)
    assert cfg.ALLOW_TX is True
    assert cfg.DEVICE_URI == "ip:10.0.0.5"
    importlib.reload(cfg)  # restore


def test_rf_frontend_enabled_defaults_off(monkeypatch):
    monkeypatch.delenv("RF_FRONTEND_ENABLED", raising=False)
    import importlib
    from backend import config
    importlib.reload(config)
    assert config.RF_FRONTEND_ENABLED is False
