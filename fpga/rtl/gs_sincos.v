// gs_sincos.v - carrier table: 12-bit phase index -> cos, sin (int16, Q1.15).
//
// LAT_SINCOS = 3 clocks: an index presented in cycle n gives out_cos/out_sin
// valid in cycle n+3. A new index is accepted every clock.
//
// Storage: ONE quarter-wave table, 1024 x 32 ({sin, cos}, one read address r),
// inferred as a single RAMB36 (2 RAMB18), registered output.  The Python table
// (roms.sincos_table) is exactly reconstructible:
//   cos_q[r] = round(32767*cos(2*pi*r/4096)),  r = 0..1023
//   sin_q[r] = cos_q[1024-r] for r = 1..1023, sin_q[0] = 0   (verified)
// and the quadrant extension is exact (roms.table_value): q = idx[11:10],
// (c,s) = (C,S) , (-S,C) , (-C,-S) , (S,-C). So no extra table is needed.
//
// Init file: `GS_SINCOS_MEM (default {GS_MEMDIR,"gs_sincos2.mem"}), 1024 lines,
// 8 hex digits each: {sin_q[r], cos_q[r]}, r = 0..1023 (scripts/fpga_vectors.py).
`include "gs_defs.vh"

`ifndef GS_SINCOS_MEM
`define GS_SINCOS_MEM {`GS_MEMDIR, "gs_sincos2.mem"}
`endif

module gs_sincos (
    input  wire               clk,
    input  wire        [11:0] in_idx,
    output reg  signed [15:0] out_cos,
    output reg  signed [15:0] out_sin,
    // gs_pipe interface: magnitudes (table values are >= 0) and the quadrant
    // signs, so the DSP inputs need no negation: cos = (nc ? -cm : cm).
    // pre_nc/pre_ns are the signs one clock before out_cm/out_sm (aligned with
    // the c0/s0 stage); out_cos == pre_nc ? -out_cm : out_cm one clock later.
    output reg  signed [15:0] out_cm,
    output reg  signed [15:0] out_sm,
    output wire               pre_nc,
    output wire               pre_ns,
    // the same signs one clock earlier (comb. from the index register q1)
    output wire               nx_nc,
    output wire               nx_ns
);
    // One 1024 x 32 table {sin_q[r], cos_q[r]}: a single read address r = idx[9:0]
    // (no 1024-r complement in front of the BRAM).  The init file holds both halves
    // (a derived init in an initial block is not synthesisable).
    (* ram_style = "block" *) reg [31:0] tab [0:1023];
    initial $readmemh(`GS_SINCOS_MEM, tab);

    wire [9:0] ra = in_idx[9:0];

    // stage 1: BRAM read (address registered inside the BRAM)
    reg [31:0] dab;
    reg [1:0]  q1, q2;
    always @(posedge clk) begin
        dab <= tab[ra];
        q1 <= in_idx[11:10];
    end

    // stage 2: BRAM output register
    reg signed [15:0] c0, s0;
    always @(posedge clk) begin
        c0 <= dab[15:0];
        s0 <= dab[31:16];
        q2 <= q1;
    end

    assign nx_nc = q1[1] ^ q1[0];     // pre_nc one clock earlier
    assign nx_ns = q1[1];
    assign pre_nc = (q2 == 2'd1) | (q2 == 2'd2);
    assign pre_ns = q2[1];
    always @(posedge clk) begin
        out_cm <= q2[0] ? s0 : c0;
        out_sm <= q2[0] ? c0 : s0;
    end

    // stage 3: quadrant rotation, registered output
    always @(posedge clk) begin
        case (q2)
            2'd0: begin out_cos <=  c0; out_sin <=  s0; end
            2'd1: begin out_cos <= -s0; out_sin <=  c0; end
            2'd2: begin out_cos <= -c0; out_sin <= -s0; end
            default: begin out_cos <= s0; out_sin <= -c0; end
        endcase
    end
endmodule
