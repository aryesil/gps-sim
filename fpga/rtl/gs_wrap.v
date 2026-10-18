// gs_wrap.v - gnss_synth integration wrapper for the tezuka_gnss block design.
//
// Contains gs_top (engine clock), the AXI-Lite register file (clk_sys), the
// command async FIFO (clk_div -> clk_eng), the sample async FIFO
// (clk_eng -> clk_div) and the command-mode mux on the TX path.  No module
// parameters (module reference in the BD); configuration by localparam.
//
// Register map (offsets in the 0x1000 window, 32-bit):
//   0x00 ID                RO  0x47530001
//   0x04 CTRL              RW  b0 enable (engine run), b1 cmd_mode,
//                              b2 soft_reset (write 1; reads 1 while busy),
//                              b3 clear_counters (write 1, self-clearing)
//   0x08 STATUS            RO  b0 running (engine), b1 cmd_fifo_ok (cmd FIFO
//                              below almost-full, divclk view), b2 cmd_mode as
//                              seen in divclk, b3 playing (sample FIFO prefilled),
//                              b4 cmd FIFO empty (engine view), b5 sample FIFO
//                              empty (divclk view), b6 cmd FIFO full seen,
//                              [31:16] queued command words = async cmd FIFO
//                              level + gs_top internal FIFO level
//   0x10/0x14 PLAY_IDX LO/HI   RO  64-bit block index (blocks of 65536 samples). Not coherent: read
//                              HI, LO, HI and retry if HI differs.
//   0x18 UNDERRUNS         RO  (engine counters, relative to last clear)
//   0x1C DROPPED_LATE, 0x20 CLIPPED, 0x24 CHIP_LATE, 0x28 BAD_WORDS
//   0x2C SAMPLE_FIFO_UNDERFLOWS  RO  divclk, counted only after prefill
//   0x30 SAMPLE_FIFO_LEVEL RO  divclk-side level (sample FIFO read view)
//   0x34 CMD_FIFO_OVERRUNS RO  divclk, words pushed while full (should be 0)
// clear_counters zeroes 0x18..0x2C and 0x34 as seen by the host (a baseline
// snapshot is subtracted in the clk_sys domain; PLAY_IDX is not affected).
//
// Behaviour
//   cmd_mode_div = 0: up_rd_en = dm_req_out, tx_* = dm_*, engine idle.
//   cmd_mode_div = 1: upack words (up_valid) are pushed into the command FIFO;
//     up_rd_en = command FIFO not almost-full (32 words of margin cover the one
//     clock read latency of upack); gs_top reads the FIFO; gs_top samples go to
//     the sample FIFO (1024 x 32); gs_top.out_space = at least 64 free entries
//     (from the write-side level, which can only over-estimate occupancy).
//     On dac_req one sample is popped; tx_valid0/tx_i0/tx_q0 are registered
//     (one divclk after dac_req).  Before the sample FIFO has reached half
//     full ("prefill") after enable the output is zero and nothing is counted
//     (if enable is low and samples are present they are played out at once);
//     afterwards an empty FIFO with enable still set outputs zero and counts
//     one underflow.  When enable drops, the remaining samples are still played
//     out; playing then clears when the FIFO is empty.
//   Mode changes / restarts: use soft_reset (flushes both FIFOs and gs_top).
//   irq is tied low (no interrupt source defined in the register map).
//
// Clock-domain crossings (all single-bit levels use a 2-FF synchroniser with
// ASYNC_REG; multi-bit data crosses only through gs_afifo gray pointers or a
// toggle handshake):
//   1. clk_sys -> clk_div / clk_eng : ctl_enable, ctl_cmd_mode (levels, 2-FF).
//   2. clk_sys -> clk_div / clk_eng : soft_req level (2-FF); each domain turns
//      it into its local reset; the local reset level returns to clk_sys by
//      2-FF (ack).  soft_req is dropped when both acks were seen at the same
//      time (both sides in reset together) or after a 2**20 clk_sys timeout.
//   3. clk_div <-> clk_eng : gs_afifo cmd (div->eng) and sample (eng->div),
//      gray pointers, 2-FF.
//   4. clk_eng -> clk_sys, clk_div -> clk_sys : status snapshots (gs_snap):
//      sys toggles/levels a request, 2-FF in the source domain, the source
//      copies its (synchronous) status into a hold register and returns the
//      request level as ack, 2-FF in sys, then sys copies the hold register
//      (stable until the next request).  Sampled data are multi-cycle paths;
//      constrain with set_max_delay -datapath_only or clock groups.
// Resets: rst_*_n are synchronous-use, active low, expected asserted together;
// the register file works without clk_eng/clk_div (snapshots just stall).

