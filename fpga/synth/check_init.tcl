# check_init.tcl - synthesize gs_top out-of-context and dump every RAMB18E1/RAMB36E1
# (INIT_xx / INITP_xx, port widths, address / data / enable pin nets) plus LUT-RAM
# cells to <out_dir>/bram_dump.txt, for scripts/fpga_check_init.py.
# Usage: vivado -mode batch -source check_init.tcl -tclargs <rtl_dir> <mem_dir> <out_dir>
#   rtl_dir: dir with gs_*.v / gs_defs.vh;  mem_dir: dir the RTL $readmemh's from
# Same synth_design options as ooc.tcl.  gs_wrap/gs_axil/gs_afifo are not part of
# gs_top (they are wrappers still under development) and are not read.
set rtl [lindex $argv 0]
set mem [string trimright [lindex $argv 1] /]/
set out [lindex $argv 2]
file mkdir $out

set files {}
foreach f [lsort [glob $rtl/gs_*.v]] {
    if {[lsearch -exact {gs_wrap.v gs_axil.v gs_afifo.v} [file tail $f]] >= 0} continue
    lappend files $f
}
read_verilog $files
synth_design -mode out_of_context -top gs_top -part xc7z020clg400-2 \
    -flatten_hierarchy rebuilt -include_dirs $rtl \
    -verilog_define "GS_MEMDIR=\"$mem\""

set f [open $out/bram_dump.txt w]
proc pin_net {c pn} {
    set p [get_pins -quiet -of_objects $c -filter "REF_PIN_NAME == $pn"]
    if {$p eq ""} { return "-" }
    set n [get_nets -quiet -of_objects $p]
    if {$n eq ""} { return "-" }
    return [get_property NAME $n]
}
foreach c [lsort [get_cells -hierarchical -filter {REF_NAME == RAMB36E1 || REF_NAME == RAMB18E1}]] {
    set ref [get_property REF_NAME $c]
    puts $f "CELL [get_property NAME $c] $ref [get_property READ_WIDTH_A $c] [get_property READ_WIDTH_B $c] [get_property RAM_MODE $c]"
    set ni [expr {$ref eq "RAMB36E1" ? 128 : 64}]
    set np [expr {$ref eq "RAMB36E1" ? 16 : 8}]
    for {set i 0} {$i < $ni} {incr i} { puts $f [format "INIT_%02X %s" $i [get_property [format INIT_%02X $i] $c]] }
    for {set i 0} {$i < $np} {incr i} { puts $f [format "INITP_%02X %s" $i [get_property [format INITP_%02X $i] $c]] }
    foreach p [get_pins -of_objects $c] {
        set pn [get_property REF_PIN_NAME $p]
        if {![regexp {^(ADDRARDADDR|ADDRBWRADDR|DOADO|DOBDO|DOPADOP|DOPBDOP|ENARDEN|ENBWREN|WEA|WEBWE|REGCEAREGCE|REGCEB)} $pn]} continue
        set d [get_property DIRECTION $p]
        set n [get_nets -quiet -of_objects $p]
        if {$n eq ""} { puts $f "PIN $pn -" } else { puts $f "PIN $pn [get_property NAME $n]" }
    }
    puts $f "END"
}
# distributed RAM / ROM primitives (LUT based memories)
foreach c [lsort [get_cells -hierarchical -filter {REF_NAME =~ RAM32* || REF_NAME =~ RAM64* || REF_NAME =~ RAM128* || REF_NAME =~ RAM256* || REF_NAME =~ ROM*}]] {
    set init ""
    catch { set init [get_property INIT $c] }
    puts $f "LUTRAM [get_property NAME $c] [get_property REF_NAME $c] $init"
}

# Combinational cone behind the registered code-ROM address: rom_addr <= base_q + widx.
# Vivado re-expresses this adder on the base BRAM outputs (carry chain), so the base
# table is verified functionally from the netlist: dump LUT/CARRY4 cells back to
# the first flop / BRAM output / constant.
set roots [get_cells -quiet -hierarchical -filter {NAME =~ u_scan/rom_addr_reg[*] && REF_NAME == FDRE}]
set seen [dict create]
set queue {}
foreach r $roots {
    puts $f "CONE_ROOT [get_property NAME $r]"
    lappend queue $r
}
set first 1
while {[llength $queue]} {
    set c [lindex $queue 0]; set queue [lrange $queue 1 end]
    set cn [get_property NAME $c]
    if {[dict exists $seen $cn]} continue
    dict set seen $cn 1
    set ref [get_property REF_NAME $c]
    set init "-"
    if {[string match LUT* $ref]} { set init [get_property INIT $c] }
    puts $f "CONE_CELL $cn $ref $init"
    foreach ip [get_pins -of_objects $c -filter {DIRECTION == IN}] {
        set pn [get_property REF_PIN_NAME $ip]
        if {[string match FD* $ref] && $pn ne "D"} continue
        set n [get_nets -quiet -of_objects $ip]
        if {$n eq ""} { puts $f "CONE_IN $cn $pn - -"; continue }
        set d [get_pins -quiet -of_objects $n -filter {DIRECTION == OUT}]
        if {$d eq ""} { puts $f "CONE_IN $cn $pn PORT [get_property NAME $n]"; continue }
        set dc [get_cells -of_objects $d]
        set dref [get_property REF_NAME $dc]
        puts $f "CONE_IN $cn $pn [get_property NAME $dc] [get_property REF_PIN_NAME $d] $dref"
        if {[string match LUT* $dref] || $dref eq "CARRY4"} { lappend queue $dc }
        if {![string match LUT* $dref] && $dref ne "CARRY4" && $dref ne "GND" && $dref ne "VCC" && ![string match FD* $dref] && ![string match RAMB* $dref]} {
            puts $f "CONE_UNSUPPORTED $dref [get_property NAME $dc]"
        }
    }
}
close $f
puts "CHECK_INIT_DUMP_DONE $out/bram_dump.txt"
