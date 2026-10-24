`timescale 1ns/1fs
// gt_clk.v - timebase clock for gpsdo_tb: 40 MHz VCTCXO -> 200 MHz.
//
// Synthesis: MMCME2_BASE (VCO 40 MHz x 25 = 1000 MHz, CLKOUT0 / 5 = 200 MHz)
// followed by a BUFG; locked is the MMCM LOCKED output (asynchronous to tb_clk).
// Simulation (-d GT_SIM): behavioural model.  The 200 MHz clock free-runs with a
// period equal to one fifth of the last measured clk40 period, so ppm offsets of
// clk40 carry over to tb_clk.  locked rises after four clk40 edges.
module gt_clk (
    input  wire clk40,
    output wire tb_clk,
    output wire locked
);
`ifdef GT_SIM
    real    t_last   = 0.0;
    real    per_est  = 25.0;
    integer n_edges  = 0;
    reg     clk_r    = 1'b0;
    reg     lock_r   = 1'b0;
    always @(posedge clk40) begin
        if (n_edges > 0) per_est = $realtime - t_last;
        t_last  = $realtime;
        n_edges = n_edges + 1;
        if (n_edges >= 4) lock_r = 1'b1;
    end
    initial forever #(per_est / 10.0) clk_r = ~clk_r;
    assign tb_clk = clk_r;
    assign locked = lock_r;
`else
    wire fb, clk0, locked_i;
    MMCME2_BASE #(
        .BANDWIDTH("OPTIMIZED"),
        .CLKIN1_PERIOD(25.000),
        .DIVCLK_DIVIDE(1),
        .CLKFBOUT_MULT_F(25.000),
        .CLKOUT0_DIVIDE_F(5.000),
        .STARTUP_WAIT("FALSE")
    ) u_mmcm (
        .CLKIN1(clk40), .CLKFBIN(fb), .CLKFBOUT(fb), .CLKOUT0(clk0),
        .LOCKED(locked_i), .PWRDWN(1'b0), .RST(1'b0)
    );
    BUFG u_bufg (.I(clk0), .O(tb_clk));
    assign locked = locked_i;
`endif
endmodule
