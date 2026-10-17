"""Unit tests for Galileo real broadcast epoch and IODnav derivation in align_epochs.

Verifies:
1. Galileo records within REAL_EPH_WINDOW_S retain their real toe, toc, and iode.
2. Galileo records outside the window are relabeled to k, and iode/iodnav is derived from k // 600.
3. inav_encode.nav_stream produces identical IODnav across word types 1, 2, 3, 4.
4. fnav_encode.nav_stream produces identical IODnav across word types 1, 2, 3, 4.
5. Concurrent I/NAV and F/NAV navigation streams have matching IODnav values.
"""
import pytest
from backend.analysis import inav_decode, inav_encode, fnav_decode, fnav_encode
from backend.ephem import ephemeris


@pytest.fixture(scope="module")
def galileo_eph():
    ephs = ephemeris.parse_rinex_multi("tests/fixtures/brdc_mixed.rnx", ("E",), require=())
    rec = [v for k, v in ephs.items() if k[0] == "E"][0]
    return rec


def test_galileo_within_real_window_unchanged(galileo_eph):
    """REAL_EPH_WINDOW_S içindeki Galileo kaydı: toe, toc, iode DEĞİŞMEMELİ."""
    orig_toe = float(galileo_eph["toe"])
    orig_toc = float(galileo_eph["toc"])
    orig_iode = galileo_eph.get("iode")
    week = int(galileo_eph["gps_week"])

    # 1 saat (3600s) fark < REAL_EPH_WINDOW_S (14400s)
    sow_near = orig_toe + 3600.0
    aligned = ephemeris.align_epochs({("E", galileo_eph["prn"]): galileo_eph},
                                     week, sow_near,
                                     kepler_grid_s=3600.0,
                                     keep_real_within_s=ephemeris.REAL_EPH_WINDOW_S)
    res = list(aligned.values())[0]

    assert res["toe"] == orig_toe
    assert res["toc"] == orig_toc
    assert res.get("iode") == orig_iode
    assert res["gps_week"] == week


def test_galileo_outside_real_window_relabelled(galileo_eph):
    """REAL_EPH_WINDOW_S dışındaki Galileo kaydı: toe == toc == k,
    iode == int(k // 600) & 0x3FF olmalı; farklı k için farklı iode üretmeli.
    """
    week = int(galileo_eph["gps_week"])
    # 20 saat (72000s) fark > REAL_EPH_WINDOW_S (14400s)
    sow_far1 = (float(galileo_eph["toe"]) + 72000.0) % 604800.0
    aligned1 = ephemeris.align_epochs({("E", galileo_eph["prn"]): galileo_eph},
                                      week, sow_far1,
                                      kepler_grid_s=3600.0,
                                      keep_real_within_s=ephemeris.REAL_EPH_WINDOW_S)
    res1 = list(aligned1.values())[0]

    k1 = (sow_far1 // 3600.0) * 3600.0
    expected_iod1 = int(k1 // 600) & 0x3FF
    assert res1["toe"] == k1
    assert res1["toc"] == k1
    assert res1["iode"] == expected_iod1

    # Farklı bir k2 epoku için farklı iode üretildiğini doğrula
    sow_far2 = (k1 + 7200.0) % 604800.0
    aligned2 = ephemeris.align_epochs({("E", galileo_eph["prn"]): galileo_eph},
                                      week, sow_far2,
                                      kepler_grid_s=3600.0,
                                      keep_real_within_s=ephemeris.REAL_EPH_WINDOW_S)
    res2 = list(aligned2.values())[0]
    k2 = (sow_far2 // 3600.0) * 3600.0
    expected_iod2 = int(k2 // 600) & 0x3FF
    assert res2["toe"] == k2
    assert res2["toc"] == k2
    assert res2["iode"] == expected_iod2
    assert expected_iod1 != expected_iod2


def test_inav_and_fnav_iodnav_consistency(galileo_eph):
    """inav_encode ve fnav_encode nav stream'lerinde:
    - I/NAV word 1, 2, 3, 4 IODnav alanlarının BİREBİR AYNI olduğunu doğrula.
    - F/NAV word 1, 2, 3, 4 IODnav alanlarının BİREBİR AYNI olduğunu doğrula.
    - Eş zamanlı üretilen I/NAV ve F/NAV nav stream'lerinde IODnav değerlerinin EŞLEŞTİĞİNİ doğrula.
    """
    eph = dict(galileo_eph)
    week = int(eph["gps_week"]) - 1024
    sow = float(eph["toe"])
    prn = int(eph["prn"])

    # 1. I/NAV nav_stream ve çözümü (en az 60 saniye: tüm word tiplerini kapsar)
    arr_inav = inav_encode.nav_stream(eph, week, sow, duration_s=60.0)
    msgs_inav = inav_decode.decode_messages(arr_inav)
    assert len(msgs_inav) >= 4, "En az 4 I/NAV sayfası çözülmeli"

    iodnav_by_type_inav = {}
    for m in msgs_inav:
        wtype = m["type"]
        if wtype in (1, 2, 3, 4):
            iodnav_by_type_inav[wtype] = m["fields"].get("iodnav")

    assert set(iodnav_by_type_inav.keys()) == {1, 2, 3, 4}, "I/NAV word 1-4 mevcut olmalı"
    vals_inav = set(iodnav_by_type_inav.values())
    assert len(vals_inav) == 1, f"I/NAV word 1-4 IODnav alanları aynı olmalı, bulunan: {iodnav_by_type_inav}"
    inav_iodnav = vals_inav.pop()

    # 2. F/NAV nav_stream ve çözümü (en az 60 saniye: word 1, 2, 3, 4 10'ar saniye)
    arr_fnav, _ = fnav_encode.nav_stream(eph, prn, week, sow, duration_s=60.0)
    msgs_fnav = fnav_decode.decode_messages(arr_fnav)
    assert len(msgs_fnav) >= 4, "En az 4 F/NAV sayfası çözülmeli"

    iodnav_by_type_fnav = {}
    for m in msgs_fnav:
        wtype = m["type"]
        if wtype in (1, 2, 3, 4):
            iodnav_by_type_fnav[wtype] = m["fields"].get("iodnav")

    assert set(iodnav_by_type_fnav.keys()) == {1, 2, 3, 4}, "F/NAV word 1-4 mevcut olmalı"
    vals_fnav = set(iodnav_by_type_fnav.values())
    assert len(vals_fnav) == 1, f"F/NAV word 1-4 IODnav alanları aynı olmalı, bulunan: {iodnav_by_type_fnav}"
    fnav_iodnav = vals_fnav.pop()

    # 3. I/NAV ve F/NAV nav stream'lerinde IODnav değerlerinin eşleştiğini doğrula
    assert inav_iodnav == fnav_iodnav == float(galileo_eph.get("iode", 0))
