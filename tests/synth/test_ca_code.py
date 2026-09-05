from backend import inspector
from backend.synth import _lib

# IS-GPS-200 Table 3-Ia: first 10 chips of the C/A code, octal (MSB first).
# NOTE: the task brief listed 0o1330 / 0o1400 / 0o1725 for PRN 19 / 25 / 32;
# those are transcription errors. The IS-GPS-200 Table 3-Ia values are
# 0o1633 / 0o1743 / 0o1712 and are corroborated three ways: the C++ generator,
# the independent backend.inspector.ca_code validator, and the published
# phase-select table (PRN 19 = stages 3+6, first 10 chips 1633, etc.).
_FIRST10_OCTAL = {1: 0o1440, 2: 0o1620, 3: 0o1710, 4: 0o1744,
                  5: 0o1133, 6: 0o1455, 7: 0o1131,
                  19: 0o1633, 25: 0o1743, 32: 0o1712}


def _first10_octal(bits01):
    v = 0
    for b in bits01[:10]:
        v = (v << 1) | b
    return v


def test_ca_code_length_and_alphabet():
    c = _lib.ca_code(5)
    assert len(c) == 1023
    assert set(c) == {-1, 1}


def test_ca_code_first_ten_chips_match_isgps200():
    for prn, want in _FIRST10_OCTAL.items():
        bits01 = [0 if x == 1 else 1 for x in _lib.ca_code(prn)]  # +1 -> 0, -1 -> 1
        assert _first10_octal(bits01) == want, prn


def test_ca_code_balance():
    # A Gold code of length 1023 has 512 ones and 511 zeros (or vice versa).
    for prn in (1, 7, 13, 31):
        ones = sum(1 for x in _lib.ca_code(prn) if x == -1)
        assert ones in (511, 512)


def test_cpp_ca_code_matches_inspector_reference():
    # inspector.ca_code is the validator-side generator; it returns an int8
    # ndarray with the same 0->+1, 1->-1 chip mapping, so compare directly.
    for prn in range(1, 33):
        assert _lib.ca_code(prn) == [int(x) for x in inspector.ca_code(prn)]
