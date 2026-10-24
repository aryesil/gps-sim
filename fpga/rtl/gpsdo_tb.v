// gpsdo_tb.v - GPSDO timebase and time-stamp block for the tezuka_gnss design.
//
// Provides a 48-bit free-running timebase (TB) on a 200 MHz clock derived from
// the 40 MHz VCTCXO (so TB ticks are in the disciplined time domain), a 64-bit
// RX sample counter in the AD9361 divclk domain, and capture logic that ties
// the two together and time-stamps an optional external PPS input.  There is no
// PPS output and nothing on the 250 MHz engine clock.  Module reference in the
// BD (no parameters; configuration by localparam).
//
// Clock domains
//   clk_sys  (100 MHz)  AXI-Lite, software-visible registers
//   clk_div             AD9361 divclk: sample counter (rx_stb strobes)
//   tb_clk   (200 MHz)  gt_clk output: TB counter and captures
// Crossings: single-bit levels / toggles use 2-FF ASYNC_REG synchronisers; a
// multi-bit hold register is only read by the destination after the event
// toggle that announces it has crossed (the hold register does not change again
// for >= the lockout / mark interval).  Constrain the hold buses with
// set_max_delay -datapath_only (see fpga/synth/gt_ooc.xdc).
//
// Register map (offsets in the 0x1000 window, 32-bit)
//   0x00 ID        RO  0x47544231 ("GTB1")
//   0x04 CTRL      RW  b4 samp_clear (write 1; clears the sample counter and the
//                      mark interval counter, self clearing)
//                      b5 snap_req (write 1; start a TB_NOW / SAMP_NOW snapshot)
//                      [12:8] mark_log2 (default 20; a mark every 2^mark_log2
//                      samples, clamped to 10..27; set it before samp_clear)
//   0x08 STATUS    RO  b0 mmcm_locked, b1 ppsin_present (edge seen within
//                      1.5 s), b2 snap_busy
//   0x0C IRQ       RW  [2:0] pending (W1C): b0 ppsin, b1 mark, b2 snap_done
//                      [18:16] enables
//   0x10/0x14 TB_NOW    LO/HI RO  TB at the last snapshot (48 bit)
//   0x18/0x1C PPSIN_TB  RO  TB at the last PPS_IN rising edge (48 bit)
//   0x20/0x24 PPSIN_SAMP RO sample index at the last PPS_IN edge (64 bit)
//   0x28/0x2C MARK_TB   RO  TB at the last sample mark (48 bit)
//   0x30/0x34 MARK_SAMP RO  sample count at that mark (64 bit; an exact
//                           multiple of 2^mark_log2 after samp_clear)
//   0x38/0x3C SAMP_NOW  RO  sample counter at the last snapshot (64 bit)
//   0x40 SEQS      RO  [15:0] ppsin_seq (TB capture count), [31:16] mark_seq
//   0x44 VERSION   RO  [7:0] tb_clk MHz (200), [15:8] default mark_log2
//   0x48 PSEQ2     RO  [15:0] ppsin_samp_seq (sample-index capture count)
// 64-bit reads are coherent: reading LO latches the matching HI word, reading
// HI returns that latched word.  Always read LO first.
// Latencies (constants for software): a PPS_IN edge is captured 3 TB ticks
// (15 ns) after it arrives at the pin, a mark 3 TB ticks after the sample that
// completed it was accepted; PPSIN_SAMP is accurate to about one sample.
// PPS_IN must be high for at least two divclk periods.
// TB_NOW and SAMP_NOW of one snapshot are taken a few clocks apart (coarse);
// use the marks for exact TB <-> sample association.

module gpsdo_tb (
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 clk_sys CLK", X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF s_axi, ASSOCIATED_RESET rst_sys_n" *)
    input  wire        clk_sys,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 rst_sys_n RST", X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
    input  wire        rst_sys_n,
    input  wire        clk_div,
    input  wire        rst_div_n,
    input  wire        clk40,           // VCTCXO pin; plain data port in the BD (no clock interface, no FREQ_HZ)
    input  wire        pps_in,
    input  wire        rx_stb,          // RX sample strobe (divclk); not named *_valid: Vivado infers an ADI fifo_rd bus from that

    input  wire [11:0] s_axi_awaddr,
    input  wire        s_axi_awvalid,
    output wire        s_axi_awready,
    input  wire [31:0] s_axi_wdata,
    input  wire [3:0]  s_axi_wstrb,
    input  wire        s_axi_wvalid,
    output wire        s_axi_wready,
    output wire [1:0]  s_axi_bresp,
    output wire        s_axi_bvalid,
    input  wire        s_axi_bready,
    input  wire [11:0] s_axi_araddr,
    input  wire        s_axi_arvalid,
    output wire        s_axi_arready,
    output wire [31:0] s_axi_rdata,
    output wire [1:0]  s_axi_rresp,
    output wire        s_axi_rvalid,
    input  wire        s_axi_rready,

    output wire        irq
);
    localparam [31:0] ID_VALUE     = 32'h47544231;
    localparam [4:0]  MARK_DEFAULT = 5'd20;
