// tb_sincos.v - exhaustive check of gs_sincos against the golden table.
// Golden: `GS_SINCOS_GOLDEN (default {GS_MEMDIR,"gs_sincos_full.mem"}),
// 4096 lines "ccccssss" (cos then sin, int16 hex). Prints PASS or FAIL <n>.
`timescale 1ns/1ps
`include "gs_defs.vh"
`ifndef GS_SINCOS_GOLDEN
`define GS_SINCOS_GOLDEN {`GS_MEMDIR, "gs_sincos_full.mem"}
`endif

module tb_sincos;
    localparam LAT = 3;
    reg clk = 0;
    always #2 clk = ~clk;

    reg  [11:0] idx = 0;
    wire signed [15:0] oc, os;
    gs_sincos dut (.clk(clk), .in_idx(idx), .out_cos(oc), .out_sin(os));

    reg [31:0] gold [0:4095];
    integer n, errs, i;
    initial begin
        $readmemh(`GS_SINCOS_GOLDEN, gold);
        errs = 0;
        // drive a new index every clock (ascending, then a pseudo-random pass)
        for (n = 0; n < 4096 + 4096 + LAT; n = n + 1) begin
            @(negedge clk);
            // check output for the index presented LAT cycles ago
            if (n >= LAT) begin
                i = (n - LAT < 4096) ? (n - LAT) : ((n - LAT - 4096) * 2731) % 4096;
                if ({oc, os} !== gold[i]) begin
                    errs = errs + 1;
                    if (errs <= 10)
                        $display("mismatch idx %0d: got %h exp %h", i, {oc, os}, gold[i]);
                end
            end
            idx = (n < 4096) ? n : ((n - 4096) * 2731) % 4096;
        end
        if (errs == 0) $display("PASS"); else $display("FAIL %0d", errs);
        $finish;
    end
endmodule