`include "gs_defs.vh"

module gs_wrap (
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 clk_sys CLK", X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF s_axi, ASSOCIATED_RESET rst_sys_n" *)
    input  wire        clk_sys,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 rst_sys_n RST", X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
    input  wire        rst_sys_n,
    input  wire        clk_div,
    input  wire        rst_div_n,
    input  wire        clk_eng,
    input  wire        rst_eng_n,

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

    input  wire [31:0] up_data,
    input  wire        up_valid,
    output wire        up_rd_en,
    input  wire        dm_req_out,
    input  wire        dm_valid_out,
    input  wire [15:0] dm_i0,
    input  wire [15:0] dm_q0,
    input  wire        dac_req,
    output wire        tx_valid0,
    output wire [15:0] tx_i0,
    output wire [15:0] tx_q0,
    output wire        cmd_mode_div,
    output wire        irq
);
    localparam CMD_AW    = 12;     // 4096 words
    localparam SMP_AW    = 10;     // 1024 samples
    localparam SMP_DEPTH = 1024;
    localparam PREFILL   = 512;
    localparam SPACE_MIN = 64;

    assign irq = 1'b0;

    // ------------------------------------------------------------------
    // sys domain: AXI-Lite, control, soft reset machine
    // ------------------------------------------------------------------
    wire        ctl_enable, ctl_cmd_mode, soft_pulse, clear_pulse;
    wire [511:0] rd_regs;
    wire        soft_busy;

    gs_axil u_axil (
        .clk(clk_sys), .rst_n(rst_sys_n),
        .awaddr(s_axi_awaddr), .awvalid(s_axi_awvalid), .awready(s_axi_awready),
        .wdata(s_axi_wdata), .wstrb(s_axi_wstrb), .wvalid(s_axi_wvalid), .wready(s_axi_wready),
        .bresp(s_axi_bresp), .bvalid(s_axi_bvalid), .bready(s_axi_bready),
        .araddr(s_axi_araddr), .arvalid(s_axi_arvalid), .arready(s_axi_arready),
        .rdata(s_axi_rdata), .rresp(s_axi_rresp), .rvalid(s_axi_rvalid), .rready(s_axi_rready),
        .ctl_enable(ctl_enable), .ctl_cmd_mode(ctl_cmd_mode),
        .soft_reset_pulse(soft_pulse), .clear_pulse(clear_pulse),
        .rd_regs(rd_regs));

    // soft reset request: level in sys, acked by the two local resets
    reg         soft_req = 1'b0;
    reg  [19:0] soft_to  = 20'd0;
    reg  [5:0]  soft_hold = 6'd0;
    (* ASYNC_REG = "TRUE" *) reg ack_div_s1 = 1'b0, ack_div_s2 = 1'b0;
    (* ASYNC_REG = "TRUE" *) reg ack_eng_s1 = 1'b0, ack_eng_s2 = 1'b0;
    wire        rdiv_q_w, reng_q_w;      // local reset levels (div / eng domain)
    always @(posedge clk_sys) begin
        ack_div_s1 <= rdiv_q_w; ack_div_s2 <= ack_div_s1;
        ack_eng_s1 <= reng_q_w; ack_eng_s2 <= ack_eng_s1;
    end
    always @(posedge clk_sys) begin
        if (!rst_sys_n) begin
            soft_req <= 1'b0; soft_to <= 20'd0; soft_hold <= 6'd0;
        end else if (!soft_req) begin
            soft_to <= 20'd0; soft_hold <= 6'd0;
            if (soft_pulse) soft_req <= 1'b1;
        end else begin
            soft_to <= soft_to + 20'd1;
            if (ack_div_s2 & ack_eng_s2) soft_hold <= soft_hold + 6'd1;
            else soft_hold <= 6'd0;
            // both sides in reset with zeroed pointers for >= 32 clk_sys, so the
            // FIFO pointer synchronisers of both domains are flushed
            if (soft_hold[5] | (&soft_to)) soft_req <= 1'b0;
        end
    end
    assign soft_busy = soft_req;

    // ------------------------------------------------------------------
    // div domain: reset, mode, cmd FIFO write side, sample FIFO read side
    // ------------------------------------------------------------------
    (* ASYNC_REG = "TRUE" *) reg [1:0] en_d_s = 2'b00, mode_d_s = 2'b00, srst_d_s = 2'b00;
    always @(posedge clk_div) begin
        en_d_s   <= {en_d_s[0],   ctl_enable};
        mode_d_s <= {mode_d_s[0], ctl_cmd_mode};
        srst_d_s <= {srst_d_s[0], soft_req};
    end
    reg rdiv_q = 1'b1;                       // active-high local reset
    reg reng_q = 1'b1;                       // active-high eng reset (eng domain)
    always @(posedge clk_div) rdiv_q <= ~rst_div_n | srst_d_s[1];
    reg rdiv_d = 1'b1;
    always @(posedge clk_div) rdiv_d <= rdiv_q;
    assign rdiv_q_w = rdiv_q & rdiv_d;   // pointers zeroed
    wire cmd_mode_d = mode_d_s[1];
    wire play_en_d  = en_d_s[1] & cmd_mode_d;
    assign cmd_mode_div = cmd_mode_d;

    // command FIFO (div -> eng)
    wire        cf_full, cf_afull, cf_rempty, cf_raempty;
    wire [CMD_AW:0] cf_wlevel, cf_rlevel;
    wire [31:0] cf_rdata;
    wire        cf_ren;
    wire        cf_push = cmd_mode_d & up_valid & ~rdiv_q;

    gs_afifo #(.W(32), .AW(CMD_AW), .AF_MARGIN(32), .AE_THR(0)) u_cmdf (
        .wclk(clk_div), .wrst_n(~rdiv_q), .wen(cf_push), .wdata(up_data),
        .wfull(cf_full), .w_almost_full(cf_afull), .w_level(cf_wlevel),
        .rclk(clk_eng), .rrst_n(~reng_q), .ren(cf_ren), .rdata(cf_rdata),
        .rempty(cf_rempty), .r_almost_empty(cf_raempty), .r_level(cf_rlevel));

    assign up_rd_en = cmd_mode_d ? (~cf_afull & ~cf_full & ~rdiv_q) : dm_req_out;

    reg [31:0] cmd_overruns = 32'd0;
    always @(posedge clk_div) begin
        if (rdiv_q) cmd_overruns <= 32'd0;
        else if (cf_push & cf_full) cmd_overruns <= cmd_overruns + 32'd1;
    end

    // sample FIFO (eng -> div), read side
    wire        sf_full, sf_afull, sf_rempty, sf_raempty;
    wire [SMP_AW:0] sf_wlevel, sf_rlevel;
    wire [31:0] sf_rdata;
    wire        sf_push;
    wire [31:0] sf_wdata;

    reg         playing = 1'b0;
    wire        pop     = dac_req & cmd_mode_d & playing & ~sf_rempty;
    wire        uf_pulse = dac_req & cmd_mode_d & playing & sf_rempty & play_en_d;
    reg         pop_q = 1'b0;                // tx word holds a real sample
    reg [31:0]  sample_underflows = 32'd0;
    reg         tx_v_r = 1'b0;
    reg [15:0]  tx_i_r = 16'd0, tx_q_r = 16'd0;

    gs_afifo #(.W(32), .AW(SMP_AW), .AF_MARGIN(16), .AE_THR(0)) u_smpf (
        .wclk(clk_eng), .wrst_n(~reng_q), .wen(sf_push), .wdata(sf_wdata),
        .wfull(sf_full), .w_almost_full(sf_afull), .w_level(sf_wlevel),
        .rclk(clk_div), .rrst_n(~rdiv_q), .ren(pop), .rdata(sf_rdata),
        .rempty(sf_rempty), .r_almost_empty(sf_raempty), .r_level(sf_rlevel));

    always @(posedge clk_div) begin
        if (rdiv_q) begin
            playing <= 1'b0; pop_q <= 1'b0; tx_v_r <= 1'b0;
            tx_i_r <= 16'd0; tx_q_r <= 16'd0; sample_underflows <= 32'd0;
        end else begin
            if (!cmd_mode_d) playing <= 1'b0;
            else if (!play_en_d && sf_rempty) playing <= 1'b0;
            else if ((play_en_d && sf_rlevel >= PREFILL) || (!play_en_d && !sf_rempty))
                playing <= 1'b1;   // prefill reached, or engine stopped: flush the rest
            pop_q  <= pop;
            tx_v_r <= dac_req & cmd_mode_d;
            tx_i_r <= pop ? sf_rdata[31:16] : 16'd0;
            tx_q_r <= pop ? sf_rdata[15:0]  : 16'd0;
            if (uf_pulse) sample_underflows <= sample_underflows + 32'd1;
        end
    end

    assign tx_valid0 = cmd_mode_d ? tx_v_r : dm_valid_out;
    assign tx_i0     = cmd_mode_d ? tx_i_r : dm_i0;
    assign tx_q0     = cmd_mode_d ? tx_q_r : dm_q0;

    // ------------------------------------------------------------------
    // eng domain: reset, gs_top, FIFO glue
    // ------------------------------------------------------------------
    (* ASYNC_REG = "TRUE" *) reg [1:0] en_e_s = 2'b00, mode_e_s = 2'b00, srst_e_s = 2'b00;
    always @(posedge clk_eng) begin
        en_e_s   <= {en_e_s[0],   ctl_enable};
        mode_e_s <= {mode_e_s[0], ctl_cmd_mode};
        srst_e_s <= {srst_e_s[0], soft_req};
    end
    always @(posedge clk_eng) reng_q <= ~rst_eng_n | srst_e_s[1];
    reg reng_d = 1'b1;
    always @(posedge clk_eng) reng_d <= reng_q;
    assign reng_q_w = reng_q & reng_d;
    wire enable_e = en_e_s[1] & mode_e_s[1];

    wire        top_cmd_ready, top_out_valid;
    wire [15:0] top_out_i, top_out_q;
    wire [63:0] st_play_idx;
    wire [31:0] st_underruns, st_dropped_late, st_clipped, st_chip_late, st_bad_words;
    wire        st_running;
    wire [15:0] st_cmd_level;
    reg         out_space_r = 1'b0;

    gs_top u_top (
        .clk(clk_eng), .rst(reng_q), .enable(enable_e),
        .cmd_valid(~cf_rempty & ~reng_q), .cmd_data(cf_rdata), .cmd_ready(top_cmd_ready),
        .out_space(out_space_r),
        .out_valid(top_out_valid), .out_i(top_out_i), .out_q(top_out_q),
        .st_play_idx(st_play_idx), .st_underruns(st_underruns),
        .st_dropped_late(st_dropped_late), .st_clipped(st_clipped),
        .st_chip_late(st_chip_late), .st_bad_words(st_bad_words),
        .st_running(st_running), .st_cmd_level(st_cmd_level));

    assign cf_ren  = top_cmd_ready & ~cf_rempty & ~reng_q;
    assign sf_push = top_out_valid & ~reng_q;
    assign sf_wdata = {top_out_i, top_out_q};

    always @(posedge clk_eng) begin
        if (reng_q) out_space_r <= 1'b0;
        else out_space_r <= ((SMP_DEPTH - sf_wlevel) >= SPACE_MIN) & ~sf_full;
    end

    // ------------------------------------------------------------------
    // status snapshots to clk_sys
    // ------------------------------------------------------------------
    reg [15:0] cmdq_sum = 16'd0;
    always @(posedge clk_eng) cmdq_sum <= {3'b000, cf_rlevel} + st_cmd_level;

    wire [31:0]  eng_misc_in = {cmdq_sum, 14'd0, cf_rempty, st_running};
    wire [255:0] eng_q;
    gs_snap #(.W(256)) u_snap_eng (
        .sclk(clk_sys), .srst(~rst_sys_n), .dclk(clk_eng),
        .d_in({eng_misc_in, st_play_idx, st_underruns, st_dropped_late,
               st_clipped, st_chip_late, st_bad_words}),
        .q_out(eng_q));
    wire [31:0]  e_bad   = eng_q[31:0];
    wire [31:0]  e_chip  = eng_q[63:32];
    wire [31:0]  e_clip  = eng_q[95:64];
    wire [31:0]  e_late  = eng_q[127:96];
    wire [31:0]  e_under = eng_q[159:128];
    wire [63:0]  e_play  = eng_q[223:160];
    wire [31:0]  e_misc  = eng_q[255:224];

    wire [31:0]  div_misc_in = {27'd0, cf_full, sf_rempty, playing, cmd_mode_d, ~cf_afull};
    wire [127:0] div_q;
    gs_snap #(.W(128)) u_snap_div (
        .sclk(clk_sys), .srst(~rst_sys_n), .dclk(clk_div),
        .d_in({div_misc_in, sample_underflows, cmd_overruns,
               {{(31-SMP_AW){1'b0}}, sf_rlevel}}),
        .q_out(div_q));
    wire [31:0]  d_lvl  = div_q[31:0];
    wire [31:0]  d_ov   = div_q[63:32];
    wire [31:0]  d_uf   = div_q[95:64];
    wire [31:0]  d_misc = div_q[127:96];

    // clear_counters baselines (clk_sys); zero while a soft reset runs
    reg [31:0] b_under = 32'd0, b_late = 32'd0, b_clip = 32'd0, b_chip = 32'd0;
    reg [31:0] b_bad = 32'd0, b_uf = 32'd0, b_ov = 32'd0;
    always @(posedge clk_sys) begin
        if (!rst_sys_n | soft_req) begin
            b_under <= 32'd0; b_late <= 32'd0; b_clip <= 32'd0; b_chip <= 32'd0;
            b_bad <= 32'd0; b_uf <= 32'd0; b_ov <= 32'd0;
        end else if (clear_pulse) begin
            b_under <= e_under; b_late <= e_late; b_clip <= e_clip; b_chip <= e_chip;
            b_bad <= e_bad; b_uf <= d_uf; b_ov <= d_ov;
        end
    end

    wire [31:0] status_w = {e_misc[31:16], 9'd0, d_misc[4], d_misc[3], e_misc[1],
                            d_misc[2], d_misc[1], d_misc[0], e_misc[0]};
    wire [31:0] ctrl_rb = {28'd0, 1'b0, soft_busy, ctl_cmd_mode, ctl_enable};
    assign rd_regs = {
        32'd0,                    // 0x3C
        32'd0,                    // 0x38
        d_ov - b_ov,              // 0x34 CMD_FIFO_OVERRUNS
        d_lvl,                    // 0x30 SAMPLE_FIFO_LEVEL
        d_uf - b_uf,              // 0x2C SAMPLE_FIFO_UNDERFLOWS
        e_bad - b_bad,            // 0x28 BAD_WORDS
        e_chip - b_chip,          // 0x24 CHIP_LATE
        e_clip - b_clip,          // 0x20 CLIPPED
        e_late - b_late,          // 0x1C DROPPED_LATE
        e_under - b_under,        // 0x18 UNDERRUNS
        e_play[63:32],            // 0x14 PLAY_IDX_HI
        e_play[31:0],             // 0x10 PLAY_IDX_LO
        32'd0,                    // 0x0C
        status_w,                 // 0x08 STATUS
        ctrl_rb,                  // 0x04 CTRL
        32'h47530001              // 0x00 ID
    };
endmodule

// gs_snap: toggle-free request/ack status snapshot from dclk to sclk.
module gs_snap #(
    parameter W = 32
) (
    input  wire         sclk,
    input  wire         srst,
    input  wire         dclk,
    input  wire [W-1:0] d_in,
    output reg  [W-1:0] q_out
);
    reg         req = 1'b0;
    reg         ack_t = 1'b0;
    reg [W-1:0] hold = {W{1'b0}};
    (* ASYNC_REG = "TRUE" *) reg [1:0] req_d = 2'b00;
    (* ASYNC_REG = "TRUE" *) reg [1:0] ack_s = 2'b00;
    initial q_out = {W{1'b0}};

    always @(posedge dclk) begin
        req_d <= {req_d[0], req};
        if (req_d[1] != ack_t) begin
            hold  <= d_in;
            ack_t <= req_d[1];
        end
    end
    always @(posedge sclk) begin
        ack_s <= {ack_s[0], ack_t};
        if (srst) begin
            req <= 1'b0; q_out <= {W{1'b0}};
        end else if (ack_s[1] == req) begin
            q_out <= hold;
            req   <= ~req;
        end
    end
endmodule
