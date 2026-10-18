// gs_axil.v - minimal AXI4-Lite slave register front end (single clock).
//
// 32-bit data, 12-bit address, word registers.  Everything is in the AXI clock
// domain; this module has no clock crossings.
//   Read : rd_regs[32*i +: 32] is returned for word index i = addr[5:2]
//          (0..15); addresses with addr[11:6] != 0 read as 0.
//   Write: only word index 1 (offset 0x04, CTRL) is writable, byte lane 0 only:
//            bit0 enable, bit1 cmd_mode      level registers
//            bit2 soft_reset, bit3 clear     one-cycle pulses (not stored)
//          all other writes are accepted and ignored.  Responses are always OKAY.
// Channel handling: AW and W are accepted independently (one outstanding
// write); the write is performed when both have arrived and B is not pending.
// AR is accepted when R is not pending.
module gs_axil (
    input  wire         clk,
    input  wire         rst_n,
    input  wire [11:0]  awaddr,
    input  wire         awvalid,
    output reg          awready,
    input  wire [31:0]  wdata,
    input  wire [3:0]   wstrb,
    input  wire         wvalid,
    output reg          wready,
    output wire [1:0]   bresp,
    output reg          bvalid,
    input  wire         bready,
    input  wire [11:0]  araddr,
    input  wire         arvalid,
    output reg          arready,
    output reg  [31:0]  rdata,
    output wire [1:0]   rresp,
    output reg          rvalid,
    input  wire         rready,

    output reg          ctl_enable,
    output reg          ctl_cmd_mode,
    output reg          soft_reset_pulse,
    output reg          clear_pulse,
    input  wire [511:0] rd_regs
);
    assign bresp = 2'b00;
    assign rresp = 2'b00;

    reg        aw_have, w_have;
    reg [11:0] aw_q;
    reg [31:0] w_q;
    reg [3:0]  ws_q;

    wire do_wr = aw_have & w_have & ~bvalid;

    always @(posedge clk) begin
        if (!rst_n) begin
            awready <= 1'b0; wready <= 1'b0; bvalid <= 1'b0;
            aw_have <= 1'b0; w_have <= 1'b0;
            aw_q <= 12'd0; w_q <= 32'd0; ws_q <= 4'd0;
            ctl_enable <= 1'b0; ctl_cmd_mode <= 1'b0;
            soft_reset_pulse <= 1'b0; clear_pulse <= 1'b0;
        end else begin
            soft_reset_pulse <= 1'b0;
            clear_pulse      <= 1'b0;
            // accept AW / W (ready pulses for one cycle when a slot is free)
            awready <= ~aw_have & ~awready & ~bvalid;
            wready  <= ~w_have & ~wready & ~bvalid;
            if (awvalid & awready) begin aw_have <= 1'b1; aw_q <= awaddr; end
            if (wvalid & wready)   begin w_have  <= 1'b1; w_q <= wdata; ws_q <= wstrb; end
            if (do_wr) begin
                aw_have <= 1'b0; w_have <= 1'b0; bvalid <= 1'b1;
                if (aw_q[5:2] == 4'd1 && aw_q[11:6] == 6'd0 && ws_q[0]) begin
                    ctl_enable       <= w_q[0];
                    ctl_cmd_mode     <= w_q[1];
                    soft_reset_pulse <= w_q[2];
                    clear_pulse      <= w_q[3];
                end
            end
            if (bvalid & bready) bvalid <= 1'b0;
        end
    end

    // read channel
    reg [11:0] ar_q;
    always @(posedge clk) begin
        if (!rst_n) begin
            arready <= 1'b0; rvalid <= 1'b0; rdata <= 32'd0; ar_q <= 12'd0;
        end else begin
            arready <= ~rvalid & ~arready & ~(arvalid & arready);
            if (arvalid & arready) begin
                rvalid <= 1'b1;
                ar_q   <= araddr;
                rdata  <= (araddr[11:6] == 6'd0) ? rd_regs[{araddr[5:2], 5'd0} +: 32] : 32'd0;
            end else if (rvalid & rready) begin
                rvalid <= 1'b0;
            end
        end
    end
endmodule
