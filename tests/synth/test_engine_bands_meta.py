import numpy as np

from backend.synth import _lib


def test_two_bands_write_two_files(tmp_path):
    code = _lib.code(0, 5, 1023)[0].astype(np.int8)
    sv = _lib.one_sv_spec(code, carrier_hz=1000.0, code_phase0=10.0)
    b1 = _lib.BandSpec()
    _lib.fill_band(b1, str(tmp_path / "L1.bin"), 2_600_000.0, 2, 26000, [sv])
    b2 = _lib.BandSpec()
    _lib.fill_band(b2, str(tmp_path / "G1.bin"), 16_000_000.0, 2, 160000, [sv])
    rc = _lib.run_bands([b1, b2])
    assert rc == 0
    assert (tmp_path / "L1.bin").stat().st_size == 26000 * 4  # int16 IQ
    assert (tmp_path / "G1.bin").stat().st_size == 160000 * 4


def test_abi_version_is_16():
    assert _lib.load_lib().synth_abi_version() == _lib.ABI_VERSION == 16
