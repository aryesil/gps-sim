"""Structural / statistical IQ integrity checks."""
import numpy as np
import pytest

from backend.models import impairments
from backend.analysis import iq_integrity


def test_clean_synth_iq_passes(synth_iq):
    inter, meta = synth_iq(duration_s=0.05, sample_format="int16")
    iq = (inter[0::2] + 1j * inter[1::2]).astype(np.complex64)
    rep = iq_integrity.validate_array(iq, 2.6e6, sample_format="int16")
    assert rep["ok"], rep["problems"]
    assert abs(rep["iq_power_ratio_db"]) < 1.0
    assert abs(rep["iq_correlation"]) < 0.2


def test_file_roundtrip_and_size_check(tmp_path, synth_iq):
    inter, meta = synth_iq(duration_s=0.04, sample_format="int16")
    p = tmp_path / "clean.bin"
    inter.tofile(p)
    rep = iq_integrity.validate_file(p, "int16", 2.6e6, expected_duration_s=0.04)
    assert rep["ok"], rep["problems"]
    assert rep["n_samples"] == meta["n"]

    # truncate by one byte -> not a whole number of I/Q pairs
    bad = tmp_path / "bad.bin"
    bad.write_bytes(p.read_bytes()[:-1])
    rep2 = iq_integrity.validate_file(bad, "int16", 2.6e6)
    assert not rep2["ok"]
    assert "whole number of I/Q pairs" in rep2["problems"][0]


def test_detects_dc_offset(synth_iq):
    inter, _ = synth_iq(duration_s=0.03, quantize=False)
    iq = np.asarray(inter) + (500.0 + 0j)
    rep = iq_integrity.validate_array(iq, 2.6e6, sample_format="int16")
    assert not rep["ok"]
    assert any("DC offset" in m for m in rep["problems"])


def test_detects_clipping(synth_iq):
    inter, _ = synth_iq(duration_s=0.03, quantize=False)
    iq = np.asarray(inter).astype(np.complex64)
    iq, _ = impairments.apply(iq, 2.6e6, impairments.ImpairmentConfig(
        enabled_flag=True, clip_fraction=0.2))
    # rescale so "full scale" is meaningful, then push most samples past it
    iq = iq / np.max(np.abs(iq)) * 40000.0
    rep = iq_integrity.validate_array(iq, 2.6e6, sample_format="int16")
    assert not rep["ok"]
    assert any("full scale" in m for m in rep["problems"])


def test_detects_nonfinite():
    iq = np.ones(1000, dtype=np.complex64)
    iq[10] = np.nan
    rep = iq_integrity.validate_array(iq, 2.6e6)
    assert not rep["ok"]
    assert any("non-finite" in m for m in rep["problems"])


def test_detects_iq_imbalance(synth_iq):
    inter, _ = synth_iq(duration_s=0.03, quantize=False)
    iq = np.asarray(inter).astype(np.complex64)
    iq, _ = impairments.apply(iq, 2.6e6, impairments.ImpairmentConfig(
        enabled_flag=True, iq_gain_db=3.0, iq_phase_deg=15.0))
    rep = iq_integrity.validate_array(iq, 2.6e6, sample_format="int16")
    assert not rep["ok"]
    assert any("I/Q" in m for m in rep["problems"])


def test_empty_is_flagged():
    rep = iq_integrity.validate_array(np.array([], dtype=np.complex64), 2.6e6)
    assert not rep["ok"] and "empty IQ" in rep["problems"]
