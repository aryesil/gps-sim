# paths.tcl - summarize worst setup paths of a checkpoint, grouped by start/end cell class.
# vivado -mode batch -source paths.tcl -tclargs <dcp> <out.txt> [n]
set dcp [lindex $argv 0]; set out [lindex $argv 1]
set n [expr {[llength $argv] > 2 ? [lindex $argv 2] : 3000}]
open_checkpoint $dcp
set f [open $out w]
set seen {}
foreach p [get_timing_paths -max_paths $n -nworst 1 -unique_pins -setup] {
    set s [get_property STARTPOINT_PIN $p]; set e [get_property ENDPOINT_PIN $p]
    regsub -all {\[[0-9]+\]} $s {[*]} s2; regsub -all {\[[0-9]+\]} $e {[*]} e2
    regsub -all {_[0-9]+/} $s2 {_N/} s2; regsub -all {_[0-9]+/} $e2 {_N/} e2
    set key "$s2 -> $e2"
    if {[dict exists $seen $key]} continue
    dict set seen $key 1
    puts $f [format "%7.3f lv=%2d %s" [get_property SLACK $p] [get_property LOGIC_LEVELS $p] $key]
}
close $f
