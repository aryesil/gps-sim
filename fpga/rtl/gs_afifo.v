// gs_afifo.v - gray-code asynchronous FIFO, first-word-fall-through.
//
// Plain Verilog-2001.  Depth 2**AW, width W.  Synchronous resets (wrst_n for
// the write side, rrst_n for the read side); both sides should be reset
// together (the wrapper guarantees an overlapping reset window).
//
// Flags are registered:
//   wfull        exact (gray compare against the synchronised read pointer;
//                the synchronised pointer is stale, so full is conservative)
//   rempty       exact (gray compare against the synchronised write pointer,
//                conservative likewise)
//   w_almost_full  w_level >= DEPTH - AF_MARGIN
//   r_almost_empty r_level <= AE_THR
//   w_level / r_level  occupancy as seen from each side.  They are computed
//                from the *other* side's pointer after the gray synchroniser
//                (and a binary conversion register), so w_level never
//                underestimates and r_level never overestimates the true
//                occupancy; both lag by a few cycles of the local clock.
//
// FWFT: when rempty = 0, rdata is mem[rptr]; ren (qualified internally with
// ~rempty) advances to the next word on the same edge.
//
// CDC (the only crossings in this module):
//   wgray (write clock) -> rclk : 2-FF synchroniser wg_s1/wg_s2 (ASYNC_REG)
//   rgray (read clock)  -> wclk : 2-FF synchroniser rg_s1/rg_s2 (ASYNC_REG)
// Pointers cross only as gray code (one bit changes per increment), so a
// sampled value is always either the old or the new pointer.  The memory is
// written in wclk and read in rclk; a word is read only after its write
// pointer has crossed, which takes at least two rclk edges after the write.
// The synchroniser flops have no reset and are initialised to 0 (flushed by
// the other side's reset value anyway).
module gs_afifo #(
    parameter W         = 32,
    parameter AW        = 10,
    parameter AF_MARGIN = 16,
    parameter AE_THR    = 0
) (
    input  wire          wclk,
    input  wire          wrst_n,
    input  wire          wen,
    input  wire [W-1:0]  wdata,
    output reg           wfull,
    output reg           w_almost_full,
    output reg  [AW:0]   w_level,

    input  wire          rclk,
    input  wire          rrst_n,
    input  wire          ren,
    output reg  [W-1:0]  rdata,
    output reg           rempty,
    output reg           r_almost_empty,
    output reg  [AW:0]   r_level
);
    localparam DEPTH = (1 << AW);

    reg [W-1:0] mem [0:DEPTH-1];
    integer ii;
    initial begin
        for (ii = 0; ii < DEPTH; ii = ii + 1) mem[ii] = {W{1'b0}};
    end

    reg  [AW:0] wbin = {(AW+1){1'b0}}, wgray = {(AW+1){1'b0}};
    reg  [AW:0] rbin = {(AW+1){1'b0}}, rgray = {(AW+1){1'b0}};
    (* ASYNC_REG = "TRUE" *) reg [AW:0] rg_s1 = {(AW+1){1'b0}};
    (* ASYNC_REG = "TRUE" *) reg [AW:0] rg_s2 = {(AW+1){1'b0}};
    (* ASYNC_REG = "TRUE" *) reg [AW:0] wg_s1 = {(AW+1){1'b0}};
    (* ASYNC_REG = "TRUE" *) reg [AW:0] wg_s2 = {(AW+1){1'b0}};

    // ---- write side
    reg  [AW:0] rbin_w = {(AW+1){1'b0}};   // synchronised read pointer, binary

    wire        w_do    = wen & ~wfull;
    wire [AW:0] wbin_n  = wbin + {{AW{1'b0}}, w_do};
    wire [AW:0] wgray_n = (wbin_n >> 1) ^ wbin_n;

    function [AW:0] g2b;
        input [AW:0] g;
        integer k;
        begin
            g2b[AW] = g[AW];
            for (k = AW - 1; k >= 0; k = k - 1) g2b[k] = g2b[k+1] ^ g[k];
        end
    endfunction

    wire [AW:0] wlevel_n = wbin_n - rbin_w;

    always @(posedge wclk) begin
        rg_s1 <= rgray;
        rg_s2 <= rg_s1;
    end

    always @(posedge wclk) begin
        if (!wrst_n) begin
            wbin <= {(AW+1){1'b0}}; wgray <= {(AW+1){1'b0}};
            wfull <= 1'b0; w_almost_full <= 1'b0; w_level <= {(AW+1){1'b0}};
            rbin_w <= {(AW+1){1'b0}};
        end else begin
            wbin  <= wbin_n;
            wgray <= wgray_n;
            wfull <= (wgray_n == {~rg_s2[AW:AW-1], rg_s2[AW-2:0]});
            rbin_w <= g2b(rg_s2);
            w_level <= wlevel_n;
            w_almost_full <= (wlevel_n >= (DEPTH - AF_MARGIN));
        end
    end

    always @(posedge wclk) if (w_do) mem[wbin[AW-1:0]] <= wdata;

    // ---- read side
    reg  [AW:0] wbin_r = {(AW+1){1'b0}};   // synchronised write pointer, binary

    wire        r_do    = ren & ~rempty;
    wire [AW:0] rbin_n  = rbin + {{AW{1'b0}}, r_do};
    wire [AW:0] rgray_n = (rbin_n >> 1) ^ rbin_n;
    wire [AW:0] rlevel_n = wbin_r - rbin_n;

    always @(posedge rclk) begin
        wg_s1 <= wgray;
        wg_s2 <= wg_s1;
    end

    always @(posedge rclk) begin
        if (!rrst_n) begin
            rbin <= {(AW+1){1'b0}}; rgray <= {(AW+1){1'b0}};
            rempty <= 1'b1; r_almost_empty <= 1'b1; r_level <= {(AW+1){1'b0}};
            wbin_r <= {(AW+1){1'b0}};
        end else begin
            rbin  <= rbin_n;
            rgray <= rgray_n;
            rempty <= (rgray_n == wg_s2);
            wbin_r <= g2b(wg_s2);
            r_level <= rlevel_n;
            r_almost_empty <= (rlevel_n <= AE_THR);
        end
    end

    // FWFT read: address of the *next* word is used when advancing, so rdata
    // always equals mem[rbin] one edge after rbin changes; re-read every
    // cycle so a freshly written word is picked up before rempty drops.
    wire [AW-1:0] raddr = rbin_n[AW-1:0];
    initial rdata = {W{1'b0}};
    always @(posedge rclk) rdata <= mem[raddr];
endmodule
