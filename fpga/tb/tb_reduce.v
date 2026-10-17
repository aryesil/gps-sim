`timescale 1ns/1ps
`include "gs_defs.vh"
// tb_reduce: gs_reduce vs inline reference (random, extreme, ties, saturation,
// shift 0..8, back-to-back / every-4 / random gaps). Prints PASS or FAIL <n>.
module tb_reduce;
    localparam P = `GS_P;
    localparam NMAX = 400000;

    reg clk = 0, rst = 1;
    always #2 clk = ~clk;

    reg                  in_valid = 0;
    reg  [P*38-1:0]      in_i = 0, in_q = 0;
    reg  [7:0]           shift = 0;
    wire                 out_valid;
    wire signed [15:0]   out_i, out_q;
    wire [1:0]           clipped_inc;

    gs_reduce dut (.clk(clk), .rst(rst), .in_valid(in_valid), .in_i_all(in_i),
                   .in_q_all(in_q), .shift(shift), .out_valid(out_valid),
                   .out_i(out_i), .out_q(out_q), .clipped_inc(clipped_inc));

    localparam LAT = dut.LAT_REDUCE;

    reg signed [15:0] exp_i [0:NMAX-1];
    reg signed [15:0] exp_q [0:NMAX-1];
    reg [1:0]         exp_c [0:NMAX-1];
    integer wr = 0, rd = 0, errors = 0, seed = 12345, gapmode = 0;
    reg [127:0] vd = 0;

    // reference
    task ref_comp;
        input  [P*38-1:0] vec;
        input  [7:0]      sft;
        output reg signed [15:0] o;
        output integer           clip;
        integer p, sh;
        reg signed [63:0] sum, v;
        reg signed [37:0] e;
        begin
            sum = 0;
            for (p = 0; p < P; p = p + 1) begin
                e = vec[p*38 +: 38];
                sum = sum + e;
            end
            sh = (sft > 7) ? 7 : sft;
            v = (sum + (64'sd1 <<< (20 + sh))) >>> (21 + sh);
            clip = 0;
            if (v > 32767) begin o = 16'sh7FFF; clip = 1; end
            else if (v < -32768) begin o = 16'sh8000; clip = 1; end
            else o = v[15:0];
        end
    endtask

    // send one sample (drive at negedge, hold one clock), then gap
    task send;
        input [P*38-1:0] vi;
        input [P*38-1:0] vq;
        reg signed [15:0] oi, oq;
        integer ci, cq, g, r;
        begin
            @(negedge clk);
            in_valid = 1; in_i = vi; in_q = vq;
            ref_comp(vi, shift, oi, ci);
            ref_comp(vq, shift, oq, cq);
            exp_i[wr] = oi; exp_q[wr] = oq; exp_c[wr] = ci + cq; wr = wr + 1;
            @(negedge clk);
            in_valid = 0;
            in_i = {P*38{1'bx}}; in_q = {P*38{1'bx}};
            case (gapmode)
                0: g = 0;
                1: g = 3;
                default: begin r = $random(seed); g = (r & 32'h7fffffff) % 6; end
            endcase
            repeat (g) @(negedge clk);
        end
    endtask

    task flush; begin repeat (LAT + 4) @(negedge clk); end endtask

    // output checker
    always @(negedge clk) begin
        vd <= {vd[126:0], in_valid};
        if (!rst) begin
            if (out_valid !== vd[LAT-1]) begin
                errors = errors + 1;
                if (errors < 10) $display("mismatch: out_valid=%b expected %b t=%0t", out_valid, vd[LAT-1], $time);
            end
            if (out_valid === 1'b1) begin
                if (rd >= wr) begin errors = errors + 1; $display("mismatch: unexpected output"); end
                else begin
                    if (out_i !== exp_i[rd] || out_q !== exp_q[rd] || clipped_inc !== exp_c[rd]) begin
                        errors = errors + 1;
                        if (errors < 10) $display("mismatch #%0d: i=%0d/%0d q=%0d/%0d c=%0d/%0d sh=%0d", rd,
                            out_i, exp_i[rd], out_q, exp_q[rd], clipped_inc, exp_c[rd], shift);
                    end
                    rd = rd + 1;
                end
            end else if (clipped_inc !== 2'd0) begin
                errors = errors + 1; $display("mismatch: clipped_inc without valid");
            end
        end
    end

    // random vector generators
    function [P*38-1:0] rnd_vec;
        input integer mode;
        integer p, r, sc;
        reg [37:0] x;
        reg [P*38-1:0] acc;
        begin
            acc = 0;
            for (p = 0; p < P; p = p + 1) begin
                x = {$random(seed), $random(seed)};
                sc = ($random(seed) & 32'h7fffffff) % 38;
                if (mode == 0) x = $signed(x) >>> sc;           // varied magnitude
                else if (mode == 1) x = $signed(x) >>> 20;
                acc[p*38 +: 38] = x;
            end
            rnd_vec = acc;
        end
    endfunction

    // vector whose sum is exactly t (t fits 38 bits), others random small
    function [P*38-1:0] split_vec;
        input signed [63:0] t;
        input integer noisy;
        integer p;
        reg signed [63:0] rem, x;
        reg [P*38-1:0] acc;
        begin
            acc = 0; rem = t;
            if (noisy) for (p = 1; p < P; p = p + 1) begin
                x = $random(seed); x = x >>> 4;       // ~28-bit
                acc[p*38 +: 38] = x[37:0];
                rem = rem - x;
            end
            // rem must fit 38-bit signed, else fall back to no noise
            if (rem > 64'sd137438953471 || rem < -64'sd137438953472) begin
                acc = 0; rem = t;
            end
            acc[37:0] = rem[37:0];
            split_vec = acc;
        end
    endfunction

    integer n, k, sh, m, d;
    reg signed [63:0] t, kk;
    reg [P*38-1:0] ev, vmax, vmin, vzero;
    integer p;
    localparam signed [37:0] MAXV = 38'sh1FFFFFFFFF;   // 2^37-1
    localparam signed [37:0] MINV = 38'sh2000000000;   // -2^37

    initial begin
        repeat (4) @(negedge clk);
        rst = 0; $display("GS_P=%0d LAT_REDUCE=%0d", P, LAT);
        repeat (2) @(negedge clk);

        // --- extreme vectors, every shift, all gap modes
        for (gapmode = 0; gapmode < 3; gapmode = gapmode + 1)
        for (sh = 0; sh <= 8; sh = sh + 1) begin
            shift = sh;
            for (p = 0; p < P; p = p + 1) begin
                vmax[p*38 +: 38] = MAXV; vmin[p*38 +: 38] = MINV; vzero[p*38 +: 38] = 0;
            end
            send(vmax, vmax);
            send(vmin, vmin);
            send(vmax, vmin);
            send(vmin, vmax);
            send(vzero, vzero);
            flush;
        end

        // --- ties and saturation boundaries
        gapmode = 0;
        for (sh = 0; sh <= 8; sh = sh + 1) begin
            shift = sh;
            m = (sh > 7) ? 7 : sh;
            for (k = -40; k <= 40; k = k + 1) begin
                // target v = k*? : sum+half exactly on a multiple of 2^(21+m), +-1 around it
                for (d = -1; d <= 1; d = d + 1) begin
                    kk = k;
                    t = (kk <<< (21 + m)) - (64'sd1 <<< (20 + m)) + d;
                    send(split_vec(t, k & 1), split_vec(-t - (d * 3), (k >> 1) & 1));
                end
            end
            // around saturation points
            for (k = -2; k <= 2; k = k + 1) begin
                for (d = -1; d <= 1; d = d + 1) begin
                    kk = 32767 + k;
                    t = (kk <<< (21 + m)) - (64'sd1 <<< (20 + m)) + d;
                    if (t < 64'sd137438953471 && t > -64'sd137438953472) begin
                        send(split_vec(t, 1), split_vec(-t, 0));
                        send(split_vec(-t - (64'sd1 <<< (21 + m)), 0), split_vec(t, 0));
                    end
                end
            end
            flush;
        end

        // --- random: varied magnitudes, all shifts, all gap modes
        for (gapmode = 0; gapmode < 3; gapmode = gapmode + 1) begin
            for (n = 0; n < 33000; n = n + 1) begin
                if (n % 4 == 0) begin flush; shift = (n >> 2) % 9; end
                send(rnd_vec(n % 3), rnd_vec((n + 1) % 3));
            end
            flush;
        end

        flush;
        if (rd != wr) begin errors = errors + 1; $display("count mismatch: rd=%0d wr=%0d", rd, wr); end
        if (errors == 0) $display("PASS"); else $display("FAIL %0d", errors);
        $finish;
    end
endmodule
