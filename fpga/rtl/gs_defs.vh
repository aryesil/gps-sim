// gs_defs.vh - global configuration macros for the gnss_synth core.
// Configuration is by `define with `ifndef defaults (no parameters on gs_top).
`ifndef GS_DEFS_VH
`define GS_DEFS_VH

// Number of parallel pipes.
`ifndef GS_P
`define GS_P 12
`endif

// Max channels per core and derived slots per pipe: SLOTS = ceil(64 / GS_P).
`define GS_NCH_MAX 64
`define GS_SLOTS ((`GS_NCH_MAX + `GS_P - 1) / `GS_P)

// Directory of generated memory images (trailing slash included). Overridable
// with -d GS_MEMDIR="path/"; run_xsim.sh passes an absolute path.
`ifndef GS_MEMDIR
`define GS_MEMDIR "fpga/mem/"
`endif

// ---------------------------------------------------------------------------
// Packed record layouts (normative; loader -> pipe, pipe -> scanner).
// PAR (static, written by the loader into the shadow bank), GS_PAR_W = 173:
//   [0] valid  [1] boc  [3:2] cboc (wire encoding 0,1=+1,2=-1)  [5:4] kind
//   [11:6] prn  [43:12] carr_inc  [86:44] code_inc (43 bit)
//   [118:87] sign_bits  [136:119] coef0  [154:137] coef1  [172:155] coef2
// ST (dynamic initial state), GS_ST_W = 148:
//   [31:0] carr_phase  [83:32] cp (code phase, 52 bit)  [135:84] sp_frac
//   [140:136] per (= sign_idx0, since sign_phase0 < code_len<<40)
//   [147:141] cur_widx (= code_phase0 >> 45)
// A loader write (l_we) writes PAR+ST of the shadow entry, clears cur_loaded
// (cur_widx taken from ST) and clears nxt_loaded.
// SCI (scanner info, comb from sc_slot/sc_bank), GS_SCI_W = 25:
//   [0] valid  [2:1] kind  [8:3] prn  [9] cur_loaded  [16:10] cur_widx
//   [23:17] nxt_widx  [24] nxt_loaded
// ---------------------------------------------------------------------------
`define GS_PAR_W 173
`define GS_ST_W  148
`define GS_SCI_W 25

`endif
