#!/usr/bin/env bash
# Usage: fpga/synth/ooc.sh [period_ns=4.0] [par=auto|0|1]
# Out-of-context synthesis of gs_top (xc7z020clg400-2); reports in fpga/synth/reports/.
set -u
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
root=$(cd "$here/../.." && pwd)
VIVADO_BIN=${VIVADO_BIN:-/d/Programs/Xiliinx/Vivado/2023.2/bin}
if [ -f "$VIVADO_BIN/vivado.bat" ]; then viv="$VIVADO_BIN/vivado.bat"; else viv="$VIVADO_BIN/vivado"; fi
win() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else echo "$1"; fi; }
mkdir -p "$here/reports"
cd "$here/reports" || exit 2     # Vivado droppings (.Xil, clockInfo.txt) stay here (gitignored)
# Vivado on Windows occasionally fails to read its own script files ("couldn't read
# file ...", seen with another Vivado instance open): retry up to 3 times.
for try in 1 2 3; do
  "$viv" -mode batch -source "$(win "$here/ooc.tcl")" -nojournal       -log "$(win "$here/reports/ooc.log")" -tclargs "$(win "$root")" "${1:-4.0}" "${2:-auto}"
  rc=$?
  grep -q "couldn't read file" "$here/reports/ooc.log" || break
  echo "ooc.sh: transient Vivado file-read error, retry $try"
done
exit $rc
