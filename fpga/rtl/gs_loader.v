// gs_loader - command FIFO, record parser and shadow-bank writer.
//
// Words (cmd_valid/cmd_data/cmd_ready) go into a GS_CMD_DEPTH-deep FIFO
// (inferred RAM, registered read with head prefetch).  The parser follows
// backend/fpga/cmdfmt.py exactly:
//   - CFG (6 words): latched into cfg_* at its last word (nch<=64 checked at
//     w1).  Consumed at any time; gs_seq decides when to accept it (stopped).
//   - BLOCK: idx == target_idx -> load; idx < target_idx -> whole record
//     dropped, dropped_late++; idx > target_idx -> parser waits (FIFO fills,
//     cmd_ready drops) until target_idx catches up.  A block whose target
//     disappears while it is loaded (swap/underrun) is dropped from there on
//     (dropped_late++).  A second block with idx == target while the first is
//     already loaded is also dropped (dropped_late++).
//   - Resync: a word that does not start a consistent record is skipped one
//     word at a time, bad_words++ per skipped word.  When a candidate header
//     fails its consistency check after its first words were consumed, those
//     words are replayed through a 3-word buffer, i.e. exactly what
//     cmdfmt.unpack_stream does (it restarts at header+1).
// Loading a block: first an invalidation sweep (all-zero PAR/ST, valid=0) of
// the entries for ch = nch .. GS_P*GS_SLOTS-1, then the nch channel records
// (one l_we per record, slot map pipe = ch mod GS_P, slot = ch div GS_P).
// Writes only while load_enable (sequencer: not in the SWAP_QUIET window).
// shadow_dirty pulses with every l_we; shadow_loaded rises LOADED_DLY clocks
// after the last write of the block for target_idx and falls when target_idx
// changes.
//
// Latencies: cmd_valid -> word in parser: 2 clocks; last record word ->
// l_we: 2 clocks (+ wait for load_enable); l_we -> shadow_loaded: 4 clocks.
`include "gs_defs.vh"
`include "gs_cmdfmt.vh"

`ifndef GS_CMD_DEPTH
`define GS_CMD_DEPTH 4096
`endif
`ifndef GS_SLOT_W
`define GS_SLOT_W 3
`endif

