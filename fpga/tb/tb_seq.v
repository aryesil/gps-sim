// tb_seq - gs_seq with a stub loaded/clean model. Run with -d GS_BLOCK_LOG2=6.
// -d NCH=n (default 12), -d GS_P=5 -d GS_SLOT_W=4 for P=5.
// Scenario: START=5; first block withheld 500 clocks (no start, no underrun);
// block START+2 is never loaded in time -> underrun + mute for that block.
`timescale 1ns/1ps
`include "gs_defs.vh"
`ifndef NCH
`define NCH 12
`endif
`ifndef GS_SLOT_W
`define GS_SLOT_W 3
`endif
`ifndef GS_BLOCK_LOG2
`define GS_BLOCK_LOG2 16
`endif
module tb_seq;
  localparam BL = 1 << `GS_BLOCK_LOG2;
  localparam START = 5;
  localparam TC = (`NCH + `GS_P - 1) / `GS_P;
  localparam T = (TC < 4) ? 4 : TC;
  reg clk = 0, rst = 1; always #2 clk = ~clk;
  reg enable = 1, cfg_valid = 0, out_space = 1;
  reg [6:0] cfg_nch = `NCH; reg [63:0] cfg_start = START;
  reg shadow_loaded = 0, shadow_clean = 0;
  wire [63:0] target_idx, play_idx; wire bank, shadow_bank, load_enable;
  wire s_valid, s_bank, s_first, s_last, s_mute, running;
  wire [`GS_SLOT_W-1:0] s_slot; wire [31:0] underruns;
  gs_seq dut (.clk(clk), .rst(rst), .enable(enable), .cfg_valid(cfg_valid), .cfg_nch(cfg_nch),
    .cfg_start(cfg_start), .shadow_loaded(shadow_loaded), .shadow_clean(shadow_clean),
    .out_space(out_space), .target_idx(target_idx), .bank(bank), .shadow_bank(shadow_bank),
    .load_enable(load_enable), .s_valid(s_valid), .s_slot(s_slot), .s_bank(s_bank),
    .s_first(s_first), .s_last(s_last), .s_mute(s_mute), .play_idx(play_idx),
    .underruns(underruns), .running(running));

  integer errs = 0, cycle = 0, seed = 7, release_at = 500;
  task err; input [255:0] m; begin errs = errs + 1;
    if (errs < 10) $display("mismatch: %0s cycle %0d", m, cycle); end endtask

  // ---- loaded/clean stub: loads target after 30 clocks, withholds START+2
  reg [63:0] tg_q = 0; integer ldc = 0, clc = 0;
  always @(posedge clk) begin
    cycle <= cycle + 1;
    tg_q <= target_idx;
    if (target_idx != tg_q) begin shadow_loaded <= 0; shadow_clean <= 0; ldc <= 0; clc <= 0; end
    else begin
      if (!shadow_loaded && cycle >= release_at && target_idx != START + 2) begin
        ldc <= ldc + 1; if (ldc == 30) shadow_loaded <= 1;
      end
      if (shadow_loaded && !shadow_clean) begin clc <= clc + 1; if (clc == 3) shadow_clean <= 1; end
    end
  end
  // out_space: random, plus a forced low window every 700 clocks
  always @(posedge clk) out_space <= ((cycle % 700) >= 150) && (($random(seed) & 3) != 0);

  // ---- monitor
  integer samples = 0, exp_slot = 0, blk, nt, k, qcnt = 0;
  reg in_sample = 0, osp_q = 1, bank_q = 0, qchk = 0, done = 0;
  always @(posedge clk) if (!rst) begin
    if (s_valid) begin
      exp_slot = in_sample ? exp_slot + 1 : 0;
      if (s_slot !== exp_slot) err("slot");
      if (s_first !== (exp_slot == 0)) err("first");
      if (s_last !== (exp_slot == T - 1)) err("last");
      if (exp_slot > T - 1) err("slot beyond T");
      in_sample = !s_last;
      if (s_first) begin
        if (!osp_q) err("start without out_space");
        if (!running) err("not running at start");
        blk = samples / BL;
        if (samples % BL == 0 && play_idx != START + blk) err("play_idx");
        if (s_mute !== (blk == 2)) err("mute");
        nt = (blk < 2) ? blk + 1 : blk;
        if (s_bank !== (nt % 2 == 1)) err("bank");
        if (target_idx != play_idx + 1) err("target");
        samples = samples + 1;
      end
    end else begin
      if (in_sample) err("gap inside sample");
      if (!running && (underruns != 0)) err("underrun before first block");
    end
    osp_q = out_space;
    // quiet window
    if (bank !== bank_q && cycle > 1) begin qchk = 1; qcnt = 0; end
    bank_q = bank;
    if (qchk) begin
      if (qcnt < 16 && load_enable !== 1'b0) err("load_enable high in quiet");
      if (qcnt == 16) begin if (load_enable !== 1'b1) err("load_enable low after quiet"); qchk = 0; end
      qcnt = qcnt + 1;
    end
    if (samples == 5 * BL + 3) done = 1;
  end

  initial begin
    repeat (5) @(posedge clk); rst <= 0;
    repeat (300) @(posedge clk);
    if (running || s_valid || underruns != 0) err("started before cfg");
    cfg_valid <= 1;
    repeat (150) @(posedge clk);
    if (running || s_valid || underruns != 0) err("started before block");
    if (target_idx != START) err("target before start");
    wait (done || cycle > 6000000);
    repeat (3 * T + 10) @(posedge clk);
    if (!done) err("timeout");
    if (underruns != 1) begin errs = errs + 1; $display("underruns %0d", underruns); end
    if (play_idx != START + 5) begin errs = errs + 1; $display("play_idx %0d", play_idx); end
    if (errs == 0) $display("PASS"); else $display("FAIL %0d", errs);
    $finish;
  end
endmodule
