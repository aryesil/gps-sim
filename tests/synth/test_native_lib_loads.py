import importlib
import pathlib

import pytest

from backend.synth import _lib


def test_missing_lib_raises_explicit(monkeypatch):
    monkeypatch.setattr(_lib, "_CACHED", None, raising=False)
    monkeypatch.setattr(_lib, "LIB_PATH", pathlib.Path("/nonexistent/libgnsssynth.dylib"))
    with pytest.raises(_lib.NativeEngineUnavailable) as ei:
        _lib.load_lib()
    assert "make -C backend/synth/native" in str(ei.value)


def test_lib_builds_and_loads():
    # Requires `make -C backend/synth/native` to have run (CI build step / conftest).
    lib = _lib.load_lib()
    assert lib.synth_abi_version() == _lib.ABI_VERSION
