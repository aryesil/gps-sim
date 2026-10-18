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
  (* max_fanout = 32 *) output reg l_we, (* max_fanout = 32 *) output reg [3:0] l_pipe, (* max_fanout = 32 *) output reg [`GS_SLOT_W-1:0] l_slot, (* max_fanout = 32 *) output reg l_bank,
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
  wire [AW:0] count = wp - rp;            // words in the RAM not yet read
  // cmd_ready is registered (timing): it drops when count >= DEPTH-4 (one clock
  // late, at most one extra word can still arrive), so the FIFO never overflows.
  reg rdy_r;
  assign cmd_ready  = rdy_r;
  always @(posedge clk) rdy_r <= rst ? 1'b1 : (count < DEPTH - 4);
  wire wr_en = cmd_valid & cmd_ready;
  wire fifo_pop;

  // RAM read address is the plain read pointer (no dependence on the parser);
  // words read are collected in a 4-entry skid s0..s3 (s0 = head).  A read is
  // issued only while skid + in-flight <= 2 (registered counts only).
  // skid: 4-entry circular buffer sb0..sb3 (hp = head index, sk_n = entries); a pop
  // only moves hp / sk_n (no data shifting, so the pop does not fan out to the
  // data flops' enables)
  reg [31:0] sb0, sb1, sb2, sb3;
  reg  [1:0]  hp;
  reg  [2:0]  sk_n;
  reg         infl, hv;
  wire        issue = (wp != rp) & ({1'b0, sk_n} + infl <= 4'd2);
  assign fifo_level = {{(15-AW){1'b0}}, count} + sk_n + infl;
  always @(posedge clk) begin
    if (wr_en) mem[wp[AW-1:0]] <= cmd_data;
    q <= mem[rp[AW-1:0]];
  end
  wire [1:0]  wix = hp + sk_n[1:0];                  // slot of the word arriving from the RAM
  wire [31:0] s0  = (hp == 2'd0) ? sb0 : (hp == 2'd1) ? sb1 : (hp == 2'd2) ? sb2 : sb3;
  wire [2:0]  sk_nx = sk_n + infl - fifo_pop;
  always @(posedge clk) begin
    if (infl && wix == 2'd0) sb0 <= q;
    if (infl && wix == 2'd1) sb1 <= q;
    if (infl && wix == 2'd2) sb2 <= q;
    if (infl && wix == 2'd3) sb3 <= q;
  end
  always @(posedge clk) begin
    if (rst) begin wp <= 0; rp <= 0; sk_n <= 0; hp <= 0; infl <= 1'b0; hv <= 1'b0; end
    else begin
      if (wr_en) wp <= wp + 1'b1;
      if (issue) rp <= rp + 1'b1;
      infl <= issue;
      if (fifo_pop) hp <= hp + 1'b1;
      sk_n <= sk_nx; hv <= (sk_nx != 0);
    end
  end

  // -------------------------------------------------------- source / replay
  // replay buffer: 4-entry circular buffer, rqp = head index (like the skid), so a
  // take only moves rqp / rq_n (4 flops) and never the CE of the 96 data flops
  reg [31:0] rqm0, rqm1, rqm2, rqm3;
  reg [1:0]  rqp;
  reg [1:0]  rq_n;
  // registered copies of (rq_n != 0): one for the control (take / wv), one for the
  // word select (32-bit mux); dont_touch so the two are not merged
  (* dont_touch = "true" *) reg rq_nzt, rq_nzw;
  wire        wv = rq_nzt | hv;
  wire [31:0] rqh = (rqp == 2'd0) ? rqm0 : (rqp == 2'd1) ? rqm1 : (rqp == 2'd2) ? rqm2 : rqm3;
  wire [31:0] w  = rq_nzw ? rqh : s0;
  wire [1:0]  rqp_m1 = rqp - 2'd1;
  reg         take;
  assign fifo_pop = take & ~rq_nzt;

  // ----------------------------------------------------------------- parser
  localparam S_HDR = 0, S_CFG = 1, S_B = 2, S_WAIT = 3, S_CLR = 4, S_REC = 5,
             S_WR = 6, S_FIN = 7, S_SKIP = 8, S_SET = 9, S_HD2 = 10, S_HD3 = 11,
             S_RPC = 12, S_RPB = 13;      // replay load after a failed CFG / BLOCK header
  (* fsm_encoding = "one_hot" *) reg [3:0]  st;
  reg [2:0]  cnt;
  reg [15:0] hlen;
  reg [31:0] h0, h1, h2, h3;
  reg [63:0] blk_idx;
  reg [6:0]  blk_nch;
  reg [9:0]  left;
  reg        left_nz;                        // == (left != 0), registered
  reg [3:0]  wi;
  reg [6:0]  cl_ch;
  reg [319:0] rec;
  reg        ld_done;
  reg [2:0]  ldly;
  reg [63:0] tgt_q;
  // registered compare flags (64-bit compares are too slow for one clock next
  // to the state machine); valid one clock after blk_idx/target_idx change,
  // hence the S_SET settle state after blk_idx is loaded.
  reg        tgt_chg, tgt_eq, tgt_gt;           // gt: block is in the future
  always @(posedge clk) begin
    tgt_chg <= (target_idx != tgt_q);
    tgt_eq  <= (target_idx == blk_idx);
    tgt_gt  <= (blk_idx > target_idx);
  end
  // deferred header checks (registered arithmetic)
  reg [10:0] chk_len;
  reg        chk_big, chk_bad, hd_cfg, hd_blk, h1_big;

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
  wire [`GS_ST_W-1:0]  st_w  = {r_cph[51:45], r_sidx, r_sph, r_cph, (r_caph + 32'h0008_0000)};

  reg [`GS_PAR_W-1:0] par_r;
  reg [`GS_ST_W-1:0]  st_r;
  assign l_par = par_r;
  assign l_st  = st_r;

  wire [6:0] wr_ch = (st == S_CLR) ? cl_ch : {1'b0, r_ch};
  wire [6:0] wr_pipe = wr_ch % `GS_P;
  wire [6:0] wr_slot = wr_ch / `GS_P;

  // par_r/st_r are loaded every clock of S_CLR (zero) / S_WR (record): the value is
  // stable there, so no enable depends on load_enable; l_we marks the valid clock.
  always @(posedge clk) begin
    if (st == S_CLR) begin par_r <= 0; st_r <= 0; end
    else if (st == S_WR) begin par_r <= par_w; st_r <= st_w; end
  end

  task enter_skip; begin
    st <= S_SKIP; dropped_late <= dropped_late + 1'b1;
  end endtask

  always @* begin
    take = 1'b0;
    case (st)
      S_HDR: if (wv) take = 1'b1;
      S_CFG: if (wv && cnt != 6 && cnt != 7) take = 1'b1;     // cnt 7, 6: check cycles
      S_B:   if (wv && cnt <= 3) take = 1'b1;     // cnt 4, 5: check cycles
      S_REC: if (wv && tgt_eq) take = 1'b1;
      S_SKIP: if (wv && left_nz) take = 1'b1;
      default: ;
    endcase
  end

  always @(posedge clk) begin
    l_we <= 1'b0; shadow_dirty <= 1'b0;
    // address bus registered every clock (no load_enable in its path); it is only
    // meaningful in the clock where l_we is high
    l_bank <= shadow_bank; l_pipe <= wr_pipe[3:0]; l_slot <= wr_slot[`GS_SLOT_W-1:0];
    tgt_q <= target_idx;
    if (rst) begin
      st <= S_HDR; cnt <= 0; rq_n <= 0; rqp <= 0; rq_nzt <= 1'b0; rq_nzw <= 1'b0; ld_done <= 1'b0; ldly <= 0;
      shadow_loaded <= 1'b0; cfg_valid <= 1'b0; cfg_nch <= 0; cfg_fs <= 0;
      cfg_shift <= 0; cfg_start <= 0; dropped_late <= 0; bad_words <= 0;
      left <= 0; left_nz <= 1'b0; wi <= 0; cl_ch <= 0; blk_idx <= 0; blk_nch <= 0; hlen <= 0;
      tgt_q <= 0;
    end else begin
      // replay buffer shift (overridden by a failure below)
      if (take && rq_nzt) begin
        rqp <= rqp + 1'b1; rq_n <= rq_n - 1'b1;
        rq_nzt <= (rq_n != 2'd1); rq_nzw <= (rq_n != 2'd1);
      end
      begin
        case (st)
          S_HDR: if (take) begin           // decode in the next clock (from the register)
            h0 <= w; hlen <= w[15:0]; cnt <= 1; st <= S_HD2;
          end
          S_HD2: begin                     // header decode, registered flags
            hd_cfg <= (h0[31:24] == `GS_CMD_MAGIC && h0[23:16] == `GS_TYPE_CFG && h0[15:0] == `GS_CFG_WORDS);
            hd_blk <= (h0[31:24] == `GS_CMD_MAGIC && h0[23:16] == `GS_TYPE_BLOCK &&
                       h0[15:0] >= `GS_BLOCK_HDR_WORDS);
            st <= S_HD3;
          end
          S_HD3: begin
            if (hd_cfg) st <= S_CFG;
            else if (hd_blk) st <= S_B;
            else begin bad_words <= bad_words + 1'b1; st <= S_HDR; cnt <= 0; end
          end
          S_CFG: if (take || cnt >= 6) begin
            if (cnt != 1 && cnt < 6) cnt <= cnt + 1'b1;
            case (cnt)
              1: begin h1 <= w; cnt <= 7; end   // nch; compared in cnt 7, checked in cnt 6
              7: begin h1_big <= (h1 > 32'd64); cnt <= 6; end
              6: begin
                if (h1_big) begin               // bad nch: restart at header+1
                  bad_words <= bad_words + 1'b1;
                  st <= S_RPC;
                end else cnt <= 2;
              end
              2: h2 <= w;       // fs
              3: h3 <= w;       // shift
              4: cfg_start[31:0] <= w;
              default: begin    // 5: start high, commit
                cfg_start[63:32] <= w; cfg_nch <= h1[6:0]; cfg_fs <= h2;
                cfg_shift <= h3; cfg_valid <= 1'b1; st <= S_HDR; cnt <= 0;
              end
            endcase
          end
          S_B: if (take || cnt >= 4) begin
            cnt <= cnt + 1'b1;
            case (cnt)
              1: h1 <= w;
              2: h2 <= w;
              3: h3 <= w;
              4: begin
                chk_len <= 11'd4 + {h3[6:0], 3'b000} + {h3[6:0], 1'b0};   // 4 + 10*nch
                chk_big <= (|h3[31:7]) | (h3[6:0] > 7'd64);
              end
              5: chk_bad <= chk_big || (hlen[10:0] != chk_len) || (|hlen[15:11]);
              default: begin       // 6
                if (chk_bad) begin
                  bad_words <= bad_words + 1'b1;     // restart at header+1
                  st <= S_RPB;
                end else begin
                  blk_idx <= {h2, h1}; blk_nch <= h3[6:0]; left <= chk_len[9:0] - 10'd4; left_nz <= (chk_len != 11'd4);
                  wi <= 0; cl_ch <= h3[6:0]; st <= S_SET; cnt <= 0;
                end
              end
            endcase
          end
          S_RPC: begin                     // replay after a bad CFG: h1 goes in front of the buffer
            st <= S_HDR; cnt <= 0;
            rqp <= rqp_m1; rq_n <= rq_n + 1'b1; rq_nzt <= 1'b1; rq_nzw <= 1'b1;
            case (rqp_m1)
              2'd0: rqm0 <= h1;
              2'd1: rqm1 <= h1;
              2'd2: rqm2 <= h1;
              default: rqm3 <= h1;
            endcase
          end
          S_RPB: begin                     // replay after a bad BLOCK header: h1, h2, h3
            st <= S_HDR; cnt <= 0;
            rqm0 <= h1; rqm1 <= h2; rqm2 <= h3; rqp <= 2'd0; rq_n <= 2'd3; rq_nzt <= 1'b1; rq_nzw <= 1'b1;
          end
          S_SET: st <= S_WAIT;
          S_WAIT: begin
            if (tgt_eq && (!ld_done || tgt_chg)) st <= S_CLR;
            else if (!tgt_gt) enter_skip;     // idx < target, or duplicate
          end
          S_CLR: begin
            if (!tgt_eq) enter_skip;
            else if (cl_ch >= TOTAL) st <= (blk_nch == 0) ? S_FIN : S_REC;
            else if (load_enable) begin
              l_we <= 1'b1; shadow_dirty <= 1'b1;
              cl_ch <= cl_ch + 1'b1;
              if (cl_ch == TOTAL - 1) st <= (blk_nch == 0) ? S_FIN : S_REC;
            end
          end
          S_REC: begin
            if (!tgt_eq) enter_skip;
            else if (take) begin
              rec <= {w, rec[319:32]};
              left <= left - 1'b1; left_nz <= (left != 10'd1);
              if (wi == 9) begin wi <= 0; st <= S_WR; end
              else wi <= wi + 1'b1;
            end
          end
          S_WR: begin
            if (!tgt_eq) enter_skip;
            else if (load_enable) begin
              l_we <= 1'b1; shadow_dirty <= 1'b1;
              st <= (!left_nz) ? S_FIN : S_REC;
            end
          end
          S_FIN: begin
            if (tgt_eq) begin ld_done <= 1'b1; ldly <= LOADED_DLY; st <= S_HDR; end
            else enter_skip;
          end
          S_SKIP: begin
            if (!left_nz) begin st <= S_HDR; cnt <= 0; end
            else if (take) begin left <= left - 1'b1; left_nz <= (left != 10'd1); end
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
