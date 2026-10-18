// gs_sincos.v - carrier table: 12-bit phase index -> cos, sin (int16, Q1.15).
//
// LAT_SINCOS = 3 clocks: an index presented in cycle n gives out_cos/out_sin
// valid in cycle n+3. A new index is accepted every clock.
//
// Storage: ONE quarter-wave table, 1024 x 16, inferred as a single dual-port
// BRAM18 (port A = cos address r, port B = sin address 1024-r), registered
// output. The Python table (roms.sincos_table) is exactly reconstructible:
//   cos_q[r] = round(32767*cos(2*pi*r/4096)),  r = 0..1023
//   sin_q[r] = cos_q[1024-r] for r = 1..1023, sin_q[0] = 0   (verified)
// and the quadrant extension is exact (roms.table_value): q = idx[11:10],
// (c,s) = (C,S) , (-S,C) , (-C,-S) , (S,-C). So no extra table is needed.
//
// Init file: `GS_SINCOS_MEM (default {GS_MEMDIR,"gs_sincos.mem"}), 1024 lines,
// 4 hex digits each, the quarter-wave COS entries cos_q[0..1023].
`include "gs_defs.vh"

`ifndef GS_SINCOS_MEM
`define GS_SINCOS_MEM {`GS_MEMDIR, "gs_sincos.mem"}
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
    output wire               pre_ns
);
    (* ram_style = "block" *) reg [15:0] tab [0:1023];
    initial $readmemh(`GS_SINCOS_MEM, tab);

    wire [9:0] ra = in_idx[9:0];
    wire [10:0] rb_f = 11'd1024 - {1'b0, in_idx[9:0]};
    wire [9:0]  rb   = rb_f[9:0];                     // 1024-r, wraps to 0 for r = 0

    // stage 1: BRAM read (address registered inside the BRAM)
    reg [15:0] da, db;
    reg [1:0]  q1, q2;
    reg        z1, z2;                                // r == 0 -> sin_q = 0
    always @(posedge clk) begin
        da <= tab[ra];
        db <= tab[rb];
        q1 <= in_idx[11:10];
        z1 <= (in_idx[9:0] == 10'd0);
    end

    // stage 2: BRAM output register
    reg signed [15:0] c0, s0;
    always @(posedge clk) begin
        c0 <= da;
        s0 <= z1 ? 16'sd0 : db;
        q2 <= q1;
    end

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
