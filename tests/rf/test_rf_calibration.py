import pytest

from backend.rf.frontend import calibration, capabilities


class _FakeAttr:
    def __init__(self, value):
        self.value = value


class _FakeCtrl:
    def __init__(self, calib_mode_available="auto manual tx_quad rx_quad rf_dc_offs",
                 settle_after_reads=1, has_calib_mode=True, has_temp=True):
        try:
            self.attrs = {}
            if has_calib_mode:
                self.attrs["calib_mode_available"] = _FakeAttr(calib_mode_available)
                self.attrs["calib_mode"] = _FakeAttr("auto")
        except Exception:
            # If attrs is a property that raises during initialization, skip
            pass
        self._settle_after_reads = settle_after_reads
        self._reads_since_trigger = 0
        self._has_temp = has_temp

    def find_channel(self, name):
        if name == "temp0" and self._has_temp:
            return object()
        raise Exception(f"no such channel {name!r}")


class _FakeSDR:
    def __init__(self, ctrl):
        self._ctrl = ctrl
        self.tx_lo = 1_575_420_000
        self.tx_hardwaregain_chan0 = -50.0


def test_detect_reports_full_capabilities_when_everything_present():
    sdr = _FakeSDR(_FakeCtrl())
    caps = capabilities.detect(sdr)
    assert caps.supports_tx_quad_calibration is True
    assert caps.supports_tx_lo_control is True
    assert caps.supports_tx_hardware_gain is True
    assert caps.supports_temperature_readout is True


def test_detect_degrades_missing_calib_mode_without_raising():
    sdr = _FakeSDR(_FakeCtrl(has_calib_mode=False))
    caps = capabilities.detect(sdr)
    assert caps.supports_tx_quad_calibration is False
    assert caps.supports_tx_lo_control is True  # unrelated capability unaffected


def test_detect_degrades_missing_temperature_without_raising():
    sdr = _FakeSDR(_FakeCtrl(has_temp=False))
    caps = capabilities.detect(sdr)
    assert caps.supports_temperature_readout is False


def test_detect_never_raises_on_a_bare_object():
    class _Empty:
        pass
    caps = capabilities.detect(_Empty())
    assert caps == capabilities.DeviceCapabilities(False, False, False, False)


def _settle_reader(ctrl, settle_after=1):
    state = {"reads": 0}
    real_attr = ctrl.attrs["calib_mode"]
    class _SettlingAttr:
        @property
        def value(self):
            state["reads"] += 1
            if state["reads"] > settle_after:
                return "auto"
            return real_attr.value
        @value.setter
        def value(self, v):
            real_attr.value = v
    ctrl.attrs["calib_mode"] = _SettlingAttr()


def test_calibrate_success_when_calib_mode_settles():
    ctrl = _FakeCtrl()
    _settle_reader(ctrl, settle_after=2)
    sdr = _FakeSDR(ctrl)
    caps = capabilities.detect(sdr)
    result = calibration.calibrate(sdr, caps, tx_lo_hz=1_574_420_000,
                                    target_rf_hz=1_575_420_000,
                                    baseband_offset_hz=1_000_000)
    assert result.success is True
    assert result.calibration_type == "TX_QUAD"
    assert result.error_message is None
    assert result.tx_lo_hz == 1_574_420_000
    assert result.target_rf_hz == 1_575_420_000
    assert result.baseband_offset_hz == 1_000_000


def test_calibrate_unsupported_device_never_fabricates_success():
    sdr = _FakeSDR(_FakeCtrl(has_calib_mode=False))
    caps = capabilities.detect(sdr)
    result = calibration.calibrate(sdr, caps, tx_lo_hz=1_575_420_000,
                                    target_rf_hz=1_575_420_000, baseband_offset_hz=0)
    assert result.success is False
    assert result.calibration_type == "NONE"
    assert "calib_mode" in result.error_message


def test_calibrate_reports_failure_when_trigger_value_not_available():
    ctrl = _FakeCtrl(calib_mode_available="auto manual rx_quad")
    sdr = _FakeSDR(ctrl)
    caps = capabilities.detect(sdr)
    result = calibration.calibrate(sdr, caps, tx_lo_hz=1_575_420_000,
                                    target_rf_hz=1_575_420_000, baseband_offset_hz=0)
    assert result.success is False
    assert "tx_quad" in result.error_message


def test_calibrate_timeout_reports_failure_not_exception(monkeypatch):
    ctrl = _FakeCtrl()
    # calib_mode never reverts from "tx_quad" -- simulates a stuck/slow device.
    sdr = _FakeSDR(ctrl)
    caps = capabilities.detect(sdr)
    monkeypatch.setattr(calibration, "_POLL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(calibration, "_POLL_INTERVAL_S", 0.01)
    result = calibration.calibrate(sdr, caps, tx_lo_hz=1_575_420_000,
                                    target_rf_hz=1_575_420_000, baseband_offset_hz=0)
    assert result.success is False
    assert "did not clear" in result.error_message


def test_calibrate_catches_hardware_exception_as_a_result_not_a_raise():
    class _BoomCtrl(_FakeCtrl):
        @property
        def attrs(self):
            raise RuntimeError("iio context gone")
        @attrs.setter
        def attrs(self, v):
            pass
    ctrl = _BoomCtrl()
    sdr = _FakeSDR(ctrl)
    caps = capabilities.DeviceCapabilities(True, True, True, True)
    result = calibration.calibrate(sdr, caps, tx_lo_hz=1_575_420_000,
                                    target_rf_hz=1_575_420_000, baseband_offset_hz=0)
    assert result.success is False
    assert "iio context gone" in result.error_message
