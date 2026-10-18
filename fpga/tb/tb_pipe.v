// tb_pipe.v - gs_pipe (PIDX=0) against the golden per-sample channel-term sums.
//
// Cases (CASE_ID): 0 ca1, 1 e1b, 2 e1c (CBOC), 3 b1i, 4 pair (ca1 in slot 0 +
// e1c in slot 1), 5 mute (ca1: 32 muted samples must give 0, state keeps
// advancing, so the following samples equal the golden terms), 255 = all.
// Defines: -d CASE_ID=n, -d T_SLOTS=4..8 (slots per sample, idle slots invalid),
// -d NBLK=2 (blocks per case; block b+1 is loaded into the shadow bank while
// block b plays and the bank flips at the block edge), -d NSAMP_BLK=65536
// (samples per block, for quick runs), -d GAP=1 (idle clocks every 997 samples).
// Vectors: fpga/vec/<case>_{par,st,term}.hex from scripts/fpga_vectors.py.
// A behavioural scanner (ROM gs_rom.mem + base table gs_base.mem) services the
// sc_* port of the DUT.  Prints PASS or FAIL <n> as the last line.
`timescale 1ns/1ps
`include "gs_defs.vh"

`ifndef CASE_ID
`define CASE_ID 255
`endif
`ifndef T_SLOTS
`define T_SLOTS 4
`endif
`ifndef NBLK
`define NBLK 2
`endif
`ifndef NSAMP_BLK
`define NSAMP_BLK 65536
`endif
`ifndef GAP
`define GAP 1
`endif
`ifndef GS_VECDIR
`define GS_VECDIR {`GS_MEMDIR, "../vec/"}
`endif

module tb_pipe;
    localparam T = `T_SLOTS;
    reg clk = 1'b0;
    reg rst = 1'b1;
    always #2 clk = ~clk;

    reg         s_valid = 0, s_bank = 0, s_first = 0, s_last = 0, s_mute = 0;
    reg   [2:0] s_slot = 0;
    reg         l_we = 0, l_bank = 0;
    reg   [3:0] l_pipe = 0;
    reg   [2:0] l_slot = 0;
    reg [`GS_PAR_W-1:0] l_par = 0;
    reg [`GS_ST_W-1:0]  l_st = 0;
    reg   [2:0] sc_slot = 0;
    reg         sc_bank = 0;
    wire [`GS_SCI_W-1:0] sc_info;
    reg         sc_we = 0, sc_wbank = 0, sc_wwhich = 0;
    reg   [3:0] sc_wpipe = 0;
    reg   [2:0] sc_wslot = 0;
    reg  [31:0] sc_wword = 0;
    reg   [6:0] sc_wwidx = 0;
    wire        o_valid, o_chip_late;
    wire signed [37:0] o_i, o_q;

    gs_pipe #(.PIDX(0)) dut (
        .clk(clk), .rst(rst),
        .s_valid(s_valid), .s_slot(s_slot), .s_bank(s_bank), .s_first(s_first),
        .s_last(s_last), .s_mute(s_mute),
        .l_we(l_we), .l_pipe(l_pipe), .l_slot(l_slot), .l_bank(l_bank),
        .l_par(l_par), .l_st(l_st),
        .sc_slot(sc_slot), .sc_bank(sc_bank), .sc_info(sc_info),
        .sc_we(sc_we), .sc_wpipe(sc_wpipe), .sc_wslot(sc_wslot), .sc_wbank(sc_wbank),
        .sc_wwhich(sc_wwhich), .sc_wword(sc_wword), .sc_wwidx(sc_wwidx),
        .o_valid(o_valid), .o_i(o_i), .o_q(o_q), .o_chip_late(o_chip_late));

    // ------------------------------------------------ behavioural scanner
    reg [31:0] rom  [0:17855];
    reg [15:0] base [0:255];
    initial begin
        $readmemh({`GS_MEMDIR, "gs_rom.mem"}, rom);
        $readmemh({`GS_MEMDIR, "gs_base.mem"}, base);
    end

    function [7:0] wpc;
        input [1:0] kind;
        begin
            case (kind)
                2'd0: wpc = 8'd32;
                2'd3: wpc = 8'd64;
                default: wpc = 8'd128;
            endcase
        end
    endfunction

    reg [3:0] sctr = 0;
    wire        i_valid = sc_info[0];
    wire [1:0]  i_kind  = sc_info[2:1];
    wire [5:0]  i_prn   = sc_info[8:3];
    wire        i_curl  = sc_info[9];
    wire [6:0]  i_curw  = sc_info[16:10];
    wire [6:0]  i_nxtw  = sc_info[23:17];
    wire        i_nxtl  = sc_info[24];
    wire [6:0]  i_succ  = ({1'b0, i_curw} + 8'd1 >= wpc(i_kind)) ? 7'd0 : i_curw + 7'd1;
    wire [15:0] i_base  = base[{i_kind, i_prn}];
    always @(posedge clk) begin
        sc_we <= 1'b0;
        if (i_valid && !rst) begin
            if (!i_curl) begin
                sc_we <= 1'b1; sc_wwhich <= 1'b0; sc_wbank <= sc_bank; sc_wslot <= sc_slot;
                sc_wword <= rom[i_base + i_curw]; sc_wwidx <= i_curw;
            end else if (!i_nxtl || i_nxtw != i_succ) begin
                sc_we <= 1'b1; sc_wwhich <= 1'b1; sc_wbank <= sc_bank; sc_wslot <= sc_slot;
                sc_wword <= rom[i_base + i_succ]; sc_wwidx <= i_succ;
            end
        end
        sctr <= sctr + 4'd1;
        sc_slot <= sctr[2:0] + 3'd1;
        sc_bank <= (sctr == 4'd15) ? 1'b0 : ((sctr[2:0] == 3'd7) ? 1'b1 : sc_bank);
    end

    // ---------------------------------------------------------- checker
    reg [79:0]  expm [0:131071];
    reg [`GS_PAR_W-1:0] parm [0:15];
    reg [`GS_ST_W-1:0]  stm  [0:15];
    integer errs = 0, oidx = 0, mute_n = 0, nsb = `NSAMP_BLK, late_cnt = 0;
    reg chk_en = 1'b0;
    integer eidx;
    reg [79:0] ex;
    always @(posedge clk) begin
        if (o_chip_late) late_cnt = late_cnt + 1;
        if (o_valid && chk_en) begin
            eidx = (oidx / nsb) * 65536 + (oidx % nsb);
            ex = (oidx < mute_n) ? 80'd0 : expm[eidx];
            if (o_i[37:0] !== ex[77:40] || o_q[37:0] !== ex[37:0]) begin
                errs = errs + 1;
                if (errs <= 10)
                    $display("mismatch sample %0d: i=%h q=%h exp i=%h q=%h", oidx,
                             o_i, o_q, ex[77:40], ex[37:0]);
            end
            oidx = oidx + 1;
        end
    end

    // ----------------------------------------------------------- drivers
    task idle;
        input integer n;
        integer k;
        begin
            for (k = 0; k < n; k = k + 1) begin
                @(posedge clk); #1;
                s_valid = 0; s_first = 0; s_last = 0; l_we = 0;
            end
        end
    endtask

    task load;
        input b;
        input [2:0] slot;
        input [`GS_PAR_W-1:0] par;
        input [`GS_ST_W-1:0] st;
        begin
            @(posedge clk); #1;
            l_we = 1; l_pipe = 0; l_bank = b; l_slot = slot; l_par = par; l_st = {st[`GS_ST_W-1:32], st[31:0] + 32'h0008_0000};  // loader-side +2^19 offset
            @(posedge clk); #1;
            l_we = 0;
        end
    endtask

    task load_vectors;
        input integer id;
        begin
            case (id)
                0, 5: begin
                    $readmemh({`GS_VECDIR, "ca1_par.hex"}, parm);
                    $readmemh({`GS_VECDIR, "ca1_st.hex"}, stm);
                    $readmemh({`GS_VECDIR, "ca1_term.hex"}, expm);
                end
                1: begin
                    $readmemh({`GS_VECDIR, "e1b_par.hex"}, parm);
                    $readmemh({`GS_VECDIR, "e1b_st.hex"}, stm);
                    $readmemh({`GS_VECDIR, "e1b_term.hex"}, expm);
                end
                2: begin
                    $readmemh({`GS_VECDIR, "e1c_par.hex"}, parm);
                    $readmemh({`GS_VECDIR, "e1c_st.hex"}, stm);
                    $readmemh({`GS_VECDIR, "e1c_term.hex"}, expm);
                end
                3: begin
                    $readmemh({`GS_VECDIR, "b1i_par.hex"}, parm);
                    $readmemh({`GS_VECDIR, "b1i_st.hex"}, stm);
                    $readmemh({`GS_VECDIR, "b1i_term.hex"}, expm);
                end
                default: begin
                    $readmemh({`GS_VECDIR, "pair_par.hex"}, parm);
                    $readmemh({`GS_VECDIR, "pair_st.hex"}, stm);
                    $readmemh({`GS_VECDIR, "pair_term.hex"}, expm);
                end
            endcase
        end
    endtask

    // one sample: T consecutive slot clocks on bank `bk`; with ld set, the
    // records of block blk+1 are written into the shadow bank meanwhile
    task sample;
        input bk;
        input mute;
        input ld;
        input integer nch;
        input integer nxt;      // block index to load
        integer j;
        begin
            for (j = 0; j < T; j = j + 1) begin
                @(posedge clk); #1;
                s_valid = 1; s_slot = j; s_bank = bk; s_first = (j == 0);
                s_last = (j == T - 1); s_mute = mute;
                if (ld && j < nch) begin
                    l_we = 1; l_pipe = 0; l_bank = ~bk; l_slot = j;
                    l_par = parm[nxt * nch + j]; l_st = stm[nxt * nch + j]; l_st[31:0] = l_st[31:0] + 32'h0008_0000;
                end else begin
                    l_we = 0;
                end
            end
        end
    endtask

    integer id, cid, nch, nblk, n, blk, k, bi, si, tstart;
    integer total_errs = 0;
    reg bk;
    initial begin
        repeat (4) @(posedge clk);
        #1 rst = 0;
        for (id = 0; id <= 5; id = id + 1) begin
            if (`CASE_ID == 255 || `CASE_ID == id) begin
                tstart = $time;
                load_vectors(id);
                nch = (id == 4) ? 2 : 1;
                nblk = (id == 5) ? 1 : `NBLK;
                nsb = (id == 5) ? 64 : `NSAMP_BLK;
                mute_n = (id == 5) ? 32 : 0;
                errs = 0; oidx = 0; late_cnt = 0; chk_en = 0;
                // clear both banks, then load block 0 into bank 0
                for (bi = 0; bi < 2; bi = bi + 1)
                    for (si = 0; si < 8; si = si + 1)
                        load(bi[0], si[2:0], {`GS_PAR_W{1'b0}}, {`GS_ST_W{1'b0}});
                for (si = 0; si < nch; si = si + 1) load(1'b0, si[2:0], parm[si], stm[si]);
                idle(300);
                chk_en = 1;
                for (n = 0; n < nblk * nsb; n = n + 1) begin
                    blk = n / nsb; k = n % nsb;
                    bk = blk[0];
                    sample(bk, (id == 5) && (n < 32),
                           (k == 100) && (blk + 1 < nblk), nch, blk + 1);
                    if (`GAP && (n % 997) == 996) idle(3);
                end
                idle(30);
                chk_en = 0;
                if (oidx != nblk * nsb) begin
                    errs = errs + 1;
                    $display("output count %0d != %0d", oidx, nblk * nsb);
                end
                if (late_cnt != 0) begin
                    errs = errs + 1;
                    $display("o_chip_late pulses: %0d", late_cnt);
                end
                $display("case %0d done: errs=%0d samples=%0d T=%0d", id, errs, oidx, T);
                total_errs = total_errs + errs;
            end
        end
        if (total_errs == 0) $display("PASS");
        else $display("FAIL %0d", total_errs);
        $finish;
    end
endmodule
