// Stub of gs_wrap with the final port list (plan P2). Used ONLY to validate the
// maia-sdr tezuka_gnss block design before the real RTL exists.
// Use: GNSS_RTL_DIR=D:/codes/gps-sim/fpga/stub
`timescale 1ns/1ps
module gs_wrap (
  (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 clk_sys CLK", X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF s_axi, ASSOCIATED_RESET rst_sys_n" *) input clk_sys,
  (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 rst_sys_n RST", X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *) input rst_sys_n,
  input clk_div, input rst_div_n,
  input clk_eng, input rst_eng_n,
  input  [11:0] s_axi_awaddr, input s_axi_awvalid, output s_axi_awready,
  input  [31:0] s_axi_wdata, input [3:0] s_axi_wstrb, input s_axi_wvalid, output s_axi_wready,
  output [1:0] s_axi_bresp, output s_axi_bvalid, input s_axi_bready,
  input  [11:0] s_axi_araddr, input s_axi_arvalid, output s_axi_arready,
  output [31:0] s_axi_rdata, output [1:0] s_axi_rresp, output s_axi_rvalid, input s_axi_rready,
  input  [31:0] up_data, input up_valid, output up_rd_en,
  input  dm_req_out, input dm_valid_out, input [15:0] dm_i0, input [15:0] dm_q0,
  input  dac_req,
  output tx_valid0, output [15:0] tx_i0, output [15:0] tx_q0,
  output cmd_mode_div,
  output irq
);
  assign s_axi_awready = 1'b1; assign s_axi_wready = 1'b1;
  assign s_axi_bresp = 2'b00;  assign s_axi_bvalid = s_axi_wvalid;
  assign s_axi_arready = 1'b1; assign s_axi_rdata = 32'h47530001;
  assign s_axi_rresp = 2'b00;  assign s_axi_rvalid = s_axi_arvalid;
  assign up_rd_en = dm_req_out;
  assign tx_valid0 = dm_valid_out; assign tx_i0 = dm_i0; assign tx_q0 = dm_q0;
  assign cmd_mode_div = 1'b0; assign irq = 1'b0;
endmodule
