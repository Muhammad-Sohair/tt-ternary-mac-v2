## How it works

TMAC2 is a 4 × 5 array of ternary (1.58-bit) processing elements that **fetches its own weights from external
QSPI flash**. Each weight is −1, 0 or +1, so a "multiply" is an add, a subtract or nothing, and the array has
no multipliers. It carries an FPGA accelerator (Zynq-7020, 512 PEs) onto SKY130, with the same weight format
and the same requantisation contract as the FPGA datapath. It is a superset of TMAC (v1): every v1 command
works unchanged, so the chip is fully usable without the flash.

- **Weights** are packed 5 per byte in base 3 (`byte = Σ (w_k+1)·3^k`, lowest index first; 1.6 bits per
  weight). One row of 5 weights is exactly one byte, and the whole array is 4 bytes. Bytes 243–255 are
  illegal: they load as zeros and set the sticky `ILLEGAL` flag. Weights come either over SPI (`LOAD_W`) or
  from the flash stream.
- **Activations** are 5 signed int8 values shared by all four rows.
- **Compute** forms `dot_r = Σ_c w[r][c]·a[c]` for all rows (pipelined over two clocks) and adds it into a
  24-bit saturating accumulator (`CMP`), or overwrites it (`CMP_CLR`). Saturation clamps (never wraps) and sets
  a sticky per-row flag. 24 bits are exact for any reduction length K ≤ 65,535.
- **Readout** returns `sat16(acc >>> shift)` per row: an arithmetic right shift (floor division, so −1 >>> 1 =
  −1), then a clamp to int16, with `shift` = 0–31 set by command. This is the FPGA's requantisation.
  `READ_RAW` returns the exact 24-bit values.
- **BIST** runs 32 LFSR-generated rounds through the real datapath and compares a 16-bit signature with the
  value computed by the Python reference model. PASS/DONE appear on output pins.

### Flash weight stream (new in TMAC2)

The chip is a QSPI master for the W25Q128JV flash on the Tiny Tapeout QSPI Pmod, plugged into the
bidirectional Pmod header. It uses Fast Read Quad Output (6Bh) and keeps one read open for a whole matrix:
the flash auto-increments, and SCK simply stops between chunks.

A weight matrix W (N × K) is stored in flash in the order the chip consumes it (`test/tmac_model.py`,
`pack_stream`): rows in groups of 4, K in chunks of 5, and for each group, for each chunk, the 4 packed bytes
of that 4 × 5 block. N and K are padded with zero weights; the host pads the activations with zeros.
Streams start on 256-byte boundaries.

y = W·x then runs as: `FSTART page`, then for each row group: `CLEAR`, `LOAD_FC` with the K activations,
`READ_ACC` (or `READ_RAW`) for 4 outputs. After every 5th activation of `LOAD_FC` the chip computes as soon
as the matching 4 weight bytes are in the array, then fetches the next 4 bytes while the host sends the next
activations. `uo[1]` (FLASH_READY) is high when a chunk is loaded and waiting.

The flash timing is calibratable: `FCFG` sets the SCK divider (clk/2, /4, /8, /16; reset /4 = 12.5 MHz at
50 MHz) and the sample point (1–4 clk after the clock that drops SCK; reset 2 clk = 40 ns), to absorb the
pad and mux round trip on real silicon. Activations must arrive slower than chunks; with the SPI limit below
this always holds. If an activation arrives while a compute is still waiting for weights, the sticky
`OVERRUN` flag is set.

### SPI protocol

Mode 0 (CPOL=0, CPHA=0), MSB first, one command per CS_n frame: an opcode byte, then data bytes. While the
opcode byte is clocked in, MISO returns the status byte. **f_SCK ≤ f_clk/16.** Keep CS_n low ≥ 8 clk
before the first SCK edge, and high ≥ 8 clk between frames.

