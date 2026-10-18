// tb_wrap - gs_wrap (AXI-Lite, async FIFOs, command-mode mux, gs_top) against
// the Python model vectors.
//   -d CASEN=n    1 ca1 (default)  5 gce12  7 gapcase
//   -d NS=n       samples checked in pass 1 (default 65536 = one block; capped
//                 at the case total, which also enables the counter compare)
//   -d DIV_NS=r   clk_div period ns (default 33.333 = 30 MHz)
//   -d ENG_NS=r   clk_eng period ns (default 4.0)
//   -d DREQ_N=n   dac_req every n-th clk_div (default 1: every cycle)
//   -d LOWFS=0/1  second pass after soft_reset at 0.5 MHz, NS2 samples (default 1)
//   -d NS2=n      (default 560)
//   -d UFTEST=1   deliberately slow engine; checks SAMPLE_FIFO_UNDERFLOWS only
// Phases: A) cmd_mode=0 pass-through, FIFOs untouched, engine idle;
//         B) pass 1: command words through a behavioural upack (random stalls),
//            samples vs _exp.hex bit-exact, counters over AXI-Lite, clear_counters;
//         C) soft_reset, then pass 2 at low fs.
`timescale 1ns/1ps
`ifndef CASEN
`define CASEN 1
`endif
`ifndef NS
`define NS 65536
`endif
`ifndef DIV_NS
`define DIV_NS 33.333
`endif
`ifndef ENG_NS
`define ENG_NS 4.0
`endif
`ifndef DREQ_N
`define DREQ_N 1
`endif
`ifndef LOWFS
`define LOWFS 1
`endif
`ifndef NS2
`define NS2 560
`endif
`ifndef UFTEST
`define UFTEST 0
`endif
`ifndef GS_MEMDIR
`define GS_MEMDIR "fpga/mem/"
`endif
`ifndef GS_VECDIR
`define GS_VECDIR {`GS_MEMDIR, "../vec/"}
`endif
module tb_wrap;
  reg clk_sys = 0, clk_div = 0, clk_eng = 0;
  reg rst_sys_n = 0, rst_div_n = 0, rst_eng_n = 0;
  real div_half = `DIV_NS / 2.0;
  always #5 clk_sys = ~clk_sys;
  always #(div_half) clk_div = ~clk_div;
  always #(`ENG_NS / 2.0) clk_eng = ~clk_eng;

  reg  [11:0] awaddr = 0, araddr = 0;
  reg         awvalid = 0, wvalid = 0, arvalid = 0, bready = 1, rready = 1;
  reg  [31:0] wdata = 0;
  wire        awready, wready, bvalid, arready, rvalid;
  wire [1:0]  bresp, rresp;
  wire [31:0] rdata;

  reg  [31:0] up_data = 0;
  reg         up_valid = 0;
  wire        up_rd_en;
  reg         dm_req_out = 0, dm_valid_out = 0, dac_req = 0;
  reg  [15:0] dm_i0 = 0, dm_q0 = 0;
  wire        tx_valid0, cmd_mode_div, irq;
  wire [15:0] tx_i0, tx_q0;

  gs_wrap dut (
    .clk_sys(clk_sys), .rst_sys_n(rst_sys_n), .clk_div(clk_div), .rst_div_n(rst_div_n),
    .clk_eng(clk_eng), .rst_eng_n(rst_eng_n),
    .s_axi_awaddr(awaddr), .s_axi_awvalid(awvalid), .s_axi_awready(awready),
    .s_axi_wdata(wdata), .s_axi_wstrb(4'hF), .s_axi_wvalid(wvalid), .s_axi_wready(wready),
    .s_axi_bresp(bresp), .s_axi_bvalid(bvalid), .s_axi_bready(bready),
    .s_axi_araddr(araddr), .s_axi_arvalid(arvalid), .s_axi_arready(arready),
    .s_axi_rdata(rdata), .s_axi_rresp(rresp), .s_axi_rvalid(rvalid), .s_axi_rready(rready),
    .up_data(up_data), .up_valid(up_valid), .up_rd_en(up_rd_en),
    .dm_req_out(dm_req_out), .dm_valid_out(dm_valid_out), .dm_i0(dm_i0), .dm_q0(dm_q0),
    .dac_req(dac_req), .tx_valid0(tx_valid0), .tx_i0(tx_i0), .tx_q0(tx_q0),
    .cmd_mode_div(cmd_mode_div), .irq(irq));

  // ---- vectors
  reg [8*16-1:0] cname;
  reg [8*512-1:0] fname;
  reg [31:0] cmdw [0:4095];
  reg [31:0] expw [0:262143];
  integer n_cmd, n_exp, fd, r, e_under, e_late, e_clip;
  integer errs = 0, seed = 4242, ns_run;
  task err; input [255:0] m; begin errs = errs + 1;
    if (errs <= 20) $display("mismatch: %0s (t=%0t)", m, $time); end endtask

  // ---- AXI-Lite BFM (clk_sys)
  task axi_wr; input [11:0] a; input [31:0] d;
    reg aw_d, w_d; integer to;
    begin
      @(posedge clk_sys);
      awaddr <= a; awvalid <= 1; wdata <= d; wvalid <= 1; aw_d = 0; w_d = 0; to = 0;
      while (!(aw_d && w_d) && to < 1000) begin
        @(posedge clk_sys); to = to + 1;
        if (awvalid && awready) begin awvalid <= 0; aw_d = 1; end
        if (wvalid && wready)   begin wvalid  <= 0; w_d  = 1; end
      end
      to = 0;
      while (!bvalid && to < 1000) begin @(posedge clk_sys); to = to + 1; end
      if (to >= 1000) err("axi write timeout");
      if (bresp !== 2'b00) err("axi bresp");
    end
  endtask
  task axi_rd; input [11:0] a; output [31:0] d;
    integer to;
    begin
      @(posedge clk_sys);
      araddr <= a; arvalid <= 1; to = 0;
      @(posedge clk_sys);
      while (!arready && to < 1000) begin @(posedge clk_sys); to = to + 1; end
      arvalid <= 0; to = 0;
      @(posedge clk_sys);
      while (!rvalid && to < 1000) begin @(posedge clk_sys); to = to + 1; end
      if (to >= 1000) err("axi read timeout");
      if (rresp !== 2'b00) err("axi rresp");
      d = rdata;
    end
  endtask

  // ---- dac_req
  integer dq = 0;
  always @(posedge clk_div) begin
    dac_req <= (dq == 0);
    dq <= (dq == `DREQ_N - 1) ? 0 : dq + 1;
  end

  // ---- upack model
  reg feed_en = 0, noise_en = 0;
  integer widx = 0, stall = 0;
  always @(posedge clk_div) begin
    up_valid <= 1'b0;
    if (noise_en) begin up_valid <= $random(seed); up_data <= $random(seed); end
    else if (stall > 0) stall = stall - 1;
    else if (up_rd_en && feed_en && widx < n_cmd) begin
      up_valid <= 1'b1; up_data <= cmdw[widx]; widx <= widx + 1;
      if (($random(seed) & 7) == 0) stall = $random(seed) & 15;
    end
  end

  // ---- phase A driver: random dm_* inputs
  always @(posedge clk_div) begin
    dm_req_out <= $random(seed); dm_valid_out <= $random(seed);
    dm_i0 <= $random(seed); dm_q0 <= $random(seed);
  end
  integer passthru_chk = 0;
  always @(negedge clk_div) if (passthru_chk) begin
    if (up_rd_en !== dm_req_out) err("passthrough up_rd_en");
    if (tx_valid0 !== dm_valid_out || tx_i0 !== dm_i0 || tx_q0 !== dm_q0) err("passthrough tx");
  end

  // ---- X check after reset
  reg x_chk = 0;
  always @(negedge clk_div) if (x_chk) begin
    if ((^{tx_valid0, tx_i0, tx_q0, up_rd_en, cmd_mode_div, irq}) === 1'bx) err("X on outputs");
  end
  always @(negedge clk_sys) if (x_chk) begin
    if ((^{awready, wready, bvalid, bresp, arready, rvalid, rresp, rdata}) === 1'bx) err("X on axi outputs");
  end

  // ---- sample collector (clk_div)
  integer n_got = 0, ns_tgt = 0, chk_data = 1, uf_tb = 0;
  reg     collecting = 0;
  reg     dq_prev = 0, mode_prev = 0, rst_prev = 1;
  always @(posedge clk_div) begin
    dq_prev <= dac_req;
    mode_prev <= dut.cmd_mode_d;
    rst_prev <= dut.rdiv_q;
    if (dut.cmd_mode_d && x_chk) begin
      if (mode_prev && !rst_prev && tx_valid0 !== dq_prev) err("tx_valid0 != delayed dac_req");
      if (!dut.pop_q && (tx_i0 !== 16'd0 || tx_q0 !== 16'd0)) err("nonzero tx without pop");
      if (dut.uf_pulse) uf_tb = uf_tb + 1;
      if (dut.pop_q && collecting) begin
        if (n_got < ns_tgt) begin
          if (chk_data && {tx_i0, tx_q0} !== expw[n_got]) begin
            errs = errs + 1;
            if (errs <= 20) $display("mismatch: sample %0d got %h exp %h", n_got, {tx_i0, tx_q0}, expw[n_got]);
          end
          n_got = n_got + 1;
        end
      end
    end
  end

  // ---- command words as seen by gs_top
  integer ridx = 0;
  always @(posedge clk_eng) if (dut.cf_ren) begin
    if (dut.cf_rdata !== cmdw[ridx]) begin errs = errs + 1;
      if (errs <= 20) $display("mismatch: cmd word %0d got %h exp %h", ridx, dut.cf_rdata, cmdw[ridx]); end
    ridx = ridx + 1;
  end

  // ---- engine sample starts
  integer starts = 0;
  always @(posedge clk_eng) if (dut.u_top.s_valid && dut.u_top.s_first) starts = starts + 1;

  // ---- helpers
  reg [31:0] v, v2, v3;
  task rd_status; begin axi_rd(12'h008, v); end endtask
  task run_pass; input integer ns; input integer cd; input integer full;
    integer timeout, disabled, zz;
    begin
      ns_tgt = ns; chk_data = cd; n_got = 0; starts = 0; uf_tb = 0; disabled = 0;
      widx = 0; ridx = 0; feed_en = 0;
      axi_wr(12'h004, 32'h2);              // cmd_mode
      repeat (20) @(posedge clk_div);
      feed_en = 1;
      timeout = 0;
      while (widx < n_cmd && timeout < 100000) begin @(posedge clk_sys); timeout = timeout + 1; end
      if (widx < n_cmd) begin err("cmd words not consumed"); $display("widx=%0d up_rd_en=%b", widx, up_rd_en); end
      repeat (50) @(posedge clk_sys);
      collecting = 1;
      axi_wr(12'h004, 32'h3);              // enable + cmd_mode
      timeout = 0;
      while (n_got < ns && timeout < 20000 + ns * (2 * div_half / 10.0 + 2) * 2) begin
        @(posedge clk_sys); timeout = timeout + 1;
        if (!disabled && starts >= ns) begin axi_wr(12'h004, 32'h2); disabled = 1; end
      end
      if (n_got < ns) begin err("timeout waiting for samples"); $display("n_got=%0d starts=%0d", n_got, starts); end
      if (!disabled) axi_wr(12'h004, 32'h2);
      collecting = 0; feed_en = 0;
      // let the FIFO drain and the snapshots settle
      zz = 0;
      while (zz < 3000) begin @(posedge clk_div); zz = zz + 1; end
      repeat (400) @(posedge clk_sys);
    end
  endtask

  task check_counters; input integer full; input integer expect_uf0;
    reg [31:0] a, b, c;
    begin
      axi_rd(12'h018, a); if (full ? (a !== e_under) : (a !== 0)) begin errs = errs + 1; $display("mismatch: UNDERRUNS %0d exp %0d", a, full ? e_under : 0); end
      axi_rd(12'h01C, a); if (full ? (a !== e_late) : (a !== 0)) begin errs = errs + 1; $display("mismatch: DROPPED_LATE %0d", a); end
      axi_rd(12'h020, a); if (full ? (a !== e_clip) : 0) begin errs = errs + 1; $display("mismatch: CLIPPED %0d exp %0d", a, e_clip); end
      axi_rd(12'h024, a); if (a !== 0) begin errs = errs + 1; $display("mismatch: CHIP_LATE %0d", a); end
      axi_rd(12'h028, a); if (a !== 0) begin errs = errs + 1; $display("mismatch: BAD_WORDS %0d", a); end
      axi_rd(12'h034, a); if (a !== 0) begin errs = errs + 1; $display("mismatch: CMD_FIFO_OVERRUNS %0d", a); end
      axi_rd(12'h02C, a);
      if (expect_uf0 && a !== 0) begin errs = errs + 1; $display("mismatch: SAMPLE_FIFO_UNDERFLOWS %0d", a); end
      if (a !== uf_tb) begin errs = errs + 1; $display("mismatch: SAMPLE_FIFO_UNDERFLOWS %0d vs tb count %0d", a, uf_tb); end
      // play index: HI-LO-HI, compare with the engine value (engine stopped)
      axi_rd(12'h014, a); axi_rd(12'h010, b); axi_rd(12'h014, c);
      if (a !== c) err("PLAY_IDX HI changed");
      if ({a, b} !== dut.u_top.st_play_idx) begin errs = errs + 1;
        $display("mismatch: PLAY_IDX %0d vs %0d", {a, b}, dut.u_top.st_play_idx); end
      $display("  play_idx=%0d underruns=%0d sample_uf=%0d", {a, b}, dut.u_top.st_underruns, uf_tb);
    end
  endtask

  integer k, t0;
  initial begin
    cname = (`CASEN == 5) ? "gce12" : (`CASEN == 7) ? "gapcase" : "ca1";
    $sformat(fname, "%0s%0s_cmd.hex", `GS_VECDIR, cname);
    fd = $fopen(fname, "r");
    if (fd == 0) begin $display("cannot open %0s", fname); $display("FAIL 1"); $finish; end
    n_cmd = 0; while ($fscanf(fd, "%h\n", cmdw[n_cmd]) == 1) n_cmd = n_cmd + 1; $fclose(fd);
    $sformat(fname, "%0s%0s_exp.hex", `GS_VECDIR, cname);
    fd = $fopen(fname, "r");
    if (fd == 0) begin $display("cannot open %0s", fname); $display("FAIL 1"); $finish; end
    n_exp = 0; while ($fscanf(fd, "%h\n", expw[n_exp]) == 1) n_exp = n_exp + 1; $fclose(fd);
    $sformat(fname, "%0s%0s_cnt.txt", `GS_VECDIR, cname);
    fd = $fopen(fname, "r");
    r = $fscanf(fd, "underruns=%d\n", e_under);
    r = r + $fscanf(fd, "dropped_late=%d\n", e_late);
    r = r + $fscanf(fd, "clipped=%d\n", e_clip);
    $fclose(fd);
    ns_run = (`NS > n_exp) ? n_exp : `NS;
    $display("case %0s: %0d cmd words, %0d expected samples, run %0d; div %0f ns eng %0f ns dreq_n %0d",
             cname, n_cmd, n_exp, ns_run, `DIV_NS, `ENG_NS, `DREQ_N);
    t0 = $time;

    // reset
    #200;
    repeat (4) @(posedge clk_sys);
    rst_sys_n <= 1;
    repeat (3) @(posedge clk_div); rst_div_n <= 1;
    repeat (3) @(posedge clk_eng); rst_eng_n <= 1;
    repeat (20) @(posedge clk_div);
    x_chk = 1;

    // ---- phase A: pass-through
    axi_rd(12'h000, v); if (v !== 32'h47530001) begin errs = errs + 1; $display("mismatch: ID %h", v); end
    axi_rd(12'h004, v); if (v !== 0) err("CTRL reset value");
    noise_en = 1; passthru_chk = 1;
    repeat (400) @(posedge clk_div);
    rd_status;
    if (v[0] !== 1'b0 || v[31:16] !== 16'd0 || v[2] !== 1'b0) begin errs = errs + 1; $display("mismatch: STATUS in passthrough %h", v); end
    passthru_chk = 0; noise_en = 0;
    axi_rd(12'h034, v); if (v !== 0) err("overruns in passthrough");
    axi_rd(12'h02C, v); if (v !== 0) err("underflows in passthrough");
    if (dut.u_cmdf.rbin !== 0 || dut.u_cmdf.wbin !== 0) err("cmd FIFO touched in passthrough");
    $display("phase A (pass-through) done");

    // ---- phase B: pass 1
    if (`UFTEST) begin
      run_pass(ns_run, 0, 0);
      rd_status; $display("  STATUS %h", v);
      axi_rd(12'h02C, v2);
      $display("  underflows axi=%0d tb=%0d", v2, uf_tb);
      if (v2 === 0) err("expected sample FIFO underflows");
      if (v2 !== uf_tb) err("underflow count mismatch");
    end else begin
      run_pass(ns_run, 1, (ns_run == n_exp));
      $display("pass 1: %0d samples checked, sim time %0t", n_got, $time - t0);
      rd_status; $display("  STATUS %h", v);
      check_counters((ns_run == n_exp), 1);
      axi_rd(12'h018, v);
      if (v !== 0) begin
        axi_wr(12'h004, 32'h2 | 32'h8);
        repeat (400) @(posedge clk_sys);
        axi_rd(12'h018, v2);
        if (v2 !== 0) err("clear_counters did not clear UNDERRUNS");
        else $display("  clear_counters ok (UNDERRUNS %0d -> 0)", v);
      end
      // ---- phase C: soft reset, low fs pass
      if (`LOWFS) begin
        axi_wr(12'h004, 32'h2 | 32'h4);
        k = 0; v = 32'h4;
        while (v[2] && k < 20000) begin axi_rd(12'h004, v); k = k + 1; end
        if (v[2]) err("soft_reset stuck busy");
        repeat (200) @(posedge clk_sys);
        rd_status;
        if (v[0] !== 0 || v[31:16] !== 0) begin errs = errs + 1; $display("mismatch: STATUS after soft_reset %h", v); end
        axi_rd(12'h030, v); if (v !== 0) err("sample level after soft_reset");
        if (dut.u_cmdf.rbin !== 0 || dut.u_smpf.wbin !== 0 || dut.u_smpf.rbin !== 0) err("FIFOs not flushed");
        axi_rd(12'h018, v); axi_rd(12'h02C, v2); axi_rd(12'h034, v3);
        if (v !== 0 || v2 !== 0 || v3 !== 0) err("counters after soft_reset");
        $display("soft_reset ok");
        div_half = 1000.0;                 // 0.5 MHz
        repeat (4) @(posedge clk_div);
        run_pass((`NS2 > n_exp) ? n_exp : `NS2, 1, 0);
        $display("pass 2 (0.5 MHz): %0d samples checked", n_got);
        check_counters(0, 1);
      end
    end

    if (errs == 0) $display("PASS"); else $display("FAIL %0d", errs);
    $finish;
  end

  // watchdog (sys clock cycles)
  integer wd = 0;
  always @(posedge clk_sys) begin
    wd = wd + 1;
    if (wd > 200000000) begin $display("watchdog"); $display("FAIL %0d", errs + 1); $finish; end
  end
endmodule
