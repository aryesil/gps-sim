// gt_axil.v - AXI4-Lite slave bus front end for gpsdo_tb (single clock).
//
// 32-bit data, 12-bit address, word registers.  The register file lives in
// gpsdo_tb; this module only runs the bus protocol:
//   write : wr_en pulses for one cycle with wr_idx = addr[7:2] and wr_data
//           (byte lane 0..3 enables are merged against wr_old by gpsdo_tb, the
//           bus layer only passes wstrb through).  Addresses with addr[11:8]
//           != 0 are accepted and ignored.
//   read  : rd_idx = araddr[7:2]; rd_data is sampled combinationally in the
//           cycle the AR channel is accepted (rd_fire pulses in that cycle),
//           addresses with addr[11:8] != 0 read as 0.
// AW and W are accepted independently (one outstanding write); the write is
// performed when both have arrived and B is not pending.  AR is accepted when
// R is not pending.  Responses are always OKAY.
module gt_axil (
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

    output reg          wr_en,
    output reg  [5:0]   wr_idx,
    output reg  [31:0]  wr_data,
    output reg  [3:0]   wr_strb,
    output wire         rd_fire,
    output wire [5:0]   rd_idx,
    input  wire [31:0]  rd_data
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
            wr_en <= 1'b0; wr_idx <= 6'd0; wr_data <= 32'd0; wr_strb <= 4'd0;
        end else begin
            wr_en <= 1'b0;
            awready <= ~aw_have & ~awready & ~bvalid;
            wready  <= ~w_have & ~wready & ~bvalid;
            if (awvalid & awready) begin aw_have <= 1'b1; aw_q <= awaddr; end
            if (wvalid & wready)   begin w_have  <= 1'b1; w_q <= wdata; ws_q <= wstrb; end
            if (do_wr) begin
                aw_have <= 1'b0; w_have <= 1'b0; bvalid <= 1'b1;
                if (aw_q[11:8] == 4'd0) begin
                    wr_en   <= 1'b1;
                    wr_idx  <= aw_q[7:2];
                    wr_data <= w_q;
                    wr_strb <= ws_q;
                end
            end
            if (bvalid & bready) bvalid <= 1'b0;
        end
    end

    // read channel
    assign rd_idx  = araddr[7:2];
    assign rd_fire = arvalid & arready & (araddr[11:8] == 4'd0);

    always @(posedge clk) begin
        if (!rst_n) begin
            arready <= 1'b0; rvalid <= 1'b0; rdata <= 32'd0;
        end else begin
            arready <= ~rvalid & ~arready & ~(arvalid & arready);
            if (arvalid & arready) begin
                rvalid <= 1'b1;
                rdata  <= (araddr[11:8] == 4'd0) ? rd_data : 32'd0;
            end else if (rvalid & rready) begin
                rvalid <= 1'b0;
            end
        end
    end
endmodule
