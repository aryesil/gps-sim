"""AD9361 TDD transmit mode: duplex switch through debugfs, ENSM tx state,
back to FDD on close. Uses a fake phy device, no hardware."""
from types import SimpleNamespace

from backend.rf.backends import ad9361


class _Attr:
    def __init__(self, value, log=None, name=""):
        self._v, self._log, self._name = value, log, name

    @property
    def value(self):
        return self._v

    @value.setter
    def value(self, v):
        self._v = v
        if self._log is not None:
            self._log.append((self._name, v))


class _Phy:
    """Just enough of the pyadi-iio ad9361 control device."""

    def __init__(self, fdd="1", debug=True):
        self.log = []
        self.fdd_mode = fdd
        self.attrs = {
            "ensm_mode": _Attr("fdd", self.log, "ensm_mode"),
            "ensm_mode_available": _Attr("sleep wait alert fdd pinctrl", None),
        }
        self.debug_attrs = {}
        if debug:
            self.debug_attrs = {
                ad9361._FDD_FLAG: _Attr(fdd, self.log, "duplex"),
                "initialize": _Attr("0", self.log, "initialize"),
            }
        self._lo = {n: _Attr("0", self.log, n) for n in ("altvoltage0", "altvoltage1")}

    def find_channel(self, name, output=False):
        return SimpleNamespace(attrs={"powerdown": self._lo[name]})

    # initialize re-reads the duplex flag: the available ENSM states follow it
    def apply_initialize(self):
        tdd = self.debug_attrs[ad9361._FDD_FLAG].value == "0"
        self.attrs["ensm_mode_available"]._v = (
            "sleep wait alert rx tx pinctrl" if tdd else "sleep wait alert fdd pinctrl")


class _Handle:
    def __init__(self, phy):
        self._ctrl = phy
        self.gains = []

    def __setattr__(self, k, v):
        if k.startswith("tx_hardwaregain_chan"):
            self.gains.append((k, v))
        object.__setattr__(self, k, v)


def _wire_initialize(phy):
    """Writing ``initialize`` makes the fake chip apply the duplex flag."""
    class _Init(_Attr):
        @property
        def value(self):
            return self._v

        @value.setter
        def value(self, v):
            _Attr.value.fset(self, v)
            phy.apply_initialize()

    phy.debug_attrs["initialize"] = _Init("0", phy.log, "initialize")


def test_switch_to_tdd_mutes_gain_and_silences_lo():
    phy = _Phy()
    _wire_initialize(phy)
    h = _Handle(phy)
    assert ad9361._set_duplex_mode(h, fdd=False) is True
    assert phy.debug_attrs[ad9361._FDD_FLAG].value == "0"
    names = [n for n, _ in phy.log]
    # TX LO goes off before the chip is re-initialised, and again after it
    assert names.index("altvoltage1") < names.index("initialize")
    assert phy.log[-1] == ("ensm_mode", "alert")
    assert ("tx_hardwaregain_chan0", ad9361._MUTE_GAIN_DB) in h.gains
    assert phy._lo["altvoltage1"].value == "1"


def test_switch_is_noop_when_already_in_mode():
    phy = _Phy(fdd="0")
    h = _Handle(phy)
    assert ad9361._set_duplex_mode(h, fdd=False) is True
    assert phy.log == []


def test_switch_reports_false_without_debugfs():
    h = _Handle(_Phy(debug=False))
    assert ad9361._set_duplex_mode(h, fdd=False) is False


def test_ensm_tx_state_is_used_in_tdd_and_fdd_otherwise():
    phy = _Phy(fdd="0")
    phy.attrs["ensm_mode_available"]._v = "sleep wait alert rx tx pinctrl"
    ad9361._set_ensm_mode(_Handle(phy), "tx")
    assert phy.attrs["ensm_mode"].value == "tx"
    phy2 = _Phy()
    ad9361._set_ensm_mode(_Handle(phy2), "fdd")
    assert phy2.attrs["ensm_mode"].value == "fdd"


def test_leave_tdd_restores_fdd_once():
    phy = _Phy(fdd="0")
    _wire_initialize(phy)
    h = _Handle(phy)
    h._gs_tdd = True
    ad9361.leave_tdd(h)
    assert phy.debug_attrs[ad9361._FDD_FLAG].value == "1"
    assert h._gs_tdd is False
    n = len(phy.log)
    ad9361.leave_tdd(h)            # second call: nothing to do
    assert len(phy.log) == n
