# gui_show.tcl - open the routed out-of-context gs_top checkpoint in the Vivado GUI and
# set up the views that show the timing work: hierarchy/utilization, the worst path,
# and the schematic of one pipe's datapath.
#   vivado -mode gui -source fpga/synth/gui_show.tcl
set dcp "D:/codes/gps-sim/fpga/synth/reports/route.dcp"
open_checkpoint $dcp

# 1. Hierarchical utilization (loader / seq / scan / 12 pipes / reduce).
report_utilization -hierarchical -hierarchical_depth 2 -name util_hier

# 2. Worst setup paths (4.0 ns clock): shows the pipelined stages and logic levels.
report_timing -max_paths 5 -nworst 1 -delay_type max -name worst_paths

# 3. Pipe 0 datapath schematic: DSP48 multiplier stages, registered broadcasts and
#    the dont_touch control registers added to cut fanout.
set pipe [get_cells -hier -filter {NAME =~ *u_pipe[0].u_pipe}]
if {[llength $pipe] == 0} { set pipe [lindex [get_cells -hier -filter {NAME =~ *u_pipe*}] 0] }
select_objects $pipe
show_schematic $pipe

# 4. Highlight the DSP48 multipliers, block RAMs and distributed RAMs of pipe 0.
highlight_objects -color yellow [get_cells -hier -filter {REF_NAME == DSP48E1 && NAME =~ *u_pipe[0]*}]
highlight_objects -color cyan   [get_cells -hier -filter {PRIMITIVE_GROUP == BLOCKRAM}]
highlight_objects -color green  [get_cells -hier -filter {PRIMITIVE_SUBGROUP == dram && NAME =~ *u_pipe[0]*}]
