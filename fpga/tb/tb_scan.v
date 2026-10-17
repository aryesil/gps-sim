// tb_scan.v - testbench for gs_scan (chip-word scanner + code ROM).
// Behavioural sc_info model: entries [bank][pipe][slot] in flat arrays; the
// SCI word is combinational in (sc_slot, sc_bank) of the same clock (the DUT
// registers it; see the gs_scan.v header for the latency).  Writes: sc_we
// sets cur or nxt word/widx and the loaded flag at the clock edge.
// Prints PASS or FAIL <n>.
`timescale 1ns/1ps
`include "gs_defs.vh"

module tb_scan;
    localparam P = `GS_P;
    localparam S = `GS_SLOTS;
    localparam NE = 2 * P * S;
    localparam PERIOD = 2 * P * S;
    localparam SW = (S <= 8) ? 3 : (S <= 16) ? 4 : (S <= 32) ? 5 : 6;

    reg clk = 0, rst = 1;
    always #2 clk = ~clk;

    wire [3:0] sc_pipe; wire [SW-1:0] sc_slot; wire sc_bank;
    wire sc_we; wire [3:0] sc_wpipe; wire [SW-1:0] sc_wslot; wire sc_wbank;
    wire sc_wwhich; wire [31:0] sc_wword; wire [6:0] sc_wwidx;
    wire [P*`GS_SCI_W-1:0] info_all;
    reg shadow_bank = 1'b1, shadow_dirty = 0;
    wire shadow_clean;

    gs_scan dut (.clk(clk), .rst(rst), .sc_pipe(sc_pipe), .sc_slot(sc_slot), .sc_bank(sc_bank),
        .sc_info_all(info_all), .sc_we(sc_we), .sc_wpipe(sc_wpipe), .sc_wslot(sc_wslot),
        .sc_wbank(sc_wbank), .sc_wwhich(sc_wwhich), .sc_wword(sc_wword), .sc_wwidx(sc_wwidx),
        .shadow_bank(shadow_bank), .shadow_dirty(shadow_dirty), .shadow_clean(shadow_clean));

    // golden data (independent read of the same files)
    reg [31:0] grom [0:17855];
    reg [15:0] gbase [0:255];

    // entry model, index e = (bank*P + pipe)*S + slot
    reg        e_valid [0:NE-1];
    reg [1:0]  e_kind  [0:NE-1];
    reg [5:0]  e_prn   [0:NE-1];
    reg        e_curl  [0:NE-1];
    reg [6:0]  e_curw  [0:NE-1];
    reg [31:0] e_curd  [0:NE-1];
    reg [6:0]  e_nxtw  [0:NE-1];
    reg        e_nxtl  [0:NE-1];
    reg [31:0] e_nxtd  [0:NE-1];

    genvar gp;
    generate for (gp = 0; gp < P; gp = gp + 1) begin : g_info
        wire [31:0] ei = (sc_bank * P + gp) * S + sc_slot;
        assign info_all[gp*`GS_SCI_W +: `GS_SCI_W] = {e_nxtl[ei], e_nxtw[ei], e_curw[ei], e_curl[ei],
                                                      e_prn[ei], e_kind[ei], e_valid[ei]};
    end endgenerate

    integer errs = 0;
    integer wr_count = 0, wr_invalid = 0, wr_wrongbank_cur = 0;
    reg [31:0] we_idx;
    always @(posedge clk) if (!rst && sc_we) begin
        we_idx = (sc_wbank * P + sc_wpipe) * S + sc_wslot;
        wr_count = wr_count + 1;
        if (!e_valid[we_idx]) wr_invalid = wr_invalid + 1;
        if (sc_wwhich == 1'b0) begin
            e_curd[we_idx] <= sc_wword; e_curw[we_idx] <= sc_wwidx; e_curl[we_idx] <= 1'b1;
            if (sc_wbank != shadow_bank) wr_wrongbank_cur = wr_wrongbank_cur + 1;
        end else begin
            e_nxtd[we_idx] <= sc_wword; e_nxtw[we_idx] <= sc_wwidx; e_nxtl[we_idx] <= 1'b1;
        end
    end

    // ROM traffic monitor: ROM reads must equal fetches written (+ squashed).
    integer rom_reads = 0;
    always @(posedge clk) if (!rst && dut.s3v) rom_reads = rom_reads + 1;

    function [7:0] wpc; input [1:0] k;
        wpc = (k == 0) ? 32 : (k == 3) ? 64 : 128;
    endfunction
    function [6:0] succf; input [1:0] k; input [6:0] w;
        succf = ({1'b0, w} + 8'd1 >= wpc(k)) ? 7'd0 : (w + 7'd1);
    endfunction
    function [5:0] maxprn; input [1:0] k;
        maxprn = (k == 0) ? 32 : (k == 3) ? 63 : 50;
    endfunction
    function [31:0] romword; input [1:0] k; input [5:0] n; input [6:0] w;
        romword = grom[gbase[{k, n}] + w];
    endfunction

    integer b, p, s, e, i;
    reg [6:0] w;
    task fill_entry; input integer ee; input integer sel;
        begin
            e_valid[ee] = 1;
            e_kind[ee] = sel[1:0];
            case ((sel >> 2) % 4)
                0: e_prn[ee] = maxprn(e_kind[ee]);
                1: e_prn[ee] = 1;
                2: e_prn[ee] = 1 + (sel * 7) % maxprn(e_kind[ee]);
                default: e_prn[ee] = maxprn(e_kind[ee]) - 1;
            endcase
            case ((sel >> 4) % 4)
                0: w = wpc(e_kind[ee]) - 1;           // last word: succ wraps
                1: w = 0;
                2: w = wpc(e_kind[ee]) - 2;
                default: w = (sel * 5) % wpc(e_kind[ee]);
            endcase
            e_curw[ee] = w;
            e_curl[ee] = 1'b0; e_nxtl[ee] = 1'b0;
            e_curd[ee] = 32'hxxxxxxxx; e_nxtd[ee] = 32'hxxxxxxxx; e_nxtw[ee] = 7'h55;
        end
    endtask

    task setup; input integer sbank;
        begin
            for (e = 0; e < NE; e = e + 1) begin
                e_valid[e] = 0; e_kind[e] = 0; e_prn[e] = 0; e_curl[e] = 1; e_curw[e] = 0;
                e_curd[e] = 0; e_nxtw[e] = 0; e_nxtl[e] = 1; e_nxtd[e] = 0;
            end
            for (b = 0; b < 2; b = b + 1)
              for (p = 0; p < P; p = p + 1)
                for (s = 0; s < S; s = s + 1) begin
                    e = (b * P + p) * S + s;
                    // 3 of 4 entries valid, a mix of kinds
                    if ((p + s * 3 + b) % 4 != 3) begin
                        fill_entry(e, e * 5 + b + s);
                        // active bank: cur is maintained by the pipe
                        if (b != sbank) begin
                            e_curl[e] = 1'b1; e_curd[e] = romword(e_kind[e], e_prn[e], e_curw[e]);
                        end
                    end else begin
                        // invalid entry with poison contents that would be wrong if touched
                        e_valid[e] = 0; e_kind[e] = 1; e_prn[e] = 7; e_curl[e] = 0;
                        e_nxtl[e] = 0; e_curw[e] = 3; e_nxtw[e] = 99;
                    end
                end
        end
    endtask

    task check_all; input [127:0] tag;
        integer bb, pp, ss, ee;
        begin
            for (bb = 0; bb < 2; bb = bb + 1)
              for (pp = 0; pp < P; pp = pp + 1)
                for (ss = 0; ss < S; ss = ss + 1) begin
                    ee = (bb * P + pp) * S + ss;
                    if (e_valid[ee]) begin
                        if (!e_curl[ee] || e_curd[ee] !== romword(e_kind[ee], e_prn[ee], e_curw[ee])) begin
                            errs = errs + 1;
                            $display("mismatch %0s cur b%0d p%0d s%0d kind%0d prn%0d widx%0d got %h exp %h l=%b",
                                tag, bb, pp, ss, e_kind[ee], e_prn[ee], e_curw[ee], e_curd[ee],
                                romword(e_kind[ee], e_prn[ee], e_curw[ee]), e_curl[ee]);
                        end
                        if (!e_nxtl[ee] || e_nxtw[ee] !== succf(e_kind[ee], e_curw[ee]) ||
                            e_nxtd[ee] !== romword(e_kind[ee], e_prn[ee], succf(e_kind[ee], e_curw[ee]))) begin
                            errs = errs + 1;
                            $display("mismatch %0s nxt b%0d p%0d s%0d kind%0d prn%0d widx%0d nxtw %0d got %h l=%b",
                                tag, bb, pp, ss, e_kind[ee], e_prn[ee], e_curw[ee], e_nxtw[ee], e_nxtd[ee], e_nxtl[ee]);
                        end
                    end else if (e_curl[ee] !== 1'b0 || e_nxtl[ee] !== 1'b0 || e_nxtw[ee] !== 7'd99) begin
                        errs = errs + 1; $display("mismatch invalid entry touched b%0d p%0d s%0d", bb, pp, ss);
                    end
                end
        end
    endtask

    // pipe crossing: cur <- nxt on every valid entry of the active bank
    task cross_active; input integer abank;
        integer pp, ss, ee;
        begin
            for (pp = 0; pp < P; pp = pp + 1)
              for (ss = 0; ss < S; ss = ss + 1) begin
                  ee = (abank * P + pp) * S + ss;
                  if (e_valid[ee]) begin
                      e_curw[ee] = e_nxtw[ee]; e_curd[ee] = e_nxtd[ee];
                  end
              end
        end
    endtask

    integer t, clean_rise, wr0, k;
    initial begin
        $readmemh({`GS_MEMDIR, "gs_rom.mem"}, grom);
        $readmemh({`GS_MEMDIR, "gs_base.mem"}, gbase);
        setup(1);
        repeat (4) @(posedge clk);
        rst = 0;

        // ---- phase 1: initial fill within a bounded time ----
        // shadow CUR fetches + NXT of both banks: at most ~2 passes + latency
        clean_rise = -1;
        for (t = 0; t < 3 * PERIOD + 40; t = t + 1) begin
            @(posedge clk); #1;
            if (shadow_clean && clean_rise < 0) clean_rise = t;
            if (t < 40 && shadow_clean) begin errs = errs + 1; $display("mismatch clean too early t=%0d", t); end
        end
        check_all("fill");
        if (!shadow_clean) begin errs = errs + 1; $display("mismatch clean not set after fill"); end
        if (clean_rise < 0 || clean_rise > 3 * PERIOD + 20) begin
            errs = errs + 1; $display("mismatch clean_rise=%0d", clean_rise);
        end
        $display("info: first shadow_clean at clock %0d, period %0d", clean_rise, PERIOD);
        if (wr_invalid != 0) begin errs = errs + 1; $display("mismatch write to invalid entry"); end
        if (rom_reads != wr_count) begin errs = errs + 1; $display("mismatch rom_reads %0d != writes %0d", rom_reads, wr_count); end
        if (wr_wrongbank_cur != 0) begin errs = errs + 1; $display("mismatch CUR written to active bank"); end

        // ---- phase 2: idle: no traffic, clean stays ----
        wr0 = wr_count;
        repeat (3 * PERIOD) begin
            @(posedge clk); #1;
            if (!shadow_clean) begin errs = errs + 1; $display("mismatch clean dropped while idle"); end
        end
        if (wr_count != wr0) begin errs = errs + 1; $display("mismatch traffic while idle"); end

        // ---- phase 3: pipe crossings in the active bank -> nxt refetched within one period ----
        for (k = 0; k < 4; k = k + 1) begin
            @(negedge clk); cross_active(0);
            repeat (PERIOD + 12) @(posedge clk);
            #1; check_all("cross");
            if (!shadow_clean) begin errs = errs + 1; $display("mismatch clean low after active crossing"); end
        end

        // ---- phase 4: shadow_dirty: loader rewrites two shadow entries ----
        @(negedge clk);
        for (i = 0; i < 2; i = i + 1) begin
            e = (1 * P + i) * S + 0;
            fill_entry(e, 40 + i * 9);
        end
        shadow_dirty = 1; @(posedge clk); #1; shadow_dirty = 0;
        if (shadow_clean !== 1'b0) begin errs = errs + 1; $display("mismatch clean not cleared by dirty"); end
        clean_rise = -1;
        for (t = 0; t < 3 * PERIOD + 40; t = t + 1) begin
            @(posedge clk); #1;
            if (shadow_clean && clean_rise < 0) clean_rise = t;
            if (t < 20 && shadow_clean) begin errs = errs + 1; $display("mismatch clean early after dirty t=%0d", t); end
        end
        check_all("dirty");
        if (!shadow_clean) begin errs = errs + 1; $display("mismatch clean not set after dirty refill"); end
        $display("info: shadow_clean after dirty at clock %0d", clean_rise);

        // ---- phase 5: bank swap: shadow 1 -> 0 ----
        @(negedge clk);
        shadow_bank = 1'b0;
        @(posedge clk); #1;
        if (shadow_clean !== 1'b0) begin errs = errs + 1; $display("mismatch clean not cleared by bank change"); end
        // new shadow (bank 0): new channels, cur not loaded
        for (p = 0; p < P; p = p + 1) for (s = 0; s < S; s = s + 1) begin
            e = p * S + s;
            if (e_valid[e]) fill_entry(e, e * 3 + 1);
        end
        // bank 1 (new active): pipe maintains cur
        for (p = 0; p < P; p = p + 1) for (s = 0; s < S; s = s + 1) begin
            e = (P + p) * S + s;
            if (e_valid[e]) begin e_curl[e] = 1; e_curd[e] = romword(e_kind[e], e_prn[e], e_curw[e]); end
        end
        shadow_dirty = 1; @(posedge clk); #1; shadow_dirty = 0;
        repeat (3 * PERIOD + 40) @(posedge clk);
        #1; check_all("swap");
        if (!shadow_clean) begin errs = errs + 1; $display("mismatch clean not set after swap refill"); end
        if (wr_wrongbank_cur != 0) begin errs = errs + 1; $display("mismatch CUR written to active bank (swap)"); end
        if (wr_invalid != 0) begin errs = errs + 1; $display("mismatch write to invalid entry (late)"); end

        if (errs == 0) $display("PASS"); else $display("FAIL %0d", errs);
        $finish;
    end

    initial begin
        #(4 * 200 * (3 * PERIOD + 40) * 20);
        $display("FAIL timeout");
        $finish;
    end
endmodule
