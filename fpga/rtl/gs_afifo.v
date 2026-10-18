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
    // wbin1/wgray1 = wbin+1 and its gray code, kept in registers so that the
    // flag / level logic never has the increment (w_do -> adder) in front of
    // its compare: both candidates are computed from registers and w_do only
    // selects (timing at 250 MHz).  Values are identical to the plain form
    // wfull <= (gray(wbin + w_do) == ...), w_level <= wbin + w_do - rbin_w.
    reg  [AW:0] wbin1 = {{AW{1'b0}}, 1'b1}, wgray1 = {{AW{1'b0}}, 1'b1};

    wire        w_do    = wen & ~wfull;
    wire [AW:0] wbin2   = wbin1 + {{AW{1'b0}}, 1'b1};

    function [AW:0] g2b;
        input [AW:0] g;
        integer k;
        begin
            g2b[AW] = g[AW];
            for (k = AW - 1; k >= 0; k = k - 1) g2b[k] = g2b[k+1] ^ g[k];
        end
    endfunction

    wire [AW:0] rfull_g = {~rg_s2[AW:AW-1], rg_s2[AW-2:0]};
    wire        wfull0  = (wgray  == rfull_g);    // no write this clock
    wire        wfull1  = (wgray1 == rfull_g);    // one write this clock
    wire [AW:0] wlev0   = wbin  - rbin_w;
    wire [AW:0] wlev1   = wbin1 - rbin_w;
    wire        waf0    = (wlev0 >= (DEPTH - AF_MARGIN));
    wire        waf1    = (wlev1 >= (DEPTH - AF_MARGIN));

    always @(posedge wclk) begin
        rg_s1 <= rgray;
        rg_s2 <= rg_s1;
    end

    always @(posedge wclk) begin
        if (!wrst_n) begin
            wbin <= {(AW+1){1'b0}}; wgray <= {(AW+1){1'b0}};
            wbin1 <= {{AW{1'b0}}, 1'b1}; wgray1 <= {{AW{1'b0}}, 1'b1};
            wfull <= 1'b0; w_almost_full <= 1'b0; w_level <= {(AW+1){1'b0}};
            rbin_w <= {(AW+1){1'b0}};
        end else begin
            if (w_do) begin
                wbin   <= wbin1;
                wgray  <= wgray1;
                wbin1  <= wbin2;
                wgray1 <= (wbin2 >> 1) ^ wbin2;
            end
            wfull         <= w_do ? wfull1 : wfull0;
            w_level       <= w_do ? wlev1  : wlev0;
            w_almost_full <= w_do ? waf1   : waf0;
            rbin_w <= g2b(rg_s2);
        end
    end

    always @(posedge wclk) if (w_do) mem[wbin[AW-1:0]] <= wdata;

    // ---- read side
    reg  [AW:0] wbin_r = {(AW+1){1'b0}};   // synchronised write pointer, binary
    // rbin1/rgray1 = rbin+1 and its gray code (same restructuring as the write
    // side: r_do only selects between precomputed candidates).
    reg  [AW:0] rbin1 = {{AW{1'b0}}, 1'b1}, rgray1 = {{AW{1'b0}}, 1'b1};

    wire        r_do    = ren & ~rempty;
    wire [AW:0] rbin2   = rbin1 + {{AW{1'b0}}, 1'b1};
    wire        rempty0 = (rgray  == wg_s2);
    wire        rempty1 = (rgray1 == wg_s2);
    wire [AW:0] rlev0   = wbin_r - rbin;
    wire [AW:0] rlev1   = wbin_r - rbin1;
    wire        rae0    = (rlev0 <= AE_THR);
    wire        rae1    = (rlev1 <= AE_THR);

    always @(posedge rclk) begin
        wg_s1 <= wgray;
        wg_s2 <= wg_s1;
    end

    always @(posedge rclk) begin
        if (!rrst_n) begin
            rbin <= {(AW+1){1'b0}}; rgray <= {(AW+1){1'b0}};
            rbin1 <= {{AW{1'b0}}, 1'b1}; rgray1 <= {{AW{1'b0}}, 1'b1};
            rempty <= 1'b1; r_almost_empty <= 1'b1; r_level <= {(AW+1){1'b0}};
            wbin_r <= {(AW+1){1'b0}};
        end else begin
            if (r_do) begin
                rbin   <= rbin1;
                rgray  <= rgray1;
                rbin1  <= rbin2;
                rgray1 <= (rbin2 >> 1) ^ rbin2;
            end
            rempty         <= r_do ? rempty1 : rempty0;
            r_level        <= r_do ? rlev1   : rlev0;
            r_almost_empty <= r_do ? rae1    : rae0;
            wbin_r <= g2b(wg_s2);
        end
    end

    // FWFT read: address of the *next* word is used when advancing, so rdata
    // always equals mem[rbin] one edge after rbin changes; re-read every
    // cycle so a freshly written word is picked up before rempty drops.
    wire [AW-1:0] raddr = r_do ? rbin1[AW-1:0] : rbin[AW-1:0];
    initial rdata = {W{1'b0}};
    always @(posedge rclk) rdata <= mem[raddr];
endmodule
