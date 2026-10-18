// gs_top.v - gnss_synth core integration (no parameters; GS_P by `define).
//
// cmd stream -> gs_loader (FIFO, parser, shadow-bank writer) -> GS_P x gs_pipe
// (per-bank RAMs, datapath) <- gs_scan (code ROM, chip-word fetch);
// gs_seq issues T slots/sample and swaps banks; pipe sums -> gs_reduce ->
// out_valid/out_i/out_q.
//
// Latency (clocks), sample start = cycle of s_first (decision clock + 1):
//   out_valid is high  (T-1) + LAT_PIPE(7) + LAT_REDUCE  clocks after s_first,
//   i.e. 18 clocks for P=12 and T=4 (LAT_REDUCE = 8); (T-1) is the issue of the
//   last slot.  In-flight depth = ceil(that / T) + 1 samples: a sink whose
//   out_space means ">= 16 free entries" is therefore always sufficient (the
//   sequencer samples out_space in the decision clock only; it never needs to
//   be stable afterwards).
// cfg_shift is applied to gs_reduce only while not running (static during
// operation); values > 7 are clamped to 7.  st_* are registered copies.
`include "gs_defs.vh"

module gs_top (
    input  wire        clk,
    input  wire        rst,
    input  wire        enable,
    input  wire        cmd_valid,
    input  wire [31:0] cmd_data,
    output wire        cmd_ready,
    input  wire        out_space,
    output wire        out_valid,
    output wire [15:0] out_i,
    output wire [15:0] out_q,
    output reg  [63:0] st_play_idx,
    output reg  [31:0] st_underruns,
    output reg  [31:0] st_dropped_late,
    output reg  [31:0] st_clipped,
    output reg  [31:0] st_chip_late,
    output reg  [31:0] st_bad_words,
    output reg         st_running,
    output reg  [15:0] st_cmd_level
);
    localparam P = `GS_P;

    // ---- loader
    wire [63:0] target_idx;
    wire        bank, shadow_bank, load_enable;
    wire        l_we, l_bank, shadow_dirty, shadow_loaded;
    wire  [3:0] l_pipe;
    wire  [2:0] l_slot;
    wire [`GS_PAR_W-1:0] l_par;
    wire [`GS_ST_W-1:0]  l_st;
    wire        cfg_valid;
    wire  [6:0] cfg_nch;
    wire [31:0] cfg_fs, cfg_shift, l_dropped, l_bad;
    wire [63:0] cfg_start;
    wire [15:0] fifo_level;

    gs_loader u_loader (.clk(clk), .rst(rst),
        .cmd_valid(cmd_valid), .cmd_data(cmd_data), .cmd_ready(cmd_ready),
        .target_idx(target_idx), .shadow_bank(shadow_bank), .load_enable(load_enable),
        .l_we(l_we), .l_pipe(l_pipe), .l_slot(l_slot), .l_bank(l_bank),
        .l_par(l_par), .l_st(l_st),
        .shadow_dirty(shadow_dirty), .shadow_loaded(shadow_loaded),
        .cfg_valid(cfg_valid), .cfg_nch(cfg_nch), .cfg_fs(cfg_fs),
        .cfg_shift(cfg_shift), .cfg_start(cfg_start),
        .dropped_late(l_dropped), .bad_words(l_bad), .fifo_level(fifo_level));

    // ---- sequencer
    wire        s_valid, s_bank, s_first, s_last, s_mute, running, shadow_clean;
    wire  [2:0] s_slot;
    wire [63:0] play_idx;
    wire [31:0] underruns;

    gs_seq u_seq (.clk(clk), .rst(rst), .enable(enable),
        .cfg_valid(cfg_valid), .cfg_nch(cfg_nch), .cfg_start(cfg_start),
        .shadow_loaded(shadow_loaded), .shadow_clean(shadow_clean),
        .out_space(out_space),
        .target_idx(target_idx), .bank(bank), .shadow_bank(shadow_bank),
        .load_enable(load_enable),
        .s_valid(s_valid), .s_slot(s_slot), .s_bank(s_bank),
        .s_first(s_first), .s_last(s_last), .s_mute(s_mute),
        .play_idx(play_idx), .underruns(underruns), .running(running));

    // ---- scanner
    wire  [3:0] sc_pipe, sc_wpipe;
    wire  [`GS_SCAN_SW-1:0] sc_slot, sc_wslot;
    wire [P*`GS_SCAN_SW-1:0] sc_slot_p;
    wire [P-1:0] sc_bank_p;
    wire        sc_bank, sc_we, sc_wbank, sc_wwhich;
    wire [31:0] sc_wword;
    wire  [6:0] sc_wwidx;
    wire [P*`GS_SCI_W-1:0] sc_info_all;

    gs_scan u_scan (.clk(clk), .rst(rst),
        .sc_pipe(sc_pipe), .sc_slot(sc_slot), .sc_bank(sc_bank),
        .sc_slot_p(sc_slot_p), .sc_bank_p(sc_bank_p),
        .sc_info_all(sc_info_all),
        .sc_we(sc_we), .sc_wpipe(sc_wpipe), .sc_wslot(sc_wslot), .sc_wbank(sc_wbank),
        .sc_wwhich(sc_wwhich), .sc_wword(sc_wword), .sc_wwidx(sc_wwidx),
        .shadow_bank(shadow_bank), .shadow_dirty(shadow_dirty),
        .shadow_clean(shadow_clean));

    // ---- pipes
    wire [P-1:0]    p_valid, p_late;
    wire [P*38-1:0] p_i, p_q;
    genvar g;
    generate
        for (g = 0; g < P; g = g + 1) begin : g_pipe
            wire signed [37:0] oi, oq;
            gs_pipe #(.PIDX(g)) u_pipe (.clk(clk), .rst(rst),
                .s_valid(s_valid), .s_slot(s_slot), .s_bank(s_bank),
                .s_first(s_first), .s_last(s_last), .s_mute(s_mute),
                .l_we(l_we), .l_pipe(l_pipe), .l_slot(l_slot), .l_bank(l_bank),
                .l_par(l_par), .l_st(l_st),
                .sc_slot(sc_slot_p[g*`GS_SCAN_SW +: `GS_SCAN_SW]), .sc_bank(sc_bank_p[g]),
                .sc_info(sc_info_all[g*`GS_SCI_W +: `GS_SCI_W]),
                .sc_we(sc_we), .sc_wpipe(sc_wpipe), .sc_wslot(sc_wslot),
                .sc_wbank(sc_wbank), .sc_wwhich(sc_wwhich),
                .sc_wword(sc_wword), .sc_wwidx(sc_wwidx),
                .o_valid(p_valid[g]), .o_i(oi), .o_q(oq), .o_chip_late(p_late[g]));
            assign p_i[g*38 +: 38] = oi;
            assign p_q[g*38 +: 38] = oq;
        end
    endgenerate

    // ---- shift: applied only while stopped
    reg [7:0] shift_r;
    always @(posedge clk) begin
        if (rst) shift_r <= 8'd0;
        else if (!running && cfg_valid)
            shift_r <= (cfg_shift > 32'd7) ? 8'd7 : cfg_shift[7:0];
    end

    // ---- reduce / output
    wire [1:0] clipped_inc;
    gs_reduce u_reduce (.clk(clk), .rst(rst), .in_valid(p_valid[0]),
        .in_i_all(p_i), .in_q_all(p_q), .shift(shift_r),
        .out_valid(out_valid), .out_i(out_i), .out_q(out_q),
        .clipped_inc(clipped_inc));

    // ---- status
    always @(posedge clk) begin
        if (rst) begin
            st_play_idx <= 0; st_underruns <= 0; st_dropped_late <= 0;
            st_clipped <= 0; st_chip_late <= 0; st_bad_words <= 0;
            st_running <= 1'b0; st_cmd_level <= 0;
        end else begin
            st_play_idx     <= play_idx;
            st_underruns    <= underruns;
            st_dropped_late <= l_dropped;
            st_bad_words    <= l_bad;
            st_running      <= running;
            st_cmd_level    <= fifo_level;
            st_clipped      <= st_clipped + clipped_inc;
            if (|p_late) st_chip_late <= st_chip_late + 1'b1;
        end
    end
endmodule
