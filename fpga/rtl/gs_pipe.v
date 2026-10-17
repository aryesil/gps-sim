// gs_pipe.v - one channel pipe of the gnss_synth core: per-bank parameter/state
// RAMs (distributed RAM, async read), the sample datapath and the slot
// accumulator.
//
// LAT_PIPE = 6 clocks: if the last slot of a sample is issued in cycle L
// (s_valid && s_last), o_valid is high in cycle L+6 with o_i/o_q the sum over
// the slots of that sample (38-bit signed).  o_chip_late is a one-clock pulse
// 3 clocks after the issue of the offending slot (cycle t+4), independent of
// the o_valid timing.  Constant for every pipe.
//
// Timeline for a slot issued in cycle t (inputs s_* presented in cycle t):
//   t   async reads of PAR/ST/CUR of (s_bank, s_slot) -> stage-1 registers
//   t+1 carrier index (-> gs_sincos, output valid in t+4), phase/code/sign
//       phase increments, chip bit, period-sign bit, S11/S61 sub-carrier bits
//   t+2 code-phase wrap subtraction (cp+inc-lim, sp+inc-lim), coefficient
//       select/negate -> a (19 bit signed)
//   t+3 wrap select, ST write (and CUR <- NXT at a word crossing) of the
//       active bank; visible to a read in cycle t+4 (so T >= 4 slots/sample
//       works without bypass); o_chip_late detection
//   t+4 multiply a * cos / a * sin (DSP)
//   t+5 accumulate over the slots of the sample (cleared by s_first)
//   t+6 o_valid / o_i / o_q on the last slot
//
// Packed records PAR/ST/SCI: see gs_defs.vh.  Behaviour:
//  * loader write (l_we && l_pipe==PIDX): writes PAR+ST of (l_bank,l_slot),
//    sets CUR.cur_widx from ST[147:141] with cur_loaded=0, clears nxt_loaded.
//  * scanner write (sc_we && sc_wpipe==PIDX): CUR (word,widx,cur_loaded=1) or
//    NXT (word,widx,nxt_loaded=1) of (sc_wbank,sc_wslot).
//  * the pipe updates ST and (at a 32-chip word crossing) CUR <- NXT of the
//    issued bank; o_chip_late pulses if at a crossing nxt_widx != new word
//    index or !nxt_loaded, or if an unmuted valid channel was issued with
//    cur_loaded == 0.
//  * s_mute forces the contribution to zero but the state advances normally.
//  * Entries with valid==0 contribute zero and their state is not written.
`include "gs_defs.vh"

// One bank of per-slot (8 slots) storage.  Separate async-read arrays, one
// write port each; the write sources are arbitrated per array.
module gs_pipe_bank (
    input  wire        clk,
    // read A: pipe issue
    input  wire  [2:0] ra,
    output wire [11:0] a_parh,      // PAR[11:0]
    output wire [160:0] a_parb,     // PAR[172:12]
    output wire [140:0] a_st,       // ST[140:0]
    output wire [31:0] a_curw,
    output wire  [7:0] a_curx,      // {cur_loaded, cur_widx}
    // read B: scanner
    input  wire  [2:0] rb,
    output wire [`GS_SCI_W-1:0] b_sci,
    // read C: pipe at the write stage (NXT)
    input  wire  [2:0] rc,
    output wire [31:0] c_nxtw,
    output wire  [6:0] c_nxtx,
    output wire        c_nxtl,
    // loader write
    input  wire        lw_en,
    input  wire  [2:0] lw_slot,
    input  wire [`GS_PAR_W-1:0] lw_par,
    input  wire [`GS_ST_W-1:0]  lw_st,
    // pipe write
    input  wire        pw_en,
    input  wire  [2:0] pw_slot,
    input  wire [140:0] pw_st,
    input  wire        px_en,       // word crossing: CUR <- NXT
    input  wire [31:0] px_word,
    input  wire  [6:0] px_widx,
    input  wire        px_loaded,
    // scanner write
    input  wire        sw_en,
    input  wire  [2:0] sw_slot,
    input  wire        sw_which,    // 0 CUR, 1 NXT
    input  wire [31:0] sw_word,
    input  wire  [6:0] sw_widx
);
    reg [11:0]  parh [0:7];
    reg [160:0] parb [0:7];
    reg [140:0] st   [0:7];
    reg [31:0]  curw [0:7];
    reg [7:0]   curx [0:7];
    reg [38:0]  nxtw [0:7];         // {widx, word}
    reg         nxtl [0:7];
    integer i;
    initial begin
        for (i = 0; i < 8; i = i + 1) begin
            parh[i] = 12'd0; parb[i] = 161'd0; st[i] = 141'd0;
            curw[i] = 32'd0; curx[i] = 8'd0; nxtw[i] = 39'd0; nxtl[i] = 1'b0;
        end
    end

    always @(posedge clk) begin
        if (lw_en) begin
            parh[lw_slot] <= lw_par[11:0];
            parb[lw_slot] <= lw_par[`GS_PAR_W-1:12];
        end
        if (pw_en)      st[pw_slot] <= pw_st;
        else if (lw_en) st[lw_slot] <= lw_st[140:0];

        if (px_en)                       curw[pw_slot] <= px_word;
        else if (sw_en && !sw_which)     curw[sw_slot] <= sw_word;

        if (px_en)                       curx[pw_slot] <= {px_loaded, px_widx};
        else if (lw_en)                  curx[lw_slot] <= {1'b0, lw_st[147:141]};
        else if (sw_en && !sw_which)     curx[sw_slot] <= {1'b1, sw_widx};

        if (sw_en && sw_which)           nxtw[sw_slot] <= {sw_widx, sw_word};

        if (lw_en)                       nxtl[lw_slot] <= 1'b0;
        else if (sw_en && sw_which)      nxtl[sw_slot] <= 1'b1;
    end

    assign a_parh = parh[ra];
    assign a_parb = parb[ra];
    assign a_st   = st[ra];
    assign a_curw = curw[ra];
    assign a_curx = curx[ra];

    wire [11:0] bh = parh[rb];
    wire [7:0]  bx = curx[rb];
    assign b_sci = {nxtl[rb], nxtw[rb][38:32], bx[6:0], bx[7], bh[11:6], bh[5:4], bh[0]};

    assign c_nxtw = nxtw[rc][31:0];
    assign c_nxtx = nxtw[rc][38:32];
    assign c_nxtl = nxtl[rc];
endmodule

module gs_pipe #(parameter PIDX = 0) (
    input  wire        clk,
    input  wire        rst,
    // sequencer broadcast
    input  wire        s_valid,
    input  wire  [2:0] s_slot,
    input  wire        s_bank,
    input  wire        s_first,
    input  wire        s_last,
    input  wire        s_mute,
    // loader broadcast
    input  wire        l_we,
    input  wire  [3:0] l_pipe,
    input  wire  [2:0] l_slot,
    input  wire        l_bank,
    input  wire [`GS_PAR_W-1:0] l_par,
    input  wire [`GS_ST_W-1:0]  l_st,
    // scanner access
    input  wire  [2:0] sc_slot,
    input  wire        sc_bank,
    output wire [`GS_SCI_W-1:0] sc_info,
    input  wire        sc_we,
    input  wire  [3:0] sc_wpipe,
    input  wire  [2:0] sc_wslot,
    input  wire        sc_wbank,
    input  wire        sc_wwhich,
    input  wire [31:0] sc_wword,
    input  wire  [6:0] sc_wwidx,
    // outputs
    output reg         o_valid,
    output reg  signed [37:0] o_i,
    output reg  signed [37:0] o_q,
    output reg         o_chip_late
);
    localparam LAT_PIPE = 6;        // documentation only

    // ------------------------------------------------------------ banks
    wire lwe = l_we && (l_pipe == PIDX);
    wire swe = sc_we && (sc_wpipe == PIDX);

    // stage-3 write controls (declared early, driven below)
    reg         r3_v, r3_bank;
    reg   [2:0] r3_slot;
    wire        w_en;
    wire        w_cross;
    wire [140:0] w_st;

    wire [11:0]  a_parh [0:1];
    wire [160:0] a_parb [0:1];
    wire [140:0] a_st   [0:1];
    wire [31:0]  a_curw [0:1];
    wire [7:0]   a_curx [0:1];
    wire [`GS_SCI_W-1:0] b_sci [0:1];
    wire [31:0]  c_nxtw [0:1];
    wire [6:0]   c_nxtx [0:1];
    wire         c_nxtl [0:1];

    genvar b;
    generate
        for (b = 0; b < 2; b = b + 1) begin : g_bank
            gs_pipe_bank u_bank (
                .clk(clk),
                .ra(s_slot), .a_parh(a_parh[b]), .a_parb(a_parb[b]), .a_st(a_st[b]),
                .a_curw(a_curw[b]), .a_curx(a_curx[b]),
                .rb(sc_slot), .b_sci(b_sci[b]),
                .rc(r3_slot), .c_nxtw(c_nxtw[b]), .c_nxtx(c_nxtx[b]), .c_nxtl(c_nxtl[b]),
                .lw_en(lwe && (l_bank == b)), .lw_slot(l_slot), .lw_par(l_par), .lw_st(l_st),
                .pw_en(w_en && (r3_bank == b)), .pw_slot(r3_slot), .pw_st(w_st),
                .px_en(w_en && (r3_bank == b) && w_cross),
                .px_word(c_nxtw[b]), .px_widx(c_nxtx[b]), .px_loaded(c_nxtl[b]),
                .sw_en(swe && (sc_wbank == b)), .sw_slot(sc_wslot), .sw_which(sc_wwhich),
                .sw_word(sc_wword), .sw_widx(sc_wwidx));
        end
    endgenerate

    assign sc_info = sc_bank ? b_sci[1] : b_sci[0];

    // ---------------------------------------------------------- stage 1
    reg         r1_v, r1_first, r1_last, r1_mute, r1_bank;
    reg   [2:0] r1_slot;
    reg  [11:0] r1_parh;
    reg [160:0] r1_parb;
    reg [140:0] r1_st;
    reg  [31:0] r1_curw;
    reg   [7:0] r1_curx;
    always @(posedge clk) begin
        if (rst) r1_v <= 1'b0; else r1_v <= s_valid;
        r1_first <= s_first;
        r1_last  <= s_last;
        r1_mute  <= s_mute;
        r1_bank  <= s_bank;
        r1_slot  <= s_slot;
        r1_parh  <= s_bank ? a_parh[1] : a_parh[0];
        r1_parb  <= s_bank ? a_parb[1] : a_parb[0];
        r1_st    <= s_bank ? a_st[1]   : a_st[0];
        r1_curw  <= s_bank ? a_curw[1] : a_curw[0];
        r1_curx  <= s_bank ? a_curx[1] : a_curx[0];
    end

    wire [`GS_PAR_W-1:0] r1_par = {r1_parb, r1_parh};
    wire        p_valid = r1_par[0];
    wire        p_boc   = r1_par[1];
    wire  [1:0] p_cboc  = r1_par[3:2];
    wire  [1:0] p_kind  = r1_par[5:4];
    wire [31:0] p_cinc  = r1_par[43:12];
    wire [42:0] p_kinc  = r1_par[86:44];
    wire [31:0] p_sbits = r1_par[118:87];
    wire [17:0] p_c0    = r1_par[136:119];
    wire [17:0] p_c1    = r1_par[154:137];
    wire [17:0] p_c2    = r1_par[172:155];
    wire [31:0] s_ph    = r1_st[31:0];
    wire [51:0] s_cp    = r1_st[83:32];
    wire [51:0] s_sp    = r1_st[135:84];
    wire  [4:0] s_per   = r1_st[140:136];

    // carrier index -> table (output valid 3 clocks later = stage 4)
    wire [31:0] ph_r = s_ph + 32'h0008_0000;
    wire [11:0] idx  = ph_r[31:20];
    wire signed [15:0] t_cos, t_sin;
    gs_sincos u_sc (.clk(clk), .in_idx(idx), .out_cos(t_cos), .out_sin(t_sin));

    // 12*cp bit 40 depends on cp[40:0] only
    wire [40:0] cp41 = s_cp[40:0];
    wire [40:0] c12  = (cp41 << 3) + (cp41 << 2);

    // ---------------------------------------------------------- stage 2
    reg         r2_v, r2_first, r2_last, r2_mute, r2_bank, r2_pv, r2_boc;
    reg   [2:0] r2_slot;
    reg   [1:0] r2_cboc, r2_kind;
    reg  [31:0] r2_ph;
    reg  [52:0] r2_cps, r2_sps;
    reg   [6:0] r2_owidx;
    reg   [4:0] r2_per;
    reg         r2_chipn, r2_sgnn, r2_s11n, r2_s61n, r2_unl;
    reg  [17:0] r2_c0, r2_c1, r2_c2;
    always @(posedge clk) begin
        if (rst) r2_v <= 1'b0; else r2_v <= r1_v;
        r2_first <= r1_first;
        r2_last  <= r1_last;
        r2_mute  <= r1_mute;
        r2_bank  <= r1_bank;
        r2_slot  <= r1_slot;
        r2_pv    <= p_valid;
        r2_boc   <= p_boc;
        r2_cboc  <= p_cboc;
        r2_kind  <= p_kind;
        r2_ph    <= s_ph + p_cinc;
        r2_cps   <= {1'b0, s_cp} + {10'd0, p_kinc};
        r2_sps   <= {1'b0, s_sp} + {10'd0, p_kinc};
        r2_owidx <= s_cp[51:45];
        r2_per   <= s_per;
        r2_chipn <= ~r1_curw[s_cp[44:40]];
        r2_sgnn  <= ~p_sbits[s_per];
        r2_s11n  <= s_cp[39];
        r2_s61n  <= c12[40];
        r2_unl   <= ~r1_curx[7];
        r2_c0    <= p_c0;
        r2_c1    <= p_c1;
        r2_c2    <= p_c2;
    end

    function [51:0] lim_of;
        input [1:0] kind;
        begin
            case (kind)
                2'd0:    lim_of = 52'd1023 << 40;
                2'd3:    lim_of = 52'd2046 << 40;
                default: lim_of = 52'd4092 << 40;
            endcase
        end
    endfunction

    wire [51:0] lim2 = lim_of(r2_kind);
    wire        cb_neg  = (r2_cboc == 2'd2);
    wire        cb_any  = (r2_cboc != 2'd0);
    wire        cb_pos  = ~(cb_neg ^ r2_s11n ^ r2_s61n);        // cboc*s11*s61 > 0
    wire [17:0] mag     = (r2_boc && cb_any) ? (cb_pos ? r2_c1 : r2_c2) : r2_c0;
    wire        a_neg   = (r2_boc & r2_s11n) ^ r2_chipn ^ r2_sgnn;
    wire signed [18:0] mag_s = {mag[17], mag};
    wire signed [18:0] a_pre = a_neg ? -mag_s : mag_s;
    wire signed [18:0] a2    = (r2_pv && !r2_mute) ? a_pre : 19'sd0;

    // ---------------------------------------------------------- stage 3
    reg         r3_first, r3_last, r3_pv, r3_unl, r3_mute;
    reg  [31:0] r3_ph;
    reg  [52:0] r3_cps, r3_sps;
    reg  [53:0] r3_dc, r3_ds;
    reg   [6:0] r3_owidx;
    reg   [4:0] r3_per;
    reg signed [18:0] r3_a;
    always @(posedge clk) begin
        if (rst) r3_v <= 1'b0; else r3_v <= r2_v;
        r3_first <= r2_first;
        r3_last  <= r2_last;
        r3_bank  <= r2_bank;
        r3_slot  <= r2_slot;
        r3_pv    <= r2_pv;
        r3_unl   <= r2_unl;
        r3_mute  <= r2_mute;
        r3_ph    <= r2_ph;
        r3_cps   <= r2_cps;
        r3_sps   <= r2_sps;
        r3_dc    <= {1'b0, r2_cps} - {2'b00, lim2};
        r3_ds    <= {1'b0, r2_sps} - {2'b00, lim2};
        r3_owidx <= r2_owidx;
        r3_per   <= r2_per;
        r3_a     <= a2;
    end

    wire        cwrap = ~r3_dc[53];
    wire        swrap = ~r3_ds[53];
    wire [51:0] cp_new = cwrap ? r3_dc[51:0] : r3_cps[51:0];
    wire [51:0] sp_new = swrap ? r3_ds[51:0] : r3_sps[51:0];
    wire  [4:0] per_new = swrap ? (r3_per + 5'd1) : r3_per;
    assign w_st    = {per_new, sp_new, cp_new, r3_ph};
    assign w_en    = r3_v && r3_pv;
    wire [6:0] nw_idx = cp_new[51:45];
    assign w_cross = (nw_idx != r3_owidx);
    wire [6:0] n_x = r3_bank ? c_nxtx[1] : c_nxtx[0];
    wire       n_l = r3_bank ? c_nxtl[1] : c_nxtl[0];
    wire late = w_en && !r3_mute &&
                (r3_unl || (w_cross && (!n_l || (n_x != nw_idx))));

    // ---------------------------------------------------------- stage 4/5
    reg         r4_v, r4_first, r4_last;
    reg signed [18:0] r4_a;
    always @(posedge clk) begin
        if (rst) r4_v <= 1'b0; else r4_v <= r3_v;
        r4_first <= r3_first;
        r4_last  <= r3_last;
        r4_a     <= r3_a;
    end

    (* use_dsp = "yes" *) reg signed [34:0] m_i;
    (* use_dsp = "yes" *) reg signed [34:0] m_q;
    reg         r5_v, r5_first, r5_last;
    always @(posedge clk) begin
        m_i <= r4_a * t_cos;
        m_q <= r4_a * t_sin;
        if (rst) r5_v <= 1'b0; else r5_v <= r4_v;
        r5_first <= r4_first;
        r5_last  <= r4_last;
    end

    reg signed [37:0] acc_i, acc_q;
    wire signed [37:0] sum_i = (r5_first ? 38'sd0 : acc_i) + m_i;
    wire signed [37:0] sum_q = (r5_first ? 38'sd0 : acc_q) + m_q;
    always @(posedge clk) begin
        if (rst) begin
            o_valid <= 1'b0;
            o_chip_late <= 1'b0;
        end else begin
            o_valid <= r5_v && r5_last;
            o_chip_late <= late;
        end
        if (r5_v) begin
            acc_i <= sum_i;
            acc_q <= sum_q;
        end
        if (r5_v && r5_last) begin
            o_i <= sum_i;
            o_q <= sum_q;
        end
    end
endmodule
