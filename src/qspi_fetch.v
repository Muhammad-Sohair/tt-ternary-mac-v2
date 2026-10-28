/*
 * Copyright (c) 2026 Muhammad Sohair Khan
 * SPDX-License-Identifier: Apache-2.0
 *
 * Weight fetcher for a W25Q128JV QSPI flash (TT QSPI Pmod), Fast Read Quad
 * Output (6Bh): /CS low, 8-bit instruction and 24-bit address on IO0, 8 dummy
 * clocks, then one nibble per SCK on IO3..IO0, high nibble first. The flash
 * auto-increments the address, so one command streams a whole weight matrix;
 * SCK simply stops (low, SPI mode 0) whenever no chunk is wanted.
 * Timing source: notes/v2_02_flash_timing.md.
 *
 *   start    : (re)issue the command at {page, 8'h00} and fetch the first chunk
 *   next     : fetch the next chunk (CHUNK bytes) from the open stream
 *   stop     : raise /CS
 *
 * Each received byte is presented for one cycle on we/waddr/wbyte, with
 * waddr = 0..CHUNK-1 (combinational: the core registers its write port).
 * ready = a full chunk has been delivered and no fetch is in progress.
 *
 * SCK = clk / (2 * 2^div). The IO pins are sampled by a free-running input
 * register; a nibble launched by an SCK falling edge is taken from the
 * sample made (samp + 1) clk periods after the clock edge that drives SCK
 * low. The sample point can therefore be moved to match the pad/mux round
 * trip measured on silicon.
 */

