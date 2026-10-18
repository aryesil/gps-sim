#!/usr/bin/env bash
# Usage: fpga/sim/run_xsim.sh <tb_name> [-d NAME=VAL ...]
# Compiles fpga/rtl/*.v + fpga/tb/<tb_name>.v with Vivado xsim, runs it and
# exits non-zero unless the last output line is PASS. Run from the repo root
# (or anywhere: paths are resolved from this script's location).
# Env: VIVADO_BIN overrides the Vivado bin dir; GS_MEMDIR_ABS the mem dir;
# GS_RTL="gs_a.v gs_b.v" restricts the compiled rtl files (default: all gs_*.v).
# Defines (-d NAME=VAL) go through an xvlog -f args file: the xvlog.bat wrapper
# splits command-line arguments at '='.
set -u
tb=${1:?usage: run_xsim.sh <tb_name> [-d NAME=VAL ...]}; shift
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
root=$(cd "$here/../.." && pwd)
VIVADO_BIN=${VIVADO_BIN:-/d/Programs/Xiliinx/Vivado/2023.2/bin}
# Windows: .bat wrappers; native paths (D:/...) for the tools.
if [ -f "$VIVADO_BIN/xvlog.bat" ]; then ext=.bat; else ext=; fi
win() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else echo "$1"; fi; }

work="${GS_WORK:-$root/fpga/sim/work/$tb}"
rm -rf "$work"; mkdir -p "$work"
memdir=$(win "${GS_MEMDIR_ABS:-$root/fpga/mem}")/
argf="$work/xvlog.args"
: >"$argf"
while [ $# -gt 0 ]; do
  if [ "$1" = "-d" ]; then echo "-d $2" >>"$argf"; shift 2; else echo "$1" >>"$argf"; shift; fi
done

rtl=()
if [ -n "${GS_RTL:-}" ]; then
  for n in $GS_RTL; do rtl+=("$(win "$root/fpga/rtl/$n")"); done
else
  for f in "$root"/fpga/rtl/gs_*.v; do [ -e "$f" ] && rtl+=("$(win "$f")"); done
fi
tbf=$(win "$root/fpga/tb/$tb.v")
inc=$(win "$root/fpga/rtl")

cd "$work" || exit 2
log=run.log
{
  "$VIVADO_BIN/xvlog$ext" -i "$inc" -d "GS_MEMDIR=\"$memdir\"" -f "$(win "$argf")" "${rtl[@]}" "$tbf" &&
  "$VIVADO_BIN/xelab$ext" --debug off --timescale 1ns/1ps -s snap "work.$tb" &&
  "$VIVADO_BIN/xsim$ext" snap -R
} >"$log" 2>&1
rc=$?
if [ $rc -ne 0 ]; then
  grep -E "ERROR|Error" "$log" | head -20; echo "FAIL tool_error(rc=$rc), see $log"; exit 1
fi
last=$(grep -E '^(PASS|FAIL)' "$log" | tail -1)
grep -vE '^(INFO|Time resolution|\s*$)' "$log" | grep -E 'mismatch|WARNING: \[XSIM' | head -15
echo "${last:-FAIL no_result}"
[ "$last" = PASS ]
