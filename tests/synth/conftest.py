import subprocess
import pathlib
import sys

import pytest

_NATIVE = pathlib.Path(__file__).parent.parent.parent / "backend" / "synth" / "native"
_LIBEXT = {"darwin": "dylib", "win32": "dll"}.get(sys.platform, "so")
_LIB = _NATIVE / f"libgnsssynth.{_LIBEXT}"


@pytest.fixture(scope="session", autouse=True)
def _build_native_lib():
    try:
        subprocess.run(["make", "-C", str(_NATIVE)], check=True, capture_output=True)
    except FileNotFoundError:
        # No `make` on PATH (common on a bare Windows/MinGW setup). If the
        # library was already built by another means, use it as-is; POSIX
        # environments always have make, so this branch never fires there.
        if not _LIB.exists():
            raise
