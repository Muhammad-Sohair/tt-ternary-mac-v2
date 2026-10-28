# v2 Step 2 — W25Q128JV quad-read timing, verified against the datasheet

Source: Winbond W25Q128JV datasheet, Revision F (27 Mar 2018), 78 pages, downloaded from winbond.com
on 2026-10-28. The part on the TT QSPI Pmod is W25Q128JVSIM (`mole99/qspi-pmod` schematic). Section
numbers below are the datasheet's.

## Command chosen: Fast Read Quad Output (6Bh)

| item | datasheet | § |
|---|---|---|
| sequence | `/CS` low → 8-bit instruction 6Bh on IO0 → 24-bit address on IO0 (MSB first) → **8 dummy clocks** → data on IO0–IO3, 2 clocks per byte | 8.2.9, Fig. 20; instruction table "Fast Read Quad Output 6Bh … Dummy" |
| nibble order | IO3..IO0 = bit 7..4 on the first clock, bit 3..0 on the second (Fig. 20: IO0 carries 4, 0; IO3 carries 7, 3) | Fig. 20 |
| direction | instruction and address are inputs on IO0 only; IO1–IO3 may stay high-Z. **All IOs must be high-Z before the falling edge of the first data-out clock** | 8.2.9 |
| continuous read | the address auto-increments after each byte; "the entire memory can be accessed with a single instruction as long as the clock continues"; ended by `/CS` high | 8.2.6 (Read Data; 6Bh is described as similar) |
| prerequisite | QE bit (SR2 bit S9) must be 1. QE is **0** from the factory on "IM"/"JM" parts; the TT store Pmods ship with QE set, TT's guide shows how to set it otherwise | 7.1.4; TT QSPI guide |
| SPI mode | Mode 0 and Mode 3 supported; input latched on CLK rising, output shifted on CLK falling | 6.1, 8.2.6 |
| address alignment | AC table note 6: "4-bytes address alignment for Quad Read" | 9.6 note 6 |

Why 6Bh and not EBh: EBh sends the address on all four IOs and needs a mode byte (M7–M0 = Fxh) and a
direction switch earlier in the command. 6Bh only turns IO0 around once, after the address. The command
overhead (8 + 24 + 8 = 40 clocks) is paid once per matrix, so EBh's shorter header buys nothing here.

## AC limits (§9.6, 3.0–3.6 V)

| parameter | symbol | limit | what it means for us |
|---|---|---|---|
| clock frequency (6Bh) | FR | ≤ 133 MHz | not a constraint: TT outputs are ≤ ~33 MHz |
| clock high / low time | tCLH, tCLL | ≥ 45 % of 1/fc(max) = 3.4 ns | a 50 % duty SCK from a divider is fine |
| /CS setup before first CLK rise | tSLCH | ≥ 3 ns | ≥ 1 clk of our 20 ns clock |
| /CS hold after last CLK | tCHSH / tCHSL | ≥ 3 ns | ≥ 1 clk |
| /CS high between reads | tSHSL1 | ≥ 10 ns | ≥ 1 clk |
| data-in setup / hold | tDVCH / tCHDX | ≥ 1 ns / ≥ 2 ns | we change IO0 on SCK falling edges: half a period of margin each way |
| clock low → output valid | tCLQV | ≤ 6 ns | see the read-timing budget |
| output hold | tCLQX | ≥ 1.5 ns | see the read-timing budget |
| output disable | tSHQZ | ≤ 7 ns | turnaround after `/CS` high before reusing the bus |

## Read-timing budget, and the decision it forces

Data leaves the flash ≤ 6 ns after an SCK falling edge **at the flash**. Our SCK falling edge is created by
a flop inside the chip and has to cross the TT output mux, a pad, the demoboard and the Pmod; the data then
comes back through the same chain. The v1 notes record the TT mux round trip as ~20 ns
(tinytapeout.com/specs). So data is valid at our input flop roughly **26 ns after the internal clock edge
that drops SCK** `[ESTIMATE]`, and stays valid until about 1.5 ns + the minimum round trip after the *next*
falling edge.

With clk = 50 MHz and SCK = clk/2 (40 ns period):

- sampling at the clock edge that raises SCK (20 ns after the fall) would **miss** the data (needs ~26 ns);
- sampling one full SCK period after the fall (40 ns) leaves ~14 ns of setup margin, and the hold is
  protected because the next falling edge only reaches the flash after the output path delay.

The round-trip delay on real silicon is not known precisely `[UNKNOWN]` and the Pmod even has an optional
22 pF "CK" delay. So the RTL will **not hard-code** the sample point. Decision for A:

1. **Programmable SCK divider**: clk/2, /4, /8, /16 (reset default **/4**, i.e. 12.5 MHz, ~6 MB/s).
2. **Programmable sample delay**: the nibble is captured 1…4 clk after the internal falling-edge event
   (reset default chosen for the /4 divider in the cocotb model).
3. The cocotb flash model includes a configurable output delay, so the tests sweep delays and prove
   which divider/sample settings work for which round trip.
4. Matrix start addresses in the flash image are aligned to 256 bytes (covers note 6's 4-byte alignment).

This costs a few flops and makes the silicon calibratable from the devkit: the bring-up script reads a
known pattern from flash at each setting and picks the fastest that passes.
