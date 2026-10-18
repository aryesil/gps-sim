# ooc.tcl - out-of-context synthesis (and optional PAR) of gs_top.
# Usage: vivado -mode batch -source ooc.tcl -tclargs <repo_root> [period_ns] [par:0|1|auto]
set root   [lindex $argv 0]
set period [expr {[llength $argv] > 1 ? [lindex $argv 1] : 4.0}]
set par    [expr {[llength $argv] > 2 ? [lindex $argv 2] : "auto"}]
set rep    $root/fpga/synth/reports
file mkdir $rep
set memdir $root/fpga/mem/

read_verilog [glob $root/fpga/rtl/gs_*.v]
synth_design -mode out_of_context -top gs_top -part xc7z020clg400-2 \
    -flatten_hierarchy rebuilt -include_dirs $root/fpga/rtl \
    -verilog_define "GS_MEMDIR=\"$memdir\""
create_clock -name clk -period $period [get_ports clk]
write_checkpoint -force $rep/synth.dcp
report_utilization -hierarchical -hierarchical_depth 3 -file $rep/synth_util_hier.rpt
report_utilization -file $rep/synth_util.rpt
report_timing_summary -max_paths 10 -file $rep/synth_timing.rpt
report_timing -max_paths 10 -nworst 1 -unique_pins -file $rep/synth_paths.rpt
set wns [get_property SLACK [get_timing_paths -max_paths 1 -setup]]
puts "SYNTH_WNS $wns at period $period"

if {$par eq "auto"} { set par [expr {$wns > -1.0 ? 1 : 0}] }
if {$par} {
    opt_design
    place_design
    phys_opt_design
    route_design
    report_utilization -hierarchical -hierarchical_depth 3 -file $rep/route_util_hier.rpt
    report_timing_summary -max_paths 10 -file $rep/route_timing.rpt
    report_timing -max_paths 10 -nworst 1 -unique_pins -file $rep/route_paths.rpt
    write_checkpoint -force $rep/route.dcp
    puts "ROUTE_WNS [get_property SLACK [get_timing_paths -max_paths 1 -setup]] at period $period"
}
