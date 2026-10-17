// gs_reduce: sum the GS_P pipe sums, round, shift and saturate to int16.
//
//   out = sat16((sum + 2^(20+sh)) >>> (21+sh)),  sh = min(shift, 7)
//
// shift is limited to 0..7: values > 7 are clamped to 7. shift must be static
// while samples are in flight (it is sampled by the round/shift stages).
//
// Pipeline (every stage registered, a new input is accepted every clock):
//   stage 1        input register (sign-extended to W bits)
//   stages 2..1+NST  adder tree, at most 3 inputs per add, NST = ceil(log3(P))
//   stage NST+2    add rounding constant
//   stage NST+3    arithmetic shift + saturate; out_valid/out_i/out_q/clipped_inc
// LAT_REDUCE = NST + 3 clocks from in_valid to out_valid (P=12 -> NST=3,
// LAT_REDUCE=6; P=1 -> NST=0, LAT_REDUCE=3; P=5 -> NST=2, LAT_REDUCE=5).
// Exposed as localparam LAT_REDUCE (hierarchically readable).
//
// clipped_inc (2 bit) = number of saturated components (0..2) of the sample
// presented on out_*; 0 when out_valid=0.
// Generate-based: works for any GS_P 1..16 (odd included).
`include "gs_defs.vh"

module gs_reduce (
    input  wire                     clk,
    input  wire                     rst,
    input  wire                     in_valid,
    input  wire [`GS_P*38-1:0]      in_i_all,
    input  wire [`GS_P*38-1:0]      in_q_all,
    input  wire [7:0]               shift,
    output reg                      out_valid,
    output reg  signed [15:0]       out_i,
    output reg  signed [15:0]       out_q,
    output reg  [1:0]               clipped_inc
);
    localparam P  = `GS_P;
    localparam W  = 44;               // 38 + 4 growth bits (+2 margin for rounding)
    localparam MX = 16;               // max elements per level

    // elements at tree level s (level 0 = input register)
    function integer cnt;
        input integer s;
        integer k, n;
        begin
            n = P;
            for (k = 0; k < s; k = k + 1) n = (n + 2) / 3;
            cnt = n;
        end
    endfunction
    function integer nstages;
        input integer dummy;
        integer s;
        begin
            s = 0;
            while (cnt(s) > 1) s = s + 1;
            nstages = s;
        end
    endfunction

    localparam NST = nstages(0);
    localparam LAT_REDUCE = NST + 3;

    reg signed [W-1:0] ti [0:(NST+1)*MX-1];
    reg signed [W-1:0] tq [0:(NST+1)*MX-1];
    reg [NST+1:0]      vl;            // valid per tree level (0..NST)

    wire [2:0] sh = (shift > 8'd7) ? 3'd7 : shift[2:0];

    genvar s, j;
    generate
        // level 0: input register
        for (j = 0; j < P; j = j + 1) begin : g_in
            always @(posedge clk) begin
                ti[j] <= $signed(in_i_all[j*38 +: 38]);
                tq[j] <= $signed(in_q_all[j*38 +: 38]);
            end
        end
        // levels 1..NST
        for (s = 0; s < NST; s = s + 1) begin : g_lv
            for (j = 0; j < cnt(s+1); j = j + 1) begin : g_add
                if (3*j + 2 < cnt(s)) begin : g3
                    always @(posedge clk) begin
                        ti[(s+1)*MX+j] <= ti[s*MX+3*j] + ti[s*MX+3*j+1] + ti[s*MX+3*j+2];
                        tq[(s+1)*MX+j] <= tq[s*MX+3*j] + tq[s*MX+3*j+1] + tq[s*MX+3*j+2];
                    end
                end else if (3*j + 1 < cnt(s)) begin : g2
                    always @(posedge clk) begin
                        ti[(s+1)*MX+j] <= ti[s*MX+3*j] + ti[s*MX+3*j+1];
                        tq[(s+1)*MX+j] <= tq[s*MX+3*j] + tq[s*MX+3*j+1];
                    end
                end else begin : g1
                    always @(posedge clk) begin
                        ti[(s+1)*MX+j] <= ti[s*MX+3*j];
                        tq[(s+1)*MX+j] <= tq[s*MX+3*j];
                    end
                end
            end
        end
    endgenerate

    // valid pipeline through input reg + tree
    integer vi;
    always @(posedge clk) begin
        if (rst) vl <= 0;
        else begin
            vl[0] <= in_valid;
            for (vi = 1; vi <= NST; vi = vi + 1) vl[vi] <= vl[vi-1];
        end
    end

    // rounding stage
    reg signed [W-1:0] ri, rq;
    reg                rv;
    wire signed [W-1:0] half = ({{(W-1){1'b0}}, 1'b1}) << (6'd20 + sh);
    always @(posedge clk) begin
        ri <= ti[NST*MX] + half;
        rq <= tq[NST*MX] + half;
        rv <= rst ? 1'b0 : vl[NST];
    end

    // shift + saturate stage
    wire signed [W-1:0] si = ri >>> (5'd21 + sh);
    wire signed [W-1:0] sq = rq >>> (5'd21 + sh);
    wire ci_hi = (si >  44'sd32767);
    wire ci_lo = (si < -44'sd32768);
    wire cq_hi = (sq >  44'sd32767);
    wire cq_lo = (sq < -44'sd32768);
    wire ci = ci_hi | ci_lo;
    wire cq = cq_hi | cq_lo;

    always @(posedge clk) begin
        out_valid   <= rst ? 1'b0 : rv;
        out_i       <= ci_hi ? 16'sh7FFF : ci_lo ? 16'sh8000 : si[15:0];
        out_q       <= cq_hi ? 16'sh7FFF : cq_lo ? 16'sh8000 : sq[15:0];
        clipped_inc <= (rst | ~rv) ? 2'd0 : ({1'b0, ci} + {1'b0, cq});
    end
endmodule