module gs_loader (input clk, input rst,
  input cmd_valid, input [31:0] cmd_data, output cmd_ready,
  input [63:0] target_idx, input shadow_bank, input load_enable,
  output reg l_we, output reg [3:0] l_pipe, output reg [`GS_SLOT_W-1:0] l_slot, output reg l_bank,
  output [`GS_PAR_W-1:0] l_par, output [`GS_ST_W-1:0] l_st,
  output reg shadow_dirty, output reg shadow_loaded,
  output reg cfg_valid, output reg [6:0] cfg_nch, output reg [31:0] cfg_fs, cfg_shift, output reg [63:0] cfg_start,
  output reg [31:0] dropped_late, bad_words, output [15:0] fifo_level);

  localparam DEPTH = `GS_CMD_DEPTH;
  localparam TOTAL = `GS_P * `GS_SLOTS;
  localparam LOADED_DLY = 4;

  function integer clog2; input integer v; integer i; begin
    clog2 = 0; for (i = 1; i < v; i = i * 2) clog2 = clog2 + 1; end
  endfunction
  localparam AW = clog2(DEPTH);

  // ------------------------------------------------------------------ FIFO
  reg [31:0] mem [0:DEPTH-1];
  reg [AW:0] wp, rp;
  reg [31:0] q;
  reg        hv;
  wire [AW:0] count = wp - rp;
  assign cmd_ready  = (count < DEPTH - 1);
  assign fifo_level = {{(15-AW){1'b0}}, count};
  wire wr_en = cmd_valid & cmd_ready;
  wire fifo_pop;
  wire [AW:0] raddr = fifo_pop ? rp + 1'b1 : rp;

  always @(posedge clk) begin
    if (wr_en) mem[wp[AW-1:0]] <= cmd_data;
    q <= mem[raddr[AW-1:0]];
  end
  always @(posedge clk) begin
    if (rst) begin wp <= 0; rp <= 0; hv <= 1'b0; end
    else begin
      if (wr_en) wp <= wp + 1'b1;
      rp <= raddr;
      hv <= (wp != raddr);
    end
  end

  // -------------------------------------------------------- source / replay
  reg [31:0] rq0, rq1, rq2;
  reg [1:0]  rq_n;
  wire        wv = (rq_n != 0) | hv;
  wire [31:0] w  = (rq_n != 0) ? rq0 : q;
  reg         take, fail;
  reg  [1:0]  fail_n;
  assign fifo_pop = take & (rq_n == 0);

  // ----------------------------------------------------------------- parser
  localparam S_HDR = 0, S_CFG = 1, S_B = 2, S_WAIT = 3, S_CLR = 4, S_REC = 5,
             S_WR = 6, S_FIN = 7, S_SKIP = 8;
  reg [3:0]  st;
  reg [2:0]  cnt;
  reg [15:0] hlen;
  reg [31:0] h1, h2, h3;
  reg [63:0] blk_idx;
  reg [6:0]  blk_nch;
  reg [9:0]  left;
  reg [3:0]  wi;
  reg [6:0]  cl_ch;
  reg [319:0] rec;
  reg        ld_done;
  reg [2:0]  ldly;
  reg [63:0] tgt_q;
  wire       tgt_chg = (target_idx != tgt_q);
  wire       tgt_eq  = (target_idx == blk_idx);
  wire       tgt_gt  = (blk_idx > target_idx);   // block is in the future

  // record fields
  wire [5:0]  r_ch   = rec[`GS_F_CH_HI:`GS_F_CH_LO];
  wire [5:0]  r_prn  = rec[`GS_F_PRN_HI:`GS_F_PRN_LO];
  wire [1:0]  r_kind = rec[`GS_F_KIND_HI:`GS_F_KIND_LO];
  wire        r_val  = rec[`GS_F_VALID_LO];
  wire        r_boc  = rec[`GS_F_BOC_LO];
  wire [1:0]  r_cboc = rec[`GS_F_CBOC_HI:`GS_F_CBOC_LO];
  wire [4:0]  r_sidx = rec[`GS_F_SIGN_IDX0_HI:`GS_F_SIGN_IDX0_LO];
  wire [42:0] r_cinc = {rec[`GS_F_CODE_INC_HI_HI:`GS_F_CODE_INC_HI_LO],
                        rec[`GS_F_CODE_INC_LO_HI:`GS_F_CODE_INC_LO_LO]};
  wire [31:0] r_cainc = rec[`GS_F_CARR_INC_HI:`GS_F_CARR_INC_LO];
  wire [31:0] r_caph  = rec[`GS_F_CARR_PHASE0_HI:`GS_F_CARR_PHASE0_LO];
  wire [31:0] r_sbits = rec[`GS_F_SIGN_BITS_HI:`GS_F_SIGN_BITS_LO];
  wire [51:0] r_cph   = rec[`GS_F_CODE_PHASE0_HI:`GS_F_CODE_PHASE0_LO];
  wire [51:0] r_sph   = rec[`GS_F_SIGN_PHASE0_HI:`GS_F_SIGN_PHASE0_LO];
  wire [17:0] r_c0 = rec[`GS_F_COEF0_HI:`GS_F_COEF0_LO];
  wire [17:0] r_c1 = rec[`GS_F_COEF1_HI:`GS_F_COEF1_LO];
  wire [17:0] r_c2 = rec[`GS_F_COEF2_HI:`GS_F_COEF2_LO];

  wire [`GS_PAR_W-1:0] par_w = {r_c2, r_c1, r_c0, r_sbits, r_cinc, r_cainc,
                                r_prn, r_kind, r_cboc, r_boc, r_val};
  wire [`GS_ST_W-1:0]  st_w  = {r_cph[51:45], r_sidx, r_sph, r_cph, r_caph};

  reg [`GS_PAR_W-1:0] par_r;
  reg [`GS_ST_W-1:0]  st_r;
  assign l_par = par_r;
  assign l_st  = st_r;

  wire [6:0] wr_ch = (st == S_CLR) ? cl_ch : {1'b0, r_ch};
  wire [6:0] wr_pipe = wr_ch % `GS_P;
  wire [6:0] wr_slot = wr_ch / `GS_P;

  task enter_skip; begin
    st <= S_SKIP; dropped_late <= dropped_late + 1'b1;
  end endtask

  always @* begin
    take = 1'b0; fail = 1'b0; fail_n = 2'd0;
    case (st)
      S_HDR: if (wv) take = 1'b1;
      S_CFG: if (wv) begin take = 1'b1; if (cnt == 1 && w > 64) begin fail = 1'b1; fail_n = 2'd1; end end
      S_B:   if (wv) begin
               take = 1'b1;
               if (cnt == 3 && !(w <= 64 && hlen == 4 + 10 * w)) begin fail = 1'b1; fail_n = 2'd3; end
             end
      S_REC: if (wv && tgt_eq) take = 1'b1;
      S_SKIP: if (wv && left != 0) take = 1'b1;
      default: ;
    endcase
  end

  always @(posedge clk) begin
    l_we <= 1'b0; shadow_dirty <= 1'b0;
    tgt_q <= target_idx;
    if (rst) begin
      st <= S_HDR; cnt <= 0; rq_n <= 0; ld_done <= 1'b0; ldly <= 0;
      shadow_loaded <= 1'b0; cfg_valid <= 1'b0; cfg_nch <= 0; cfg_fs <= 0;
      cfg_shift <= 0; cfg_start <= 0; dropped_late <= 0; bad_words <= 0;
      left <= 0; wi <= 0; cl_ch <= 0; blk_idx <= 0; blk_nch <= 0; hlen <= 0;
      tgt_q <= 0;
    end else begin
      // replay buffer shift (overridden by a failure below)
      if (take && rq_n != 0) begin
        rq0 <= rq1; rq1 <= rq2; rq_n <= rq_n - 1'b1;
      end
      if (take && fail) begin
        bad_words <= bad_words + 1'b1;
        st <= S_HDR; cnt <= 0;
        rq_n <= fail_n;
        if (fail_n == 2'd1) rq0 <= w;
        else begin rq0 <= h1; rq1 <= h2; rq2 <= w; end
      end else begin
        case (st)
          S_HDR: if (take) begin
            hlen <= w[15:0]; cnt <= 1;
            if (w[31:24] != `GS_CMD_MAGIC) bad_words <= bad_words + 1'b1;
            else if (w[23:16] == `GS_TYPE_CFG && w[15:0] == `GS_CFG_WORDS) st <= S_CFG;
            else if (w[23:16] == `GS_TYPE_BLOCK && w[15:0] >= `GS_BLOCK_HDR_WORDS) st <= S_B;
            else bad_words <= bad_words + 1'b1;
          end
          S_CFG: if (take) begin
            cnt <= cnt + 1'b1;
            case (cnt)
              1: h1 <= w;       // nch
              2: h2 <= w;       // fs
              3: h3 <= w;       // shift
              4: cfg_start[31:0] <= w;
              default: begin    // 5: start high, commit
                cfg_start[63:32] <= w; cfg_nch <= h1[6:0]; cfg_fs <= h2;
                cfg_shift <= h3; cfg_valid <= 1'b1; st <= S_HDR; cnt <= 0;
              end
            endcase
          end
          S_B: if (take) begin
            cnt <= cnt + 1'b1;
            case (cnt)
              1: h1 <= w;
              2: h2 <= w;
              default: begin
                blk_idx <= {h2, h1}; blk_nch <= w[6:0]; left <= 10 * w[6:0];
                wi <= 0; cl_ch <= w[6:0]; st <= S_WAIT; cnt <= 0;
              end
            endcase
          end
          S_WAIT: begin
            if (tgt_eq && (!ld_done || tgt_chg)) st <= S_CLR;
            else if (!tgt_gt) enter_skip;     // idx < target, or duplicate
          end
          S_CLR: begin
            if (!tgt_eq) enter_skip;
            else if (cl_ch >= TOTAL) st <= (blk_nch == 0) ? S_FIN : S_REC;
            else if (load_enable) begin
              l_we <= 1'b1; shadow_dirty <= 1'b1; l_bank <= shadow_bank;
              l_pipe <= wr_pipe[3:0]; l_slot <= wr_slot[`GS_SLOT_W-1:0];
              par_r <= 0; st_r <= 0;
              cl_ch <= cl_ch + 1'b1;
              if (cl_ch == TOTAL - 1) st <= (blk_nch == 0) ? S_FIN : S_REC;
            end
          end
          S_REC: begin
            if (!tgt_eq) enter_skip;
            else if (take) begin
              rec <= {w, rec[319:32]};
              left <= left - 1'b1;
              if (wi == 9) begin wi <= 0; st <= S_WR; end
              else wi <= wi + 1'b1;
            end
          end
          S_WR: begin
            if (!tgt_eq) enter_skip;
            else if (load_enable) begin
              l_we <= 1'b1; shadow_dirty <= 1'b1; l_bank <= shadow_bank;
              l_pipe <= wr_pipe[3:0]; l_slot <= wr_slot[`GS_SLOT_W-1:0];
              par_r <= par_w; st_r <= st_w;
              st <= (left == 0) ? S_FIN : S_REC;
            end
          end
          S_FIN: begin
            if (tgt_eq) begin ld_done <= 1'b1; ldly <= LOADED_DLY; st <= S_HDR; end
            else enter_skip;
          end
          S_SKIP: begin
            if (left == 0) begin st <= S_HDR; cnt <= 0; end
            else if (take) left <= left - 1'b1;
          end
          default: st <= S_HDR;
        endcase
      end
      // loaded flag
      if (ldly != 0) begin
        ldly <= ldly - 1'b1;
        if (ldly == 1) shadow_loaded <= 1'b1;
      end
      if (tgt_chg) begin
        shadow_loaded <= 1'b0; ld_done <= 1'b0; ldly <= 0;
      end
    end
  end
endmodule
