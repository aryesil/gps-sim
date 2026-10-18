// tb_top - gs_top against the Python model vectors.
//   -d CASEN=n   1 ca1 2 e1b 3 e1c 4 b1i 5 gce12 6 gce45 7 gapcase 8 latecase 9 maxch
//   -d STRESS=1  out_space drops randomly (and in long windows)
//   -d CTL=1     enable held low until cmd_ready has been seen low (FIFO full,
//                run with -d GS_CMD_DEPTH=64 and a case with > 64 words after
//                the first block, e.g. CASEN=5); checks st_running/out_valid
//                stay low meanwhile.
// Reads fpga/vec/<case>_{cmd,exp}.hex and <case>_cnt.txt, writes <case>_got.hex.
// Checks: sample sequence bit-exact vs _exp.hex (no timing), underruns /
// dropped_late / clipped vs _cnt.txt, chip_late == 0, bad_words == 0,
// st_running only after the first block's words were accepted, no output
// before running, exactly n_exp samples (enable is dropped after n_exp sample
// starts so the core does not enter an extra block).
// Pipeline depth (sample start s_first -> out_valid): (T-1)+6+LAT_REDUCE clocks,
// 17 for P=12/T=4 (<= 6 samples in flight); the real sink's out_space means
// >= 16 free FIFO entries, which covers it.
`timescale 1ns/1ps
`include "gs_defs.vh"
`ifndef CASEN
`define CASEN 1
`endif
`ifndef STRESS
`define STRESS 0
`endif
`ifndef CTL
`define CTL 0
`endif
`ifndef GS_VECDIR
`define GS_VECDIR {`GS_MEMDIR, "../vec/"}
`endif
module tb_top;
  reg clk = 0, rst = 1; always #2 clk = ~clk;
  reg enable = 0, cmd_valid = 0, out_space = 1;
  reg [31:0] cmd_data = 0;
  wire cmd_ready, out_valid, st_running;
  wire [15:0] out_i, out_q, st_cmd_level;
  wire [63:0] st_play_idx;
  wire [31:0] st_underruns, st_dropped_late, st_clipped, st_chip_late, st_bad_words;

  gs_top dut (.clk(clk), .rst(rst), .enable(enable), .cmd_valid(cmd_valid),
    .cmd_data(cmd_data), .cmd_ready(cmd_ready), .out_space(out_space),
    .out_valid(out_valid), .out_i(out_i), .out_q(out_q),
    .st_play_idx(st_play_idx), .st_underruns(st_underruns),
    .st_dropped_late(st_dropped_late), .st_clipped(st_clipped),
    .st_chip_late(st_chip_late), .st_bad_words(st_bad_words),
    .st_running(st_running), .st_cmd_level(st_cmd_level));

  reg [8*16-1:0] cname;
  reg [8*512-1:0] fname;
  reg [31:0] cmdw [0:4095];
  reg [31:0] expw [0:262143];
  integer n_cmd, n_exp, fd, r;
  integer e_under, e_late, e_clip;
  integer errs = 0, seed = 12345, cycle = 0;
  integer n_got = 0, n_start = 0, acc = 0, blk0_end;
  reg stop_en = 0, done = 0, ctl_release = 0, bp_seen = 0, prod_done = 0;
  reg run_seen = 0;
  integer gfd;
  integer rel_cnt = 0;

  task err; input [255:0] m; begin errs = errs + 1;
    if (errs <= 12) $display("mismatch: %0s (cycle %0d)", m, cycle); end endtask

  initial begin
    case (`CASEN)
      1: cname = "ca1";   2: cname = "e1b";    3: cname = "e1c";
      4: cname = "b1i";   5: cname = "gce12";  6: cname = "gce45";
      7: cname = "gapcase"; 8: cname = "latecase"; default: cname = "maxch";
    endcase
    $sformat(fname, "%0s%0s_cmd.hex", `GS_VECDIR, cname);
    fd = $fopen(fname, "r");
    if (fd == 0) begin $display("cannot open %0s", fname); $display("FAIL 1"); $finish; end
    n_cmd = 0;
    while ($fscanf(fd, "%h\n", cmdw[n_cmd]) == 1) n_cmd = n_cmd + 1;
    $fclose(fd);
    $sformat(fname, "%0s%0s_exp.hex", `GS_VECDIR, cname);
    fd = $fopen(fname, "r");
    if (fd == 0) begin $display("cannot open %0s", fname); $display("FAIL 1"); $finish; end
    n_exp = 0;
    while ($fscanf(fd, "%h\n", expw[n_exp]) == 1) n_exp = n_exp + 1;
    $fclose(fd);
    $sformat(fname, "%0s%0s_cnt.txt", `GS_VECDIR, cname);
    fd = $fopen(fname, "r");
    if (fd == 0) begin $display("cannot open %0s", fname); $display("FAIL 1"); $finish; end
    r = $fscanf(fd, "underruns=%d\n", e_under);
    r = r + $fscanf(fd, "dropped_late=%d\n", e_late);
    r = r + $fscanf(fd, "clipped=%d\n", e_clip);
    $fclose(fd);
    if (r != 3) begin $display("bad cnt file"); $display("FAIL 1"); $finish; end
    blk0_end = 6 + cmdw[6][15:0];
    $sformat(fname, "%0s%0s_got.hex", `GS_VECDIR, cname);
    gfd = $fopen(fname, "w");
    $display("case %0s: %0d cmd words, %0d expected samples, exp underruns=%0d late=%0d clipped=%0d",
             cname, n_cmd, n_exp, e_under, e_late, e_clip);
    repeat (10) @(posedge clk);
    rst <= 0;
  end

  // ---- command producer with random gaps
  integer gap, w;
  initial begin
    wait (!rst);
    repeat (3) @(posedge clk);
    for (w = 0; w < n_cmd; w = w + 1) begin
      gap = (($random(seed) & 15) == 0) ? ($random(seed) & 31) : ($random(seed) & 3);
      repeat (gap) @(posedge clk);
      cmd_valid <= 1'b1; cmd_data <= cmdw[w];
      @(posedge clk);
      while (!cmd_ready) @(posedge clk);
      cmd_valid <= 1'b0;
    end
    prod_done <= 1'b1;
  end

  always @(posedge clk) if (cmd_valid && cmd_ready) acc <= acc + 1;
  always @(posedge clk) if (!rst && cmd_valid && !cmd_ready) bp_seen <= 1'b1;

  // ---- enable control
  always @(posedge clk) begin
    cycle <= cycle + 1;
    if (!rst) begin
      if (`CTL == 0) enable <= !stop_en;
      else begin
        if (!ctl_release && ((bp_seen && rel_cnt > 3000) || prod_done)) begin
          ctl_release <= 1'b1;
          if (!bp_seen) err("ctl: no cmd_ready backpressure");
          if (st_running) err("ctl: running before release");
        end
        if (bp_seen) rel_cnt <= rel_cnt + 1;
        enable <= ctl_release && !stop_en;
        if (!ctl_release && (st_running || out_valid)) err("ctl: activity while enable low");
      end
    end
  end

  // ---- out_space
  integer ph = 0;
  always @(posedge clk) begin
    ph <= ph + 1;
    if (`STRESS) out_space <= ((ph % 5000) >= 700) && (($random(seed) & 3) != 0);
    else out_space <= 1'b1;
  end

  // ---- sample start count -> stop enable after n_exp starts
  always @(posedge clk) if (!rst) begin
    if (dut.s_valid && dut.s_first) begin
      n_start <= n_start + 1;
      if (n_start + 1 == n_exp) stop_en <= 1'b1;
    end
  end

  // ---- running only after first block's words accepted
  always @(posedge clk) if (!rst) begin
    if (st_running && !run_seen) begin
      run_seen <= 1'b1;
      if (acc < blk0_end) err("running before first block accepted");
    end
    if (out_valid && !st_running) err("output before running");
  end

  // ---- collect and compare
  always @(posedge clk) if (!rst && out_valid) begin
    if (n_got < n_exp) begin
      if ({out_i, out_q} !== expw[n_got]) begin
        errs = errs + 1;
        if (errs <= 12) $display("mismatch: sample %0d got %h exp %h", n_got, {out_i, out_q}, expw[n_got]);
      end
      $fdisplay(gfd, "%h", {out_i, out_q});
    end else err("extra output sample");
    n_got = n_got + 1;
  end

  // ---- finish
  integer tail = 0;
  always @(posedge clk) if (!rst && !done) begin
    if (n_got >= n_exp && n_exp > 0) tail <= tail + 1;
    if (tail == 200) begin
      done <= 1'b1;
      if (n_got != n_exp) err("sample count");
      if (st_underruns !== e_under) begin errs = errs + 1; $display("mismatch: underruns %0d exp %0d", st_underruns, e_under); end
      if (st_dropped_late !== e_late) begin errs = errs + 1; $display("mismatch: dropped_late %0d exp %0d", st_dropped_late, e_late); end
      if (st_clipped !== e_clip) begin errs = errs + 1; $display("mismatch: clipped %0d exp %0d", st_clipped, e_clip); end
      if (st_chip_late !== 0) begin errs = errs + 1; $display("mismatch: chip_late %0d", st_chip_late); end
      if (st_bad_words !== 0) begin errs = errs + 1; $display("mismatch: bad_words %0d", st_bad_words); end
      $display("samples=%0d play_idx=%0d underruns=%0d dropped_late=%0d clipped=%0d chip_late=%0d bad_words=%0d clocks=%0d",
               n_got, st_play_idx, st_underruns, st_dropped_late, st_clipped, st_chip_late, st_bad_words, cycle);
      $fclose(gfd);
      if (errs == 0) $display("PASS"); else $display("FAIL %0d", errs);
      $finish;
    end
    if (cycle > 30000000) begin
      $display("timeout: %0d of %0d samples", n_got, n_exp);
      $display("FAIL %0d", errs + 1);
      $finish;
    end
  end
endmodule
