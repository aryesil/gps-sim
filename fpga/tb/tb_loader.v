// tb_loader - gs_loader against fpga/vec/<CASE>_load*.hex (scripts/fpga_loader_vectors.py).
// Defines: CASE "gce12" (string, default), INJ=1 injects junk words before the
// second BLOCK record (expects bad_words==6), GS_P (also 5 with GS_SLOT_W=4 and
// VSUF "_p5"), GS_CMD_DEPTH (small depth: expect cmd_ready to drop when stalled).
`timescale 1ns/1ps
`include "gs_defs.vh"
// CASEN: 0 gce12, 1 gapcase, 2 latecase, 3 maxch (quotes do not survive -d)
`ifndef CASEN
`define CASEN 0
`endif
`ifndef INJ
`define INJ 0
`endif
`ifndef GS_VECDIR
`define GS_VECDIR "../../../vec/"
`endif
`ifndef GS_CMD_DEPTH
`define GS_CMD_DEPTH 4096
`endif
`ifndef GS_SLOT_W
`define GS_SLOT_W 3
`endif

module tb_loader;
  localparam TOTAL = `GS_P * `GS_SLOTS;
  localparam MAXW = 4096;
  reg clk = 0, rst = 1;
  always #2 clk = ~clk;

  reg [31:0] cmd [0:MAXW-1];
  reg [31:0] stream [0:MAXW+31];
  reg [331:0] expm [0:1023];
  reg [31:0] info [0:7];
  integer ncmd, nstream, nexp;

  reg cmd_valid = 0; reg [31:0] cmd_data = 0; wire cmd_ready;
  reg [63:0] target_idx = 0; reg shadow_bank = 1; reg load_enable = 1;
  wire l_we; wire [3:0] l_pipe; wire [`GS_SLOT_W-1:0] l_slot; wire l_bank;
  wire [`GS_PAR_W-1:0] l_par; wire [`GS_ST_W-1:0] l_st;
  wire shadow_dirty, shadow_loaded, cfg_valid;
  wire [6:0] cfg_nch; wire [31:0] cfg_fs, cfg_shift; wire [63:0] cfg_start;
  wire [31:0] dropped_late, bad_words; wire [15:0] fifo_level;

  gs_loader dut (.clk(clk), .rst(rst), .cmd_valid(cmd_valid), .cmd_data(cmd_data),
    .cmd_ready(cmd_ready), .target_idx(target_idx), .shadow_bank(shadow_bank),
    .load_enable(load_enable), .l_we(l_we), .l_pipe(l_pipe), .l_slot(l_slot),
    .l_bank(l_bank), .l_par(l_par), .l_st(l_st), .shadow_dirty(shadow_dirty),
    .shadow_loaded(shadow_loaded), .cfg_valid(cfg_valid), .cfg_nch(cfg_nch),
    .cfg_fs(cfg_fs), .cfg_shift(cfg_shift), .cfg_start(cfg_start),
    .dropped_late(dropped_late), .bad_words(bad_words), .fifo_level(fifo_level));

  integer errs = 0, nw = 0, i, k, seed = 12345;
  integer blocks_loaded = 0, sent_done = 0, stub_done = 0;
  integer not_ready_seen = 0, quiet = 0, wait_cnt = 0, hold = 0, stall_w0 = 0;
  integer n_dirty = 0, last_we_cycle = 0, cycle = 0;
  reg [331:0] got;
  reg prev_loaded = 0;
  integer inj_at, p, rl;
  reg [8*8-1:0] cname; reg [8*3-1:0] vsuf;

  task err; input [255:0] msg; begin
    errs = errs + 1;
    if (errs < 12) $display("mismatch: %0s at write %0d t=%0t", msg, nw, $time);
  end endtask

  // ------------------------------------------------------------- stream setup
  initial begin
    for (i = 0; i < MAXW; i = i + 1) cmd[i] = 32'bx;
    case (`CASEN)
      1: cname = "gapcase"; 2: cname = "latecase"; 3: cname = "maxch"; default: cname = "gce12";
    endcase
    vsuf = (`GS_P == 12) ? "" : "_p5";
    $readmemh({`GS_VECDIR, cname, "_cmd.hex"}, cmd);
    ncmd = 0; while (ncmd < MAXW && ^cmd[ncmd] !== 1'bx) ncmd = ncmd + 1;
    $readmemh({`GS_VECDIR, cname, "_load", vsuf, ".hex"}, expm);
    $readmemh({`GS_VECDIR, cname, "_loadinfo", vsuf, ".hex"}, info);
    nexp = info[5];
    // find the second BLOCK record
    inj_at = -1; p = 0; k = 0;
    while (p < ncmd) begin
      rl = cmd[p] & 16'hFFFF;
      if (cmd[p][23:16] == 2) begin
        k = k + 1;
        if (k == 2) inj_at = p;
      end
      p = p + rl;
    end
    nstream = 0;
    for (i = 0; i < ncmd; i = i + 1) begin
      if (`INJ && i == inj_at) begin
        stream[nstream] = 32'h00000001; nstream = nstream + 1;
        stream[nstream] = 32'hDEADBEEF; nstream = nstream + 1;
        stream[nstream] = 32'hA5030006; nstream = nstream + 1;
        stream[nstream] = 32'hA5010007; nstream = nstream + 1;
        stream[nstream] = 32'hA5010006; nstream = nstream + 1;
        stream[nstream] = 32'hA5020008; nstream = nstream + 1;
      end
      stream[nstream] = cmd[i]; nstream = nstream + 1;
    end
    repeat (5) @(posedge clk);
    rst <= 0;
  end

  // ----------------------------------------------------------- command driver
  integer wi_d = 0;
  always @(posedge clk) if (!rst && !sent_done) begin
    if (!cmd_valid || cmd_ready) begin
      if (cmd_valid && cmd_ready) wi_d = wi_d + 1;
      if (wi_d >= nstream) begin cmd_valid <= 0; sent_done <= 1; end
      else if ($random(seed) & 1) begin cmd_valid <= 1; cmd_data <= stream[wi_d]; end
      else cmd_valid <= 0;
    end
  end
  always @(posedge clk) if (!cmd_ready && cmd_valid) not_ready_seen <= 1;

  // ------------------------------------------------------------ stub sequencer
  // target starts at cfg_start; after shadow_loaded + 40 clocks target++ and the
  // bank flips, load_enable low 16 clocks (SWAP_QUIET). Case gapcase: at
  // target==2 hold 3000 clocks and check nothing is written.
  reg started = 0;
  always @(posedge clk) begin
    cycle <= cycle + 1;
    if (rst) begin started <= 0; quiet <= 0; wait_cnt <= 0; hold <= 0; end
    else begin
      if (cfg_valid && !started) begin started <= 1; target_idx <= cfg_start; end
      if (quiet > 0) quiet <= quiet - 1;
      load_enable <= (quiet <= 1) && (($random(seed) & 3) != 0);
      if (started && shadow_loaded) begin
        wait_cnt <= wait_cnt + 1;
        if (wait_cnt == 40) begin
          target_idx <= target_idx + 1; shadow_bank <= ~shadow_bank;
          quiet <= 16; wait_cnt <= 0; blocks_loaded <= blocks_loaded + 1;
          if (blocks_loaded + 1 >= info[7]) stub_done <= 1;
        end
      end else wait_cnt <= 0;
    end
  end

  // ------------------------------------------------------------------ checkers
  always @(posedge clk) if (!rst) begin
    if (l_we) begin
      got = 0;
      got[331:328] = l_pipe; got[324 +: `GS_SLOT_W] = l_slot;
      got[148 +: `GS_PAR_W] = l_par; got[0 +: `GS_ST_W] = l_st; if (l_par[0]) got[31:0] = l_st[31:0] - 32'h0008_0000;  // undo the +2^19 offset (valid records only)
      if (nw >= nexp) err("extra write");
      else if (got !== expm[nw]) begin
        err("record");
        if (errs < 4) $display("  got %h\n  exp %h", got, expm[nw]);
      end
      if (l_bank !== shadow_bank) err("bank");
      if (shadow_loaded) err("write while loaded");
      nw = nw + 1;
      last_we_cycle = cycle;
    end
    // shadow_dirty must coincide with l_we
    if (l_we !== shadow_dirty) err("dirty/we");
    // loaded rises only after all writes of the block are in
    if (shadow_loaded && !prev_loaded) begin
      if (nw != TOTAL * (blocks_loaded + 1)) err("loaded early");
      if (cycle - last_we_cycle < 4) err("loaded too soon");
    end
    prev_loaded <= shadow_loaded;
  end

  // --------------------------------------------------------------- main script
  integer to;
  initial begin
    wait (!rst);
    // gapcase style stall check: wait until blocks_loaded == 2 (target 2)
    if (`CASEN == 1) begin
      wait (blocks_loaded == 2);
      repeat (50) @(posedge clk);
      stall_w0 = nw;
      repeat (3000) @(posedge clk);
      if (nw != stall_w0) err("write during stall");
      if (shadow_loaded) err("loaded during stall");
      if (`GS_CMD_DEPTH <= 256 && !not_ready_seen) err("cmd_ready never dropped");
      if (`GS_CMD_DEPTH <= 256 && sent_done) err("stream fully accepted during stall");
      if (dropped_late != 0) err("dropped during stall");
      // let it proceed: target 3
      target_idx <= 3; shadow_bank <= ~shadow_bank; quiet <= 16; wait_cnt <= 0;
      repeat (2) @(posedge clk);
    end
    to = 0;
    while (!(stub_done && sent_done) && to < 200000) begin @(posedge clk); to = to + 1; end
    repeat (300) @(posedge clk);
    if (to >= 200000) err("timeout");
    if (nw != nexp) begin errs = errs + 1; $display("write count %0d expected %0d", nw, nexp); end
    if (dropped_late != info[6]) begin errs = errs + 1; $display("dropped_late %0d expected %0d", dropped_late, info[6]); end
    if (bad_words != (`INJ ? 6 : 0)) begin errs = errs + 1; $display("bad_words %0d", bad_words); end
    if (cfg_nch != info[0] || cfg_fs != info[1] || cfg_shift != info[2] ||
        cfg_start != {info[4], info[3]} || !cfg_valid) begin errs = errs + 1; $display("cfg mismatch"); end
    if (fifo_level != 0) begin errs = errs + 1; $display("fifo_level %0d", fifo_level); end
    if (errs == 0) $display("PASS"); else $display("FAIL %0d", errs);
    $finish;
  end
endmodule
