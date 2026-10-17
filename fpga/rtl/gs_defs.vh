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
// Packed record layouts (GS_PAR_W, GS_ST_W, GS_SCI_W) are added here by the
// pipe task (Task 2), together with scripts/fpga_vectors.py pack_par/pack_st.
// ---------------------------------------------------------------------------

`endif
