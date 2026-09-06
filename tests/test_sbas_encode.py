"""SP-D: SBAS L1 encoder -- preamble cycle, continuous FEC, CRC round-trip."""
import numpy as np
import pytest

from backend.analysis import sbas_encode as S
from backend.ephem import ephemeris


@pytest.fixture(scope="module")
def srec():
    e = ephemeris.parse_rinex_multi("tests/fixtures/brdc_mixed.rnx", ("S",),
                                    require=())
    rec = [v for k, v in e.items() if k[0] == "S"][0]
    rec["prn"] = 120
    return rec


def test_conv_is_rate_half_and_invertible():
    conv = S._Conv()
    data = [1, 0, 1, 1, 0, 0, 1, 0] * 10
    coded = conv.encode(data)
    assert len(coded) == 2 * len(data)
    assert S.conv_decode_stream(coded) == data


def test_block_layout_and_crc(srec):
    blk = S.build_block(0, 9, srec)
    assert len(blk) == 250
    assert blk[:8] == [0, 1, 0, 1, 0, 0, 1, 1]          # 0x53
    assert S.crc24q(blk[:226]) == blk[226:]


def test_stream_decodes_with_preamble_cycle_and_crc(srec):
    arr, rate = S.nav_stream(srec, 0, 561600.0, 6)
    assert rate == 500.0
    blks = S.decode_stream(arr)
    assert len(blks) >= 6
    assert [b["preamble"] for b in blks[:3]] == [0x53, 0x9A, 0xC6]
    assert all(b["crc_ok"] for b in blks)
    assert blks[0]["type"] == 9


def test_stream_is_deterministic(srec):
    a, _ = S.nav_stream(srec, 0, 561600.0, 6)
    b, _ = S.nav_stream(srec, 0, 561600.0, 6)
    assert np.array_equal(a, b)
    assert sorted(set(a.tolist())) == [-1, 1]