| op | command | data |
|---|---|---|
| 0x00 | STATUS | out: per-row SAT flags (bit r = row r), then status |
| 0x10 | LOAD_W | in: 4 packed bytes, row 0 first (byte r = row r, columns 0–4) |
| 0x20 | LOAD_A | in: 5 int8 activations, a[0] first |
| 0x21 | LOAD_AC | in: activations; after every 5th byte, compute (accumulate). Stream any number of vectors |
| 0x22 | LOAD_FC | in: activations; after every 5th byte, compute with the next weight chunk from flash |
| 0x30 | CMP | acc += W·a |
| 0x31 | CMP_CLR | acc = W·a |
| 0x40 | READ_ACC | out: 4 × int16 `sat16(acc >>> shift)`, row 0 first, MSB first |
| 0x41 | READ_RAW | out: 4 × 24-bit accumulators, MSB first |
| 0x50 | READ_W | out: 5 bytes of stored 2-bit codes (−1=11, 0=00, +1=01), 4 per byte, lowest index in LSBs |
| 0x60 | BIST | start the self-test (busy for 384 clk); closes an open flash stream |
| 0x61 | READ_SIG | out: 16-bit BIST signature, MSB first (expected 0xA97E) |
| 0x70 | CLEAR | zero accumulators, SAT, RSAT, ILLEGAL, PEND and OVERRUN |
| 0x80 | SHIFT | in: one byte, readout shift = bits 4:0 |
| 0x90 | FCFG | in: one byte, bits 1:0 SCK divider (0 = /2 … 3 = /16), bits 3:2 sample delay − 1 |
| 0x91 | FSTART | in: two bytes, flash address bits 23:16 then 15:8; opens the stream and loads the first chunk |
| 0x92 | FSTOP | close the stream (flash /CS high) |
| 0x93 | FSTAT | out: fetch status, then config (`{0000, samp, div}`, reset 0x05) |

Other opcodes do nothing and return status. Status byte: `bit7 = 1, bit6 BIST_BUSY, bit5 BIST_DONE,
bit4 BIST_PASS, bit3 ILLEGAL, bit2 ANY_SAT, bit1 RSAT (sticky: a READ_ACC returned a clamped value since the
last CLEAR), bit0 = 1`. After reset it reads 0x81. Fetch status byte: `bit7 OPEN (flash /CS low), bit6 BUSY,
bit5 READY, bit4 PEND (a compute is waiting for weights), bit3 OVERRUN, bits 2:0 = 010`; 0x02 after reset.
Reset takes effect 2 clocks after `rst_n` (synchroniser).

## How to test

1. Reset, then read STATUS: you should get 0x81. FSTAT should read 0x02, 0x05.
2. Pulse `ui[3]` (BIST_START). After ~384 clocks `uo[2]` (DONE) and `uo[3]` (PASS) go high. This needs no SPI.
3. Over SPI, try a hand-checkable example. Weights: `LOAD_W F2 79 79 79` (0xF2 = 242 = all +1; 0x79 = 121 =
   all 0). Activations 1…5. Then `CMP_CLR` and `READ_RAW`: row 0 = 15, rows 1–3 = 0.
4. Flash: write the same four bytes `F2 79 79 79` at flash address 0x000100 with the TT Flasher. Then
   `FSTART 00 01`, `CLEAR`, `LOAD_FC 01 02 03 04 05`, `READ_RAW`: row 0 = 15, rows 1–3 = 0. If the
   weights read back wrong (`READ_W`), try other `FCFG` settings: a slower divider first, then the sample delay.
5. The repository's `test/tmac_model.py` is the bit-exact reference for anything else.

## External hardware

The Tiny Tapeout QSPI Pmod (W25Q128JV flash with the Quad Enable bit set, as shipped by the TT store) on the
bidirectional Pmod header, for the flash stream. Without it the chip works as TMAC v1. The two PSRAM chip
selects (`uio[6]`, `uio[7]`) are driven high so the RAMs stay off the bus. The devkit's RP2040 drives SPI and
the BIST pin and must leave the `uio` pins as inputs while the chip owns the Pmod.
