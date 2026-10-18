// gs_pipe.v - one channel pipe of the gnss_synth core: per-bank parameter/state
// RAMs (distributed RAM, async read), the sample datapath and the slot
// accumulator.
//
// LAT_PIPE = 7 clocks: if the last slot of a sample is issued in cycle L
// (s_valid && s_last), o_valid is high in cycle L+7 with o_i/o_q the sum over
// the slots of that sample (38-bit signed).  o_chip_late is a one-clock pulse in
// cycle t+7 for an offending slot issued in cycle t, independent of the o_valid
// timing.  Constant for every pipe.
//
// Timeline for a slot issued in cycle t (inputs s_* presented in cycle t; "in
// cycle n" = the register named is valid / the RAM is read in cycle n):
//   t+1 p0_* (local copy of the sequencer broadcast); async reads of PAR/ST/CUR
//       of (bank, slot) -> stage-1 registers
//   t+2 r1_*: carrier index -> gs_sincos (output valid in t+5), phase/code/sign
//       phase increments, chip bit, period-sign bit, S11/S61 sub-carrier bits
//   t+3 r2_*: code-phase wrap subtraction (cp+inc-lim, sp+inc-lim), word-crossing
//       bounds, coefficient select (zeroed when muted / invalid) and amplitude
//       sign xor quadrant sign
//   t+4 r3_*: wrap select, ST write (and CUR <- NXT at a word crossing) of the
//       active bank at the end of t+4, visible to a read in cycle t+5: the
//       read-modify-write loop is t+1 .. t+4 (4 clocks), so T >= 4 slots/sample
//       works without bypass.  chip_late inputs are registered (ln_*)
//   t+5 r4_*: amplitude with the cos / sin quadrant sign (conditional negation
//       of the magnitude), multiplied by the table value (DSP) at the end of
//       t+5; chip_late compare / combine
//   t+6 m_i / m_q accumulated at the end of t+6 (cleared by s_first); late_r
//   t+7 acc valid: o_valid / o_i / o_q on the last slot (the accumulator is the
//       DSP P register); o_chip_late
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
    // read A: pipe issue (one address copy per array group: placement/fan-out)
    input  wire  [2:0] ra_pb,       // parh, parb
    input  wire  [2:0] ra_st,       // st
    input  wire  [2:0] ra_m,        // curw, curx
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
    // loader write (enable and slot copies per array group)
    input  wire        lw_en,
    input  wire  [2:0] lw_slot_p,   // parh, parb
    input  wire  [2:0] lw_slot_s,   // st
    input  wire  [2:0] lw_slot_m,   // curx, nxtl
    input  wire [`GS_PAR_W-1:0] lw_par,
    input  wire [`GS_ST_W-1:0]  lw_st,
    // pipe write
    input  wire        pw_en,
    input  wire  [2:0] pw_slot_s,   // st
    input  wire  [2:0] pw_slot_m,   // curw, curx
    input  wire [140:0] pw_st,
    input  wire        px_en,       // word crossing: CUR <- NXT
    input  wire [31:0] px_word,
    input  wire  [6:0] px_widx,
    input  wire        px_loaded,
    // scanner write (separate registered enables for CUR / NXT)
    input  wire        swc_en,
    input  wire        swn_en,
    input  wire  [2:0] sw_slot,
    input  wire [31:0] sw_word,
    input  wire  [6:0] sw_widx
);
    // ST[31:0] (carrier phase) is stored with the +2^19 table-rounding offset
    // already applied by the loader (phase' = phase + 2^19 mod 2^32), so the
    // table index is phase'[31:20] without an adder in front of the BRAM.
    wire [140:0] lw_st_adj = lw_st[140:0];
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

    // One write port per array: enable / address / data are combined explicitly
    // (priority pipe > loader > scanner as before).  Written as separate guarded
    // assignments, the tool turned the RAMs into always-write RAMs with a
    // read-back of the old value through a second address (huge fan-out on the
    // scanner slot, extra LUT in the data path).
    wire        st_we   = pw_en | lw_en;
    wire  [2:0] st_wa   = pw_en ? pw_slot_s : lw_slot_s;
    wire [140:0] st_wd  = pw_en ? pw_st : lw_st_adj;
    wire        cw_we   = px_en | swc_en;
    wire  [2:0] cw_wa   = px_en ? pw_slot_m : sw_slot;
    wire [31:0] cw_wd   = px_en ? px_word : sw_word;
    wire        cx_we   = px_en | lw_en | swc_en;
    wire  [2:0] cx_wa   = px_en ? pw_slot_m : (lw_en ? lw_slot_m : sw_slot);
    wire  [7:0] cx_wd   = px_en ? {px_loaded, px_widx} :
                          (lw_en ? {1'b0, lw_st[147:141]} : {1'b1, sw_widx});
    wire        nl_we   = lw_en | swn_en;
    wire  [2:0] nl_wa   = lw_en ? lw_slot_m : sw_slot;

    always @(posedge clk) begin
        if (lw_en) begin
            parh[lw_slot_p] <= lw_par[11:0];
            parb[lw_slot_p] <= lw_par[`GS_PAR_W-1:12];
        end
        if (st_we) st[st_wa]   <= st_wd;
        if (cw_we) curw[cw_wa] <= cw_wd;
        if (cx_we) curx[cx_wa] <= cx_wd;
        if (swn_en) nxtw[sw_slot] <= {sw_widx, sw_word};
        if (nl_we) nxtl[nl_wa] <= ~lw_en;
    end

    assign a_parh = parh[ra_pb];
    assign a_parb = parb[ra_pb];
    assign a_st   = st[ra_st];
    assign a_curw = curw[ra_m];
    assign a_curx = curx[ra_m];

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
    output wire signed [37:0] o_i,
    output wire signed [37:0] o_q,
    output reg         o_chip_late
);
    localparam LAT_PIPE = 7;        // documentation only

    // The control / address registers below are marked dont_touch: the 12 pipes
    // see identical broadcast inputs, and without it the tool merges the
    // identical per-pipe copies into one register (or an SRL) that then drives
    // all pipes across the die.  Address copies are made by hand (one per
    // array group and bank) so each copy has a small fan-out next to its RAMs.

    // ------------------------------------------------ boundary registers
    // Loader / scanner write buses are registered once at the pipe boundary.
    // Effect: loader / scanner writes land one clock later than documented.
    (* dont_touch = "true" *) reg  [1:0] lwe;               // loader write, per bank
    (* dont_touch = "true" *) reg  [1:0] swc, swn;          // scanner CUR / NXT write, per bank
    (* dont_touch = "true" *) reg [17:0] lq_sl;             // loader slot copies [bank*9 + {par,st,misc}*3]
    (* dont_touch = "true" *) reg  [2:0] sq_slot;
    reg [`GS_PAR_W-1:0]    lq_par;
    reg [`GS_ST_W-1:0]     lq_st;
    reg             [31:0] sq_word;
    reg              [6:0] sq_widx;
    wire l_mine = l_we && (l_pipe == PIDX);
    wire s_mine = sc_we && (sc_wpipe == PIDX);
    always @(posedge clk) begin
        lwe[0] <= rst ? 1'b0 : (l_mine && !l_bank);
        lwe[1] <= rst ? 1'b0 : (l_mine &&  l_bank);
        swc[0] <= rst ? 1'b0 : (s_mine && !sc_wbank && !sc_wwhich);
        swc[1] <= rst ? 1'b0 : (s_mine &&  sc_wbank && !sc_wwhich);
        swn[0] <= rst ? 1'b0 : (s_mine && !sc_wbank &&  sc_wwhich);
        swn[1] <= rst ? 1'b0 : (s_mine &&  sc_wbank &&  sc_wwhich);
        lq_sl  <= {6{l_slot}};
        lq_par <= l_par; lq_st <= l_st;
        sq_slot <= sc_wslot;
        sq_word <= sc_wword; sq_widx <= sc_wwidx;
    end

    // Sequencer broadcast, registered locally (+1 clock).  The bank / slot copies
    // feed the read addresses of every array group and the bank selects of the
    // stage-1 registers.
    (* dont_touch = "true" *) reg        p0_v, p0_first, p0_last, p0_mute;
    (* dont_touch = "true" *) reg  [3:0] p0_bk;             // bank copies (stage-1 muxes)
    (* dont_touch = "true" *) reg [17:0] p0_sl;             // slot copies [bank*9 + {par,st,misc}*3]
    always @(posedge clk) begin
        if (rst) p0_v <= 1'b0; else p0_v <= s_valid;
        p0_first <= s_first; p0_last <= s_last; p0_mute <= s_mute;
        p0_bk <= {4{s_bank}};
        p0_sl <= {6{s_slot}};
    end

    // stage-3 write controls (declared early, driven below)
    (* dont_touch = "true" *) reg  [1:0] r3_wv;             // pipe write enable per bank
    (* dont_touch = "true" *) reg [11:0] r3_sl;             // slot copies: [bank*3] misc (write + NXT read), [6+bank*3] st
    (* dont_touch = "true" *) reg        r3_bank;           // bank select of the NXT read mux
    (* dont_touch = "true" *) reg        r3_v;
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
                .ra_pb(p0_sl[b*9 +: 3]), .ra_st(p0_sl[b*9+3 +: 3]), .ra_m(p0_sl[b*9+6 +: 3]),
                .a_parh(a_parh[b]), .a_parb(a_parb[b]), .a_st(a_st[b]),
                .a_curw(a_curw[b]), .a_curx(a_curx[b]),
                .rb(sc_slot), .b_sci(b_sci[b]),
                .rc(r3_sl[b*3 +: 3]), .c_nxtw(c_nxtw[b]), .c_nxtx(c_nxtx[b]), .c_nxtl(c_nxtl[b]),
                .lw_en(lwe[b]), .lw_slot_p(lq_sl[b*9 +: 3]), .lw_slot_s(lq_sl[b*9+3 +: 3]),
                .lw_slot_m(lq_sl[b*9+6 +: 3]), .lw_par(lq_par), .lw_st(lq_st),
                .pw_en(r3_wv[b]), .pw_slot_s(r3_sl[6+b*3 +: 3]), .pw_slot_m(r3_sl[b*3 +: 3]),
                .pw_st(w_st),
                .px_en(r3_wv[b] && w_cross),
                .px_word(c_nxtw[b]), .px_widx(c_nxtx[b]), .px_loaded(c_nxtl[b]),
                .swc_en(swc[b]), .swn_en(swn[b]), .sw_slot(sq_slot),
                .sw_word(sq_word), .sw_widx(sq_widx));
        end
    endgenerate

    assign sc_info = sc_bank ? b_sci[1] : b_sci[0];

    // ---------------------------------------------------------- stage 1
    (* dont_touch = "true" *) reg         r1_v, r1_first, r1_last, r1_mute, r1_bank;
    (* dont_touch = "true" *) reg   [2:0] r1_slot;
    reg  [11:0] r1_parh;
    reg [160:0] r1_parb;
    reg [140:0] r1_st;
    reg  [31:0] r1_curw;
    reg   [7:0] r1_curx;
    always @(posedge clk) begin
        if (rst) r1_v <= 1'b0; else r1_v <= p0_v;
        r1_first <= p0_first;
        r1_last  <= p0_last;
        r1_mute  <= p0_mute;
        r1_bank  <= p0_bk[0];
        r1_slot  <= p0_sl[2:0];
        r1_parh  <= p0_bk[0] ? a_parh[1] : a_parh[0];
        r1_parb  <= p0_bk[1] ? a_parb[1] : a_parb[0];
        r1_st    <= p0_bk[2] ? a_st[1]   : a_st[0];
        r1_curw  <= p0_bk[3] ? a_curw[1] : a_curw[0];
        r1_curx  <= p0_bk[3] ? a_curx[1] : a_curx[0];
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
    wire [11:0] idx  = s_ph[31:20];     // s_ph already includes the +2^19 offset
    wire signed [15:0] t_cm, t_sm;            // |cos|, |sin| (table values >= 0)
    wire               pre_nc, pre_ns;        // quadrant signs, one clock before t_cm/t_sm
    wire               nx_nc, nx_ns;          // the same, one clock earlier (stage 2)
    gs_sincos u_sc (.clk(clk), .in_idx(idx), .out_cos(), .out_sin(),
                    .out_cm(t_cm), .out_sm(t_sm), .pre_nc(pre_nc), .pre_ns(pre_ns),
                    .nx_nc(nx_nc), .nx_ns(nx_ns));

    // 12*cp bit 40 depends on cp[40:0] only
    wire [40:0] cp41 = s_cp[40:0];
    wire [40:0] c12  = (cp41 << 3) + (cp41 << 2);

    // ---------------------------------------------------------- stage 2
    (* dont_touch = "true" *) reg         r2_v, r2_first, r2_last, r2_mute, r2_bank, r2_pv;
    (* dont_touch = "true" *) reg   [2:0] r2_slot;
    reg         r2_boc;
    reg   [1:0] r2_cboc, r2_kind;
    reg  [31:0] r2_ph;
    reg  [52:0] r2_cps, r2_sps;
    reg   [6:0] r2_owidx;
    reg   [4:0] r2_per;
    reg         r2_chipn, r2_sgnn, r2_s11n, r2_s61n, r2_unl;
    reg  [17:0] r2_c0, r2_c1, r2_c2;
    reg  [12:0] r2_t0, r2_t1;           // word-crossing bounds in the wrap case: [t0, t1)
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
        // wrap case: new word index (cps_hi - lim_hi) >> 5 == old index  <=>  cps_hi in [lim_hi + 32*o, +32)
        r2_t0    <= {1'b0, lim_of(p_kind) >> 40} + {s_cp[51:45], 5'd0};
        r2_t1    <= {1'b0, lim_of(p_kind) >> 40} + {{1'b0, s_cp[51:45]} + 8'd1, 5'd0};
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
    wire [17:0] mag_z   = (r2_pv && !r2_mute) ? mag : 18'd0;     // zero contribution

    // ---------------------------------------------------------- stage 3
    (* dont_touch = "true" *) reg         r3_first, r3_last, r3_pv, r3_unl, r3_mute;
    reg  [31:0] r3_ph;
    reg  [52:0] r3_cps, r3_sps;
    reg  [53:0] r3_dc, r3_ds;
    reg   [6:0] r3_owidx, r3_idxn, r3_idxw;
    reg         r3_xn, r3_xw;
    reg   [4:0] r3_per;
    reg  [17:0] r3_mag;             // amplitude magnitude (0 when muted / invalid)
    reg         r3_nc, r3_ns;       // amplitude sign xor cos / sin quadrant sign (registered one clock early)
    always @(posedge clk) begin
        if (rst) r3_v <= 1'b0; else r3_v <= r2_v;
        r3_wv[0] <= !rst && r2_v && r2_pv && !r2_bank;
        r3_wv[1] <= !rst && r2_v && r2_pv &&  r2_bank;
        r3_first <= r2_first;
        r3_last  <= r2_last;
        r3_bank  <= r2_bank;
        r3_sl    <= {4{r2_slot}};
        r3_pv    <= r2_pv;
        r3_unl   <= r2_unl;
        r3_mute  <= r2_mute;
        r3_ph    <= r2_ph;
        r3_cps   <= r2_cps;
        r3_sps   <= r2_sps;
        r3_dc    <= {1'b0, r2_cps} - {2'b00, lim2};
        r3_ds    <= {1'b0, r2_sps} - {2'b00, lim2};
        r3_owidx <= r2_owidx;
        r3_idxn  <= r2_cps[51:45];                              // new word index, no wrap
        r3_idxw  <= (r2_cps[51:40] - lim2[51:40]) >> 5;         // new word index, wrap
        r3_xn    <= (r2_cps[51:45] != r2_owidx);                // word crossing, no wrap
        r3_xw    <= ~(({1'b0, r2_cps[51:40]} >= r2_t0) && ({1'b0, r2_cps[51:40]} < r2_t1));  // wrap
        r3_per   <= r2_per;
        r3_mag   <= mag_z;
        r3_nc    <= a_neg ^ nx_nc;
        r3_ns    <= a_neg ^ nx_ns;
    end

    wire        cwrap = ~r3_dc[53];
    wire        swrap = ~r3_ds[53];
    wire [51:0] cp_new = cwrap ? r3_dc[51:0] : r3_cps[51:0];
    wire [51:0] sp_new = swrap ? r3_ds[51:0] : r3_sps[51:0];
    wire  [4:0] per_new = swrap ? (r3_per + 5'd1) : r3_per;
    assign w_st    = {per_new, sp_new, cp_new, r3_ph};
    assign w_en    = r3_v && r3_pv;
    wire [6:0] nw_idx = cwrap ? r3_idxw : r3_idxn;           // == cp_new[51:45]
    assign w_cross = cwrap ? r3_xw : r3_xn;       // == (nw_idx != r3_owidx)
    wire [6:0] n_x = r3_bank ? c_nxtx[1] : c_nxtx[0];
    wire       n_l = r3_bank ? c_nxtl[1] : c_nxtl[0];
    // chip_late detection in two registered steps (NXT read / mux / index select in
    // the write stage, compare and combine one clock later)
    reg        ln_en, ln_unl, ln_cross, ln_l;
    reg  [6:0] ln_x, ln_idx;
    always @(posedge clk) begin
        ln_en    <= w_en && !r3_mute;
        ln_unl   <= r3_unl;
        ln_cross <= w_cross;
        ln_l     <= n_l;
        ln_x     <= n_x;
        ln_idx   <= nw_idx;
    end
    wire late = ln_en && (ln_unl || (ln_cross && (!ln_l || (ln_x != ln_idx))));

    // ---------------------------------------------------------- stage 4/5
    // Amplitude = r3_mag with the combined sign r3_nc / r3_ns (one negation).
    reg         r4_v, r4_first, r4_last;
    reg signed [18:0] r4_ac, r4_as;           // amplitude with the cos / sin quadrant sign
    wire signed [18:0] mag_s = {r3_mag[17], r3_mag};
    // conditional negation as xor + carry-in (one LUT row in front of the carry chain)
    wire signed [18:0] cx_c = mag_s ^ {19{r3_nc}};
    wire signed [18:0] cx_s = mag_s ^ {19{r3_ns}};
    always @(posedge clk) begin
        if (rst) r4_v <= 1'b0; else r4_v <= r3_v;
        r4_first <= r3_first;
        r4_last  <= r3_last;
        r4_ac    <= cx_c + {18'd0, r3_nc};
        r4_as    <= cx_s + {18'd0, r3_ns};
    end

    (* use_dsp = "yes" *) reg signed [34:0] m_i;
    (* use_dsp = "yes" *) reg signed [34:0] m_q;
    reg         r5_v, r5_first, r5_last;
    always @(posedge clk) begin
        m_i <= r4_ac * t_cm;
        m_q <= r4_as * t_sm;
        if (rst) r5_v <= 1'b0; else r5_v <= r4_v;
        r5_first <= r4_first;
        r5_last  <= r4_last;
    end

    // accumulator: maps onto the DSP48E1 P register (OPMODE clears on s_first).
    // o_i/o_q are the accumulator itself: in the clock after the last slot's add
    // (the o_valid clock) it holds the complete sample sum.
    reg signed [37:0] acc_i, acc_q;
    reg late_r;                       // late detection registered once more (timing)
    always @(posedge clk) begin
        if (rst) begin
            o_valid <= 1'b0;
            o_chip_late <= 1'b0; late_r <= 1'b0;
        end else begin
            o_valid <= r5_v && r5_last;
            late_r <= late;
            o_chip_late <= late_r;
        end
        if (r5_v) begin
            acc_i <= (r5_first ? 38'sd0 : acc_i) + m_i;
            acc_q <= (r5_first ? 38'sd0 : acc_q) + m_q;
        end
    end
    assign o_i = acc_i;
    assign o_q = acc_q;
endmodule
