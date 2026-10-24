# gt_ooc.tcl - out-of-context synthesis and PAR of gpsdo_tb.
# Usage: vivado -mode batch -source gt_ooc.tcl -tclargs <repo_root> [lite:0|1] [outdir]
set root [lindex $argv 0]
set rep  [expr {[llength $argv] > 2 ? [lindex $argv 2] : "$root/fpga/synth/reports/gt"}]
file mkdir $rep
read_verilog [list $root/fpga/rtl/gpsdo_tb.v $root/fpga/rtl/gt_axil.v $root/fpga/rtl/gt_clk.v]
synth_design -mode out_of_context -top gpsdo_tb -part xc7z020clg400-2 -flatten_hierarchy rebuilt
read_xdc -mode out_of_context $root/fpga/synth/gt_ooc.xdc
write_checkpoint -force $rep/synth.dcp
report_utilization -file $rep/synth_util.rpt
opt_design
place_design
phys_opt_design
route_design
report_utilization -file $rep/route_util.rpt
report_timing_summary -max_paths 10 -file $rep/route_timing.rpt
report_clock_interaction -file $rep/clock_interaction.rpt
report_cdc -file $rep/cdc.rpt
write_checkpoint -force $rep/route.dcp
puts "GT_ROUTE_WNS [get_property SLACK [get_timing_paths -max_paths 1 -setup]] WHS [get_property SLACK [get_timing_paths -max_paths 1 -hold]]"
