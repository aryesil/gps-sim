import pytest

from backend.rf.frontend import rf_frontend as rff


def _cfg(**kw):
    base = dict(target_rf_frequency_hz=1_575_420_000, tx_sample_rate_hz=2_600_000)
    base.update(kw)
    return rff.RFFrontendConfig(**base)


def test_disabled_passes_target_through_unchanged():
    result = rff.plan(_cfg(lo_offset_mode="DISABLED"))
    assert result.tx_lo_hz == 1_575_420_000
    assert result.baseband_offset_hz == 0
    assert result.target_rf_hz == 1_575_420_000


def test_manual_negative_lo_offset_worked_example():
    result = rff.plan(_cfg(lo_offset_mode="MANUAL", lo_offset_hz=-1_000_000))
    assert result.tx_lo_hz == 1_574_420_000
    assert result.baseband_offset_hz == 1_000_000
    assert result.target_rf_hz == 1_575_420_000


def test_manual_negative_lo_offset_second_worked_example():
    result = rff.plan(rff.RFFrontendConfig(
        target_rf_frequency_hz=445_400_000, lo_offset_mode="MANUAL",
        lo_offset_hz=-1_000_000, tx_sample_rate_hz=2_600_000))
    assert result.tx_lo_hz == 444_400_000
    assert result.baseband_offset_hz == 1_000_000
    assert result.target_rf_hz == 445_400_000


def test_manual_positive_lo_offset():
    result = rff.plan(_cfg(lo_offset_mode="MANUAL", lo_offset_hz=1_000_000))
    assert result.tx_lo_hz == 1_576_420_000
    assert result.baseband_offset_hz == -1_000_000


def test_manual_zero_offset_matches_disabled():
    result = rff.plan(_cfg(lo_offset_mode="MANUAL", lo_offset_hz=0))
    assert result.tx_lo_hz == 1_575_420_000
    assert result.baseband_offset_hz == 0


def test_every_result_satisfies_the_target_invariant():
    for mode, offset in (("DISABLED", 0), ("MANUAL", -1_000_000), ("MANUAL", 1_000_000)):
        result = rff.plan(_cfg(lo_offset_mode=mode, lo_offset_hz=offset))
        assert result.tx_lo_hz + result.baseband_offset_hz == result.target_rf_hz


def test_manual_offset_exceeding_nyquist_rejected():
    with pytest.raises(rff.RFFrontendError, match="Nyquist"):
        rff.plan(_cfg(lo_offset_mode="MANUAL", lo_offset_hz=-1_400_000,
                       tx_sample_rate_hz=2_500_000))


def test_manual_offset_within_nyquist_but_over_rf_bandwidth_rejected():
    with pytest.raises(rff.RFFrontendError, match="bandwidth"):
        rff.plan(_cfg(lo_offset_mode="MANUAL", lo_offset_hz=-1_000_000,
                       tx_sample_rate_hz=2_600_000, tx_rf_bandwidth_hz=2_200_000))


def test_auto_mode_picks_a_validated_offset():
    result = rff.plan(_cfg(lo_offset_mode="AUTO"))
    assert abs(result.baseband_offset_hz) == 1_000_000
    assert result.tx_lo_hz + result.baseband_offset_hz == result.target_rf_hz


def test_auto_mode_rejects_when_sample_rate_too_low_for_any_candidate():
    with pytest.raises(rff.RFFrontendError, match="AUTO"):
        rff.plan(_cfg(lo_offset_mode="AUTO", tx_sample_rate_hz=1_500_000))


def test_unknown_mode_rejected():
    with pytest.raises(rff.RFFrontendError, match="lo_offset_mode"):
        rff.plan(_cfg(lo_offset_mode="BOGUS"))


def test_missing_sample_rate_rejected_for_manual():
    with pytest.raises(rff.RFFrontendError, match="sample_rate"):
        rff.plan(_cfg(lo_offset_mode="MANUAL", lo_offset_hz=1_000, tx_sample_rate_hz=0))
