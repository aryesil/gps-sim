// gs_seq - sample / block sequencer, bank swap, mute and counters.
//
// One sample = T consecutive clocks of s_valid with s_slot 0..T-1 (s_first at
// 0, s_last at T-1), T = max(4, ceil(nch/GS_P)).  Back-to-back samples are
// issued while enable && out_space (sampled in the decision clock = the
// s_last clock, or any clock while idle).
// Blocks are 2**GS_BLOCK_LOG2 (default 65536) samples.  At the first sample
// start of a block ("edge"):
//   - first block ever: waits for enable && cfg_valid && shadow_loaded &&
//     shadow_clean (no underrun counted while waiting), then swaps (bank
//     flips), play_idx = cfg_start, running = 1;
//   - later blocks: shadow_loaded && shadow_clean -> swap, play_idx++;
//     else underruns++, no swap, s_mute = 1 for the whole block, play_idx++.
// target_idx = cfg_start before the first block, play_idx+1 after.
// cfg_nch / cfg_start are sampled only while not running (CFG accepted only
// while stopped).  enable low only pauses new sample starts (running stays).
// The shadow bank must not be written for SWAP_QUIET = 16 clocks after a swap:
// load_enable is low from the clock in which `bank` changes for 16 clocks.
// (The invalidation of stale shadow entries is done by gs_loader's sweep.)
// All outputs are registered.  Latency: decision clock -> s_first clock: 1.
`include "gs_defs.vh"
`ifndef GS_SLOT_W
`define GS_SLOT_W 3
`endif
`ifndef GS_BLOCK_LOG2
`define GS_BLOCK_LOG2 16
`endif

module gs_seq (input clk, input rst, input enable,
  input cfg_valid, input [6:0] cfg_nch, input [63:0] cfg_start, input shadow_loaded, shadow_clean,
  input out_space,
  output [63:0] target_idx, output bank, output shadow_bank, output load_enable,
  output reg s_valid, (* max_fanout = 32 *) output reg [`GS_SLOT_W-1:0] s_slot, output s_bank, output reg s_first, s_last, output reg s_mute,
  output [63:0] play_idx, output [31:0] underruns, output running);

  localparam SWAP_QUIET = 16;

  reg        started, bank_r;
  reg [63:0] play_r, start_l, tgt_r, pr1, tg1, sl1;   // pr1/tg1/sl1: registered +1 copies (settled long before use)   // tgt_r == (started ? play_r + 1 : start_l), registered
  reg [6:0]  nch_l;
  reg [31:0] under_r;
  reg [`GS_BLOCK_LOG2-1:0] scnt;      // index of the next sample within its block
  reg [4:0]  quiet;
  reg        le_r;            // == (quiet == 0), registered
  reg        cont;            // == (s_valid && !s_last), registered (slot counter enable)

  wire [6:0] tc = (nch_l + `GS_P - 1) / `GS_P;
  wire [4:0] T  = (tc < 4) ? 5'd4 : tc[4:0];
  wire [4:0] slot_n2 = {{(5-`GS_SLOT_W){1'b0}}, s_slot} + 5'd2;

  assign bank       = bank_r;
  assign shadow_bank = ~bank_r;
  assign s_bank     = bank_r;
  assign play_idx   = play_r;
  assign underruns  = under_r;
  assign running    = started;
  assign target_idx = tgt_r;
  assign load_enable = le_r;

  wire ready_blk = shadow_loaded & shadow_clean;
  reg  at_edge;       // == (scnt == 0), registered
  // sl1 = source + 1 in two registered steps (low half + its carry, then the upper half):
  // the settle time of sl1 after cfg_start changes is 2 clocks (a block is loaded
  // long after the CFG that sets cfg_start, see gs_loader)
  wire [63:0] sl_src = (!started && cfg_valid) ? cfg_start : start_l;
  reg  [31:0] sl_lo1, sl_hi0;
  reg         sl_cy;
  always @(posedge clk) begin
    pr1 <= play_r + 64'd1; tg1 <= tgt_r + 64'd1;
    sl_lo1 <= sl_src[31:0] + 32'd1; sl_cy <= &sl_src[31:0]; sl_hi0 <= sl_src[63:32];
    sl1 <= {sl_hi0 + {31'd0, sl_cy}, sl_lo1};
  end

  always @(posedge clk) begin
    if (rst) begin
      started <= 1'b0; bank_r <= 1'b0; play_r <= 0; start_l <= 0; tgt_r <= 0; nch_l <= 0;
      under_r <= 0; scnt <= 0; at_edge <= 1'b1; quiet <= 0; le_r <= 1'b1; cont <= 1'b0; s_valid <= 1'b0; s_slot <= 0;
      s_first <= 1'b0; s_last <= 1'b0; s_mute <= 1'b0;
    end else begin
      if (quiet != 0) begin quiet <= quiet - 1'b1; le_r <= (quiet == 5'd1); end
      if (!started && cfg_valid) begin nch_l <= cfg_nch; start_l <= cfg_start; tgt_r <= cfg_start; end
      if (cont) begin
        s_slot <= s_slot + 1'b1; s_first <= 1'b0;
        s_last <= (slot_n2 == T); cont <= (slot_n2 != T);
      end else begin
        s_valid <= 1'b0; s_first <= 1'b0; s_last <= 1'b0; s_slot <= 0;
        if (enable && out_space) begin
          if (!started) begin
            if (cfg_valid && ready_blk) begin
              started <= 1'b1; bank_r <= ~bank_r; play_r <= start_l; tgt_r <= sl1; quiet <= SWAP_QUIET; le_r <= 1'b0;
              s_mute <= 1'b0;
              s_valid <= 1'b1; s_slot <= 0; s_first <= 1'b1; cont <= 1'b1; scnt <= scnt + 1'b1; at_edge <= (scnt == {`GS_BLOCK_LOG2{1'b1}});
            end
          end else begin
            if (at_edge) begin
              play_r <= pr1; tgt_r <= tg1;
              if (ready_blk) begin
                bank_r <= ~bank_r; quiet <= SWAP_QUIET; le_r <= 1'b0; s_mute <= 1'b0;
              end else begin
                under_r <= under_r + 1'b1; s_mute <= 1'b1;
              end
            end
            s_valid <= 1'b1; s_slot <= 0; s_first <= 1'b1; cont <= 1'b1; scnt <= scnt + 1'b1; at_edge <= (scnt == {`GS_BLOCK_LOG2{1'b1}});
          end
        end
      end
    end
  end
endmodule
