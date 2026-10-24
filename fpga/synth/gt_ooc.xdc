# gt_ooc.xdc - clocks and crossing constraints for gpsdo_tb (out-of-context run and a
# template for the system build, where clk_sys/clk_div come from the BD clocks).
create_clock -name clk_sys -period 10.000 [get_ports clk_sys]
create_clock -name clk_div -period 16.000 [get_ports clk_div]
create_clock -name clk40   -period 25.000 [get_ports clk40]
# tb_clk (200 MHz) is generated from clk40 by the MMCM in gt_clk.
# Crossings between the three domains are 2-FF synchronisers, toggle handshakes or
# hold registers that are stable for many cycles; bound the data path delay.
set tb [get_clocks -of_objects [get_pins -hier -filter {NAME =~ *u_bufg/O}]]
set_max_delay -datapath_only -from [get_clocks clk_sys] -to $tb 5.000
set_max_delay -datapath_only -from $tb -to [get_clocks clk_sys] 5.000
set_max_delay -datapath_only -from [get_clocks clk_div] -to $tb 5.000
set_max_delay -datapath_only -from $tb -to [get_clocks clk_div] 5.000
set_max_delay -datapath_only -from [get_clocks clk_sys] -to [get_clocks clk_div] 5.000
set_max_delay -datapath_only -from [get_clocks clk_div] -to [get_clocks clk_sys] 5.000
# PPS_IN is asynchronous to everything
set_false_path -from [get_ports pps_in]