`default_nettype none

module qspi_fetch #(
    parameter CHUNK = 4                    // bytes per fetch = weight bytes per array load
) (
    input  wire        clk,
    input  wire        rst,

    input  wire        start,
    input  wire        next,
    input  wire        stop,
    input  wire [15:0] page,               // address[23:8]; must not change while /CS is low
    input  wire [1:0]  div,                // SCK = clk/2, /4, /8, /16
    input  wire [1:0]  samp,               // sample 1..4 clk after the falling-edge clock

    output wire        we,
    output reg  [$clog2(CHUNK)-1:0] waddr,
    output wire [7:0]  wbyte,
    output wire        busy,
    output wire        ready,
    output wire        open_,              // /CS is low

    output wire        sck,
    output wire        csn,
    output wire        io0_out,
    output wire        io0_oe,
    input  wire [3:0]  io_in               // {IO3, IO2, IO1, IO0}
);

  localparam [7:0] CMD = 8'h6B;

  localparam [2:0] S_IDLE  = 3'd0,
                   S_GAP   = 3'd1,         // /CS high between commands (tSHSL1)
                   S_CMD   = 3'd2,         // instruction + address, 32 clocks
                   S_DUMMY = 3'd3,         // 8 dummy clocks
                   S_DATA  = 3'd4,         // SCK running, nibbles arriving
                   S_DRAIN = 3'd5,         // SCK stopped, waiting for the last nibble
                   S_HOLD  = 3'd6;         // chunk delivered, SCK stopped, /CS low

  reg [2:0]  state;
  reg        sck_q, csn_q, oe_q;
  reg [5:0]  bitn;                         // clocks left in S_CMD / S_DUMMY
  reg [2:0]  hcnt;                         // clk cycles left in this SCK phase
  reg [3:0]  nib_out;                      // falling edges still to produce in S_DATA
  reg [3:0]  nib_in;                       // nibbles still to capture
  reg        hi_half;                      // next captured nibble is a high nibble
  reg [3:0]  hi_nib;
  reg [4:0]  evs;                          // evs[k]: a launching fall happened k clk ago
  reg [3:0]  in_q;                         // free-running input register

  wire [2:0] half = (3'd1 << div) - 3'd1;  // clk cycles per SCK phase, minus 1
  wire       tick = (hcnt == 3'd0);
  wire       fall_now = tick && sck_q;     // SCK goes low at this edge
  // evs[k] is set k clk after the launching edge; in_q then holds the pin
  // value sampled k clk after it. (evs[0] would sample before SCK fell.)
  wire       grab = evs[samp + 3'd1] && (nib_in != 4'd0);

  // A falling edge in S_DATA launches a nibble. The 8th dummy clock's fall
  // launches the first one, so the count starts there.
  wire launch = fall_now && ((state == S_DATA) || (state == S_DUMMY && bitn == 6'd1));

  // The command word is never copied: IO0 picks bit (bitn - 1) of it, and
  // bitn steps down on each SCK fall. Address bits [7:0] are always zero.
  wire [31:0] cmd_word = {CMD, page, 8'h00};

  assign sck     = sck_q;
  assign csn     = csn_q;
  assign io0_out = cmd_word[bitn[4:0] - 5'd1];
  assign io0_oe  = oe_q;
  assign busy    = (state != S_IDLE) && (state != S_HOLD);
  assign ready   = (state == S_HOLD);
  assign open_   = ~csn_q;
  assign we      = grab && !hi_half;
  assign wbyte   = {hi_nib, in_q};

  always @(posedge clk) in_q <= io_in;

  always @(posedge clk) begin
    evs <= {evs[3:0], launch};
    if (rst || stop) begin
      state   <= S_IDLE;
      sck_q   <= 1'b0;
      csn_q   <= 1'b1;
      oe_q    <= 1'b0;
      hcnt    <= 3'd0;
      evs     <= 5'd0;
      nib_out <= 4'd0;
      nib_in  <= 4'd0;
      hi_half <= 1'b1;
      waddr   <= 0;
      if (rst) begin bitn <= 6'd0; hi_nib <= 4'd0; end
    end else if (start) begin
      // Restart: raise /CS for one SCK phase, then issue the new command.
      state   <= S_GAP;
      sck_q   <= 1'b0;
      csn_q   <= 1'b1;
      oe_q    <= 1'b0;
      hcnt    <= half;
      evs     <= 5'd0;
      nib_in  <= 4'd0;
      waddr   <= 0;
    end else begin
      if (hcnt != 3'd0) hcnt <= hcnt - 3'd1;

      // capture: one nibble per launching fall, (samp + 1) clk later
      if (grab) begin
        nib_in  <= nib_in - 4'd1;
        hi_half <= ~hi_half;
        if (hi_half) hi_nib <= in_q;
        else waddr <= (waddr == CHUNK - 1) ? 0 : waddr + 1'b1;
      end

      case (state)
        S_GAP: if (tick) begin
          // /CS low with IO0 = first bit; the first rise comes one phase later (tSLCH)
          state <= S_CMD;
          csn_q <= 1'b0;
          oe_q  <= 1'b1;
          bitn  <= 6'd32;
          hcnt  <= half;
        end
        S_CMD: if (tick) begin
          sck_q <= ~sck_q;
          hcnt  <= half;
          if (sck_q) begin                 // falling edge: next bit, or release IO0
            bitn <= bitn - 6'd1;
            if (bitn == 6'd1) begin
              state <= S_DUMMY;
              oe_q  <= 1'b0;               // IOs high-Z well before the first data fall
              bitn  <= 6'd8;
            end
          end
        end
        S_DUMMY: if (tick) begin
          sck_q <= ~sck_q;
          hcnt  <= half;
          if (sck_q) begin
            bitn <= bitn - 6'd1;
            if (bitn == 6'd1) begin        // this fall launched nibble 0
              state   <= S_DATA;
              nib_out <= 2 * CHUNK - 1;
              nib_in  <= 2 * CHUNK;
              hi_half <= 1'b1;
            end
          end
        end
        S_DATA: if (tick) begin
          if (!sck_q && nib_out == 4'd0) begin
            state <= S_DRAIN;              // all launching falls produced; SCK stays low
          end else begin
            sck_q <= ~sck_q;
            hcnt  <= half;
            if (sck_q) nib_out <= nib_out - 4'd1;
          end
        end
        S_DRAIN: if (nib_in == 4'd0) state <= S_HOLD;
        S_HOLD: if (next) begin
          // Resume the stream. The flash still drives the last nibble; the
          // next rise/fall pair launches the next one.
          state   <= S_DATA;
          nib_out <= 2 * CHUNK;
          nib_in  <= 2 * CHUNK;
          hi_half <= 1'b1;
          hcnt    <= half;
        end
        default: ;
      endcase
    end
  end

endmodule
