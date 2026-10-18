// gs_scan.v - chip-word scanner and code ROM.
//
// Visits (bank, pipe, slot) one entry per clock, cyclically; slot fastest,
// then pipe, then bank.  Scan period = 2*GS_P*GS_SLOTS clocks (144 for P=12,
// 128 for P=2).  For an entry with valid=1:
//   - bank==shadow_bank and !cur_loaded : fetch word widx=cur_widx of
//     (kind,prn) and write CUR (sc_wwhich=0);
//   - else if !nxt_loaded or nxt_widx != succ(cur_widx): fetch succ(cur_widx)
//     and write NXT (sc_wwhich=1).  succ wraps at words-per-code
//     (32/128/128/64 for kind 0..3).
// Entries with valid=0 cause no ROM access and no write.
//
// Latency (clock t = first clock in which sc_pipe/sc_slot/sc_bank hold the
// address A; the pipe's combinational sc_info for A is valid in that same t):
//   t   : sc_info_all[sc_pipe*GS_SCI_W +: GS_SCI_W] muxed and registered (the pipe's
//         sc_info is registered inside the pipe, sc_info_q; the timing is unchanged)
//   t+1 : decision (comb), base-table BRAM read issued
//   t+2 : rom address = base + widx registered
//   t+3 : code ROM BRAM read issued
//   t+4 : rom data registered -> sc_we/sc_w* registered at the end of t+4
//   t+5 : sc_we and write fields are visible; the RAM write lands at the end
//         of t+5, so sc_info shows the result from t+6.  (5 clocks from scan
//         address to visible write, 6 to visible effect.)
// The scan period is much longer than this, so the effect of a fetch is
// always seen before the entry is visited again.
//
// shadow_dirty: pulse (or held) from the loader.  It must be high in every
// clock in which the loader asserts l_we (single-clock pulses per write, or
// held for a burst).  It squashes all fetches in flight (the shadow entry is
// being rewritten, a stale CUR/NXT write would corrupt it) and clears
// shadow_clean one clock later.  A change of shadow_bank likewise squashes
// everything in flight and clears shadow_clean.  If sc_we collides with a
// loader write to the same entry, the loader must win; with the rule above
// no stale write is ever issued after the dirty clock.
//
// shadow_clean: 1 after GS_P*GS_SLOTS consecutive scan positions of the
// shadow bank (= one complete pass) in which no fetch was issued, with nothing
// in flight.  Cleared (registered) by shadow_dirty, shadow_bank change, or any
// fetch decided on the shadow bank.
//
// For the active bank cur_loaded is assumed 1 (pipe maintains it).
//
// Slot index width is 3 for GS_SLOTS<=8 (P=12: exactly the plan interface);
// for smaller GS_P (test configurations) it widens automatically.
`timescale 1ns/1ps
`include "gs_defs.vh"

`define GS_SCAN_SW ((`GS_SLOTS <= 8) ? 3 : (`GS_SLOTS <= 16) ? 4 : (`GS_SLOTS <= 32) ? 5 : 6)

