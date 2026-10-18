#!/usr/bin/env bash
# Usage: fpga/synth/check_init.sh [--root DIR] [--ref DIR]
#   --root DIR  tree to synthesize (default: this repo; needs DIR/fpga/rtl and DIR/fpga/mem)
#   --ref DIR   tree holding the reference images (default: this repo, fpga/mem)
# Synthesizes gs_top out-of-context, dumps all RAMB INIT/INITP and verifies the
# ROM / base / carrier tables bit-exactly against fpga/mem/*.mem.
# Exit 0 = PASS, non-zero = FAIL.  Work files: fpga/synth/reports/initcheck/ (or
# <root>/fpga/synth/reports/initcheck for --root).
set -u
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo=$(cd "$here/../.." && pwd)
root=$repo; ref=$repo
while [ $# -gt 0 ]; do
  case "$1" in
    --root) root=$(cd "$2" && pwd); shift 2;;
    --ref)  ref=$(cd "$2" && pwd); shift 2;;
    *) echo "unknown arg $1"; exit 2;;
  esac
done
VIVADO_BIN=${VIVADO_BIN:-/d/Programs/Xiliinx/Vivado/2023.2/bin}
if [ -f "$VIVADO_BIN/vivado.bat" ]; then viv="$VIVADO_BIN/vivado.bat"; else viv="$VIVADO_BIN/vivado"; fi
win() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else echo "$1"; fi; }
out="$root/fpga/synth/reports/initcheck"
mkdir -p "$out"
cd "$out" || exit 2          # Vivado droppings (.Xil, clockInfo.txt) stay here
rm -f bram_dump.txt
t0=$(date +%s)
for try in 1 2 3; do
  "$viv" -mode batch -source "$(win "$here/check_init.tcl")" -nojournal -log "$(win "$out/synth.log")" \
      -tclargs "$(win "$root/fpga/rtl")" "$(win "$root/fpga/mem")" "$(win "$out")"
  if grep -q "couldn't read file" "$out/synth.log" || ! grep -q CHECK_INIT_DUMP_DONE "$out/synth.log"; then
    echo "check_init.sh: transient Vivado error / no dump, retry $try"
  else
    break
  fi
done
t1=$(date +%s)
[ -s "$out/bram_dump.txt" ] || { echo "FAIL: synthesis produced no dump (see $out/synth.log)"; exit 3; }
echo "synthesis+dump wall time: $((t1-t0)) s"
py=python; command -v python >/dev/null 2>&1 || py=python3
"$py" "$repo/scripts/fpga_check_init.py" "$out/bram_dump.txt" "$ref/fpga/mem"
exit $?