`ifdef GT_SIM
    localparam [28:0] PRESENT_TICKS = 29'd2_000_000;     // 10 ms (simulation only)
`else
    localparam [28:0] PRESENT_TICKS = 29'd300_000_000;   // 1.5 s at 200 MHz
`endif

    // ------------------------------------------------------------------
    // timebase clock
    // ------------------------------------------------------------------
    wire tb_clk, mmcm_locked;
    gt_clk u_clk (.clk40(clk40), .tb_clk(tb_clk), .locked(mmcm_locked));

    // ==================================================================
    // sys domain: AXI-Lite, registers
    // ==================================================================
    wire        wr_en, rd_fire;
    wire [5:0]  wr_idx, rd_idx;
    wire [31:0] wr_data;
    wire [3:0]  wr_strb;
    reg  [31:0] rd_data;

    gt_axil u_axil (
        .clk(clk_sys), .rst_n(rst_sys_n),
        .awaddr(s_axi_awaddr), .awvalid(s_axi_awvalid), .awready(s_axi_awready),
        .wdata(s_axi_wdata), .wstrb(s_axi_wstrb), .wvalid(s_axi_wvalid), .wready(s_axi_wready),
        .bresp(s_axi_bresp), .bvalid(s_axi_bvalid), .bready(s_axi_bready),
        .araddr(s_axi_araddr), .arvalid(s_axi_arvalid), .arready(s_axi_arready),
        .rdata(s_axi_rdata), .rresp(s_axi_rresp), .rvalid(s_axi_rvalid), .rready(s_axi_rready),
        .wr_en(wr_en), .wr_idx(wr_idx), .wr_data(wr_data), .wr_strb(wr_strb),
        .rd_fire(rd_fire), .rd_idx(rd_idx), .rd_data(rd_data));

    // control
    reg [4:0] mark_log2_s = MARK_DEFAULT;
    reg       clr_tog_s   = 1'b0;     // toggles -> clk_div clears the counters
    reg       snap_tog_s  = 1'b0;     // toggles -> tb_clk and clk_div snapshot
    reg [2:0] irq_pend    = 3'b000;
    reg [2:0] irq_en      = 3'b000;
    wire      snap_busy;
    wire      wr_ctrl = wr_en && (wr_idx == 6'd1);
    wire      wr_irq  = wr_en && (wr_idx == 6'd3);

    // events from the other domains (sys-side, defined below)
    wire ev_ppsin, ev_ppsin_samp, ev_mark, ev_snap_done;

    always @(posedge clk_sys) begin
        if (!rst_sys_n) begin
            mark_log2_s <= MARK_DEFAULT;
            clr_tog_s   <= 1'b0;
            snap_tog_s  <= 1'b0;
            irq_pend    <= 3'b000;
            irq_en      <= 3'b000;
        end else begin
            if (wr_ctrl && wr_strb[0] && wr_data[4]) clr_tog_s <= ~clr_tog_s;
            if (wr_ctrl && wr_strb[0] && wr_data[5] && !snap_busy) snap_tog_s <= ~snap_tog_s;
            if (wr_ctrl && wr_strb[1]) mark_log2_s <= wr_data[12:8];
            if (wr_irq && wr_strb[2]) irq_en <= wr_data[18:16];
            // set has priority over clear
            irq_pend <= (irq_pend & ~((wr_irq && wr_strb[0]) ? wr_data[2:0] : 3'b000))
                      | {ev_snap_done, ev_mark, ev_ppsin};
        end
    end
    assign irq = |(irq_pend & irq_en);

    // ------------------------------------------------------------------
    // sys <- tb / div status levels (2-FF)
    // ------------------------------------------------------------------
    (* ASYNC_REG = "TRUE" *) reg [1:0] lock_s = 2'b00, pres_s = 2'b00;
    wire present_tb;
    always @(posedge clk_sys) begin
        lock_s <= {lock_s[0], mmcm_locked};
        pres_s <= {pres_s[0], present_tb};
    end

    // ==================================================================
    // tb_clk domain
    // ==================================================================
    // reset: asynchronous assert on lock loss, synchronous release
    (* ASYNC_REG = "TRUE" *) reg [2:0] rst_tb_sr = 3'b000;
    always @(posedge tb_clk or negedge mmcm_locked) begin
        if (!mmcm_locked) rst_tb_sr <= 3'b000;
        else              rst_tb_sr <= {rst_tb_sr[1:0], 1'b1};
    end
    wire tb_rst_n = rst_tb_sr[2];

    reg [47:0] tb_cnt = 48'd0;
    always @(posedge tb_clk) begin
        if (!tb_rst_n) tb_cnt <= 48'd0;
        else           tb_cnt <= tb_cnt + 48'd1;
    end

    // PPS_IN capture
    (* ASYNC_REG = "TRUE" *) reg [1:0] pps_tb_s = 2'b00;
    reg        pps_tb_d = 1'b0;
    reg [15:0] pps_tb_lock = 16'd0;
    reg [47:0] ppsin_tb_hold = 48'd0;
    reg        ppsin_tb_tog = 1'b0;
    reg [28:0] wd_cnt = PRESENT_TICKS;
    wire       pps_tb_rise = pps_tb_s[1] & ~pps_tb_d;
    always @(posedge tb_clk) begin
        pps_tb_s <= {pps_tb_s[0], pps_in};
        pps_tb_d <= pps_tb_s[1];
        if (!tb_rst_n) begin
            pps_tb_lock <= 16'd0; ppsin_tb_hold <= 48'd0; ppsin_tb_tog <= 1'b0;
            wd_cnt <= PRESENT_TICKS;
        end else begin
            if (pps_tb_lock != 16'd0) pps_tb_lock <= pps_tb_lock - 16'd1;
            if (pps_tb_rise && pps_tb_lock == 16'd0) begin
                pps_tb_lock   <= 16'hFFFF;
                ppsin_tb_hold <= tb_cnt;
                ppsin_tb_tog  <= ~ppsin_tb_tog;
                wd_cnt        <= 29'd0;
            end else if (wd_cnt != PRESENT_TICKS) begin
                wd_cnt <= wd_cnt + 29'd1;
            end
        end
    end
    // registered before the sys-domain synchroniser (no logic ahead of a CDC)
    reg present_r = 1'b0;
    always @(posedge tb_clk) present_r <= tb_rst_n && (wd_cnt != PRESENT_TICKS);
    assign present_tb = present_r;

    // mark capture (toggle from clk_div, sample count hold from clk_div)
    wire        mark_div_tog;
    wire [63:0] mark_samp_div_hold;
    (* ASYNC_REG = "TRUE" *) reg [1:0] mark_tb_s = 2'b00;
    reg         mark_tb_d = 1'b0;
    reg [47:0]  mark_tb_hold = 48'd0;
    reg [63:0]  mark_samp_tb_hold = 64'd0;
    reg         mark_tb_tog = 1'b0;
    always @(posedge tb_clk) begin
        mark_tb_s <= {mark_tb_s[0], mark_div_tog};
        mark_tb_d <= mark_tb_s[1];
        if (!tb_rst_n) begin
            mark_tb_hold <= 48'd0; mark_samp_tb_hold <= 64'd0; mark_tb_tog <= 1'b0;
        end else if (mark_tb_s[1] ^ mark_tb_d) begin
            mark_tb_hold      <= tb_cnt;
            mark_samp_tb_hold <= mark_samp_div_hold;
            mark_tb_tog       <= ~mark_tb_tog;
        end
    end

    // snapshot (request toggle from sys)
    (* ASYNC_REG = "TRUE" *) reg [1:0] snap_tb_s = 2'b00;
    reg [47:0] now_tb_hold = 48'd0;
    reg        snap_tb_ack = 1'b0;
    always @(posedge tb_clk) begin
        snap_tb_s <= {snap_tb_s[0], snap_tog_s};
        if (!tb_rst_n) begin
            now_tb_hold <= 48'd0; snap_tb_ack <= 1'b0;
        end else if (snap_tb_s[1] != snap_tb_ack) begin
            now_tb_hold <= tb_cnt;
            snap_tb_ack <= snap_tb_s[1];
        end
    end

    // ==================================================================
    // clk_div domain
    // ==================================================================
    // control crossings
    (* ASYNC_REG = "TRUE" *) reg [1:0] clr_div_s = 2'b00;
    reg clr_div_d = 1'b0;
    (* ASYNC_REG = "TRUE" *) reg [1:0] snap_div_s = 2'b00;
    (* ASYNC_REG = "TRUE" *) reg [1:0] ml_div_s [0:4];
    reg [4:0] ml_div;
    integer mi;
    initial for (mi = 0; mi < 5; mi = mi + 1) ml_div_s[mi] = 2'b00;
    always @(posedge clk_div) begin
        clr_div_s  <= {clr_div_s[0], clr_tog_s};
        clr_div_d  <= clr_div_s[1];
        snap_div_s <= {snap_div_s[0], snap_tog_s};
        for (mi = 0; mi < 5; mi = mi + 1) ml_div_s[mi] <= {ml_div_s[mi][0], mark_log2_s[mi]};
        ml_div <= {ml_div_s[4][1], ml_div_s[3][1], ml_div_s[2][1], ml_div_s[1][1], ml_div_s[0][1]};
    end
    wire clr_div = clr_div_s[1] ^ clr_div_d;
    // clamp 10..27
    wire [4:0] ml_eff = (ml_div < 5'd10) ? 5'd10 : ((ml_div > 5'd27) ? 5'd27 : ml_div);

    reg [63:0] samp_cnt = 64'd0;
    reg [27:0] mk_cnt = 28'd0;
    reg        mark_div_tog_r = 1'b0;
    reg [63:0] mark_samp_hold = 64'd0;
    wire [63:0] samp_nx = samp_cnt + 64'd1;
    wire [27:0] mk_nx   = mk_cnt + 28'd1;
    wire        mk_hit  = |(mk_nx & (28'd1 << ml_eff));
    always @(posedge clk_div) begin
        if (!rst_div_n || clr_div) begin
            samp_cnt <= 64'd0; mk_cnt <= 28'd0;
            if (!rst_div_n) begin mark_div_tog_r <= 1'b0; mark_samp_hold <= 64'd0; end
        end else if (rx_stb) begin
            samp_cnt <= samp_nx;
            if (mk_hit) begin
                mk_cnt <= 28'd0;
                mark_samp_hold <= samp_nx;
                mark_div_tog_r <= ~mark_div_tog_r;
            end else begin
                mk_cnt <= mk_nx;
            end
        end
    end
    assign mark_div_tog       = mark_div_tog_r;
    assign mark_samp_div_hold = mark_samp_hold;

    // PPS_IN sample-index capture
    (* ASYNC_REG = "TRUE" *) reg [1:0] pps_div_s = 2'b00;
    reg        pps_div_d = 1'b0;
    reg [11:0] pps_div_lock = 12'd0;
    reg [63:0] ppsin_samp_hold = 64'd0;
    reg        ppsin_samp_tog = 1'b0;
    always @(posedge clk_div) begin
        pps_div_s <= {pps_div_s[0], pps_in};
        pps_div_d <= pps_div_s[1];
        if (!rst_div_n) begin
            pps_div_lock <= 12'd0; ppsin_samp_hold <= 64'd0; ppsin_samp_tog <= 1'b0;
        end else begin
            if (pps_div_lock != 12'd0) pps_div_lock <= pps_div_lock - 12'd1;
            if (pps_div_s[1] & ~pps_div_d && pps_div_lock == 12'd0) begin
                pps_div_lock    <= 12'hFFF;
                ppsin_samp_hold <= samp_cnt;
                ppsin_samp_tog  <= ~ppsin_samp_tog;
            end
        end
    end

    // snapshot of the sample counter
    reg [63:0] now_samp_hold = 64'd0;
    reg        snap_div_ack = 1'b0;
    always @(posedge clk_div) begin
        if (!rst_div_n) begin
            now_samp_hold <= 64'd0; snap_div_ack <= 1'b0;
        end else if (snap_div_s[1] != snap_div_ack) begin
            now_samp_hold <= samp_cnt;
            snap_div_ack  <= snap_div_s[1];
        end
    end

    // ==================================================================
    // back in the sys domain: event capture
    // ==================================================================
    (* ASYNC_REG = "TRUE" *) reg [1:0] pt_s = 2'b00, mt_s = 2'b00, ps_s = 2'b00, sa_tb_s = 2'b00, sa_div_s = 2'b00;
    reg pt_d = 1'b0, mt_d = 1'b0, ps_d = 1'b0;
    reg [47:0] ppsin_tb_s = 48'd0, mark_tb_s_r = 48'd0, now_tb_s = 48'd0;
    reg [63:0] ppsin_samp_s = 64'd0, mark_samp_s = 64'd0, now_samp_s = 64'd0;
    reg [15:0] ppsin_seq = 16'd0, mark_seq = 16'd0, ppsin_samp_seq = 16'd0;
    reg        snap_pend = 1'b0;

    assign ev_ppsin      = pt_s[1] ^ pt_d;
    assign ev_mark       = mt_s[1] ^ mt_d;
    assign ev_ppsin_samp = ps_s[1] ^ ps_d;
    wire   snap_done_lvl = (sa_tb_s[1] == snap_tog_s) && (sa_div_s[1] == snap_tog_s);
    assign ev_snap_done  = snap_pend && snap_done_lvl;
    assign snap_busy     = snap_pend;

    always @(posedge clk_sys) begin
        pt_s    <= {pt_s[0], ppsin_tb_tog};
        mt_s    <= {mt_s[0], mark_tb_tog};
        ps_s    <= {ps_s[0], ppsin_samp_tog};
        sa_tb_s <= {sa_tb_s[0], snap_tb_ack};
        sa_div_s <= {sa_div_s[0], snap_div_ack};
        pt_d    <= pt_s[1];
        mt_d    <= mt_s[1];
        ps_d    <= ps_s[1];
        if (!rst_sys_n) begin
            ppsin_tb_s <= 48'd0; mark_tb_s_r <= 48'd0; now_tb_s <= 48'd0;
            ppsin_samp_s <= 64'd0; mark_samp_s <= 64'd0; now_samp_s <= 64'd0;
            ppsin_seq <= 16'd0; mark_seq <= 16'd0; ppsin_samp_seq <= 16'd0;
            snap_pend <= 1'b0;
        end else begin
            if (ev_ppsin) begin ppsin_tb_s <= ppsin_tb_hold; ppsin_seq <= ppsin_seq + 16'd1; end
            if (ev_ppsin_samp) begin ppsin_samp_s <= ppsin_samp_hold; ppsin_samp_seq <= ppsin_samp_seq + 16'd1; end
            if (ev_mark) begin
                mark_tb_s_r <= mark_tb_hold; mark_samp_s <= mark_samp_tb_hold;
                mark_seq <= mark_seq + 16'd1;
            end
            if (wr_ctrl && wr_strb[0] && wr_data[5] && !snap_pend) snap_pend <= 1'b1;
            if (ev_snap_done) begin
                snap_pend <= 1'b0; now_tb_s <= now_tb_hold; now_samp_s <= now_samp_hold;
            end
        end
    end

    // ------------------------------------------------------------------
    // read mux with coherent 64-bit pairs (LO read latches HI)
    // ------------------------------------------------------------------
    reg [31:0] hi_sh [0:5];
    integer hi;
    initial for (hi = 0; hi < 6; hi = hi + 1) hi_sh[hi] = 32'd0;
    wire [31:0] hi_cur [0:5];
    assign hi_cur[0] = {16'd0, now_tb_s[47:32]};
    assign hi_cur[1] = {16'd0, ppsin_tb_s[47:32]};
    assign hi_cur[2] = ppsin_samp_s[63:32];
    assign hi_cur[3] = {16'd0, mark_tb_s_r[47:32]};
    assign hi_cur[4] = mark_samp_s[63:32];
    assign hi_cur[5] = now_samp_s[63:32];
    wire lo_rd = rd_fire && rd_idx >= 6'd4 && rd_idx <= 6'd14 && !rd_idx[0];
    wire [2:0] pair_rd = (rd_idx - 6'd4) >> 1;
    always @(posedge clk_sys) begin
        if (lo_rd) hi_sh[pair_rd] <= hi_cur[pair_rd];
    end

    always @* begin
        case (rd_idx)
            6'd0:  rd_data = ID_VALUE;
            6'd1:  rd_data = {19'd0, mark_log2_s, 8'd0};
            6'd2:  rd_data = {29'd0, snap_busy, pres_s[1], lock_s[1]};
            6'd3:  rd_data = {13'd0, irq_en, 13'd0, irq_pend};
            6'd4:  rd_data = now_tb_s[31:0];
            6'd5:  rd_data = hi_sh[0];
            6'd6:  rd_data = ppsin_tb_s[31:0];
            6'd7:  rd_data = hi_sh[1];
            6'd8:  rd_data = ppsin_samp_s[31:0];
            6'd9:  rd_data = hi_sh[2];
            6'd10: rd_data = mark_tb_s_r[31:0];
            6'd11: rd_data = hi_sh[3];
            6'd12: rd_data = mark_samp_s[31:0];
            6'd13: rd_data = hi_sh[4];
            6'd14: rd_data = now_samp_s[31:0];
            6'd15: rd_data = hi_sh[5];
            6'd16: rd_data = {mark_seq, ppsin_seq};
            6'd17: rd_data = {16'd0, 3'd0, MARK_DEFAULT, 8'd200};
            6'd18: rd_data = {16'd0, ppsin_samp_seq};
            default: rd_data = 32'd0;
        endcase
    end
endmodule