module gs_scan (
    input  clk,
    input  rst,
    output reg [3:0] sc_pipe,
    output reg [`GS_SCAN_SW-1:0] sc_slot,
    output reg sc_bank,
    // per-pipe copies of sc_slot / sc_bank (identical values; dont_touch so they are
    // not merged: each copy drives the read addresses of one pipe only)
    output reg [`GS_P*`GS_SCAN_SW-1:0] sc_slot_p,
    output reg [`GS_P-1:0] sc_bank_p,
    input  [`GS_P*`GS_SCI_W-1:0] sc_info_all,
    output reg sc_we,
    output reg [3:0] sc_wpipe,
    output reg [`GS_SCAN_SW-1:0] sc_wslot,
    output reg sc_wbank,
    output reg sc_wwhich,
    output reg [31:0] sc_wword,
    output reg [6:0] sc_wwidx,
    input  shadow_bank,
    input  shadow_dirty,
    output reg shadow_clean     // comb. from registers (always @*)
);
    localparam SW    = `GS_SCAN_SW;
    localparam NPOS  = `GS_P * `GS_SLOTS;     // entries per bank
    localparam P_SCI_BITS = `GS_P * `GS_SCI_W;
    localparam ROM_N = 17856;

    // ---- memories (inferred BRAM, registered read) ----
    // The code ROM is split at 16384 words: a single 17856-deep array is mapped by
    // the tool as 32 x RAMB36 (32K x 1 aspect); 16384 x 32 maps as 16 x (16K x 2)
    // and the 1472-word remainder as 2 x (1K x 36): 18 RAMB36 in total.
    localparam ROM_LO = 16384;
    (* rom_style = "block" *) reg [31:0] rom_lo [0:ROM_LO-1];
    (* rom_style = "block" *) reg [31:0] rom_hi [0:ROM_N-ROM_LO-1];
    (* rom_style = "block" *) reg [15:0] base [0:255];
    initial begin
        $readmemh({`GS_MEMDIR, "gs_rom.mem"},    rom_lo, 0, ROM_LO-1);   // first 16384 lines
        $readmemh({`GS_MEMDIR, "gs_rom_hi.mem"}, rom_hi);                // words 16384.. (1472 lines)
        $readmemh({`GS_MEMDIR, "gs_base.mem"}, base);
    end

    // ---- scan address counter (stage t) ----
    wire [SW-1:0] slot_nx = (sc_slot == `GS_SLOTS-1) ? {SW{1'b0}} : sc_slot + 1'b1;
    wire          bank_nx = (sc_slot == `GS_SLOTS-1 && sc_pipe == `GS_P-1) ? ~sc_bank : sc_bank;
    (* dont_touch = "true" *) reg [`GS_P*SW-1:0] sc_slot_p_i;
    (* dont_touch = "true" *) reg [`GS_P-1:0]    sc_bank_p_i;
    always @(posedge clk) begin
        sc_slot_p_i <= rst ? {`GS_P*SW{1'b0}} : {`GS_P{slot_nx}};
        sc_bank_p_i <= rst ? {`GS_P{1'b0}}   : {`GS_P{bank_nx}};
    end
    always @* begin sc_slot_p = sc_slot_p_i; sc_bank_p = sc_bank_p_i; end
    always @(posedge clk) begin
        if (rst) begin
            sc_pipe <= 0; sc_slot <= 0; sc_bank <= 0;
        end else begin
            if (sc_slot == `GS_SLOTS-1) begin
                sc_slot <= 0;
                if (sc_pipe == `GS_P-1) begin
                    sc_pipe <= 0;
                    sc_bank <= ~sc_bank;
                end else sc_pipe <= sc_pipe + 1'b1;
            end else sc_slot <= sc_slot + 1'b1;
        end
    end

    // ---- squash control ----
    reg dirty_q, dirty_q2, dirty_q3, sb_q, sb_q2, sb_q3;
    always @(posedge clk) begin
        if (rst) begin dirty_q <= 0; dirty_q2 <= 0; dirty_q3 <= 0; sb_q <= shadow_bank; sb_q2 <= shadow_bank; sb_q3 <= shadow_bank; end
        else begin dirty_q <= shadow_dirty; dirty_q2 <= dirty_q; dirty_q3 <= dirty_q2; sb_q <= shadow_bank; sb_q2 <= sb_q; sb_q3 <= sb_q2; end
    end
    wire kill = shadow_dirty | dirty_q | dirty_q2 | dirty_q3 | (shadow_bank != sb_q) | (shadow_bank != sb_q2) | (shadow_bank != sb_q3);

    // ---- stage t+1: registered info ----
    // stage t+1 (new): the pipes' async sc_info is registered here first (the
    // distributed-RAM read + 12:1 mux did not fit in one clock); the squash
    // window is one clock longer to match (dirty_q2).
    reg s0v;
    // sc_info_all is already registered inside the pipes (sc_info_q), one clock
    // after the address (sc_slot_p/sc_bank_p), which replaces the former info_all_q.
    wire [P_SCI_BITS-1:0] info_all_q = sc_info_all;
    reg [3:0] a0_pipe; reg [SW-1:0] a0_slot; reg a0_bank;
    always @(posedge clk) begin
        s0v <= rst ? 1'b0 : 1'b1;
        a0_pipe <= sc_pipe; a0_slot <= sc_slot; a0_bank <= sc_bank;
    end
    reg s1v;
    (* dont_touch = "true" *) reg [`GS_SCI_W-1:0] info_r;   // keep the flop: it must not be retimed into the base BRAM address
    reg [3:0] a1_pipe; reg [SW-1:0] a1_slot; reg a1_bank;
    wire [`GS_SCI_W-1:0] info_mux = info_all_q[a0_pipe*`GS_SCI_W +: `GS_SCI_W];
    always @(posedge clk) begin
        if (rst) s1v <= 0;
        else s1v <= s0v;
        info_r  <= info_mux;
        a1_pipe <= a0_pipe; a1_slot <= a0_slot; a1_bank <= a0_bank;
    end

    wire        i_valid = info_r[0];
    wire [1:0]  i_kind  = info_r[2:1];
    wire [5:0]  i_prn   = info_r[8:3];
    wire        i_curl  = info_r[9];
    wire [6:0]  i_curw  = info_r[16:10];
    wire [6:0]  i_nxtw  = info_r[23:17];
    wire        i_nxtl  = info_r[24];

    wire [6:0] wpc_m1 = (i_kind == 2'd0) ? 7'd31 :
                        (i_kind == 2'd3) ? 7'd63 : 7'd127;
    wire [6:0] succ   = (i_curw >= wpc_m1) ? 7'd0 : i_curw + 7'd1;
    wire is_sh        = (a1_bank == shadow_bank);
    wire f_cur        = s1v & i_valid & is_sh & ~i_curl;
    wire f_nxt        = s1v & i_valid & ~f_cur & (~i_nxtl | (i_nxtw != succ));
    wire fetch        = (f_cur | f_nxt) & ~kill;

    // base table read (registered, issued from info_mux): base_q valid with info_r, base_r in stage t+2
    // The base table is read one clock earlier, with the address muxed from the
    // pipe info (same clock as info_r is loaded), and the BRAM output is moved
    // into a fabric register: the base + widx adder then starts from a flop, not
    // from the BRAM clk-to-q.  base_r is aligned with s2_widx (stage t+2).
    reg [15:0] base_q, base_r;
    always @(posedge clk) begin
        base_q <= base[{info_mux[2:1], info_mux[8:3]}];
        base_r <= base_q;
    end

    // ---- stage t+2 ----
    reg s2v, s2_which; reg [6:0] s2_widx;
    reg [3:0] s2_pipe; reg [SW-1:0] s2_slot; reg s2_bank;
    always @(posedge clk) begin
        s2v <= rst ? 1'b0 : fetch;
        s2_which <= f_nxt; s2_widx <= f_cur ? i_curw : succ;
        s2_pipe <= a1_pipe; s2_slot <= a1_slot; s2_bank <= a1_bank;
    end

    // ---- stage t+3: rom address ----
    reg s3v, s3_which; reg [6:0] s3_widx;
    reg [3:0] s3_pipe; reg [SW-1:0] s3_slot; reg s3_bank;
    (* dont_touch = "true" *) reg [15:0] rom_addr;   // keep the flop (no retiming into the ROM BRAM address)
    always @(posedge clk) begin
        s3v <= rst ? 1'b0 : (s2v & ~kill);
        rom_addr <= base_r + {9'd0, s2_widx};
        s3_which <= s2_which; s3_widx <= s2_widx;
        s3_pipe <= s2_pipe; s3_slot <= s2_slot; s3_bank <= s2_bank;
    end

    // ---- stage t+4: rom read (only when a fetch is valid) ----
    reg s4v, s4_which; reg [6:0] s4_widx;
    reg [3:0] s4_pipe; reg [SW-1:0] s4_slot; reg s4_bank;
    reg [31:0] q_lo, q_hi;
    reg        q_sel;
    wire [31:0] rom_q = q_sel ? q_hi : q_lo;
    always @(posedge clk) begin
        s4v <= rst ? 1'b0 : (s3v & ~kill);
        if (s3v) begin
            q_lo  <= rom_lo[rom_addr[13:0]];
            q_hi  <= rom_hi[rom_addr[10:0]];
            q_sel <= rom_addr[14];
        end
        s4_which <= s3_which; s4_widx <= s3_widx;
        s4_pipe <= s3_pipe; s4_slot <= s3_slot; s4_bank <= s3_bank;
    end

    // ---- stage t+5: write port ----
    always @(posedge clk) begin
        sc_we <= rst ? 1'b0 : (s4v & ~kill);
        sc_wword <= rom_q; sc_wwidx <= s4_widx; sc_wwhich <= s4_which;
        sc_wpipe <= s4_pipe; sc_wslot <= s4_slot; sc_wbank <= s4_bank;
    end

    // ---- shadow_clean ----
    // The fetch decision (late: info_r -> compares) only reaches this logic
    // registered (s2v): cnt restarts one clock after a fetch on the shadow bank
    // (so "clean" is at most one position later than with the combinational
    // fetch), and the shadow_clean output is gated with the registered fetch of
    // the clock after the decision (so it still drops in that clock).
    reg [15:0] cnt;
    reg we_d; reg wb_d;         // write register inside the pipes (one clock after sc_we)
    always @(posedge clk) begin we_d <= rst ? 1'b0 : sc_we; wb_d <= sc_wbank; end
    wire fq = s2v & (s2_bank == shadow_bank);       // fetch decided last clock, on the shadow bank
    wire inflight = fq | (s3v & (s3_bank == shadow_bank)) |
                    (s4v & (s4_bank == shadow_bank)) | (sc_we & (sc_wbank == shadow_bank)) |
                    (we_d & (wb_d == shadow_bank));
    wire count_en = s1v & is_sh;
    wire [15:0] cnt_n = (kill | fq) ? 16'd0 :
                        (count_en && cnt < NPOS) ? cnt + 16'd1 : cnt;
    reg clean_r;
    always @(posedge clk) begin
        if (rst) begin cnt <= 0; clean_r <= 1'b0; end
        else begin
            cnt <= cnt_n;
            clean_r <= ~kill & ~inflight & (cnt_n == NPOS);
        end
    end
    always @* shadow_clean = clean_r & ~fq;
endmodule
