"""Shared test helper: wrap a Python fixture script as a directly
executable launcher, for tests that stand in a fake gps-sdr-sim binary.
subprocess.run([binary, ...]) (no shell=True) can launch a .bat/.cmd
directly on Windows but not a POSIX shebang script -- give it a batch-file
launcher there, and a chmod+x shebang script everywhere else. Was
copy-pasted near-identically across four test files before being factored
out here.
"""
from __future__ import annotations

import pathlib
import stat
import sys


def launcher_for(py_path: pathlib.Path) -> str:
    """``py_path``: an already-written Python fixture script. Returns the
    str path of an executable launcher for it, alongside it in the same
    directory."""
    stem = py_path.stem
    tmp_path = py_path.parent
    if sys.platform == "win32":
        sh = tmp_path / f"{stem}.bat"
        sh.write_text(f'@"{sys.executable}" "{py_path}" %*\r\n')
        return str(sh)
    sh = tmp_path / stem
    sh.write_text(f'#!/usr/bin/env bash\nexec python "{py_path}" "$@"\n')
    sh.chmod(sh.stat().st_mode | stat.S_IEXEC)
    return str(sh)
