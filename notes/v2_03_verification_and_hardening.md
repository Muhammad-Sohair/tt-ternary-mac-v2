# v2 Step 3 — A: verification and hardening log

Design: `tt_um_msohair_tmac2` = v1 datapath + `qspi_fetch.v` (Fast Read Quad Output from the QSPI Pmod
flash). Tiles 2x2. Flow: LibreLane 3.0.14 via `tt-gds-action@ttsky26d`, sky130A, 50 MHz.

## Verification

**RTL: 14/14** (the 9 v1 tests unchanged, plus 5 flash tests; `test/README.md`). The flash tests run a
behavioural W25Q128JV that checks the 6Bh protocol (instruction, address, IO0 released before data, PSRAM
chip selects high, mode 0, /CS gap) and puts **random garbage on the data lines between t_hold and t_valid
after every SCK fall**, so a wrong sample point reads wrong data rather than passing by luck.

`test_flash_timing_windows` checks 10 (divider, sample delay, round-trip) cases against the timing budget of
notes/v2_02, **in both directions**: the 5 settings the budget calls safe read correctly, and the 5 it calls
unsafe read garbage. So the test can tell a good sample point from a bad one.

### Bugs and mistakes found along the way

1. **Testbench: the flash model swallowed its first pin change.** It used the first value-change event as
   its baseline. When FSTART was the first command, that event was the /CS fall, so the model never saw the
   command and the data lines stayed 0 (every weight decoded as −1, all rows equal). Fix: take the baseline
   from the pins at construction. RTL was not involved.
2. **My first mutation batch was invalid.** The harness ran `make` with the caller's `PWD`, so every build
   failed with "file not found", and the script counted each failure as a caught mutant: 10/10 "caught",
   including mutants that cannot affect the test that "caught" them. Adding a **no-mutation control**
   exposed it (the control "failed" too). Fixed (PWD = the test dir, forward slashes), re-validated with the
   control, re-run. Lesson kept: a mutation batch without a passing control proves nothing.
3. **Area:** the first fetcher was 3,318 µm². Selecting IO0 bits straight from the page register instead of
   copying them into a 32-bit shift register, and dropping output registers that the core's registered
   write port already provides, brought it to 1,929 µm² (whole design +11 % over v1 instead of +16 %).

### Mutation batch (shipped RTL, control passes all 5 flash tests)

| mutant | caught by | message |
|---|---|---|
| instruction 6Bh → 6Ch | test_flash_matvec | flash protocol: unexpected instruction 0x6c |
| nibble order swapped | test_flash_matvec | raw acc [-43, -198, 76, -51] != model [-95, -123, 20, -260] |
| 7 dummy clocks instead of 8 | test_flash_matvec | raw acc [-200, -362, -201, -94] != model […] |
| sample point one clk early | test_flash_timing_windows | div 0 samp 1 t_valid 26.0: read bad, budget predicts ok |
| compute does not wait for weights | test_flash_wait_and_overrun | after one vector: FSTAT 0x02 |
| OVERRUN never set | test_flash_wait_and_overrun | after an extra activation: FSTAT 0x12 |
| IO0 not released for the dummy clocks | test_flash_matvec | IO0 still driven by the chip during data out |
| PSRAM A /CS driven low | test_flash_pins_reset | uio_out 0x81 |
| write address steps on every nibble | test_flash_matvec | raw acc [0, 20, 0, -260] != model […] |
| next chunk one nibble short | test_flash_matvec | FLASH_READY never rose |

**10 of 10 caught.** The v1 mutation batch (8/8) covers the unchanged datapath.

**Gate level: 14/14** on the run-a netlist (`final/pnl`, UNIT_DELAY, sky130_fd_sc_hd models), including
the 24-bit internal-saturation test (`TMAC_GL_SLOW=1`, 10 min). CI `gl_test` passes on every run.

## Hardening

| run | change | util | setup WNS | hold WNS | max-slew pins nom_ss / max_ss | fanout | DRC/LVS/ant |
|---|---|---|---|---|---|---|---|
| 1 | v1's V7 flow settings | 63.3 % | +4.88 | +0.102 | 5 / 10 | 3 | 0/0/0 |
| a | post-GRT repair slew margin 40 → 55 % | 63.8 % | +4.88 | +0.103 | **0 / 5** | 2 | 0/0/0 |
| b | `nor2_1` excluded | 63.3 % | +4.84 | +0.099 | 5 / 5 | 2 | 0/0/0 |
| c | run 1 at **3x2** | 42.2 % | +5.25 | +0.108 | 16 / 20 | 2 (+1 cap) | 0/0/0 |

**Run 1's slew violation, diagnosed.** One net, 0.31 ns over at nom ss: a decoder output bit
(`u_core.u_dec` code for column 3) fanning out to the weight registers of all four rows (`w[3]`, `w[8]`,
`w[13]`, `w[18]`). Through mid-PnR it was driven by a `nor2_2` with no violation. The post-global-route
repair step (41) then **downsized it to `nor2_1`**, to relieve a violation on the net feeding it. With the
40 % margin the global-route estimate (0.43 ns) still looked safe, but detailed routing made the wire
longer and the real slew 1.06 ns. Raising the post-GRT repair margin to 55 % (run a) stops that downsizing.
`nor2_1` exclusion (b) shifts the problem to the sibling bit.

**3x2 is worse, not better** (c): as with v1's fallback, a bigger die spreads the same logic and lengthens
the wires. More tiles would cost 140 € and buy more violations.

**Reproducibility.** Run a's setting was committed to `main` (one key changed:
`GRT_DESIGN_REPAIR_MAX_SLEW_PCT` 40 → 55) and re-run in CI (run 40677816085): **0 of 318 metrics differ**
from eval run 40673894596, the LEF is identical, and the GDS is identical once its 91 timestamp fields
(BGNLIB/BGNSTR dates) are masked.

All runs: setup and hold met at every corner (0 violations), Magic DRC 0, routing DRC 0, LVS 0, antenna 0,
precheck passes.

### Residuals in run a (adopted on `main`)

1. **Max slew, 1 data net, max_ss only.** `_4435_` (`a21oi_2`) → 4 × `a22o_2` → `w[3]/w[8]/w[13]/w[18]`, the
   decoder code bit for column 3: **0.8006 ns vs 0.75 ns (+50.5 ps) at max_ss_100C_1v60; 0 at nom_ss and
   at every other corner.** Data, not clock or reset. These registers are written by a registered write
   port and only read one or more cycles later, and the worst setup slack is +4.88 ns, so 50 ps of extra
   transition time is functionally negligible. v1's accepted residual was +4.8 ps nom ss / +47.5 ps max ss.
2. **Max fanout 16 > 8 on the clock root** (`clkbuf_0_clk`, a `clkbuf_16`), and 9 > 8 on one data fanout
   buffer (`fanout407`). The limit of 8 is our own `MAX_FANOUT_CONSTRAINT`, not a library limit. CTS chose
   the strongest clock buffer for the root and drives 16 second-level buffers (4 levels, 439 sinks clustered
   to 60). **Root output slew 0.294 ns at max_ss (limit 0.75), 0 cap violations, 0 clock slew violations
   at every corner.** v1 had 0 fanout violations, so this is new, and it is on the clock net: flagged to
   the owner for a decision, not accepted by me.
