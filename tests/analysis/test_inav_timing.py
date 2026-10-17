"""Timing verification tests for Galileo E1 I/NAV navigation stream (Stage 4).

Verifies:
1. inav_decode.decode_messages annotates each decoded page with sym_idx.
2. The transmit time t_tx = sow + sym_idx / 250.0 matches the TOW broadcast in Word Types 5 and 0.
3. No 4 ms, 1 s, or 2 s offsets exist between transmit time and broadcast TOW.
"""
import pytest
from backend.analysis import inav_decode, inav_encode
from backend.ephem import ephemeris


@pytest.fixture(scope="module")
def galileo_eph():
    ephs = ephemeris.parse_rinex_multi("tests/fixtures/brdc_mixed.rnx", ("E",), require=())
    rec = [v for k, v in ephs.items() if k[0] == "E"][0]
    return rec


def test_inav_page_timing_and_tow_alignment(galileo_eph):
    """Galileo I/NAV 2 saniye nominal sayfa iletim zamanı (t_tx) ile
    Word 5 ve Word 0 içinde yayınlanan TOW arasındaki bağıntıyı doğrula.
    Galileo OS SIS ICD Madde 4.1.3 gereği TOW, sayfanın başlangıç anına (t_tx) eşittir.
    """
    sow = 100000.0
    week = 1410
    duration_s = 120.0

    arr = inav_encode.nav_stream(galileo_eph, week, sow, duration_s=duration_s)
    msgs = inav_decode.decode_messages(arr)
    assert len(msgs) >= 30, f"En az 30 sayfa çözülmeli, bulunan: {len(msgs)}"

    tow_checked = 0
    for idx, page in enumerate(msgs):
        assert "sym_idx" in page, f"Sayfa {idx} 'sym_idx' alanını taşımalı"
        sym_idx = page["sym_idx"]
        t_tx = sow + sym_idx / 250.0

        wtype = page["type"]
        tow = page["fields"].get("tow")
        if tow is not None:
            # Word 5 ve Word 0'da TOW yayınlanır.
            assert wtype in (0, 5)
            # t_tx ile tow birebir eşit olmalı (sıfır kayma)
            assert t_tx == pytest.approx(tow, abs=1e-6), (
                f"Zamanlama kayması tespit edildi! Sayfa {idx} (tip {wtype}): "
                f"t_tx={t_tx}, tow={tow}, fark={t_tx - tow} s"
            )
            tow_checked += 1

    assert tow_checked >= 10, f"En az 10 adet TOW içeren sayfa test edilmeli, kontrol edilen: {tow_checked}"


def test_inav_end_to_end_iq_timing_and_demod(tmp_path):
    """C++ Native motor tarafından üretilen gerçek RF IQ dosyasını (gpssim.bin)
    demod_symbols_e1b ile demodüle edip I/NAV sayfalarını ve TOW zamanlamasını doğrula.
    Böylece mikser, kod fazı, tx_chips_offset ve geometrik gecikme (rho/c) zincirinin tamamı sınanır.
    """
    import datetime as dt
    import json
    import numpy as np
    from backend.scenario import ScenarioRequest
    from backend.synth import engine

    fs = 6_000_000.0
    req = ScenarioRequest(
        rinex_path="tests/fixtures/brdc_mixed.rnx",
        lat=41.0, lon=29.0, alt=100.0,
        start=dt.datetime(2026, 9, 1, 12, 0, 0),
        duration_s=8,
        sample_rate=fs,
        sample_format="int16",
        engine="native",
        systems=("E",),
        nav_message=True,
        outdir=tmp_path,
    )
    out = engine.run(req)
    raw = np.fromfile(out / "gpssim.bin", dtype=np.int16).astype(np.float32)
    iq = raw[0::2] + 1j * raw[1::2]
    meta = json.loads((out / "meta.json").read_text())
    sv = meta["provenance"]["svs"][0]
    prn = sv["prn"]

    # RF IQ'dan E1-B BOC(1,1) sembollerini demodüle et
    syms = inav_decode.demod_symbols_e1b(iq, fs, prn)
    assert len(syms) >= 1000, f"En az 1000 sembol demodüle edilmeli, bulunan: {len(syms)}"

    # Demodüle sembollerden I/NAV sayfalarını çöz
    msgs = inav_decode.decode_messages(syms)
    assert len(msgs) >= 2, f"En az 2 tam sayfa çözülmeli, bulunan: {len(msgs)}"

    # Çözülen sayfalar arası 500 sembol (2.0 saniye) mesafeyi ve TOW tutarlılığını doğrula
    for i in range(len(msgs) - 1):
        p1, p2 = msgs[i], msgs[i + 1]
        assert p2["sym_idx"] - p1["sym_idx"] == 500, "Sayfalar arası mesafe tam 500 sembol (2 s) olmalı"
        tow1 = p1["fields"].get("tow")
        tow2 = p2["fields"].get("tow")
        if tow1 is not None and tow2 is not None:
            assert tow2 - tow1 == 2.0, "İki ardışık sayfa TOW farkı 2 saniye olmalı"

